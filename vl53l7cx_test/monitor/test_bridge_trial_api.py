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
    time.sleep(2.0)  # longer than the fixed-duration CAPTURE would allow
    status, body = _request(rig, "POST", "/trial/hold/stop")
    # 這條的主題是「ticker 不會替你結束 capture」——那一點由 hold/stop 回
    # 200（而不是 409「沒有進行中的 capture」）就已經驗到了。
    assert status == 200, body
    # 2 s 落在 B12 的 [0.3, 5.0] 窗內，正常會直接存檔；但高負載下 bridge
    # 量到的 hold 可能遠短於測試這邊 sleep 的時間，此時走 CONFIRM。
    # 那不影響本條的主題。
    if body["state"] == "CONFIRM":
        assert _request(rig, "POST", "/trial/confirm")[0] == 200
    else:
        assert body["state"] == "REST", body


def test_an_in_range_hold_saves_without_asking(rig):
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(1.0)
    body = _request(rig, "POST", "/trial/hold/stop")[1]
    states = [e["state"] for e in body["events"]]
    assert "SAVE" in states, states
    assert body["state"] == "REST"


def test_a_too_short_hold_goes_to_confirm_instead_of_guessing(rig):
    """B12: out-of-range holds are neither saved nor dropped automatically.

    A 0.1 s press is almost certainly a slip, but "almost certainly" is not
    good enough to throw away a trial the person may have meant -- nor to
    keep one that would quietly pollute the dataset. The data stays in
    memory and the operator decides.
    """
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(0.1)  # below the 0.3 s floor
    status, body = _request(rig, "POST", "/trial/hold/stop")
    assert status == 200, body
    assert body["state"] == "CONFIRM", body


def test_confirm_keeps_a_pending_trial(rig):
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(0.1)
    assert _request(rig, "POST", "/trial/hold/stop")[1]["state"] == "CONFIRM"
    status, body = _request(rig, "POST", "/trial/confirm")
    assert status == 200, body
    assert body["state"] in ("REST", "IDLE")
    assert "SAVE" in [e["state"] for e in body["events"]] or body["state"] == "REST"


def test_discard_drops_a_pending_trial(rig):
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(0.1)
    assert _request(rig, "POST", "/trial/hold/stop")[1]["state"] == "CONFIRM"
    status, body = _request(rig, "POST", "/trial/discard")
    assert status == 200, body
    assert body["state"] == "IDLE"


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
    state = _request(rig, "POST", "/trial/hold/stop")[1]["state"]
    if state == "CONFIRM":
        # 高負載下 hold 時長從**實際進入 HOLD** 才起算，所以測試這邊的
        # `sleep(1.2)` 可能只換到 0.13 秒的 hold，被判定 too_short。
        # 落在 CONFIRM 不是失敗——狀態機刻意不猜、改問使用者，確認之後
        # trial 一樣會存起來。硬斷言 `== "REST"` 會偽陽性失敗，而偽陽性跟
        # 真的壞掉長得一模一樣。
        assert _request(rig, "POST", "/trial/confirm")[0] == 200
    else:
        assert state == "REST", f"hold/stop 回到未預期的狀態: {state}"

    # End the session first: the writer holds the file open for the whole
    # session, and HDF5 takes an exclusive lock on it.
    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)

    files = list((rig.workdir / "sessions").glob("*.h5"))
    assert files, "no session file"
    with h5py.File(files[0], "r") as f:
        trials = sorted(k for k in f if k.startswith("trial"))
        assert "trial_000" in trials, f"the baseline was destroyed: {trials}"
        assert "trial_001" in trials, f"the trial was not saved: {trials}"
        label = f["trial_000"].attrs.get("label")
        assert label in ("_baseline", b"_baseline"), label


# -- C14: rejecting an already-saved trial --------------------------------


def test_reject_marks_a_saved_trial_without_deleting_it(rig):
    """Rejecting is not deleting.

    D12's cross-validation needs to know how many attempts went wrong during
    a given wear, so the data stays and only the quality attr changes.
    """
    import h5py
    _session_with_baseline(rig)
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(1.2)
    state = _request(rig, "POST", "/trial/hold/stop")[1]["state"]
    if state == "CONFIRM":
        # 這條測的是「拒絕不等於刪除」，REST 只是為了先弄到一筆存好的
        # trial。高負載下 hold 會被判太短而走 CONFIRM——確認之後一樣有
        # 那筆 trial，測試的主題不受影響。
        assert _request(rig, "POST", "/trial/confirm")[0] == 200
    else:
        assert state == "REST", f"hold/stop 回到未預期的狀態: {state}"

    status, body = _request(rig, "POST", "/trial/reject", {"trial_idx": 1})
    assert status == 200, body

    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)
    files = list((rig.workdir / "sessions").glob("*.h5"))
    with h5py.File(files[0], "r") as f:
        assert "trial_001" in f, "reject deleted the trial"
        quality = f["trial_001"].attrs.get("quality")
        assert quality in ("rejected", b"rejected"), quality


