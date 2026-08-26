#!/usr/bin/env python3
"""
Local bridge for the VL53L7CX x2 + bone-conduction mic live monitor panel.

Reads the '$TOF,...' / '$MIC,...' / '$STATUS,...' / '$REC,...' lines the
firmware prints over the console UART, re-broadcasts them as Server-Sent
Events to any browser tab with panel.html open, and serves the panel itself
over plain HTTP.

Two host-triggered actions:
  - POST /switch?res=4|8   edits TOF_RESOLUTION_MODE in main/vl53l7cx_test.c,
    rebuilds, reflashes, and resumes streaming -- the ULD driver's grid size
    isn't runtime-switchable, so a resolution change is a reflash by design.
  - POST /record?seconds=N sends "REC:N\\n" to the board over the same
    serial link (no reflash needed -- main/uart_cmd.c parses it at runtime),
    the mic task pauses its live $MIC stream, captures N seconds, and dumps
    it base64-encoded between BEGIN_WAV_B64/END_WAV_B64 markers; this file
    decodes that, writes a .wav under <repo root>/voice/, and serves it back
    at /voice/<filename> for the panel's playback button.

No third-party dependencies: stdlib http.server + threading for SSE,
pyserial for the UART link (already used elsewhere in this project).

Usage:
    python3 bridge_server.py [--port /dev/ttyUSB0] [--http-port 8765]
"""

import argparse
import base64
import http.server
import json
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import socketserver
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import numpy as np

MONITOR_DIR = Path(__file__).resolve().parent
PROJECT_DIR = MONITOR_DIR.parent
ROOT_DIR = PROJECT_DIR.parent
MAIN_SRC = PROJECT_DIR / "main" / "vl53l7cx_test.c"
IDF_EXPORT = Path.home() / "esp" / "esp-idf" / "export.sh"
PANEL_HTML = MONITOR_DIR / "panel.html"
PANEL_DIR = MONITOR_DIR / "panel"
VOICE_DIR = ROOT_DIR / "voice"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

# B14：WAV -> log-Mel 備援路線。`host/` 跟這支腳本不在同一個套件樹下，
# 用之前得先把 repo 根目錄塞進 sys.path。
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# B19: these are stdlib-only (no librosa, no h5py), so unlike the mel backend
# below they can be imported eagerly -- the bridge cannot do its job without
# them any more.
from host.capture.protocol import ProtocolParser          # noqa: E402
from host.capture.dropwatch import DropTracker, tof_stream  # noqa: E402
from host.clock.align import ClockAligner                 # noqa: E402
from host.quality.metrics import QualityAggregator, ThresholdTable  # noqa: E402
from host.align.aligner import Aligner                              # noqa: E402
from host.storage.session_registry import (                         # noqa: E402
    SessionRegistry, MissingFieldsError, SessionAlreadyActiveError, NoActiveSessionError,
)
from host.storage.baseline import capture_baseline_trial            # noqa: E402
from host.clock.ping_sync import (                                  # noqa: E402
    PingSyncer, SessionClockSync, SESSION_START, SESSION_END,
)

THRESHOLDS_PATH = ROOT_DIR / "config" / "quality_thresholds.json"
LAST_SESSION_PATH = ROOT_DIR / "config" / "last_session.json"
#: Fitted PCA models for GET /pca. Nothing writes here yet -- the endpoint
#: reports "no model" rather than inventing one (see _handle_pca).
MODELS_DIR = ROOT_DIR / "models"

# Imported lazily: mel_pipeline pulls in librosa, which is a heavy optional
# dependency. The bridge's core job -- serving the panel and relaying $-lines
# over SSE -- must still start on a machine that only has pyserial, and the
# E08 fallback demo depends on that. A missing librosa degrades the MFCC
# feature to an SSE error event instead of killing the whole server.
_MEL_IMPORT_ERROR = None
wav_to_log_mel_timed = None
write_mel_to_trial = None


def _load_mel_backend():
    """Return True once the B14 mel backend is importable; cache the failure."""
    global wav_to_log_mel_timed, write_mel_to_trial, _MEL_IMPORT_ERROR
    if wav_to_log_mel_timed is not None:
        return True
    if _MEL_IMPORT_ERROR is not None:
        return False
    try:
        from host.features.mel_pipeline import wav_to_log_mel_timed as _w
        from host.storage.mel_writer import write_mel_to_trial as _t
    except ImportError as exc:
        _MEL_IMPORT_ERROR = exc
        print(f"[bridge] mel backend unavailable ({exc}); "
              f"MFCC disabled, everything else still works")
        return False
    wav_to_log_mel_timed, write_mel_to_trial = _w, _t
    return True

# B09（session/trial 狀態機）還沒做，這裡先用「每次錄音自動 +1」模擬 trial_idx，
# 讓 B07/B09 落地前就能對著一個真實 h5 檔驗證「特徵寫回 HDF5」這條路徑通不通。
# --h5-session 沒給的話就只算 mel、存 .npy，不寫 HDF5。
mfcc_target = {"h5_path": None, "next_trial_idx": 0}

RESOLUTION_RE = re.compile(r"#define\s+TOF_RESOLUTION_MODE\s+\d+")
WAV_HEADER_RE = re.compile(r"rate=(\d+) bits=(\d+) channels=(\d+) bytes=(\d+)")


class Broadcaster:
    """Fan-out of parsed events to every connected SSE client."""

    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._clients.discard(q)

    def publish(self, event):
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # slow client, drop the frame rather than block the reader thread


broadcaster = Broadcaster()

# Guards the serial port + build/flash cycle so only one is ever in flight,
# and lets the reader thread know to stand down while a flash is running.
serial_lock = threading.Lock()
flashing = threading.Event()
current_resolution = {"dim": 8}

