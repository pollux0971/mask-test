"""B21 -- 端到端測試：從 baseline 算 energy_mu/sigma 一路到 HDF5 的 VAD attrs。

這條鏈有六個接縫：

    evaluate_baseline() 算 energy_mu/sigma      -- host/storage/test_baseline.py 測了
      -> capture_baseline_trial() 塞進 meta      -- host/storage/test_baseline.py 測了
      -> SessionWriter 落盤到 /meta              -- session_writer 自己的測試測了
      -> bridge 從 /meta 讀出來傳進建構子         -- ⚠️ 見下方說明，esp-mask-test-ed 還沒做
      -> TrialStateMachine 傳給 detect_lips()    -- host/trial/test_state_machine.py 白箱測了
      -> 偵測結果寫回 trial attrs                -- host/trial/test_state_machine.py 測了

每一環各自都有測試，但這個檔案是唯一一個真的跨越全部、用真實 HDF5 檔案
（不是 mock）走一遍的。這個專案反覆出現的失敗型態就是「每層各自正確，
接縫上錯」（`recognition_service` 用舊校準、`time.monotonic` vs
`time.time`、`MEL:` 指令根本不存在、`.get("complementary")` 拿到
`None`）——這條鏈有六個接縫，值得專門測一次全程。

⚠️ **第 4 環尚未在正式環境接通**：`bridge_server.py` 的 `open_trial_machine()`
（`esp-mask-test-ed` 負責的 `/trial/*` wiring）目前還沒有把 session 的
`/meta`（`baseline_mu_*`/`noise_floor_*`/`energy_mu`/`energy_sigma`）讀出來
傳進 `TrialStateMachine` 的建構子。下面的 `_record_one_trial()` 是**手動**
用 `session_loader.load_session()` 讀出 `/meta` 再自己組出建構子參數，
繞過那個缺口，好讓其餘每一個接縫都能被這個測試驗證到——**不代表這條鏈
在正式環境裡已經全部自動接通了**，那一環要等 `ed` 的 `/trial/*` wiring
接上 B21 的新建構子參數才算數。

一律用 `analysis.reporting.session_loader.load_session()` 讀 HDF5，不直接
開 h5py——`session_loader._as_scalar()` 已經把 `numpy.bool_`/bytes 之類的
HDF5 型別正規化成 Python 原生型別，那個陷阱（`numpy.bool_(True) is True`
是 `False`）`session_writer` 自己的測試已經測過，這裡不重複測。
"""
import numpy as np
import pytest

from analysis.reporting.session_loader import load_session
from host.align.aligner import Aligner
from host.storage.baseline import capture_baseline_trial
from host.storage.session_writer import SessionWriter
from host.storage.test_baseline import _stable_tof
from host.storage.test_session_writer import _sample_meta
from host.trial.state_machine import TrialStateMachine
from host.vad.test_audio_vad import NOISE_MU, NOISE_SIGMA, synth_recording
from host.vad.test_tof_vad import N_ZONES, synth_tof


def _tof_events(tof, t_us, sensor):
    events = []
    for row, ts in zip(tof, t_us):
        events.append({
            "type": "tof", "sensor": sensor, "seq": len(events), "t_us": int(ts),
            "dim": N_ZONES, "distance": list(row[:N_ZONES]), "signal": list(row[N_ZONES:]),
            "valid": [True] * N_ZONES,
        })
    return events


def _mic_events(rms, t_us):
    return [{"type": "mic", "seq": i, "t_us": int(ts), "rms": float(r), "peak": 0.0}
            for i, (r, ts) in enumerate(zip(rms, t_us))]


def _make_baseline_session(session_dir):
    """步驟 1：真的跑一次 capture_baseline_trial()，回傳寫好的 .h5 路徑。"""
    rng = np.random.default_rng(100)
    tof_A, valid_A = _stable_tof(rng=rng)
    tof_B, valid_B = _stable_tof(rng=rng)
    mic_rms = rng.normal(loc=300, scale=10, size=930).astype(np.float32)
    mic_peak = (mic_rms * 1.5).astype(np.int16)
    mic_t_us = np.arange(930, dtype=np.int64) * 32_258
    tof_t_us = np.arange(tof_A.shape[0], dtype=np.int64) * 33_333

    meta_base = _sample_meta()
    for k in ("baseline_mu_A", "baseline_sigma_A", "baseline_mu_B",
              "baseline_sigma_B", "noise_floor_mu", "noise_floor_sigma"):
        del meta_base[k]  # capture_baseline_trial 負責算出並填入這幾個

    path = session_dir / "session.h5"
    outcome = capture_baseline_trial(
        path, meta_base,
        tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
        tof_valid_A=valid_A, tof_valid_B=valid_B,
        mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
        wear_id=1, mode="quiz",
    )
    assert outcome.ok, f"baseline fixture 不穩定，測試資料需要調整: {outcome.reason}"
    return path


