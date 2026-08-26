"""B19 -- the `quality` SSE event: link health computed once, on the host.

Drop rate, alignment residual, left/right symmetry and the rest all need a
sliding window and a bit of history. Computing them in the panel would mean
every mode re-implementing the same statistics, and losing all of it on a
mode switch. So the bridge computes them once, at 1 Hz, and every mode
subscribes to the same event.

Levels (green/yellow/red) are decided here too, not in the panel, and so is
the ``hint`` that goes with a non-green level: the metric's meaning lives on
this side, so the advice should as well. A panel that had to keep its own
"which message goes with which red light" table would drift from the metric
it is describing.

Thresholds and hints both come from ``config/quality_thresholds.json`` and
are re-read when the file changes, so tuning a threshold mid-session does not
mean restarting the bridge and losing the link.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

#: 8N1 framing: one byte costs ten bits on the wire. 460800 baud is
#: therefore ~46 kB/s, which is the figure CONTRACTS.md #1.4 budgets against.
BITS_PER_BYTE_ON_WIRE = 10

#: Percentile of recent $M rms taken as the noise floor. A low percentile,
#: not the mean: the floor is what the microphone reads when nobody is
#: speaking, and averaging in the speech would measure the speech instead.
NOISE_FLOOR_PERCENTILE = 10

DEFAULT_WINDOW_S = 30.0

#: Below this many clock buckets a residual fit is meaningless, so the
#: metric reports None rather than a number nobody should act on.
MIN_CLOCK_BUCKETS = 3

#: How long a stream may go completely silent before it is called stale, in
#: seconds. Per stream, because the rates differ by more than an order of
#: magnitude: $T runs at 30 Hz (10 Hz at 8x8), $M around 31 Hz, $F at 62.5 Hz
#: and $H at 1 Hz. One number for all of them would either cry wolf on the
#: heartbeat or take a minute to notice a dead sensor.
DEFAULT_STREAM_TIMEOUTS = {
    "tof_A": 3.0,
    "tof_B": 3.0,
    "mic": 3.0,
    "mel": 3.0,
    "heartbeat": 10.0,
}

METRIC_ORDER = (
    "drop_rate",
    "valid_zones",
    "symmetry",
    "clock_resid",
    "noise_floor",
    "bandwidth",
)


def _percentile(values, pct):
    """Nearest-rank percentile. Avoids a numpy dependency in this module."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


class ThresholdTable:
    """Thresholds + hints from JSON, re-read whenever the file changes.

    Hot reload is an acceptance requirement, and it is also what makes the
    thresholds tunable during a wear session: the person adjusting the
    headset is the one who knows whether 0.21 asymmetry is actually bad on
    this build, and they should not have to restart the link to find out.

    A file that has become unreadable or invalid is ignored -- the previous
    table stays live. Half-written JSON is a completely normal thing to
    observe when someone is editing the file, and dropping every metric to
    "unknown" because we caught the editor mid-save would be worse than
    being one edit stale.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._table: dict = {}
        self._mtime: float | None = None
        self.load_error: str | None = None
        self.reload()

    def reload(self) -> bool:
        """Re-read if the file changed. Returns True if the table changed."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            with self._lock:
                self.load_error = f"{self.path}: {exc}"
            return False
        with self._lock:
            if self._mtime == mtime:
                return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top level must be an object")
        except (OSError, ValueError) as exc:
            with self._lock:
                self.load_error = f"{self.path}: {exc}"
            return False
        with self._lock:
            self._table = {k: v for k, v in raw.items() if not k.startswith("_")}
            self._mtime = mtime
            self.load_error = None
        return True

    def spec(self, metric: str) -> dict | None:
        with self._lock:
            spec = self._table.get(metric)
        if not isinstance(spec, dict):
            return None
        if spec.get("green") is None or spec.get("yellow") is None:
            return None  # placeholder row, not yet filled in
        return spec

    def classify(self, metric: str, value) -> tuple[str, str | None]:
        """Return ``(level, hint)`` for one metric value.

        ``level`` is ``"unknown"`` when there is no value yet or no usable
        threshold row -- deliberately not "green". A metric nobody has
        configured is not a metric that is passing, and showing it green
        would hide exactly the thing the dashboard exists to surface.
        """
        if value is None:
            return "unknown", None
        spec = self.spec(metric)
        if spec is None:
            return "unknown", None

        green, yellow = spec["green"], spec["yellow"]
        if spec.get("direction") == "higher_better":
            level = "green" if value >= green else "yellow" if value >= yellow else "red"
        else:
            level = "green" if value <= green else "yellow" if value <= yellow else "red"

        hint = None if level == "green" else spec.get(f"{level}_hint")
        return level, hint


