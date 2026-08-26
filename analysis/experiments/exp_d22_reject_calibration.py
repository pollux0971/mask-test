"""D22 — 拒識門檻校準：舊方法（單邊 LOO）vs 新方法（雙邊 ROC）並列比較。

規格見 `stories/D-analysis/D22.md`。重用 `D09` 的掃描框架
（`generate_centers()`/`sample_templates()`——固定類別幾何、對多組獨立
幾何取平均，避免 `D09` 踩過的兩個坑：①用同一個種子對每個 n 重新生成
中心導致幾何隨 n 改變；②單一組幾何的抽樣運氣主導結論，見
`exp_d09_template_count_study.py` 的模組 docstring），對同一張「樣板數
vs 誤拒率」表跑兩種校準方法，直接比較。

**這是合成資料，結論帶安全係數，待 `E05` 真實資料複驗**——如果新方法
在合成資料上也沒有改善，那本身就是有效的交付結論（誠實回報，不是失敗）。
"""
import time

import numpy as np

from analysis.experiments.exp_d09_template_count_study import (
    DEFAULT_GEOMETRY_SEEDS,
    DEFAULT_N_SWEEP,
    SLICES,
    _make_trial,
    generate_centers,
    sample_templates,
)
from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.enrollment import calibrate_reject_threshold
from analysis.similarity.reject_calibration_roc import STRATEGY_EER, calibrate_threshold_roc
from analysis.similarity.scoring import class_distances

FALSE_REJECT_TARGET = 0.10


def _wrap_old_calibrator(percentile=95.0):
    """把 D06/D08 的舊方法包成跟新方法一樣的 `(templates_by_class,
    reject_templates, dist_fn) -> {"theta":.., "calibration_ms":..}` 介面，
    才能跟新方法共用同一個評估迴圈，公平比較。"""
    def calibrate(templates_by_class, reject_templates, dist_fn):
        t0 = time.perf_counter()
        result = calibrate_reject_threshold(templates_by_class, reject_templates, dist_fn, percentile=percentile)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {"theta": result["theta"], "calibration_ms": elapsed_ms}
    return calibrate


def _wrap_roc_calibrator(strategy=STRATEGY_EER, target=0.05):
    def calibrate(templates_by_class, reject_templates, dist_fn):
        result = calibrate_threshold_roc(templates_by_class, reject_templates, dist_fn, strategy=strategy, target=target)
        return {"theta": result["theta"], "calibration_ms": result["calibration_ms"]}
    return calibrate


def measure_out_of_sample_rates(templates_by_class, reject_templates, centers, reject_center,
                                 dist_fn, calibrate_fn, rng, n_trials=60):
    """校準一次門檻，再用**跟校準無關的、新抽出來的**查詢樣本量測
    out-of-sample 誤拒率／正確拒識率——不是拿校準用的樣本自己評自己，
    這樣新舊方法比較才公平（跟 `D09` 的 `measure_reject_rates` 同一套
    評估邏輯，只有「怎麼校準門檻」這一步不同）。
    """
    cal = calibrate_fn(templates_by_class, reject_templates, dist_fn)
    theta = cal["theta"]

    false_rejects = []
    labels = list(centers.keys())
    for _ in range(n_trials):
        label = labels[rng.integers(0, len(labels))]
        q = _make_trial(rng, centers[label])
        _, d = class_distances(q, templates_by_class, dist_fn)
        false_rejects.append(d.min() > theta)

    correct_rejects = []
    for _ in range(n_trials):
        q = _make_trial(rng, reject_center)
        _, d = class_distances(q, templates_by_class, dist_fn)
        correct_rejects.append(d.min() > theta)

    return {
        "theta": theta,
        "false_reject_rate": float(np.mean(false_rejects)),
        "correct_reject_rate": float(np.mean(correct_rejects)),
        "calibration_ms": cal["calibration_ms"],
    }


