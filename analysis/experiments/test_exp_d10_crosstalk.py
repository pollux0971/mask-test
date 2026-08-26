import matplotlib
matplotlib.use("Agg")

import numpy as np
import pytest

from analysis.experiments.exp_d10_crosstalk import (
    DISTANCE_PASS_THRESHOLD_MM,
    N_ZONES,
    crosstalk_verdict,
    format_report,
    plot_crosstalk_heatmap,
    zone_ambient_delta,
    zone_distance_delta,
)
from analysis.reporting.plot_style import assert_grayscale_safe
from analysis.reporting.text_checks import assert_english_only


def _synthetic_solo_dual_hdf5_arrays(rng, n_t=200, extra_bias=None):
    """合成一組 solo/dual 距離＋valid＋ambient 陣列，模擬
    `schema_example.py` 尚未產生的 `tof_ambient_A/B/t_us`（見模組 docstring）。

    extra_bias: (16,) 可選，dual 模式額外疊加的系統性距離偏移，模擬 crosstalk。
    """
    base = 500.0 + rng.normal(0, 1.0, size=N_ZONES)  # 各 zone 的基準距離 mm
    dist_solo = base[None, :] + rng.normal(0, 0.5, size=(n_t, N_ZONES))
    bias = extra_bias if extra_bias is not None else np.zeros(N_ZONES)
    dist_dual = base[None, :] + bias[None, :] + rng.normal(0, 0.5, size=(n_t, N_ZONES))
    valid_solo = np.ones((n_t, N_ZONES), dtype=bool)
    valid_dual = np.ones((n_t, N_ZONES), dtype=bool)
    return dist_solo, valid_solo, dist_dual, valid_dual


def test_zone_distance_delta_recovers_known_bias():
    rng = np.random.default_rng(0)
    bias = np.zeros(N_ZONES)
    bias[3] = 3.5  # 只讓 zone 3 有明顯偏移，其餘理論上接近 0
    dist_solo, valid_solo, dist_dual, valid_dual = _synthetic_solo_dual_hdf5_arrays(
        rng, n_t=500, extra_bias=bias
    )

    delta = zone_distance_delta(dist_solo, valid_solo, dist_dual, valid_dual)

    assert delta.shape == (N_ZONES,)
    assert delta[3] == pytest.approx(3.5, abs=0.3)
    assert np.all(delta[np.arange(N_ZONES) != 3] < 1.0)


def test_zone_distance_delta_ignores_invalid_frames():
    n_t = 50
    dist_solo = np.full((n_t, N_ZONES), 500.0)
    dist_dual = np.full((n_t, N_ZONES), 500.0)
    valid_solo = np.ones((n_t, N_ZONES), dtype=bool)
    valid_dual = np.ones((n_t, N_ZONES), dtype=bool)

    # 在 dual 資料裡塞入離譜的無效幀，若沒被正確過濾，平均值會被污染
    dist_dual[0, :] = 99999.0
    valid_dual[0, :] = False

    delta = zone_distance_delta(dist_solo, valid_solo, dist_dual, valid_dual)

    np.testing.assert_allclose(delta, 0.0, atol=1e-9)


def test_zone_distance_delta_rejects_wrong_shape():
    with pytest.raises(ValueError):
        zone_distance_delta(
            np.zeros((10, N_ZONES - 1)), np.ones((10, N_ZONES - 1), dtype=bool),
            np.zeros((10, N_ZONES)), np.ones((10, N_ZONES), dtype=bool),
        )


def test_zone_ambient_delta_known_case():
    n_ta = 30
    ambient_solo = np.full((n_ta, N_ZONES), 10.0)
    ambient_dual = np.full((n_ta, N_ZONES), 10.0)
    ambient_dual[:, 5] = 15.0  # zone 5 上升 50%

    delta, rate = zone_ambient_delta(ambient_solo, ambient_dual)

    assert delta[5] == pytest.approx(5.0)
    assert rate[5] == pytest.approx(0.5)
    np.testing.assert_allclose(delta[np.arange(N_ZONES) != 5], 0.0)


def test_zone_ambient_delta_ignores_nan_invalid_frames():
    """CONTRACTS §2：ambient 無效 zone 一律 NaN，nanmean 要正確忽略
    （只忽略無效的那幾幀，不是整個 zone 都無效——那種情況本來就該回傳 NaN，
    見 `test_zone_ambient_delta_all_nan_zone_returns_nan`）。"""
    n_ta = 20
    ambient_solo = np.full((n_ta, N_ZONES), 10.0)
    ambient_dual = np.full((n_ta, N_ZONES), 10.0)
    ambient_solo[:5, 2] = np.nan   # zone 2 只有前 5 幀無效，其餘 15 幀仍有效
    ambient_dual[:5, 2] = 9999.0   # 若沒被 NaN 遮蔽，這裡會製造假訊號
    ambient_dual[:5, 2] = np.nan

    delta, rate = zone_ambient_delta(ambient_solo, ambient_dual)

    assert np.isfinite(delta[2])
    assert delta[2] == pytest.approx(0.0)


