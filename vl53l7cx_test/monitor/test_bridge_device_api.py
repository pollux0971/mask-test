"""`/device/*` —— 掃描序列埠、回報連線狀態。

使用者的原話：「我希望有個按鈕可以掃描與連接我的 esp32」。
在此之前，26 個端點裡一個 device 相關的都沒有，他必須自己開終端機、
自己知道是哪個埠——而那顆板子的接頭會鬆，拔插後可能就變成 ttyUSB1。
"""

from __future__ import annotations

import pytest

from test_bridge_session_api import _request
from test_bridge_sse import Rig, _of_type


@pytest.fixture
def rig():
    r = Rig("--proto", "v2")
    try:
        yield r
    finally:
        r.close()


# -- scanning -------------------------------------------------------------


def test_ports_lists_every_serial_port(rig):
    status, body = _request(rig, "GET", "/device/ports")
    assert status == 200, body
    assert isinstance(body["ports"], list)
    for p in body["ports"]:
        assert set(p) >= {"device", "description", "vid", "pid", "likely_esp32"}
        assert isinstance(p["likely_esp32"], bool)


def test_nothing_is_filtered_out(rig):
    """A board behind an unrecognised adapter must not vanish from a list
    that claims to be complete -- likely candidates only sort first."""
    body = _request(rig, "GET", "/device/ports")[1]
    devices = [p["device"] for p in body["ports"]]
    assert len(devices) == len(set(devices))
    likely = [i for i, p in enumerate(body["ports"]) if p["likely_esp32"]]
    if likely:
        assert likely == list(range(len(likely))), "likely ports are not first"


def test_unknown_adapters_are_flagged_false_not_guessed(rig):
    """Motherboard /dev/ttyS* have no VID at all; guessing either way is worse.

    This machine reports 32 of them. An unannotated list of 32 identical
    entries is unusable, which is the whole reason the flag exists.
    """
    body = _request(rig, "GET", "/device/ports")[1]
    for p in body["ports"]:
        if p["vid"] is None:
            assert p["likely_esp32"] is False, p["device"]


def test_a_hint_is_given_when_no_esp32_is_obvious(rig):
    """"No likely port" must not read as "none of these work"."""
    body = _request(rig, "GET", "/device/ports")[1]
    if not body["likely"]:
        assert body.get("hint"), "no candidates and no explanation"


def test_ports_reports_which_port_is_currently_in_use(rig):
    body = _request(rig, "GET", "/device/ports")[1]
    assert "connected_port" in body
    assert body["connected_port"] == rig.pty


# -- status: opened vs actually receiving ---------------------------------


def test_status_distinguishes_opened_from_receiving(rig):
    """A port that opens is not a board that works.

    Measured on the real board: the port opened cleanly while neither ToF
    sensor produced a single line. Collapsing the two would show
    "connected" over a silent link, and the user would have no idea what to
    check.
    """
    rig.read_events(2.0)
    status, body = _request(rig, "GET", "/device/status")
    assert status == 200, body
    assert body["state"] == "receiving"
    assert body["data_seen"] is True
    assert body["port"] == rig.pty
    assert body["connected_for_s"] > 0
    assert body["seconds_to_first_line"] is not None


def test_status_carries_the_sensor_health_signals(rig):
    """"Connected but one sensor died" is a state, and not the same as
    "disconnected" -- so status carries the same signals the panel shows."""
    rig.read_events(2.0)
    body = _request(rig, "GET", "/device/status")[1]
    assert body["sensors_seen"] == "AB"
    assert isinstance(body["stale_streams"], list)


def test_link_event_says_whether_data_has_arrived(rig):
    """The panel should be able to show "opened, waiting" then "connected"."""
    events = rig.read_events(2.0)
    links = _of_type(events, "link")
    for e in links:
        if e["state"] == "up":
            assert "data_seen" in e, "link up says nothing about actual data"
            assert e.get("port")


