"""DTW 距離（Sakoe-Chiba band 約束）。

規格見 `stories/D-analysis/D05.md`。輸入用 D03 的 `FeatureSeq.data_raw`
（原始幀數版），**不是 `data`（固定長度版，那是 D04 餘弦距離用的）**——
DTW 存在的意義就是在時間軸上做非線性對齊，固定長度重採樣會先把
語速差異抹掉，於是 DTW 就沒東西可以對齊了。

底層用 `librosa.sequence.dtw`：`host/features/dtw_compare.py`（B14）已經
在用它算 DTW 距離，這裡延續同一套實作，不另外裝 `tslearn` / `dtaidistance`
——它原生支援 `global_constraints=True` + `band_rad` 做 Sakoe-Chiba band，
剛好是這個 story 要的東西。

**Sakoe-Chiba band 不只是加速用的，更是正則化。** 沒有約束的 DTW
可以把一個詞的全部音框硬拗到另一個詞的單一音框上，路徑近乎水平或垂直，
產生病態的小距離，反而降低判別力。`band_ratio` 限制對齊路徑偏離對角線
的比例（0.2 表示最多偏離 20%），把這種病態對齊排除掉。`band_ratio`
留成參數不寫死，因為 `D08` 的 LOOCV 之後要拿真實資料校準它。
"""
import numpy as np
from librosa.sequence import dtw as _librosa_dtw

DEFAULT_BAND_RATIO = 0.2


def dtw_dist(a, b, band_ratio=DEFAULT_BAND_RATIO):
    """兩個 (T, D) 序列（長度可以不同）的 Sakoe-Chiba 限制 DTW 距離。

    回傳值除以對齊路徑長度做正規化，讓不同長度的序列也能互相比較——
    跟 `host/features/dtw_compare.dtw_distance()` 用同樣的正規化方式。
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.ndim != 2 or b.ndim != 2 or a.shape[1] != b.shape[1]:
        raise ValueError(f"shape 不相容: {a.shape} vs {b.shape}")
    if not (0.0 < band_ratio <= 1.0):
        raise ValueError(f"band_ratio 應在 (0, 1] 之間，收到 {band_ratio}")

    cost, wp = _librosa_dtw(
        X=a.T, Y=b.T, metric="euclidean",
        global_constraints=True, band_rad=band_ratio,
    )
    return float(cost[-1, -1] / len(wp))


def modality_dtw_dist(a, b, slices, modality, band_ratio=DEFAULT_BAND_RATIO):
    """只用單一模態算 DTW 距離。

    a, b: (T, D) 完整特徵序列（各自可以不同長度）
    slices: 對應 D03 `FeatureSeq.slices`
    """
    sl = slices[modality]
    return dtw_dist(a[:, sl], b[:, sl], band_ratio=band_ratio)


def batch_dtw_dist(query, templates, band_ratio=DEFAULT_BAND_RATIO):
    """一個 query 對 N 個 templates 的 DTW 距離。

    templates 是長度可以互不相同的序列組成的 list（不重採樣正是重點），
    DTW 沒有 D04 餘弦那種矩陣乘法捷徑，這裡就是逐一呼叫 `dtw_dist()`。
    回傳 (N,) 距離陣列。
    """
    return np.array([dtw_dist(query, t, band_ratio=band_ratio) for t in templates])
