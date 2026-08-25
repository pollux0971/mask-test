"""距離轉分數與拒識閾值。

規格見 `stories/D-analysis/D06.md`。跟距離度量本身解耦：所有函式吃一個
外部傳入的 `dist_fn(a, b) -> float`（例如 D04 的 `cosine_dist`，之後也可以
換成 D05 的 DTW 距離），這裡只管「距離 -> 分數 -> 拒識」這一段轉換。

拒識閾值（`theta_reject`）的估計方式：vocab.json（CONTRACTS.md §6）裡的
`_reject`（「靜止／其他」）本身是一個有自己 enrollment 樣板的類別，
不是事後推算出來的。`fit_reject_threshold()` 對 `_reject` 類別自己的樣板
做 leave-one-out，取「兩筆都不是任何詞、彼此差多少」的內部距離分布，
其 p95 就是 theta_reject——代表「兩筆完全無關的錄音，距離最多可以近到
多少」的保守上界。之後判斷一個新輸入時，只要它離「最接近的真詞」都比
這個上界還遠，就代表它跟隨便兩筆靜止錄音之間的相似程度差不多，
沒有理由相信它是那個詞，所以拒識。這個方向是為了讓「靜止拒識率 > 90%」
與「真實語音誤拒率 < 10%」兩個驗收條件同時成立：真詞跟自己的樣板應該
比兩筆隨機靜止錄音彼此更接近，靜止跟任何詞的距離則應該比這個上界更遠。
"""
import numpy as np

NORM_EPS = 1e-9
DEFAULT_TAU = 0.5  # D06.md 建議的起始值，之後用實際 Demo 錄影調整


def class_distances(query, templates_by_class, dist_fn):
    """每一類的距離 = 該類所有樣板的最小距離（用 min 不用 mean）。

    templates_by_class: dict {class_label: [templates...]}
    dist_fn: (a, b) -> float

    回傳 (classes, d_class)：classes 是 list（順序等於 dict 的 key 順序），
    d_class 是對應的 (C,) ndarray。
    """
    classes = list(templates_by_class.keys())
    d_class = np.array([
        min(dist_fn(query, t) for t in templates_by_class[c])
        for c in classes
    ])
    return classes, d_class


def normalize_distances(d_class):
    """d_norm = (d_class - min) / (std + eps)。"""
    d_class = np.asarray(d_class, dtype=np.float64)
    return (d_class - d_class.min()) / (d_class.std() + NORM_EPS)


def softmax_scores(d_norm, tau=DEFAULT_TAU):
    """scores = softmax(-d_norm / tau)，總和為 1。

    tau 越小分數越極端（越自信），越大越平坦（越猶豫）。
    """
    d_norm = np.asarray(d_norm, dtype=np.float64)
    logits = -d_norm / tau
    logits = logits - logits.max()  # 數值穩定，避免 exp 溢位
    exp = np.exp(logits)
    return exp / exp.sum()


def fit_reject_threshold(reject_class_templates, dist_fn, percentile=95.0):
    """從 `_reject`（靜止）類別自己的樣板，用 leave-one-out 估計 theta_reject。

    對每一筆 `_reject` 樣板，算它跟其餘 `_reject` 樣板的最小距離，湊出一個
    「兩筆都是靜止，彼此最多能有多近」的距離分布，取其 p95 當閾值。
    """
    templates = list(reject_class_templates)
    n = len(templates)
    if n < 2:
        raise ValueError("至少需要 2 筆 _reject 樣板才能算 leave-one-out 內部距離分布")

    loo_dists = [
        min(dist_fn(templates[i], templates[j]) for j in range(n) if j != i)
        for i in range(n)
    ]
    return float(np.percentile(loo_dists, percentile))


def recognize_scores(query, templates_by_class, dist_fn, tau=DEFAULT_TAU, theta_reject=None):
    """完整流程：query -> (classes, scores, reject)。

    templates_by_class 不含 `_reject`（那是拿來校準 theta_reject 用的，
    不參與 softmax 分數的類別集合）。theta_reject 為 None 時不做拒識判斷。
    """
    classes, d_class = class_distances(query, templates_by_class, dist_fn)
    d_norm = normalize_distances(d_class)
    scores = softmax_scores(d_norm, tau=tau)
    reject = None if theta_reject is None else bool(d_class.min() > theta_reject)
    return classes, scores, reject
