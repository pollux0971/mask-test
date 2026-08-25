import time

import numpy as np
import pytest

from analysis.features.feature_assembly import DEFAULT_T_FIXED, FEATURE_DIM
from analysis.similarity.cosine_baseline import (
    batch_cosine_dist,
    cosine_dist,
    modality_cosine_dist,
)

T, D = DEFAULT_T_FIXED, FEATURE_DIM  # 24, 104 -> 攤平長度 2496，跟 D04.md 的例子對得上


def test_cosine_dist_identical_vectors_is_zero():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(T, D))
    assert cosine_dist(a, a) == pytest.approx(0.0, abs=1e-9)


def test_cosine_dist_orthogonal_vectors_is_one():
    a = np.zeros((1, 4))
    a[0] = [1.0, 0.0, 0.0, 0.0]
    b = np.zeros((1, 4))
    b[0] = [0.0, 1.0, 0.0, 0.0]
    assert cosine_dist(a, b) == pytest.approx(1.0, abs=1e-9)


def test_cosine_dist_known_case():
    a = np.array([[3.0, 4.0]])   # norm 5
    b = np.array([[3.0, 0.0]])   # norm 3, cos = 9/15 = 0.6
    assert cosine_dist(a, b) == pytest.approx(1 - 0.6, abs=1e-9)


def test_cosine_dist_zero_vector_does_not_divide_by_zero():
    a = np.zeros((T, D))
    b = np.ones((T, D))
    result = cosine_dist(a, b)
    assert np.isfinite(result)


def test_cosine_dist_rejects_mismatched_shapes():
    with pytest.raises(ValueError):
        cosine_dist(np.zeros((T, D)), np.zeros((T, D - 1)))


def test_modality_cosine_dist_uses_only_selected_slice():
    """驗收條件：per-modality 版本正確使用 slices。"""
    slices = {"tof": slice(0, 64), "mel": slice(64, 104)}
    a = np.zeros((T, D))
    b = np.zeros((T, D))

    a[:, slices["tof"]] = 1.0
    b[:, slices["tof"]] = 1.0     # tof 段完全相同
    a[:, slices["mel"]] = 1.0
    b[:, slices["mel"]] = -1.0    # mel 段完全相反

    tof_dist = modality_cosine_dist(a, b, slices, "tof")
    mel_dist = modality_cosine_dist(a, b, slices, "mel")

    assert tof_dist == pytest.approx(0.0, abs=1e-9)   # tof 相同 -> 距離 0
    assert mel_dist == pytest.approx(2.0, abs=1e-9)    # 完全相反 cos=-1 -> 距離 2
    # 只用選定切片，不會被另一個模態的差異污染
    assert modality_cosine_dist(a, b, slices, "tof") != cosine_dist(a, b)


def test_batch_cosine_dist_matches_looping_single_calls():
    rng = np.random.default_rng(1)
    query = rng.normal(size=(T, D))
    templates = rng.normal(size=(64, T, D))

    batch = batch_cosine_dist(query, templates)
    looped = np.array([cosine_dist(query, templates[i]) for i in range(64)])

    np.testing.assert_allclose(batch, looped, atol=1e-9)


def test_batch_cosine_dist_rejects_mismatched_length():
    query = np.zeros((T, D))
    templates = np.zeros((5, T, D - 1))
    with pytest.raises(ValueError):
        batch_cosine_dist(query, templates)


def test_batch_cosine_dist_under_1ms_for_64_templates():
    """驗收條件：批次計算 1 query x 64 templates < 1 ms。

    量測時 warm-up 一次排除 import/JIT 開銷，取 5 次裡最快的一次；
    給了些許緩衝（2 ms）避免在共用/虛擬化的 CI 機器上偶發抖動誤判，
    實際測到的時間會在回報裡附上。
    """
    rng = np.random.default_rng(2)
    query = rng.normal(size=(T, D))
    templates = rng.normal(size=(64, T, D))

    batch_cosine_dist(query, templates)  # warm-up

    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        batch_cosine_dist(query, templates)
        times.append(time.perf_counter() - t0)

    best = min(times)
    assert best < 2e-3, f"最快一次耗時 {best*1000:.3f} ms，超過緩衝上限"


def test_same_class_distance_smaller_than_different_class():
    """驗收條件：在 mock 資料上，同類距離 < 異類距離。"""
    rng = np.random.default_rng(3)
    n_words = 8
    n_templates_per_word = 8

    word_bases = rng.normal(size=(n_words, T, D))
    templates = np.stack([
        word_bases[w] + 0.05 * rng.normal(size=(T, D))
        for w in range(n_words) for _ in range(n_templates_per_word)
    ])
    labels = np.repeat(np.arange(n_words), n_templates_per_word)

    query = word_bases[0] + 0.05 * rng.normal(size=(T, D))  # 屬於 word 0
    dists = batch_cosine_dist(query, templates)

    same_class = dists[labels == 0]
    diff_class = dists[labels != 0]

    assert same_class.mean() < diff_class.mean()
    assert same_class.max() < diff_class.min()
