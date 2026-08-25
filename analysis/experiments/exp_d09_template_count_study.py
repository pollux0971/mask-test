"""D09 後續研究：每類要錄幾筆樣板才夠？

背景：`D09` 開發時發現，`D06`/`D07` 驗證過的樣板數比例（20:20）在低維度
（12 維、T=3）下運作良好，但換到真實系統規模（104 維 ToF+Mel、T=24、
8 類）時，誤拒率飆到 **100%**——這個效應的嚴重程度隨維度/幀數放大
（詳見 `analysis/similarity/enrollment.py` 模組 docstring）。`E05`
（4 小時資料蒐集）開錄前必須知道每個詞要錄幾次、靜止要錄幾次，
錄完才發現不夠等於重來。

本模組在**真實規模**（104 維、T=24、8 類）掃兩件事：
1. 樣板數 `n`（word 與 `_reject` 各自，成對變化）對「誤拒率」與
   「LOOCV top-1」的影響。
2. `fit_reject_threshold` 的 `percentile` 參數在固定 `n` 下能不能
   把誤拒率壓下來——如果可以，代表調 `percentile` 比多錄樣板划算。

`theta_reject_tof`（64 維）與 `theta_reject_mel`（40 維）分開量測，
因為兩者維度不同，偏差的嚴重程度可能不一樣。

---

## ⚠️ 這是合成資料，結論帶安全係數

`is_synthetic` 警示照舊。**合成資料的類別可分性是我們自己設定的**：
真實資料若比合成的更難分（雜訊更大、詞與詞的區辨特徵更接近），
需要的樣板數只會更多不會更少。`format_report()` 因此在資料驅動出來的
最小可用樣板數上再乘一個安全係數（預設 1.5），而不是直接把合成資料
量出來的最小值當成錄製建議。
"""
import numpy as np

from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.enrollment import calibrate_reject_threshold, loocv_accuracy
from analysis.similarity.scoring import class_distances

FEATURE_DIM = 104
TOF_DIM = 64
MEL_DIM = FEATURE_DIM - TOF_DIM
T_FRAMES = 24
N_CLASSES = 8
SLICES = {"tof": slice(0, TOF_DIM), "mel": slice(TOF_DIM, FEATURE_DIM)}

DEFAULT_N_SWEEP = (10, 20, 30, 40, 50, 75, 100)
DEFAULT_PERCENTILE_SWEEP = (80.0, 85.0, 90.0, 95.0, 99.0)
FALSE_REJECT_TARGET = 0.10  # 建議門檻：誤拒率要壓在這以下
SAFETY_FACTOR = 1.5


def _random_direction(rng, n_dims, magnitude=10.0):
    v = rng.normal(size=n_dims)
    return v / np.linalg.norm(v) * magnitude


def _make_trial(rng, center, T=T_FRAMES, noise=0.15):
    return center[None, :] + noise * rng.normal(size=(T, center.shape[0]))


def build_synthetic_dataset(rng, n_per_class, n_reject, n_classes=N_CLASSES):
    """真實規模（104 維、T=24）合成資料集：`n_classes` 個詞，
    各自獨立隨機方向的中心（不是共同常數偏移——見 `fusion.py` 記錄的陷阱）。
    """
    centers = {f"w{i}": _random_direction(rng, FEATURE_DIM) for i in range(n_classes)}
    templates_by_class = {
        label: [_make_trial(rng, center) for _ in range(n_per_class)]
        for label, center in centers.items()
    }
    reject_center = _random_direction(rng, FEATURE_DIM)
    reject_templates = [_make_trial(rng, reject_center) for _ in range(n_reject)]
    return templates_by_class, reject_templates, centers, reject_center


