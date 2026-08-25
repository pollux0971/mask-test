"""餘弦距離基準：D05（DTW）跑出來之前，先有一個能跑的辨識器與基準數字。

規格見 `stories/D-analysis/D04.md`。輸入用 `D03`（`analysis/features/feature_assembly.py`）
的 `FeatureSeq.data`（T=24 固定長度版）——固定長度是餘弦距離可比的前提，
`data_raw`（原始長度版）是留給 D05 的 DTW 用的，這裡不用。
"""
import numpy as np

DIST_EPS = 1e-12  # 兩個向量任一為全零時，避免除以 0


def cosine_dist(a, b):
    """兩個 (T, D) 序列攤平後的餘弦距離，回傳純量：1 - cos_similarity。

    距離越小代表越像；完全相同回傳 0，正交回傳 1。
    """
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    if x.shape != y.shape:
        raise ValueError(f"a、b 攤平後長度不一致: {x.shape} vs {y.shape}")
    return 1.0 - (x @ y) / (np.linalg.norm(x) * np.linalg.norm(y) + DIST_EPS)


def modality_cosine_dist(a, b, slices, modality):
    """只用單一模態（`slices["tof"]` 或 `slices["mel"]`）算餘弦距離。

    a, b: (T, D) 完整特徵序列（例如兩個 `FeatureSeq.data`）
    slices: 對應 `FeatureSeq.slices`，見 D03
    """
    sl = slices[modality]
    return cosine_dist(a[:, sl], b[:, sl])


def batch_cosine_dist(query, templates):
    """一個 query 對 N 個樣板的餘弦距離，用矩陣運算一次算完。

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

    q_norm = np.linalg.norm(q)
    t_norms = np.linalg.norm(flat, axis=1)
    dots = flat @ q
    sims = dots / (t_norms * q_norm + DIST_EPS)
    return 1.0 - sims
