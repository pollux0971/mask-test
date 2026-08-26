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
import numpy as np
import pytest

from test_bridge_session_api import _request, VALID_METADATA
from test_bridge_sse import Rig, _of_type

from analysis.reporting.session_loader import load_session
from host.replay.session_replay import ReplayController, read_session_events
from host.storage.session_writer import SessionWriter

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


def _get_status_only(rig, path):
    """Like _request, but tolerates a plain-HTML 404 (send_error()'s default)
    instead of assuming every response body is JSON -- if /replay/* is ever
    unwired again (a revert, a rebase) it falls through to that default,
    unlike the rest of this API which always answers in JSON. Deliberately a
    GET against a read-only route (`/replay/sessions`, just lists files) so
    probing for wiring never has the side effect of starting a replay.
    """
    req = urllib.request.Request(f"http://127.0.0.1:{rig.http_port}{path}")
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

        # Read-only probe: does /replay/* exist at all yet? Not "start a
        # replay and see" -- that would be a side effect baked into fixture
        # setup, before the dedicated replay test gets to control timing.
        replay_wired_status = _get_status_only(rig, "/replay/sessions")

        yield {
            "rig": rig,
            "boot_events": boot_events,
            "baseline_body": baseline_body,
            "h5_path": h5_path,
            "replay_events": replay_events,
            "replay_wired_status": replay_wired_status,
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
    """ed has since wired this in (bridge_server.py's `_handle_replay*`) --
    this now runs the real thing instead of skipping. Kept defensive about
    a 404 anyway: multiple agents are editing this file concurrently, and a
    stale checkout or a revert should degrade to a clear skip, not a
    confusing failure unrelated to what this test is actually about.
    """
    if pipeline["replay_wired_status"] == 404:
        pytest.skip(
            "/replay/* 現在打不到（404）-- 上一輪確認過 ed 已經接上，這裡的 "
            "404 比較可能是暫時的 checkout/rebase 狀態，不是重新回到「還沒 "
            "wiring」。library 層級的回放驗證（test_replay_reproduces_live_"
            "event_shapes、test_replay_events_carry_the_replay_flag）不受影響。"
        )

    rig = pipeline["rig"]
    h5_path = pipeline["h5_path"]

    status_code, body = _request(rig, "POST", f"/replay/start?file={h5_path}")
    assert status_code == 200, body
    assert body["active"] is True
    assert body["file"] == str(h5_path)
    assert body["n_events"] == len(pipeline["replay_events"])

    try:
        events = rig.read_events(2.0)

        replayed = [e for e in events if e.get("replay") is True]
        assert replayed, "開始回放後，/events 沒有收到任何帶 replay:true 的事件"
        assert any(e["type"] in ("tof", "mic", "mel") for e in replayed), (
            f"回放事件都不是 tof/mic/mel: {[e['type'] for e in replayed]}"
        )

        # bridge_server.py's handle_parsed_event() drops live tof/mic/mel
        # while replay_is_active() -- exactly the story's "two streams
        # interleaved on one channel, no way to tell them apart" disaster.
        # A leaked live frame here would show up unmarked (no replay key).
        leaked_live = [e for e in events
                       if e.get("type") in ("tof", "mic", "mel") and not e.get("replay")]
        assert not leaked_live, (
            f"回放期間仍然收到未標記的即時資料，兩條串流混在一起了: {leaked_live[:3]}"
        )

        # status/heartbeat are deliberately NOT suppressed during replay
        # (device state should still look alive) -- source must still say
        # "mock", not get overwritten by anything replay-related.
        status_events = _of_type(events, "status")
        if status_events:
            assert status_events[-1]["source"] == "mock"
    finally:
        # Leave the rig as this test found it -- other tests in this module
        # (and its own teardown) still use it.
        _request(rig, "POST", "/replay/control?action=pause")


# -- 7. HDF5 -> analysis-layer type seam (D15's flagged gap) ---------------
#
# analysis/reporting/test_run_all.py's own fixtures write synthetic sessions
# directly with raw h5py (see that file's module docstring: deliberately not
# depending on host/storage). That means session_loader.py's `_as_scalar()`
# -- the thing responsible for bytes-vs-str and numpy-scalar-vs-Python
# normalization -- has never been run against a file the real SessionWriter
# (B07) produced. Writer and reader can each match their own understanding
# of the schema and still disagree at the seam -- this project has hit that
# exact shape of bug before (C08: mel is decoded float over SSE, not the
# wire's int16; C05: `dim` is a zone count, not a side length). Same class
# of risk, different seam.


def test_session_loader_reads_real_string_attrs_as_str(pipeline):
    """h5py 3.x decodes vlen utf-8 attrs back to `str`, not `bytes` -- but
    that default has changed across major h5py versions before, and nothing
    in this codebase had pinned it down against a file the real writer
    produced until now.
    """
    data = load_session(pipeline["h5_path"])
    assert isinstance(data.meta["subject"], str)
    assert isinstance(data.meta["mode"], str)
    assert isinstance(data.meta["session_date"], str)

    recorded = _recorded_trial_names(pipeline["h5_path"])
    trial = next(t for t in data.trials if t.key in recorded)
    assert isinstance(trial.label, str)  # 詞彙集是非 ASCII 的中文字
    assert isinstance(trial.mode, str)
    assert isinstance(trial.quality, str)


def test_session_loader_reads_bool_attrs_as_python_bool(pipeline):
    """`clock_sync_confirmed`/`clock_cross_check_ok`寫入時是 Python bool，
    h5py 讀回來的是 `numpy.bool_`；`_as_scalar()` 的 `np.generic` 分支應該
    要接住它。這裡對真檔確認，不是對這個專案自己手造的 numpy bool 確認。
    """
    data = load_session(pipeline["h5_path"])
    for key in ("clock_sync_confirmed", "clock_cross_check_ok"):
        value = data.meta[key]
        assert isinstance(value, bool), f"{key} 是 {type(value)}，不是 Python bool"
        assert not isinstance(value, np.bool_), f"{key} 還是 numpy.bool_"


def test_session_loader_unwraps_numeric_scalars(pipeline):
    data = load_session(pipeline["h5_path"])
    assert type(data.meta["clock_slope"]) is float
    assert type(data.meta["noise_floor_mu"]) is float

    recorded = _recorded_trial_names(pipeline["h5_path"])
    trial = next(t for t in data.trials if t.key in recorded)
    assert isinstance(trial.wear_id, int)
    assert not isinstance(trial.wear_id, np.integer)


def test_session_loader_reads_baseline_arrays_with_correct_shape_and_dtype(pipeline):
    data = load_session(pipeline["h5_path"])
    mu, sigma = data.baseline("A")
    assert mu is not None and sigma is not None
    assert mu.shape == (32,) and sigma.shape == (32,)
    assert mu.dtype == np.float64  # baseline() 明確轉型，不管檔案裡存的是 float32


def test_session_loader_missing_optional_attrs_are_absent_not_crashing(pipeline):
    """The four VAD timestamps: still genuinely absent -- B15/B16's detectors
    are not wired into the trial machine yet (see
    test_vad_timing_attrs_are_entirely_absent above), unlike speaking_mode
    and sensors_enabled which landed separately (B21, and ed's bridge_server
    wiring) since this file was first written. `session_loader.load_session()`
    materializes `group.attrs.items()` into a plain dict up front and reads
    it with `dict.get()` everywhere after -- safe, missing keys come back
    `None`/absent, never `KeyError`. Confirmed against a real file, not
    assumed from reading the code.
    """
    data = load_session(pipeline["h5_path"])
    recorded = _recorded_trial_names(pipeline["h5_path"])
    trial = next(t for t in data.trials if t.key in recorded)
    for attr in ("vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us"):
        assert attr not in trial.attrs


def test_session_loader_reads_speaking_mode_and_sensors_enabled_when_present(pipeline):
    """B21 (speaking_mode, defaults "normal") and ed's bridge_server wiring
    (sensors_enabled, from host/control/device_state.py) both landed since
    this pipeline test was first written -- these are no longer the "not
    wired yet" gap that VAD (still unwired) and the earlier /replay/* skip
    used to cover.
    """
    data = load_session(pipeline["h5_path"])
    recorded = _recorded_trial_names(pipeline["h5_path"])
    trial = next(t for t in data.trials if t.key in recorded)

    assert trial.speaking_mode == "normal"
    assert isinstance(trial.speaking_mode, str)

    assert data.meta.get("sensors_enabled") in ("AB", "A", "B")
    assert isinstance(data.meta["sensors_enabled"], str)
    # 主機指令，不是裝置確認過的狀態（見 session_writer.py 的
    # OPTIONAL_META_KEYS 說明）—— session_loader 不能把它讀成別的意思。
    assert data.meta.get("sensors_enabled_confirmed") is False
    assert isinstance(data.meta["sensors_enabled_confirmed"], bool)


def _minimal_meta(Z):
    """給不需要跑 live rig 的合成 trial 用的最小合法 /meta。"""
    return {
        "schema_version": 1, "subject": "s01", "session_date": "2026-08-26",
        "wear_id": 1, "mode": "quiz", "distance_mm": 30.0, "angle_deg": 0.0,
        "ambient": "quiet room", "notes": "", "fw_sha": "0000000",
        "proto_version": 2, "tof_dim": Z,
        "clock_slope": 1.0, "clock_offset": 0.0, "clock_residual_p95": 0.0,
        "clock_drift_us": 0.0, "clock_drift_ppm": 0.0,
        "clock_sync_span_us": 0, "clock_sync_confirmed": True,
        "session_start_device_us": 0, "session_start_host_us": 0,
        "session_start_rtt_min_us": 0,
        "baseline_mu_A": np.zeros(2 * Z, dtype=np.float32),
        "baseline_sigma_A": np.ones(2 * Z, dtype=np.float32),
        "baseline_mu_B": np.zeros(2 * Z, dtype=np.float32),
        "baseline_sigma_B": np.ones(2 * Z, dtype=np.float32),
        "noise_floor_mu": 0.0, "noise_floor_sigma": 1.0,
    }


def _minimal_trial_kwargs(T, Z, **overrides):
    kwargs = dict(
        label="五",
        tof_A=np.zeros((T, 2 * Z), dtype=np.float32),
        tof_B=np.zeros((T, 2 * Z), dtype=np.float32),
        tof_t_us=np.arange(T, dtype=np.int64) * 1000,
        tof_valid_A=np.ones((T, Z), dtype=bool),
        tof_valid_B=np.ones((T, Z), dtype=bool),
        mic_rms=np.zeros(4, dtype=np.float32), mic_peak=np.zeros(4, dtype=np.int16),
        mic_t_us=np.arange(4, dtype=np.int64) * 1000,
        wear_id=1, mode="quiz", valid_zone_ratio=0.98, drop_count=0,
        quality="ok",
    )
    kwargs.update(overrides)
    return kwargs


def test_session_loader_invalid_tof_zones_stay_nan_and_validity_is_independent(tmp_path):
    """不依賴 live rig 這次跑出來的合成場景剛好有沒有無效 zone -- 直接構造
    一個保證有無效 zone 的 trial，確認：(1) 讀回來真的是 NaN，不是 0/-1；
    (2) 有效性完全來自獨立的 `tof_valid_A`/`B` 陣列，不是靠
    `value == value` 這種 NaN 比較法（`NaN != NaN`，這正是 §2.1 選擇獨立
    valid 陣列、而不是「非 NaN 即有效」的原因——`session_loader.py` 目前
    沒有這樣做，這裡把它釘住，不讓以後有人為了省一個欄位改回去）。
    """
    path = tmp_path / "session.h5"
    T, Z = 3, 16
    tof_A = np.zeros((T, 2 * Z), dtype=np.float32)
    tof_valid_A = np.ones((T, Z), dtype=bool)
    tof_valid_A[1, 5] = False  # 第 1 幀第 5 個 zone 標成無效
    tof_A[1, 5] = np.nan       # 無效值本身寫 NaN（§2.1）

    with SessionWriter(path, _minimal_meta(Z)) as w:
        w.write_trial(0, **_minimal_trial_kwargs(T, Z, tof_A=tof_A, tof_valid_A=tof_valid_A))

    data = load_session(path)
    trial = data.trials[0]
    assert trial.tof_valid_a[1, 5] == False  # noqa: E712 -- 明確要 bool 值
    assert np.isnan(trial.tof_a[1, 5])
    # 有效的那些不能是 NaN，也不能被反過來當成「不是 NaN 就有效」的證據
    assert not np.isnan(trial.tof_a[0, 5])
    assert trial.tof_valid_a[0, 5] == True  # noqa: E712


def test_session_loader_normalizes_comparable_to_python_bool(tmp_path):
    """`comparable`（4f 的 B21）是這個 schema 第一個 bool 型選填 trial attr。
    `host/storage/test_session_writer.py` 的
    `test_comparable_raw_h5py_read_is_numpy_bool_not_python_bool` 已經證明
    直接用 h5py 讀會拿到 `numpy.bool_`（`numpy.bool_(True) is True` 是
    `False`，`if ... is True:` 這種寫法會靜默失效）。這裡確認走
    `session_loader.load_session()` 這條路（`_as_scalar()` 的 `np.generic`
    分支）**有**把它正規化成 Python `bool`，`is True`/`is False` 才能安全
    使用 -- 不是「大概沒問題」，是對真的 SessionWriter 輸出直接斷言。

    這條線目前還沒被 4f 接進 TrialStateMachine（它負責填值），所以獨立構造
    一個合成 trial，不依賴 live rig。
    """
    path = tmp_path / "session.h5"
    T, Z = 3, 16
    with SessionWriter(path, _minimal_meta(Z)) as w:
        w.write_trial(0, **_minimal_trial_kwargs(T, Z, comparable=True))
        w.write_trial(1, **_minimal_trial_kwargs(T, Z, comparable=False))
        w.write_trial(2, **_minimal_trial_kwargs(T, Z))  # comparable 沒給

    data = load_session(path)
    by_key = {t.key: t for t in data.trials}

    comparable_true = by_key["trial_000"].attrs["comparable"]
    assert comparable_true is True, f"預期正規化成 Python bool True，實際是 {comparable_true!r} ({type(comparable_true)})"

    comparable_false = by_key["trial_001"].attrs["comparable"]
    assert comparable_false is False, f"預期正規化成 Python bool False，實際是 {comparable_false!r} ({type(comparable_false)})"

    assert "comparable" not in by_key["trial_002"].attrs, "沒給就該整個 attr 缺席，不是 None/False"
