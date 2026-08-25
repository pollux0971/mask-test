"""B03 tests.

Two halves. The unit tests pin down the gap arithmetic and the three cases
that look alike on the wire -- normal increment, real loss, and a device
reboot. The integration test runs the real T04 mock device over a pty and
checks the host's independently-derived count against the device's own
``drop_*`` in ``$H``, which is the cross-validation the story is actually
about.
"""

from __future__ import annotations

import os
import pty  # noqa: F401  (imported for parity with mock_device's requirements)
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from host.capture.dropwatch import DropTracker, tof_stream

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_DEVICE = REPO_ROOT / "ssi-backlog" / "tools" / "mock_device.py"


class FakeClock:
    """Manual clock, so window tests do not depend on wall time."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


# -- gap arithmetic -----------------------------------------------------


def test_contiguous_seq_reports_no_loss():
    t = DropTracker()
    for seq in range(10):
        assert t.observe("tof_A", seq) == 0
    s = t.stats("tof_A")
    assert (s.received, s.missing, s.drop_rate) == (10, 0, 0.0)


def test_gap_counts_exactly_the_frames_between():
    t = DropTracker()
    t.observe("tof_A", 0)
    assert t.observe("tof_A", 4) == 3  # 1, 2, 3 never arrived
    s = t.stats("tof_A")
    assert (s.received, s.missing) == (2, 3)
    assert s.drop_rate == pytest.approx(3 / 5)


def test_streams_are_counted_separately():
    t = DropTracker()
    t.observe(tof_stream("A"), 0)
    t.observe(tof_stream("B"), 0)
    t.observe(tof_stream("A"), 5)  # 4 lost on A
    t.observe(tof_stream("B"), 1)  # B is clean
    assert t.stats("tof_A").missing == 4
    assert t.stats("tof_B").missing == 0


def test_u32_wraparound_is_not_four_billion_drops():
    """The one case where naive subtraction is catastrophically wrong."""
    t = DropTracker()
    t.observe("mic", 2**32 - 2)
    assert t.observe("mic", 2**32 - 1) == 0
    assert t.observe("mic", 0) == 0  # wrapped, contiguous
    assert t.observe("mic", 3) == 2  # wrapped, and 1-2 lost
    assert t.stats("mic").missing == 2


def test_seq_outside_u32_is_rejected():
    t = DropTracker()
    with pytest.raises(ValueError):
        t.observe("mic", -1)
    with pytest.raises(ValueError):
        t.observe("mic", 2**32)
    with pytest.raises(TypeError):
        t.observe("mic", 1.0)


# -- the gap guard ------------------------------------------------------


def test_implausible_gap_is_an_anomaly_not_a_million_drops():
    t = DropTracker(gap_limit=1000)
    t.observe("tof_A", 10)
    assert t.observe("tof_A", 9_000_000) == 0
    s = t.stats("tof_A")
    assert s.anomalies == 1
    assert s.missing == 0


def test_one_corrupt_seq_does_not_desync_the_stream():
    """After garbage, the next real frame must still line up against 10."""
    t = DropTracker(gap_limit=1000)
    t.observe("tof_A", 10)
    t.observe("tof_A", 9_000_000)  # garbage, ignored
    assert t.observe("tof_A", 11) == 0  # continues cleanly
    s = t.stats("tof_A")
    assert (s.missing, s.anomalies, s.received) == (0, 1, 2)


def test_sustained_implausible_gaps_eventually_re_baseline():
    """Recovery path for a reboot whose $STATUS was itself lost."""
    t = DropTracker(gap_limit=1000, resync_after=3)
    t.observe("tof_A", 500_000)
    t.observe("tof_A", 0)
    t.observe("tof_A", 1)
    t.observe("tof_A", 2)  # third strike -> re-baseline here
    assert t.stats("tof_A").resyncs == 1
    assert t.observe("tof_A", 3) == 0
    assert t.stats("tof_A").last_seq == 3


def test_resync_at_a_large_seq_does_not_invent_drops():
    """Anomaly recovery at a normal mid-session seq must not charge it as loss."""
    t = DropTracker(gap_limit=1000, resync_after=3)
    t.observe("mic", 10)
    for seq in (500_000, 500_001, 500_002):
        t.observe("mic", seq)
    s = t.stats("mic")
    assert s.resyncs == 1
    assert s.missing == 0  # not 500_002


# -- $STATUS: reboot vs PING -------------------------------------------


def test_status_alone_does_not_reset_anything():
    """$STATUS arrives on every PING; resetting on it would zero the gauge.

    This is the host-side mirror of the CONTRACTS.md #1.1 amendment of
    2026-08-26 -- drop_* counts since boot, not since the last $STATUS.
    """
    t = DropTracker()
    t.observe("tof_A", 0)
    t.observe("tof_A", 4)  # 3 lost
    t.on_status()  # e.g. the reply to a PING
    t.observe("tof_A", 5)  # session continues, seq goes forward
    s = t.stats("tof_A")
    assert s.missing == 3
    assert s.resyncs == 0


def test_hundred_pings_do_not_erode_the_count():
    """B05 sends 100 PINGs; each one answers with a $STATUS."""
    t = DropTracker()
    t.observe("tof_A", 0)
    t.observe("tof_A", 11)  # 10 lost
    for i in range(100):
        t.on_status()
        t.observe("tof_A", 12 + i)
    s = t.stats("tof_A")
    assert s.missing == 10
    assert s.resyncs == 0


def test_reboot_resets_and_invents_no_drops():
    """$STATUS followed by seq restarting: a real new session."""
    t = DropTracker()
    for seq in range(0, 900):
        t.observe("tof_A", seq)
    t.on_status()
    assert t.observe("tof_A", 0) == 0  # not 4 billion, not 900
    s = t.stats("tof_A")
    assert (s.missing, s.received, s.last_seq, s.resyncs) == (0, 1, 0, 1)


def test_reboot_counts_frames_lost_before_the_first_one_seen():
    t = DropTracker()
    t.observe("tof_A", 500)
    t.on_status()
    assert t.observe("tof_A", 3) == 3  # new session dropped 0,1,2
    assert t.stats("tof_A").missing == 3


def test_status_at_session_start_counts_the_pre_roll():
    """Attached from boot: a first seq of 3 means 3 frames already lost."""
    t = DropTracker()
    t.on_status()
    assert t.observe("mic", 3) == 3


def test_status_mid_session_does_not_charge_a_large_first_seq():
    """Attached mid-session, then a PING: seq 50000 is not 50000 drops."""
    t = DropTracker()
    t.on_status()
    assert t.observe("mic", 50_000) == 0
    assert t.stats("mic").missing == 0


def test_arming_is_consumed_by_the_next_frame():
    """A $STATUS from ten minutes ago must not explain away later garbage."""
    t = DropTracker()
    t.observe("tof_A", 100)
    t.on_status()
    t.observe("tof_A", 101)  # consumes the arm
    t.observe("tof_A", 5)  # backwards, but unarmed -> anomaly, not reboot
    s = t.stats("tof_A")
    assert s.resyncs == 0
    assert s.anomalies == 1


# -- sliding window -----------------------------------------------------


def test_window_forgets_old_losses():
    clock = FakeClock()
    t = DropTracker(window_s=30.0, clock=clock)
    t.observe("tof_A", 0)
    t.observe("tof_A", 10)  # 9 lost, at t=0
    assert t.drop_rate("tof_A") == pytest.approx(9 / 11)

    clock.advance(31.0)
    for i in range(11, 31):
        t.observe("tof_A", i)
    assert t.drop_rate("tof_A") == 0.0  # the bad burst has aged out
    assert t.stats("tof_A").missing == 9  # ...but the total remembers it


def test_window_rate_is_none_before_any_frame():
    t = DropTracker()
    assert t.drop_rate("tof_A") is None
    assert t.overall_drop_rate() is None


def test_overall_rate_pools_streams():
    clock = FakeClock()
    t = DropTracker(clock=clock)
    t.observe("tof_A", 0)
    t.observe("tof_A", 2)  # 1 lost, 2 received
    t.observe("tof_B", 0)
    t.observe("tof_B", 1)  # 0 lost, 2 received
    assert t.overall_drop_rate() == pytest.approx(1 / 5)


def test_reset_clears_everything():
    t = DropTracker()
    t.observe("tof_A", 0)
    t.observe("tof_A", 9)
    t.reset()
    s = t.stats("tof_A")
    assert (s.missing, s.received, s.last_seq) == (0, 0, None)


def test_snapshot_shape():
    t = DropTracker()
    t.observe("tof_A", 0)
    snap = t.snapshot()
    assert set(snap) >= {"tof_A", "tof_B", "mic", "mel"}
    assert set(snap["tof_A"]) == {
        "received", "missing", "resyncs", "anomalies",
        "last_seq", "drop_rate_total", "drop_rate_window",
    }
    assert snap["mel"]["last_seq"] is None  # known, but never emitted yet


# -- cross-check against the device's own counters ----------------------


def test_cross_check_reports_agreement():
    t = DropTracker()
    t.observe("tof_A", 0)
    t.observe("tof_A", 6)  # host says 5 lost
    result = t.cross_check({"tof_A": 5})
    assert result["tof_A"]["delta"] == 0
    assert result["tof_A"]["rate_delta"] == pytest.approx(0.0)


def test_cross_check_exposes_disagreement():
    """The interesting case: the two counters disagree, so the transport lied."""
    t = DropTracker()
    for seq in range(100):
        t.observe("tof_A", seq)  # host saw everything
    result = t.cross_check({"tof_A": 10})  # device says it dropped 10
    assert result["tof_A"]["delta"] == -10
    assert result["tof_A"]["rate_delta"] == pytest.approx(-0.1)


# -- end to end against the real T04 mock device -------------------------


def _run_mock(args, seconds):
    """Run mock_device.py over a pty, return (lines, stderr_text)."""
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_DEVICE), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        header = proc.stdout.readline()
        m = re.search(r"pty ready:\s*(\S+)", header)
        assert m, f"unexpected mock_device header: {header!r}"
        fd = os.open(m.group(1), os.O_RDONLY | os.O_NONBLOCK)
        try:
            buf, lines = "", []
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    time.sleep(0.005)
                    continue
                if not chunk:
                    break
                buf += chunk.decode("ascii", errors="replace")
                *ready, buf = buf.split("\n")
                lines.extend(ready)
        finally:
            os.close(fd)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return lines, proc.stderr.read()


def _feed(tracker, lines):
    """Minimal $T/$M/$H/$STATUS field-splitter.

    Deliberately not B01's parser: B03 only needs (stream, seq), and
    depending on protocol.py while its interface is still moving would
    couple two stories that do not otherwise need each other.
    """
    device_drops = None
    for line in lines:
        parts = line.split(",")
        kind = parts[0]
        try:
            if kind == "$T":
                tracker.observe(tof_stream(parts[1]), int(parts[2]))
            elif kind == "$M":
                tracker.observe("mic", int(parts[1]))
            elif kind == "$F":
                tracker.observe("mel", int(parts[1]))
            elif kind == "$H":
                device_drops = {
                    "tof_A": int(parts[2]),
                    "tof_B": int(parts[3]),
                    "mic": int(parts[4]),
                }
            elif kind == "$STATUS":
                tracker.on_status()
        except (IndexError, ValueError):
            continue  # partial or malformed line; B01 owns strict parsing
    return device_drops


@pytest.mark.skipif(not MOCK_DEVICE.exists(), reason="T04 mock_device.py not present")
def test_end_to_end_measured_rate_matches_requested_rate():
    """Acceptance: --drop-rate 0.05 must read back as 4-6%."""
    lines, _ = _run_mock(
        ["--fps", "60", "--mic-fps", "60", "--drop-rate", "0.05", "--seed", "1234"],
        seconds=12.0,
    )
    tracker = DropTracker(window_s=3600.0)
    _feed(tracker, lines)

    for stream in ("tof_A", "tof_B", "mic"):
        s = tracker.stats(stream)
        assert s.expected > 300, f"{stream}: only {s.expected} frames, sample too small"
        assert 0.04 <= s.drop_rate <= 0.06, f"{stream}: {s.drop_rate:.4f} outside 4-6%"
        assert s.resyncs == 0 and s.anomalies == 0


@pytest.mark.skipif(not MOCK_DEVICE.exists(), reason="T04 mock_device.py not present")
def test_end_to_end_agrees_with_device_reported_drops():
    """Acceptance: host-derived rate within 0.5 pp of the firmware's $H."""
    lines, _ = _run_mock(
        ["--fps", "60", "--mic-fps", "60", "--drop-rate", "0.05", "--seed", "99"],
        seconds=12.0,
    )
    tracker = DropTracker(window_s=3600.0)
    device_drops = _feed(tracker, lines)
    assert device_drops is not None, "no $H line seen"

    for stream, cmp in tracker.cross_check(device_drops).items():
        # The device's $H is stamped before the frames that followed it, so
        # the host legitimately sees a few more drops than the last $H
        # reported. That is a small positive delta, never a negative one:
        # the host cannot know about a drop the device has not counted.
        assert cmp["delta"] >= 0, f"{stream}: host {cmp['host']} < device {cmp['device']}"
        assert abs(cmp["rate_delta"]) < 0.005, (
            f"{stream}: {cmp['rate_delta']:.4f} exceeds 0.5 pp "
            f"(host {cmp['host']} vs device {cmp['device']})"
        )


@pytest.mark.skipif(not MOCK_DEVICE.exists(), reason="T04 mock_device.py not present")
def test_end_to_end_clean_link_reports_zero():
    """No drop injection: the tracker must not manufacture losses."""
    lines, _ = _run_mock(["--fps", "60", "--mic-fps", "60", "--seed", "7"], seconds=6.0)
    tracker = DropTracker(window_s=3600.0)
    _feed(tracker, lines)
    for stream in ("tof_A", "tof_B", "mic"):
        s = tracker.stats(stream)
        assert s.expected > 100
        assert (s.missing, s.resyncs, s.anomalies) == (0, 0, 0)
