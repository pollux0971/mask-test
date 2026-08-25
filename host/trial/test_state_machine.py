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
