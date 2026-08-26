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
import inspect
import json
import queue
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
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
from host.storage.session_writer import SessionWriter               # noqa: E402
from host.trial.state_machine import TrialStateMachine              # noqa: E402
from host.replay.session_replay import (                            # noqa: E402
    ReplayController, read_session_events, NoReplayEventsError, TrialNotFoundError,
)

THRESHOLDS_PATH = ROOT_DIR / "config" / "quality_thresholds.json"
LAST_SESSION_PATH = ROOT_DIR / "config" / "last_session.json"
#: Fitted PCA models for GET /pca. Nothing writes here yet -- the endpoint
#: reports "no model" rather than inventing one (see _handle_pca).
MODELS_DIR = ROOT_DIR / "models"
VOCAB_PATH = ROOT_DIR / "config" / "vocab.json"

# Imported lazily: mel_pipeline pulls in librosa, which is a heavy optional
# dependency. The bridge's core job -- serving the panel and relaying $-lines
# over SSE -- must still start on a machine that only has pyserial, and the
# E08 fallback demo depends on that. A missing librosa degrades the MFCC
# feature to an SSE error event instead of killing the whole server.
_MEL_IMPORT_ERROR = None
wav_to_log_mel_timed = None


def _load_mel_backend():
    """Return True once the B14 mel backend is importable; cache the failure.

    Only mel_pipeline is guarded here. It used to import host.storage.
    mel_writer in the same try/except, which meant anything wrong with that
    module took the whole backend down -- including the .npy path, which has
    nothing to do with it -- and did so silently, because the ImportError was
    swallowed by the except. mel_writer has since been deleted (it wrote a
    `mel` dataset with no `mel_t_us`, violating the pairing rule in schema
    section 2, and its own docstring said it was meant to retire once B07
    landed).
    """
    global wav_to_log_mel_timed, _MEL_IMPORT_ERROR
    if wav_to_log_mel_timed is not None:
        return True
    if _MEL_IMPORT_ERROR is not None:
        return False
    try:
        from host.features.mel_pipeline import wav_to_log_mel_timed as _w
    except ImportError as exc:
        _MEL_IMPORT_ERROR = exc
        print(f"[bridge] mel backend unavailable ({exc}); "
              f"MFCC disabled, everything else still works")
        return False
    wav_to_log_mel_timed = _w
    return True


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
def current_parser_stats():
    """ProtocolParser.stats for the live connection, or {} before one exists."""
    parser = protocol_state.get("parser")
    return parser.stats.as_dict() if parser is not None else {}


