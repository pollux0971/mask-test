import time

import numpy as np
import pytest

from analysis.features.feature_assembly import DEFAULT_T_FIXED, FEATURE_DIM
from analysis.similarity.euclidean_baseline import (
    batch_euclidean_dist,
    euclidean_dist,
    modality_euclidean_dist,
)

T, D = DEFAULT_T_FIXED, FEATURE_DIM  # 24, 104


def test_euclidean_dist_identical_vectors_is_zero():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(T, D))
    assert euclidean_dist(a, a) == pytest.approx(0.0, abs=1e-9)


def test_euclidean_dist_known_case():
    a = np.array([[0.0, 0.0]])
    b = np.array([[3.0, 4.0]])   # 3-4-5 直角三角形
    assert euclidean_dist(a, b) == pytest.approx(5.0, abs=1e-9)


def test_euclidean_dist_rejects_mismatched_shapes():
    with pytest.raises(ValueError):
        euclidean_dist(np.zeros((T, D)), np.zeros((T, D - 1)))


def test_euclidean_dist_scales_with_magnitude_unlike_cosine():
    """驗收條件：跟 cosine 不同，歐式距離對「大小」敏感，不只看方向。"""
    a = np.array([[1.0, 0.0]])
    b = np.array([[2.0, 0.0]])   # 同方向，長度不同 -> cosine=0 但 euclidean != 0
    assert euclidean_dist(a, b) == pytest.approx(1.0, abs=1e-9)


def test_modality_euclidean_dist_uses_only_selected_slice():
    slices = {"tof": slice(0, 64), "mel": slice(64, 104)}
    a = np.zeros((T, D))
    b = np.zeros((T, D))

    a[:, slices["tof"]] = 1.0
    b[:, slices["tof"]] = 1.0     # tof 段完全相同
    a[:, slices["mel"]] = 1.0
    b[:, slices["mel"]] = -1.0    # mel 段差 2.0 每個元素

    tof_dist = modality_euclidean_dist(a, b, slices, "tof")
    mel_dist = modality_euclidean_dist(a, b, slices, "mel")

    assert tof_dist == pytest.approx(0.0, abs=1e-9)
    n_mel_elements = T * (slices["mel"].stop - slices["mel"].start)
    assert mel_dist == pytest.approx(np.sqrt(n_mel_elements * 2.0 ** 2), abs=1e-6)
    assert modality_euclidean_dist(a, b, slices, "tof") != euclidean_dist(a, b)


def test_batch_euclidean_dist_matches_looping_single_calls():
    rng = np.random.default_rng(1)
    query = rng.normal(size=(T, D))
    templates = rng.normal(size=(64, T, D))

    batch = batch_euclidean_dist(query, templates)
    looped = np.array([euclidean_dist(query, templates[i]) for i in range(64)])

    np.testing.assert_allclose(batch, looped, atol=1e-9)


def test_batch_euclidean_dist_rejects_mismatched_length():
    query = np.zeros((T, D))
    templates = np.zeros((5, T, D - 1))
    with pytest.raises(ValueError):
        batch_euclidean_dist(query, templates)


def test_batch_euclidean_dist_under_1ms_for_64_templates():
    """驗收條件跟 cosine 版一樣（見 test_cosine_baseline.py）：矩陣運算，
    不逐筆迴圈，1 query x 64 templates 應該遠低於 1 ms，給 2 ms 緩衝。"""
    rng = np.random.default_rng(2)
    query = rng.normal(size=(T, D))
    templates = rng.normal(size=(64, T, D))

    batch_euclidean_dist(query, templates)  # warm-up

    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        batch_euclidean_dist(query, templates)
        times.append(time.perf_counter() - t0)

    best = min(times)
    assert best < 2e-3, f"最快一次耗時 {best*1000:.3f} ms，超過緩衝上限"


def test_same_class_distance_smaller_than_different_class():
    rng = np.random.default_rng(3)
    n_words = 8
    n_templates_per_word = 8

    word_bases = rng.normal(size=(n_words, T, D))
    templates = np.stack([
        word_bases[w] + 0.05 * rng.normal(size=(T, D))
        for w in range(n_words) for _ in range(n_templates_per_word)
    ])
    labels = np.repeat(np.arange(n_words), n_templates_per_word)

    query = word_bases[0] + 0.05 * rng.normal(size=(T, D))
    dists = batch_euclidean_dist(query, templates)

    same_class = dists[labels == 0]
    diff_class = dists[labels != 0]

    assert same_class.mean() < diff_class.mean()
    assert same_class.max() < diff_class.min()