# -- connect / disconnect -------------------------------------------------


def test_connect_needs_a_port(rig):
    status, body = _request(rig, "POST", "/device/connect", {})
    assert status == 400 and "port" in body["error"]


def test_connect_to_a_missing_port_says_so_plainly(rig):
    """"Does not exist" and "no permission" are different problems.

    A generic "cannot open" makes a permissions issue look like a broken
    board, and the user goes hunting for the wrong thing.
    """
    status, body = _request(rig, "POST", "/device/connect",
                            {"port": "/dev/nope-xyz"})
    assert status == 409
    assert "不存在" in body["error"]
    assert "dialout" not in body["error"], "wrong diagnosis for a missing port"


def test_connect_is_refused_during_a_session(rig):
    """Swapping boards mid-session would put two devices' data under one /meta."""
    from test_bridge_session_api import VALID_METADATA
    rig.read_events(2.0)
    assert _request(rig, "POST", "/session/start", VALID_METADATA)[0] == 200
    status, body = _request(rig, "POST", "/device/connect", {"port": rig.pty})
    assert status == 409
    assert body["session_id"]


def test_disconnect_is_refused_during_a_session(rig):
    from test_bridge_session_api import VALID_METADATA
    rig.read_events(2.0)
    assert _request(rig, "POST", "/session/start", VALID_METADATA)[0] == 200
    assert _request(rig, "POST", "/device/disconnect")[0] == 409


def test_disconnect_is_a_state_not_a_fault(rig):
    """The panel must not tell the user to check the wiring of a board they
    just unplugged themselves."""
    import time
    rig.read_events(2.0)
    assert _request(rig, "POST", "/device/disconnect")[0] == 200
    time.sleep(1.0)
    body = _request(rig, "GET", "/device/status")[1]
    assert body["state"] == "disconnected"
    assert body["user_disconnected"] is True
    assert body["stale_streams"] == [], (
        "a deliberate disconnect raised stale-stream alarms"
    )


def test_reconnecting_forgets_the_previous_board(rig):
    """A new link must not inherit the old one's observations.

    seq counters restart on a different device and "which sensors have been
    seen" says nothing about the new board -- carrying either across would
    make a fresh board look like whatever was plugged in before.
    """
    import time
    rig.read_events(2.5)
    before = _request(rig, "GET", "/device/status")[1]
    assert before["sensors_seen"] == "AB"

    assert _request(rig, "POST", "/device/disconnect")[0] == 200
    time.sleep(0.8)
    assert _request(rig, "GET", "/device/status")[1]["sensors_seen"] == ""

    status, body = _request(rig, "POST", "/device/connect", {"port": rig.pty})
    assert status == 202, body
    assert body["state"] == "connecting"

    # It comes back on its own, and the observations rebuild from scratch.
    for _ in range(40):
        time.sleep(0.25)
        again = _request(rig, "GET", "/device/status")[1]
        if again["state"] == "receiving":
            break
    assert again["state"] == "receiving", again
    assert again["user_disconnected"] is False
    assert again["sensors_seen"] == "AB"


def test_protocol_is_renegotiated_after_a_reconnect(rig):
    """A different board can be a different firmware version.

    The v1 degradation race was exactly this: a device that announces itself
    once. Reconnecting puts us back in that situation, so the parser is
    rebuilt and the bridge asks again.
    """
    import time
    rig.read_events(2.5)
    assert _request(rig, "POST", "/device/disconnect")[0] == 200
    time.sleep(0.8)
    assert _request(rig, "POST", "/device/connect", {"port": rig.pty})[0] == 202

    for _ in range(40):
        time.sleep(0.25)
        status = _of_type(rig.read_events(0.4), "status")
        if status and status[-1].get("proto_confirmed"):
            break
    assert status and status[-1]["proto_confirmed"] is True, status
    assert status[-1]["sr"] == 16000