quality = QualityAggregator(
    quality_thresholds,
    drop_tracker=drop_tracker,
    clock_aligner=clock_aligner,
    # The malformed counter was already being computed and already reaching
    # this process; it was simply never read here. A rejected line leaves the
    # same seq gap as a frame that never arrived, so without it the transport
    # alarm cannot tell the two apart.
    parser_stats=current_parser_stats,
    sensors_seen=lambda: sensors_seen_string(),
    # $F can legitimately be off (MEL:0). Reporting a deliberately silent
    # stream as a fault is how an alarm gets trained out of people.
    mel_enabled=lambda: (protocol_state["parser"].state().get("mel")
                         if protocol_state.get("parser") else None),
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
        # protocol.py attaches the host-side parser counters to every $H.
        # They used to be dropped right here by the whitelist -- computed,
        # attached, then discarded at the last gate.
        if event.get("host") is not None:
            out["host"] = event["host"]
    elif kind == "status":
        # parser.state() is the seam B02 built for exactly this: it already
        # carries protocol_version / degraded / warning / recording_allowed,
        # so the panel can grey out the record button without re-deriving
        # any of it. `dim` is in there too, which keeps the event shape
        # backward-compatible with the panel's existing status handler.
        parser = protocol_state["parser"]
        out = {"type": "status", "source": link_source["value"]}
        if parser is not None:
            out.update(parser.state())
        else:
            out["dim"] = event.get("dim")
        out["source"] = link_source["value"]
        out["sensors_seen"] = sensors_seen_string()
        out["sensors_enabled"] = sensors_enabled_string() or ""
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

    if parsed.get("type") in ("tof", "mic", "mel") and replay_is_active():
        # The disaster the story names explicitly: two data streams
        # interleaved on one channel, with no way for the panel to tell
        # which frame came from where. Device state (status/heartbeat) still
        # gets through -- it is not replayed, and hiding it would make the
        # link look dead during a replay.
        return

    observe_for_quality(parsed)
    sse = to_sse_event(parsed)
    if sse:
        broadcaster.publish(sse)
    publish_status_if_changed()


def publish_status_if_changed():
    """Emit a `status` event whenever the negotiated state actually changes.

    Not only on `$STATUS` lines. A v1 device sends exactly one, at boot, and
    a panel that connects a moment later would never learn the link is
    degraded -- so its record button would stay enabled on a link whose data
    has no timestamps and cannot be verified, which is the one thing B02
    exists to prevent. The parser works the version out from the data lines
    within a frame or two; this makes that reach the panel.
    """
    parser = protocol_state.get("parser")
    if parser is None:
        return
    state = parser.state()
    signature = (state.get("protocol_version"), state.get("degraded"),
                 state.get("version_mismatch"), state.get("recording_allowed"),
                 state.get("dim"), state.get("fw"), state.get("mel"),
                 # A sensor appearing or going quiet is a status change too.
                 # Without it the panel keeps showing whatever was true when
                 # it connected -- and on a one-sensor board that is wrong.
                 sensors_seen_string())
    if signature == protocol_state.get("last_status_signature"):
        return
    protocol_state["last_status_signature"] = signature
    broadcaster.publish({"type": "status", "source": link_source["value"],
                         "sensors_seen": sensors_seen_string(),
                         "sensors_enabled": sensors_enabled_string() or "",
                         **state})


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
runtime_paths = {"sessions": ROOT_DIR / "sessions", "last_session": LAST_SESSION_PATH,
                 "verification": ROOT_DIR / "reports" / "verification",
                 "templates": ROOT_DIR / "templates"}

# What this link is actually connected to (CONTRACTS #4.2). Declared on the
# command line, never inferred: a pty from T04's synthetic device and a pty
# from T05's log replay are indistinguishable from here, and so is a real
# USB-UART. Guessing would be worse than useless -- E05 records for four
# hours, and a session accidentally captured against the mock would produce
# an HDF5 full of synthetic data labelled as real measurement, which every
# downstream analysis would happily consume.
VALID_SOURCES = ("live", "mock", "replay-log", "replay-session")
link_source = {"value": "live"}
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

# --- D09: POST /recognize, GET /templates --------------------------------
#
# RecognitionService (analysis/similarity/recognition_service.py) and the
# whole D01->D02->D03 feature-assembly chain it needs already exist and are
# individually tested -- this block only imports and wires them, it does not
# reimplement any of them. host/features/live_pipeline.py is the one
# genuinely new piece: nothing in the repo previously turned aligned device
# frames into the (T,104) vector RecognitionService.recognize() expects
# (see reports/PANEL_LEAK_AUDIT.md-adjacent investigation notes / the
# completion report for this story). RecognitionService/enrollment are
# imported lazily inside the functions below, matching this file's existing
# h5py/librosa convention (see read_baseline_thresholds above) rather than
# adding new eager imports to the stdlib-only block at the top of the file.
RECOGNIZE_WINDOW_S = 2.0  # live-capture path: how far back into session_aligner to look

recognition_service_state = {"service": None, "error": None, "path": None}
recognition_service_lock = threading.Lock()


def _load_recognition_service():
    """Lazily build (and cache) a RecognitionService from the first .npz
    found under runtime_paths["templates"]. That directory is empty in this
    repo right now -- no enrollment has ever been recorded -- and that is
    the expected, common state, not an error condition: callers get an
    honest "no templates yet" reason string back, never a 500 from deep
    inside RecognitionService's constructor.
    """
    with recognition_service_lock:
        if recognition_service_state["service"] is not None:
            return recognition_service_state["service"], None
        if recognition_service_state["error"] is not None:
            return None, recognition_service_state["error"]

        from analysis.similarity.recognition_service import RecognitionService
        from analysis.similarity.enrollment import load_templates

        templates_dir = Path(runtime_paths["templates"])
        npz_files = sorted(templates_dir.glob("*.npz")) if templates_dir.is_dir() else []
        if not npz_files:
            reason = f"{templates_dir} 底下沒有任何 enrollment 樣板（*.npz）—— 尚未錄過 enrollment"
            recognition_service_state["error"] = reason
            return None, reason

        path = npz_files[0]
        try:
            templates_by_class, meta, warning = load_templates(path)
            if warning:
                print(f"[bridge] /recognize: {warning}")
            reject_templates = templates_by_class.pop("_reject", [])
            if not reject_templates:
                reason = f"{path.name} 沒有 _reject 樣板，RecognitionService 無法校準拒識門檻"
                recognition_service_state["error"] = reason
                return None, reason
            slices = {"tof": slice(0, 64), "mel": slice(64, 104)}  # CONTRACTS.md §3.3
            service = RecognitionService(
                templates_by_class, reject_templates, slices,
                subject=meta.get("subject"), wear_id=meta.get("wear_id"),
            )
        except Exception as exc:
            reason = f"讀取/建立 RecognitionService 失敗（{path.name}）: {exc}"
            recognition_service_state["error"] = reason
            return None, reason

        recognition_service_state["service"] = service
        recognition_service_state["path"] = str(path)
        return service, None


def _frames_from_live_session(window_s):
    """Same idiom as capture_session_baseline() above: last `window_s`
    seconds out of the always-running session_aligner buffer."""
    latest = device_clock["last_t_us"]
    if latest is None:
        return []
    return list(session_aligner.frames(latest - int(window_s * 1e6), latest))


def _frames_from_stored_trial(h5_path, trial_group):
    """Read one already-recorded trial's tof_A/tof_B/mel back out of the
    session HDF5 file and re-align them through a fresh Aligner. Reads via
    analysis/reporting/session_loader.py (not raw h5py directly -- that
    would leave bool datasets as numpy.bool_ instead of Python bool;
    session_loader already does that conversion, same convention
    analysis/similarity/build_templates_from_session.py uses for the
    training-side path). ToF (@30Hz) and Mel (@62.5Hz) are stored on their
    own separate time axes (CONTRACTS.md §2: "mel 的時間軸是 F 不是 M")
    and are not frame-aligned to each other on disk, so this cannot just
    concatenate the raw arrays -- it has to go back through Aligner exactly
    like the live-capture path does, via the same
    assemble_query_from_aligned_frames() downstream.
    """
    from analysis.reporting.session_loader import load_session

    session = load_session(Path(h5_path))
    trial = next((t for t in session.trials if t.key == trial_group), None)
    if trial is None:
        raise KeyError(f"{trial_group} 不存在於 {h5_path}")
    if trial.mel is None or trial.mel_t_us is None:
        raise ValueError(f"{trial_group} 沒有 mel/mel_t_us（選填欄位，這筆錄音當下 Mel 未開啟）")

    n_zones = trial.n_zones
    aligner = Aligner()
    for i in range(len(trial.tof_t_us)):
        aligner.push_tof(
            "A", int(trial.tof_t_us[i]),
            [None if np.isnan(v) else float(v) for v in trial.tof_a[i, :n_zones]],
            [None if np.isnan(v) else float(v) for v in trial.tof_a[i, n_zones:]],
            [bool(v) for v in trial.tof_valid_a[i]])
        aligner.push_tof(
            "B", int(trial.tof_t_us[i]),
            [None if np.isnan(v) else float(v) for v in trial.tof_b[i, :n_zones]],
            [None if np.isnan(v) else float(v) for v in trial.tof_b[i, n_zones:]],
            [bool(v) for v in trial.tof_valid_b[i]])
    for i in range(len(trial.mel_t_us)):
        aligner.push_mel(int(trial.mel_t_us[i]), [float(v) for v in trial.mel[i]])

    t_start = int(min(trial.tof_t_us[0], trial.mel_t_us[0]))
    t_end = int(max(trial.tof_t_us[-1], trial.mel_t_us[-1]))
    return list(aligner.frames(t_start, t_end))


# --- D08 (partial): POST /templates/build, GET /templates/build/state ---
#
# The "trial recording -> saved templates" step existed nowhere at all
# until esp-mask-test-ad's audit (2026-08-26): the save/load mechanics
# (analysis/similarity/enrollment.py) were built, the training-side
# assembly (analysis/similarity/build_templates_from_session.py) exists,
# but nothing let the user trigger it without opening a terminal --
# unusable while wearing the device mid-recording. This wires that script
# in as a background job, same 202+poll pattern as /verify/run below.
templates_build_state = {
    "running": False,
    "started_at": None,
    "started_monotonic": None,
    # Not cleared when a new run starts -- same reasoning as verify_state:
    # the panel should keep showing the last result while a new build is
    # in flight, not go blank.
    "last_result": None,
    "last_error": None,
}
templates_build_lock = threading.Lock()


def templates_build_status():
    with templates_build_lock:
        elapsed = None
        if templates_build_state["running"] and templates_build_state["started_monotonic"] is not None:
            elapsed = time.monotonic() - templates_build_state["started_monotonic"]
        return {
            "type": "templates_build_state",
            "running": templates_build_state["running"],
            "started_at": templates_build_state["started_at"],
            "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "last_result": templates_build_state["last_result"],
            "last_error": templates_build_state["last_error"],
        }


def run_templates_build(session_paths, out_path, subject, wear_id):
    """Background thread. Direct import, not subprocess -- same reason as
    run_verification() below: a raised exception should stay a real
    exception object here, not turn into stderr text to re-parse.
    """
    from analysis.reporting.session_loader import load_session
    from analysis.similarity.build_templates_from_session import build_and_save_templates

    error = None
    result = None
    try:
        sessions = [load_session(Path(p)) for p in session_paths]
        result = build_and_save_templates(sessions, out_path, subject, wear_id)
    except BlockingIOError:
        # Confirmed live (2026-08-26): a session file still held open by
        # SessionWriter cannot be opened for reading at the same time (no
        # SWMR mode in use anywhere in this repo).
        error = ("這個 session 還在錄製中（檔案被寫入中，無法同時讀取）——"
                 "請先按「結束 session」再建樣板")
    except Exception as exc:  # noqa: BLE001 -- background thread, nobody else catches this
        error = f"{type(exc).__name__}: {exc}"
        print(f"[bridge] /templates/build 失敗: {error}", file=sys.stderr)
        traceback.print_exc()
    finally:
        with templates_build_lock:
            templates_build_state["running"] = False
            templates_build_state["started_monotonic"] = None
            templates_build_state["last_error"] = error
            if result is not None:
                templates_build_state["last_result"] = result
        if result is not None:
            # Force a reload: the next /recognize or GET /templates must
            # see the templates that were just built, not keep answering
            # from whatever was cached before this ran -- "state changed
            # underneath, cached answer didn't" is the exact shape today's
            # other fixes were about; not repeating it here.
            with recognition_service_lock:
                recognition_service_state["service"] = None
                recognition_service_state["error"] = None
                recognition_service_state["path"] = None
        broadcaster.publish(templates_build_status())


# Newest device timestamp seen on any stream. The trial machine needs a
# device-clock reading to mark capture boundaries (CONTRACTS #1.3 puts trial
# edges on device time), and the baseline needs one to know where "the last
# 30 seconds" ends. Host time will not do: the two clocks drift, which is
# the whole reason B04 exists.
device_clock = {"last_t_us": None}

# Which port the reader thread is currently bound to, and whether anything
# has actually arrived on it. "The port opened" and "the board is talking"
# are different facts -- measured on the real board, where the port opened
# cleanly while neither ToF sensor produced a single line.
serial_link = {"port": None, "opened_at": None, "first_line_at": None,
               "silent_warned": False}

# What the reader thread is being asked to do. `generation` bumps on every
# change so the loop can tell "reopen on a new port" from "keep going".
# `connected=False` means the user pressed disconnect -- a deliberate state,
# not a fault, and it must not look like one.
serial_target = {"port": None, "baud": 460800, "allow_v1": False,
                 "generation": 0, "connected": True}
serial_target_lock = threading.Lock()

#: How long a port may stay open with nothing recognisable arriving before
#: the panel is told. Long enough to cover a board that is still booting,
#: short enough that picking /dev/ttyS0 by mistake does not look like a hang.
SILENT_PORT_TIMEOUT_S = 8.0


def request_serial_port(port, *, connected=True):
    """Ask the reader thread to move to `port` (or to stop, if not connected)."""
    with serial_target_lock:
        serial_target["port"] = port
        serial_target["connected"] = connected
        serial_target["generation"] += 1
        return serial_target["generation"]


def reset_link_observations():
    """Forget everything learned from the previous board.

    Every one of these is scoped to a link, not to the process: seq counters
    restart on a different device, "which sensors have been seen" says
    nothing about the new board, and a stale-stream alarm about hardware
    that has been unplugged is worse than no alarm at all.
    """
    drop_tracker.reset()
    quality.forget_streams()
    device_clock["last_t_us"] = None
    session_frame_baseline.update({"tof_A": 0, "tof_B": 0})
    protocol_state["parser"] = None
    protocol_state["last_status_signature"] = None

# What the host last *told* the sensors to do. Not what they confirmed: a
# $STATUS carries mel= and amb= but no sens_a=/sens_b=, so the device never
# says whether a sensor is actually ranging. Everything downstream is handed
# `sensors_enabled_confirmed=False` alongside it so nobody can mistake the
# command for the fact.
device_sensor_state = {"A": True, "B": True}


#: Frame counts per ToF stream at the moment the current session started, so
#: `sensors_seen` reflects this session rather than everything the process has
#: ever received.
session_frame_baseline = {"tof_A": 0, "tof_B": 0}


def snapshot_frame_counts():
    return {stream: drop_tracker.stats(stream).received for stream in ("tof_A", "tof_B")}


def sensors_seen_string(since=None):
    """Which sensor labels have actually put data on the wire.

    Derived from the stream itself, so it needs nothing from the firmware.
    That matters: a sensor that fails `is_alive` is reported over ESP_LOGE,
    which is not a `$` line and never reaches the host at all, and $H's
    drop_A/drop_B stay at zero because a sensor nobody is reading never
    fails a read. From the panel, a board with one dead sensor looks exactly
    like a healthy one.

    ⚠ The value says which labels appeared, NOT which physical sensors work.
    On the first real board, sensor A failed to initialise and sensor B was
    the one streaming -- but the frames it emitted were labelled `A`. So
    `sensors_seen == "A"` truthfully means "one stream arrived, calling
    itself A", and the host has no way to tell which device that was.
    Distinguishing them would need `sens_a=`/`sens_b=` in $STATUS, the same
    gap already noted in CONTRACTS 4.1.2.
    """
    since = since or {"tof_A": 0, "tof_B": 0}
    seen = ""
    for letter, stream in (("A", "tof_A"), ("B", "tof_B")):
        if drop_tracker.stats(stream).received > since.get(stream, 0):
            seen += letter
    return seen


def sensors_enabled_string():
    """"AB" | "A" | "B", or None when neither is enabled.

    None rather than "" because the schema validates the value domain, and
    "no sensors" is not a configuration anyone records against -- it means
    the field should be absent rather than present and meaningless.
    """
    on = [k for k in ("A", "B") if device_sensor_state.get(k)]
    return "".join(on) if on else None


def manifest_path():
    """The cross-session manifest lives beside the session files, so a test
    run pointed at a temp directory does not append to the real one."""
    return sessions_dir() / "manifest.csv"


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
        # Passed, but currently dropped: SessionWriter._write_meta only
        # writes REQUIRED_META_KEYS, so recording the link source in the
        # file needs a T02 schema addition. Left here deliberately -- the
        # value is correct and the moment `source` joins the schema this
        # starts working with no change on this side.
        "source": link_source["value"],
    }
    # "What actually arrived", as distinct from sensors_enabled ("what we
    # asked for"). The two disagreeing is itself the diagnosis -- on the
    # first real board this reads "A" against an enabled set of "AB".
    #
    # Counted since the link came up, not since /session/start. The baseline
    # is computed from the buffered seconds *before* the session was started,
    # so frames that went into it can predate the session -- gating on the
    # session's own start made this read "" on a board where both sensors
    # were plainly streaming.
    #
    # ⚠ /meta is written once, at baseline capture, because the schema needs
    # the baseline statistics. So this records what had been seen up to that
    # point, not the whole session. Enough for "one sensor never came up",
    # which is visible from boot; a sensor that drops out mid-session shows
    # up in the SSE events instead.
    meta["sensors_seen"] = sensors_seen_string()

    enabled = sensors_enabled_string()
    if enabled:
        # D10's crosstalk experiment pairs a "one sensor" recording with a
        # "both sensors" one; without this field run_all can never match
        # them up and C0 stays SKIPPED forever. The confirmed flag is
        # deliberately False -- see device_sensor_state.
        meta["sensors_enabled"] = enabled
        meta["sensors_enabled_confirmed"] = False
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


# --- B11 / B12: trial state machine ---------------------------------------


