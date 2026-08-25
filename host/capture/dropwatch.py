"""B03 -- host-side drop detection from ``seq`` gaps.

Every protocol-v2 data line carries a per-stream ``seq`` that the device
increments once per *scheduled* frame, whether or not the frame actually
made it onto the wire (CONTRACTS.md #1.1). A gap in ``seq`` is therefore a
frame the host never received, and counting those gaps gives the host an
estimate of the drop rate that is computed completely independently of the
device's own ``drop_*`` counters in ``$H``.

The point is the disagreement. Both sides are counting the same physical
event, so if the two numbers diverge the fault is in neither counter -- it
is in the transport between them (UART overrun, a bridge that cannot keep
up, a parser dropping lines). That cross-check is the whole reason this
module exists, and :meth:`DropTracker.cross_check` implements it.

Deliberately standalone: it takes ``(stream, seq)`` and nothing else, so it
does not depend on B01's line parser (``host/capture/protocol.py``) while
that interface is still moving. B19 wires the two together.

Counter semantics match the device side exactly (CONTRACTS.md #1.1, as
amended 2026-08-26): ``missing`` is cumulative **since boot** and is NOT
reset by a ``$STATUS`` line. ``$STATUS`` is re-sent on every ``PING`` and
every ``SENS``/``MEL`` change, so resetting on it would zero the counters
roughly a hundred times during B05's clock calibration and leave the health
metric reading zero exactly when it matters. A reboot -- a real new session
-- does reset both sides, and is detected from ``seq`` restarting, not from
``$STATUS`` arriving.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

_U32 = 1 << 32

#: The four streams the device multiplexes onto one UART. ``mel`` ($F) is
#: not emitted yet (A11-A14); it is listed so the tracker reports it as a
#: known-but-silent stream rather than inventing it on first sight.
DEFAULT_STREAMS = ("tof_A", "tof_B", "mic", "mel")

#: A forward gap at least this large is not believable as real frame loss.
#: At 30 Hz, 1000 frames is 33 seconds of continuous silence -- by then the
#: link is down, not lossy. Without this guard a single corrupted ``seq``
#: would add millions to the count and make the whole gauge useless.
DEFAULT_GAP_LIMIT = 1000

#: How many consecutive implausible gaps before the tracker gives up and
#: re-baselines anyway. This is the recovery path for a reboot whose
#: ``$STATUS`` line was itself lost: without it the stream would stay
#: desynced forever, reporting an anomaly on every single frame.
DEFAULT_RESYNC_AFTER = 3

#: Sliding window, in seconds, for the *rate* (as opposed to the cumulative
#: totals). Matches the story's 30 s.
DEFAULT_WINDOW_S = 30.0


def tof_stream(sensor: str) -> str:
    """Stream name for a ``$T`` line's ``<A|B>`` field."""
    return f"tof_{sensor}"


@dataclass
class StreamStats:
    """Cumulative, since-boot counters for one stream."""

    received: int = 0
    """Frames actually parsed off the wire."""

    missing: int = 0
    """Frames the device scheduled but the host never saw, from seq gaps."""

    resyncs: int = 0
    """Times this stream was re-baselined: device reboot, or recovery from
    a run of implausible gaps. Not frame loss -- a jump in the count means
    the device restarted, which is a different problem entirely."""

    anomalies: int = 0
    """Implausible ``seq`` values rejected by the gap guard. Almost always a
    parse or transport fault rather than loss, so they are counted apart
    from ``missing`` instead of being silently swallowed."""

    last_seq: int | None = None
    """Most recent accepted ``seq``; ``None`` before the first frame."""

    @property
    def expected(self) -> int:
        """Frames the device scheduled: what arrived plus what didn't."""
        return self.received + self.missing

    @property
    def drop_rate(self) -> float | None:
        """Since-boot drop rate, or ``None`` before any frame arrived."""
        return self.missing / self.expected if self.expected else None


@dataclass
class _StreamState:
    stats: StreamStats = field(default_factory=StreamStats)
    window: deque = field(default_factory=deque)
    consecutive_anomalies: int = 0
    status_armed: bool = False