# The live serial.Serial instance, so the /record HTTP handler (a different
# thread than the reader) can write the "REC:<n>\n" command to the board.
# Guarded separately from serial_lock, which is about the open/close and
# flash-vs-read handoff, not individual writes.
serial_write_lock = threading.Lock()
current_serial_holder = {"ser": None}


# B19: one ProtocolParser per serial connection (it carries the version
# negotiation state), plus the quality machinery that watches the same
# stream. Re-created on every reconnect so a reboot starts a clean session.
protocol_state = {"parser": None}

drop_tracker = DropTracker()
quality_thresholds = ThresholdTable(THRESHOLDS_PATH)
clock_aligner = ClockAligner()
quality = QualityAggregator(
    quality_thresholds,
    drop_tracker=drop_tracker,
    clock_aligner=clock_aligner,
)


def to_sse_event(event):
    """Translate one host.capture.protocol event into its CONTRACTS #4.2 shape.

    The wire names and the SSE names differ (`distance` vs `dist`, `log_mel`
    vs `bands`), so this is a rename layer, not a second parser -- all the
    validation already happened in protocol.py.

    Fields the firmware did not send stay absent rather than being filled
    with a default (CONTRACTS #1.1.2). A v1 line has no `seq`/`t_us` at all,
    and a panel that received `seq: 0` could not tell that apart from a real
    first frame.
    """
    kind = event.get("type")

    if kind == "tof":
        out = {"type": "tof", "sensor": event["sensor"], "dim": event["dim"],
               "dist": event["distance"], "signal": event["signal"],
               "valid": event["valid"]}
    elif kind == "mic":
        out = {"type": "mic", "rms": event["rms"], "peak": event["peak"]}
    elif kind == "mel":
        out = {"type": "mel", "bands": event["log_mel"]}
    elif kind == "heartbeat":
        out = {"type": "heartbeat", "drop_A": event["drop_A"],
               "drop_B": event["drop_B"], "drop_M": event["drop_M"],
               "heap": event["heap"], "temp_c": event["temp_c"]}
    elif kind == "status":
        # parser.state() is the seam B02 built for exactly this: it already
        # carries protocol_version / degraded / warning / recording_allowed,
        # so the panel can grey out the record button without re-deriving
        # any of it. `dim` is in there too, which keeps the event shape
        # backward-compatible with the panel's existing status handler.
        parser = protocol_state["parser"]
        out = {"type": "status"}
        if parser is not None:
            out.update(parser.state())
        else:
            out["dim"] = event.get("dim")
        return out
    elif kind == "record":
        return {"type": "record", "state": event["state"], "seconds": event["seconds"]}
    else:
        return None

    for key in ("seq", "t_us"):
        if event.get(key) is not None:
            out[key] = event[key]
    if event.get("proto") == 1:
        # Marked explicitly so the panel can label degraded data rather than
        # silently mixing it in with timestamped v2 frames.
        out["proto"] = 1
        out["has_timestamp"] = False
    return out


def handle_parsed_event(parsed):
    """Everything the bridge does with one parsed device event.

    Factored out of the reader loop because the PING burst also produces
    them: PingSyncer reads lines itself while waiting for a reply, and hands
    back the $T/$M that arrive in the meantime through `on_event`. Routing
    those through the same function is what stops a clock sync from punching
    a hole in the data stream.
    """
    if parsed.get("type") == "status" and parsed.get("dim"):
        current_resolution["dim"] = parsed["dim"]
    observe_for_quality(parsed)
    sse = to_sse_event(parsed)
    if sse:
        broadcaster.publish(sse)


def observe_for_quality(event):
    """Feed one parsed event to the drop tracker and the quality metrics."""
    kind = event.get("type")
    seq = event.get("seq")
    if kind in ("tof", "mic", "mel"):
        # One shared buffer: B10's baseline reads the last 30 s of it, and
        # B11's trial machine reads the capture window out of the same
        # history. Fed unconditionally so both are already populated when
        # their request arrives.
        t_us = event.get("t_us")
        if t_us is not None:
            device_clock["last_t_us"] = t_us
        try:
            session_aligner.push_event(event)
        except Exception as exc:
            print(f"[bridge] aligner rejected a {kind} event: {exc}")
        trial = session_runtime.get("trial")
        if trial is not None:
            try:
                trial.push_event(event)
            except Exception as exc:
                print(f"[bridge] trial machine rejected a {kind} event: {exc}")
    if kind == "tof":
        if seq is not None:
            drop_tracker.observe(tof_stream(event["sensor"]), seq)
        quality.observe_tof(event)
    elif kind == "mic":
        if seq is not None:
            drop_tracker.observe("mic", seq)
        quality.observe_mic(event)
    elif kind == "mel":
        if seq is not None:
            drop_tracker.observe("mel", seq)
        quality.observe_mel(event)
    elif kind == "heartbeat":
        quality.observe_heartbeat(event)
    elif kind == "status":
        # Arms a possible session restart; it does NOT reset the counters.
        # $STATUS is re-sent on every PING and every SENS/MEL change, so a
        # reset here would zero the gauge roughly a hundred times during
        # B05's clock calibration (CONTRACTS #1.1, amended 2026-08-26).
        drop_tracker.on_status()


def quality_emitter(interval=1.0):
    """Publishes the `quality` event at 1 Hz for the lifetime of the process.

    Independent of the serial reader on purpose: the panel's health lights
    must keep updating while the link is down or a flash is running, and a
    dashboard that freezes at the moment something goes wrong is the one
    that is least useful.
    """
    while True:
        time.sleep(interval)
        try:
            broadcaster.publish(quality.snapshot())
        except Exception as exc:  # never let a metric bug kill the stream
            print(f"[bridge] quality snapshot failed: {exc}")