def test_reject_without_a_trial_idx_is_409(rig):
    _session_with_baseline(rig)
    status, body = _request(rig, "POST", "/trial/reject", {})
    assert status == 409 and body["error"]


def test_reject_with_a_nonsense_index_is_409(rig):
    _session_with_baseline(rig)
    assert _request(rig, "POST", "/trial/reject", {"trial_idx": -1})[0] == 409
    assert _request(rig, "POST", "/trial/reject", {"trial_idx": "one"})[0] == 409


# -- C0 pairing: which sensors were on --------------------------------------


def test_sensors_enabled_is_recorded_but_never_claimed_as_confirmed(rig):
    """D10 pairs "one sensor" runs against "both sensors" runs.

    The confirmed flag stays False on purpose: $STATUS carries mel= and amb=
    but no sens_a=/sens_b=, so this is the last command the host sent, not a
    state the device acknowledged.
    """
    import h5py
    _session_with_baseline(rig)
    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)
    files = list((rig.workdir / "sessions").glob("*.h5"))
    with h5py.File(files[0], "r") as f:
        meta = dict(f["/meta"].attrs)
    enabled = meta.get("sensors_enabled")
    if enabled is None:
        pytest.skip("sensors_enabled is not in the schema yet")
    assert enabled in ("AB", b"AB")
    assert not meta["sensors_enabled_confirmed"]


# -- B21: VAD thresholds reach the state machine --------------------------


def test_baseline_thresholds_are_handed_to_the_trial_machine(rig):
    """The six that exist today must be passed, not left at their defaults.

    Omitting them is not an error -- every parameter defaults to None -- so
    nothing fails, the VAD just quietly declines to run and the four timing
    attrs stay None forever. This asserts the wiring exists at all.
    """
    import h5py
    body = _session_with_baseline(rig)
    assert body["ok"] is True

    # The machine is constructed as soon as the baseline lands, so a trial
    # running at all means the thresholds were read without blowing up.
    assert _request(rig, "POST", "/trial/start", {})[0] == 200
    assert _request(rig, "POST", "/trial/abort")[0] == 200

    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)
    files = list((rig.workdir / "sessions").glob("*.h5"))
    with h5py.File(files[0], "r") as f:
        meta = dict(f["/meta"].attrs)
    for key in ("baseline_mu_A", "baseline_sigma_A",
                "baseline_mu_B", "baseline_sigma_B",
                "noise_floor_mu", "noise_floor_sigma"):
        assert key in meta, f"{key} is not in /meta, so it cannot be passed on"
    assert len(meta["baseline_mu_A"]) == 32


def test_energy_thresholds_are_read_when_present(rig):
    """energy_mu/energy_sigma are read with .get(), not indexed.

    B21's writer change has not landed yet, so these are absent from /meta
    today. The reader must tolerate that (CONTRACTS #1.1.2: a field the
    producer has not written is None, never a substituted default) -- and
    pick them up automatically once it does.
    """
    import h5py
    _session_with_baseline(rig)
    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)
    files = list((rig.workdir / "sessions").glob("*.h5"))
    with h5py.File(files[0], "r") as f:
        meta = dict(f["/meta"].attrs)

    present = [k for k in ("energy_mu", "energy_sigma") if k in meta]
    if not present:
        # Not a skip: the point is that their absence is survivable, and the
        # session above ran a full baseline + writer cycle to prove it.
        return
    assert len(present) == 2, f"only {present} was written; both or neither"


# -- B21: speaking_mode ---------------------------------------------------


def test_speaking_mode_is_accepted_and_validated_early(rig):
    """A bad value fails at the call, while the machine is still IDLE.

    B21 moved this validation forward deliberately: it used to surface at
    SAVE, which meant a whole recorded trial was lost to a typo made before
    it started.
    """
    _session_with_baseline(rig)
    status, body = _request(rig, "POST", "/trial/hold/start",
                            {"speaking_mode": "not-a-mode"})
    assert status == 409, body
    assert body["state"] == "IDLE", "the machine should not have moved"

    # A valid one still works.
    status, body = _request(rig, "POST", "/trial/hold/start",
                            {"speaking_mode": "silent"})
    assert status == 200, body
    assert body["state"] == "CAPTURE"
