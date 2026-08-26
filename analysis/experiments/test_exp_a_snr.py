import matplotlib
matplotlib.use("Agg")  # 無頭環境，不需要真的顯示視窗

import numpy as np
import pytest

from analysis.experiments.exp_a_snr import (
    N_ZONES,
    SIGMA_FLOOR,
    VERDICT_ADJUST,
    VERDICT_DIAGNOSE,
    VERDICT_PASS,
    overall_snr,
    plot_zone_snr_heatmaps,
    symmetry,
    three_way_verdict,
    zone_snr,
    zone_snr_grid,
)
from analysis.features.tof_features import active_zone_indices


def test_zone_snr_known_case():
    """人工案例：round/spread 平均值差 4，baseline std 2 -> SNR 應為 2。"""
    rng = np.random.default_rng(1)
    baseline = rng.normal(loc=100.0, scale=2.0, size=(2000, N_ZONES))
    round_trials = np.full((30, N_ZONES), 104.0)
    spread_trials = np.full((30, N_ZONES), 100.0)

    snr = zone_snr(baseline, round_trials, spread_trials)

    assert snr.shape == (N_ZONES,)
    np.testing.assert_allclose(snr, 2.0, atol=0.15)


def test_zone_snr_zero_baseline_std_no_nan_or_inf():
    baseline = np.full((10, N_ZONES), 50.0)  # std = 0
    round_trials = np.full((5, N_ZONES), 55.0)
    spread_trials = np.full((5, N_ZONES), 50.0)

    snr = zone_snr(baseline, round_trials, spread_trials)

    assert np.all(np.isfinite(snr))
    # sigma floor 到 SIGMA_FLOOR（量化雜訊的理論下限 1/√12，不是任意小數）
    np.testing.assert_allclose(snr, 5.0 / SIGMA_FLOOR)


def test_zone_snr_rejects_wrong_shape():
    baseline = np.zeros((10, N_ZONES))
    with pytest.raises(ValueError):
        zone_snr(baseline, np.zeros((5, N_ZONES - 1)), np.zeros((5, N_ZONES)))


def test_overall_snr_is_mean_of_zone_snr():
    zone = np.array([1.0, 2.0, 3.0, 4.0] + [0.0] * (N_ZONES - 4))
    assert overall_snr(zone) == pytest.approx(zone.mean())


def test_symmetry_known_values():
    """驗收條件：對稱性 |SNR_L - SNR_R| / max 計算正確。"""
    snr_l = np.array([4.0, 10.0, 0.0])
    snr_r = np.array([2.0, 10.0, 0.0])

    result = symmetry(snr_l, snr_r)

    np.testing.assert_allclose(result[0], 2.0 / 4.0)   # |4-2|/max(4,2) = 0.5
    np.testing.assert_allclose(result[1], 0.0)          # 完全對稱
    assert np.isfinite(result[2])                       # 兩邊皆 0：靠 floor 保護不炸掉


def test_symmetry_scalar_overall_snr():
    assert symmetry(6.0, 3.0) == pytest.approx(0.5)


def test_zone_snr_grid_reshape_row_major():
    zone = np.arange(N_ZONES)
    grid = zone_snr_grid(zone)
    assert grid.shape == (4, 4)
    np.testing.assert_array_equal(grid[0], [0, 1, 2, 3])
    np.testing.assert_array_equal(grid[3], [12, 13, 14, 15])


def test_zone_snr_grid_rejects_wrong_count():
    with pytest.raises(ValueError):
        zone_snr_grid(np.zeros(15))


@pytest.mark.parametrize(
    "snr_a, snr_b, threshold, expected",
    [
        (5.0, 5.0, 3.0, VERDICT_PASS),
        (5.0, 1.0, 3.0, VERDICT_ADJUST),
        (1.0, 5.0, 3.0, VERDICT_ADJUST),
        (1.0, 1.0, 3.0, VERDICT_DIAGNOSE),
        (3.0, 3.0, 3.0, VERDICT_PASS),  # 邊界：等於門檻算通過
    ],
)
def test_three_way_verdict(snr_a, snr_b, threshold, expected):
    """驗收條件：三分法判定與對應處置建議。"""
    verdict, action, detail = three_way_verdict(snr_a, snr_b, threshold)
    assert verdict == expected
    assert action  # 有對應的中文處置建議字串
    assert detail["snr_a"] == snr_a
    assert detail["snr_b"] == snr_b


def test_active_zone_indices_directly_consumes_zone_snr_output():
    """驗收條件：活躍 zone 索引輸出供 D01 使用——確認介面直接相容不需轉換。"""
    rng = np.random.default_rng(2)
    baseline = rng.normal(loc=0.0, scale=1.0, size=(500, N_ZONES))
    round_trials = np.zeros((10, N_ZONES))
    spread_trials = np.zeros((10, N_ZONES))
    # 讓 zone 2 和 5 有明顯訊號，其他 zone 沒有
    round_trials[:, 2] = 10.0
    round_trials[:, 5] = 10.0

    snr = zone_snr(baseline, round_trials, spread_trials)

    # 不做任何轉換，直接把 D11 的輸出餵給 D01 的函式
    idx = active_zone_indices(snr, threshold=2.0)

    assert 2 in idx
    assert 5 in idx


def test_plot_zone_snr_heatmaps_runs_and_assigns_correct_data():
    """驗收條件：逐 zone SNR 熱力圖（距離 + signal rate 各一張）。"""
    snr_distance = np.arange(N_ZONES, dtype=float)
    snr_signal = np.arange(N_ZONES, dtype=float)[::-1]

    fig = plot_zone_snr_heatmaps(snr_distance, snr_signal, threshold=2.0)

    assert len(fig.axes) == 2 * 2  # 每張熱力圖各帶一個 colorbar 軸
    heatmap_axes = [ax for ax in fig.axes if ax.get_images()]
    assert len(heatmap_axes) == 2

    drawn_distance = heatmap_axes[0].get_images()[0].get_array()
    drawn_signal = heatmap_axes[1].get_images()[0].get_array()
    np.testing.assert_array_equal(np.asarray(drawn_distance), snr_distance.reshape(4, 4))
    np.testing.assert_array_equal(np.asarray(drawn_signal), snr_signal.reshape(4, 4))
