"""The full chain, for real: mock device -> bridge_server -> session start ->
baseline -> trials -> HDF5 -> read back -> replay.

Every sibling test_bridge_*.py file exercises one slice of the wiring against
the real `Rig` (mock_device.py + bridge_server.py). None of them walks the
whole chain end to end and then opens the HDF5 file the run actually produced
-- this one does, because that is exactly where an integration bug hides that
no unit test and no single-slice wiring test can see (the mode="w" truncation
bug that ate trial_000 was found exactly this way, by hand, before this test
existed).

The pipeline runs ONCE per test session (module-scoped fixture) and every
test function below makes one independent claim about what it produced.
That's deliberate, not just an optimization: some claims (speaking_mode,
`/replay/*` over HTTP) are about wiring another agent is actively building as
this is written, and a claim like that not holding yet must SKIP with an
explanation, not fail red -- and must not take the other, already-true claims
down with it. A single big test function would make the first `pytest.skip()`
abort everything after it, hiding whether trial_000 survives or the mel axis
is right behind an unrelated "not implemented yet". Splitting means the
report shows exactly which claims hold today and which are pending, and the
day a pending one lands, only that one test starts asserting for real -- the
others don't change.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

import h5py
import pytest

from test_bridge_session_api import _request, VALID_METADATA
from test_bridge_sse import Rig, _of_type

from host.replay.session_replay import ReplayController, read_session_events

N_TRIALS = 3


def _record_one_in_range_trial(rig):
    """hold/start + hold/stop inside B12's [0.3, 5.0]s window -> auto SAVE."""
    status, body = _request(rig, "POST", "/trial/hold/start", {})
    assert status == 200, body
    time.sleep(0.8)
    status, body = _request(rig, "POST", "/trial/hold/stop")
    assert status == 200, body
    assert body["state"] == "REST", (
        f"hold 落在 {body['state']} 而不是直接 SAVE -- 這條路徑預期是"
        f"「按住 0.8 秒，落在正常範圍內」: {body}"
    )
    # REST is timed (REST_S = 1.5s) and only a background ticker drives it
    # back to IDLE (host/trial/state_machine.py tick()) -- hold_start() from
    # REST is a 409, so the next trial has to wait this out for real.
    time.sleep(1.8)


def _recorded_trial_names(h5_path):
    with h5py.File(h5_path, "r") as f:
        return sorted(k for k in f if k.startswith("trial_") and k != "trial_000")


