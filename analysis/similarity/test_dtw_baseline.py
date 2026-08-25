import time

import numpy as np
import pytest

from analysis.features.feature_assembly import resample_fixed_length
from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.dtw_baseline import (
    batch_dtw_dist,
    dtw_dist,
    modality_dtw_dist,
)


def test_dtw_dist_identical_sequences_is_zero():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(20, 10)) + 3.0  # 偏離 0，避免 cosine 在近零向量上不穩定
    assert dtw_dist(a, a) == pytest.approx(0.0, abs=1e-9)


def test_dtw_dist_exact_uniform_stretch_is_near_zero():
    """同一段訊號整段拉長 2 倍（每幀重複一次），DTW 應該幾乎完美對齊。"""
    rng = np.random.default_rng(1)
    a = rng.normal(size=(15, 10)) + 3.0
    b = np.repeat(a, 2, axis=0)
    assert dtw_dist(a, b, band_ratio=0.2) == pytest.approx(0.0, abs=1e-6)


def test_dtw_dist_rejects_mismatched_channel_width():
    with pytest.raises(ValueError):
        dtw_dist(np.zeros((10, 4)), np.zeros((10, 5)))


def test_dtw_dist_rejects_invalid_band_ratio():
    a = np.zeros((5, 3))
    with pytest.raises(ValueError):
        dtw_dist(a, a, band_ratio=0.0)
    with pytest.raises(ValueError):
        dtw_dist(a, a, band_ratio=1.5)


def test_modality_dtw_dist_uses_only_selected_slice():
    """驗收條件：per-modality 版本正確。"""
    slices = {"tof": slice(0, 6), "mel": slice(6, 10)}
    T = 12
    a = np.zeros((T, 10)) + 3.0
    b = np.zeros((T, 10)) + 3.0
    a[:, slices["mel"]] = 5.0
    b[:, slices["mel"]] = -5.0  # mel 段完全不同

    tof_dist = modality_dtw_dist(a, b, slices, "tof")
    mel_dist = modality_dtw_dist(a, b, slices, "mel")

    assert tof_dist == pytest.approx(0.0, abs=1e-6)  # tof 段相同
    assert mel_dist > tof_dist  # mel 段的差異被正確抓到，不會被 tof 段稀釋


def test_batch_dtw_dist_matches_individual_calls_with_varying_lengths():
    rng = np.random.default_rng(2)
    query = rng.normal(size=(15, 6)) + 3.0
    templates = [rng.normal(size=(rng.integers(10, 25), 6)) + 3.0 for _ in range(10)]

    batch = batch_dtw_dist(query, templates)
    looped = np.array([dtw_dist(query, t) for t in templates])

    np.testing.assert_allclose(batch, looped)


def test_batch_dtw_dist_8x8_templates_under_100ms():
    """驗收條件：8 類 × 8 樣板的完整比對 < 100 ms。"""
    rng = np.random.default_rng(3)
    query = rng.normal(size=(20, 104)) + 3.0
    templates = [rng.normal(size=(rng.integers(15, 30), 104)) + 3.0 for _ in range(64)]

    batch_dtw_dist(query, templates)  # warm-up

    t0 = time.perf_counter()
    batch_dtw_dist(query, templates)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert elapsed_ms < 100, f"64 個樣板比對耗時 {elapsed_ms:.1f} ms，超過 100 ms"


def test_speed_doubled_word_dtw_distance_notably_smaller_than_cosine():
    """驗收條件：語速差 2 倍的同一個詞，DTW 距離明顯小於餘弦距離。

    合成資料設計：平滑、逐幀相關的基底（模擬連續的物理訊號），疊加一個
    時間上局部的「事件」（模擬詞彙的特徵性音素/唇形尖峰）。同一個詞放慢
    到 2 倍長度時，事件在整段錄音裡的相對位置通常會跟著往後飄一點
    （不是均勻拉長每一幀）——這正是固定長度重採樣沒辦法處理、而 DTW
    的非線性對齊可以處理的情境。
    """
    D = 104
    freqs = np.arange(1, D + 1) * 0.3

    def make_sequence(T, event_center_frac, event_width_frac=0.06):
        u = np.linspace(0, 1, T)
        baseline = np.sin(2 * np.pi * np.outer(u, freqs) * 0.5) + 1.5
        bump = np.exp(-0.5 * ((u - event_center_frac) / event_width_frac) ** 2)
        event = np.outer(bump, np.ones(D)) * 8.0
        return baseline + event

    a = make_sequence(15, event_center_frac=0.40)          # 正常語速
    b = make_sequence(30, event_center_frac=0.55)          # 2 倍長，事件相對位置略微後移

    d_dtw = dtw_dist(a, b, band_ratio=0.2)

    a_fixed, _ = resample_fixed_length(a, np.arange(15), t_fixed=24)
    b_fixed, _ = resample_fixed_length(b, np.arange(30), t_fixed=24)
    d_cos = cosine_dist(a_fixed, b_fixed)

    assert d_dtw < d_cos * 0.5, (
        f"DTW={d_dtw:.4f} 應明顯小於 cosine={d_cos:.4f}（< 一半），"
        "沒有明顯小於代表 DTW 沒有發揮非線性對齊的優勢"
    )
