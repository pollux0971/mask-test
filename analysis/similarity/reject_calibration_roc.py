"""D22 — 重新設計拒識門檻校準：雙邊 ROC。

規格見 `stories/D-analysis/D22.md`。`D09` 的樣板數研究證明：現行
`D06`/`D08` 的 `fit_reject_threshold()`（只用 `_reject` 類別自己的
leave-one-out 距離分布單邊校準）在真實規模（104 維、T=24）下有結構性
缺陷——多錄樣板救不了，因為「`_reject` 類別 LOO 最近距離」與
「真詞查詢到自己類別的最近距離」是同一種統計量（都是「同噪音分布下的
最近鄰距離」），會用類似速度一起縮小，門檻不會相對變寬。

**雙邊 ROC 校準**：同時取樣「真詞的 LOOCV 距離分布」
（`compute_word_loocv_distances`）與「拒識樣本到最近詞類別的距離分布」
（`compute_reject_distances`），對候選門檻掃出 FRR(τ)（誤拒率）/FAR(τ)
（誤受率）曲線，直接看兩個分布的實際重疊程度，而不是只看單邊分布
再假設它能套用到另一邊。

**舊方法（`analysis.similarity.enrollment.fit_reject_threshold`）保留當
對照，不刪除**——它是這個發現的證據，真實資料上結論可能不同，
`E05` 之後需要拿真實資料重新比較兩者。

`theta_reject_tof` / `theta_reject_mel` 各自獨立跑（CONTRACTS §4.3 已凍結
的兩個獨立欄位），這裡的 `calibrate_tri_threshold_roc()` 對兩個模態各自
呼叫一次，不共用一個門檻。
"""
import time

import numpy as np

from analysis.similarity.scoring import class_distances

STRATEGY_TARGET_FRR = "target_frr"
STRATEGY_TARGET_FAR = "target_far"
STRATEGY_EER = "eer"
VALID_STRATEGIES = {STRATEGY_TARGET_FRR, STRATEGY_TARGET_FAR, STRATEGY_EER}


def compute_word_loocv_distances(templates_by_class, dist_fn):
    """真詞的 LOOCV「到自己類別」距離分布（genuine match 分布）。

    每一筆樣板當 query，只跟**自己類別**扣掉自己之後的其餘樣板比，
    取最小距離——這裡要的是「真的是這個詞時，距離長什麼樣子」，
    不是判斷分類正不正確（那是 `enrollment.loocv_accuracy` 的事）。
    """
    distances = []
    for templates in templates_by_class.values():
        n = len(templates)
        for i in range(n):
            others = [t for j, t in enumerate(templates) if j != i]
            if not others:
                continue
            distances.append(min(dist_fn(templates[i], t) for t in others))
    return np.array(distances)


def compute_reject_distances(reject_templates, templates_by_class, dist_fn):
    """拒識樣本到「最近詞類別」的距離分布（impostor 分布）。

    每一筆 `_reject` 樣板當 query，跟全部詞類別樣板比，取最小距離——
    不需要 leave-one-out，`_reject` 樣板本來就不屬於任何詞類別。
    """
    distances = []
    for q in reject_templates:
        _, d_class = class_distances(q, templates_by_class, dist_fn)
        distances.append(float(d_class.min()))
    return np.array(distances)


def compute_roc(word_distances, reject_distances):
    """回傳 (thresholds, frr, far)：thresholds 是兩個分布觀察值排序去重後
    的候選門檻；frr[i]/far[i] 是門檻 `thresholds[i]` 下的誤拒率／誤受率。

    FRR(τ) = 真詞距離 > τ 的比例（該拒的沒拒——喔不，是不該拒的被拒了）。
    FAR(τ) = 拒識樣本距離 <= τ 的比例（該拒的沒被拒，被誤認成某個詞）。
    τ 越大越寬鬆：FRR 隨 τ 增加而下降，FAR 隨 τ 增加而上升。
    """
    thresholds = np.unique(np.concatenate([word_distances, reject_distances]))
    frr = np.array([(word_distances > t).mean() for t in thresholds])
    far = np.array([(reject_distances <= t).mean() for t in thresholds])
    return thresholds, frr, far


