"""D05 — DTW vs D04 餘弦的比較報告產生器。

規格見 `stories/D-analysis/D05.md`：「若 DTW 的提升 < 3 個百分點，就用餘弦」。

**這裡的資料全部是合成的（見 `generate_synthetic_dataset`），不是真實錄音。**
真正的決策要等真實 enrollment 資料 + `D08` 的 LOOCV 才能下——這份報告的目的
是把「怎麼比」的流程與報告格式先建好、驗證合成資料上邏輯正確，讓 D08
接手時只需要換資料來源，不用重新設計比較方法。合成資料刻意讓詞彙之間
可分、同一詞語速有變化，用來檢查 DTW 在語速變化下是否至少不劣於餘弦，
不是用來宣稱「DTW 真的比較準」。
"""
import time

import numpy as np

from analysis.features.feature_assembly import resample_fixed_length
from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.dtw_baseline import dtw_dist
from analysis.similarity.scoring import class_distances

MIN_IMPROVEMENT_PP = 3.0  # D05.md：提升 < 3 個百分點就用餘弦


def generate_synthetic_dataset(rng, n_classes=8, n_trials_per_class=8, n_dims=104):
    """每個類別一個獨特的基底相位 + 事件位置；每筆 trial 幀數隨機（模擬語速
    變化），事件的相對位置也有小幅抖動（模擬非均勻的局部時序差異）。

    回傳 list of (label, sequence)。
    """
    freqs = np.arange(1, n_dims + 1) * 0.3
    trials = []
    for c in range(n_classes):
        class_phase = c * 0.37
        event_center_base = 0.30 + 0.05 * c
        for _ in range(n_trials_per_class):
            T = int(rng.integers(15, 31))
            event_center = event_center_base + rng.normal(0, 0.03)
            u = np.linspace(0, 1, T)
            baseline = np.sin(2 * np.pi * np.outer(u, freqs) * 0.5 + class_phase) + 1.5
            bump = np.exp(-0.5 * ((u - event_center) / 0.06) ** 2)
            seq = baseline + np.outer(bump, np.ones(n_dims)) * 8.0
            seq = seq + 0.05 * rng.normal(size=seq.shape)
            trials.append((f"class_{c}", seq))
    return trials


def loocv_top1_accuracy_and_latency(trials, dist_fn, preprocess=None):
    """Leave-one-out：每筆當 query，其餘依 label 分組當 templates，
    用 D06 的 `class_distances`（min-per-class）選最近類別。

    preprocess: 可選，套用在每筆序列上（例如餘弦法要先重採樣到固定長度）。
    回傳 (accuracy, 平均單次比對耗時秒數)。單次比對耗時只計
    `class_distances` 本身（含該筆 query 與所有其他樣板的距離計算）。
    """
    processed = [(label, preprocess(seq) if preprocess else seq) for label, seq in trials]

    correct = 0
    latencies = []
    for i, (true_label, query) in enumerate(processed):
        templates_by_class = {}
        for j, (label, seq) in enumerate(processed):
            if j == i:
                continue
            templates_by_class.setdefault(label, []).append(seq)

        t0 = time.perf_counter()
        classes, d_class = class_distances(query, templates_by_class, dist_fn)
        latencies.append(time.perf_counter() - t0)

        pred = classes[int(np.argmin(d_class))]
        correct += int(pred == true_label)

    return correct / len(processed), float(np.mean(latencies))


def _cosine_preprocess(seq):
    t_us = np.arange(seq.shape[0])
    fixed, _ = resample_fixed_length(seq, t_us, t_fixed=24)
    return fixed


def run_comparison_experiment(n_classes=8, n_trials_per_class=8, seed=0):
    """回傳 dict：{method_name: (accuracy, avg_latency_seconds)}。"""
    rng = np.random.default_rng(seed)
    trials = generate_synthetic_dataset(rng, n_classes, n_trials_per_class)

    results = {}
    results["cosine (T=24)"] = loocv_top1_accuracy_and_latency(
        trials, cosine_dist, preprocess=_cosine_preprocess
    )
    for r in (0.1, 0.2, 0.4):
        results[f"DTW (r={r})"] = loocv_top1_accuracy_and_latency(
            trials, lambda a, b, _r=r: dtw_dist(a, b, band_ratio=_r)
        )
    return results


def format_report(results):
    """依 D05.md 的表格格式輸出 markdown，含決策建議。"""
    lines = [
        "# D05 — DTW vs 餘弦 比較報告（合成資料，非真實準確率）",
        "",
        "**資料來源：合成資料**（`generate_synthetic_dataset`），"
        "只驗證比較流程與 DTW 在語速變化下的行為，不是真實辨識率。"
        "真實決策待 `D08` 用真實 enrollment 資料重跑本報告的方法。",
        "",
        "| 方法 | LOOCV top-1 | 單次比對耗時 |",
        "|---|---|---|",
    ]
    cosine_acc = results["cosine (T=24)"][0]
    best_dtw_method, best_dtw_acc = None, -1.0
    for method, (acc, lat) in results.items():
        lines.append(f"| {method} | {acc:.1%} | {lat * 1000:.2f} ms |")
        if method.startswith("DTW") and acc > best_dtw_acc:
            best_dtw_method, best_dtw_acc = method, acc

    improvement_pp = (best_dtw_acc - cosine_acc) * 100
    lines += ["", "## 決策建議"]
    if improvement_pp < MIN_IMPROVEMENT_PP:
        lines.append(
            f"最佳 DTW（{best_dtw_method}）比餘弦只提升 {improvement_pp:.1f} 個百分點，"
            f"低於 {MIN_IMPROVEMENT_PP} 的門檻——**照 D05.md 的原則應該用餘弦**"
            "（Demo 即時性優先，複雜度不值得）。**但這是合成資料上的結果**，"
            "合成資料刻意讓所有類別可分，不代表真實資料的語速變化幅度與雜訊水準；"
            "真實決策仍要等 D08 用真實資料重跑。"
        )
    else:
        lines.append(
            f"最佳 DTW（{best_dtw_method}）比餘弦提升 {improvement_pp:.1f} 個百分點，"
            f"超過 {MIN_IMPROVEMENT_PP} 的門檻——**建議用 DTW**，"
            "但仍要留意單次比對耗時是否符合 C17 滑桿 < 50 ms 的即時性要求，"
            "且這是合成資料結果，真實決策待 D08 用真實資料重跑。"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    from pathlib import Path

    results = run_comparison_experiment()
    report = format_report(results)
    print(report)

    out_path = Path(__file__).with_name("d05_dtw_vs_cosine_report.md")
    out_path.write_text(report + "\n")
    print(f"\n報告已寫入 {out_path}")
