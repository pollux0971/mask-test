"""三軌評分與融合：ToF-only / Mel-only / Fused 三組分數。

規格見 `stories/D-analysis/D07.md`；`TriResult` 對應 CONTRACTS.md §4.3
的 JSON 形狀（`classes`/`d_tof`/`d_mel`/`reject_tof`/`reject_mel`）。

**這是 C17 融合滑桿背後的計算引擎。** 距離只算一次（用 D04 的 `cosine_dist`
或 D05 的 `dtw_dist`，由呼叫端注入），`TriResult.fuse(w)` 之後只是對
已經算好、已經正規化的距離向量做加權平均——拖動滑桿不需要重新比對，
這正是回傳結構要長這樣的理由。

**兩個模態的距離必須各自正規化後才能加權**（`analysis.similarity.scoring
.normalize_distances`）：ToF 與 Mel 的原始距離量級可能差 10 倍，
不正規化的話 `w=0.5` 實際上等於「幾乎只用其中一個」。
"""
from dataclasses import dataclass

import numpy as np

from analysis.similarity.scoring import (
    DEFAULT_TAU,
    class_distances,
    fit_reject_threshold,
    normalize_distances,
    softmax_scores,
)


@dataclass
class TriResult:
    classes: list
    d_tof: np.ndarray   # 正規化後的距離向量（CONTRACTS §4.3 "d_tof"）
    d_mel: np.ndarray   # 正規化後的距離向量（CONTRACTS §4.3 "d_mel"）
    reject_tof: bool    # 純 ToF（w=1）的拒識判定，用 ToF 自己的原始距離 + 專屬閾值算好，固定存起來
    reject_mel: bool    # 純 Mel（w=0）的拒識判定，同理
    theta_reject_tof: float  # ToF 專屬的拒識閾值（見下方 CONTRACTS 疑問說明）
    theta_reject_mel: float  # Mel 專屬的拒識閾值
    tau: float = DEFAULT_TAU

    def fuse(self, w):
        """d_fused = w*d_tof + (1-w)*d_mel，轉成 softmax 分數。

        w=1 等於純 ToF（`softmax(-d_tof/tau)`）、w=0 等於純 Mel。
        `d_tof`/`d_mel` 已經是正規化後的向量，這裡只是加權平均，
        不重新計算距離。
        """
        if not (0.0 <= w <= 1.0):
            raise ValueError(f"w 應在 [0, 1] 之間，收到 {w}")
        d_fused = w * self.d_tof + (1 - w) * self.d_mel
        return softmax_scores(d_fused, tau=self.tau)

    def top1(self, w):
        """回傳給定 w 下分數最高的類別名稱。"""
        scores = self.fuse(w)
        return self.classes[int(np.argmax(scores))]


def compute_tri_result(
    query, templates_by_class, reject_templates, slices, dist_fn,
    tau=DEFAULT_TAU, reject_percentile=95.0,
):
    """算一次距離，組出可以之後任意 w 重算的 `TriResult`。

    query:              (T, 104) 完整特徵序列（D03 `FeatureSeq.data` 或
                         `data_raw`，視 `dist_fn` 是 D04 餘弦還是 D05 DTW）
    templates_by_class: dict {class_label: [完整 104 維序列...]}，不含 `_reject`
    reject_templates:   `_reject` 類別自己的完整 104 維序列樣板
                         （供 `fit_reject_threshold` 分別校準 ToF/Mel 各自的閾值）
    slices:             對應 D03 `FeatureSeq.slices`，
                         例如 {"tof": slice(0,64), "mel": slice(64,104)}
    dist_fn:            (a, b) -> float，套用在單一模態切片後的資料上
                         （例如 D04 `cosine_dist` 或 D05 `dtw_dist`）

    **ToF 跟 Mel 的原始距離尺度不同，拒識閾值也必須各自校準，
    不能共用一個數字**——這是 `reject_tof`/`reject_mel` 分開存、
    `theta_reject_tof`/`theta_reject_mel` 也分開存的原因。
    """
    def _modality_dist(sl):
        return lambda a, b: dist_fn(a[:, sl], b[:, sl])

    tof_dist_fn = _modality_dist(slices["tof"])
    mel_dist_fn = _modality_dist(slices["mel"])

    classes_tof, d_tof_raw = class_distances(query, templates_by_class, tof_dist_fn)
    classes_mel, d_mel_raw = class_distances(query, templates_by_class, mel_dist_fn)
    if classes_tof != classes_mel:
        raise ValueError(
            "ToF 與 Mel 用的是同一份 templates_by_class，理論上類別順序必須一致："
            f"tof={classes_tof} mel={classes_mel}"
        )

    theta_tof = fit_reject_threshold(reject_templates, tof_dist_fn, percentile=reject_percentile)
    theta_mel = fit_reject_threshold(reject_templates, mel_dist_fn, percentile=reject_percentile)
    reject_tof = bool(d_tof_raw.min() > theta_tof)
    reject_mel = bool(d_mel_raw.min() > theta_mel)

    return TriResult(
        classes=classes_tof,
        d_tof=normalize_distances(d_tof_raw),
        d_mel=normalize_distances(d_mel_raw),
        reject_tof=reject_tof,
        reject_mel=reject_mel,
        theta_reject_tof=theta_tof,
        theta_reject_mel=theta_mel,
        tau=tau,
    )