def _json_safe(obj):
    """Replace NaN/Inf with None, recursively.

    json.dumps emits the bare literals NaN / Infinity by default, which are
    not valid JSON and make the browser's JSON.parse throw -- one such value
    anywhere in an event kills the whole SSE message.

    None is the right substitute rather than 0.0: a baseline zone with no
    signal has a NaN mean, and rendering that as zero would show a stable
    sensor reading where there is none. The zone flag arrays
    (no_signal_zones and friends) stay authoritative about which is which.
    """
    if isinstance(obj, float):
        return None if (obj != obj or obj in (float("inf"), float("-inf"))) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


# --- B09 / B10: session lifecycle -----------------------------------------

# Both paths are overridable from the command line so a test run does not
# write into the working tree: `sessions/*.h5` and `config/last_session.json`
# are real runtime artefacts, and a test that leaves them behind shows up as
# repo changes nobody made on purpose.
runtime_paths = {"sessions": ROOT_DIR / "sessions", "last_session": LAST_SESSION_PATH}
session_registry = SessionRegistry(LAST_SESSION_PATH)

# Fed from the serial reader for the whole life of the process, not just
# while a session is open: B10's baseline is computed from the 30 seconds
# *before* the operator presses the button, so the history has to already be
# there when the request arrives.
session_aligner = Aligner()

# Everything a running session owns beyond its metadata. `writer` stays open
# for the session's lifetime; see the note in _handle_baseline about why the
# baseline has to create it.
session_runtime = {"baseline": None, "h5_path": None, "writer": None, "trial": None}
session_lock = threading.Lock()

# Newest device timestamp seen on any stream. The trial machine needs a
# device-clock reading to mark capture boundaries (CONTRACTS #1.3 puts trial
# edges on device time), and the baseline needs one to know where "the last
# 30 seconds" ends. Host time will not do: the two clocks drift, which is
# the whole reason B04 exists.
device_clock = {"last_t_us": None}


