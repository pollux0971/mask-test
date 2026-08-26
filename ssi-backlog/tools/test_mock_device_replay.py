"""Tests for T05 -- mock_device.py's --replay-log playback.

Split from test_mock_device.py (owned by B02/B03/B05/B06's fixture work,
not to be edited here) because T05 is a self-contained mechanism: pure
parsing/scheduling functions plus one CLI wiring check. `MockLink` is
imported, not copied, from test_mock_device.py -- it is the same
"launch mock_device.py, talk to its pty" harness every other end-to-end
test in this file's sibling already uses.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mock_device import MockLink  # noqa: E402

from mock_device import extract_t_us, is_protocol_line, replay_log  # noqa: E402


# ---------------------------------------------------------------------------
# is_protocol_line / extract_t_us: pure functions, no pty involved.

@pytest.mark.parametrize("line", [
    "$T,A,12,1000000,4,17,17,17,17,17,17,17,17,17,17,17,17,17,17,17,17,140,140,140,140,140,140,140,140,140,140,140,140,140,140,140,140",
    "$M,3,1000500,300,480",
    "$F,7,1000700,-500,-510",
    "$H,1001000,0,0,0,150000,38,64",
    "$STATUS,res=4,proto=2,fw=abc1234",
    "$A,A,0,1002000,4,20,20,20,20",
    "BEGIN_WAV_B64 rate=16000 bits=16 channels=1 bytes=32000",
    "END_WAV_B64",
])
def test_is_protocol_line_accepts_every_real_wire_line(line):
    assert is_protocol_line(line)


@pytest.mark.parametrize("line", [
    "",
    "ets Jul 29 2019 12:21:46",
    "I (306) cpu_start: Pro cpu up.",
    "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
    "\x1b[0;32mI (312) wifi:mode : sta\x1b[0m",
])
def test_is_protocol_line_rejects_esp_log_noise(line):
    assert not is_protocol_line(line)


def test_extract_t_us_reads_the_right_field_per_line_type():
    assert extract_t_us("$T,A,12,1000000,4,17,17,17,17,140,140,140,140") == 1000000
    assert extract_t_us("$A,B,0,1002000,4,20,20,20,20") == 1002000
    assert extract_t_us("$M,3,1000500,300,480") == 1000500
    assert extract_t_us("$F,7,1000700,-500,-510") == 1000700
    assert extract_t_us("$H,1001000,0,0,0,150000,38,64") == 1001000


def test_extract_t_us_is_none_for_untimed_or_malformed_lines():
    assert extract_t_us("$STATUS,res=4,proto=2") is None
    assert extract_t_us("BEGIN_WAV_B64 rate=16000") is None
    assert extract_t_us("$M,3") is None  # too few fields for its own index
    assert extract_t_us("$H,not_a_number") is None


# ---------------------------------------------------------------------------
# replay_log: fake clock/sleep, no real waiting -- verifies the scheduling
# arithmetic itself (anchored targets, not accumulated sleeps).

def test_replay_log_skips_noise_and_keeps_protocol_lines_in_order(tmp_path):
    log = tmp_path / "capture.log"
    log.write_text(
        "ets Jul 29 2019 12:21:46\n"
        "$STATUS,res=4,proto=2\n"
        "I (306) cpu_start: noise\n"
        "$T,A,0,1000000,4,17,17,17,17,140,140,140,140\n"
        "$H,1001000,0,0,0,150000,38,64\n"
    )
    written = []
    replay_log(log, written.append, sleep_fn=lambda s: None, clock_fn=lambda: 0.0)

    assert written == [
        "$STATUS,res=4,proto=2",
        "$T,A,0,1000000,4,17,17,17,17,140,140,140,140",
        "$H,1001000,0,0,0,150000,38,64",
    ]


def test_replay_log_schedules_from_a_fixed_anchor_not_accumulated_sleeps(tmp_path):
    """The B06-aligner lesson: every target must be `anchor + delta/speed`,
    not `previous_target + this_delta` -- otherwise per-call float error
    would compound over a long replay. Feed a fake clock that reports the
    *previous* sleep call's requested duration as elapsed (best case for a
    buggy cumulative implementation) and check the requested durations
    still match fixed-anchor deltas exactly."""
    log = tmp_path / "capture.log"
    log.write_text(
        "$H,1000000,0,0,0,0,0,0\n"
        "$H,1010000,0,0,0,0,0,0\n"   # +10ms
        "$H,1030000,0,0,0,0,0,0\n"   # +30ms from t0 (+20ms from previous)
        "$H,1060000,0,0,0,0,0,0\n"   # +60ms from t0 (+30ms from previous)
    )
    now = [0.0]

    def clock_fn():
        return now[0]

    requested = []

    def sleep_fn(seconds):
        requested.append(seconds)
        now[0] += seconds

    replay_log(log, lambda line: None, sleep_fn=sleep_fn, clock_fn=clock_fn)

    assert requested == pytest.approx([0.010, 0.020, 0.030])


def test_replay_log_speed_scales_the_requested_sleep_duration(tmp_path):
    log = tmp_path / "capture.log"
    log.write_text("$H,1000000,0,0,0,0,0,0\n$H,1040000,0,0,0,0,0,0\n")  # +40ms
    now = [0.0]
    requested = []
    replay_log(
        log, lambda line: None,
        speed=4.0,
        sleep_fn=lambda s: (requested.append(s), now.__setitem__(0, now[0] + s)),
        clock_fn=lambda: now[0],
    )
    assert requested == pytest.approx([0.010])  # 40ms / speed 4.0


def test_replay_log_loop_reanchors_instead_of_drifting(tmp_path):
    log = tmp_path / "capture.log"
    log.write_text("$H,1000000,0,0,0,0,0,0\n$H,1020000,0,0,0,0,0,0\n")  # +20ms
    now = [0.0]
    stop_after = {"n": 0}

    def running_fn():
        stop_after["n"] += 1
        return stop_after["n"] <= 5  # allow a bit more than one full pass through the 2-line file

    requested = []
    replay_log(
        log, lambda line: None, loop=True, running_fn=running_fn,
        sleep_fn=lambda s: (requested.append(s), now.__setitem__(0, now[0] + s)),
        clock_fn=lambda: now[0],
    )

    # Each pass re-anchors at its own start, so every pass reproduces the
    # same +20ms gap -- it does not keep growing across loop iterations.
    assert requested == pytest.approx([0.020, 0.020])


def test_replay_log_running_fn_false_stops_immediately(tmp_path):
    log = tmp_path / "capture.log"
    log.write_text("$H,1000000,0,0,0,0,0,0\n$H,1020000,0,0,0,0,0,0\n")
    written = []
    replay_log(log, written.append, sleep_fn=lambda s: None, clock_fn=lambda: 0.0,
               running_fn=lambda: False)
    assert written == []


# ---------------------------------------------------------------------------
# Real timing accuracy: no fake clock this time -- validates the story's own
# acceptance bar ("< 10 ms error") against a real, short replay.

def test_replay_log_real_timing_matches_t_us_deltas_within_10ms(tmp_path):
    log = tmp_path / "capture.log"
    log.write_text(
        "$H,1000000,0,0,0,0,0,0\n"
        "$H,1050000,0,0,0,0,0,0\n"   # +50ms
        "$H,1150000,0,0,0,0,0,0\n"   # +100ms
    )
    arrival_wall_times = []
    start = time.monotonic()
    replay_log(log, lambda line: arrival_wall_times.append(time.monotonic() - start))

    deltas_ms = [(arrival_wall_times[1] - arrival_wall_times[0]) * 1000,
                 (arrival_wall_times[2] - arrival_wall_times[1]) * 1000]
    assert deltas_ms[0] == pytest.approx(50, abs=10)
    assert deltas_ms[1] == pytest.approx(100, abs=10)


# ---------------------------------------------------------------------------
# End-to-end: mock_device.py itself, recording its own output as the "real"
# log (E01 -- an actual board -- is skipped for this project; the mechanism
# is identical regardless of where the log came from, see mock_device.py's
# module docstring), then replaying that file through a second invocation.

def test_cli_replay_reproduces_a_self_recorded_log(tmp_path):
    link = MockLink("--fps", "20", "--seed", "1")
    try:
        lines = [ln for ln in link.drain(seconds=0.5) if ln]
    finally:
        link.proc.terminate()
        link.proc.wait(timeout=2)

    assert any(ln.startswith("$T,") for ln in lines), "fixture recorded no $T lines to replay"

    log_path = tmp_path / "captured.log"
    # Splice in a noise line, exactly as idf.py monitor's boot banner would
    # interleave with real protocol lines in a captured file.
    log_path.write_text("ets Jul 29 2019 12:21:46\n" + "\n".join(lines) + "\n")

    replay = MockLink("--replay-log", str(log_path), "--replay-speed", "2")
    try:
        replayed = [ln for ln in replay.drain(seconds=2.0) if ln]
    finally:
        replay.proc.terminate()
        replay.proc.wait(timeout=2)

    original_protocol_lines = [ln for ln in lines if is_protocol_line(ln)]
    assert replayed == original_protocol_lines
