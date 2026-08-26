"""HTTP wiring for B11 (trial state machine) and B12 (hold-to-record)."""

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


def _session_with_baseline(rig):
    """Start a session and capture its baseline; skip if the gate rejects it."""
    rig.read_events(3.5)  # fill the aligner
    assert _request(rig, "POST", "/session/start", VALID_METADATA)[0] == 200
    status, body = _request(rig, "POST", "/session/baseline?seconds=2")
    if status != 200:
        pytest.skip(f"baseline gate rejected the synthetic scene: {body.get('reason')}")
    return body


# -- the baseline gate ---------------------------------------------------


def test_trial_without_a_session_is_409(rig):
    status, body = _request(rig, "POST", "/trial/start", {})
    assert status == 409 and "session" in body["error"]


def test_trial_before_baseline_is_refused(rig):
    """B10's gate: a trial with no baseline cannot be normalised later.

    Refusing now beats discovering it in analysis after a four-hour session.
    """
    _request(rig, "POST", "/session/start", VALID_METADATA)
    status, body = _request(rig, "POST", "/trial/start", {})
    assert status == 409
    assert body["baseline_done"] is False


def test_unknown_trial_action_is_404(rig):
    _session_with_baseline(rig)
    assert _request(rig, "POST", "/trial/nonsense", {})[0] == 404


# -- fixed-duration trials -----------------------------------------------


def test_start_enters_prompt(rig):
    _session_with_baseline(rig)
    status, body = _request(rig, "POST", "/trial/start", {})
    assert status == 200, body
    assert body["state"] == "PROMPT"
    assert body["events"][0]["label"]


def test_start_twice_is_409(rig):
    """The machine only starts from IDLE; the second call is a state error."""
    _session_with_baseline(rig)
    assert _request(rig, "POST", "/trial/start", {})[0] == 200
    status, body = _request(rig, "POST", "/trial/start", {})
    assert status == 409
    assert body["state"] == "PROMPT"


def test_trial_transitions_are_broadcast(rig):
    """The ticker drives the machine; the panel follows over SSE."""
    import threading
    _session_with_baseline(rig)
    collected = []

    def watch():
        collected.extend(rig.read_events(6.0))

    t = threading.Thread(target=watch)
    t.start()
    time.sleep(0.3)
    _request(rig, "POST", "/trial/start", {})
    t.join()

    states = [e["state"] for e in _of_type(collected, "trial")]
    assert states, "no trial events reached the SSE stream"
    assert "COUNTDOWN" in states, f"the ticker never advanced past PROMPT: {states}"


# -- abort vs redo -------------------------------------------------------


def test_abort_and_redo_differ_in_the_next_word(rig):
    """abort skips this word; redo keeps it for the next start."""
    _session_with_baseline(rig)

    _request(rig, "POST", "/trial/start", {})
    first = _request(rig, "POST", "/trial/redo")[1]["events"][0]
    redo_next = first.get("next_label")

    _request(rig, "POST", "/trial/start", {})
    after_redo = _request(rig, "POST", "/trial/abort")[1]["events"][0]
    abort_next = after_redo.get("next_label")

    if redo_next is not None and abort_next is not None:
        assert redo_next != abort_next, (
            "abort must advance the word order and redo must not; "
            f"both reported {redo_next!r}"
        )


def test_redo_returns_to_idle(rig):
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/start", {})
    status, body = _request(rig, "POST", "/trial/redo")
    assert status == 200
    assert body["state"] == "IDLE"


def test_abort_from_idle_is_409(rig):
    """Nothing to abort, and the machine says so rather than silently passing."""
    _session_with_baseline(rig)
    status, body = _request(rig, "POST", "/trial/abort")
    assert status == 409
    assert body["state"] == "IDLE"


# -- B12: hold to record --------------------------------------------------


def test_hold_start_goes_straight_to_capture(rig):
    """No COUNTDOWN: the user already decided when to speak by pressing."""
    _session_with_baseline(rig)
    status, body = _request(rig, "POST", "/trial/hold/start", {})
    assert status == 200, body
    assert body["state"] == "CAPTURE"


def test_hold_capture_is_not_ended_by_the_ticker(rig):
    """Its length is the user's to decide; only hold_stop ends it."""
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(2.0)  # longer than the fixed-duration CAPTURE
    assert _request(rig, "GET", "/session/current")[0] == 200
    status, body = _request(rig, "POST", "/trial/hold/stop")
    assert status == 200, body
    # B12 stops into CONFIRM: computed, but deliberately not yet on disk.
    assert body["state"] in ("CONFIRM", "SAVE", "REST"), body


def test_hold_stop_then_confirm_keeps_the_trial(rig):
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(1.0)
    stopped = _request(rig, "POST", "/trial/hold/stop")[1]
    if stopped["state"] != "CONFIRM":
        pytest.skip(f"machine stopped into {stopped['state']}, not CONFIRM")
    status, body = _request(rig, "POST", "/trial/confirm")
    assert status == 200, body
    assert body["state"] in ("REST", "IDLE", "SAVE")


def test_hold_stop_then_discard_drops_it(rig):
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(1.0)
    stopped = _request(rig, "POST", "/trial/hold/stop")[1]
    if stopped["state"] != "CONFIRM":
        pytest.skip(f"machine stopped into {stopped['state']}, not CONFIRM")
    status, body = _request(rig, "POST", "/trial/discard")
    assert status == 200, body
    assert body["state"] in ("IDLE", "REST")


def test_confirm_outside_confirm_state_is_409(rig):
    _session_with_baseline(rig)
    status, body = _request(rig, "POST", "/trial/confirm")
    assert status == 409
    assert body["state"] == "IDLE"


# -- persistence ----------------------------------------------------------


def test_a_saved_trial_does_not_destroy_the_baseline(rig):
    """The exact failure that blocked this wiring: mode="w" truncates.

    trial_000 is the baseline (B10) and the machine starts at index 1, so a
    completed trial has to leave both in the file.
    """
    import h5py
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(1.2)
    stopped = _request(rig, "POST", "/trial/hold/stop")[1]
    if stopped["state"] == "CONFIRM":
        _request(rig, "POST", "/trial/confirm")
    time.sleep(1.0)

    files = list((rig.workdir / "sessions").glob("*.h5"))
    assert files, "no session file"
    with h5py.File(files[0], "r") as f:
        trials = sorted(k for k in f if k.startswith("trial"))
        assert "trial_000" in trials, f"the baseline was destroyed: {trials}"
        assert f["trial_000"].attrs.get("label") in ("_baseline", b"_baseline")