def sessions_dir():
    d = Path(runtime_paths["sessions"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _frames_to_tof(frames, values_attr, present_attr):
    """AlignedFrame list -> (values (T,32), valid (T,16)) for B10.

    A frame with no reading for that sensor becomes all-NaN with every zone
    invalid, rather than being dropped: the baseline's zone statistics are
    per-zone over a fixed time window, so silently shortening the window for
    one sensor would make the two sensors' numbers incomparable.
    """
    values, valid = [], []
    for f in frames:
        sample = getattr(f, values_attr)
        if getattr(f, present_attr) and sample is not None:
            # TofSample.values is already the 32-wide schema layout, with
            # None in the invalid slots -- NaN is the HDF5 spelling of the
            # same thing (CONTRACTS #2), so the two agree on "no reading".
            values.append([float("nan") if v is None else float(v) for v in sample.values])
            valid.append([bool(v) for v in sample.valid])
        else:
            values.append([float("nan")] * 32)
            valid.append([False] * 16)
    return (np.array(values, dtype=np.float32).reshape(len(frames), 32),
            np.array(valid, dtype=bool).reshape(len(frames), 16))


def _baseline_payload(outcome, source):
    """BaselineOutcome -> the shape C06 draws from.

    The three zone-flag arrays are the authoritative answer to "why is this
    zone not usable", because the statistics themselves cannot carry that:
    a no-signal zone has a NaN mean, which crosses the wire as null. Null
    means "no number", not "zero" -- the panel renders those zones as N/A
    rather than as a perfectly stable 0.0 mm reading.
    """
    d = outcome.to_dict()
    quality = d.get("quality") or {}
    merged = {}
    for key in ("unstable_zones", "no_signal_zones", "suspect_zero_variance_zones"):
        # Zone indices are per-sensor 0..15, so they are reported per sensor
        # as well as merged -- a flat union would say "zone 3 is bad" without
        # saying which sensor's zone 3.
        merged[key] = {side: list((quality.get(side) or {}).get(key, []))
                       for side in ("A", "B")}
    return {
        "source": source,
        "ok": d["ok"],
        "reason": d["reason"],
        "captured_at_us": outcome_capture_time(outcome),
        "mu_A": d["baseline_mu_A"], "sigma_A": d["baseline_sigma_A"],
        "mu_B": d["baseline_mu_B"], "sigma_B": d["baseline_sigma_B"],
        "noise_floor_mu": d["noise_floor_mu"],
        "noise_floor_sigma": d["noise_floor_sigma"],
        "valid_zone_ratio": d["valid_zone_ratio"],
        "quality": quality,
        **merged,
    }


def outcome_capture_time(outcome):
    return getattr(outcome, "captured_at_us", None)


def capture_session_baseline(info, seconds):
    """Run B10's baseline over the last `seconds` of buffered device data.

    Returns ``(outcome, error)``; exactly one is None.
    """
    latest = device_clock["last_t_us"]
    if latest is None:
        frames = []
    else:
        frames = list(session_aligner.frames(latest - int(seconds * 1e6), latest))
    if len(frames) < 2:
        return None, (f"緩衝區裡沒有足夠的裝置資料（收到 {len(frames)} 幀）。"
                      f"確認鏈路是 up 的，並讓它先跑滿 {seconds} 秒。")

    tof_A, valid_A = _frames_to_tof(frames, "tof_A", "tof_A_present")
    tof_B, valid_B = _frames_to_tof(frames, "tof_B", "tof_B_present")
    tof_t_us = np.array([f.t_us for f in frames], dtype=np.int64)

    mic = [f for f in frames if f.mic_present and f.mic_rms is not None]
    if mic:
        mic_rms = np.array([f.mic_rms for f in mic], dtype=np.float32)
        mic_peak = np.array([int(f.mic_peak or 0) for f in mic], dtype=np.int16)
        mic_t_us = np.array([f.t_us for f in mic], dtype=np.int64)
    else:
        mic_rms = np.zeros(1, dtype=np.float32)
        mic_peak = np.zeros(1, dtype=np.int16)
        mic_t_us = tof_t_us[:1]

    meta_base = build_session_meta_base(info, tof_t_us)
    h5_path = session_runtime["h5_path"]
    try:
        outcome = capture_baseline_trial(
            h5_path, meta_base,
            tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
            tof_valid_A=valid_A, tof_valid_B=valid_B,
            mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
            wear_id=info.wear_id, mode=info.mode,
        )
    except Exception as exc:
        return None, f"baseline 擷取失敗: {exc}"

    outcome.captured_at_us = int(tof_t_us[-1])
    with session_lock:
        session_runtime["baseline"] = outcome
    return outcome, None


# --- B05: PING clock sync ------------------------------------------------
#
# The burst runs on the serial reader thread, not the HTTP thread, because
# PingSyncer reads lines itself and there is only one reader on the port.
# The HTTP handler just leaves a request behind and returns immediately --
# a 20-ping burst takes up to ~2 s, which is far too long to hold a request
# open, and the result is not needed until the baseline is captured anyway.
ping_request = {"label": None}
clock_sync = {SESSION_START: None, SESSION_END: None}


def request_ping_burst(label):
    ping_request["label"] = label


def run_ping_burst(ser, label):
    """Fire one burst on the reader thread. Never raises: a clock sync that
    fails must degrade to `confirmed=False`, not take the link down."""
    previous_timeout = ser.timeout

    def send():
        with serial_write_lock:
            ser.write(b"PING\n")

    def read(timeout_s):
        ser.timeout = timeout_s
        return ser.readline()

    try:
        syncer = PingSyncer(
            send, read,
            parser=protocol_state["parser"],
            on_event=handle_parsed_event,   # data lines keep flowing
        )
        burst = syncer.burst(label)
        syncer.feed_into(clock_aligner, burst)
    except Exception as exc:
        print(f"[bridge] PING burst ({label}) failed: {exc}")
        return None
    finally:
        ser.timeout = previous_timeout

    clock_sync[label] = burst
    print(f"[bridge] PING burst {label}: {burst.n_ok}/{burst.attempts} ok, "
          f"confirmed={burst.confirmed}, rtt_min={burst.rtt_min_us}us")
    broadcaster.publish({
        "type": "clock_sync", "label": label,
        "n_ok": burst.n_ok, "n_attempts": burst.attempts,
        "confirmed": burst.confirmed,
        "rtt_min_us": burst.rtt_min_us,
        "rtt_median_us": burst.rtt_median_us,
        "meets_acceptance": burst.meets_acceptance,
    })
    return burst


def _clock_meta():
    """The `/meta` clock block (CONTRACTS #2), from B04's fit and B05's bursts.

    `None` becomes -1 for the numeric fields because HDF5 attributes cannot
    hold it, and -1 is already this schema's "not measured" for the other
    time fields. `clock_sync_confirmed` stays a real boolean and is reported
    honestly: B05 tells apart a PING reply from the 1 Hz heartbeat by the
    `$STATUS` that follows it, and when it cannot get that confirmation it
    still returns the sample but marks it unconfirmed. Passing that through
    as success would be claiming a calibration nobody verified.
    """
    fit = None
    if clock_aligner.n_buckets >= 3:
        try:
            fit = clock_aligner.fit()
        except Exception:
            fit = None

    meta = {
        "clock_slope": float(fit.slope) if fit else 1.0,
        "clock_offset": float(fit.offset_us) if fit else 0.0,
        "clock_residual_p95": float(fit.residual_p95_us) if fit else -1.0,
        "clock_drift_us": -1.0,
        "clock_drift_ppm": -1.0,
        "clock_sync_span_us": -1,
        "clock_sync_confirmed": False,
        "session_start_device_us": -1,
        "session_start_host_us": -1.0,
        "session_start_rtt_min_us": -1.0,
    }

    start = clock_sync.get(SESSION_START)
    if start is None:
        return meta

    sync = SessionClockSync(start=start, end=clock_sync.get(SESSION_END))
    for key, value in sync.to_meta().items():
        if key in meta:
            meta[key] = value if value is not None else meta[key]
        else:
            meta[key] = value      # the extra diagnostics B05 also reports
    meta["clock_sync_confirmed"] = bool(meta.get("clock_sync_confirmed"))
    return meta


def build_session_meta_base(info, tof_t_us):
    """The `/meta` fields that are known at session start (CONTRACTS #2).

    The clock block comes from the live alignment fit rather than being
    stubbed: those values are what makes a recorded session verifiable
    later, so a session recorded before the fit has converged is marked
    `clock_sync_confirmed: False` instead of carrying plausible-looking
    numbers nobody checked.
    """
    parser = protocol_state.get("parser")
    state = parser.state() if parser is not None else {}
    meta = {
        "schema_version": 1,
        "subject": info.subject,
        "session_date": info.started_at[:10],
        "wear_id": info.wear_id,
        "mode": info.mode,
        "distance_mm": info.distance_mm,
        "angle_deg": info.angle_deg,
        "ambient": info.ambient,
        "notes": info.notes,
        "fw_sha": state.get("fw") or "unknown",
        "proto_version": state.get("protocol_version") or 2,
        "tof_dim": current_resolution["dim"],
    }
    meta.update(_clock_meta())
    if meta["session_start_device_us"] == -1:
        # No usable PING burst. Fall back to the first buffered frame's own
        # timestamp so the session is still readable, and leave
        # clock_sync_confirmed False so nobody mistakes it for a calibration.
        meta["session_start_device_us"] = int(tof_t_us[0])
        meta["session_start_host_us"] = float(time.time() * 1e6)
    return meta


def load_pca_model(source):
    """Load a fitted PCA model for C10, or None if none has been saved.

    Nothing in the pipeline writes these yet. Returning None (-> 204) rather
    than fitting one here on the fly is deliberate: C10 clears its trajectory
    only when `source`/`dims` change, so a model silently refitted on a
    rolling window would keep the same label while rotating the axes
    underneath the points already plotted.
    """
    path = MODELS_DIR / f"pca_{source}.joblib"
    if not path.is_file():
        return None
    try:
        import joblib
        model = joblib.load(path)
        return {
            "source": source,
            "dims": int(model.mean_.shape[0]),
            "mean": [float(v) for v in model.mean_],
            "components": [[float(v) for v in row] for row in model.components_],
            "explainedVarianceRatio": [float(v) for v in model.explained_variance_ratio_],
        }
    except Exception as exc:
        print(f"[bridge] could not load {path}: {exc}")
        return None


# --- B09 / B11 seams -------------------------------------------------------
# The state machines themselves are not built yet. These exist so that when
# they are, both stories emit the shapes CONTRACTS #4.2 already froze rather
# than each inventing their own -- which is the failure mode that made the
# $STATUS/drop_* mismatch expensive to find.

TRIAL_STATES = ("PROMPT", "COUNTDOWN", "CAPTURE", "SAVE", "REST")
SESSION_STATES = ("started", "baseline", "ended")


def publish_trial_state(state, label=None, idx=None, **extra):
    """Emit a `trial` event. Called by B11's trial state machine."""
    if state not in TRIAL_STATES:
        raise ValueError(f"trial state must be one of {TRIAL_STATES}, got {state!r}")
    event = {"type": "trial", "state": state, **extra}
    if label is not None:
        event["label"] = label
    if idx is not None:
        event["idx"] = idx
    broadcaster.publish(event)


def publish_session_state(state, progress=None, **extra):
    """Emit a `session` event. Called by B09's session lifecycle."""
    if state not in SESSION_STATES:
        raise ValueError(f"session state must be one of {SESSION_STATES}, got {state!r}")
    event = {"type": "session", "state": state, **extra}
    if progress is not None:
        event["progress"] = progress
    broadcaster.publish(event)


def save_wav(rate, bits, channels, pcm_bytes):
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm_bytes)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block_align, bits)
    header += b"data" + struct.pack("<I", len(pcm_bytes))

    filename = f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
    path = VOICE_DIR / filename
    path.write_bytes(header + pcm_bytes)
    return filename, len(header) + len(pcm_bytes)


