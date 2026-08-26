import matplotlib
matplotlib.use("Agg")

from analysis.experiments.exp_d22_reject_calibration import (
    format_report,
    sweep_compare_methods,
    sweep_imbalanced_ratio,
)


def test_sweep_compare_methods_returns_both_methods_same_shape():
    result = sweep_compare_methods(ns=(10, 30), n_trials=20, geometry_seeds=(0, 1))
    assert set(result.keys()) == {"loo_single", "roc"}
    assert len(result["loo_single"]) == len(result["roc"]) == 2
    for method_rows in result.values():
        for row in method_rows:
            assert "tof" in row and "mel" in row
            for modality in ("tof", "mel"):
                assert 0.0 <= row[modality]["false_reject_rate"] <= 1.0
                assert row[modality]["calibration_ms"] >= 0.0


def test_sweep_imbalanced_ratio_covers_all_ratios_for_both_methods():
    result = sweep_imbalanced_ratio(n=15, ratios=(0.5, 1.0, 2.0), n_trials=15, geometry_seeds=(0,))
    assert set(result.keys()) == {"loo_single", "roc"}
    for method_rows in result.values():
        assert [r["ratio"] for r in method_rows] == [0.5, 1.0, 2.0]
        for row in method_rows:
            assert 0.0 <= row["tof"]["false_reject_rate"] <= 1.0
            assert 0.0 <= row["mel"]["false_reject_rate"] <= 1.0


def test_format_report_includes_both_methods_and_conclusion():
    compare_rows = sweep_compare_methods(ns=(10, 30), n_trials=20, geometry_seeds=(0, 1))
    imbalance_rows = sweep_imbalanced_ratio(n=15, ratios=(1.0,), n_trials=15, geometry_seeds=(0,))

    report = format_report(compare_rows, imbalance_rows, is_synthetic=True)

    assert "舊方法" in report and "新方法" in report
    assert "結論" in report
    assert "真實資料複驗" in report