def _sweep_one_geometry(ns, n_reject_ratio, calibrate_fn, geometry_seed, n_trials, dist_fn):
    centers, reject_center = generate_centers(geometry_seed)
    rows = []
    for n in ns:
        rng = np.random.default_rng(geometry_seed * 10_000 + n)
        n_reject = max(2, int(round(n * n_reject_ratio)))
        templates_by_class, reject_templates = sample_templates(centers, reject_center, n, n_reject, rng)

        modality_results = {}
        for modality, sl in SLICES.items():
            def mod_dist(a, b, sl=sl):
                return dist_fn(a[:, sl], b[:, sl])
            modality_results[modality] = measure_out_of_sample_rates(
                templates_by_class, reject_templates, centers, reject_center,
                mod_dist, calibrate_fn, rng, n_trials=n_trials,
            )
        rows.append({"n": n, "n_reject": n_reject, "tof": modality_results["tof"], "mel": modality_results["mel"]})
    return rows


def _average_rows(per_geometry, ns):
    averaged = []
    for i, n in enumerate(ns):
        entry = {"n": n, "n_reject": per_geometry[0][i]["n_reject"]}
        for modality in ("tof", "mel"):
            fr = float(np.mean([g[i][modality]["false_reject_rate"] for g in per_geometry]))
            cr = float(np.mean([g[i][modality]["correct_reject_rate"] for g in per_geometry]))
            ms = float(np.mean([g[i][modality]["calibration_ms"] for g in per_geometry]))
            entry[modality] = {"false_reject_rate": fr, "correct_reject_rate": cr, "calibration_ms": ms}
        averaged.append(entry)
    return averaged


def sweep_compare_methods(ns=DEFAULT_N_SWEEP, n_reject_ratio=1.0,
                           old_percentile=95.0, roc_strategy=STRATEGY_EER, roc_target=0.05,
                           geometry_seeds=DEFAULT_GEOMETRY_SEEDS, n_trials=60, dist_fn=cosine_dist):
    """對每個 n、每組幾何，分別用舊方法（`loo_single`）與新方法（`roc`）
    校準+評估，回傳 `{"loo_single": [...], "roc": [...]}`，兩者格式相同，
    跟 `D09` 的 `sweep_template_counts()` 輸出格式相容，方便並列比較。
    """
    old_calibrate = _wrap_old_calibrator(old_percentile)
    roc_calibrate = _wrap_roc_calibrator(roc_strategy, roc_target)

    results = {}
    for method_name, calibrate_fn in (("loo_single", old_calibrate), ("roc", roc_calibrate)):
        per_geometry = [
            _sweep_one_geometry(ns, n_reject_ratio, calibrate_fn, gs, n_trials, dist_fn)
            for gs in geometry_seeds
        ]
        results[method_name] = _average_rows(per_geometry, ns)
    return results


def sweep_imbalanced_ratio(n=30, ratios=(0.3, 0.5, 1.0, 2.0, 3.0), old_percentile=95.0,
                            roc_strategy=STRATEGY_EER, roc_target=0.05,
                            geometry_seeds=DEFAULT_GEOMETRY_SEEDS, n_trials=60, dist_fn=cosine_dist):
    """驗證新方法在 word:reject 樣板數不平衡時的行為（實作提示第 3 點）：
    固定 word 樣板數 `n`，讓 `_reject` 樣板數以 `ratios` 倍數變化。
    回傳 `{"loo_single": [{"ratio":.., "n_reject":.., "tof":{...}, "mel":{...}}...], "roc": [...]}`。
    """
    old_calibrate = _wrap_old_calibrator(old_percentile)
    roc_calibrate = _wrap_roc_calibrator(roc_strategy, roc_target)

    results = {}
    for method_name, calibrate_fn in (("loo_single", old_calibrate), ("roc", roc_calibrate)):
        per_geometry = []
        for gs in geometry_seeds:
            centers, reject_center = generate_centers(gs)
            rows = []
            for ratio in ratios:
                rng = np.random.default_rng(gs * 10_000 + int(ratio * 1000))
                n_reject = max(2, int(round(n * ratio)))
                templates_by_class, reject_templates = sample_templates(centers, reject_center, n, n_reject, rng)

                modality_results = {}
                for modality, sl in SLICES.items():
                    def mod_dist(a, b, sl=sl):
                        return dist_fn(a[:, sl], b[:, sl])
                    modality_results[modality] = measure_out_of_sample_rates(
                        templates_by_class, reject_templates, centers, reject_center,
                        mod_dist, calibrate_fn, rng, n_trials=n_trials,
                    )
                rows.append({"ratio": ratio, "n_reject": n_reject, "tof": modality_results["tof"], "mel": modality_results["mel"]})
            per_geometry.append(rows)

        averaged = []
        for i, ratio in enumerate(ratios):
            entry = {"ratio": ratio, "n_reject": per_geometry[0][i]["n_reject"]}
            for modality in ("tof", "mel"):
                fr = float(np.mean([g[i][modality]["false_reject_rate"] for g in per_geometry]))
                entry[modality] = {"false_reject_rate": fr}
            averaged.append(entry)
        results[method_name] = averaged
    return results


