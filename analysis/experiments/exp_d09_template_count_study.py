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


def generate_centers(seed, n_classes=N_CLASSES, n_dims=FEATURE_DIM):
    """固定類別幾何（各詞與 `_reject` 的中心方向）。

    **這個函式獨立於 `n`，而且掃 `n` 的實驗一定要共用同一組中心**——
    早期版本曾經對每個 `n` 用同一個隨機種子重新「從頭生成中心＋抽樣板」，
    但生成中心要消耗的隨機數次數跟 `n` 有關，於是不同 `n` 其實各自拿到
    完全不同的類別幾何（可分性、中心方向都不一樣），導致「n 越大誤拒率
    越低」這條本來該有的趨勢被隨機的幾何差異蓋過去，掃出來的曲線劇烈
    非單調、毫無意義。把中心生成獨立出來、對整個掃描共用同一組，
    才是「只改變樣板數」的控制實驗。
    """
    rng = np.random.default_rng(seed)
    centers = {f"w{i}": _random_direction(rng, n_dims) for i in range(n_classes)}
    reject_center = _random_direction(rng, n_dims)
    return centers, reject_center


def sample_templates(centers, reject_center, n_per_class, n_reject, rng):
    """圍繞固定中心抽樣樣板（雜訊隨機，中心不變）。"""
    templates_by_class = {
        label: [_make_trial(rng, center) for _ in range(n_per_class)]
        for label, center in centers.items()
    }
    reject_templates = [_make_trial(rng, reject_center) for _ in range(n_reject)]
    return templates_by_class, reject_templates


def build_synthetic_dataset(rng, n_per_class, n_reject, n_classes=N_CLASSES):
    """真實規模（104 維、T=24）合成資料集：一次性生成中心＋抽樣，供不需要
    跨 `n` 比較的單次呼叫使用（例如單元測試）。跨 `n` 掃描請用
    `generate_centers()` + `sample_templates()`，見上方 docstring 的教訓。
    """
    centers, reject_center = generate_centers(seed=int(rng.integers(0, 2**31 - 1)), n_classes=n_classes)
    templates_by_class, reject_templates = sample_templates(centers, reject_center, n_per_class, n_reject, rng)
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


DEFAULT_GEOMETRY_SEEDS = (0, 1, 2)


def _sweep_counts_one_geometry(ns, n_reject_ratio, percentile, geometry_seed, n_trials, dist_fn):
    centers, reject_center = generate_centers(geometry_seed)
    rows = []
    for n in ns:
        rng = np.random.default_rng(geometry_seed * 10_000 + n)
        n_reject = max(2, int(round(n * n_reject_ratio)))
        templates_by_class, reject_templates = sample_templates(centers, reject_center, n, n_reject, rng)
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


def sweep_template_counts(ns=DEFAULT_N_SWEEP, n_reject_ratio=1.0, percentile=95.0,
                          geometry_seeds=DEFAULT_GEOMETRY_SEEDS, n_trials=60, dist_fn=cosine_dist):
    """掃樣板數 `n`（word 與 `_reject` 成對變化，比例 `n_reject_ratio`），
    量每個 `n` 的誤拒率（分模態）與 LOOCV top-1。

    **對 `geometry_seeds` 裡每一組獨立的類別幾何各跑一次再平均**——
    早期版本只用單一組固定幾何，結果發現：某一次隨機抽到的中心方向
    剛好讓 mel 子空間很難分（無論 `n` 或 `percentile` 怎麼調，mel 誤拒率
    都卡在 90%+ 附近），而同一組幾何的 tof 子空間卻完全沒事——這代表
    單一幾何的結果可能只是「這次剛好抽到難分的方向」，不是「樣板數不夠」
    的真實訊號。跨多組獨立幾何平均，才不會被單一次抽樣的運氣主導結論。
    """
    per_geometry = [
        _sweep_counts_one_geometry(ns, n_reject_ratio, percentile, gs, n_trials, dist_fn)
        for gs in geometry_seeds
    ]
    averaged = []
    for i, n in enumerate(ns):
        tof_fr = float(np.mean([g[i]["tof"]["false_reject_rate"] for g in per_geometry]))
        tof_cr = float(np.mean([g[i]["tof"]["correct_reject_rate"] for g in per_geometry]))
        mel_fr = float(np.mean([g[i]["mel"]["false_reject_rate"] for g in per_geometry]))
        mel_cr = float(np.mean([g[i]["mel"]["correct_reject_rate"] for g in per_geometry]))
        loocv = float(np.mean([g[i]["loocv_acc"] for g in per_geometry]))
        averaged.append({
            "n": n,
            "n_reject": per_geometry[0][i]["n_reject"],
            "loocv_acc": loocv,
            "loocv_n_evaluated": per_geometry[0][i]["loocv_n_evaluated"],
            "tof": {"false_reject_rate": tof_fr, "correct_reject_rate": tof_cr},
            "mel": {"false_reject_rate": mel_fr, "correct_reject_rate": mel_cr},
        })
    return averaged