def load_vocab():
    """Labels for the trial rotation, from config/vocab.json (CONTRACTS #6).

    Includes the `reject` entry. It used to be dropped -- only the `words`
    array was read -- so `_reject` was not merely rare in the rotation, it
    was structurally absent: the state machine cycles through this list, and
    a label that is not in it can never come up. Measured over 45 trials and
    five full cycles, it never appeared once, while the panel's progress bar
    showed "_reject: 0/72" as though it were still pending.
    """
    try:
        data = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
        labels = [w["text"] for w in data.get("words", []) if w.get("text")]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[bridge] could not read {VOCAB_PATH}: {exc}")
        return []

    reject = (data.get("reject") or {}).get("id")
    if reject:
        # Keyed by `id`, not `text`, unlike the words. That is the existing
        # convention downstream -- analysis/similarity/scoring.py looks for
        # the class literally named `_reject`, and
        # ssi-backlog/tools/make_reference_session.py already uses
        # data["reject"]["id"] -- so recording it under "靜止／其他" would
        # produce trials nothing could find.
        #
        # Same frequency as every other label: D22's two-sided ROC needs
        # enough _reject samples, and its method is close to immune to class
        # imbalance between 1:0.3 and 1:3, so equal is the safe default.
        labels.append(reject)
    else:
        print(f"[bridge] ⚠ {VOCAB_PATH} has no `reject` entry — "
              f"_reject templates cannot be recorded, and without them "
              f"rejection calibration (D22) has no input")
    return labels


#: The VAD threshold fields B21 takes, as they are named in /meta. Read with
#: .get(): esp-mask-test-18's writer change for energy_* has not landed yet,
#: and per CONTRACTS #1.1.2 a field the producer has not written is None, not
#: a default.
BASELINE_THRESHOLD_KEYS = (
    "baseline_mu_A", "baseline_sigma_A", "baseline_mu_B", "baseline_sigma_B",
    "noise_floor_mu", "noise_floor_sigma", "energy_mu", "energy_sigma",
)


def read_baseline_thresholds(h5_path):
    """Read B10's baseline statistics back out of the session's /meta.

    Read from the file rather than kept in memory from the capture because
    /meta is the record that outlives this process, and a value that
    disagrees with what was written is worse than one that is missing.
    """
    try:
        import h5py
        with h5py.File(h5_path, "r") as f:
            attrs = dict(f["/meta"].attrs) if "meta" in f else {}
    except Exception as exc:
        print(f"[bridge] could not read baseline thresholds from {h5_path}: {exc}")
        return {}

    out = {}
    for key in BASELINE_THRESHOLD_KEYS:
        value = attrs.get(key)
        if value is None:
            continue
        out[key] = (np.asarray(value) if np.ndim(value) else float(value))
    missing = [k for k in BASELINE_THRESHOLD_KEYS if k not in out]
    if missing:
        print(f"[bridge] ⚠ VAD thresholds missing from /meta: {missing} "
              f"— lip/voice detection will degrade silently for these")
    return out


def open_trial_machine(info):
    """Build the trial machine, once the baseline has created the session file.

    Ordering is forced by the schema, not by preference: `/meta` requires the
    baseline statistics, which only exist after the baseline runs, so the
    file has to be created by the baseline and reopened here. mode="a" is
    what makes that safe -- mode="w" truncates, and would take the baseline
    trial with it.
    """
    words = load_vocab()
    if not words:
        return None, "config/vocab.json 讀不到任何詞，無法開始 trial"
    h5_path = session_runtime["h5_path"]
    if h5_path is None or not Path(h5_path).is_file():
        return None, "session 檔案還不存在——請先擷取 baseline"

    try:
        writer = SessionWriter(h5_path, mode="a")
        writer.__enter__()
    except Exception as exc:
        return None, f"無法開啟 session 檔案: {exc}"

    # B21: the VAD thresholds. Every one of these has a default of None, so
    # omitting them is a silent behaviour change rather than an error -- and
    # the two failure modes differ:
    #
    #   baseline_mu/sigma, noise_floor_*  missing -> detect_*_activity()
    #       returns applicable=False, the four VAD attrs stay None. Useless,
    #       but visibly so.
    #   energy_mu/energy_sigma            missing -> lip detection estimates
    #       them itself, about 23% too strict (measured in B16). Lip onset
    #       lands systematically late, so D14's "how far ahead of the voice
    #       do the lips move" comes out systematically small -- while
    #       everything looks like it worked.
    #
    # The second is the dangerous one, so these are read from /meta, which
    # capture_baseline_trial() has already written, rather than recomputed.
    vad = read_baseline_thresholds(h5_path)
    machine = TrialStateMachine(
        words, session_aligner, writer, h5_path, manifest_path(),
        wear_id=info.wear_id, mode=info.mode,
        # trial_000 belongs to the baseline (B10); starting at 0 here would
        # collide with it.
        first_trial_idx=1,
        **vad,
    )
    with session_lock:
        session_runtime["writer"] = writer
        session_runtime["trial"] = machine
    return machine, None


# TrialStateMachine is not thread-safe, and two threads drive it: the ticker
# advances timed transitions while HTTP handlers act on button presses. They
# collide for real -- hold_stop() runs the HDF5 write inside the request, and
# an unsynchronised tick() during it took the whole handler down.
trial_lock = threading.Lock()


#: Fields the panel may send that the state machine is still growing support
#: for. Passed through when the method accepts them, and reported loudly when
#: it does not -- silently dropping a field the client bothered to send is the
#: failure mode this project keeps paying for (speaking_mode was dropped this
#: way, and energy_mu before it).
OPTIONAL_TRIAL_FIELDS = ("baseline_age_s",)
_warned_unsupported_fields = set()


def _optional_trial_kwargs(method, body):
    """Forward the optional fields this method actually accepts.

    baseline_age_s records how stale the baseline was when a trial was
    recorded. It matters after the fact, not during: a four-hour session
    will outlive its baseline, the panel warns the operator at the time, but
    nothing in the HDF5 would say which trials were recorded after it went
    stale -- and E05 is not going to be recorded twice.
    """
    try:
        accepted = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return {}
    out = {}
    for field in OPTIONAL_TRIAL_FIELDS:
        value = body.get(field)
        if value is None:
            continue
        if field in accepted:
            out[field] = value
        elif field not in _warned_unsupported_fields:
            _warned_unsupported_fields.add(field)
            print(f"[bridge] ⚠ panel sent {field}={value!r} but "
                  f"{method.__name__}() does not accept it yet — dropping it. "
                  f"Recorded trials will not carry this field until it does.")
    return out


def as_trial_events(result):
    """Normalise one state-machine return value into a list of events.

    The machine is not uniform about this and reasonably so: `start_trial()`
    and `abort()` cause exactly one transition, while `hold_stop()` and
    `tick()` can cause several in one go (CAPTURE -> SAVE -> REST). Callers
    should not have to remember which is which -- getting it wrong produced
    a nested list that took down the request thread.
    """
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    return [e for e in result if isinstance(e, dict)]


def publish_trial_events(events):
    for event in as_trial_events(events):
        broadcaster.publish({"type": "trial", **event})


def trial_ticker(interval=0.05):
    """Advances the trial state machine on its own thread.

    The machine has no timer of its own by design, so something has to poll
    it. 20 Hz is well under the shortest state duration and keeps the SSE
    transitions tight enough that a countdown looks like a countdown.
    """
    while True:
        time.sleep(interval)
        machine = session_runtime.get("trial")
        if machine is None:
            continue
        try:
            # CONTRACTS #1.3: trial boundaries are marked in device time, so
            # tick() needs the newest device timestamp for the two edges
            # that record one (COUNTDOWN->CAPTURE and leaving CAPTURE).
            with trial_lock:
                events = machine.tick(device_t_us=device_clock["last_t_us"])
        except ValueError as exc:
            # No device timestamp yet: the link is down mid-trial. Reported,
            # not crashed -- the ticker has to survive to run the next one.
            print(f"[bridge] trial tick could not advance: {exc}")
            continue
        except Exception as exc:
            print(f"[bridge] trial tick failed: {exc}")
            continue
        publish_trial_events(events)


# --- B17: session replay --------------------------------------------------

replay_state = {"controller": None, "file": None}
replay_lock = threading.Lock()


def replay_is_active():
    controller = replay_state["controller"]
    return controller is not None and controller.is_active


def resolve_session_file(raw):
    """Contain a requested replay path inside the sessions directory.

    Same resolve-then-contain as the panel's static route, and for the same
    reason: this is a path from an HTTP query string, and `..` in it would
    otherwise read any file the process can reach.
    """
    root = sessions_dir().resolve()
    try:
        path = Path(raw)
        path = (path if path.is_absolute() else root / path).resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        return None
    return path if path.is_file() else None


# -- B19/C23: /verify/* —— 背景跑 D15 的驗證套件 ---------------------------

def verify_dir():
    """驗證報告的輸出根目錄。**可由 `--verification-dir` 覆蓋。**

    跟 `sessions_dir()` 同一個理由：寫死成 repo 底下的路徑，測試跑一次就
    把報告留在工作樹裡（`Rig` 已經為了同樣的問題把 sessions 與
    last_session 沙箱化了）。
    """
    d = Path(runtime_paths["verification"])
    d.mkdir(parents=True, exist_ok=True)
    return d

# 一次只能跑一輪。沿用 `flashing` 的慣例：衝突回 409，不排隊。
# 排隊在這裡沒有意義——第二個請求想要的是「用現在的資料重跑」，
# 而它排到的時候資料已經被前一輪讀完了。
verify_lock = threading.Lock()
verify_state = {
    "running": False,
    "run_id": None,
    "started_at": None,       # ISO 字串
    "started_monotonic": None,
    # ⚠️ 跑到一半時**不清空**：使用者在等新結果的期間，畫面上該繼續顯示
    # 上一次的結論，而不是變成一片空白（那看起來像「結果不見了」）。
    "last_run": None,
    "last_error": None,
}


def verify_status():
    """`GET /verify/state` 的內容。一律 200——「沒有在跑」不是錯誤。"""
    with verify_lock:
        elapsed = None
        if verify_state["running"] and verify_state["started_monotonic"] is not None:
            elapsed = time.monotonic() - verify_state["started_monotonic"]
        return {
            "type": "verify_state",
            "running": verify_state["running"],
            "run_id": verify_state["run_id"],
            "started_at": verify_state["started_at"],
            "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "last_run": verify_state["last_run"],
            "last_error": verify_state["last_error"],
        }


