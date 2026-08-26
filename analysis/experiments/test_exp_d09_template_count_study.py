import matplotlib
matplotlib.use("Agg")

import numpy as np

from analysis.experiments.exp_d09_template_count_study import (
    FALSE_REJECT_TARGET,
    FEATURE_DIM,
    MEL_DIM,
    TOF_DIM,
    build_synthetic_dataset,
    format_report,
    measure_reject_rates,
    plot_false_reject_vs_n,
    sweep_percentile,
    sweep_template_counts,
)
from analysis.reporting.text_checks import assert_english_only
from analysis.similarity.cosine_baseline import cosine_dist


def test_build_synthetic_dataset_shapes():
    rng = np.random.default_rng(0)
    templates_by_class, reject_templates, centers, reject_center = build_synthetic_dataset(
        rng, n_per_class=5, n_reject=6, n_classes=3
    )
    assert len(templates_by_class) == 3
    assert all(len(ts) == 5 for ts in templates_by_class.values())
    assert len(reject_templates) == 6
    for ts in templates_by_class.values():
        assert ts[0].shape[1] == FEATURE_DIM
    assert TOF_DIM + MEL_DIM == FEATURE_DIM


def test_measure_reject_rates_returns_both_modalities_with_valid_rates():
    rng = np.random.default_rng(1)
    templates_by_class, reject_templates, centers, reject_center = build_synthetic_dataset(
        rng, n_per_class=30, n_reject=30, n_classes=4
    )
    result = measure_reject_rates(
        templates_by_class, reject_templates, centers, reject_center,
        cosine_dist, percentile=95.0, rng=rng, n_trials=30,
    )
    assert set(result.keys()) == {"tof", "mel"}
    for modality in ("tof", "mel"):
        assert 0.0 <= result[modality]["false_reject_rate"] <= 1.0
        assert 0.0 <= result[modality]["correct_reject_rate"] <= 1.0
        assert np.isfinite(result[modality]["theta"])


def test_sweep_template_counts_returns_averaged_rows_across_geometries():
    """驗證掃描機制本身（用小規模、少量幾何快速跑）：結構正確、
    有對多組幾何取平均（不是只跑一組幾何的原始數字）。"""
    rows = sweep_template_counts(ns=(10, 60), n_trials=30, geometry_seeds=(0, 1))
    assert len(rows) == 2
    for row in rows:
        assert "tof" in row and "mel" in row
        assert 0.0 <= row["loocv_acc"] <= 1.0
        assert 0.0 <= row["tof"]["false_reject_rate"] <= 1.0
        assert row["loocv_n_evaluated"] == row["n"] * 8  # N_CLASSES=8


def test_sweep_percentile_produces_row_per_n_and_percentile_combo():
    rows = sweep_percentile(ns=(20,), percentiles=(80.0, 95.0), n_trials=20, geometry_seeds=(0, 1))
    assert len(rows) == 2
    assert {r["percentile"] for r in rows} == {80.0, 95.0}
    for r in rows:
        assert 0.0 <= r["tof"]["false_reject_rate"] <= 1.0
        assert 0.0 <= r["mel"]["false_reject_rate"] <= 1.0


def test_plot_false_reject_vs_n_draws_two_lines_english_only():
    rows = sweep_template_counts(ns=(10, 30), n_trials=20, geometry_seeds=(0,))
    fig = plot_false_reject_vs_n(rows)

    line_axes = [ax for ax in fig.axes if ax.lines]
    assert len(line_axes) == 1
    assert len(line_axes[0].lines) >= 2  # ToF、Mel（可能還有 target 虛線）

    # D20：改用共用的 `assert_english_only`（涵蓋 legend，原本這裡的本地
    # `has_cjk` 檢查沒有掃 legend），不再各自維護一份會漂掉的字面實作。
    assert_english_only(fig)


def test_format_report_flags_synthetic_and_gives_concrete_recommendation():
    count_rows = [
        {"n": 10, "loocv_acc": 0.9, "loocv_n_evaluated": 80,
         "tof": {"false_reject_rate": 0.5, "correct_reject_rate": 0.9, "theta": 0.1},
         "mel": {"false_reject_rate": 0.4, "correct_reject_rate": 0.9, "theta": 0.1}},
        {"n": 50, "loocv_acc": 0.99, "loocv_n_evaluated": 400,
         "tof": {"false_reject_rate": 0.05, "correct_reject_rate": 0.95, "theta": 0.2},
         "mel": {"false_reject_rate": 0.03, "correct_reject_rate": 0.95, "theta": 0.2}},
    ]
    percentile_rows = [
        {"n": 10, "percentile": 95.0,
         "tof": {"false_reject_rate": 0.5, "correct_reject_rate": 0.9, "theta": 0.1},
         "mel": {"false_reject_rate": 0.4, "correct_reject_rate": 0.9, "theta": 0.1}},
    ]

    report = format_report(count_rows, percentile_rows, is_synthetic=True)

    assert "合成資料" in report
    assert "建議每個詞錄" in report
    assert "50" in report  # min_n meeting target


def test_format_report_notes_when_no_n_meets_target_but_trend_visible():
    """誤拒率隨 n 有明顯下降趨勢、但還沒壓到目標以下——用「建議測更大的 n」。"""
    count_rows = [
        {"n": 10, "loocv_acc": 0.5, "loocv_n_evaluated": 80,
         "tof": {"false_reject_rate": 0.9, "correct_reject_rate": 0.9},
         "mel": {"false_reject_rate": 0.9, "correct_reject_rate": 0.9}},
        {"n": 100, "loocv_acc": 0.9, "loocv_n_evaluated": 800,
         "tof": {"false_reject_rate": 0.15, "correct_reject_rate": 0.9},
         "mel": {"false_reject_rate": 0.15, "correct_reject_rate": 0.9}},
    ]
    report = format_report(count_rows, [], is_synthetic=True)
    assert "沒有任何" in report
    assert "校準方法本身" not in report


def test_format_report_flags_flat_false_reject_rate_across_n():
    """調度員交代的核心發現：誤拒率幾乎不隨 n 改變時，不能建議「多錄樣板」，
    要明確指出可能是校準方法本身的限制。"""
    count_rows = [
        {"n": n, "loocv_acc": 1.0, "loocv_n_evaluated": n * 8,
         "tof": {"false_reject_rate": 0.33, "correct_reject_rate": 1.0},
         "mel": {"false_reject_rate": 0.47, "correct_reject_rate": 1.0}}
        for n in (10, 20, 30, 50, 100)
    ]
    report = format_report(count_rows, [], is_synthetic=True)
    assert "校準方法本身" in report
    assert "不建議只靠「多錄樣板」" in report