class DropTracker:
    """Counts missing frames per stream from ``seq`` gaps.

    Not safe against a torn read from another thread unless you go through
    the public methods -- those take an internal lock, because the bridge
    reads the serial port on one thread and serves the panel from another.
    """

    def __init__(
        self,
        window_s: float = DEFAULT_WINDOW_S,
        gap_limit: int = DEFAULT_GAP_LIMIT,
        resync_after: int = DEFAULT_RESYNC_AFTER,
        streams: tuple[str, ...] = DEFAULT_STREAMS,
        clock=time.monotonic,
    ):
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        if gap_limit < 1:
            raise ValueError("gap_limit must be at least 1")
        if resync_after < 1:
            raise ValueError("resync_after must be at least 1")
        self.window_s = window_s
        self.gap_limit = gap_limit
        self.resync_after = resync_after
        self._clock = clock
        self._lock = threading.Lock()
        self._streams: dict[str, _StreamState] = {s: _StreamState() for s in streams}

    # -- observation ----------------------------------------------------

    def observe(self, stream: str, seq: int, now: float | None = None) -> int:
        """Record one received frame. Returns how many frames it proved missing.

        ``seq`` must be the raw u32 from the wire. A stream not seen before
        is created on the fly, so a device that grows a new stream does not
        need this module changed.
        """
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise TypeError(f"seq must be an int, got {type(seq).__name__}")
        if not 0 <= seq < _U32:
            raise ValueError(f"seq {seq} outside u32 range")

        with self._lock:
            st = self._streams.setdefault(stream, _StreamState())
            now = self._clock() if now is None else now
            prev = st.stats.last_seq
            armed, st.status_armed = st.status_armed, False

            if prev is None:
                # First frame on this stream. If a $STATUS told us we were
                # watching from the session's start, seq 0 is the first
                # frame the device ever scheduled, so a first seq of k means
                # k frames were already lost before we saw anything. Without
                # that assurance we may have attached mid-session, and
                # anything before now is simply not ours to count.
                missing = seq if (armed and seq < self.gap_limit) else 0
                st.stats.missing += missing
                st.stats.received += 1
                st.stats.last_seq = seq
                st.consecutive_anomalies = 0
                self._push(st, now, missing, 1)
                return missing

            if armed and seq < prev:
                # $STATUS, then seq restarts: the device rebooted. New
                # session -- seq is session-scoped (CONTRACTS.md #1.3), so
                # the counters restart with it and the backwards jump is
                # not frame loss.
                return self._restart(st, seq, now)

            gap = (seq - prev - 1) % _U32

            if gap < self.gap_limit:
                # The ordinary path, and the u32 wrap path: modular
                # arithmetic makes 0xFFFFFFFF -> 0 a gap of 0, not of four
                # billion. (Per CONTRACTS.md #1.3 the wrap is not otherwise
                # handled -- at 30 Hz it is ~4.5 years away.)
                st.stats.missing += gap
                st.stats.received += 1
                st.stats.last_seq = seq
                st.consecutive_anomalies = 0
                self._push(st, now, gap, 1)
                return gap

            # Implausible gap: corrupted seq, or a reboot whose $STATUS we
            # never saw.
            st.consecutive_anomalies += 1
            if st.consecutive_anomalies >= self.resync_after:
                return self._restart(st, seq, now)

            # One bad line must not desync the stream, so last_seq is left
            # alone: if the next frame continues the real sequence, the gap
            # against it still works out. Not counted as received either --
            # we do not actually know what frame this was.
            st.stats.anomalies += 1
            return 0

    def on_status(self, now: float | None = None) -> None:
        """Record a ``$STATUS`` line.

        This does NOT reset anything by itself. ``$STATUS`` is re-sent on
        every ``PING`` and every ``SENS``/``MEL`` change, so treating it as
        a reset would zero the counters constantly during normal operation.
        It only arms the next frame on each stream to be read as a possible
        session start -- and that reading is confirmed by ``seq`` actually
        restarting, not by the ``$STATUS`` alone.
        """
        with self._lock:
            for st in self._streams.values():
                st.status_armed = True

    # -- reads ----------------------------------------------------------

    def stats(self, stream: str) -> StreamStats:
        """Cumulative counters for one stream (a copy; safe to hold onto)."""
        with self._lock:
            st = self._streams.get(stream)
            return StreamStats(**vars(st.stats)) if st else StreamStats()

    def drop_rate(self, stream: str, now: float | None = None) -> float | None:
        """Drop rate over the sliding window, or ``None`` if the window is empty.

        Windowed rather than cumulative so the panel shows what the link is
        doing *now*: a bad thirty seconds during setup should not keep the
        gauge red for the rest of an hour-long session.
        """
        with self._lock:
            st = self._streams.get(stream)
            if st is None:
                return None
            now = self._clock() if now is None else now
            self._prune(st, now)
            missing = sum(m for _, m, _ in st.window)
            received = sum(r for _, _, r in st.window)
            total = missing + received
            return missing / total if total else None

    def overall_drop_rate(self, now: float | None = None) -> float | None:
        """Drop rate over the sliding window across every stream at once."""
        with self._lock:
            now = self._clock() if now is None else now
            missing = received = 0
            for st in self._streams.values():
                self._prune(st, now)
                missing += sum(m for _, m, _ in st.window)
                received += sum(r for _, _, r in st.window)
            total = missing + received
            return missing / total if total else None

    def cross_check(self, device_drops: dict[str, int]) -> dict[str, dict]:
        """Compare host-derived counts against the device's ``$H`` ``drop_*``.

        ``device_drops`` maps stream name to the cumulative counter from the
        most recent ``$H`` -- e.g. ``{"tof_A": drop_A, "tof_B": drop_B,
        "mic": drop_M}``. Both sides count since boot and neither is reset
        by ``$STATUS``, so on a healthy link they should agree exactly; the
        only expected difference is frames still in flight when ``$H`` was
        stamped.

        Returns, per stream: ``host``, ``device``, ``delta``
        (host - device), and ``rate_delta`` -- the difference expressed in
        drop-rate terms, which is the number the acceptance threshold is
        written against.
        """
        out: dict[str, dict] = {}
        for stream, device in device_drops.items():
            s = self.stats(stream)
            expected = s.expected
            out[stream] = {
                "host": s.missing,
                "device": device,
                "delta": s.missing - device,
                "rate_delta": (s.missing - device) / expected if expected else None,
            }
        return out

    def snapshot(self, now: float | None = None) -> dict:
        """Everything a panel or a log line needs, in one consistent read."""
        now = self._clock() if now is None else now
        out = {}
        for stream in list(self._streams):
            s = self.stats(stream)
            out[stream] = {
                "received": s.received,
                "missing": s.missing,
                "resyncs": s.resyncs,
                "anomalies": s.anomalies,
                "last_seq": s.last_seq,
                "drop_rate_total": s.drop_rate,
                "drop_rate_window": self.drop_rate(stream, now=now),
            }
        return out

    def reset(self) -> None:
        """Drop all state, keeping the configured stream names."""
        with self._lock:
            for name in self._streams:
                self._streams[name] = _StreamState()

    # -- internals ------------------------------------------------------

    def _restart(self, st: _StreamState, seq: int, now: float) -> int:
        """Re-baseline a stream onto a new session starting at ``seq``."""
        resyncs = st.stats.resyncs + 1
        st.stats = StreamStats(resyncs=resyncs)
        st.window.clear()
        st.consecutive_anomalies = 0
        # A real new session starts at seq 0, so a first seq of k means k
        # frames of this session were already lost. Only when k is small
        # enough to be believable, though: this path is also reached by the
        # anomaly-recovery route, where the "restart" may really be a
        # desync at a perfectly normal large seq, and charging that seq as
        # missing would invent millions of drops.
        missing = seq if seq < self.gap_limit else 0
        st.stats.missing = missing
        st.stats.received = 1
        st.stats.last_seq = seq
        self._push(st, now, missing, 1)
        return missing

    def _push(self, st: _StreamState, now: float, missing: int, received: int) -> None:
        st.window.append((now, missing, received))
        self._prune(st, now)

    def _prune(self, st: _StreamState, now: float) -> None:
        cutoff = now - self.window_s
        while st.window and st.window[0][0] < cutoff:
            st.window.popleft()
