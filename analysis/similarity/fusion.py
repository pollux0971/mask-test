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

---

## 合成測資的已知陷阱

寫這個模組的 pytest 時踩到一個坑，**症狀看起來像拒識邏輯的 bug，
其實是合成測資設計錯誤**，容易讓人往錯的方向 debug：

**錯誤做法：** 每個類別的中心向量 = 隨機向量 + `i * 某個常數`
（常數均勻加到向量的每一維，例如 `rng.normal(size=D) + i*50`）。

**後果：** 這個常數項在所有維度上是同一個方向，而隨機項相對很小，
於是所有類別的向量幾乎指向同一個方向（只差整體長度）。
**cosine 距離只看方向、不看長度**，於是完全分不出類別——
拒識機制會近乎 100% 誤拒真詞，看起來像是 `fit_reject_threshold` 或
`normalize_distances` 壞了，但其實兩者的邏輯都是對的，錯的是測資。

**正確做法：** 每個類別的中心是各自獨立、正規化到相同量級的隨機方向：

```python
def _random_direction(rng, n_dims, magnitude=10.0):
    v = rng.normal(size=n_dims)
    return v / np.linalg.norm(v) * magnitude
```

`D08`/`D09`/`D19` 這幾個 story 都會寫合成測資、都會用到 cosine 距離，
遇到「合成資料上拒識/分類莫名其妙全部失敗」時，先檢查類別中心是不是
不小心共用了同一個主方向，不要先假設是距離/正規化函式的邏輯錯了。
（`D13` 也踩過同類型的坑：合成訊號只放在一個 zone/一個 band，
被其餘幾百維的雜訊稀釋掉——都是「測資的統計結構不符合預期」這一類問題。）
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
    # 原始（未正規化）距離，只給 fused_reject()/theta_reject_fused() 用，
    # 不是 CONTRACTS §4.3 JSON 的一部分（那兩個公開欄位是正規化後的
    # d_tof/d_mel）。手動建構的 TriResult（例如舊測試）留 None 也能用
    # fuse()/top1()，只有呼叫 fused 拒識時才會需要它們。
    d_tof_raw: np.ndarray = None
    d_mel_raw: np.ndarray = None

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

    def theta_reject_fused(self, w):
        """融合後的拒識閾值：`w*theta_reject_tof + (1-w)*theta_reject_mel`
        （CONTRACTS §4.3）。

        線性內插：融合距離本身就是 `w*d_tof + (1-w)*d_mel`，門檻用同一組
        權重才自洽，而且 `w=1`/`w=0` 兩端恰好精確退化成
        `theta_reject_tof`/`theta_reject_mel`——拖滑桿到底不會有行為跳變。
        """
        if not (0.0 <= w <= 1.0):
            raise ValueError(f"w 應在 [0, 1] 之間，收到 {w}")
        return w * self.theta_reject_tof + (1 - w) * self.theta_reject_mel

    def reject_fused(self, w):
        """融合後的拒識判定：融合後的最小距離 > `theta_reject_fused(w)`
        （CONTRACTS §4.3）。

        **用原始（未正規化）距離算融合最小距離，不是用 `d_tof`/`d_mel`**——
        那兩個已經正規化過，`min()` 恆為 0，拿來判斷拒識沒有意義（任何
        正的門檻都不會被超過）。用原始距離也是 `w=1`/`w=0` 兩端會精確
        退化成 `reject_tof`/`reject_mel` 的原因：兩者本來就是同一組原始
        距離、同樣的比較方式算出來的，不是碰巧一致。
        """
        if self.d_tof_raw is None or self.d_mel_raw is None:
            raise ValueError(
                "reject_fused 需要原始距離（d_tof_raw/d_mel_raw）——"
                "這個 TriResult 沒有帶原始距離（手動建構或舊呼叫端），"
                "用 compute_tri_result() 產生的 TriResult 才支援 reject_fused"
            )
        if not (0.0 <= w <= 1.0):
            raise ValueError(f"w 應在 [0, 1] 之間，收到 {w}")
        d_fused_raw = w * self.d_tof_raw + (1 - w) * self.d_mel_raw
        return bool(d_fused_raw.min() > self.theta_reject_fused(w))


def compute_tri_result(
    query, templates_by_class, reject_templates, slices, dist_fn,
    tau=DEFAULT_TAU, reject_percentile=95.0, precomputed_thresholds=None,
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
    precomputed_thresholds: 可選，`{"tof": theta, "mel": theta}`。給定時
                         跳過 `fit_reject_threshold` 重新校準，直接用這兩個
                         值——`D09` 即時辨識服務熱載入樣板時只校準一次、
                         快取起來，每次 `recognize()` 呼叫不需要重算
                         （對 DTW 這種較慢的距離函式尤其重要）。

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

    if precomputed_thresholds is not None:
        theta_tof = precomputed_thresholds["tof"]
        theta_mel = precomputed_thresholds["mel"]
    else:
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
        d_tof_raw=d_tof_raw,
        d_mel_raw=d_mel_raw,
    )