def test_energy_floor_reaches_meta_after_baseline_capture(tmp_path):
    """步驟 2：/meta 裡真的讀得到 energy_mu/energy_sigma——18 把
    session_writer.py 那半接上之後，這條才能真的斷言（之前只能斷言
    capture_baseline_trial() 回傳的 outcome，見 test_baseline.py）。"""
    path = _make_baseline_session(tmp_path)
    session = load_session(path)
    assert session.meta.get("energy_mu") is not None
    assert session.meta.get("energy_sigma") is not None
    assert np.isfinite(session.meta["energy_mu"])
    assert np.isfinite(session.meta["energy_sigma"])


def _record_one_trial(tmp_path, name, *, pass_energy_floor):
    """步驟 3-5：從 baseline 的 /meta 讀出來的值建構 TrialStateMachine
    （見檔案頂端關於第 4 環尚未接通的說明），錄一筆有唇動的合成 trial，
    重新開檔讀回整個 session。跟正式流程一樣重開**同一個** session 檔
    （`mode="a"`），trial 從 `first_trial_idx=1` 開始（trial_000 是
    baseline）。
    """
    session_dir = tmp_path / name
    session_dir.mkdir()
    session_path = _make_baseline_session(session_dir)
    baseline_session = load_session(session_path)
    baseline_mu_A, baseline_sigma_A = baseline_session.baseline("A")

    manifest_path = session_dir / "manifest.csv"
    writer = SessionWriter(session_path, mode="a")
    writer.__enter__()
    aligner = Aligner()

    kwargs = dict(
        baseline_mu_A=np.asarray(baseline_mu_A), baseline_sigma_A=np.asarray(baseline_sigma_A),
        noise_floor_mu=baseline_session.meta["noise_floor_mu"],
        noise_floor_sigma=baseline_session.meta["noise_floor_sigma"],
        first_trial_idx=1,
    )
    if pass_energy_floor:
        kwargs["energy_mu"] = baseline_session.meta["energy_mu"]
        kwargs["energy_sigma"] = baseline_session.meta["energy_sigma"]

    sm = TrialStateMachine(
        ["ba"], aligner, writer, session_path, manifest_path,
        wear_id=1, mode="quiz", seed=1,
        **kwargs,
    )

    tof, tof_t, _, _ = synth_tof(np.random.RandomState(200))
    rms, mic_t, _, _ = synth_recording(np.random.RandomState(200), mu=NOISE_MU, sigma=NOISE_SIGMA)
    for e in _tof_events(tof, tof_t, "A"):
        sm.push_event(e)
    for e in _mic_events(rms, mic_t):
        sm.push_event(e)

    hold_start_t_us = tof_t[0] + 1_500_000
    hold_stop_t_us = hold_start_t_us + 1_200_000
    sm.hold_start(device_t_us=hold_start_t_us, speaking_mode="normal")
    sm.hold_stop(device_t_us=hold_stop_t_us)
    writer.__exit__(None, None, None)

    return load_session(session_path)


def _recorded_trial(session):
    matches = [t for t in session.trials if t.key == "trial_001"]
    assert len(matches) == 1, "應該有且只有一筆 trial_001（trial_000 是 baseline）"
    return matches[0]


def test_full_chain_baseline_to_hdf5_vad_attrs(tmp_path):
    """步驟 3-5：完整跑一次，四個 VAD 欄位跟 comparable 都應該是真值——
    不是整個 attr 缺席（那代表偵測沒跑），也不是隨便填的佔位值。"""
    session = _record_one_trial(tmp_path, "with_energy", pass_energy_floor=True)
    trial = _recorded_trial(session)
    for key in ("vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us", "comparable"):
        assert key in trial.attrs, f"{key} 應該是真值，不該整個 attr 缺席"


def test_energy_floor_from_baseline_changes_detection_vs_self_estimate(tmp_path):
    """步驟 6（關鍵）：建構時給不給 energy_mu/energy_sigma，偵測結果要不同
    ——前五步只證明「有東西流過去」，這一步才證明「流過去的東西真的被
    用了」，不是傳了但被忽略。

    兩次呼叫用同一個 rng seed（100）產生 baseline、同一個 rng seed（200）
    產生 trial 資料，唯一的差別就是要不要把 baseline 算好的
    energy_mu/energy_sigma 傳進建構子——baseline 期間幾乎無雜訊（見
    `_stable_tof` 的 noise=0.02mm），trial 資料本身雜訊大得多
    （`synth_tof` 的 `BASE_SIGMA_MM=1.2`），兩者的能量門檻估計本來就該
    明顯不同。
    """
    with_energy = _record_one_trial(tmp_path, "with_energy", pass_energy_floor=True)
    without_energy = _record_one_trial(tmp_path, "without_energy", pass_energy_floor=False)

    t_with = _recorded_trial(with_energy).attrs
    t_without = _recorded_trial(without_energy).attrs

    assert t_with.get("lip_onset_us") != t_without.get("lip_onset_us"), (
        "傳 baseline 算好的 energy_mu/sigma 跟讓 detect_lip_activity() 自己從 "
        "trial 資料估，偵測結果應該不同——如果一樣，代表這兩個值傳進 "
        "TrialStateMachine 之後根本沒被用上"
    )