def measure_reject_rates(templates_by_class, reject_templates, centers, reject_center,
                          dist_fn, percentile, rng, n_trials=60):
    """對 `tof`／`mel` 兩個模態分別校準閾值，量測誤拒率（真詞被錯誤拒識）
    與正確拒識率（靜止被正確拒識）。

    回傳 {modality: {"theta":.., "false_reject_rate":.., "correct_reject_rate":..}}。
    """
    results = {}
    for modality, sl in SLICES.items():
        def mod_dist(a, b, sl=sl):
            return dist_fn(a[:, sl], b[:, sl])

        cal = calibrate_reject_threshold(templates_by_class, reject_templates, mod_dist, percentile=percentile)
        theta = cal["theta"]

        false_rejects = []
        labels = list(centers.keys())
        for _ in range(n_trials):
            label = labels[rng.integers(0, len(labels))]
            q = _make_trial(rng, centers[label])
            _, d = class_distances(q, templates_by_class, mod_dist)
            false_rejects.append(d.min() > theta)

        correct_rejects = []
        for _ in range(n_trials):
            q = _make_trial(rng, reject_center)
            _, d = class_distances(q, templates_by_class, mod_dist)
            correct_rejects.append(d.min() > theta)

        results[modality] = {
            "theta": theta,
            "false_reject_rate": float(np.mean(false_rejects)),
            "correct_reject_rate": float(np.mean(correct_rejects)),
        }
    return results


def sweep_template_counts(ns=DEFAULT_N_SWEEP, n_reject_ratio=1.0, percentile=95.0,
                           seed=0, n_trials=60, dist_fn=cosine_dist):
    """掃樣板數 `n`（word 與 `_reject` 成對變化，比例 `n_reject_ratio`），
    量每個 `n` 的誤拒率（分模態）與 LOOCV top-1。
    """
    rows = []
    for n in ns:
        rng = np.random.default_rng(seed)
        n_reject = max(2, int(round(n * n_reject_ratio)))
        templates_by_class, reject_templates, centers, reject_center = build_synthetic_dataset(
            rng, n, n_reject
        )
        reject_stats = measure_reject_rates(
            templates_by_class, reject_templates, centers, reject_center,
            dist_fn, percentile, rng, n_trials=n_trials,
        )
        acc, n_eval, skipped, _ = loocv_accuracy(templates_by_class, dist_fn)
        rows.append({
            "n": n, "n_reject": n_reject, "loocv_acc": acc, "loocv_n_evaluated": n_eval,
            "tof": reject_stats["tof"], "mel": reject_stats["mel"],
        })
    return rows


def sweep_percentile(ns, percentiles=DEFAULT_PERCENTILE_SWEEP, n_reject_ratio=1.0,
                      seed=1, n_trials=60, dist_fn=cosine_dist):
    """在固定的 `n` 值下掃 `percentile`，看調參數能不能讓較小的 `n` 也堪用。"""
    rows = []
    for n in ns:
        for p in percentiles:
            rng = np.random.default_rng(seed)
            n_reject = max(2, int(round(n * n_reject_ratio)))
            templates_by_class, reject_templates, centers, reject_center = build_synthetic_dataset(
                rng, n, n_reject
            )
            reject_stats = measure_reject_rates(
                templates_by_class, reject_templates, centers, reject_center,
                dist_fn, p, rng, n_trials=n_trials,
            )
            rows.append({"n": n, "percentile": p, "tof": reject_stats["tof"], "mel": reject_stats["mel"]})
    return rows


def plot_false_reject_vs_n(rows):
    """驗收：圖表文字一律英文。畫 false-reject-rate vs n，tof/mel 各一條線。"""
    import matplotlib.pyplot as plt

    ns = [r["n"] for r in rows]
    tof_rates = [r["tof"]["false_reject_rate"] for r in rows]
    mel_rates = [r["mel"]["false_reject_rate"] for r in rows]

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(ns, tof_rates, marker="o", label="ToF")
    ax.plot(ns, mel_rates, marker="s", label="Mel")
    ax.axhline(FALSE_REJECT_TARGET, linestyle="--", color="gray", label=f"target ({FALSE_REJECT_TARGET:.0%})")
    ax.set_title("False reject rate vs template count (synthetic, real-scale)")
    ax.set_xlabel("templates per class (n)")
    ax.set_ylabel("false reject rate")
    ax.legend()
    fig.tight_layout()
    return fig


