import numpy as np
import pytest

from host.features.dtw_compare import dtw_distance, pearson_corr


def _tone_log_mel(freq_bin, n_frames=50, n_mels=40, noise=0.0, rng=None):
    """合成一段「音型固定在某個 mel band 附近」的假 log-mel，模擬同一個詞
    的多次錄音（同 freq_bin，加一點雜訊模擬錄音間的微小差異）。"""
    rng = rng or np.random.default_rng(0)
    frames = np.zeros((n_frames, n_mels), dtype=np.float32)
    band = np.exp(-0.5 * ((np.arange(n_mels) - freq_bin) / 2.0) ** 2)
    frames[:] = band
    if noise:
        frames += rng.normal(scale=noise, size=frames.shape)
    return frames


def test_same_word_distance_smaller_than_cross_word_distance():
    """驗收條件：同一個詞錄 5 次，彼此的 DTW 距離顯著小於跨詞距離。"""
    rng = np.random.default_rng(42)

    word_a_takes = [_tone_log_mel(freq_bin=10, noise=0.05, rng=rng) for _ in range(5)]
    word_b_takes = [_tone_log_mel(freq_bin=30, noise=0.05, rng=rng) for _ in range(5)]

    same_word_distances = [
        dtw_distance(word_a_takes[i], word_a_takes[j])
        for i in range(5) for j in range(i + 1, 5)
    ]
    cross_word_distances = [
        dtw_distance(a, b) for a in word_a_takes for b in word_b_takes
    ]

    assert max(same_word_distances) < min(cross_word_distances)


def test_dtw_distance_handles_different_lengths():
    a = _tone_log_mel(freq_bin=10, n_frames=40)
    b = _tone_log_mel(freq_bin=10, n_frames=60)
    d = dtw_distance(a, b)
    assert d >= 0.0
    assert np.isfinite(d)


def test_dtw_distance_rejects_mismatched_band_count():
    a = np.zeros((10, 40))
    b = np.zeros((10, 39))
    with pytest.raises(ValueError):
        dtw_distance(a, b)


def test_pearson_corr_identical_arrays_is_one():
    a = np.random.default_rng(0).normal(size=(20, 40))
    assert pearson_corr(a, a) == pytest.approx(1.0)


def test_pearson_corr_rejects_shape_mismatch():
    a = np.zeros((10, 40))
    b = np.zeros((11, 40))
    with pytest.raises(ValueError):
        pearson_corr(a, b)