#: 🔴 值得在動這個檔案之前讀一次。
#:
#: 這個專案到目前為止，**同一個形狀的 bug 出現了五次**，而且全部落在
#: 「資料已經算好、正確、就在手上」之後的**最後一關**：
#:
#:   * `$H` 的 `host` 解析統計 —— 被 `to_sse_event()` 的白名單濾掉
#:   * `sensors_seen`          —— 寫進 `/meta` 了，驗證層沒有人讀
#:   * `coverage`              —— `live_pipeline` 算好了，沒離開那一層
#:   * `figures`               —— 檔案在，`iterdir()` 只掃一層所以列不到
#:   * `extras`（D19 的 p 值） —— `build_report()` 有算，序列化沒帶出去
#:
#: **共通點**：白名單、只掃一層、忘了 passthrough——**這些構造的預設行為
#: 都是「丟掉」，而丟掉不會報錯、不會變紅、不會有任何人發現。**
#: 上游算得再對也沒用。
#:
#: 所以：**在這個檔案裡加一個「只挑幾個欄位」的地方時，先問一句
#: 「沒被挑到的那些，有沒有人需要？」** 預設應該是帶出去，不是丟掉。
_LAST_GATE_NOTE = __doc__


# --- device discovery ------------------------------------------------------
#
# USB-serial bridges found on ESP32 dev boards, plus Espressif's own native
# USB. Used only to *hint*: an unrecognised VID/PID is reported as
# likely_esp32=False and still listed, because guessing wrong in either
# direction is worse than saying "I don't know, you pick".
_ESP32_USB_VENDORS = {
    0x10C4: "Silicon Labs CP210x",
    0x1A86: "WCH CH340/CH9102",
    0x0403: "FTDI",
    0x303A: "Espressif (native USB)",
}


def list_serial_ports():
    """Every serial port on the machine, with a hint about which is the board.

    The hint is not a nicety. On this machine `list_ports.comports()` returns
    32 motherboard `/dev/ttyS*` entries with no description, no VID and no
    PID -- an unannotated list of those is unusable, and the one entry that
    matters is not there at all until the board is plugged in.

    Nothing is filtered out: a board behind an adapter this table does not
    know would otherwise vanish from a list that claims to be complete.
    Likely candidates simply sort first.
    """
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        return [], f"pyserial 沒有 list_ports: {exc}"

    ports = []
    for p in list_ports.comports():
        vendor = _ESP32_USB_VENDORS.get(p.vid)
        ports.append({
            "device": p.device,
            "description": p.description or "n/a",
            "manufacturer": p.manufacturer,
            "serial_number": p.serial_number,
            "vid": p.vid,
            "pid": p.pid,
            "likely_esp32": vendor is not None,
            "usb_vendor": vendor,
        })
    ports.sort(key=lambda d: (not d["likely_esp32"], d["device"]))
    return ports, None


def probe_serial_port(port):
    """Can we actually open `port`? Returns `(ok, message)`.

    Checked before handing the port to the reader thread so the failure
    comes back on the request that caused it, rather than as a log line the
    user never sees.

    The two common failures need different answers: a missing port means
    "plug it in / pick another", while a permissions error means "add
    yourself to dialout and log out and back in" -- and that one looks
    identical to a broken board if the message just says "cannot open".
    """
    path = Path(port)
    if not path.exists():
        return False, f"{port} 不存在。板子可能沒插上，或拔插後換成了別的埠號。"
    try:
        import serial
        with serial_lock:
            serial.Serial(port, serial_target["baud"], timeout=0.2).close()
    except PermissionError:
        return False, (f"沒有權限開啟 {port}。請確認你的帳號在 dialout 群組："
                       f"`sudo usermod -a -G dialout $USER`，然後重新登入。")
    except Exception as exc:
        message = str(exc)
        if "Permission denied" in message or "denied" in message.lower():
            return False, (f"沒有權限開啟 {port}。請確認你的帳號在 dialout 群組："
                           f"`sudo usermod -a -G dialout $USER`，然後重新登入。")
        if "Device or resource busy" in message or "busy" in message.lower():
            return False, f"{port} 已經被其他程式佔用。"
        return False, f"無法開啟 {port}：{message}"
    return True, "ok"


def device_status():
    """Where the link stands right now.

    Reports "opened" and "receiving" separately on purpose. A port that
    opens is not a board that works, and collapsing the two would let the
    panel show "connected" over a completely silent link -- which is exactly
    what the real board did the day one sensor failed to initialise.
    """
    port = serial_link["port"]
    opened_at = serial_link["opened_at"]
    first = serial_link["first_line_at"]
    now = time.time()
    with serial_target_lock:
        wanted, connected = serial_target["port"], serial_target["connected"]
    if not connected:
        # The user pressed disconnect. Distinct from a fault, and reported
        # as such -- otherwise the panel says "sensor A has been silent for
        # 12 seconds, check the wiring" about a board they unplugged
        # themselves.
        return {"state": "disconnected", "port": None, "opened_at": None,
                "connected_for_s": None, "data_seen": False,
                "seconds_to_first_line": None, "sensors_seen": "",
                "stale_streams": [], "user_disconnected": True}
    if port is None:
        state = "connecting" if wanted else "disconnected"
    elif first is None:
        state = "opened"        # port is open, nothing has arrived yet
    else:
        state = "receiving"
    return {
        "state": state,
        "port": port,
        "opened_at": opened_at,
        "connected_for_s": round(now - opened_at, 1) if opened_at else None,
        "data_seen": first is not None,
        "seconds_to_first_line": (round(first - opened_at, 2)
                                  if first and opened_at else None),
        "sensors_seen": sensors_seen_string(),
        "user_disconnected": False,
        "silent": bool(serial_link.get("silent_warned")),
        # Whatever has gone quiet since, from the same detector the panel
        # already shows -- "connected but one sensor died" is a state the
        # user needs, and it is not the same as "disconnected".
        "stale_streams": quality.stale_streams(),
    }


def summarize_verify_sessions(session_paths):
    """「這份報告是用什麼資料算的」——矩陣正上方那一行。

    委員問的第二個問題，而畫面上目前完全看不出來。

    ⚠️ **`sensors_seen` 同時給 session 層與 trial 層，這是刻意的。**
    `/meta` 的值是整個 session 一個字串，而實測證明**中途掉線時它是
    `"AB"`**（見 `reports/DEGRADED_SESSION.md` 第二輪）——
    只顯示那個值，反而會讓人看到「兩顆都在」而放心。
    per-trial 的統計才看得出「6 筆裡有 3 筆只有 A」。

    讀不出來的 session 不會讓整個回應失敗：驗證報告本身已經跑完了，
    摘要壞掉不該把結果一起帶走。
    """
    from analysis.reporting import session_loader

    out = {"sessions": [], "n_trials": 0, "labels": [],
           "trials_per_label": {}, "sensors_seen": [],
           "trial_sensors_seen": {}, "errors": []}
    labels, per_label, trial_seen = [], {}, {}

    for path in session_paths:
        try:
            session = session_loader.load_session(path)
        except Exception as exc:
            out["errors"].append(f"{path}: {exc}")
            continue
        trials = list(session.trials)
        out["sessions"].append({
            "path": str(path),
            "subject": session.meta.get("subject"),
            "wear_id": session.meta.get("wear_id"),
            "mode": session.meta.get("mode"),
            "sensors_seen": session.meta.get("sensors_seen"),
            "sensors_enabled": session.meta.get("sensors_enabled"),
            "n_trials": len(trials),
        })
        out["n_trials"] += len(trials)
        seen = session.meta.get("sensors_seen")
        if seen is not None and seen not in out["sensors_seen"]:
            out["sensors_seen"].append(seen)
        for trial in trials:
            if trial.label:
                labels.append(trial.label)
                per_label[trial.label] = per_label.get(trial.label, 0) + 1
            value = (trial.attrs or {}).get("sensors_seen")
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            if value is not None:
                trial_seen[value] = trial_seen.get(value, 0) + 1

    out["labels"] = sorted(set(labels))
    out["n_labels"] = len(out["labels"])
    out["trials_per_label"] = per_label
    out["trial_sensors_seen"] = trial_seen
    # The one line worth putting on screen: how many trials were recorded
    # with something other than both sensors streaming.
    out["trials_missing_a_sensor"] = sum(
        n for value, n in trial_seen.items() if value != "AB")
    # ⚠ Distinguish "checked, and every trial had both" from "these files
    # predate the per-trial field, so nothing was checked". Both would
    # otherwise show trials_missing_a_sensor == 0, and the second one is not
    # reassurance -- it is an absence of evidence. Same trap as the `""` vs
    # missing distinction on sensors_seen itself.
    out["trial_sensors_seen_available"] = bool(trial_seen)
    return out


#: Intermediate arrays that are large and that nothing downstream reads.
#: `permutation_scores` is a thousand floats per modality -- the null
#: distribution the p-value was computed FROM, not a conclusion. Shipping it
#: on every /verify/state poll is waste even when it serialises.
_EXTRAS_DROP_KEYS = ("permutation_scores", "null_scores", "permutation_distribution")


def _serialisable_extras(extras):
    """Make `extras` safe for json.dumps, recursively.

    D19 hands back sklearn output, so the tree contains numpy arrays and
    numpy scalars, and `json.dumps` raises on the first one it meets.
    `_json_safe()` does not cover this -- it handles NaN, not ndarray.

    Large intermediate arrays are dropped rather than converted: they are
    what the p-value was derived from, not the answer, and the answer is
    what the panel shows.
    """
    if isinstance(extras, dict):
        return {k: _serialisable_extras(v) for k, v in extras.items()
                if k not in _EXTRAS_DROP_KEYS}
    if isinstance(extras, (list, tuple)):
        return [_serialisable_extras(v) for v in extras]
    if isinstance(extras, np.ndarray):
        return _serialisable_extras(extras.tolist())
    if isinstance(extras, np.generic):        # np.float64, np.int64, np.bool_
        return _serialisable_extras(extras.item())
    return extras


