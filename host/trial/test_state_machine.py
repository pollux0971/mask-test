"""B11 -- trial 狀態機測試。全部用假時鐘（不 sleep）+ 真正的 `Aligner`／
`SessionWriter`（host 端純 Python，不需要硬體，符合驗收條件「單元測試用
假時鐘跑完整流程，不需硬體」）。不用 `tools/mock_device.py`：那支是模擬
序列埠位元組，B11 完全不碰 `bridge_server.py`／序列埠層，直接呼叫
`push_event()`/`push_mic()` 灌事件字典，測的東西完全一樣，只是少了序列埠
往返這一層不需要的間接。
"""
import tempfile
from pathlib import Path

import h5py
import numpy as np
import pytest

from host.align.aligner import Aligner
from host.storage.manifest import read_manifest
from host.storage.session_writer import SessionWriter
from host.storage.test_session_writer import _sample_meta
from host.trial.state_machine import (
    CAPTURE_RATE_HZ,
    CAPTURE_S,
    COUNTDOWN_S,
    PROMPT_S,
    REST_S,
    TrialState,
    TrialStateMachine,
    classify_quality,
)


def _full_sample_meta(**overrides):
    """`session_writer.py` 的 `REQUIRED_META_KEYS` 這幾天陸續被 B04/B05 加了
    校時欄位，但 `test_session_writer.py` 的 `_sample_meta()`（B07 自己的
    測試 fixture，不是我的檔案）還沒跟上——連它自己的測試現在也會因為缺這些
    欄位而失敗，不是我這輪造成的。這裡在**我自己的測試檔案裡**局部補上這些
    新欄位，不去動 `test_session_writer.py`。"""
    meta = _sample_meta(**overrides)
    meta.setdefault("clock_drift_us", 12.0)
    meta.setdefault("clock_drift_ppm", 23.4)  # 跟 _sample_meta 的 clock_slope=1.0000234 對得上
    meta.setdefault("clock_sync_span_us", 5_000_000)
    meta.setdefault("clock_sync_confirmed", True)
    meta.setdefault("session_start_device_us", 0)
    meta.setdefault("session_start_host_us", 0.0)
    meta.setdefault("session_start_rtt_min_us", 800.0)
    return meta


class FakeClock:
    """注入用的假時鐘：`advance()` 直接跳時間，不用真的 sleep。"""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt
        return self.now


def _feed_tof(aligner, sensor, t_start_us, t_end_us, rate_hz=30.0, dim=16):
    period_us = int(1e6 / rate_hz)
    t = t_start_us
    while t <= t_end_us:
        aligner.push_tof(sensor, t, distance=[100.0] * dim, signal=[10.0] * dim, valid=[True] * dim)
        t += period_us


def _feed_mic(aligner_or_sm, t_start_us, t_end_us, rate_hz=31.25):
    period_us = int(1e6 / rate_hz)
    t = t_start_us
    while t <= t_end_us:
        if hasattr(aligner_or_sm, "push_mic"):
            aligner_or_sm.push_mic(t, rms=100.0, peak=1000.0)
        t += period_us


def _feed_mel(sm, t_start_us, t_end_us, rate_hz=62.5, n_bands=40):
    period_us = int(1e6 / rate_hz)
    t = t_start_us
    while t <= t_end_us:
        sm.push_mel(t, [0.0] * n_bands)
        t += period_us


def _make_sm(tmp_path, *, words=("五", "四", "八"), seed=1, clock=None, wear_id=3, mode="quiz",
             baseline_mu_A=None, baseline_sigma_A=None, baseline_mu_B=None, baseline_sigma_B=None,
             noise_floor_mu=None, noise_floor_sigma=None, energy_mu=None, energy_sigma=None):
    """每次呼叫都在 `tmp_path` 底下開一個獨立子目錄放 session/manifest，
    這樣同一個測試裡呼叫兩次（例如比較兩個不同 seed 的 order）不會撞同一個
    還開著的 HDF5 檔案。`clock` 沒給就用一個新的 `FakeClock()`——**呼叫端如果
    要自己控制時間推進，必須把同一個 clock 物件傳進來**，不然狀態機讀到的
    跟測試裡 `.advance()` 的是兩個不相干的時鐘，`tick()` 永遠看不到時間變化。

    `baseline_*`/`noise_floor_*`（B21）預設都是 `None`——絕大多數測試不關心
    VAD，維持原本「沒有 baseline 就是 applicable=False」的行為，不用每個
    既有測試都被迫多傳幾個參數。
    """
    session_dir = Path(tempfile.mkdtemp(dir=tmp_path))
    h5_path = session_dir / "session.h5"
    manifest_path = session_dir / "manifest.csv"
    writer = SessionWriter(h5_path, _full_sample_meta(wear_id=wear_id, mode=mode))
    writer.__enter__()
    aligner = Aligner()
    sm = TrialStateMachine(
        words, aligner, writer, h5_path, manifest_path,
        wear_id=wear_id, mode=mode, seed=seed,
        clock=clock or FakeClock(), manifest_root=session_dir,
        baseline_mu_A=baseline_mu_A, baseline_sigma_A=baseline_sigma_A,
        baseline_mu_B=baseline_mu_B, baseline_sigma_B=baseline_sigma_B,
        noise_floor_mu=noise_floor_mu, noise_floor_sigma=noise_floor_sigma,
        energy_mu=energy_mu, energy_sigma=energy_sigma,
    )
    return sm, writer, aligner, h5_path, manifest_path


# ---------------------------------------------------------------------------
# 完整流程


