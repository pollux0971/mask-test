import random
import time

import pytest

from host.align.aligner import Aligner, DEFAULT_MAX_GAP_US

N_ZONES = 16


def _valid_tof_sample(distance_base=500):
    distance = [distance_base + i for i in range(N_ZONES)]
    signal = [100 + i for i in range(N_ZONES)]
    valid = [True] * N_ZONES
    return distance, signal, valid


def _push_uniform_tof(aligner, sensor, t0_us, t1_us, fps):
    period_us = 1e6 / fps
    t = t0_us
    while t <= t1_us:
        distance, signal, valid = _valid_tof_sample(distance_base=int(t % 1000))
        aligner.push_tof(sensor, int(t), distance, signal, valid)
        t += period_us


def _push_uniform_mic(aligner, t0_us, t1_us, fps):
    period_us = 1e6 / fps
    t = t0_us
    while t <= t1_us:
        aligner.push_mic(int(t), rms=300.0, peak=1000.0)
        t += period_us


def _push_uniform_mel(aligner, t0_us, t1_us, fps, n_mels=40):
    period_us = 1e6 / fps
    t = t0_us
    while t <= t1_us:
        aligner.push_mel(int(t), [0.1 * i for i in range(n_mels)])
        t += period_us


# ---------------------------------------------------------------------------
# 正常情境


def test_normal_all_modalities_present_and_frame_spacing_stable():
    """驗收條件：輸出幀的 t_us 間隔穩定在 1/rate ± 1ms；正常情境下四個模態都在。"""
    aligner = Aligner()
    _push_uniform_tof(aligner, "A", 0, 3_000_000, fps=30)
    _push_uniform_tof(aligner, "B", 0, 3_000_000, fps=30)
    _push_uniform_mic(aligner, 0, 3_000_000, fps=31.25)
    _push_uniform_mel(aligner, 0, 3_000_000, fps=62.5)

    frames = list(aligner.frames(200_000, 2_800_000, rate_hz=30))

    assert len(frames) > 10
    for f in frames:
        assert f.tof_A_present and f.tof_B_present and f.mic_present and f.mel_present

    gaps = [b.t_us - a.t_us for a, b in zip(frames, frames[1:])]
    expected_period_us = 1e6 / 30
    for g in gaps:
        assert abs(g - expected_period_us) < 1000  # ±1ms


def test_tof_values_shape_matches_zone_count():
    aligner = Aligner()
    _push_uniform_tof(aligner, "A", 0, 500_000, fps=30)

    frames = list(aligner.frames(100_000, 400_000, rate_hz=30))
    present_frames = [f for f in frames if f.tof_A_present]
    assert present_frames
    sample = present_frames[0].tof_A
    assert len(sample.values) == 2 * N_ZONES
    assert len(sample.valid) == N_ZONES


# ---------------------------------------------------------------------------
# 單模態缺失


def test_single_modality_missing_is_masked_not_zero_filled():
    """驗收條件：任一模態缺資料時，該欄位的 mask 為 False 而非填值。"""
    aligner = Aligner()
    _push_uniform_tof(aligner, "A", 0, 1_000_000, fps=30)
    _push_uniform_tof(aligner, "B", 0, 1_000_000, fps=30)
    # 完全不 push mic / mel

    frames = list(aligner.frames(200_000, 800_000, rate_hz=30))

    assert frames
    for f in frames:
        assert f.tof_A_present and f.tof_B_present
        assert not f.mic_present
        assert f.mic_rms is None and f.mic_peak is None
        assert not f.mel_present
        assert f.mel is None


def test_partial_zone_invalidity_is_masked_per_zone_not_whole_frame():
    aligner = Aligner()
    distance, signal, valid = _valid_tof_sample()
    valid[3] = False
    distance[3] = None
    signal[3] = None
    aligner.push_tof("A", 500_000, distance, signal, valid)

    frames = list(aligner.frames(500_000, 500_000, rate_hz=30))
    sample = frames[0].tof_A

    assert frames[0].tof_A_present  # 整個 frame 還是有資料
    assert sample.valid[3] is False
    assert sample.values[3] is None
    assert sample.values[3 + N_ZONES] is None
    assert sample.valid[0] is True  # 其他 zone 沒被誤傷


# ---------------------------------------------------------------------------
# 時間戳亂序


def test_out_of_order_pushes_produce_same_result_as_in_order():
    t0, t1, fps = 0, 2_000_000, 30
    period_us = 1e6 / fps

    in_order = Aligner()
    t = t0
    ordered_samples = []
    while t <= t1:
        distance, signal, valid = _valid_tof_sample(distance_base=int(t % 1000))
        ordered_samples.append((int(t), distance, signal, valid))
        t += period_us
    for t_us, d, s, v in ordered_samples:
        in_order.push_tof("A", t_us, d, s, v)

    shuffled = Aligner()
    rng = random.Random(0)
    shuffled_samples = ordered_samples[:]
    rng.shuffle(shuffled_samples)
    for t_us, d, s, v in shuffled_samples:
        shuffled.push_tof("A", t_us, d, s, v)

    frames_in_order = list(in_order.frames(200_000, 1_800_000, rate_hz=30))
    frames_shuffled = list(shuffled.frames(200_000, 1_800_000, rate_hz=30))

    assert len(frames_in_order) == len(frames_shuffled)
    for a, b in zip(frames_in_order, frames_shuffled):
        assert a.t_us == b.t_us
        assert a.tof_A_present == b.tof_A_present
        assert a.tof_A.values == b.tof_A.values


# ---------------------------------------------------------------------------
# 大 gap（模擬錄音 dump 期間 ToF 掉幀）