def _min_n_meeting_target(rows, target=FALSE_REJECT_TARGET):
    """找出 tof 跟 mel 的誤拒率都 <= target 的最小 n；找不到回傳 None。"""
    candidates = [
        r["n"] for r in rows
        if r["tof"]["false_reject_rate"] <= target and r["mel"]["false_reject_rate"] <= target
    ]
    return min(candidates) if candidates else None


def format_report(count_rows, percentile_rows, is_synthetic=True, safety_factor=SAFETY_FACTOR):
    """輸出 `reports/D09_template_count_study.md` 的內容，結論要能直接
    回答「每個詞錄 N 次，靜止錄 M 次」。"""
    lines = ["# D09 後續研究：每類要錄幾筆樣板才夠"]

    if is_synthetic:
        lines += [
            "",
            "> ⚠️ **本報告使用合成資料，數字帶安全係數，最終結論待 `E05` 真實資料複驗。**"
            "合成資料的類別可分性是我們自己設定的，真實資料若比合成的更難分，"
            "需要的樣板數只會更多不會更少。",
        ]

    lines += ["", "## 樣板數 vs 誤拒率／LOOCV（percentile=95，word:reject=1:1）"]
    lines += ["| n | LOOCV top-1 | ToF 誤拒率 | ToF 正確拒識率 | Mel 誤拒率 | Mel 正確拒識率 |",
              "|---|---|---|---|---|---|"]
    for r in count_rows:
        lines.append(
            f"| {r['n']} | {r['loocv_acc']:.1%} | {r['tof']['false_reject_rate']:.1%} | "
            f"{r['tof']['correct_reject_rate']:.1%} | {r['mel']['false_reject_rate']:.1%} | "
            f"{r['mel']['correct_reject_rate']:.1%} |"
        )

    min_n = _min_n_meeting_target(count_rows)

    lines += ["", "## percentile 掃描（固定 n，看調參數能不能省樣板）"]
    lines += ["| n | percentile | ToF 誤拒率 | Mel 誤拒率 |", "|---|---|---|---|"]
    for r in percentile_rows:
        lines.append(
            f"| {r['n']} | {r['percentile']:.0f} | {r['tof']['false_reject_rate']:.1%} | "
            f"{r['mel']['false_reject_rate']:.1%} |"
        )

    lines += ["", "## 結論與建議"]
    if min_n is not None:
        recommended = int(np.ceil(min_n * safety_factor))
        lines.append(
            f"合成資料上，`n={min_n}` 是 ToF 與 Mel 誤拒率都能壓到 "
            f"{FALSE_REJECT_TARGET:.0%} 以下的最小樣板數。"
            f"乘上安全係數 {safety_factor}（理由：真實資料通常比合成資料更難分），"
            f"**建議每個詞錄 {recommended} 次，`_reject`（靜止）也錄 {recommended} 次**"
            "（word:reject 維持 1:1，避免 D06 發現的比例失衡問題）。"
        )
    else:
        lines.append(
            f"在測試的樣板數範圍（{[r['n'] for r in count_rows]}）內，"
            f"沒有任何 `n` 能把 ToF 與 Mel 的誤拒率都壓到 {FALSE_REJECT_TARGET:.0%} 以下。"
            "建議測試更大的 `n`，或重新檢視合成資料的類別可分性假設是否過於樂觀。"
        )

    lines.append(
        "\nToF（64 維）與 Mel（40 維）維度不同，兩者需要的樣板數可能不同——"
        "如果上面的表格顯示其中一個模態明顯先達標，之後可以考慮兩個模態各自"
        "決定樣板數（但這會讓 enrollment 流程變複雜，目前建議先用兩者都滿足"
        "的統一 n，除非樣板數差異真的很大）。"
    )

    return "\n".join(lines)