def test_full_flow_with_fake_clock_no_sleep(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    device_t0 = 10_000_000  # 裝置端任意起始時間，跟主機時鐘無關
    countdown_end_device_t = device_t0 + int((PROMPT_S) * 1e6)  # 假設裝置時間跟節奏一致，方便算
    capture_start_t_us = countdown_end_device_t + int(COUNTDOWN_S * 1e6)
    capture_end_t_us = capture_start_t_us + int(CAPTURE_S * 1e6)

    # 事先把 CAPTURE 視窗會用到的 ToF/mic 資料灌進 Aligner，跟真實情境一樣
    # （即時資料是邊錄邊灌，這裡因為是假時鐘快轉，一次灌完效果相同）。
    _feed_tof(aligner, "A", capture_start_t_us - 200_000, capture_end_t_us + 200_000)
    _feed_tof(aligner, "B", capture_start_t_us - 200_000, capture_end_t_us + 200_000)
    _feed_mic(sm, capture_start_t_us - 200_000, capture_end_t_us + 200_000)

    ev = sm.start_trial()
    assert ev["state"] == "PROMPT"
    assert sm.state == TrialState.PROMPT

    # 還沒到期，tick 不應該轉換
    clock.advance(PROMPT_S / 2)
    assert sm.tick() == []
    assert sm.state == TrialState.PROMPT

    clock.advance(PROMPT_S / 2 + 0.001)
    events = sm.tick()
    assert len(events) == 1 and events[0]["state"] == "COUNTDOWN"
    assert sm.state == TrialState.COUNTDOWN

    clock.advance(COUNTDOWN_S + 0.001)
    events = sm.tick(device_t_us=capture_start_t_us)
    assert len(events) == 1 and events[0]["state"] == "CAPTURE"
    assert sm.state == TrialState.CAPTURE

    clock.advance(CAPTURE_S + 0.001)
    events = sm.tick(device_t_us=capture_end_t_us)
    assert [e["state"] for e in events] == ["SAVE", "REST"]
    save_event = events[0]
    assert save_event["quality"] in ("ok", "low", "rejected")
    assert sm.state == TrialState.REST

    clock.advance(REST_S + 0.001)
    events = sm.tick()
    assert len(events) == 1 and events[0]["state"] == "IDLE"
    assert sm.state == TrialState.IDLE

    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        assert "trial_000" in f
        n_frames = f["trial_000"]["tof_A"].shape[0]

    # 驗收條件：ToF 幀數 = 期望值 ± 2（2s x 30Hz = 60）
    assert abs(n_frames - 60) <= 2

    df = read_manifest(manifest_path)
    assert len(df) == 1
    assert df.iloc[0]["trial_idx"] == 0
    assert df.iloc[0]["n_frames"] == n_frames


def test_capture_frame_count_within_tolerance_across_multiple_trials(tmp_path):
    """同一個 story 的核心驗收條件，多跑幾次確認不是碰運氣。"""
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(
        tmp_path, words=("五", "四", "八", "一"), seed=7, clock=clock
    )

    device_t = 0
    for _ in range(3):
        capture_start = device_t + 5_000_000
        capture_end = capture_start + int(CAPTURE_S * 1e6)
        _feed_tof(aligner, "A", capture_start - 100_000, capture_end + 100_000)
        _feed_tof(aligner, "B", capture_start - 100_000, capture_end + 100_000)
        _feed_mic(sm, capture_start - 100_000, capture_end + 100_000)

        sm.start_trial()
        clock.advance(PROMPT_S + 0.001)
        sm.tick()
        clock.advance(COUNTDOWN_S + 0.001)
        sm.tick(device_t_us=capture_start)
        clock.advance(CAPTURE_S + 0.001)
        sm.tick(device_t_us=capture_end)
        clock.advance(REST_S + 0.001)
        sm.tick()

        device_t = capture_end + 10_000_000

    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        for i in range(3):
            n = f[f"trial_{i:03d}"]["tof_A"].shape[0]
            assert abs(n - 60) <= 2, f"trial {i}: {n} frames"


# ---------------------------------------------------------------------------
# abort / redo


def test_abort_leaves_no_hdf5_or_manifest_row(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)

    sm.start_trial()
    clock.advance(PROMPT_S + 0.001)
    sm.tick()  # -> COUNTDOWN
    event = sm.abort()
    assert event["state"] == "IDLE"
    assert sm.state == TrialState.IDLE

    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        trial_groups = [k for k in f.keys() if k.startswith("trial_")]
        assert trial_groups == [], "abort 之後不應該有任何 trial group 被寫入"

    df = read_manifest(manifest_path)
    assert len(df) == 0, "abort 之後 manifest 應該完全沒有這筆（manifest 只在 add_session 時才寫，這裡從沒呼叫過）"


def test_abort_skips_to_next_word(tmp_path):
    sm, _, _, _, _ = _make_sm(tmp_path, words=("五", "四", "八"), seed=1)
    first_word = sm.order[0]
    second_word = sm.order[1]

    ev = sm.start_trial()
    assert ev["label"] == first_word
    sm.abort()

    ev2 = sm.start_trial()
    assert ev2["label"] == second_word, "abort 應該跳過第一個詞，換下一個"


def test_redo_keeps_same_word(tmp_path):
    sm, _, _, _, _ = _make_sm(tmp_path, words=("五", "四", "八"), seed=1)
    first_word = sm.order[0]

    ev = sm.start_trial()
    assert ev["label"] == first_word
    sm.redo()

    ev2 = sm.start_trial()
    assert ev2["label"] == first_word, "redo 應該保留同一個詞再試一次"


def test_cannot_abort_when_idle(tmp_path):
    sm, _, _, _, _ = _make_sm(tmp_path)
    with pytest.raises(RuntimeError):
        sm.abort()


def test_cannot_start_trial_when_not_idle(tmp_path):
    sm, _, _, _, _ = _make_sm(tmp_path)
    sm.start_trial()
    with pytest.raises(RuntimeError):
        sm.start_trial()


# ---------------------------------------------------------------------------
# 詞序 seed


def test_same_seed_produces_same_order(tmp_path):
    sm1, _, _, _, _ = _make_sm(tmp_path, words=("五", "四", "八", "一", "啊"), seed=42)
    sm2, _, _, _, _ = _make_sm(tmp_path, words=("五", "四", "八", "一", "啊"), seed=42)
    assert sm1.order == sm2.order


def test_different_seed_usually_produces_different_order(tmp_path):
    orders = set()
    for seed in range(10):
        sm, _, _, _, _ = _make_sm(tmp_path, words=("五", "四", "八", "一", "啊", "好", "停", "不要"), seed=seed)
        orders.add(tuple(sm.order))
    assert len(orders) > 1, "10 個不同 seed 打出一模一樣的順序，機率上不合理，懷疑 seed 沒真的被使用"


def test_seed_is_recorded_and_reported_in_events(tmp_path):
    sm, _, _, _, _ = _make_sm(tmp_path, seed=99)
    assert sm.seed == 99
    ev = sm.start_trial()
    assert ev["seed"] == 99


def test_no_seed_given_still_produces_a_usable_random_order(tmp_path):
    sm, _, _, _, _ = _make_sm(tmp_path, seed=None)
    assert isinstance(sm.seed, int)
    assert set(sm.order) == {"五", "四", "八"}


# ---------------------------------------------------------------------------
# quality 判定


def test_classify_quality_thresholds():
    assert classify_quality(valid_zone_ratio=0.95, drop_count=0) == "ok"
    assert classify_quality(valid_zone_ratio=0.5, drop_count=0) == "low"
    assert classify_quality(valid_zone_ratio=0.05, drop_count=5) == "rejected"
    assert classify_quality(valid_zone_ratio=0.9, drop_count=1) == "low", (
        "有掉幀就不能是 ok，即使 valid_zone_ratio 很高"
    )


# ---------------------------------------------------------------------------
# mark_current_trial_saved_quality (事後標記，C14 用)


def test_mark_saved_trial_quality_updates_h5_and_manifest(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    capture_start, capture_end = 1_000_000, 1_000_000 + int(CAPTURE_S * 1e6)
    _feed_tof(aligner, "A", capture_start - 100_000, capture_end + 100_000)
    _feed_tof(aligner, "B", capture_start - 100_000, capture_end + 100_000)
    _feed_mic(sm, capture_start - 100_000, capture_end + 100_000)

    sm.start_trial()
    clock.advance(PROMPT_S + 0.001)
    sm.tick()
    clock.advance(COUNTDOWN_S + 0.001)
    sm.tick(device_t_us=capture_start)
    clock.advance(CAPTURE_S + 0.001)
    sm.tick(device_t_us=capture_end)
    writer.__exit__(None, None, None)

    sm.mark_current_trial_saved_quality(h5_path, 0, "rejected")

    with h5py.File(h5_path, "r") as f:
        assert f["trial_000"].attrs["quality"] == "rejected"
    # C14: mark_current_trial_saved_quality() re-runs add_session(), which
    # defaults to excluding quality=rejected -- so the manifest should now
    # show this trial as *gone*, not present-with-quality=rejected. The data
    # itself stays in the HDF5 (asserted above); only the derived index
    # drops it.
    df = read_manifest(manifest_path)
    assert df.empty, "manifest 預設應排除 rejected 的 trial（資料仍在 HDF5，見上面的斷言）"


def test_mark_saved_trial_quality_rejects_invalid_value(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)
    with pytest.raises(ValueError):
        sm.mark_current_trial_saved_quality(h5_path, 0, "discarded")


# ---------------------------------------------------------------------------
# B21 階段 2：raw event 保留（給 host.vad.* 的 detect_from_events() 用，
# 還沒有任何呼叫端消費，先鎖住「保留＋能正確切視窗」這件事）


def _raw_tof_event(t_us, sensor="A", dim=16):
    return {
        "type": "tof", "proto": 2, "sensor": sensor, "seq": t_us, "t_us": t_us,
        "has_timestamp": True, "dim": dim,
        "distance": [100.0] * dim, "signal": [10.0] * dim, "signal_present": True,
        "valid": [True] * dim, "n_valid": dim,
    }


def _raw_mic_event(t_us, rms=100.0, peak=1000.0):
    return {"type": "mic", "proto": 2, "seq": t_us, "t_us": t_us, "has_timestamp": True,
            "rms": rms, "peak": peak}


def test_push_event_buffers_raw_tof_and_mic_events(tmp_path):
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    sm.push_event(_raw_tof_event(1_000_000, sensor="A"))
    sm.push_event(_raw_tof_event(1_000_000, sensor="B"))
    sm.push_event(_raw_mic_event(1_000_000))

    assert len(sm._raw_events) == 3
    assert {e["type"] for e in sm._raw_events} == {"tof", "mic"}


def test_push_event_ignores_non_tof_mic_types(tmp_path):
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    sm.push_event({"type": "mel", "t_us": 1_000_000, "log_mel": [0.0] * 40})
    sm.push_event({"type": "quality", "t_us": 1_000_000, "metrics": {}})
    assert sm._raw_events == []


def test_raw_events_window_slices_to_capture_boundaries(tmp_path):
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    for t_us in (900_000, 1_000_000, 1_500_000, 2_000_000, 2_100_000):
        sm.push_event(_raw_tof_event(t_us))
        sm.push_event(_raw_mic_event(t_us))

    window = sm._raw_events_window(1_000_000, 2_000_000)
    t_values = sorted({e["t_us"] for e in window})
    assert t_values == [1_000_000, 1_500_000, 2_000_000]
    assert len(window) == 6  # 3 個時間點 x (tof + mic)


def test_raw_events_trimmed_by_mic_buffer_seconds(tmp_path):
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    sm.push_event(_raw_tof_event(0))
    far_future_us = int(sm._mic_buffer_seconds * 1_000_000) + 1_000_000
    sm.push_event(_raw_tof_event(far_future_us))

    assert all(e["t_us"] != 0 for e in sm._raw_events), (
        "超過 mic_buffer_seconds 的舊事件應該被修剪掉，不能無限累積"
    )


# ---------------------------------------------------------------------------
# mel（$F）：獨立取樣率、選填


def test_mel_written_at_its_own_native_rate_when_present(tmp_path):
    """mel 62.5Hz、mic 31.25Hz，2 秒視窗理論上 mel 幀數大約是 mic 的兩倍，
    而且 write_trial() 不應該因為兩者長度不同而報錯（B07 已經改成 mel 只跟
    mel_t_us 比長度，不跟 mic_t_us 比）。"""
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    capture_start, capture_end = 1_000_000, 1_000_000 + int(CAPTURE_S * 1e6)
    _feed_tof(aligner, "A", capture_start - 100_000, capture_end + 100_000)
    _feed_tof(aligner, "B", capture_start - 100_000, capture_end + 100_000)
    _feed_mic(sm, capture_start - 100_000, capture_end + 100_000, rate_hz=31.25)
    _feed_mel(sm, capture_start - 100_000, capture_end + 100_000, rate_hz=62.5)

    sm.start_trial()
    clock.advance(PROMPT_S + 0.001)
    sm.tick()
    clock.advance(COUNTDOWN_S + 0.001)
    sm.tick(device_t_us=capture_start)
    clock.advance(CAPTURE_S + 0.001)
    sm.tick(device_t_us=capture_end)
    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        trial = f["trial_000"]
        assert "mel" in trial
        assert "mel_t_us" in trial
        n_mic = trial["mic_t_us"].shape[0]
        n_mel = trial["mel"].shape[0]
        assert trial["mel"].shape[1] == 40
        assert trial["mel_t_us"].shape == (n_mel,)
        # 不要求剛好 2 倍（邊界取樣會有 +-1 誤差），但應該明顯比 mic 多，
        # 證明兩者真的各自用自己的取樣率，沒有被硬湊成同一個長度。
        assert n_mel > n_mic


def test_mel_omitted_when_no_mel_events_in_window(tmp_path):
    """$F 被 MEL:0 關掉、或這個視窗剛好沒收到 -- mel 不該被硬塞任何資料，
    也不該讓 trial 整個寫不進去，quality 判定也不該因此變差。"""
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    capture_start, capture_end = 1_000_000, 1_000_000 + int(CAPTURE_S * 1e6)
    _feed_tof(aligner, "A", capture_start - 100_000, capture_end + 100_000)
    _feed_tof(aligner, "B", capture_start - 100_000, capture_end + 100_000)
    _feed_mic(sm, capture_start - 100_000, capture_end + 100_000)
    # 刻意不呼叫 _feed_mel

    sm.start_trial()
    clock.advance(PROMPT_S + 0.001)
    sm.tick()
    clock.advance(COUNTDOWN_S + 0.001)
    sm.tick(device_t_us=capture_start)
    clock.advance(CAPTURE_S + 0.001)
    events = sm.tick(device_t_us=capture_end)
    writer.__exit__(None, None, None)

    save_event = events[0]
    assert save_event["quality"] == "ok", "沒有 mel 不該影響 quality 判定"

    with h5py.File(h5_path, "r") as f:
        assert "mel" not in f["trial_000"]
        assert "mel_t_us" not in f["trial_000"]


# ---------------------------------------------------------------------------
# B12 -- hold-to-record


def test_hold_to_record_advances_word_across_consecutive_trials(tmp_path):
    """E05 排練時 ca 真的卡住的 bug：連續多筆 hold-to-record，詞永遠不換
    （`_next_trial_idx` 有前進、`_order_pos` 沒有）——`hold_stop()` 的直接
    存檔路徑漏了 `tick()`／`confirm_keep()` 都有的 `_order_pos += 1`，
    單元測試之前沒抓到是因為既有測試只測時長/資料，沒有斷言連續兩筆
    hold-to-record 之間詞真的換了。這裡鎖住：seed 固定、連續存 3 筆，
    三個 label 必須兩兩不同。
    """
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(
        tmp_path, words=("五", "四", "八"), seed=1, clock=clock,
    )
    _feed_tof(aligner, "A", 900_000, 20_000_000)
    _feed_tof(aligner, "B", 900_000, 20_000_000)
    _feed_mic(sm, 900_000, 20_000_000)

    labels = []
    t = 1_000_000
    for _ in range(3):
        sm.hold_start(device_t_us=t)
        events = sm.hold_stop(device_t_us=t + 1_000_000)
        save_event = events[0]
        assert save_event["state"] == "SAVE"
        labels.append(save_event["label"])
        t += 2_000_000
        clock.advance(REST_S + 0.001)
        sm.tick()  # REST -> IDLE, matching the real trial_ticker()'s job

    assert len(set(labels)) == 3, f"三筆應該是三個不同的詞，實際是 {labels}"


def test_hold_stop_save_event_carries_next_label(tmp_path):
    """跟上面同一個漏洞的另一半：`hold_stop()` 存檔成功時進的 REST 事件
    原本沒有帶 `next_label`（`tick()`／`confirm_keep()` 的等價路徑都有），
    前端因此永遠讀不到新的下一個詞提示。"""
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    _feed_tof(aligner, "A", 900_000, 1_300_000)
    _feed_tof(aligner, "B", 900_000, 1_300_000)
    _feed_mic(sm, 900_000, 1_300_000)

    sm.hold_start(device_t_us=1_000_000)
    events = sm.hold_stop(device_t_us=2_000_000)
    rest_event = events[1]
    assert rest_event["state"] == "REST"
    assert "next_label" in rest_event
    assert rest_event["next_label"] == sm.peek_next_label()


def test_hold_one_second_covers_about_1_5s_with_padding(tmp_path):
    """驗收條件：按住 1 秒放開，存下的資料涵蓋約 1.5 秒（含前後 padding）。"""
    from host.trial.state_machine import HOLD_POST_ROLL_US, HOLD_PRE_ROLL_US

    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    hold_start_t_us = 10_000_000
    hold_stop_t_us = hold_start_t_us + 1_000_000  # 按住剛好 1 秒
    window_start = hold_start_t_us - HOLD_PRE_ROLL_US
    window_end = hold_stop_t_us + HOLD_POST_ROLL_US
    _feed_tof(aligner, "A", window_start - 100_000, window_end + 100_000)
    _feed_tof(aligner, "B", window_start - 100_000, window_end + 100_000)
    _feed_mic(sm, window_start - 100_000, window_end + 100_000)

    ev = sm.hold_start(device_t_us=hold_start_t_us)
    assert ev["state"] == "CAPTURE"
    assert sm.state == TrialState.CAPTURE

    events = sm.hold_stop(device_t_us=hold_stop_t_us)
    save_event = events[0]
    assert save_event["state"] == "SAVE"
    assert save_event["hold_duration_s"] == pytest.approx(1.0, abs=0.01)
    assert sm.state == TrialState.REST

    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        tof_t_us = f["trial_000"]["tof_t_us"][:]
    covered_s = (int(tof_t_us[-1]) - int(tof_t_us[0])) / 1e6
    assert covered_s == pytest.approx(1.5, abs=0.1), f"涵蓋 {covered_s}s，應該約 1.5s"


def test_hold_pre_roll_comes_from_real_ring_buffer_data_not_zero_padding(tmp_path):
    """驗收條件：回溯的 300ms 確實來自環形緩衝，不是補零。用非零、可辨識
    的距離值餵進 pre-roll 那段，確認寫進 HDF5 的資料真的是那個值，不是
    0（或 NaN，代表『沒收到當成沒資料』，那也不對——這裡是真的有資料）。
    """
    from host.trial.state_machine import HOLD_PRE_ROLL_US

    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    hold_start_t_us = 10_000_000
    hold_stop_t_us = hold_start_t_us + 1_000_000
    pre_roll_start = hold_start_t_us - HOLD_PRE_ROLL_US

    # pre-roll 這段（按鍵按下之前）餵一個獨特的距離值，之後那段餵另一個，
    # 這樣才能確認寫進去的第一批幀真的來自「按下之前」就存在的資料。
    _feed_tof(aligner, "A", pre_roll_start - 50_000, hold_start_t_us, dim=16)
    for t in range(hold_start_t_us, hold_stop_t_us + 300_000, int(1e6 / 30)):
        aligner.push_tof("A", t, distance=[777.0] * 16, signal=[10.0] * 16, valid=[True] * 16)
    aligner.push_tof("B", pre_roll_start - 50_000, distance=[100.0] * 16, signal=[10.0] * 16, valid=[True] * 16)
    _feed_mic(sm, pre_roll_start - 50_000, hold_stop_t_us + 300_000)

    sm.hold_start(device_t_us=hold_start_t_us)
    sm.hold_stop(device_t_us=hold_stop_t_us)
    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        tof_A = f["trial_000"]["tof_A"][:]
    # 第一幀落在 pre-roll 視窗內，應該拿到 _feed_tof 餵的 100.0mm（來自
    # 「按下之前」就已經在環形緩衝裡的資料），不是 0 也不是 NaN。
    first_distance = tof_A[0, 0]
    assert not np.isnan(first_distance), "pre-roll 第一幀不該是 NaN（代表沒資料）"
    assert first_distance == pytest.approx(100.0), (
        f"pre-roll 第一幀距離是 {first_distance}，應該是環形緩衝裡按下之前的真實值 100.0，"
        "不是補零或拿到按下之後的值"
    )


def test_hold_shorter_than_min_goes_to_confirm_not_saved(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    hold_start_t_us = 10_000_000
    hold_stop_t_us = hold_start_t_us + 100_000  # 只按 0.1 秒，< 0.3s 門檻
    _feed_tof(aligner, "A", hold_start_t_us - 400_000, hold_stop_t_us + 300_000)
    _feed_tof(aligner, "B", hold_start_t_us - 400_000, hold_stop_t_us + 300_000)
    _feed_mic(sm, hold_start_t_us - 400_000, hold_stop_t_us + 300_000)

    sm.hold_start(device_t_us=hold_start_t_us)
    event = sm.hold_stop(device_t_us=hold_stop_t_us)

    assert event["state"] == "CONFIRM"
    assert event["warning"] == "too_short"
    assert sm.state == TrialState.CONFIRM

    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        assert [k for k in f.keys() if k.startswith("trial_")] == [], "超出範圍前不應該落盤"


def test_hold_longer_than_max_goes_to_confirm_not_saved(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    hold_start_t_us = 10_000_000
    hold_stop_t_us = hold_start_t_us + 6_000_000  # 按了 6 秒，> 5s 門檻
    _feed_tof(aligner, "A", hold_start_t_us - 400_000, hold_stop_t_us + 300_000)
    _feed_tof(aligner, "B", hold_start_t_us - 400_000, hold_stop_t_us + 300_000)
    _feed_mic(sm, hold_start_t_us - 400_000, hold_stop_t_us + 300_000)

    sm.hold_start(device_t_us=hold_start_t_us)
    event = sm.hold_stop(device_t_us=hold_stop_t_us)

    assert event["state"] == "CONFIRM"
    assert event["warning"] == "too_long"


def test_confirm_keep_saves_the_pending_capture(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    hold_start_t_us = 10_000_000
    hold_stop_t_us = hold_start_t_us + 100_000
    _feed_tof(aligner, "A", hold_start_t_us - 400_000, hold_stop_t_us + 300_000)
    _feed_tof(aligner, "B", hold_start_t_us - 400_000, hold_stop_t_us + 300_000)
    _feed_mic(sm, hold_start_t_us - 400_000, hold_stop_t_us + 300_000)

    sm.hold_start(device_t_us=hold_start_t_us)
    sm.hold_stop(device_t_us=hold_stop_t_us)
    assert sm.state == TrialState.CONFIRM

    events = sm.confirm_keep()
    assert events[0]["state"] == "SAVE"
    assert sm.state == TrialState.REST

    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        assert "trial_000" in f
    df = read_manifest(manifest_path)
    assert len(df) == 1


def test_discard_pending_writes_nothing_and_skips_word(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock, words=("五", "四"))
    first_word = sm.order[0]
    second_word = sm.order[1]

    hold_start_t_us = 10_000_000
    hold_stop_t_us = hold_start_t_us + 100_000
    _feed_tof(aligner, "A", hold_start_t_us - 400_000, hold_stop_t_us + 300_000)
    _feed_tof(aligner, "B", hold_start_t_us - 400_000, hold_stop_t_us + 300_000)
    _feed_mic(sm, hold_start_t_us - 400_000, hold_stop_t_us + 300_000)

    ev = sm.hold_start(device_t_us=hold_start_t_us)
    assert ev["label"] == first_word
    sm.hold_stop(device_t_us=hold_stop_t_us)  # too short -> CONFIRM

    sm.discard_pending()
    assert sm.state == TrialState.IDLE

    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        assert [k for k in f.keys() if k.startswith("trial_")] == []
    df = read_manifest(manifest_path)
    assert len(df) == 0

    ev2 = sm.hold_start(device_t_us=hold_start_t_us + 10_000_000)
    assert ev2["label"] == second_word, "discard_pending 應該跳過這個詞，換下一個"


def test_hold_stop_requires_prior_hold_start(tmp_path):
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    with pytest.raises(RuntimeError):
        sm.hold_stop(device_t_us=1_000_000)


def test_hold_start_requires_device_t_us(tmp_path):
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    with pytest.raises(ValueError):
        sm.hold_start()


# ---------------------------------------------------------------------------
# B21 階段 1：speaking_mode 是 trial 的屬性，不是 session 的（跟 `mode` 分開）


def test_speaking_mode_rejects_invalid_value_before_capture_starts(tmp_path):
    """故事的整個重點：使用者填了 {normal,whisper,silent} 以外的值，必須在
    真的開始錄之前就報錯，不能等到 SAVE 那一刻才讓整個 session 中斷。"""
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    with pytest.raises(ValueError, match="speaking_mode"):
        sm.hold_start(device_t_us=1_000_000, speaking_mode="quiz")
    assert sm.state == TrialState.IDLE, "驗證失敗不該把狀態機推進 CAPTURE"

    with pytest.raises(ValueError, match="speaking_mode"):
        sm.start_trial(speaking_mode="loud")
    assert sm.state == TrialState.IDLE


def test_speaking_mode_is_sticky_across_trials(tmp_path):
    """沒指定 speaking_mode 的呼叫沿用上一次的值，不是每次重置成 normal
    ——面板上的三顆按鈕是「目前選哪個」，不是每個 trial 各自獨立的東西。"""
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    sm.hold_start(device_t_us=1_000_000, speaking_mode="whisper")
    assert sm._speaking_mode == "whisper"
    sm.abort()  # 放棄這筆，回到 IDLE 才能再 hold_start()

    sm.hold_start(device_t_us=2_000_000)  # 沒指定
    assert sm._speaking_mode == "whisper", "沒指定就該沿用上一次的值"


def test_speaking_mode_written_to_hdf5(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)
    _feed_tof(aligner, "A", 900_000, 1_300_000)
    _feed_tof(aligner, "B", 900_000, 1_300_000)
    _feed_mic(sm, 900_000, 1_300_000)

    sm.hold_start(device_t_us=1_000_000, speaking_mode="whisper")
    sm.hold_stop(device_t_us=2_000_000)
    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        assert f["trial_000"].attrs["speaking_mode"] == "whisper"


def test_baseline_age_s_written_to_hdf5(tmp_path):
    """ca's audit: a stale baseline produces trials that look fine (no
    crash, no error) but have a wrong z-score reference the whole way
    down -- a screen warning only protects whoever's watching it live,
    this is what lets D14 flag/exclude trials after the fact. Only the
    age gets stored, not a "was it stale" verdict -- the threshold can
    change later, a stored age never goes stale itself."""
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)
    _feed_tof(aligner, "A", 900_000, 1_300_000)
    _feed_tof(aligner, "B", 900_000, 1_300_000)
    _feed_mic(sm, 900_000, 1_300_000)

    sm.hold_start(device_t_us=1_000_000, baseline_age_s=42.5)
    sm.hold_stop(device_t_us=2_000_000)
    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        assert f["trial_000"].attrs["baseline_age_s"] == pytest.approx(42.5)


def test_baseline_age_s_omitted_when_not_provided(tmp_path):
    """沒給就整個 attr 不寫入（跟 vad_start_us 等同一個原則），不是 0——
    0 秒是一個合法的年齡（剛擷取完立刻錄），會被誤讀成「baseline 很新鮮」。"""
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)
    _feed_tof(aligner, "A", 900_000, 1_300_000)
    _feed_tof(aligner, "B", 900_000, 1_300_000)
    _feed_mic(sm, 900_000, 1_300_000)

    sm.hold_start(device_t_us=1_000_000)  # baseline_age_s 沒給
    sm.hold_stop(device_t_us=2_000_000)
    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        assert "baseline_age_s" not in f["trial_000"].attrs


# ---------------------------------------------------------------------------
# B21 階段 3：真的呼叫 B15/B16，四個 VAD 欄位是真值


def _tof_events_from_synth(tof, t_us, sensor, n_zones):
    events = []
    for row, ts in zip(tof, t_us):
        events.append({
            "type": "tof", "sensor": sensor, "seq": len(events), "t_us": int(ts),
            "dim": n_zones, "distance": list(row[:n_zones]), "signal": list(row[n_zones:]),
            "valid": [True] * n_zones,
        })
    return events


def _mic_events_from_synth(rms, t_us):
    return [{"type": "mic", "seq": i, "t_us": int(ts), "rms": float(r), "peak": 0.0}
            for i, (r, ts) in enumerate(zip(rms, t_us))]


def test_vad_fields_are_real_when_baseline_and_speech_present(tmp_path):
    """錄一筆有語音的 trial，四個 VAD 欄位應該是真值，不是 None（B21 的
    驗收條件）。合成資料，不是真實錄音——跟 host/vad/test_tof_vad.py／
    test_audio_vad.py 自己承認的限制一樣，這裡驗的是接線接對了，不是
    偵測演算法本身的準確度（那是 B15/B16 自己的驗收範圍）。
    """
    from host.vad.test_audio_vad import NOISE_MU, NOISE_SIGMA, synth_recording
    from host.vad.test_tof_vad import N_ZONES, baseline, synth_tof

    baseline_mu, baseline_sigma = baseline()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(
        tmp_path, baseline_mu_A=baseline_mu, baseline_sigma_A=baseline_sigma,
        noise_floor_mu=NOISE_MU, noise_floor_sigma=NOISE_SIGMA,
    )

    tof, tof_t, _, _ = synth_tof(np.random.RandomState(21))
    rms, mic_t, _, _ = synth_recording(np.random.RandomState(21))
    for e in _tof_events_from_synth(tof, tof_t, "A", N_ZONES):
        sm.push_event(e)
    for e in _mic_events_from_synth(rms, mic_t):
        sm.push_event(e)

    # 涵蓋 synth_tof/synth_recording 兩者的動作區間（見它們自己的預設
    # onset_frame/n_active/n_voiced），加上 hold-to-record 的 pre/post-roll。
    hold_start_t_us = tof_t[0] + 1_500_000
    hold_stop_t_us = hold_start_t_us + 1_200_000
    sm.hold_start(device_t_us=hold_start_t_us, speaking_mode="normal")
    events = sm.hold_stop(device_t_us=hold_stop_t_us)
    save_event = events[0] if isinstance(events, list) else events
    assert save_event["state"] == "SAVE"
    assert "vad_comparable" in save_event

    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        attrs = f["trial_000"].attrs
        for key in ("vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us"):
            assert key in attrs, f"{key} 應該是真值，不該整個 attr 缺席"
        # comparable：18 已經把 session_writer.py 那邊接上了，值有沒有記
        # 錄下來（True 或 False 都算）才是這裡要驗的——`measure_lip_lead()`
        # 的 comparable 除了「兩邊都偵測到」還會反推門檻 σ 倍數是否一致，
        # 合成資料湊出來的兩邊門檻不一定一致，所以不斷言一定是 True。
        assert isinstance(bool(attrs["comparable"]), bool)


def test_comparable_is_false_not_absent_when_lip_or_voice_missing(tmp_path):
    """`comparable=False`（算過，結論是不可比）跟「整個 attr 不寫入」
    （沒算過/沒偵測到）是兩回事——這裡沒有 baseline，唇動偵測整個
    applicable=False，`measure_lip_lead()` 因此判定 comparable=False，
    這個 attr 應該**寫入**，不是缺席。"""
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)
    _feed_tof(aligner, "A", 900_000, 1_300_000)
    _feed_tof(aligner, "B", 900_000, 1_300_000)
    _feed_mic(sm, 900_000, 1_300_000)

    sm.hold_start(device_t_us=1_000_000)
    sm.hold_stop(device_t_us=2_000_000)
    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        attrs = f["trial_000"].attrs
        assert "comparable" in attrs
        assert bool(attrs["comparable"]) is False


def test_baseline_energy_floor_is_passed_through_to_detect_lips(tmp_path, monkeypatch):
    """B21：`energy_mu`/`energy_sigma`（`host/storage/baseline.py` 的
    `evaluate_baseline()` 算好的）要真的傳到 `detect_lips()`，不是收下來
    沒用。白箱測——直接檢查呼叫時的 kwargs，比反推偵測結果的數值差異
    可靠，不用湊出一段剛好會讓兩種估法產生可觀察差異的合成資料。
    """
    import host.trial.state_machine as sm_module

    captured = {}
    real_detect_lips = sm_module.detect_lips

    def spy(*args, **kwargs):
        captured["energy_mu"] = kwargs.get("energy_mu")
        captured["energy_sigma"] = kwargs.get("energy_sigma")
        return real_detect_lips(*args, **kwargs)

    monkeypatch.setattr(sm_module, "detect_lips", spy)

    sm, writer, aligner, h5_path, manifest_path = _make_sm(
        tmp_path, energy_mu=0.9, energy_sigma=0.2,
    )
    _feed_tof(aligner, "A", 900_000, 1_300_000)
    _feed_tof(aligner, "B", 900_000, 1_300_000)
    _feed_mic(sm, 900_000, 1_300_000)
    sm.hold_start(device_t_us=1_000_000)
    sm.hold_stop(device_t_us=2_000_000)

    assert captured["energy_mu"] == 0.9
    assert captured["energy_sigma"] == 0.2


# ---------------------------------------------------------------------------
# B21 F：union_min 雙感測器融合（調度員裁決：取 onset 較早的那顆整個結果）


def test_union_min_prefers_earlier_onset_between_two_detected_sensors():
    from host.trial.state_machine import _union_min_lips
    from host.vad.test_tof_vad import baseline, synth_tof

    baseline_mu, baseline_sigma = baseline()
    # B 的動作比 A 早（onset_frame 較小），union_min 應該選 B。
    tof_A, t_A, _, _ = synth_tof(np.random.RandomState(30), onset_frame=60)
    tof_B, t_B, _, _ = synth_tof(np.random.RandomState(31), onset_frame=20)
    # detect_lips 是 detect_from_events 的別名（吃事件字典）；這裡直接用
    # 陣列版本 detect_lip_activity() 比較省事，不用另外組事件字典。
    from host.vad.tof_vad import detect_lip_activity
    lips_A = detect_lip_activity(tof_A, t_A, baseline_mu, baseline_sigma)
    lips_B = detect_lip_activity(tof_B, t_B, baseline_mu, baseline_sigma)
    assert lips_A.detected and lips_B.detected

    fused = _union_min_lips(lips_A, lips_B)
    assert fused is lips_B, "B 的 onset 較早，union_min 應該選 B 整個結果"


def test_union_min_falls_back_to_whichever_sensor_detected():
    from host.trial.state_machine import _union_min_lips
    from host.vad.tof_vad import TofVadResult

    from host.vad.tof_vad import Segment

    not_detected = TofVadResult(applicable=True, segments=())  # 跑過了，但沒偵測到動作
    detected = TofVadResult(applicable=True, segments=(
        Segment(start_us=100, end_us=200, peak_z=5.0, mean_z=3.0, n_frames=3, n_frames_above_enter=3),
    ))

    assert _union_min_lips(detected, not_detected) is detected
    assert _union_min_lips(not_detected, detected) is detected
    # 兩顆都沒偵測到：回傳 A（reason 說明用），不拋例外。
    assert _union_min_lips(not_detected, not_detected) is not_detected


def test_lip_onset_us_a_and_b_written_separately_when_only_a_detects(tmp_path):
    """B 缺席是 union_min 設計本身假設的正常狀況——只餵 sensor A 的唇動
    資料，sensor B 全程平靜（沒有動作），`lip_onset_us_A` 應該有值，
    `lip_onset_us_B` 應該整個 attr 不寫入（不是 0，也不是跟 A 綁在一起
    檢查）。"""
    from host.vad.test_tof_vad import N_ZONES, baseline, synth_tof

    baseline_mu, baseline_sigma = baseline()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(
        tmp_path, baseline_mu_A=baseline_mu, baseline_sigma_A=baseline_sigma,
        baseline_mu_B=baseline_mu, baseline_sigma_B=baseline_sigma,
    )

    tof_A, t_A, _, _ = synth_tof(np.random.RandomState(32))
    flat_B = np.tile(baseline_mu, (len(t_A), 1))  # 完全靜止，沒有任何動作
    for e in _tof_events_from_synth(tof_A, t_A, "A", N_ZONES):
        sm.push_event(e)
    for e in _tof_events_from_synth(flat_B, t_A, "B", N_ZONES):
        sm.push_event(e)

    hold_start_t_us = t_A[0] + 1_500_000
    hold_stop_t_us = hold_start_t_us + 1_200_000
    sm.hold_start(device_t_us=hold_start_t_us)
    sm.hold_stop(device_t_us=hold_stop_t_us)
    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        attrs = f["trial_000"].attrs
        assert "lip_onset_us_A" in attrs
        assert "lip_onset_us_B" not in attrs
        # 融合後的 lip_onset_us 應該跟 A 自己的 onset 一致（B 沒有東西可選）。
        assert attrs["lip_onset_us"] == attrs["lip_onset_us_A"]


def test_silent_mode_has_lip_onset_but_no_voice_onset(tmp_path):
    """驗收條件：silent 模式下 voice_onset_us 缺席但 lip_onset_us 仍有值
    ——不可假設四個欄位同時存在或同時缺席。"""
    from host.vad.test_tof_vad import N_ZONES, baseline, synth_tof

    baseline_mu, baseline_sigma = baseline()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(
        tmp_path, baseline_mu_A=baseline_mu, baseline_sigma_A=baseline_sigma,
        noise_floor_mu=300.0, noise_floor_sigma=30.0,
    )

    tof, tof_t, _, _ = synth_tof(np.random.RandomState(22))
    for e in _tof_events_from_synth(tof, tof_t, "A", N_ZONES):
        sm.push_event(e)
    # 刻意不餵任何 mic 事件：silent 模式本來就不該有語音。

    hold_start_t_us = tof_t[0] + 1_500_000
    hold_stop_t_us = hold_start_t_us + 1_200_000
    sm.hold_start(device_t_us=hold_start_t_us, speaking_mode="silent")
    sm.hold_stop(device_t_us=hold_stop_t_us)

    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        attrs = f["trial_000"].attrs
        assert "lip_onset_us" in attrs, "silent 模式下唇動偵測仍應該有值"
        assert "voice_onset_us" not in attrs, "silent 模式下不該有語音起點"


def test_no_baseline_means_all_four_vad_attrs_absent(tmp_path):
    """沒有 baseline（例如舊測試、或呼叫端還沒接上 B21 的新參數）時，四個
    欄位應該整個 attr 不寫入——不是 0、不是 capture 視窗邊界，這是 CONTRACTS
    的既有要求，B21 接上真的偵測邏輯後不能悄悄退化回填假值。"""
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)
    _feed_tof(aligner, "A", 900_000, 1_300_000)
    _feed_tof(aligner, "B", 900_000, 1_300_000)
    _feed_mic(sm, 900_000, 1_300_000)

    sm.hold_start(device_t_us=1_000_000)
    sm.hold_stop(device_t_us=2_000_000)
    writer.__exit__(None, None, None)

    with h5py.File(h5_path, "r") as f:
        attrs = f["trial_000"].attrs
        for key in ("vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us"):
            assert key not in attrs


def test_tick_never_auto_exits_a_hold_to_record_capture(tmp_path):
    """固定時長模式的 CAPTURE_S=2.0s 計時器不該影響 hold-to-record：按住
    超過 2 秒（原本固定模式早就結束了）還是要停在 CAPTURE，直到 hold_stop()。
    """
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    sm.hold_start(device_t_us=10_000_000)
    clock.advance(CAPTURE_S + 1.0)  # 遠超過固定模式的 2.0 秒
    events = sm.tick(device_t_us=10_000_000 + int((CAPTURE_S + 1.0) * 1e6))
    assert events == [], "hold-to-record 的 CAPTURE 不該被 tick() 的固定計時器結束"
    assert sm.state == TrialState.CAPTURE


def test_abort_still_works_mid_hold(tmp_path):
    """按著按鍵中途想整個放棄（不是放開結束）也該可以用 abort()。"""
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path)
    sm.hold_start(device_t_us=10_000_000)
    event = sm.abort()
    assert event["state"] == "IDLE"
    assert sm.state == TrialState.IDLE