def _process_mfcc(filename):
    """B14 備援路線：WAV 存好之後算 log-Mel，永遠存一份 `.npy`，
    有指定 `--h5-session` 的話再多寫一份進該 session 的下一個 trial。

    在自己的執行緒跑，不擋 serial_reader 讀下一行（延遲拆解裡 MFCC 只要
    ~0.1s，但 h5py I/O 加上 GIL 底下沒必要冒風險卡到即時的 $TOF 解析）。
    """
    wav_path = VOICE_DIR / filename
    if not _load_mel_backend():
        broadcaster.publish({"type": "mfcc", "state": "error", "file": filename,
                             "message": f"mel backend unavailable: {_MEL_IMPORT_ERROR}"})
        return
    broadcaster.publish({"type": "mfcc", "state": "computing", "file": filename})
    try:
        log_mel, elapsed = wav_to_log_mel_timed(wav_path)
    except Exception as exc:
        broadcaster.publish({"type": "mfcc", "state": "error", "file": filename, "message": str(exc)})
        return

    npy_path = wav_path.with_suffix(".mel.npy")
    np.save(npy_path, log_mel)

    event = {
        "type": "mfcc", "state": "done", "file": filename,
        "npy_file": npy_path.name, "n_frames": int(log_mel.shape[0]),
        "elapsed_ms": round(elapsed * 1000, 1),
    }

    h5_path = mfcc_target["h5_path"]
    if h5_path is not None:
        trial_idx = mfcc_target["next_trial_idx"]
        mfcc_target["next_trial_idx"] += 1
        try:
            write_mel_to_trial(h5_path, trial_idx, log_mel)
            event["h5_trial_idx"] = trial_idx
        except Exception as exc:
            broadcaster.publish({
                "type": "mfcc", "state": "error", "file": filename,
                "message": f"HDF5 寫入失敗 (trial_idx={trial_idx}): {exc}",
            })
            return

    broadcaster.publish(event)