def test_zone_ambient_delta_all_nan_zone_returns_nan():
    """整個 zone 從頭到尾都無效時，沒有資料可以算差異，回傳 NaN 是正確行為，
    不是 bug——呼叫端（報告產生器）要自己決定怎麼呈現 NaN 的 zone。"""
    n_ta = 10
    ambient_solo = np.full((n_ta, N_ZONES), 10.0)
    ambient_dual = np.full((n_ta, N_ZONES), 10.0)
    ambient_solo[:, 2] = np.nan
    ambient_dual[:, 2] = np.nan

    delta, rate = zone_ambient_delta(ambient_solo, ambient_dual)

    assert np.isnan(delta[2])


def test_zone_ambient_delta_zero_solo_mean_does_not_divide_by_zero():
    n_ta = 10
    ambient_solo = np.zeros((n_ta, N_ZONES))
    ambient_dual = np.full((n_ta, N_ZONES), 2.0)

    delta, rate = zone_ambient_delta(ambient_solo, ambient_dual)

    assert np.all(np.isfinite(rate))


def test_crosstalk_verdict_pass_and_fail_with_recorded_threshold():
    """驗收條件：PASS/FAIL 判定明確——並記錄實際用掉的門檻值。"""
    zone_delta_pass = np.full(N_ZONES, 0.5)
    zone_delta_pass[7] = 1.9
    verdict_pass = crosstalk_verdict(zone_delta_pass)
    assert verdict_pass["passed"] is True
    assert verdict_pass["worst_zone"] == 7
    assert verdict_pass["threshold_mm"] == DISTANCE_PASS_THRESHOLD_MM

    zone_delta_fail = np.full(N_ZONES, 0.5)
    zone_delta_fail[2] = 2.5
    verdict_fail = crosstalk_verdict(zone_delta_fail)
    assert verdict_fail["passed"] is False
    assert verdict_fail["worst_zone"] == 2

    # 自訂門檻要如實記錄用的是哪一個
    verdict_custom = crosstalk_verdict(zone_delta_pass, threshold_mm=0.3)
    assert verdict_custom["passed"] is False
    assert verdict_custom["threshold_mm"] == 0.3


def test_plot_crosstalk_heatmap_draws_correct_data():
    zone_delta = np.arange(N_ZONES, dtype=float)
    fig = plot_crosstalk_heatmap(zone_delta, sensor_label="A")

    heatmap_axes = [ax for ax in fig.axes if ax.get_images()]
    assert len(heatmap_axes) == 1
    drawn = np.asarray(heatmap_axes[0].get_images()[0].get_array())
    np.testing.assert_array_equal(drawn, zone_delta.reshape(4, 4))


def test_plot_crosstalk_heatmap_text_is_english_only():
    """圖表文字一律英文（調度員規則）。D20：改用共用的 `assert_english_only`
    ——原本這裡跟 `D17` 各自寫一份一模一樣的 `has_cjk`，兩份遲早會漂掉一份。"""
    fig = plot_crosstalk_heatmap(np.arange(N_ZONES, dtype=float), sensor_label="B")
    assert_english_only(fig)


def test_plot_crosstalk_heatmap_passes_grayscale_check():
    """D20：`zone_distance_delta()` 回傳非負量值（見其 docstring 的
    `np.abs`），SEQUENTIAL_CMAP 語意正確，不需要 `diverging_opt_out`。"""
    fig = plot_crosstalk_heatmap(np.arange(N_ZONES, dtype=float), sensor_label="A")
    assert_grayscale_safe(fig)


def test_format_report_flags_synthetic_and_includes_fallback_when_failed():
    """驗收條件：ambient 變化率一併報告；失敗時列出 fallback 及其代價。"""
    verdict_pass = {"passed": True, "worst_zone": 1, "worst_delta_mm": 0.5, "threshold_mm": 2.0}
    verdict_fail = {"passed": False, "worst_zone": 4, "worst_delta_mm": 3.2, "threshold_mm": 2.0}
    rate_a = np.zeros(N_ZONES)
    rate_b = np.zeros(N_ZONES)
    rate_b[4] = 0.25

    report = format_report(verdict_pass, verdict_fail, rate_a, rate_b, is_synthetic=True)

    assert "合成資料" in report
    assert "FAIL" in report
    assert "Fallback" in report
    assert "15 Hz" in report


def test_format_report_no_fallback_when_all_pass():
    verdict_pass = {"passed": True, "worst_zone": 0, "worst_delta_mm": 0.1, "threshold_mm": 2.0}
    rate = np.zeros(N_ZONES)

    report = format_report(verdict_pass, verdict_pass, rate, rate, is_synthetic=False)

    assert "不需要 fallback" in report
    assert "合成資料" not in report