def test_large_gap_masks_frames_inside_it_but_not_outside():
    aligner = Aligner()
    _push_uniform_tof(aligner, "A", 0, 1_000_000, fps=30)
    # 模擬 §1.4 描述的錄音 dump：1.0s ~ 5.0s 完全沒有 $T
    _push_uniform_tof(aligner, "A", 5_000_000, 6_000_000, fps=30)

    frames = list(aligner.frames(0, 6_000_000, rate_hz=30))

    before_gap = [f for f in frames if f.t_us < 900_000]
    inside_gap = [f for f in frames if 1_300_000 < f.t_us < 4_700_000]
    after_gap = [f for f in frames if f.t_us > 5_100_000]

    assert before_gap and all(f.tof_A_present for f in before_gap)
    assert inside_gap and all(not f.tof_A_present for f in inside_gap)
    assert after_gap and all(f.tof_A_present for f in after_gap)


def test_aligner_does_not_raise_or_hang_when_a_modality_never_arrives():
    """§1.4：錄音 dump 期間 ToF 必然掉幀，對齊器要能容忍完全沒有 $T 而不卡住。"""
    aligner = Aligner()
    _push_uniform_mic(aligner, 0, 1_000_000, fps=31.25)

    frames = list(aligner.frames(0, 1_000_000, rate_hz=30))

    assert frames
    assert all(not f.tof_A_present and not f.tof_B_present for f in frames)
    assert all(f.mic_present for f in frames)


# ---------------------------------------------------------------------------
# 線性內插


def test_linear_interpolation_midpoint():
    """兩個真樣本的間隔要在 max_gap_us 容許範圍內（模擬真實模態的取樣間隔，
    例如 mic ~32ms），才會真的內插；用不合理的秒級間隔測內插沒有意義，
    那種間隔本來就該被當成缺資料處理（見 `test_gap_too_large_is_not_interpolated_across`）。"""
    aligner = Aligner()
    aligner.push_mic(0, rms=100.0, peak=200.0)
    aligner.push_mic(60_000, rms=300.0, peak=400.0)

    frames = list(aligner.frames(30_000, 30_000, rate_hz=1, interp="linear"))

    assert frames[0].mic_present
    assert frames[0].mic_rms == pytest.approx(200.0)
    assert frames[0].mic_peak == pytest.approx(300.0)


def test_linear_interpolation_respects_per_zone_validity():
    aligner = Aligner()
    d0, s0, v0 = _valid_tof_sample(distance_base=100)
    d1, s1, v1 = _valid_tof_sample(distance_base=200)
    v1[5] = False
    d1[5] = None
    s1[5] = None
    aligner.push_tof("A", 0, d0, s0, v0)
    aligner.push_tof("A", 66_666, d1, s1, v1)  # ~2 個 ToF@30Hz 週期，模擬真實間隔

    frames = list(aligner.frames(33_333, 33_333, rate_hz=1, interp="linear"))
    sample = frames[0].tof_A

    assert frames[0].tof_A_present
    assert sample.valid[5] is False
    assert sample.values[5] is None
    assert sample.valid[0] is True
    assert sample.values[0] == pytest.approx((100 + 200) / 2)


def test_linear_interpolation_exact_hit_bypasses_interpolation():
    aligner = Aligner()
    aligner.push_mic(0, rms=100.0, peak=200.0)
    aligner.push_mic(1_000_000, rms=300.0, peak=400.0)

    frames = list(aligner.frames(1_000_000, 1_000_000, rate_hz=1, interp="linear"))

    assert frames[0].mic_rms == pytest.approx(300.0)
    assert frames[0].mic_peak == pytest.approx(400.0)


def test_gap_too_large_is_not_interpolated_across():
    """兩個真樣本本身相隔太遠（超過 max_gap_us）時，中間的幀該是缺資料，
    不該被硬內插出一條看似連續、實際上跨過錄音 dump 空窗的假線。"""
    aligner = Aligner()
    aligner.push_mic(0, rms=100.0, peak=200.0)
    aligner.push_mic(3_000_000, rms=999.0, peak=999.0)  # 3 秒後才有下一筆

    frames = list(aligner.frames(1_500_000, 1_500_000, rate_hz=1, interp="linear",
                                  max_gap_us=DEFAULT_MAX_GAP_US))

    assert not frames[0].mic_present


# ---------------------------------------------------------------------------
# 例外處理 / 邊界輸入


def test_rejects_invalid_rate_hz():
    aligner = Aligner()
    with pytest.raises(ValueError):
        list(aligner.frames(0, 1000, rate_hz=0))


def test_rejects_invalid_interp():
    aligner = Aligner()
    with pytest.raises(ValueError):
        list(aligner.frames(0, 1000, interp="cubic"))


def test_empty_aligner_produces_all_missing_frames_without_crashing():
    aligner = Aligner()
    frames = list(aligner.frames(0, 100_000, rate_hz=30))
    assert frames
    assert all(not f.tof_A_present and not f.mic_present and not f.mel_present for f in frames)


# ---------------------------------------------------------------------------
# 效能


def test_processing_ten_seconds_of_data_is_fast():
    """驗收條件：處理 10 秒資料耗時 < 50 ms（只計 frames() 本身，不含 push）。"""
    aligner = Aligner()
    _push_uniform_tof(aligner, "A", 0, 10_000_000, fps=30)
    _push_uniform_tof(aligner, "B", 0, 10_000_000, fps=30)
    _push_uniform_mic(aligner, 0, 10_000_000, fps=31.25)
    _push_uniform_mel(aligner, 0, 10_000_000, fps=62.5)

    t0 = time.perf_counter()
    frames = list(aligner.frames(0, 10_000_000, rate_hz=30))
    elapsed = time.perf_counter() - t0

    assert len(frames) > 250
    assert elapsed < 0.05