#: Human-readable names for the alarm text. The panel should be able to
#: print the message as-is; "sensor A has been silent for 12 s" is
#: actionable, "something is wrong" is not.
STREAM_LABELS = {
    "tof_A": "感測器 A",
    "tof_B": "感測器 B",
    "mic": "麥克風",
    "mel": "Mel 串流",
    "heartbeat": "裝置心跳",
}


def _stale_stream_message(item):
    label = STREAM_LABELS.get(item["stream"], item["stream"])
    return (f"{label} 已經 {item['silent_for_s']:.0f} 秒沒有任何資料"
            f"（門檻 {item['timeout_s']:.0f} 秒）。"
            f"這不是掉幀——掉幀看得到 seq 缺口，這條是**完全停了**，"
            f"而且可能不會自己恢復。"
            f"裝置端的 drop 計數在這種情況下會凍住不動，$H 上看起來跟正常一樣，"
            f"所以請直接檢查該感測器的接線。")


_DEAD_MIC_HINT = (
    "麥克風的 RMS 這段時間裡一直精確是 0，不是「偏低」——貼合骨傳導麥克風"
    "即使很安靜，量到的也是個位數（例如 4-6），精確的 0 代表根本沒有訊號"
    "進來，多半是接觸不良或線材鬆脫。這不是底噪好，是麥克風可能沒接上。"
)


def _transport_alarm_message(stream, host, device, malformed):
    """Explain a positive delta without over-claiming its cause.

    The original wording said "UART overrun or the bridge falling behind",
    which sounds authoritative and is often wrong. A line the parser rejected
    as malformed leaves exactly the same seq gap as a frame that never
    arrived -- and the firmware, whose $T line was merely spliced by an
    unlocked ESP_LOG write, has no idea its output was interrupted, so its
    own counter does not move either. Same symptom, different fault, and the
    old message would send someone to check the cabling.
    """
    base = (f"{stream}：主機算出 {host} 次掉幀，裝置只認 {device} 次。"
            f"差額為正代表這些幀沒有完整送達主機。")
    if malformed:
        return base + (
            f" ⚠ 但本連線已有 {malformed} 行被判定為畸形（解析失敗）——"
            f"被拒絕的行在 seq 缺口上跟「裝置沒送」完全一樣，"
            f"所以這個差額**不一定是傳輸損失**，也可能是韌體的 log "
            f"把 $T 行切開（ESP_LOG 不走 uart_out_lock）。"
            f"先看 malformed 是否同步上升，再去查線材。"
        )
    return base + " 沒有畸形行，所以比較可能是真的傳輸損失（UART overrun 或 bridge 跟不上）。"