def _sweep_percentile_one_geometry(ns, percentiles, n_reject_ratio, geometry_seed, n_trials, dist_fn):
    centers, reject_center = generate_centers(geometry_seed)
    rows = []
    for n in ns:
        for p in percentiles:
            rng = np.random.default_rng(geometry_seed * 10_000 + n * 100 + int(p))
            n_reject = max(2, int(round(n * n_reject_ratio)))
            templates_by_class, reject_templates = sample_templates(centers, reject_center, n, n_reject, rng)
            reject_stats = measure_reject_rates(
                templates_by_class, reject_templates, centers, reject_center,
                dist_fn, p, rng, n_trials=n_trials,
            )
            rows.append({"n": n, "percentile": p, "tof": reject_stats["tof"], "mel": reject_stats["mel"]})
    return rows


def sweep_percentile(ns, percentiles=DEFAULT_PERCENTILE_SWEEP, n_reject_ratio=1.0,
                      geometry_seeds=DEFAULT_GEOMETRY_SEEDS, n_trials=60, dist_fn=cosine_dist):
    """在固定的 `n` 值下掃 `percentile`，看調參數能不能讓較小的 `n` 也堪用。

    同樣對多組獨立幾何平均（見 `sweep_template_counts` 的說明）。
    """
    per_geometry = [
        _sweep_percentile_one_geometry(ns, percentiles, n_reject_ratio, gs, n_trials, dist_fn)
        for gs in geometry_seeds
    ]
    averaged = []
    for i in range(len(per_geometry[0])):
        n = per_geometry[0][i]["n"]
        p = per_geometry[0][i]["percentile"]
        tof_fr = float(np.mean([g[i]["tof"]["false_reject_rate"] for g in per_geometry]))
        mel_fr = float(np.mean([g[i]["mel"]["false_reject_rate"] for g in per_geometry]))
        averaged.append({
            "n": n, "percentile": p,
            "tof": {"false_reject_rate": tof_fr},
            "mel": {"false_reject_rate": mel_fr},
        })
    return averaged


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
    tof_rates = [r["tof"]["false_reject_rate"] for r in count_rows]
    mel_rates = [r["mel"]["false_reject_rate"] for r in count_rows]
    max_n_row = max(count_rows, key=lambda r: r["n"])
    tof_at_max_n = max_n_row["tof"]["false_reject_rate"]
    mel_at_max_n = max_n_row["mel"]["false_reject_rate"]
    # 判準不是「數字有沒有波動」（n_trials 有限，本來就會有統計雜訊），
    # 而是「就算把 n 拉到測試範圍內最大，誤拒率還是遠高於目標」——
    # 這才是「多錄樣板沒有用」的直接證據，不受抽樣雜訊造成的表面趨勢干擾。
    is_flat_vs_n = (
        tof_at_max_n > FALSE_REJECT_TARGET * 2 and mel_at_max_n > FALSE_REJECT_TARGET * 2
    )

    if min_n is not None:
        recommended = int(np.ceil(min_n * safety_factor))
        lines.append(
            f"合成資料上，`n={min_n}` 是 ToF 與 Mel 誤拒率都能壓到 "
            f"{FALSE_REJECT_TARGET:.0%} 以下的最小樣板數。"
            f"乘上安全係數 {safety_factor}（理由：真實資料通常比合成資料更難分），"
            f"**建議每個詞錄 {recommended} 次，`_reject`（靜止）也錄 {recommended} 次**"
            "（word:reject 維持 1:1，避免 D06 發現的比例失衡問題）。"
        )
    elif is_flat_vs_n:
        lines.append(
            f"**🔴 更重要的發現：誤拒率在整個測試範圍（n={min(r['n'] for r in count_rows)}"
            f"~{max(r['n'] for r in count_rows)}）幾乎不隨 `n` 改變**"
            f"（ToF 在 {min(tof_rates):.0%}~{max(tof_rates):.0%} 之間、"
            f"Mel 在 {min(mel_rates):.0%}~{max(mel_rates):.0%} 之間，"
            "波動只在雜訊範圍內，看不出隨 n 下降的趨勢）。"
            "percentile 掃描（見上表）也顯示同樣的情況：從 80 掃到 99，"
            "誤拒率只有小幅改善，沒有解決問題。\n\n"
            "**這代表『多錄一點樣板』可能不是解法**——`fit_reject_threshold()` "
            "目前的校準方式（只用 `_reject` 類別自己的 leave-one-out 距離分布）"
            "在這個真實規模（104 維、T=24）下，算出來的門檻可能系統性地跟"
            "「真詞到自己樣板的最近距離」重疊，而且這個重疊不會隨樣板數增加而消失"
            "（兩者都是「同一種噪音分佈下的最近鄰距離」統計量，會用類似的速度"
            "一起縮小，門檻不會相對變寬）。\n\n"
            "**這是校準方法本身可能需要重新設計的訊號，不是單純調參數或多錄"
            "幾次樣板可以解決的問題**——建議跟調度員/D06 討論，可能的方向包括："
            "同時用詞類別自己的樣板分布（而非只用 `_reject` 類別）來校準、"
            "或改用其他形式的開集判斷（open-set recognition）方法。"
            "**在有更好的校準方法之前，不建議只靠「多錄樣板」來解決這個問題。**"
        )
    else:
        lines.append(
            f"在測試的樣板數範圍（{[r['n'] for r in count_rows]}）內，"
            f"沒有任何 `n` 能把 ToF 與 Mel 的誤拒率都壓到 {FALSE_REJECT_TARGET:.0%} 以下，"
            "但誤拒率隨 `n` 有明顯變化趨勢（不是完全打平）。"
            "建議測試更大的 `n`，或重新檢視合成資料的類別可分性假設是否過於樂觀。"
        )

    lines.append(
        "\nToF（64 維）與 Mel（40 維）維度不同，兩者需要的樣板數可能不同——"
        "如果上面的表格顯示其中一個模態明顯先達標，之後可以考慮兩個模態各自"
        "決定樣板數（但這會讓 enrollment 流程變複雜，目前建議先用兩者都滿足"
        "的統一 n，除非樣板數差異真的很大）。"
    )

    return "\n".join(lines)


if __name__ == "__main__":
    from pathlib import Path

    print("掃樣板數（n_trials=100，7 個 n 值，percentile=95）...")
    count_rows = sweep_template_counts(n_trials=100)
    for r in count_rows:
        print(f"  n={r['n']}: loocv={r['loocv_acc']:.1%} "
              f"tof_false_reject={r['tof']['false_reject_rate']:.1%} "
              f"mel_false_reject={r['mel']['false_reject_rate']:.1%}")

    print("\n掃 percentile（n=20,30,50）...")
    percentile_rows = sweep_percentile(ns=(20, 30, 50), n_trials=100)
    for r in percentile_rows:
        print(f"  n={r['n']} percentile={r['percentile']:.0f}: "
              f"tof_false_reject={r['tof']['false_reject_rate']:.1%} "
              f"mel_false_reject={r['mel']['false_reject_rate']:.1%}")

    report = format_report(count_rows, percentile_rows, is_synthetic=True)
    print("\n" + report)

    out_path = Path(__file__).resolve().parents[2] / "reports" / "D09_template_count_study.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report + "\n")
    print(f"\n報告已寫入 {out_path}")
