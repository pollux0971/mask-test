"""歐式距離：使用者指定「只要歐式距離不需要訓練」（`ad` 轉述，2026-08-26）。
「不需要訓練」這件事本來就成立——現行 cosine/DTW 一樣是最近鄰比對，沒有
任何模型訓練；使用者要的是換掉距離函式本身。

介面跟 `cosine_baseline.py` 對稱（`*_dist`/`modality_*_dist`/`batch_*_dist`），
`RecognitionService`／`fusion.py`／`reject_calibration_roc.py` 都吃裸的
`(a, b) -> float` 函式，不挑實作。輸入用 D03 `FeatureSeq.data`（T=24
固定長度版）——跟 `cosine_dist` 同一個理由：兩個序列長度要能直接相減，
DTW 的 `data_raw`（原始長度）在這裡不適用。

**餘弦只看方向，歐式看方向 + 大小——這裡刻意不做任何跨模態正規化。**
104 維特徵 = 64 維 ToF（D01 z-score 過）+ 40 維 Mel（D02 CMN 過），兩個
模態各自正規化的基準不同，兩者的歐式距離量級可能差好幾倍：量級大的
那個模態會在 `euclidean_dist(a, b)`（整條 104 維一起算）裡主導總距離，
小的那個等於被稀釋掉，且不會有任何錯誤或警告——這正是要拿真實資料量
過（見 `reports/DISTANCE_COMPARISON.md`）才能決定要不要用、能不能直接
當預設的原因。

**這個風險在系統裡實際落在哪一段，分類跟拒識不一樣：** `fusion.py`
內部一律用 `modality_*_dist` 分開算 tof/mel 的距離，分類用的
`d_tof`/`d_mel` 是各自正規化過（減 min、除 std）才用權重 `w` 融合，
所以 **分類本身對兩模態原始尺度不敏感**，不會被這裡的量級差影響。
**但拒識融合用的是 `d_tof_raw`/`d_mel_raw`（CONTRACTS §4.3 規定，正規化
過的距離拿去算 reject 會讓 `.min()` 恆為 0，等於拒識永遠關掉）**——
`reject_fused(w) = min(w·d_tof_raw + (1-w)·d_mel_raw) > theta_reject_fused(w)`
直接把兩個模態的原始距離加權相加，量級差幾倍，`w` 的實際效果就會被
放大同樣的倍數：即使 `theta_reject_tof`/`theta_reject_mel` 各自都用
同一套 euclidean 校準過，融合後的拒識行為還是可能被量級大的那個模態
主導。這正是這次要實測的東西，不是理論上假設一定會發生。
"""
import numpy as np


def euclidean_dist(a, b):
    """兩個 (T, D) 序列攤平後的歐式距離（L2 norm），回傳純量。

    距離越小代表越像；完全相同回傳 0，跟 cosine 的 [0, 2] 不同，這裡
    沒有上界——尺度取決於輸入本身的量級（見模組說明）。
    """
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    if x.shape != y.shape:
        raise ValueError(f"a、b 攤平後長度不一致: {x.shape} vs {y.shape}")
    return float(np.linalg.norm(x - y))


def modality_euclidean_dist(a, b, slices, modality):
    """只用單一模態（`slices["tof"]` 或 `slices["mel"]`）算歐式距離。

    a, b: (T, D) 完整特徵序列（例如兩個 `FeatureSeq.data`）
    slices: 對應 `FeatureSeq.slices`，見 D03
    """
    sl = slices[modality]
    return euclidean_dist(a[:, sl], b[:, sl])


def batch_euclidean_dist(query, templates):
    """一個 query 對 N 個樣板的歐式距離，用矩陣運算一次算完。

    query:     (T, D)
    templates: (N, T, D) 或已攤平的 (N, T*D)
    回傳: (N,) 距離陣列
    """
    query = np.asarray(query, dtype=np.float64)
    templates = np.asarray(templates, dtype=np.float64)

    q = query.ravel()
    if templates.ndim == 3:
        flat = templates.reshape(templates.shape[0], -1)
    elif templates.ndim == 2:
        flat = templates
    else:
        raise ValueError(f"templates 應為 (N,T,D) 或 (N,T*D)，收到 shape={templates.shape}")

    if flat.shape[1] != q.shape[0]:
        raise ValueError(
            f"query 攤平後長度 {q.shape[0]} 與 templates 每列長度 {flat.shape[1]} 不一致"
        )

    return np.linalg.norm(flat - q[None, :], axis=1)