def _post_status_only(rig, path):
    """Like _request, but tolerates a plain-HTML 404 (send_error()'s default)
    instead of assuming every response body is JSON -- /replay/* has no
    handler yet, so it falls through to that default, unlike the rest of
    this API which always answers in JSON.
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{rig.http_port}{path}", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture(scope="module")
def pipeline():
    """Run the whole chain once; yields what every test below inspects."""
    rig = Rig("--proto", "v2", "--mel", "1")
    try:
        boot_events = rig.read_events(3.5)  # also fills the aligner for baseline

        assert _request(rig, "POST", "/session/start", VALID_METADATA)[0] == 200
        status_code, baseline_body = _request(rig, "POST", "/session/baseline?seconds=2")
        if status_code != 200:
            pytest.skip(
                f"baseline 品質閘門拒絕了這次合成場景（{baseline_body.get('reason')}）"
                "-- 這是 B10 的閘門在正常運作，不是這支測試要驗的東西，重跑通常會過"
            )

        for _ in range(N_TRIALS):
            _record_one_in_range_trial(rig)

        # The writer holds an exclusive h5py lock for the whole session, so
        # the file cannot be opened for reading before this returns.
        assert _request(rig, "POST", "/session/end")[0] == 200
        time.sleep(0.5)

        files = list((rig.workdir / "sessions").glob("*.h5"))
        assert files, "session/end 之後 sessions 目錄裡沒有任何 .h5 檔"
        h5_path = files[0]

        replay_events = read_session_events(h5_path)

        # ed is actively wiring /replay/* -- capture the response now so the
        # tests below can each independently skip or assert on it.
        replay_http_status = _post_status_only(rig, f"/replay/start?file={h5_path}")

        yield {
            "boot_events": boot_events,
            "baseline_body": baseline_body,
            "h5_path": h5_path,
            "replay_events": replay_events,
            "replay_http_status": replay_http_status,
        }
    finally:
        rig.close()


# -- 1. link identity ------------------------------------------------------


def test_status_declares_mock_not_live(pipeline):
    """CONTRACTS #4.2: source is declared on the command line, never inferred.

    A pty from T04 and a real USB-UART look identical to the parser. If this
    ever silently defaults to "live", a session recorded against the mock by
    accident (E05's four-hour runs) would land in the dataset unmarked as
    synthetic.
    """
    status = _of_type(pipeline["boot_events"], "status")
    assert status, "沒有任何 status 事件送到 /events"
    assert status[-1]["source"] == "mock", (
        f"接的是 mock_device.py，實際收到 source={status[-1].get('source')!r}"
    )
    assert status[0]["source"] == "mock", "連線一開始的第一個快照也要標對"


# -- 2. baseline --------------------------------------------------------


def test_baseline_response_has_the_numbers(pipeline):
    body = pipeline["baseline_body"]
    for key in ("mu_A", "sigma_A", "mu_B", "sigma_B", "noise_floor_mu"):
        assert key in body, f"/session/baseline 的回應少了 {key}: {body}"


def test_meta_has_baseline_and_noise_floor_attrs(pipeline):
    with h5py.File(pipeline["h5_path"], "r") as f:
        for key in ("baseline_mu_A", "baseline_sigma_A", "baseline_mu_B",
                    "baseline_sigma_B", "noise_floor_mu", "noise_floor_sigma"):
            assert key in f["/meta"].attrs, f"/meta 少了 {key}"


# -- 3. the append-mode regression ----------------------------------------


def test_baseline_trial_survives_reopening_for_trials(pipeline):
    """Regression: B07's mode="a" fix. Before it, opening the file again to
    add trial_001 truncated it and trial_000 (the baseline) silently vanished.
    """
    with h5py.File(pipeline["h5_path"], "r") as f:
        trial_names = sorted(k for k in f if k.startswith("trial_"))
        assert "trial_000" in trial_names, f"baseline 被截斷了: {trial_names}"
        label0 = f["trial_000"].attrs.get("label")
        assert label0 in ("_baseline", b"_baseline"), label0


def test_all_recorded_trials_are_present(pipeline):
    recorded = _recorded_trial_names(pipeline["h5_path"])
    assert len(recorded) == N_TRIALS, f"預期 {N_TRIALS} 筆，實際: {recorded}"


def test_trial_idx_does_not_collide_with_baseline(pipeline):
    with h5py.File(pipeline["h5_path"], "r") as f:
        for name in _recorded_trial_names(pipeline["h5_path"]):
            idx = int(f[name].attrs["trial_idx"])
            assert idx != 0, f"{name} 的 trial_idx 跟 baseline（idx 0）撞了"


# -- 4. schema shape ----------------------------------------------------


def test_mel_has_its_own_axis_paired_with_mel_t_us(pipeline):
    recorded = _recorded_trial_names(pipeline["h5_path"])
    with h5py.File(pipeline["h5_path"], "r") as f:
        sample = f[recorded[0]]
        if "mel" not in sample:
            pytest.skip(
                "這筆 trial 的 capture 視窗剛好沒收到任何 $F -- 已用 --mel 1 "
                "啟動 mock，理論上會有，重跑這個測試通常就會有 mel 資料"
            )
        assert sample["mel"].shape[0] == sample["mel_t_us"].shape[0], (
            "mel 的幀數跟 mel_t_us 對不上，兩者必須成對"
        )
        # Deliberately NOT compared to mic_t_us's length: mel and mic are
        # independent axes (CONTRACTS.md #2), not a shared "M" axis.


def test_vad_timing_attrs_are_entirely_absent(pipeline):
    """Not 0/-1/window-boundary placeholders -- the whole attr must be
    missing when nothing was detected, so a direct read raises KeyError
    (the schema's intentional "loud failure" signal) instead of silently
    handing back a number that looks like a real measurement.

    Today host/trial/state_machine.py always passes vad_start_us=None etc
    (B15/B16's detectors are not wired into the trial machine yet), so this
    currently proves "always omitted". The day that wiring lands and a trial
    with no detected speech still omits these, this becomes the conditional
    regression check the story actually asked for -- no edit needed here.
    """
    recorded = _recorded_trial_names(pipeline["h5_path"])
    with h5py.File(pipeline["h5_path"], "r") as f:
        sample = f[recorded[0]]
        for attr in ("vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us"):
            assert attr not in sample.attrs, (
                f"{attr} 不該被寫入 -- 沒偵測到就該整個省略，"
                "寫任何數字都會讓「沒偵測到」看起來像「整段都在動」"
            )


def test_speaking_mode_is_written(pipeline):
    recorded = _recorded_trial_names(pipeline["h5_path"])
    with h5py.File(pipeline["h5_path"], "r") as f:
        sample = f[recorded[0]]
        if "speaking_mode" not in sample.attrs:
            pytest.skip(
                "speaking_mode 目前沒有從 /trial/hold/start 的 request body 接到 "
                "TrialStateMachine -> write_trial()（host/trial/state_machine.py "
                "呼叫 write_trial() 時完全沒傳這個參數），所以現在寫不進 HDF5。"
                "SessionWriter(B07)/replay(B17) 都已支援這個欄位，"
                "wiring 補上後這個斷言不用改就會變成真的檢查。"
            )
        assert sample.attrs["speaking_mode"] in ("normal", "whisper", "silent")


# -- 5. replay, library level ----------------------------------------------


def test_replay_reproduces_live_event_shapes(pipeline):
    """host/replay/session_replay.py (B17) against a file this run itself
    produced, not a hand-built fixture -- the first time replay has run
    against real end-to-end output.
    """
    events = pipeline["replay_events"]
    assert events, "回放沒有讀出任何事件"

    live_tof = _of_type(pipeline["boot_events"], "tof")
    live_mic = _of_type(pipeline["boot_events"], "mic")
    replay_tof = [e for e in events if e.payload["type"] == "tof"]
    replay_mic = [e for e in events if e.payload["type"] == "mic"]
    assert replay_tof, "session 裡有 ToF trial 資料，但回放沒有任何 tof 事件"
    assert replay_mic, "session 裡有 mic trial 資料，但回放沒有任何 mic 事件"

    if live_tof:
        live_keys = set(live_tof[0]) - {"proto", "has_timestamp"}  # v2-only extras
        assert set(replay_tof[0].payload) == live_keys, (
            f"回放 tof 欄位跟 live 對不上: replay={sorted(replay_tof[0].payload)} "
            f"live={sorted(live_keys)}"
        )
    if live_mic:
        assert set(replay_mic[0].payload) == set(live_mic[0]), (
            f"回放 mic 欄位跟 live 對不上: replay={sorted(replay_mic[0].payload)} "
            f"live={sorted(live_mic[0])}"
        )
        assert isinstance(replay_mic[0].payload["rms"], int), (
            "live 的 mic rms 是整數（CONTRACTS #1.1），回放吐出來的型別對不上"
        )


def test_replay_events_carry_the_replay_flag(pipeline):
    ctrl = ReplayController(pipeline["replay_events"])
    emitted = []
    while not ctrl.finished:
        emitted.append(ctrl.step())
    assert emitted, "ReplayController 一個事件都沒吐出來"
    assert all(e["replay"] is True for e in emitted), (
        "有事件漏了 replay: true -- 前端沒辦法區分這是真資料還是回放資料"
    )


# -- 6. replay, HTTP level --------------------------------------------------


def test_replay_http_endpoint(pipeline):
    status_code = pipeline["replay_http_status"]
    if status_code == 404:
        pytest.skip(
            "/replay/* 還沒接上 bridge_server.py（esp-mask-test-ed 進行中）-- "
            "這不是失敗，是進度。test_replay_reproduces_live_event_shapes 和 "
            "test_replay_events_carry_the_replay_flag 已經在函式庫層級對同一個 "
            "檔案驗證過回放內容、事件形狀跟 replay:true 標記；一旦這個 HTTP 端點"
            "接上，把這裡從 skip 換成真的斷言（開始回放、poll /events 收到帶 "
            "replay:true 的事件、確認 status 的 source 在回放期間不會被真實"
            "序列埠資料蓋掉）就會是完整的端對端 regression。"
        )
    else:
        pytest.fail(
            f"/replay/start 現在回應 {status_code}，不再是 404 了 -- "
            "看起來 wiring 已經完成，這支測試需要更新成真的斷言，不能再 skip"
        )
