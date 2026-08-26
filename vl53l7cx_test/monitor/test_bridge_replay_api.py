"""HTTP wiring for B17 session replay (and C24's session list)."""

from __future__ import annotations

import time

import pytest

from test_bridge_session_api import _request, VALID_METADATA
from test_bridge_sse import Rig, _of_type


@pytest.fixture
def rig():
    r = Rig("--proto", "v2")
    try:
        yield r
    finally:
        r.close()


def _recorded_session(rig):
    """Record one real trial and return the file's path on the server."""
    rig.read_events(3.5)
    assert _request(rig, "POST", "/session/start", VALID_METADATA)[0] == 200
    status, body = _request(rig, "POST", "/session/baseline?seconds=2")
    if status != 200:
        pytest.skip(f"baseline gate rejected the synthetic scene: {body.get('reason')}")
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(1.2)
    assert _request(rig, "POST", "/trial/hold/stop")[1]["state"] == "REST"
    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)

    status, listing = _request(rig, "GET", "/replay/sessions")
    assert status == 200 and listing, "the recorded session is not listed"
    return listing[0]["path"]


# -- listing --------------------------------------------------------------


def test_sessions_list_is_empty_before_anything_is_recorded(rig):
    status, body = _request(rig, "GET", "/replay/sessions")
    assert status == 200 and body == []


def test_sessions_list_describes_each_file(rig):
    _recorded_session(rig)
    status, body = _request(rig, "GET", "/replay/sessions")
    assert status == 200 and body
    entry = body[0]
    assert entry["file"].endswith(".h5")
    assert entry["bytes"] > 0
    assert entry["modified_at"]


# -- start ----------------------------------------------------------------


def test_state_is_idle_before_any_replay(rig):
    status, body = _request(rig, "GET", "/replay/state")
    assert status == 200
    assert body["state"] == "idle" and body["active"] is False


def test_start_requires_a_file(rig):
    status, body = _request(rig, "POST", "/replay/start")
    assert status == 400 and body["error"]


def test_start_refuses_a_path_outside_the_sessions_directory(rig):
    """A path from a query string must not be able to read arbitrary files."""
    for probe in ("/etc/passwd", "../../../etc/passwd", "..%2F..%2Fconfig%2Fvocab.json"):
        status, _ = _request(rig, "POST", f"/replay/start?file={probe}")
        assert status == 404, probe


def test_start_on_a_recorded_session(rig):
    path = _recorded_session(rig)
    status, body = _request(rig, "POST", f"/replay/start?file={path}")
    assert status == 200, body
    assert body["active"] is True
    assert body["n_events"] > 0
    assert body["speed"] == 1.0


def test_control_before_start_is_409(rig):
    status, body = _request(rig, "POST", "/replay/control?action=pause")
    assert status == 409 and body["error"]


# -- controls -------------------------------------------------------------


def test_pause_and_resume(rig):
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    assert _request(rig, "POST", "/replay/control?action=pause")[1]["paused"] is True
    assert _request(rig, "POST", "/replay/control?action=resume")[1]["paused"] is False


def test_step_emits_one_event_while_paused(rig):
    import threading
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    _request(rig, "POST", "/replay/control?action=pause")

    collected = []

    def watch():
        collected.extend(rig.read_events(2.0))

    t = threading.Thread(target=watch)
    t.start()
    time.sleep(0.4)
    status, body = _request(rig, "POST", "/replay/control?action=step")
    t.join()
    assert status == 200 and body["stepped"] is True
    replayed = [e for e in collected if e.get("replay")]
    assert replayed, "step published nothing"


def test_bad_control_action_is_400(rig):
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    status, body = _request(rig, "POST", "/replay/control?action=nonsense")
    assert status == 400 and body["error"]


def test_speed_accepts_only_the_three_values(rig):
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    for value in ("0.25", "1", "4"):
        status, body = _request(rig, "POST", f"/replay/speed?value={value}")
        assert status == 200, body
        assert body["speed"] == float(value)
    for bad in ("2", "0.5", "100"):
        assert _request(rig, "POST", f"/replay/speed?value={bad}")[0] == 400, bad


def test_seek_to_a_missing_trial_is_400(rig):
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    status, body = _request(rig, "POST", "/replay/seek?trial=999")
    assert status == 400 and body["error"]


def test_seek_to_an_existing_trial(rig):
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    status, body = _request(rig, "POST", "/replay/seek?trial=0")
    assert status == 200, body
    assert body["current_trial_idx"] == 0


def test_unknown_replay_action_is_404(rig):
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    assert _request(rig, "POST", "/replay/nonsense")[0] == 404


# -- the disaster case ----------------------------------------------------


def test_live_device_data_is_blocked_while_replaying(rig):
    """The story's named disaster: two data streams interleaved.

    The mock keeps streaming throughout, so without the guard every replayed
    frame would arrive mixed with a live one and the panel would have no way
    to tell which was which.
    """
    import threading
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    # Quarter speed so the replay is guaranteed to still be running for the
    # whole capture window. At 1x it can finish partway through, and then
    # live data legitimately resumes -- which would make this test pass or
    # skip for the wrong reason instead of checking the guard.
    assert _request(rig, "POST", "/replay/speed?value=0.25")[0] == 200

    collected = []

    def watch():
        collected.extend(rig.read_events(2.5))

    t = threading.Thread(target=watch)
    t.start()
    t.join()

    assert _request(rig, "GET", "/replay/state")[1]["active"] is True, (
        "the replay ended early; this test proves nothing about the guard"
    )

    data = [e for e in collected if e.get("type") in ("tof", "mic", "mel")]
    assert data, "nothing was published at all"
    live = [e for e in data if not e.get("replay")]
    assert not live, (
        f"{len(live)} live device frames leaked into the replay stream"
    )


def test_replayed_events_are_marked(rig):
    import threading
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    collected = []

    def watch():
        collected.extend(rig.read_events(2.5))

    t = threading.Thread(target=watch)
    t.start()
    t.join()

    replayed = [e for e in collected if e.get("replay")]
    assert replayed, "no replay events were published"
    # B17 replays the trial boundary events alongside the data, so the panel
    # can redraw the trial timeline as it plays; all of them carry the flag.
    kinds = {e["type"] for e in replayed}
    assert kinds <= {"tof", "mic", "mel", "trial"}, kinds
    assert kinds & {"tof", "mic"}, f"only {kinds} were replayed"
    for event in replayed[:20]:
        assert event["replay"] is True


def test_device_state_still_reaches_the_panel_during_replay(rig):
    """Only data is blocked. Hiding heartbeats would make the link look dead."""
    import threading
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    collected = []

    def watch():
        collected.extend(rig.read_events(3.0))

    t = threading.Thread(target=watch)
    t.start()
    t.join()
    assert _of_type(collected, "heartbeat"), "device heartbeats were suppressed too"


def test_stopping_a_replay_lets_live_data_through_again(rig):
    import threading
    path = _recorded_session(rig)
    _request(rig, "POST", f"/replay/start?file={path}")
    assert _request(rig, "POST", "/replay/stop")[0] == 200

    collected = []

    def watch():
        collected.extend(rig.read_events(2.0))

    t = threading.Thread(target=watch)
    t.start()
    t.join()
    live = [e for e in collected if e.get("type") == "tof" and not e.get("replay")]
    assert live, "live data did not resume after the replay was stopped"
    assert _request(rig, "GET", "/replay/state")[1]["active"] is False