def serial_reader(port, baud, allow_v1=False):
    """Runs for the lifetime of the process; pauses itself while `flashing`
    is set so the flash subprocess can have the port exclusively."""
    import serial

    # State for an in-progress BEGIN_WAV_B64 ... END_WAV_B64 capture. A
    # recording pauses the mic's normal $MIC lines but $TOF lines keep
    # interleaving on the same UART, so this only activates between the
    # markers and otherwise falls through to the regular line parser.
    wav_meta = None
    wav_chunks = []

    while True:
        if flashing.is_set():
            time.sleep(0.2)
            continue
        try:
            with serial_lock:
                ser = serial.Serial(port, baud, timeout=1)
            current_serial_holder["ser"] = ser
            # A fresh parser per connection: version negotiation state
            # belongs to one link, and reconnecting after a reflash means
            # re-reading the new firmware's $STATUS from scratch.
            protocol_state["parser"] = ProtocolParser(allow_v1=allow_v1)
            print(f"[bridge] serial open: {port} @ {baud}")
            broadcaster.publish({"type": "link", "state": "up"})
            # Ask the device to identify itself right away. The board boots
            # long before the bridge starts, so its power-on $STATUS is
            # almost always already gone by the time we open the port --
            # without this, version negotiation never confirms and the
            # frame parameters from CONTRACTS #1.1.2 stay unknown for the
            # whole session. #1.1 has the device re-send $STATUS on every
            # PING for exactly this case.
            try:
                with serial_write_lock:
                    ser.write(b"PING\n")
            except Exception as exc:
                print(f"[bridge] initial PING failed: {exc}")
            try:
                while not flashing.is_set():
                    label = ping_request["label"]
                    if label:
                        ping_request["label"] = None
                        run_ping_burst(ser, label)
                    raw = ser.readline()
                    if not raw:
                        continue
                    # Counted at the port, before parsing: malformed lines
                    # and the base64 dump occupy real link capacity, and the
                    # bandwidth metric is about how full the link is, not
                    # how much of it turned out to be useful.
                    quality.note_bytes(len(raw))
                    try:
                        text = raw.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    stripped = text.strip()

                    if stripped.startswith("BEGIN_WAV_B64"):
                        m = WAV_HEADER_RE.search(stripped)
                        wav_meta = {
                            "rate": int(m.group(1)), "bits": int(m.group(2)),
                            "channels": int(m.group(3)), "declared_bytes": int(m.group(4)),
                        } if m else {"rate": 16000, "bits": 16, "channels": 1, "declared_bytes": 0}
                        wav_chunks = []
                        broadcaster.publish({"type": "record", "state": "receiving"})
                        continue

                    if stripped == "END_WAV_B64" and wav_meta is not None:
                        try:
                            pcm = base64.b64decode("".join(wav_chunks))
                            filename, size = save_wav(
                                wav_meta["rate"], wav_meta["bits"], wav_meta["channels"], pcm)
                            broadcaster.publish({
                                "type": "record", "state": "done",
                                "file": filename, "url": f"/voice/{filename}",
                                "bytes": size, "duration_sec": round(len(pcm) / (wav_meta["rate"] * 2), 2),
                            })
                            print(f"[bridge] saved recording: voice/{filename} ({size} bytes)")
                            threading.Thread(target=_process_mfcc, args=(filename,), daemon=True).start()
                        except Exception as exc:
                            broadcaster.publish({"type": "record", "state": "error", "message": str(exc)})
                        wav_meta = None
                        wav_chunks = []
                        continue

                    if wav_meta is not None and not stripped.startswith("$"):
                        # A recording only pauses the mic's own $MIC lines;
                        # $TOF keeps interleaving on the same UART for a
                        # live panel during the capture+transfer window, so
                        # this can't assume every line seen in here is
                        # base64 -- only base64 lines are (base64 never
                        # starts with '$'). Anything '$'-prefixed falls
                        # through to the normal parser below instead.
                        wav_chunks.append(stripped)
                        continue

                    parsed = protocol_state["parser"].feed(text)
                    if parsed:
                        handle_parsed_event(parsed)
            finally:
                current_serial_holder["ser"] = None
                ser.close()
                broadcaster.publish({"type": "link", "state": "down"})
        except Exception as exc:
            broadcaster.publish({"type": "link", "state": "down", "message": str(exc)})
            time.sleep(1.0)


