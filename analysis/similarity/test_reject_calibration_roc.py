import matplotlib
matplotlib.use("Agg")

import numpy as np
import pytest

from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.reject_calibration_roc import (
    STRATEGY_EER,
    STRATEGY_TARGET_FAR,
    STRATEGY_TARGET_FRR,
    calibrate_threshold_roc,
    calibrate_tri_threshold_roc,
    compute_reject_distances,
    compute_roc,
    compute_word_loocv_distances,
    plot_roc_curves,
    select_threshold,
)


def _random_direction(rng, n_dims, magnitude=10.0):
    v = rng.normal(size=n_dims)
    return v / np.linalg.norm(v) * magnitude


def _make_trial(rng, center, T=3, noise=0.15):
    return center[None, :] + noise * rng.normal(size=(T, center.shape[0]))


def test_compute_word_loocv_distances_known_case():
    """人工案例：類別內樣板彼此距離已知，驗證回傳的是每筆樣板扣掉自己
    之後、跟同類別其餘樣板的最小距離。"""
    def scalar_dist(a, b):
        return abs(float(a) - float(b))

    templates_by_class = {"w0": [0.0, 0.1, 5.0]}  # 0.0 跟 0.1 最近，5.0 離群
    distances = compute_word_loocv_distances(templates_by_class, scalar_dist)

    assert sorted(distances) == pytest.approx(sorted([0.1, 0.1, 4.9]))


def test_compute_reject_distances_known_case():
    def scalar_dist(a, b):
        return abs(float(a) - float(b))

    templates_by_class = {"w0": [0.0, 0.1], "w1": [10.0, 10.1]}
    reject_templates = [5.0, 0.05]  # 5.0 離兩類都遠；0.05 幾乎貼著 w0

    distances = compute_reject_distances(reject_templates, templates_by_class, scalar_dist)

    assert distances[0] == pytest.approx(min(abs(5.0 - 0.0), abs(5.0 - 0.1), abs(5.0 - 10.0), abs(5.0 - 10.1)))
    assert distances[1] == pytest.approx(0.05)


def test_compute_roc_frr_and_far_are_monotonic_in_expected_directions():
    word_distances = np.array([0.1, 0.2, 0.3, 0.4])
    reject_distances = np.array([0.5, 0.6, 0.7, 0.8])
    thresholds, frr, far = compute_roc(word_distances, reject_distances)

    # τ 越大越寬鬆：FRR 應該不增（非嚴格遞減也可能持平），FAR 不減
    assert np.all(np.diff(frr) <= 0)
    assert np.all(np.diff(far) >= 0)
    # 兩個分布完全分開時，應該存在一個門檻讓 FRR=0 且 FAR=0
    assert np.any((frr == 0) & (far == 0))


def test_select_threshold_target_frr_picks_smallest_theta_meeting_target():
    thresholds = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    frr = np.array([0.5, 0.3, 0.1, 0.05, 0.0])
    far = np.array([0.0, 0.05, 0.1, 0.3, 0.5])

    theta, actual_frr, actual_far = select_threshold(thresholds, frr, far, strategy=STRATEGY_TARGET_FRR, target=0.1)

    assert theta == pytest.approx(0.3)
    assert actual_frr == pytest.approx(0.1)


def test_select_threshold_target_far_picks_largest_theta_meeting_target():
    thresholds = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    frr = np.array([0.5, 0.3, 0.1, 0.05, 0.0])
    far = np.array([0.0, 0.05, 0.1, 0.3, 0.5])

    theta, actual_frr, actual_far = select_threshold(thresholds, frr, far, strategy=STRATEGY_TARGET_FAR, target=0.1)

    assert theta == pytest.approx(0.3)
    assert actual_far == pytest.approx(0.1)


def test_select_threshold_eer_picks_closest_frr_far_crossing():
    thresholds = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    frr = np.array([0.9, 0.6, 0.4, 0.1, 0.0])
    far = np.array([0.0, 0.1, 0.4, 0.6, 0.9])

    theta, actual_frr, actual_far = select_threshold(thresholds, frr, far, strategy=STRATEGY_EER)

    assert theta == pytest.approx(0.3)  # frr=0.4, far=0.4 相等


def test_select_threshold_rejects_unknown_strategy():
    with pytest.raises(ValueError):
        select_threshold(np.array([0.1]), np.array([0.5]), np.array([0.5]), strategy="not_a_strategy")


def test_calibrate_threshold_roc_end_to_end_separates_well_separated_data():
    rng = np.random.default_rng(0)
    n_dims = 12
    word_center = _random_direction(rng, n_dims)
    reject_center = _random_direction(rng, n_dims)
    templates_by_class = {"w0": [_make_trial(rng, word_center) for _ in range(20)]}
    reject_templates = [_make_trial(rng, reject_center) for _ in range(20)]

    result = calibrate_threshold_roc(templates_by_class, reject_templates, cosine_dist, strategy=STRATEGY_EER)

    assert result["frr"] < 0.2
    assert result["far"] < 0.2
    assert result["n_word_samples"] == 20
    assert result["n_reject_samples"] == 20
    assert result["calibration_ms"] >= 0.0


def test_calibrate_tri_threshold_roc_runs_independently_per_modality():
    """調度員特別交代：theta_reject_tof / theta_reject_mel 各自獨立跑。"""
    rng = np.random.default_rng(1)
    n_dims = 12
    slices = {"tof": slice(0, 8), "mel": slice(8, 12)}
    word_center = _random_direction(rng, n_dims)
    reject_center = _random_direction(rng, n_dims)
    templates_by_class = {"w0": [_make_trial(rng, word_center) for _ in range(20)]}
    reject_templates = [_make_trial(rng, reject_center) for _ in range(20)]

    result = calibrate_tri_threshold_roc(templates_by_class, reject_templates, slices, cosine_dist)

    assert "tof" in result and "mel" in result
    assert result["tof"]["theta"] != result["mel"]["theta"]


def test_plot_roc_curves_draws_two_curves_english_only():
    rng = np.random.default_rng(2)
    n_dims = 12
    word_center = _random_direction(rng, n_dims)
    reject_center = _random_direction(rng, n_dims)
    templates_by_class = {"w0": [_make_trial(rng, word_center) for _ in range(15)]}
    reject_templates = [_make_trial(rng, reject_center) for _ in range(15)]

    roc_tof = calibrate_threshold_roc(templates_by_class, reject_templates, cosine_dist)
    roc_mel = calibrate_threshold_roc(templates_by_class, reject_templates, cosine_dist)
    fig = plot_roc_curves(roc_tof, roc_mel)

    line_axes = [ax for ax in fig.axes if ax.lines]
    assert len(line_axes) == 1
    assert len(line_axes[0].lines) == 2

    texts = [fig._suptitle.get_text()] if fig._suptitle else []
    for ax in fig.axes:
        texts.append(ax.get_title())
        texts.append(ax.get_xlabel())
        texts.append(ax.get_ylabel())

    def has_cjk(s):
        return any("一" <= ch <= "鿿" for ch in s)

    for t in texts:
        assert not has_cjk(t), f"圖表文字含 CJK 字元: {t!r}"