def format_report(compare_rows, imbalance_rows, is_synthetic=True):
    """輸出 `reports/D22_reject_calibration.md`。"""
    lines = ["# D22 — 拒識門檻校準：舊方法 vs 雙邊 ROC"]

    if is_synthetic:
        lines += [
            "",
            "> ⚠️ **本報告使用合成資料，結論待 `E05` 真實資料複驗。**"
            "若真實資料上兩種方法表現不同，以真實資料為準。",
        ]

    lines += ["", "## 樣板數 vs 誤拒率：舊方法（loo_single）vs 新方法（roc, EER）"]
    lines += [
        "| n | ToF 誤拒率 (舊) | ToF 誤拒率 (新) | Mel 誤拒率 (舊) | Mel 誤拒率 (新) | "
        "校準耗時 ToF (新, ms) | 校準耗時 Mel (新, ms) |",
        "|---|---|---|---|---|---|---|",
    ]
    old_rows = {r["n"]: r for r in compare_rows["loo_single"]}
    new_rows = {r["n"]: r for r in compare_rows["roc"]}
    for n in sorted(old_rows):
        old_r, new_r = old_rows[n], new_rows[n]
        lines.append(
            f"| {n} | {old_r['tof']['false_reject_rate']:.1%} | {new_r['tof']['false_reject_rate']:.1%} | "
            f"{old_r['mel']['false_reject_rate']:.1%} | {new_r['mel']['false_reject_rate']:.1%} | "
            f"{new_r['tof']['calibration_ms']:.2f} | {new_r['mel']['calibration_ms']:.2f} |"
        )

    max_calib_ms = max(
        max(r["tof"]["calibration_ms"], r["mel"]["calibration_ms"]) for r in compare_rows["roc"]
    )
    old_max_calib_ms = max(
        max(r["tof"]["calibration_ms"], r["mel"]["calibration_ms"]) for r in compare_rows["loo_single"]
    )
    lines += [
        "",
        f"新方法最大實測校準耗時：{max_calib_ms:.2f} ms（`E06` 預算是 30 秒 = 30000 ms，**仍在預算內**）。"
        f"但相較舊方法在同樣 n 下的耗時（{old_max_calib_ms:.2f} ms），"
        f"新方法慢了約 {max_calib_ms / max(old_max_calib_ms, 1e-9):.0f} 倍——"
        "舊方法的 LOO 只在（通常較小的）`_reject` 樣板集合內兩兩比對，"
        "新方法的 `compute_word_loocv_distances` 是對**每個詞類別自己**做 LOOCV，"
        "運算量隨樣板數平方成長。**在測試範圍（n≤100）內仍遠低於 30 秒預算**，"
        "但若之後 enrollment 樣板數大幅增加、或改用較慢的 DTW 距離，"
        "這個二次成長的耗時需要重新評估。",
    ]

    lines += ["", "## 樣板數不平衡時的行為（word 樣板數固定，`_reject` 樣板數倍數變化）"]
    lines += ["| word:reject 比例 | ToF 誤拒率 (舊) | ToF 誤拒率 (新) | Mel 誤拒率 (舊) | Mel 誤拒率 (新) |",
              "|---|---|---|---|---|"]
    old_imb = {r["ratio"]: r for r in imbalance_rows["loo_single"]}
    new_imb = {r["ratio"]: r for r in imbalance_rows["roc"]}
    for ratio in sorted(old_imb):
        old_r, new_r = old_imb[ratio], new_imb[ratio]
        lines.append(
            f"| 1:{ratio:g} | {old_r['tof']['false_reject_rate']:.1%} | {new_r['tof']['false_reject_rate']:.1%} | "
            f"{old_r['mel']['false_reject_rate']:.1%} | {new_r['mel']['false_reject_rate']:.1%} |"
        )

    # 判斷新方法是否明顯優於舊方法（誠實判斷，不是預設宣稱有效）
    old_final = old_rows[max(old_rows)]
    new_final = new_rows[max(new_rows)]
    tof_improved = new_final["tof"]["false_reject_rate"] < old_final["tof"]["false_reject_rate"] - 0.10
    mel_improved = new_final["mel"]["false_reject_rate"] < old_final["mel"]["false_reject_rate"] - 0.10

    lines += ["", "## 結論"]
    if tof_improved and mel_improved:
        lines.append(
            "**新方法（雙邊 ROC）在 ToF 與 Mel 都明顯優於舊方法**"
            f"（在最大測試樣板數下，ToF 誤拒率 {old_final['tof']['false_reject_rate']:.1%} → "
            f"{new_final['tof']['false_reject_rate']:.1%}，"
            f"Mel {old_final['mel']['false_reject_rate']:.1%} → {new_final['mel']['false_reject_rate']:.1%}）。"
            "建議採用雙邊 ROC 校準取代單邊 LOO 方法，但**仍保留舊方法程式碼作為對照**"
            "（`method=\"loo_single\"`），待 `E05` 真實資料複驗。"
        )
    elif tof_improved or mel_improved:
        lines.append(
            "**新方法只在一個模態上明顯改善**"
            f"（ToF: {old_final['tof']['false_reject_rate']:.1%} → {new_final['tof']['false_reject_rate']:.1%}，"
            f"Mel: {old_final['mel']['false_reject_rate']:.1%} → {new_final['mel']['false_reject_rate']:.1%}）。"
            "建議先在合成資料上進一步排查另一個模態沒有改善的原因，"
            "再決定是否兩個模態都換成新方法。"
        )
    else:
        lines.append(
            "**如實回報：在這份合成資料上，新方法（雙邊 ROC）並未明顯優於舊方法**"
            f"（ToF: {old_final['tof']['false_reject_rate']:.1%} → {new_final['tof']['false_reject_rate']:.1%}，"
            f"Mel: {old_final['mel']['false_reject_rate']:.1%} → {new_final['mel']['false_reject_rate']:.1%}）。"
            "這本身是有效的交付結論——代表問題可能不只是「校準方法只看單邊分布」，"
            "或者這份合成資料的類別可分性設計本身就不利於區分兩種方法的差異。"
            "建議：(1) 待 `E05` 真實資料到位後用真實資料重新比較；"
            "(2) 檢視合成資料的雜訊/可分性設計是否貼近真實情境。"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    from pathlib import Path

    print("掃樣板數（新舊方法並列，n_trials=60，3 組幾何）...")
    compare_rows = sweep_compare_methods(n_trials=60)
    for method in ("loo_single", "roc"):
        print(f"  [{method}]")
        for r in compare_rows[method]:
            print(f"    n={r['n']}: tof_fr={r['tof']['false_reject_rate']:.1%} "
                  f"mel_fr={r['mel']['false_reject_rate']:.1%} "
                  f"tof_ms={r['tof']['calibration_ms']:.2f} mel_ms={r['mel']['calibration_ms']:.2f}")

    print("\n掃樣板數不平衡（n=30 固定，reject 比例變化）...")
    imbalance_rows = sweep_imbalanced_ratio(n=30, n_trials=60)
    for method in ("loo_single", "roc"):
        print(f"  [{method}]")
        for r in imbalance_rows[method]:
            print(f"    ratio={r['ratio']}: tof_fr={r['tof']['false_reject_rate']:.1%} "
                  f"mel_fr={r['mel']['false_reject_rate']:.1%}")

    report = format_report(compare_rows, imbalance_rows, is_synthetic=True)
    print("\n" + report)

    out_path = Path(__file__).resolve().parents[2] / "reports" / "D22_reject_calibration.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report + "\n")
    print(f"\n報告已寫入 {out_path}")
