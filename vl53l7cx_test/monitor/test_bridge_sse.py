"""B19 end-to-end: mock device -> bridge_server -> SSE.

Runs the real T04 mock device and the real bridge against each other and
reads the event stream a browser would read. The unit tests cover the metric
arithmetic; this covers the wiring, which is where the interesting failures
are -- an event that never reaches /events, or reaches it missing the fields
the panel needs.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_DEVICE = REPO_ROOT / "ssi-backlog" / "tools" / "mock_device.py"
BRIDGE = Path(__file__).resolve().parent / "bridge_server.py"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    """A mock device and a bridge wired together, plus an SSE reader."""

    def __init__(self, *mock_args, bridge_args=()):
        self.mock = subprocess.Popen(
            [sys.executable, str(MOCK_DEVICE), "--fps", "30", "--mic-fps", "20",
             "--seed", "5", *mock_args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        header = self.mock.stdout.readline()
        m = re.search(r"pty ready:\s*(\S+)", header)
        assert m, f"unexpected mock_device header: {header!r}"
        self.pty = m.group(1)

        self.http_port = _free_port()
        self.bridge = subprocess.Popen(
            [sys.executable, str(BRIDGE), "--port", self.pty,
             "--http-port", str(self.http_port), *bridge_args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO_ROOT),
        )
        self._wait_for_http()

    def _wait_for_http(self, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.bridge.poll() is not None:
                raise AssertionError(
                    f"bridge exited early:\n{self.bridge.stderr.read()}")
            try:
                with socket.create_connection(("127.0.0.1", self.http_port), 0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("bridge never opened its HTTP port")

    def read_events(self, seconds):
        """Collect SSE `data:` payloads for a fixed window."""
        events = []
        req = urllib.request.Request(f"http://127.0.0.1:{self.http_port}/events")
        with urllib.request.urlopen(req, timeout=seconds + 5) as resp:
            os.set_blocking(resp.fileno(), False)
            buf = ""
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    chunk = resp.read(65536)
                except (BlockingIOError, OSError):
                    time.sleep(0.02)
                    continue
                if not chunk:
                    time.sleep(0.02)
                    continue
                buf += chunk.decode("utf-8", errors="replace")
                *lines, buf = buf.split("\n")
                for line in lines:
                    if line.startswith("data: "):
                        try:
                            events.append(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            pass
        return events

    def close(self):
        for proc in (self.bridge, self.mock):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture(scope="module")
def v2_events():
    """One 4-second capture, shared by every v2 assertion below.

    Module-scoped because each rig costs two processes and several seconds
    of streaming; the assertions are all read-only views of the same window.
    """
    rig = Rig("--proto", "v2")
    try:
        yield rig.read_events(4.0)
    finally:
        rig.close()


def _of_type(events, kind):
    return [e for e in events if e.get("type") == kind]


# -- the panel's data path ----------------------------------------------


def test_tof_events_carry_seq_and_timestamp(v2_events):
    """C03's dataStore needs these; the old v1 parser never supplied them."""
    tof = _of_type(v2_events, "tof")
    assert tof, "no tof events reached the SSE stream"
    for e in tof[:20]:
        assert isinstance(e["seq"], int)
        assert isinstance(e["t_us"], int) and e["t_us"] >= 0
        assert e["sensor"] in ("A", "B")
        assert len(e["dist"]) == e["dim"] == len(e["signal"]) == len(e["valid"])


def test_mic_events_carry_seq_and_integer_rms(v2_events):
    """CONTRACTS #1.1: rms is i16, not the draft's float."""
    mic = _of_type(v2_events, "mic")
    assert mic
    for e in mic[:20]:
        assert isinstance(e["seq"], int)
        assert isinstance(e["t_us"], int)
        assert isinstance(e["rms"], int) and not isinstance(e["rms"], bool)
        assert isinstance(e["peak"], int)


def test_both_sensors_stream(v2_events):
    sensors = {e["sensor"] for e in _of_type(v2_events, "tof")}
    assert sensors == {"A", "B"}


def test_heartbeat_reaches_the_panel(v2_events):
    hb = _of_type(v2_events, "heartbeat")
    assert hb, "no heartbeat events; the panel cannot show heap or temperature"
    for e in hb:
        assert set(e) >= {"drop_A", "drop_B", "drop_M", "heap", "temp_c"}


# -- status: B02's negotiation state, forwarded ---------------------------


def test_status_event_forwards_the_negotiation_state(v2_events):
    status = _of_type(v2_events, "status")
    assert status
    last = status[-1]
    assert last["protocol_version"] == 2
    assert last["proto_confirmed"] is True
    assert last["degraded"] is False
    assert last["recording_allowed"] is True
    assert last["warning"] is None
    assert last["dim"] == 16  # 4x4, kept for the panel's existing handler


