"""Tests for the T04 mock device's host->device command handling.

``mock_device.py`` is not an ordinary tool: it is the executable form of the
serial contract, and B02, B03, B05 and B06 all run their end-to-end tests
against it. If it drifts from CONTRACTS.md every one of those stories passes
against the wrong device, silently.

These cover the command replies specifically, because that is where the mock
and the real firmware most easily diverge -- the streaming side is exercised
by every other end-to-end test in the repo, but nothing else drives ``PING``,
``SENS:`` or ``MEL:`` and checks what comes back.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

MOCK_DEVICE = Path(__file__).resolve().parent / "mock_device.py"


class MockLink:
    """Runs mock_device.py and talks to its pty from the host side."""

    def __init__(self, *args):
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK_DEVICE), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        header = self.proc.stdout.readline()
        m = re.search(r"pty ready:\s*(\S+)", header)
        assert m, f"unexpected mock_device header: {header!r}"
        self.fd = os.open(m.group(1), os.O_RDWR | os.O_NONBLOCK)

    def drain(self, seconds=0.8):
        """Read for a fixed window and return complete lines.

        Fixed window rather than "read until quiet": the mock streams
        continuously, so there is no quiet moment to wait for, and a reply
        can arrive up to one scheduler tick after the command.
        """
        chunks = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data = os.read(self.fd, 1 << 20)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            if not data:
                break
            chunks.append(data.decode("ascii", errors="replace"))
        return "".join(chunks).split("\n")

    def send(self, command, seconds=0.8):
        """Send one command; return the lines that arrived after it."""
        os.write(self.fd, (command + "\n").encode("ascii"))
        return self.drain(seconds)

    def close(self):
        os.close(self.fd)
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


@pytest.fixture
def link(request):
    """A running mock device. Parametrise with the CLI args to pass it."""
    args = getattr(request, "param", ("--proto", "v2"))
    ln = MockLink("--fps", "5", "--mic-fps", "5", "--seed", "3", *args)
    ln.drain(0.8)  # boot $STATUS and the first frames
    yield ln
    ln.close()


def _status_lines(lines):
    return [L for L in lines if L.startswith("$STATUS")]


def _status_fields(line):
    """$STATUS parses as key=value, order-independent (CONTRACTS.md #1.1.2)."""
    out = {}
    for token in line.split(",")[1:]:
        if "=" in token:
            k, v = token.split("=", 1)
            out[k] = v
    return out


# -- v2: $STATUS self-description (CONTRACTS.md #1.1.2) -----------------


def test_boot_status_carries_the_self_description_fields():
    ln = MockLink("--proto", "v2", "--fps", "5", "--seed", "3")
    try:
        status = _status_lines(ln.drain(0.8))
        assert len(status) == 1, f"expected exactly one boot $STATUS, got {status}"
        f = _status_fields(status[0])
        assert f["proto"] == "2"
        assert f["res"] == "4"
        assert f["fw"]
        # The five fields A11-A14 made necessary: the same $F line means a
        # different thing before and after A14, so the device must say which.
        assert f["sr"] == "16000"
        assert f["mel"] == "0"
        assert f["mel_win"] == "512"
        assert f["mel_hop"] == "256"
        assert f["mic_hop"] == "512"
    finally:
        ln.close()


# -- v2: PING ------------------------------------------------------------


def test_v2_ping_answers_with_heartbeat_then_status(link):
    """CONTRACTS.md #1.1: PING re-sends $STATUS. #1.3: $H is the reply B05
    times against, so it must come first."""
    lines = link.send("PING")
    kinds = [L.split(",")[0] for L in lines if L.startswith(("$H", "$STATUS"))]
    assert "$STATUS" in kinds, "PING did not re-send $STATUS"
    assert "$H" in kinds
    assert kinds.index("$H") < kinds.index("$STATUS"), (
        f"$STATUS came before $H: {kinds}"
    )


def test_v2_ping_status_carries_the_same_fields(link):
    f = _status_fields(_status_lines(link.send("PING"))[0])
    assert {"res", "proto", "fw", "sr", "mel", "mel_win", "mel_hop", "mic_hop"} <= set(f)


# -- v2: SENS / MEL ------------------------------------------------------


def test_v2_mel_on_resends_status_with_mel_set(link):
    f = _status_fields(_status_lines(link.send("MEL:1"))[0])
    assert f["mel"] == "1"
    f = _status_fields(_status_lines(link.send("MEL:0"))[0])
    assert f["mel"] == "0"


def test_v2_sens_resends_status(link):
    assert _status_lines(link.send("SENS:B=0")), "SENS did not re-send $STATUS"


def test_v2_malformed_sens_does_not_resend_status(link):
    """A line the device could not act on must not claim the config changed.

    A $STATUS is the host's cue to re-read the device's configuration;
    emitting one for a command that did nothing tells the host to go look at
    something that did not happen.
    """
    for bad in ("SENS:C=1", "SENS:B", "SENS:", "SENS:B=0=1"):
        assert not _status_lines(link.send(bad, seconds=0.5)), (
            f"{bad!r} wrongly triggered a $STATUS re-send"
        )


def test_v2_malformed_mel_does_not_resend_status(link):
    """Same class of bug as malformed SENS: a value that is not 0 or 1."""
    for bad in ("MEL:garbage1", "MEL:", "MEL:2", "MEL:01"):
        assert not _status_lines(link.send(bad, seconds=0.5)), (
            f"{bad!r} wrongly triggered a $STATUS re-send"
        )


def test_v2_sens_off_stops_that_sensors_frames(link):
    link.send("SENS:B=0")
    lines = link.drain(1.0)
    assert not [L for L in lines if L.startswith("$T,B,")], "B still streaming"
    assert [L for L in lines if L.startswith("$T,A,")], "A stopped too"


# -- v1: the legacy firmware had none of this ---------------------------


def test_v1_boot_status_is_unchanged():
    ln = MockLink("--proto", "v1", "--fps", "5", "--seed", "3")
    try:
        status = _status_lines(ln.drain(0.8))
        assert status == ["$STATUS,res=4"]
    finally:
        ln.close()


@pytest.mark.parametrize("command", ["PING", "SENS:B=0", "MEL:1"])
def test_v1_ignores_v2_commands_entirely(command):
    """Pins down that --proto v1 stays silent, and why.

    v1 mode emulates the unmodified pre-A09 firmware, whose uart_cmd.c
    understood REC: and nothing else. Making it answer PING/SENS/MEL would
    invent a firmware that never existed, and B02 -- which uses this mode as
    its dual-protocol reference -- would then pass against a device the real
    world cannot produce. That is worse than having no test, because it
    hands back confidence that is not real.

    So this is deliberately a regression guard against a future tidy-up that
    "unifies" the two modes.
    """
    ln = MockLink("--proto", "v1", "--fps", "5", "--seed", "3")
    try:
        ln.drain(0.8)
        lines = ln.send(command, seconds=1.0)
        assert not _status_lines(lines), f"v1 answered {command} with a $STATUS"
        assert not [L for L in lines if L.startswith("$H")], (
            f"v1 answered {command} with a $H; the legacy firmware never sent $H"
        )
    finally:
        ln.close()


def test_v1_still_streams_v1_lines():
    """Sanity: the legacy mode is still doing its actual job."""
    ln = MockLink("--proto", "v1", "--fps", "5", "--seed", "3")
    try:
        lines = ln.drain(1.0)
        assert [L for L in lines if L.startswith("$TOF,")]
        assert [L for L in lines if L.startswith("$MIC,")]
        assert not [L for L in lines if L.startswith(("$T,", "$M,", "$F,"))], (
            "v1 mode emitted protocol-v2 lines"
        )
    finally:
        ln.close()
