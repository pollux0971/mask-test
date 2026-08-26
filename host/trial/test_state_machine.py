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


def _make_sm(tmp_path, *, words=("五", "四", "八"), seed=1, clock=None, wear_id=3, mode="quiz"):
    """每次呼叫都在 `tmp_path` 底下開一個獨立子目錄放 session/manifest，
    這樣同一個測試裡呼叫兩次（例如比較兩個不同 seed 的 order）不會撞同一個
    還開著的 HDF5 檔案。`clock` 沒給就用一個新的 `FakeClock()`——**呼叫端如果
    要自己控制時間推進，必須把同一個 clock 物件傳進來**，不然狀態機讀到的
    跟測試裡 `.advance()` 的是兩個不相干的時鐘，`tick()` 永遠看不到時間變化。
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
    df = read_manifest(manifest_path)
    assert df.iloc[0]["quality"] == "rejected"


def test_mark_saved_trial_quality_rejects_invalid_value(tmp_path):
    clock = FakeClock()
    sm, writer, aligner, h5_path, manifest_path = _make_sm(tmp_path, clock=clock)
    with pytest.raises(ValueError):
        sm.mark_current_trial_saved_quality(h5_path, 0, "discarded")


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
