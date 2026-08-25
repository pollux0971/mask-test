import numpy as np

from analysis.experiments.exp_d05_dtw_vs_cosine import (
    format_report,
    generate_synthetic_dataset,
    loocv_top1_accuracy_and_latency,
    run_comparison_experiment,
)
from analysis.similarity.cosine_baseline import cosine_dist


def test_generate_synthetic_dataset_shape_and_labels():
    rng = np.random.default_rng(0)
    trials = generate_synthetic_dataset(rng, n_classes=3, n_trials_per_class=2, n_dims=10)

    assert len(trials) == 6
    labels = {label for label, _ in trials}
    assert labels == {"class_0", "class_1", "class_2"}
    for _, seq in trials:
        assert seq.shape[1] == 10
        assert 15 <= seq.shape[0] <= 30


def test_loocv_accuracy_and_latency_return_types():
    rng = np.random.default_rng(1)
    trials = generate_synthetic_dataset(rng, n_classes=2, n_trials_per_class=3, n_dims=8)

    def dist_fn(a, b):
        return cosine_dist(a[: min(len(a), len(b))], b[: min(len(a), len(b))])

    acc, latency = loocv_top1_accuracy_and_latency(trials, dist_fn)
    assert 0.0 <= acc <= 1.0
    assert latency > 0


def test_run_comparison_experiment_returns_all_expected_methods():
    """驗收條件（產出要求）：與 D04 的比較報告完成，含決策建議。

    這裡用小規模合成資料快速驗證流程本身正確（每種方法都有回傳
    (accuracy, latency)），不是驗證真實準確率數字。
    """
    results = run_comparison_experiment(n_classes=3, n_trials_per_class=4, seed=0)

    expected_methods = {"cosine (T=24)", "DTW (r=0.1)", "DTW (r=0.2)", "DTW (r=0.4)"}
    assert set(results.keys()) == expected_methods
    for acc, latency in results.values():
        assert 0.0 <= acc <= 1.0
        assert latency > 0


def test_format_report_includes_table_and_decision():
    results = run_comparison_experiment(n_classes=3, n_trials_per_class=4, seed=0)
    report = format_report(results)

    assert "LOOCV top-1" in report
    assert "決策建議" in report
    for method in results:
        assert method in report