def select_threshold(thresholds, frr, far, strategy=STRATEGY_EER, target=0.05):
    """依策略從 ROC 候選門檻中選一個，回傳 (theta, actual_frr, actual_far)。

    - `target_frr`：在誤拒率 <= target 的門檻裡，選門檻值最小的
      （同時滿足 FRR 目標下讓 FAR 最小）；沒有任何門檻達標則退而求其次，
      選 FRR 最小的那個（並非真的達標，呼叫端要自己看 `actual_frr`）。
    - `target_far`：對稱地選 FAR <= target 裡門檻值最大的。
    - `eer`：FRR 與 FAR 最接近的門檻（Equal Error Rate）。
    """
    if strategy not in VALID_STRATEGIES:
        raise ValueError(f"strategy 必須是 {VALID_STRATEGIES} 之一，收到 {strategy!r}")

    if strategy == STRATEGY_TARGET_FRR:
        ok = np.flatnonzero(frr <= target)
        idx = ok[np.argmin(thresholds[ok])] if len(ok) else int(np.argmin(frr))
    elif strategy == STRATEGY_TARGET_FAR:
        ok = np.flatnonzero(far <= target)
        idx = ok[np.argmax(thresholds[ok])] if len(ok) else int(np.argmin(far))
    else:  # eer
        idx = int(np.argmin(np.abs(frr - far)))

    return float(thresholds[idx]), float(frr[idx]), float(far[idx])


def calibrate_threshold_roc(templates_by_class, reject_templates, dist_fn,
                             strategy=STRATEGY_EER, target=0.05):
    """雙邊 ROC 校準的完整流程。

    回傳 dict：`theta`、校準時算出的 `frr`/`far`（不是事後量測，是
    ROC 曲線上被選中那個門檻的理論值）、`strategy`、`target`、
    `n_word_samples`/`n_reject_samples`（實際用了幾筆樣本，供報告記錄）、
    `calibration_ms`（校準耗時，`E06` 的 30 秒預算要看這個）。
    """
    t0 = time.perf_counter()
    word_distances = compute_word_loocv_distances(templates_by_class, dist_fn)
    reject_distances = compute_reject_distances(reject_templates, templates_by_class, dist_fn)
    thresholds, frr, far = compute_roc(word_distances, reject_distances)
    theta, actual_frr, actual_far = select_threshold(thresholds, frr, far, strategy=strategy, target=target)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    return {
        "theta": theta,
        "frr": actual_frr,
        "far": actual_far,
        "strategy": strategy,
        "target": target,
        "n_word_samples": len(word_distances),
        "n_reject_samples": len(reject_distances),
        "calibration_ms": elapsed_ms,
        "thresholds": thresholds,
        "roc_frr": frr,
        "roc_far": far,
    }


def calibrate_tri_threshold_roc(templates_by_class, reject_templates, slices, dist_fn,
                                 strategy=STRATEGY_EER, target=0.05):
    """`theta_reject_tof` / `theta_reject_mel` 各自獨立跑一次 ROC 校準
    （對應 CONTRACTS §4.3 已凍結的兩個獨立欄位）。
    """
    def _modality_dist(sl):
        return lambda a, b: dist_fn(a[:, sl], b[:, sl])

    return {
        "tof": calibrate_threshold_roc(
            templates_by_class, reject_templates, _modality_dist(slices["tof"]),
            strategy=strategy, target=target,
        ),
        "mel": calibrate_threshold_roc(
            templates_by_class, reject_templates, _modality_dist(slices["mel"]),
            strategy=strategy, target=target,
        ),
    }


def plot_roc_curves(roc_tof, roc_mel):
    """ROC 曲線（FRR vs FAR），tof/mel 各一條。圖表文字一律英文。

    roc_tof/roc_mel: `calibrate_threshold_roc()` 的回傳 dict
    （用其中的 `roc_frr`/`roc_far`）。
    """
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 5))
    for label, result in (("ToF", roc_tof), ("Mel", roc_mel)):
        far = result["roc_far"]
        frr = result["roc_frr"]
        order = np.argsort(far)
        ax.plot(far[order], frr[order], label=label)
        ax.scatter([result["far"]], [result["frr"]], marker="o")
    ax.set_xlabel("FAR (false accept rate)")
    ax.set_ylabel("FRR (false reject rate)")
    ax.set_title("ROC: FRR vs FAR")
    ax.legend()
    fig.tight_layout()
    return fig