class QualityAggregator:
    """Accumulates observations and renders the 1 Hz ``quality`` event.

    Every ``observe_*`` takes an event dict exactly as
    ``host.capture.protocol.ProtocolParser.feed()`` returns it, so the bridge
    can forward what it already parsed instead of re-deriving anything.

    Safe to call from the serial reader thread while the emitter thread is
    rendering a snapshot.
    """

    def __init__(
        self,
        thresholds: ThresholdTable,
        drop_tracker=None,
        clock_aligner=None,
        baud: int = 460800,
        window_s: float = DEFAULT_WINDOW_S,
        clock=time.monotonic,
        host_clock=time.time,
        parser_stats=None,
        sensors_seen=None,
        mel_enabled=None,
        stream_timeouts=None,
    ):
        self.thresholds = thresholds
        self.drop_tracker = drop_tracker
        self.clock_aligner = clock_aligner
        self.window_s = window_s
        self.capacity_bytes_per_s = baud / BITS_PER_BYTE_ON_WIRE
        self._clock = clock
        # Two clocks, deliberately. Sliding windows use the monotonic one
        # (an NTP step must not make a window suddenly span an hour), while
        # clock alignment samples use the wall clock -- because B05's PING
        # timestamps do, and a fit fed from both at once sees the offset
        # between them as a colossal slope. That is not hypothetical: it
        # produced clock_slope = 5.9e7 the first time these two were wired
        # together (B04 alone had been self-consistent, so nothing showed).
        self._host_clock = host_clock
        #: Callable returning ProtocolParser.stats.as_dict(), or None. The
        #: parser already counts malformed lines and the count already
        #: reaches this process -- it was simply never read here.
        self._parser_stats = parser_stats
        #: Callable returning which sensor labels have actually put data on
        #: the wire. Carried on the quality event because "only one sensor is
        #: streaming" is a health fact, and it is one the device itself never
        #: reports: a sensor that fails to initialise is announced over
        #: ESP_LOGE, which never reaches the host.
        self.sensors_seen = sensors_seen
        #: Callable returning whether $F is switched on, or None if unknown.
        #: A Mel stream turned off with MEL:0 is silent on purpose, and
        #: reporting that as a fault would train people to ignore the alarm.
        self.mel_enabled = mel_enabled
        self.stream_timeouts = dict(stream_timeouts or DEFAULT_STREAM_TIMEOUTS)
        #: Monotonic time each stream was last heard from. A stream that has
        #: never been heard from is absent here and is NOT reported stale:
        #: "never came up" is a different fault with its own signal
        #: (sensors_seen), and reporting both would double-count it.
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

        self._zones = deque()        # (t, n_valid, dim)
        self._sensor_mean = {"A": deque(), "B": deque()}   # (t, mean distance mm)
        self._rms = deque()          # (t, rms)
        self._bytes = deque()        # (t, n bytes)
        self._device_drops: dict[str, int] | None = None
        self._host_drops_at_heartbeat: dict[str, int] = {}

    # -- observations ---------------------------------------------------

    def note_bytes(self, n: int, now: float | None = None) -> None:
        """Raw bytes read off the serial port, for the bandwidth metric.

        Counted at the port rather than by re-serialising parsed events:
        malformed lines, the base64 recording dump and the log noise the
        firmware interleaves all consume real link capacity, and the point
        of this metric is how full the link is, not how much of it was
        useful.
        """
        now = self._clock() if now is None else now
        with self._lock:
            self._bytes.append((now, n))
            self._prune(now)

    def mark_seen(self, stream: str, now: float | None = None) -> None:
        """Record that `stream` just delivered something."""
        with self._lock:
            self._last_seen[stream] = self._clock() if now is None else now

    def stale_streams(self, now: float | None = None):
        """Streams that have gone silent for longer than their timeout.

        This answers a question the seq-gap detector structurally cannot.
        A gap is inferred from the distance between two *received* frames,
        so a stream that stops and never resumes produces no gap at all --
        there is no next frame to measure against. On the firmware side the
        same event is equally invisible: when the I2C bus drops,
        check_data_ready() itself fails and the whole block that increments
        drop_A/drop_B is skipped, so $H's counters freeze at whatever they
        were. A dead sensor and a perfectly healthy one look identical.

        Three distinct failures, three separate signals:
          malformed  -- it arrived, but the line was corrupt
          seq gap    -- it arrived, but some frames in between did not
          stale      -- nothing arrived, and nothing may ever arrive again
        """
        now = self._clock() if now is None else now
        mel_on = None
        if self.mel_enabled is not None:
            try:
                mel_on = self.mel_enabled()
            except Exception:
                mel_on = None

        out = []
        with self._lock:
            last_seen = dict(self._last_seen)
        for stream, limit in self.stream_timeouts.items():
            last = last_seen.get(stream)
            if last is None:
                continue          # never seen: sensors_seen covers that case
            if stream == "mel" and mel_on is False:
                continue          # switched off with MEL:0, not broken
            silent = now - last
            if silent > limit:
                out.append({"stream": stream,
                            "silent_for_s": round(silent, 2),
                            "timeout_s": limit})
        return out

    #: The streams that actually carry recognition data. `heartbeat` is
    #: deliberately excluded: it is the one stream that keeps flowing even
    #: when every sensor has died, so counting it here would hide exactly
    #: the failure this check exists to catch.
    _PAYLOAD_STREAMS = ("tof_A", "tof_B", "mic")

    def _no_payload_flowing(self, stale_stream_names) -> bool:
        """True when none of tof_A/tof_B/mic are currently delivering data.

        `bandwidth` counts every byte on the wire, `$H` included -- a link
        that is nothing but heartbeats still reads as "some bandwidth in
        use" and can land comfortably inside the green threshold. This is
        what tells that apart from genuine payload traffic, reusing
        `stale_streams()`'s judgment of "silent" rather than inventing a
        second one that could disagree with it.
        """
        with self._lock:
            last_seen = dict(self._last_seen)
        for stream in self._PAYLOAD_STREAMS:
            if stream in stale_stream_names:
                continue                       # confirmed stale
            if last_seen.get(stream) is None:
                continue                       # never seen at all
            return False                       # at least one is alive
        return True

    def observe_tof(self, event: dict, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        sensor = event.get("sensor")
        if sensor in ("A", "B"):
            self.mark_seen(f"tof_{sensor}", now)
        dim = event.get("dim")
        valid = event.get("valid")
        distance = event.get("distance")
        if not dim or valid is None or distance is None:
            return
        live = [d for d, ok in zip(distance, valid) if ok and d is not None]
        with self._lock:
            self._zones.append((now, sum(1 for v in valid if v), dim))
            sensor = event.get("sensor")
            if live and sensor in self._sensor_mean:
                self._sensor_mean[sensor].append((now, sum(live) / len(live)))
            self._prune(now)
        self._note_clock(event, now)

    def observe_mic(self, event: dict, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self.mark_seen("mic", now)
        rms = event.get("rms")
        if rms is not None:
            with self._lock:
                self._rms.append((now, rms))
                self._prune(now)
        self._note_clock(event, now)

    def observe_mel(self, event: dict, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self.mark_seen("mel", now)
        self._note_clock(event, now)

    def observe_heartbeat(self, event: dict, now: float | None = None) -> None:
        """Record the device's drop counters, paired with the host's own.

        Both sides are sampled *here*, at the moment the $H is processed,
        because comparing two cumulative counters read at different times is
        meaningless. The reader delivers lines in order, so every frame the
        device counted into this $H has already been observed by the tracker
        -- and, crucially, none that came after it has.

        Comparing the tracker's live total against the last $H instead
        charges the host with every device-side drop of the past second (the
        heartbeat interval), which reads as a transport fault on any link
        that is legitimately dropping frames. B20 caught that: with 5%
        injected drops the alarm fired with delta=1 on a perfectly healthy
        transport.
        """
        self.mark_seen("heartbeat", self._clock() if now is None else now)
        drops = {}
        for stream, key in (("tof_A", "drop_A"), ("tof_B", "drop_B"), ("mic", "drop_M")):
            value = event.get(key)
            if value is not None:
                drops[stream] = value
        if drops:
            host = ({s: self.drop_tracker.stats(s).missing for s in drops}
                    if self.drop_tracker else {})
            with self._lock:
                self._device_drops = drops
                self._host_drops_at_heartbeat = host
        self._note_clock(event, self._clock() if now is None else now)

    def _note_clock(self, event: dict, now: float) -> None:
        """Feed one (device, host) timestamp pair to the alignment fit."""
        if self.clock_aligner is None:
            return
        t_us = event.get("t_us")
        if t_us is None or not event.get("has_timestamp"):
            return
        self.clock_aligner.add_sample(t_us, int(self._host_clock() * 1e6))

    # -- metrics --------------------------------------------------------

    def _valid_zone_ratio(self):
        total = sum(dim for _, _, dim in self._zones)
        if not total:
            return None
        return sum(n for _, n, _ in self._zones) / total

    def _symmetry(self):
        """Relative left/right difference in mean valid distance.

        Relative, not absolute millimetres: the same 5 mm difference means
        something quite different at 30 mm than at 300 mm, and the number
        the wearer is trying to tune out is "one side sits further away
        than the other", which is a ratio.
        """
        means = {}
        for sensor, window in self._sensor_mean.items():
            if window:
                means[sensor] = sum(v for _, v in window) / len(window)
        if len(means) < 2:
            return None
        a, b = means["A"], means["B"]
        mid = (a + b) / 2
        return abs(a - b) / mid if mid else None

    def _noise_floor(self):
        return _percentile([v for _, v in self._rms], NOISE_FLOOR_PERCENTILE)

    def _mic_all_zero(self):
        """True when every rms sample in the current window is exactly 0.

        Not "low" -- a hard, sustained 0. A real bone-conduction mic against
        skin reads single digits at rest (measured on hardware: RMS 4-6,
        confirmed normal by firmware review -- that is ~0.015% of full
        scale, and the firmware applies no scaling that could zero it out),
        so a threshold anywhere above 0 would misclassify that as dead. Only
        "never anything but 0" is a value no connected microphone produces.
        Same criterion ssi-backlog/tools/first_session_check.py already uses
        for a recorded session (`n_zero == all_rms.size`) -- one judgment,
        not two that could disagree.
        """
        return bool(self._rms) and all(v == 0 for _, v in self._rms)

    def _bandwidth(self, now):
        if not self._bytes:
            return None
        span = now - self._bytes[0][0]
        if span <= 0:
            return None
        return sum(n for _, n in self._bytes) / span / self.capacity_bytes_per_s

    def _clock_resid_s(self):
        """p95 alignment residual, in seconds (B04's bound is 5 ms)."""
        aligner = self.clock_aligner
        if aligner is None or aligner.n_buckets < MIN_CLOCK_BUCKETS:
            return None
        try:
            return aligner.fit().residual_p95_us / 1e6
        except Exception:
            # A fit that cannot converge is not a reason to stop reporting
            # every other metric; the None reads as "unknown" downstream.
            return None

    def _transport_alarms(self):
        """Streams where the host counted MORE drops than the device did.

        This is not measurement error, and it is not symmetric. The host
        infers drops from gaps between received frames, so it can only ever
        lag the device's own count (B03). A host count that runs *ahead*
        means frames the device believes it sent never arrived -- the loss
        happened in the transport, between the two counters, and neither
        counter is wrong. It is the one condition this cross-check exists to
        detect, so it raises an alarm rather than being folded into a
        tolerance band.
        """
        if self.drop_tracker is None or self._device_drops is None:
            return []
        stats = {}
        if self._parser_stats is not None:
            try:
                stats = self._parser_stats() or {}
            except Exception:
                stats = {}
        malformed = stats.get("malformed") or 0

        alarms = []
        for stream, device in self._device_drops.items():
            host = self._host_drops_at_heartbeat.get(stream)
            if host is None:
                continue
            delta = host - device
            if delta > 0:
                alarms.append({
                    "metric": "drop_rate",
                    "stream": stream,
                    "host": host,
                    "device": device,
                    "delta": delta,
                    "malformed": malformed,
                    "message": _transport_alarm_message(stream, host, device, malformed),
                })
        return alarms

    def parser_stats(self):
        if self._parser_stats is None:
            return {}
        try:
            return self._parser_stats() or {}
        except Exception:
            return {}

    # -- rendering ------------------------------------------------------

    def snapshot(self, now: float | None = None) -> dict:
        """Render the ``quality`` SSE event (CONTRACTS.md #4.2)."""
        self.thresholds.reload()
        now = self._clock() if now is None else now

        with self._lock:
            self._prune(now)
            values = {
                "valid_zones": self._valid_zone_ratio(),
                "symmetry": self._symmetry(),
                "noise_floor": self._noise_floor(),
                "bandwidth": self._bandwidth(now),
            }
            mic_dead = self._mic_all_zero()
            alarms = self._transport_alarms()

        # Outside the lock: these reach into collaborators that take their
        # own locks, and holding both would be a lock-ordering hazard.
        values["drop_rate"] = (
            self.drop_tracker.overall_drop_rate() if self.drop_tracker else None
        )
        values["clock_resid"] = self._clock_resid_s()

        metrics = {}
        for name in METRIC_ORDER:
            value = values.get(name)
            level, hint = self.thresholds.classify(name, value)
            entry = {"value": value, "level": level}
            if hint:
                entry["hint"] = hint
            metrics[name] = entry

        for alarm in alarms:
            entry = metrics.get(alarm["metric"])
            if entry is not None:
                entry["level"] = "red"
                entry["hint"] = alarm["message"]

        # A sustained, exact 0 is not "quiet" -- see _mic_all_zero(). Overrides
        # whatever the plain threshold table said, the same way alarms above do.
        if mic_dead:
            entry = metrics.get("noise_floor")
            if entry is not None:
                entry["level"] = "red"
                entry["hint"] = _DEAD_MIC_HINT

        event = {"type": "quality", "t": time.time(), "metrics": metrics}

        stale = self.stale_streams(now)
        if stale:
            event["stale_streams"] = stale
            alarms.extend({
                "metric": "drop_rate",
                "kind": "stale",
                "stream": item["stream"],
                "silent_for_s": item["silent_for_s"],
                "message": _stale_stream_message(item),
            } for item in stale)

        # bandwidth counts every byte on the wire, heartbeats included -- a
        # link with no payload streams alive can still show a "healthy"
        # low-but-nonzero number. Demote it to unknown rather than trust a
        # figure that is not measuring what the dashboard implies it is.
        stale_names = {item["stream"] for item in stale}
        if self._no_payload_flowing(stale_names):
            entry = metrics.get("bandwidth")
            if entry is not None and entry["level"] != "unknown":
                entry["level"] = "unknown"
                entry.pop("hint", None)

        if self.sensors_seen is not None:
            try:
                seen = self.sensors_seen()
            except Exception:
                seen = None
            if seen is not None:
                event["sensors_seen"] = seen

        stats = self.parser_stats()
        if stats:
            # Surfaced because a rejected line is indistinguishable from a
            # frame the device never sent, as far as the seq gap is
            # concerned -- so drop_rate silently absorbs both.
            event["malformed"] = stats.get("malformed", 0)
            event["malformed_rate"] = stats.get("malformed_rate", 0.0)
        if alarms:
            event["alarms"] = alarms
        if self.thresholds.load_error:
            event["threshold_error"] = self.thresholds.load_error
        return event

    # -- internals ------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        for window in (self._zones, self._rms, self._bytes,
                       self._sensor_mean["A"], self._sensor_mean["B"]):
            while window and window[0][0] < cutoff:
                window.popleft()