# ---------------------------------------------------------------------------
# C12 -- peek_next_label() / next_label in events


def test_peek_next_label_does_not_consume_or_change_order_pos(tmp_path):
    sm, _, _, _, _ = _make_sm(tmp_path, words=("五", "四", "八"), seed=1)
    first = sm.order[0]
    assert sm.peek_next_label() == first
    assert sm.peek_next_label() == first, "peek 兩次應該回同一個詞，不消耗"
    ev = sm.start_trial()
    assert ev["label"] == first, "peek 看到的應該跟真的 start_trial() 選到的一致"


def test_peek_next_label_wraps_around_cyclically(tmp_path):
    """詞序是循環的（E05 要遠超過 8 次重複），用完一輪要繞回開頭，不是回 None。"""
    words = ("五", "四")
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, words=words, seed=1)

    seen = []
    for _ in range(len(words) * 2 + 1):  # 跑超過兩輪
        seen.append(sm.peek_next_label())
        sm.start_trial()
        sm.abort()  # 跳過，前進到下一個詞，不落盤（不需要真的錄）

    assert seen == [sm.order[i % len(words)] for i in range(len(seen))]


def test_next_label_in_idle_and_rest_and_save_events(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(
        tmp_path, words=("五", "四", "八"), seed=1, clock=clock
    )
    expected_next = sm.order[1]  # order[0] is about to be recorded

    capture_start, capture_end = 1_000_000, 1_000_000 + int(CAPTURE_S * 1e6)
    _feed_tof(aligner, "A", capture_start - 100_000, capture_end + 100_000)
    _feed_tof(aligner, "B", capture_start - 100_000, capture_end + 100_000)
    _feed_mic(sm, capture_start - 100_000, capture_end + 100_000)

    sm.start_trial()
    clock.advance(PROMPT_S + 0.001)
    sm.tick()
    clock.advance(COUNTDOWN_S + 0.001)
    sm.tick(device_t_us=capture_start)
    clock.advance(CAPTURE_S + 0.001)
    save_event, rest_event = sm.tick(device_t_us=capture_end)

    assert save_event["next_label"] == expected_next
    assert rest_event["next_label"] == expected_next

    clock.advance(REST_S + 0.001)
    idle_event = sm.tick()[0]
    assert idle_event["state"] == "IDLE"
    assert idle_event["next_label"] == expected_next

    # 而且下一次 start_trial() 真的選到 peek 說的那個詞 -- 不只是欄位對，
    # 行為也要對。
    ev = sm.start_trial()
    assert ev["label"] == expected_next


def test_abort_advances_next_label_redo_keeps_it(tmp_path):
    """dispatcher 特別交代要釘死的：abort 之後 next_label 前進，
    redo 之後 next_label 不變。"""
    sm, _, _, _, _ = _make_sm(tmp_path, words=("五", "四", "八"), seed=1)
    first, second = sm.order[0], sm.order[1]

    sm.start_trial()
    assert sm.peek_next_label() == first  # 還沒 abort/redo 前，「下一個」就是正在錄的這個

    redo_event = sm.redo()
    assert redo_event["next_label"] == first, "redo 之後應該還是同一個詞"
    assert sm.peek_next_label() == first

    sm.start_trial()
    abort_event = sm.abort()
    assert abort_event["next_label"] == second, "abort 之後應該前進到下一個詞"
    assert sm.peek_next_label() == second


def test_confirm_keep_and_discard_pending_carry_correct_next_label(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(
        tmp_path, words=("五", "四", "八"), seed=1, clock=clock
    )
    first, second = sm.order[0], sm.order[1]

    capture_start, capture_end = 1_000_000, 1_000_000 + 100_000
    _feed_tof(aligner, "A", capture_start - 400_000, capture_end + 300_000)
    _feed_tof(aligner, "B", capture_start - 400_000, capture_end + 300_000)
    _feed_mic(sm, capture_start - 400_000, capture_end + 300_000)

    sm.hold_start(device_t_us=capture_start)
    confirm_event = sm.hold_stop(device_t_us=capture_end)  # too short -> CONFIRM
    assert confirm_event["label"] == first

    save_event, rest_event = sm.confirm_keep()
    assert save_event["next_label"] == second
    assert rest_event["next_label"] == second


def test_discard_pending_advances_next_label(tmp_path):
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, words=("五", "四", "八"), seed=1)
    first, second = sm.order[0], sm.order[1]

    sm.hold_start(device_t_us=1_000_000)
    sm.hold_stop(device_t_us=1_000_000 + 100_000)  # too short -> CONFIRM
    idle_event = sm.discard_pending()
    assert idle_event["next_label"] == second


# ---------------------------------------------------------------------------
# C12 -- first_trial_idx (baseline occupies trial_000)


def test_first_trial_idx_avoids_colliding_with_baseline(tmp_path):
    session_dir = tmp_path
    h5_path = session_dir / "session.h5"
    manifest_path = session_dir / "manifest.csv"
    writer = SessionWriter(h5_path, _full_sample_meta())
    writer.__enter__()
    aligner = Aligner()
    clock_ = FakeClock()
    sm = TrialStateMachine(
        ("五", "四"), aligner, writer, h5_path, manifest_path,
        wear_id=3, mode="quiz", seed=1, clock=clock_, manifest_root=session_dir,
        first_trial_idx=1,  # trial_000 is the baseline, written separately
    )
    assert sm.next_trial_idx == 1

    capture_start, capture_end = 1_000_000, 1_000_000 + int(CAPTURE_S * 1e6)
    _feed_tof(aligner, "A", capture_start - 100_000, capture_end + 100_000)
    _feed_tof(aligner, "B", capture_start - 100_000, capture_end + 100_000)
    _feed_mic(sm, capture_start - 100_000, capture_end + 100_000)

    sm.start_trial()
    clock_.advance(PROMPT_S + 0.001)
    sm.tick()
    clock_.advance(COUNTDOWN_S + 0.001)
    sm.tick(device_t_us=capture_start)
    clock_.advance(CAPTURE_S + 0.001)
    save_event = sm.tick(device_t_us=capture_end)[0]

    assert save_event["idx"] == 1
    writer.__exit__(None, None, None)
    with h5py.File(h5_path, "r") as f:
        assert "trial_001" in f
        assert "trial_000" not in f  # nobody wrote a baseline in this test, just confirming no collision assumption


# ---------------------------------------------------------------------------
# C12 -- abort/redo must not apply to an already-saved (REST) trial


def test_abort_raises_during_rest(tmp_path):
    """B12 修正的邊界案例：REST 代表這個 trial 已經存檔了，abort/redo
    不該再套用（否則會讓詞指標被多跳過一次）。"""
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)

    capture_start, capture_end = 1_000_000, 1_000_000 + int(CAPTURE_S * 1e6)
    _feed_tof(aligner, "A", capture_start - 100_000, capture_end + 100_000)
    _feed_tof(aligner, "B", capture_start - 100_000, capture_end + 100_000)
    _feed_mic(sm, capture_start - 100_000, capture_end + 100_000)

    sm.start_trial()
    clock.advance(PROMPT_S + 0.001)
    sm.tick()
    clock.advance(COUNTDOWN_S + 0.001)
    sm.tick(device_t_us=capture_start)
    clock.advance(CAPTURE_S + 0.001)
    sm.tick(device_t_us=capture_end)
    assert sm.state == TrialState.REST

    with pytest.raises(RuntimeError):
        sm.abort()
    with pytest.raises(RuntimeError):
        sm.redo()