def serialize_verify_report(report, run_id, out_dir, elapsed_s):
    """把 `D15` 的報告轉成前端吃得下的 JSON。

    🔴 **三種狀態（`fail` / `skipped` / `error`）原樣傳出去，不在這裡合併。**
    它們的意思完全不同——`fail` 是「跑了沒達標」（一個結果）、`skipped` 是
    「資料不足」（一個缺口）、`error` 是「程式炸了」（一個 bug）。序列化層
    把它們併成「不 OK」，前端就再也分不出來，而使用者會把缺口讀成失敗。

    ⚠️ **改這個函式之前先讀一次「最後一關」那段註解**（見 `_LAST_GATE_NOTE`）：
    這裡是一個白名單，而白名單的預設行為是**丟掉**。
    """
    return {
        "run_id": run_id,
        "out_dir": str(out_dir),
        "finished_at": datetime.now().isoformat(),
        "elapsed_s": round(elapsed_s, 1),
        "is_synthetic": report["is_synthetic"],
        "session_paths": report["session_paths"],
        "session_summary": summarize_verify_sessions(report["session_paths"]),
        "matrix": report["matrix"],
        # D19 的置換檢定 p 值就在這裡面——「這不是運氣」的證據。
        # build_report() 一直有算，只是從來沒有被序列化出去。
        "extras": _serialisable_extras(report.get("extras", {})),
        "outcomes": [o.to_dict() for o in report["outcomes"]],
        "inconsistencies": report["inconsistencies"],
        "limitations": report["limitations"],
        "blocking": [o.key for o in report["blocking"]],
        "counts": {
            "failed": len(report["failed"]),
            "skipped": len(report["skipped"]),
            "errored": len(report["errored"]),
        },
    }


def run_verification(session_paths, *, fast, real, ablation_permutations):
    """背景執行緒：跑一輪驗證並寫出報告。

    **直接 import 呼叫 `analysis.run_all`，不 shell 出去跑 CLI**——
    subprocess 會讓例外變成一串 stderr 文字，而這裡要能把
    `ExperimentOutcome` 原樣交給前端。
    """
    from analysis import run_all as verifier
    from analysis.reporting import session_loader
    from analysis.reporting.verification_report import build_report

    run_id = verify_state["run_id"]
    out_dir = verify_dir() / run_id
    started = time.monotonic()
    error = None
    payload = None
    try:
        sessions = [session_loader.load_session(p) for p in session_paths]
        verifier.apply_style()
        outcomes, extras, notes, side_reports = verifier.run_experiments(
            sessions, fast=fast, is_synthetic=not real,
            ablation_permutations=ablation_permutations)
        elapsed = time.monotonic() - started
        report = build_report(outcomes, is_synthetic=not real, extras=extras,
                              session_paths=[s.path for s in sessions],
                              elapsed_s=elapsed)
        verifier.write_outputs(report, out_dir, notes, side_reports)
        payload = ensure_serialisable(
            serialize_verify_report(report, run_id, out_dir, elapsed),
            run_id, out_dir, elapsed)
    except Exception as exc:                     # noqa: BLE001 — 背景執行緒
        # 背景執行緒的例外沒有人接。不抓的話這一輪會靜靜地永遠停在
        # `running=True`，而畫面上只會看到秒數一直加上去。
        error = f"{type(exc).__name__}: {exc}"
        print(f"[bridge] /verify/run 失敗: {error}", file=sys.stderr)
        traceback.print_exc()
    finally:
        with verify_lock:
            verify_state["running"] = False
            verify_state["started_monotonic"] = None
            verify_state["last_error"] = error
            if payload is not None:
                verify_state["last_run"] = payload
        broadcaster.publish(verify_status())


def ensure_serialisable(payload, run_id, out_dir, elapsed_s):
    """Return `payload` if it survives json.dumps, else a degraded stand-in.

    Storing a payload that cannot be serialised poisons the endpoint
    permanently: `/verify/state` re-serialises `last_run` on every poll, so
    one bad value makes every future request fail, and re-running does not
    help because the new run produces the same field. Only restarting the
    bridge clears it. That happened -- D19 returns sklearn arrays, and one
    ndarray took the whole endpoint down.

    So: check before storing. Whatever else is wrong, the panel keeps
    working and gets told what happened, and the files on disk (summary.md,
    figures, side reports) were written before this point and are fine
    regardless -- which is worth saying out loud, because "the screen is
    broken" reads like "the run is lost".

    The `_LAST_GATE_NOTE` rule is "default to passing it through, not
    dropping it". This is its other half: **check it can be passed through
    before you commit to it.**
    """
    try:
        json.dumps(_json_safe(payload), ensure_ascii=False, allow_nan=False)
        return payload
    except (TypeError, ValueError) as exc:
        print(f"[bridge] ⚠ verify payload is not serialisable ({exc}); "
              f"degrading last_run rather than poisoning /verify/state",
              file=sys.stderr)
        return {
            "run_id": run_id,
            "out_dir": str(out_dir),
            "finished_at": datetime.now().isoformat(),
            "elapsed_s": round(elapsed_s, 1),
            "serialization_error": f"{type(exc).__name__}: {exc}",
            "note": ("驗證已經跑完，報告檔案（summary.md / figures / "
                     "side reports）都正常寫到硬碟了；壞掉的只有這一步的 "
                     "JSON 序列化。請直接看 /verify/reports 的檔案。"),
            "matrix": [], "outcomes": [], "blocking": [],
            "inconsistencies": [], "limitations": [], "extras": {},
        }


def list_verify_runs():
    """歷史報告，新到舊。

    每一輪寫進 `reports/verification/<run_id>/`（時間戳）而不是固定目錄——
    **固定目錄的話 `C23` 的「並排比較兩份」永遠只有一份**，而「這次調整有
    沒有變好」正是這個工具存在的理由。
    """
    root = verify_dir()
    runs = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        summary = entry / "summary.md"
        runs.append({
            "run_id": entry.name,
            "modified_at": datetime.fromtimestamp(entry.stat().st_mtime).isoformat(),
            "has_summary": summary.is_file(),
            "files": sorted(f.name for f in entry.iterdir() if f.is_file()),
            "figures": list_run_figures(entry),
        })
    return sorted(runs, key=lambda r: r["run_id"], reverse=True)


def list_run_figures(run_dir):
    """Relative paths of every figure in one run, e.g. "figures/c_silhouette.png".

    `files` above only ever listed the run directory's own entries, and
    `write_outputs()` puts every plot in a `figures/` subdirectory -- so the
    figures existed, were already served correctly by
    `GET /verify/reports/<id>/<path>`, and yet no caller could learn their
    names. The panel had no way to render a single plot, which is the part
    of the verification page people actually look at.

    Returned relative to the run directory (not just the basename) so the
    value can be appended to the run's URL as-is; the static route resolves
    subdirectories, so no reassembly is needed on the other side.
    """
    figures = []
    for sub in ("figures",):
        directory = run_dir / sub
        if not directory.is_dir():
            continue
        for f in sorted(directory.iterdir()):
            if f.is_file():
                figures.append(f"{sub}/{f.name}")
    return figures


def replay_poller(interval=0.02):
    """Publishes due replay events. Mirrors trial_ticker: the controller has
    no timer of its own, so something has to drive it."""
    while True:
        time.sleep(interval)
        with replay_lock:
            controller = replay_state["controller"]
            if controller is None:
                continue
            try:
                # No `now` argument: the controller anchors on its own
                # monotonic clock. Handing it a timestamp from a different
                # clock is exactly how the B04/B05 alignment ended up with a
                # slope of 5.9e7 -- not repeating that here.
                due = controller.poll()
            except Exception as exc:
                print(f"[bridge] replay poll failed: {exc}")
                continue
        for event in due:
            broadcaster.publish(event)