def do_switch_resolution(new_dim, port):
    if new_dim not in (4, 8):
        broadcaster.publish({"type": "flash", "state": "error", "message": "resolution must be 4 or 8"})
        return

    flashing.set()
    try:
        with serial_lock:
            time.sleep(0.3)  # let the reader thread release the port

            broadcaster.publish({"type": "flash", "state": "editing", "dim": new_dim})
            src = MAIN_SRC.read_text()
            new_src, n = RESOLUTION_RE.subn(f"#define TOF_RESOLUTION_MODE {new_dim}", src, count=1)
            if n != 1:
                broadcaster.publish({
                    "type": "flash", "state": "error",
                    "message": "could not find TOF_RESOLUTION_MODE define in vl53l7cx_test.c",
                })
                return
            MAIN_SRC.write_text(new_src)

            broadcaster.publish({"type": "flash", "state": "building", "dim": new_dim})
            cmd = (
                f"source {IDF_EXPORT} > /dev/null 2>&1 && "
                f"cd {PROJECT_DIR} && idf.py build"
            )
            result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                broadcaster.publish({
                    "type": "flash", "state": "error",
                    "message": "build failed: " + result.stdout[-1500:] + result.stderr[-1500:],
                })
                return

            broadcaster.publish({"type": "flash", "state": "flashing", "dim": new_dim})
            cmd = (
                f"source {IDF_EXPORT} > /dev/null 2>&1 && "
                f"cd {PROJECT_DIR} && idf.py -p {port} flash"
            )
            result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                broadcaster.publish({
                    "type": "flash", "state": "error",
                    "message": "flash failed: " + result.stdout[-1500:] + result.stderr[-1500:],
                })
                return

            current_resolution["dim"] = new_dim
            broadcaster.publish({"type": "flash", "state": "done", "dim": new_dim})
    finally:
        flashing.clear()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "vl53l7cx-monitor/1.0"

    def log_message(self, fmt, *args):
        pass  # keep stdout clean; the important stuff comes from the [bridge] prints

    # -- small helpers shared by the JSON endpoints ----------------------

    def _send_json(self, code, payload=None):
        body = b"" if payload is None else json.dumps(
            _json_safe(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_json_body(self):
        """Returns the parsed body, or None after already sending a 400."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"body 不是合法的 JSON: {exc}"})
            return None
        if not isinstance(parsed, dict):
            self._send_json(400, {"error": "body 必須是 JSON 物件"})
            return None
        return parsed

    def do_GET(self):
        if self.path.startswith("/session/current"):
            self._handle_session_current()
        elif self.path.startswith("/session/prefill"):
            self._send_json(200, session_registry.get_prefill())
        elif self.path.startswith("/baseline"):
            self._handle_get_baseline()
        elif self.path.startswith("/pca"):
            self._handle_pca()
        elif self.path == "/" or self.path.startswith("/panel/"):
            self._serve_panel_asset()
        elif self.path == "/panel.html":
            # Legacy single-file panel, kept as the E08 fallback demo path.
            self._serve_panel()
        elif self.path.startswith("/events"):
            self._serve_events()
        elif self.path.startswith("/voice/"):
            self._serve_voice_file()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/switch"):
            self._handle_switch()
        elif self.path.startswith("/record"):
            self._handle_record()
        elif self.path.startswith("/session/start"):
            self._handle_session_start()
        elif self.path.startswith("/session/end"):
            self._handle_session_end()
        elif self.path.startswith("/session/baseline"):
            self._handle_session_baseline()
        else:
            self.send_error(404)

    # -- B09: session lifecycle -----------------------------------------

    def _handle_session_start(self):
        metadata = self._read_json_body()
        if metadata is None:
            return
        try:
            info = session_registry.start(metadata)
        except MissingFieldsError as exc:
            # 400 with the field names, not just "bad request": the form on
            # the other end wants to highlight exactly which inputs to fix.
            self._send_json(400, {"error": str(exc), "missing": exc.fields})
            return
        except SessionAlreadyActiveError as exc:
            self._send_json(409, {"error": str(exc), "session_id": exc.session_id})
            return

        with session_lock:
            session_runtime.update(baseline=None, trial=None,
                                   h5_path=sessions_dir() / f"{info.session_id}.h5",
                                   writer=None)
        clock_sync[SESSION_START] = None
        clock_sync[SESSION_END] = None
        request_ping_burst(SESSION_START)
        publish_session_state("started", session=info.to_dict())
        self._send_json(200, info.to_dict())

    def _handle_session_end(self):
        try:
            info = session_registry.end()
        except NoActiveSessionError as exc:
            # 409, matching start's "the session state is not what you think
            # it is" case. CONTRACTS #4.1.1 leaves this code to B19; using
            # the same one for both directions keeps the panel's handling
            # symmetric instead of making it learn two conventions.
            self._send_json(409, {"error": str(exc)})
            return
        with session_lock:
            writer = session_runtime.get("writer")
            session_runtime.update(trial=None, writer=None)
        if writer is not None:
            try:
                writer.__exit__(None, None, None)
            except Exception as exc:
                print(f"[bridge] closing the session writer failed: {exc}")
        request_ping_burst(SESSION_END)
        publish_session_state("ended", session=info.to_dict())
        self._send_json(200, info.to_dict())

    def _handle_session_current(self):
        info = session_registry.current
        if info is None:
            self._send_json(204)      # CONTRACTS #4.1.1
            return
        self._send_json(200, info.to_dict())

    # -- B10: baseline ---------------------------------------------------

    def _handle_session_baseline(self):
        """Capture the 30 s baseline that every session must start with."""
        info = session_registry.current
        if info is None:
            self._send_json(409, {"error": "沒有進行中的 session，不能擷取 baseline"})
            return

        m = re.search(r"seconds=(\d+)", self.path)
        seconds = max(1, min(120, int(m.group(1)) if m else 30))

        outcome, err = capture_session_baseline(info, seconds)
        if err is not None:
            self._send_json(409, {"error": err})
            return

        payload = _baseline_payload(outcome, source="session")
        if not outcome.ok:
            # Quality gate failed: nothing was written and baseline_done
            # stays false, so trials remain blocked. The operator is meant
            # to fix the fit and try again -- reporting "done" here would
            # let a whole session be recorded against a baseline the code
            # already knows is untrustworthy.
            self._send_json(422, payload)
            return

        session_registry.mark_baseline_recorded()
        publish_session_state("baseline", session=session_registry.current.to_dict(),
                              progress={"baseline_seconds": seconds})
        broadcaster.publish({"type": "baseline", **payload})
        self._send_json(200, payload)

    def _handle_get_baseline(self):
        """C06 reads this to draw the per-zone baseline overlay."""
        with session_lock:
            outcome = session_runtime.get("baseline")
        if outcome is None:
            self._send_json(204)
            return
        self._send_json(200, _baseline_payload(outcome, source="session"))

    # -- C10: PCA model ---------------------------------------------------

    def _handle_pca(self):
        m = re.search(r"model=([A-Za-z_]+)", self.path)
        source = m.group(1) if m else "tof_only"
        if source not in ("tof_only", "enrollment"):
            self._send_json(400, {"error": "model 必須是 tof_only 或 enrollment"})
            return
        payload = load_pca_model(source)
        if payload is None:
            # 204, not a stub: C10 keeps its own placeholder and retries
            # every 10 s, and a fabricated model would look like a real one
            # while putting the trace on axes that mean nothing.
            self._send_json(204)
            return
        self._send_json(200, payload)

    def _serve_voice_file(self):
        # basename only: no path traversal out of VOICE_DIR via ../
        name = Path(unquote(self.path[len("/voice/"):])).name
        path = VOICE_DIR / name
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # <script type="module"> enforces the JS MIME type strictly -- serving
    # .js as text/plain makes the browser refuse the module outright with
    # "Failed to load module script". BaseHTTPRequestHandler has no built-in
    # table, so spell out the types the panel actually ships.
    _PANEL_MIME = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".woff2": "font/woff2",
    }

    def _serve_panel_asset(self):
        rel = unquote(self.path[len("/panel/"):]) if self.path.startswith("/panel/") else ""
        rel = rel.split("?", 1)[0].split("#", 1)[0]
        if not rel or rel.endswith("/"):
            rel += "index.html"

        # Resolve-then-contain: PANEL_DIR has real subdirectories (js/modes/,
        # css/modes/), so the basename-only guard used by _serve_voice_file()
        # would flatten them. Resolve the joined path and require it to stay
        # under PANEL_DIR -- that rejects ../ and absolute paths alike.
        try:
            path = (PANEL_DIR / rel).resolve()
            path.relative_to(PANEL_DIR.resolve())
        except (ValueError, OSError):
            self.send_error(403)
            return
        if not path.is_file():
            self.send_error(404)
            return

        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         self._PANEL_MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_panel(self):
        body = PANEL_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = broadcaster.subscribe()
        try:
            # tell the freshly-connected client what we currently know
            # Everything we already know, so a tab opened mid-session is
            # not blank until the next $STATUS: the protocol state (B02's
            # recording_allowed / warning included) and one quality frame.
            parser = protocol_state["parser"]
            initial = {"type": "status", "dim": current_resolution["dim"]}
            if parser is not None:
                initial.update(parser.state())
            opening = [initial, quality.snapshot()]
            # A tab opened mid-session has missed the `session` broadcast
            # that fired at start, and polling /session/current just to find
            # out would defeat the point of the event stream.
            info = session_registry.current
            if info is not None:
                opening.append({"type": "session", "state": "started",
                                "session": info.to_dict()})
            with session_lock:
                baseline = session_runtime.get("baseline")
            if baseline is not None:
                opening.append({"type": "baseline",
                                **_baseline_payload(baseline, source="session")})
            for event in opening:
                self.wfile.write(
                    f"data: {json.dumps(_json_safe(event), ensure_ascii=False, allow_nan=False)}\n\n"
                    .encode("utf-8"))
            self.wfile.flush()

            last_ping = time.time()
            while True:
                try:
                    event = q.get(timeout=1.0)
                    # allow_nan=False plus the sanitiser: a single NaN would
                    # otherwise be emitted as a bare literal and make the
                    # browser's JSON.parse throw on that whole message.
                    self.wfile.write(
                        f"data: {json.dumps(_json_safe(event), ensure_ascii=False, allow_nan=False)}\n\n"
                        .encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    if time.time() - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            broadcaster.unsubscribe(q)

    def _handle_switch(self):
        m = re.search(r"res=(\d+)", self.path)
        if not m:
            self.send_error(400, "missing res= query param")
            return
        new_dim = int(m.group(1))

        if flashing.is_set():
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"a flash is already in progress"}')
            return

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"accepted": True, "dim": new_dim}).encode())

        threading.Thread(target=do_switch_resolution, args=(new_dim, self.server.serial_port), daemon=True).start()

    def _handle_record(self):
        m = re.search(r"seconds=(\d+)", self.path)
        seconds = int(m.group(1)) if m else 5
        seconds = max(1, min(30, seconds))

        if flashing.is_set():
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"a flash is in progress"}')
            return

        ser = current_serial_holder["ser"]
        if ser is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"serial link is down"}')
            return

        with serial_write_lock:
            ser.write(f"REC:{seconds}\n".encode())

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"accepted": True, "seconds": seconds}).encode())


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyUSB0", help="ESP32 serial port")
    parser.add_argument("--baud", type=int, default=460800, help="must match CONFIG_ESP_CONSOLE_UART_BAUDRATE")
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument(
        "--sessions-dir", default=None,
        help="session HDF5 檔的輸出目錄（預設 <repo>/sessions）")
    parser.add_argument(
        "--last-session", default=None,
        help="表單預填用的 last_session.json 路徑（預設 config/last_session.json）")
    parser.add_argument(
        "--allow-v1", action="store_true",
        help="B02 降級模式：接受舊韌體的 $TOF/$MIC 行。預設關閉——v1 沒有 "
             "t_us，錄下來的 session 無法做時間對齊，所以必須明確打開。")
    parser.add_argument(
        "--h5-session", default=None,
        help="B14 備援路線：每次錄音完成後，除了存 .npy，也把 log-mel 寫進這個 "
             "session HDF5 檔（trial group 要已經存在，例如用 "
             "ssi-backlog/tools/schema_example.py 產生）。不給就只存 .npy。")
    args = parser.parse_args()

    if args.h5_session:
        mfcc_target["h5_path"] = Path(args.h5_session)

    global session_registry
    if args.sessions_dir:
        runtime_paths["sessions"] = Path(args.sessions_dir)
    if args.last_session:
        runtime_paths["last_session"] = Path(args.last_session)
        session_registry = SessionRegistry(runtime_paths["last_session"])

    reader = threading.Thread(
        target=serial_reader, args=(args.port, args.baud, args.allow_v1), daemon=True)
    reader.start()

    # Runs whether or not the link is up: a health dashboard that freezes
    # when something breaks is useless exactly when it is needed.
    threading.Thread(target=quality_emitter, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    server.serial_port = args.port
    print(f"[bridge] panel: http://127.0.0.1:{args.http_port}/")
    print(f"[bridge] serial: {args.port} @ {args.baud}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