def test_status_event_carries_the_frame_parameters(v2_events):
    """CONTRACTS #1.1.2 -- the panel must know which $F cadence it is on."""
    last = _of_type(v2_events, "status")[-1]
    assert last["sr"] == 16000
    assert last["mel_win"] == 512
    assert last["mel_hop"] == 256
    assert last["mic_hop"] == 512


# -- the quality event ---------------------------------------------------


def test_quality_event_arrives_at_about_1hz(v2_events):
    quality = _of_type(v2_events, "quality")
    # One is pushed immediately on connect, then one per second.
    assert len(quality) >= 3, f"only {len(quality)} quality events in 4s"


def test_quality_event_has_every_metric(v2_events):
    from host.quality.metrics import METRIC_ORDER
    last = _of_type(v2_events, "quality")[-1]
    assert set(last["metrics"]) == set(METRIC_ORDER)
    for name, entry in last["metrics"].items():
        assert entry["level"] in ("green", "yellow", "red", "unknown"), name
        assert set(entry) <= {"value", "level", "hint"}, name


def test_quality_measures_an_uninjected_link_plausibly(v2_events):
    """No fault injection: every metric that has data should read sensibly.

    Since B20 the drop rate here is exactly zero; the next test asserts that
    specifically.
    """
    metrics = _of_type(v2_events, "quality")[-1]["metrics"]
    assert metrics["drop_rate"]["value"] == 0.0
    assert metrics["valid_zones"]["value"] > 0.9
    assert 0.0 < metrics["bandwidth"]["value"] < 1.0
    assert metrics["symmetry"]["value"] < 0.15        # same synthetic scene both sides
    assert metrics["clock_resid"]["value"] < 0.005    # B04's bound
    assert metrics["noise_floor"]["value"] is not None


def test_no_transport_loss_on_an_uninjected_link(v2_events):
    """B20 acceptance: `delta` is 0 -- the bridge loses nothing of its own.

    This assertion started life as its opposite. B19 reported ~0.7% loss
    here and raised a transport alarm, which is what opened B20. Two host
    -side counting bugs turned out to be responsible, not the transport:
    the tracker charged the attach `seq` as pre-roll loss, and the alarm
    compared the host's live total against a heartbeat sampled up to a
    second earlier. With both fixed, a bare readline() loop and the full
    bridge agree -- nothing is lost between 18% and 92% of link capacity.
    """
    last = _of_type(v2_events, "quality")[-1]
    assert last.get("alarms", []) == [], (
        f"bridge lost frames the device says it sent: {last.get('alarms')}"
    )
    assert last["metrics"]["drop_rate"]["value"] == 0.0


def test_no_threshold_load_error(v2_events):
    """The shipped config must actually parse in the running bridge."""
    for event in _of_type(v2_events, "quality"):
        assert "threshold_error" not in event, event.get("threshold_error")


# -- drop injection ------------------------------------------------------


def test_quality_reports_injected_drops():
    rig = Rig("--proto", "v2", "--drop-rate", "0.2")
    try:
        events = rig.read_events(5.0)
    finally:
        rig.close()
    quality = _of_type(events, "quality")
    assert quality
    rate = quality[-1]["metrics"]["drop_rate"]["value"]
    assert rate is not None and rate > 0.05, f"20% injected, measured {rate}"
    assert quality[-1]["metrics"]["drop_rate"]["level"] == "red"
    assert quality[-1]["metrics"]["drop_rate"]["hint"]


# -- v1 degraded mode ----------------------------------------------------


def test_v1_is_refused_by_default():
    """CONTRACTS #1.1: no silent backward compatibility."""
    rig = Rig("--proto", "v1")
    try:
        events = rig.read_events(3.0)
    finally:
        rig.close()
    assert not _of_type(events, "tof"), "v1 data was accepted without --allow-v1"
    # The quality stream must keep running even with no usable data.
    assert _of_type(events, "quality")


def test_v1_with_allow_flag_is_degraded_and_blocks_recording():
    """B02's two panel-facing acceptance criteria, now visible over SSE."""
    rig = Rig("--proto", "v1", bridge_args=("--allow-v1",))
    try:
        events = rig.read_events(3.0)
    finally:
        rig.close()
    status = _of_type(events, "status")
    assert status
    last = status[-1]
    assert last["protocol_version"] == 1
    assert last["degraded"] is True
    assert last["recording_allowed"] is False
    assert last["warning"]

    tof = _of_type(events, "tof")
    assert tof, "v1 data was not accepted even with --allow-v1"
    for e in tof[:10]:
        # v1 has no seq/t_us on the wire and none is invented here.
        assert "seq" not in e and "t_us" not in e
        assert e["proto"] == 1 and e["has_timestamp"] is False