def replay_status():
    controller = replay_state["controller"]
    if controller is None:
        return {"type": "replay", "state": "idle", "active": False}
    return {
        "type": "replay",
        "state": "finished" if controller.finished else
                 ("paused" if controller.paused else "playing"),
        "active": controller.is_active,
        "file": replay_state["file"],
        "speed": controller.speed,
        "paused": controller.paused,
        "finished": controller.finished,
        "current_trial_idx": controller.current_trial_idx,
    }


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
    """B14 備援路線：WAV 存好之後算 log-Mel，存成一份 `.npy`。

    在自己的執行緒跑，不擋 serial_reader 讀下一行（MFCC 只要 ~0.1s，
    但沒必要冒險在 GIL 底下卡到即時的 $TOF 解析）。

    以前這裡還會把 log-mel 寫進 `--h5-session` 指定的 HDF5，那條路徑
    已經移除：它產生的 `mel` dataset 沒有配對的 `mel_t_us`（違反 schema
    §2 的成對要求），而正式的 trial 寫入早就由 `/trial/*` 那條路徑的
    `SessionWriter` 負責了。
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

    broadcaster.publish(event)


def serial_reader(port, baud, allow_v1=False):
    """Runs for the lifetime of the process; pauses itself while `flashing`
    is set so the flash subprocess can have the port exclusively.

    The port is no longer fixed at startup: `POST /device/connect` bumps
    `serial_target["generation"]`, and the loop notices, closes what it has
    and opens the new one. Reopening a connection rather than restarting the
    process keeps every HTTP client, SSE subscriber and open session alive
    across a replug -- and that board's connector works loose, so replugging
    is not an edge case.
    """
    import serial

    with serial_target_lock:
        serial_target.update(port=port, baud=baud, allow_v1=allow_v1)

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

        with serial_target_lock:
            port = serial_target["port"]
            baud = serial_target["baud"]
            allow_v1 = serial_target["allow_v1"]
            generation = serial_target["generation"]
            connected = serial_target["connected"]

        if not connected or not port:
            # Deliberately disconnected. Nothing is published: a stale-stream
            # alarm here would tell the user to go and check the cabling of a
            # board they themselves just unplugged.
            time.sleep(0.2)
            continue

        try:
            with serial_lock:
                ser = serial.Serial(port, baud, timeout=1)
            current_serial_holder["ser"] = ser
            # Order matters: clear everything the previous link taught us
            # FIRST, then build the new parser. reset_link_observations()
            # nulls protocol_state["parser"], so creating it above would
            # hand the reader a None to call .feed() on.
            reset_link_observations()
            # A fresh parser per connection: version negotiation state
            # belongs to one link, and reconnecting -- to a different board
            # or after a reflash -- means reading the new firmware's $STATUS
            # from scratch.
            protocol_state["parser"] = ProtocolParser(allow_v1=allow_v1)
            serial_link.update(port=port, opened_at=time.time(),
                               first_line_at=None, silent_warned=False)
            print(f"[bridge] serial open: {port} @ {baud}")
            # "opened", not "connected": the port opening tells us nothing
            # about whether the board is talking. On the real board the port
            # opened cleanly while neither ToF sensor produced a line.
            broadcaster.publish({"type": "link", "state": "up",
                                 "port": port, "data_seen": False})
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
                last_hello = 0.0
                while not flashing.is_set():
                    with serial_target_lock:
                        if serial_target["generation"] != generation:
                            break          # asked to move to another port

                    # A port that opens but never speaks: almost always the
                    # wrong device (there are 32 /dev/ttyS* on this machine
                    # and every one of them opens cleanly). Say which it is
                    # rather than leaving the panel spinning.
                    opened_at = serial_link["opened_at"]
                    if (serial_link["first_line_at"] is None
                            and not serial_link["silent_warned"]
                            and opened_at
                            and time.time() - opened_at > SILENT_PORT_TIMEOUT_S):
                        serial_link["silent_warned"] = True
                        broadcaster.publish({
                            "type": "link", "state": "up", "port": port,
                            "data_seen": False, "silent": True,
                            "message": (f"{port} 開得起來，但 "
                                        f"{SILENT_PORT_TIMEOUT_S:.0f} 秒內沒有收到任何"
                                        f"認得的資料——可能不是這塊板子，"
                                        f"或板子沒有在送資料。"),
                        })

                    label = ping_request["label"]
                    if label:
                        ping_request["label"] = None
                        run_ping_burst(ser, label)

                    # Keep asking until the protocol is negotiated. One PING
                    # on connect is not enough: the reply can be lost on a
                    # noisy line, and on a loaded machine a panel can open
                    # before the first one has been processed -- either way
                    # the link would sit at proto_confirmed=False forever,
                    # with the frame parameters from #1.1.2 never arriving.
                    parser = protocol_state["parser"]
                    if parser is not None and not parser.proto_confirmed:
                        now = time.monotonic()
                        if now - last_hello > 2.0:
                            last_hello = now
                            try:
                                with serial_write_lock:
                                    ser.write(b"PING\n")
                            except Exception as exc:
                                print(f"[bridge] hello PING failed: {exc}")

                    raw = ser.readline()
                    if not raw:
                        continue
                    # Counted at the port, before parsing: malformed lines
                    # and the base64 dump occupy real link capacity, and the
                    # bandwidth metric is about how full the link is, not
                    # how much of it turned out to be useful.
                    quality.note_bytes(len(raw))
                    if serial_link["first_line_at"] is None and raw.strip():
                        serial_link["first_line_at"] = time.time()
                        broadcaster.publish({"type": "link", "state": "up",
                                             "port": port, "data_seen": True})
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
                serial_link.update(port=None, opened_at=None, first_line_at=None)
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

    # --- D09: /recognize, /templates --------------------------------

    def _handle_templates(self):
        """GET /templates -- list_templates() is RecognitionService's own
        method, written for exactly this endpoint (analysis/similarity/
        recognition_service.py's docstring names it explicitly)."""
        service, error = _load_recognition_service()
        if service is None:
            self._send_json(200, {"loaded": False, "reason": error})
            return
        self._send_json(200, {"loaded": True, **service.list_templates()})

    def _handle_recognize(self):
        """POST /recognize -- returns a CONTRACTS.md §4.3 TriResult.

        Optional body {"trial_id": "trial_003"}: re-reads that trial's
        already-recorded tof/mel back out of the current session's HDF5
        file. Without trial_id: live capture from the last
        RECOGNIZE_WINDOW_S seconds of session_aligner's buffer.

        §4.3 requires d_tof_raw/d_mel_raw (quiz.js's reject_fused() cannot
        work without them, verified in reports/REJECT_PATH.md) -- _json_safe
        does not handle np.ndarray, so the four distance arrays are
        .tolist()'d here explicitly.
        """
        from host.features.live_pipeline import (
            InsufficientFramesError, assemble_query_from_aligned_frames,
        )

        body = self._read_json_body()
        if body is None:
            return

        service, error = _load_recognition_service()
        if service is None:
            self._send_json(503, {"error": "尚無 enrollment 樣板，無法辨識", "reason": error})
            return

        trial_id = body.get("trial_id")
        try:
            if trial_id:
                h5_path = session_runtime["h5_path"]
                if not h5_path:
                    self._send_json(409, {"error": "目前沒有開啟中的 session，無法用 trial_id 查詢"})
                    return
                frames = _frames_from_stored_trial(h5_path, trial_id)
                baseline = read_baseline_thresholds(h5_path)
                mu_A, sigma_A = baseline.get("baseline_mu_A"), baseline.get("baseline_sigma_A")
                mu_B, sigma_B = baseline.get("baseline_mu_B"), baseline.get("baseline_sigma_B")
            else:
                baseline_outcome = session_runtime["baseline"]
                if baseline_outcome is None:
                    self._send_json(409, {"error": "尚無 session baseline，無法即時辨識（先 POST /session/baseline）"})
                    return
                frames = _frames_from_live_session(RECOGNIZE_WINDOW_S)
                mu_A, sigma_A = baseline_outcome.baseline_mu_A, baseline_outcome.baseline_sigma_A
                mu_B, sigma_B = baseline_outcome.baseline_mu_B, baseline_outcome.baseline_sigma_B

            if mu_A is None or mu_B is None:
                self._send_json(409, {"error": "baseline 缺少 mu/sigma，無法計算 ToF 特徵"})
                return

            query = assemble_query_from_aligned_frames(frames, mu_A, sigma_A, mu_B, sigma_B)
        except InsufficientFramesError as exc:
            self._send_json(422, {"error": str(exc)})
            return
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return

        # query.data (fixed T=24) matches RecognitionService's default
        # dist_method="cosine". dist_method selection isn't exposed on this
        # endpoint yet -- out of scope for this pass, see completion report.
        tri, latency_ms = service.recognize(query.data)

        self._send_json(200, {
            "classes": list(tri.classes),
            "d_tof": tri.d_tof.tolist(), "d_mel": tri.d_mel.tolist(),
            "d_tof_raw": tri.d_tof_raw.tolist(), "d_mel_raw": tri.d_mel_raw.tolist(),
            "reject_tof": bool(tri.reject_tof), "reject_mel": bool(tri.reject_mel),
            "tau": float(tri.tau),
            "theta_reject_tof": float(tri.theta_reject_tof),
            "theta_reject_mel": float(tri.theta_reject_mel),
            "dist_method": "cosine",
            "latency_ms": latency_ms,
        })

    # --- D08 (partial): /templates/build --------------------------------

    def _handle_templates_build_start(self):
        """POST /templates/build -- 202 立刻回，背景跑（跟 /verify/run 同一個
        理由：建樣板要跑 Aligner + 特徵組裝，不能卡住 HTTP 執行緒）。

        body 可選 {"session": "<path>"}；預設用目前這個 session
        （session_runtime["h5_path"]）——使用者剛錄完，他要的就是這一個，
        不該逼他選。subject/wear_id 一律從 session 自己的 /meta 讀，不吃
        request 傳的（避免跟檔案裡實際的值兜不起來）。
        """
        body = self._read_json_body()
        if body is None:
            return

        with templates_build_lock:
            if templates_build_state["running"]:
                self._send_json(409, {"error": "已經有一輪建樣板在跑",
                                      "started_at": templates_build_state["started_at"]})
                return

        raw_session = body.get("session")
        if raw_session:
            session_path = resolve_session_file(str(raw_session))
            if session_path is None:
                self._send_json(400, {"error": f"找不到或不允許的 session: {raw_session}"})
                return
        else:
            h5_path = session_runtime.get("h5_path")
            if not h5_path:
                self._send_json(400, {"error": "沒有指定 session，也沒有進行中的 session"})
                return
            session_path = Path(h5_path)
            if not session_path.is_file():
                self._send_json(409, {"error": f"目前的 session 檔案還不存在（{session_path}）"
                                               "——至少要先擷取過一次 baseline"})
                return

        # Confirmed live (2026-08-26): a session file still held open by
        # SessionWriter cannot be opened for reading at the same time (no
        # SWMR mode in use anywhere in this repo) -- check the common case
        # (this is our own session's own writer) up front for a fast,
        # specific message; the generic BlockingIOError catch below still
        # covers any other reason the same file might be locked.
        if session_runtime.get("writer") is not None and Path(session_runtime.get("h5_path") or "") == session_path:
            self._send_json(409, {"error": "這個 session 還在錄製中（檔案被寫入中，無法同時讀取）"
                                           "——請先按「結束 session」再建樣板"})
            return

        try:
            from analysis.reporting.session_loader import load_session
            peek = load_session(session_path)
        except BlockingIOError:
            self._send_json(409, {"error": "這個 session 還在錄製中（檔案被寫入中，無法同時讀取）"
                                           "——請先按「結束 session」再建樣板"})
            return
        except Exception as exc:
            self._send_json(400, {"error": f"讀不到這個 session: {exc}"})
            return

        subject = peek.meta.get("subject")
        wear_id = peek.meta.get("wear_id")
        if not subject or wear_id is None:
            self._send_json(400, {"error": "這個 session 的 /meta 缺少 subject 或 wear_id，無法決定樣板檔名"})
            return

        # Not enrollment.template_path(root, subject, wear_id) -- that helper
        # expects `root` to be the repo root and appends "templates/" itself
        # (its own doc: "templates/<subject>_<wear_id>.npz"). runtime_paths
        # ["templates"] is already the templates directory (see --templates-
        # dir's help text above), so going through template_path() here
        # would double it into ".../templates/templates/...". Same filename
        # convention, applied directly instead.
        out_path = Path(runtime_paths["templates"]) / f"{subject}_{wear_id}.npz"

        with templates_build_lock:
            templates_build_state.update({
                "running": True,
                "started_at": datetime.now().isoformat(),
                "started_monotonic": time.monotonic(),
                "last_error": None,
            })

        threading.Thread(
            target=run_templates_build,
            args=([str(session_path)], str(out_path), subject, int(wear_id)),
            daemon=True,
        ).start()

        broadcaster.publish(templates_build_status())
        self._send_json(202, {
            "session": str(session_path), "subject": subject, "wear_id": int(wear_id),
            "out_path": str(out_path),
        })

    def _handle_templates_build_state(self):
        self._send_json(200, templates_build_status())

    def do_GET(self):
        if self.path.startswith("/session/current"):
            self._handle_session_current()
        elif self.path.startswith("/session/prefill"):
            self._send_json(200, session_registry.get_prefill())
        elif self.path.startswith("/baseline"):
            self._handle_get_baseline()
        elif self.path.startswith("/pca"):
            self._handle_pca()
        elif self.path.startswith("/config/"):
            self._handle_config()
        # ⚠️ 順序有意義（跟 /verify/reports 那條同一個理由）：
        # `/templates/build/state` 必須比 `/templates` 先比對，否則
        # 後者的 prefix match 會把它吃掉。
        elif self.path.startswith("/templates/build/state"):
            self._handle_templates_build_state()
        elif self.path.startswith("/templates"):
            self._handle_templates()
        elif self.path.startswith("/replay/sessions"):
            self._handle_replay_sessions()
        elif self.path.startswith("/replay/state"):
            self._send_json(200, replay_status())
        elif self.path.startswith("/device/ports"):
            self._handle_device_ports()
        elif self.path.startswith("/device/status"):
            self._send_json(200, device_status())
        elif self.path.startswith("/verify/state"):
            self._send_json(200, verify_status())
        # ⚠️ 順序有意義：`/verify/reports/<id>/<path>` 必須比
        # `/verify/reports` 先比對，否則列表那條會把靜態檔的請求吃掉。
        elif self.path.startswith("/verify/reports/"):
            self._serve_verify_asset()
        elif self.path.startswith("/verify/reports"):
            self._send_json(200, list_verify_runs())
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
        elif self.path.startswith("/device/connect"):
            self._handle_device_connect()
        elif self.path.startswith("/device/disconnect"):
            self._handle_device_disconnect()
        elif self.path.startswith("/trial/"):
            self._handle_trial()
        elif self.path.startswith("/recognize"):
            self._handle_recognize()
        elif self.path.startswith("/templates/build"):
            self._handle_templates_build_start()
        elif self.path.startswith("/replay/"):
            self._handle_replay()
        elif self.path.startswith("/verify/run"):
            self._handle_verify_run()
        else:
            self.send_error(404)

    # -- B19/C23: /verify/* -------------------------------------------------

    _VERIFY_MIME = {
        ".md": "text/markdown; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".png": "image/png",
        ".pdf": "application/pdf",
        ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }

    def _handle_verify_run(self):
        """`POST /verify/run` —— 202 立刻回，背景跑。

        跑一輪要幾秒到兩分鐘。同步跑會讓 HTTP 連線掛在那裡、瀏覽器逾時，
        而且第二個請求會排在後面看起來像當掉。
        """
        body = self._read_json_body()
        if body is None:
            return

        sessions = body.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            self._send_json(400, {"error": "sessions 必須是非空的陣列"})
            return

        resolved = []
        for raw in sessions:
            path = resolve_session_file(str(raw))
            if path is None:
                # 同一套 resolve-then-contain：這是 HTTP body 裡的路徑，
                # 裡面的 `..` 會讀到這個 process 摸得到的任何檔案。
                self._send_json(400, {"error": f"找不到或不允許的 session: {raw}"})
                return
            resolved.append(path)

        try:
            permutations = int(body.get("ablation_permutations", 200))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "ablation_permutations 必須是整數"})
            return
        if permutations < 0:
            self._send_json(400, {"error": "ablation_permutations 不可為負"})
            return

        with verify_lock:
            if verify_state["running"]:
                self._send_json(409, {
                    "error": "已經有一輪驗證在跑",
                    "run_id": verify_state["run_id"],
                    "started_at": verify_state["started_at"],
                })
                return
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            verify_state.update({
                "running": True,
                "run_id": run_id,
                "started_at": datetime.now().isoformat(),
                "started_monotonic": time.monotonic(),
                "last_error": None,
                # `last_run` 刻意不動——見 `verify_state` 的註解。
            })

        threading.Thread(
            target=run_verification,
            args=([str(p) for p in resolved],),
            kwargs={
                "fast": bool(body.get("fast", False)),
                "real": bool(body.get("real", False)),
                "ablation_permutations": permutations,
            },
            daemon=True,
        ).start()

        broadcaster.publish(verify_status())
        self._send_json(202, {
            "run_id": run_id,
            "sessions": [str(p) for p in resolved],
            # 前端要能顯示「這一輪是用什麼參數跑的」——尤其
            # `ablation_permutations`：預設 200 是為了快，正式報告要 1000。
            "fast": bool(body.get("fast", False)),
            "real": bool(body.get("real", False)),
            "ablation_permutations": permutations,
        })

    def _handle_device_connect(self):
        """`POST /device/connect` —— `{"port": "/dev/ttyUSB0"}`。"""
        body = self._read_json_body()
        if body is None:
            return
        port = (body.get("port") or "").strip()
        if not port:
            self._send_json(400, {"error": "需要 port 欄位，例如 "
                                           "{\"port\": \"/dev/ttyUSB0\"}"})
            return

        if session_registry.current is not None:
            # Same shape as the other "not while something is in flight"
            # refusals. Swapping the port mid-session would leave the HDF5
            # holding data from two different boards under one set of /meta.
            self._send_json(409, {
                "error": "session 進行中，不能切換序列埠。請先結束 session。",
                "session_id": session_registry.current.session_id})
            return
        if flashing.is_set():
            self._send_json(409, {"error": "正在燒錄，序列埠被佔用中"})
            return

        ok, message = probe_serial_port(port)
        if not ok:
            self._send_json(409, {"error": message, "port": port})
            return

        request_serial_port(port, connected=True)
        # 202, not 200: the port is *going* to be opened by the reader
        # thread, and "opened" still is not "the board is talking" -- the
        # panel should wait for link/data_seen before claiming connected.
        self._send_json(202, {"accepted": True, "port": port,
                              "state": "connecting",
                              "note": "序列埠即將開啟。收到第一行資料前，"
                                      "狀態會停在 opened，不代表板子有在送東西。"})

    def _handle_device_disconnect(self):
        """`POST /device/disconnect` —— 使用者主動斷開，不是故障。"""
        if session_registry.current is not None:
            self._send_json(409, {
                "error": "session 進行中，不能中斷連線。請先結束 session。",
                "session_id": session_registry.current.session_id})
            return
        request_serial_port(None, connected=False)
        ser = current_serial_holder.get("ser")
        if ser is not None:
            try:
                ser.close()          # unblock a readline() sitting on timeout
            except Exception:
                pass
        self._send_json(200, {"disconnected": True, "state": "disconnected"})

    def _handle_device_ports(self):
        """`GET /device/ports` —— 掃描可用的序列埠。"""
        ports, error = list_serial_ports()
        payload = {
            "ports": ports,
            "connected_port": serial_link["port"],
            "likely": [p["device"] for p in ports if p["likely_esp32"]],
        }
        if error:
            payload["error"] = error
        if not ports:
            payload["hint"] = "找不到任何序列埠。確認板子已插上 USB。"
        elif not payload["likely"]:
            # Say so explicitly rather than leaving an all-false list to be
            # read as "none of these work": the board may simply be behind
            # an adapter this build does not recognise.
            payload["hint"] = ("找不到明顯是 ESP32 的埠（可能是沒插上，"
                               "或是用了不認得的 USB 轉接晶片）。"
                               "下面列的是這台機器上全部的序列埠，可以手動選。")
        self._send_json(200, payload)

    def _serve_verify_asset(self):
        """`GET /verify/reports/<run_id>/<path>` —— 唯讀靜態服務。"""
        rel = unquote(self.path[len("/verify/reports/"):])
        rel = rel.split("?", 1)[0].split("#", 1)[0]
        if not rel or rel.endswith("/"):
            self.send_error(404)
            return

        # 同 `_serve_panel_asset()` 的 resolve-then-contain：報告目錄底下
        # 有真的子目錄（`figures/`），所以不能用「只取檔名」那種擋法。
        try:
            root = verify_dir().resolve()
            path = (root / rel).resolve()
            path.relative_to(root)
        except (ValueError, OSError):
            self.send_error(403)
            return
        if not path.is_file():
            self.send_error(404)
            return

        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self._VERIFY_MIME.get(
            path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- B17: replay --------------------------------------------------------

    def _handle_replay_sessions(self):
        """List what can be replayed, newest first."""
        try:
            files = sorted(sessions_dir().glob("*.h5"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, [
            {"file": f.name, "path": str(f),
             "bytes": f.stat().st_size,
             "modified_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat()}
            for f in files
        ])

    def _handle_replay(self):
        action = self.path[len("/replay/"):].split("?", 1)[0].strip("/")
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if action == "start":
            return self._handle_replay_start(query)

        with replay_lock:
            controller = replay_state["controller"]
            if controller is None:
                self._send_json(409, {"error": "沒有進行中的回放"})
                return
            try:
                extra = self._apply_replay_action(controller, action, query)
            except LookupError:
                self._send_json(404, {"error": f"未知的 replay 動作: {action}"})
                return
            except (ValueError, TrialNotFoundError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            status = replay_status()
        if extra:
            status.update(extra)
        broadcaster.publish(status)
        self._send_json(200, status)

    def _apply_replay_action(self, controller, action, query):
        if action == "control":
            m = re.search(r"action=(\w+)", query)
            sub = m.group(1) if m else ""
            if sub == "pause":
                controller.pause()
            elif sub == "resume":
                controller.resume()
            elif sub == "step":
                event = controller.step()
                if event:
                    broadcaster.publish(event)
                return {"stepped": event is not None}
            else:
                raise ValueError("action 必須是 pause|resume|step")
            return None
        if action == "speed":
            m = re.search(r"value=([\d.]+)", query)
            if not m:
                raise ValueError("speed 需要 value 參數")
            controller.set_speed(float(m.group(1)))
            return None
        if action == "seek":
            m = re.search(r"trial=(\d+)", query)
            if not m:
                raise ValueError("seek 需要 trial 參數")
            controller.seek_to_trial(int(m.group(1)))
            return None
        if action == "stop":
            replay_state["controller"] = None
            replay_state["file"] = None
            return None
        raise LookupError(action)

    def _handle_replay_start(self, query):
        m = re.search(r"file=([^&]+)", query)
        if not m:
            self._send_json(400, {"error": "start 需要 file 參數"})
            return
        raw = unquote(m.group(1))
        path = resolve_session_file(raw)
        if path is None:
            # Deliberately the same answer for "outside the sessions
            # directory" and "does not exist": a different message for each
            # would let a caller probe the filesystem.
            self._send_json(404, {"error": f"找不到可回放的 session: {raw}"})
            return

        m = re.search(r"start_trial=(\d+)", query)
        start_trial = int(m.group(1)) if m else 0
        try:
            events = read_session_events(path, start_trial)
            controller = ReplayController(events)
        except NoReplayEventsError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._send_json(400, {"error": f"讀取失敗: {exc}"})
            return

        with replay_lock:
            replay_state["controller"] = controller
            replay_state["file"] = str(path)
            status = replay_status()
        status["n_events"] = len(events)
        broadcaster.publish(status)
        self._send_json(200, status)

    # -- B11 / B12: trials ------------------------------------------------

    def _handle_trial(self):
        action = self.path[len("/trial/"):].split("?", 1)[0].strip("/")
        info = session_registry.current
        if info is None:
            self._send_json(409, {"error": "沒有進行中的 session"})
            return
        if not info.baseline_done:
            # B10's gate. Trials recorded against no baseline cannot be
            # normalised later, so they are not "slightly worse data" --
            # they are unusable, and better refused now than discovered in
            # analysis after a four-hour session.
            self._send_json(409, {"error": "還沒擷取 baseline，不能開始 trial",
                                  "baseline_done": False})
            return

        with trial_lock:
            machine = session_runtime.get("trial")
            if machine is None:
                machine, err = open_trial_machine(info)
                if machine is None:
                    self._send_json(409, {"error": err})
                    return

        body = self._read_json_body() if action in ("start", "hold/start", "reject") else {}
        if body is None:
            return

        try:
            with trial_lock:
                events = self._dispatch_trial(machine, action, body)
        except LookupError:
            # JSON, not send_error()'s HTML page: this is a JSON API and a
            # client that has to special-case the error body cannot report
            # what went wrong.
            self._send_json(404, {"error": f"未知的 trial 動作: {action}"})
            return
        except RuntimeError as exc:
            # The machine refuses transitions that are wrong for its current
            # state (e.g. abort during REST, where the trial is already
            # saved). 409 rather than 400: the request is well-formed, the
            # state is not what the caller assumed.
            self._send_json(409, {"error": str(exc), "state": machine.state.value})
            return
        except ValueError as exc:
            self._send_json(409, {"error": str(exc), "state": machine.state.value})
            return
        except Exception as exc:
            # Anything else is a bug here, not a client error -- but it must
            # still come back as a response. Letting it escape kills the
            # handler thread, and the caller sees the connection drop with
            # no clue what happened.
            traceback.print_exc()
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}",
                                  "state": machine.state.value})
            return

        try:
            publish_trial_events(events)
        except Exception:
            # The action already happened; failing to broadcast it must not
            # turn a successful request into a dropped connection.
            traceback.print_exc()
        self._send_json(200, {"state": machine.state.value,
                              "events": as_trial_events(events)})

    def _dispatch_trial(self, machine, action, body):
        """Map one /trial/<action> to the state machine. Returns its events."""
        device_t_us = device_clock["last_t_us"]
        # speaking_mode (B21) is validated at the call, not at save time, so
        # a bad value comes back as a 409 while the machine is still IDLE
        # rather than taking down a trial that has already been recorded.
        if action == "start":
            return as_trial_events(machine.start_trial(
                label=body.get("label"), speaking_mode=body.get("speaking_mode"),
                **_optional_trial_kwargs(machine.start_trial, body)))
        if action == "hold/start":
            return as_trial_events(machine.hold_start(
                device_t_us=device_t_us, label=body.get("label"),
                speaking_mode=body.get("speaking_mode"),
                **_optional_trial_kwargs(machine.hold_start, body)))
        if action == "hold/stop":
            return as_trial_events(machine.hold_stop(device_t_us=device_t_us))
        if action == "confirm":
            # B12's CONFIRM state: the trial is computed but NOT on disk
            # until this call. Discard is the other half, and the panel has
            # to offer both -- leaving it ambiguous would mean a trial that
            # is neither kept nor dropped.
            return as_trial_events(machine.confirm_keep())
        if action == "discard":
            return as_trial_events(machine.discard_pending())
        if action == "abort":
            return as_trial_events(machine.abort())   # skips this word
        if action == "redo":
            return as_trial_events(machine.redo())    # keeps the same word
        if action == "reject":
            # A different layer from abort/discard: those act on a trial that
            # is still in memory, this one on a trial already written to
            # HDF5. Rejecting does NOT delete it -- the data stays and only
            # the quality attr changes, because D12's cross-validation needs
            # to know how many attempts went wrong during a given wear.
            idx = body.get("trial_idx")
            if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
                raise ValueError("reject 需要一個非負整數 trial_idx")
            machine.mark_current_trial_saved_quality(
                session_runtime["h5_path"], idx, "rejected")
            return [{"state": machine.state.value, "rejected_trial_idx": idx}]
        raise LookupError(action)

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
        session_frame_baseline.update(snapshot_frame_counts())
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
        machine, open_err = open_trial_machine(session_registry.current)
        if machine is None:
            print(f"[bridge] trial machine not opened: {open_err}")
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

    # -- shared config files ----------------------------------------------

    CONFIG_FILES = {
        "vocab": VOCAB_PATH,
        "quality_thresholds": THRESHOLDS_PATH,
        "session_targets": ROOT_DIR / "config" / "session_targets.json",
    }

    def _handle_config(self):
        """Serve config/*.json so the panel does not keep its own copy.

        A second copy is the failure this project has already paid for three
        times (schema_example.py missing mel_t_us, REQUIRED_META_KEYS behind
        the clock fields, the mock's v1 dialect drifting from the firmware).
        Read fresh on every request, which is also what makes "edit the JSON
        and the options change" true rather than true-until-restart.
        """
        name = self.path[len("/config/"):].split("?", 1)[0].strip("/")
        path = self.CONFIG_FILES.get(name)
        if path is None:
            self._send_json(404, {"error": f"未知的設定檔: {name}",
                                  "available": sorted(self.CONFIG_FILES)})
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            self._send_json(404, {"error": f"讀不到 {path.name}: {exc}"})
            return
        except ValueError as exc:
            # Malformed rather than missing: say so instead of pretending it
            # is absent, or someone will go looking for a file that is there.
            self._send_json(500, {"error": f"{path.name} 不是合法的 JSON: {exc}"})
            return
        self._send_json(200, payload)

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
            initial["source"] = link_source["value"]
            initial["sensors_seen"] = sensors_seen_string()
            initial["sensors_enabled"] = sensors_enabled_string() or ""
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


shutdown_event = threading.Event()


def _on_sigint(signum, frame):
    """Signal handler. Sets a flag and prints; touches nothing else.

    Deliberately does NOT close the HDF5 file here, and installing it at all
    is the point: with the default handler, Ctrl-C raises KeyboardInterrupt
    at whatever bytecode boundary the interpreter happens to be at -- and
    h5py's weakref cleanup callbacks are such a boundary. An exception raised
    inside a weakref callback is *swallowed* by Python, which prints
    "Exception ignored in:" and carries on. Measured at 5 times out of 6: the
    operator presses Ctrl-C, sees one line of noise, and the bridge keeps
    recording as if nothing happened. They then reach for something more
    dangerous, like pulling the power.

    Setting a flag moves "when do we shut down" from a random instant chosen
    by the signal to a checkpoint we control.
    """
    if shutdown_event.is_set():
        return  # a second Ctrl-C: let the default behaviour take over
    print(f"\n[bridge] 收到中斷訊號 ({signal.Signals(signum).name})，正在收尾…",
          flush=True)
    shutdown_event.set()


def close_session_writer():
    """Close the session HDF5 cleanly, at a point of our choosing."""
    with session_lock:
        writer = session_runtime.get("writer")
        session_runtime["writer"] = None
        session_runtime["trial"] = None
    if writer is None:
        print("[bridge] 沒有開著的 session 檔案。", flush=True)
        return
    try:
        writer.__exit__(None, None, None)
        print("[bridge] session 檔案已關閉。", flush=True)
    except Exception as exc:
        print(f"[bridge] 關閉 session 檔案時出錯: {exc}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyUSB0", help="ESP32 serial port")
    parser.add_argument("--baud", type=int, default=460800, help="must match CONFIG_ESP_CONSOLE_UART_BAUDRATE")
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument(
        "--source", choices=VALID_SOURCES, default="live",
        help="這條連線接的是什麼：live（真板子）/ mock（T04 合成）/ "
             "replay-log（T05 序列埠 log 重播）/ replay-session（HDF5 回放）。"
             "**不會自動偵測**——bridge 分辨不出 pty 與真實 UART，而錄錯來源"
             "會產生標記成真實量測的合成資料。")
    parser.add_argument(
        "--sessions-dir", default=None,
        help="session HDF5 檔的輸出目錄（預設 <repo>/sessions）")
    parser.add_argument(
        "--verification-dir", default=None,
        help="/verify/run 的報告輸出根目錄（預設 <repo>/reports/verification）。"
             "每一輪寫進底下的 <run_id>/ 子目錄——固定成同一個目錄的話，"
             "C23 的「並排比較兩份」永遠只有一份")
    parser.add_argument(
        "--last-session", default=None,
        help="表單預填用的 last_session.json 路徑（預設 config/last_session.json）")
    parser.add_argument(
        "--allow-v1", action="store_true",
        help="B02 降級模式：接受舊韌體的 $TOF/$MIC 行。預設關閉——v1 沒有 "
             "t_us，錄下來的 session 無法做時間對齊，所以必須明確打開。")
    parser.add_argument(
        "--templates-dir", default=None,
        help="D09 /recognize、/templates：enrollment 樣板 .npz 的目錄"
             "（預設 <repo>/templates，見 analysis/similarity/enrollment.py "
             "template_path()）。目錄裡沒有任何 .npz 時兩個端點都回明確的"
             "「尚無樣板」狀態，不是 500。")
    args = parser.parse_args()

    link_source["value"] = args.source
    if args.source != "live":
        print(f"[bridge] ⚠ source={args.source} —— 這條連線不是真板子，"
              f"錄下來的資料不可當成真實量測")

    global session_registry
    if args.sessions_dir:
        runtime_paths["sessions"] = Path(args.sessions_dir)
    if args.verification_dir:
        runtime_paths["verification"] = Path(args.verification_dir)
    if args.templates_dir:
        runtime_paths["templates"] = Path(args.templates_dir)
    if args.last_session:
        runtime_paths["last_session"] = Path(args.last_session)
        session_registry = SessionRegistry(runtime_paths["last_session"])

    reader = threading.Thread(
        target=serial_reader, args=(args.port, args.baud, args.allow_v1), daemon=True)
    reader.start()

    # Runs whether or not the link is up: a health dashboard that freezes
    # when something breaks is useless exactly when it is needed.
    threading.Thread(target=quality_emitter, daemon=True).start()
    threading.Thread(target=trial_ticker, daemon=True).start()
    threading.Thread(target=replay_poller, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    server.serial_port = args.port
    print(f"[bridge] panel: http://127.0.0.1:{args.http_port}/")
    print(f"[bridge] serial: {args.port} @ {args.baud}")

    # SIGINT only. SIGTERM and SIGKILL were measured as safe exactly as they
    # are (the in-progress trial disappears whole, everything before it reads
    # back), and they are safe *because* nothing Python-level runs -- adding a
    # handler would replace proven behaviour with untested behaviour.
    # SIGINT is the one that needs help: it is the only one that raises
    # through Python, and therefore the only one that can be swallowed.
    signal.signal(signal.SIGINT, _on_sigint)

    threading.Thread(target=server.serve_forever, daemon=True).start()
    shutdown_event.wait()
    close_session_writer()
    server.shutdown()
    print("[bridge] 已停止。")


if __name__ == "__main__":
    main()
