"""D08 — Enrollment 樣板管理與 LOOCV。

規格見 `stories/D-analysis/D08.md`。分類機制重用 D06/D07 已經寫好的
`class_distances`（min-per-class），不重寫一份——LOOCV 本質上就是
「拿掉一筆樣板後，能不能用剩下的樣板猜對它自己」，跟 D07 的
`compute_tri_result` 是同一套機制。

**存檔慣例：`templates/<subject>_<wear_id>.npz`**（見 `template_path()`）。

`quality` 篩選：排除 `quality == "rejected"`（B11 定義的三個值域之一，
代表擷取失敗，不該進 enrollment 池）。**`quality == "low"` 刻意保留**，
不預先篩掉——品質標籤只反映擷取時的訊號完整度，不直接等於這筆樣板對
辨識有沒有幫助；本 story 自己的「逐樣板貢獻度分析」就是用資料本身
（而非品質標籤）判斷一筆樣板是不是壞樣板，兩種機制角色不同，
不應該讓品質標籤預先篩掉貢獻度分析原本該負責的工作。

**`theta_reject_tof` / `theta_reject_mel` 要分開校準**（D07 的 `TriResult`
把兩者存成獨立欄位，已凍結進 CONTRACTS §4.3）——本模組的
`calibrate_tri_reject_thresholds()` 對兩個模態各自呼叫一次
`scoring.fit_reject_threshold()`，不會共用一個數字。

**D06 的發現在這裡是一等公民**：`_reject` 樣板數與詞類別樣板數的比例
會系統性影響 `theta_reject` 校準的鬆緊（樣板越多、LOO 最近距離的期望值
越小），`calibrate_reject_threshold()` 會在比例失衡時明確警告方向。

**D09 的後續發現：這個偏差在高維度、多幀（真實 104 維 x T=24）下會被
放大。** 用 D06 原本驗證過的樣板數比例（word:reject 約 1:1，20~30 筆）
在低維度（12 維、T=3）下誤拒率能壓到個位數百分比，但同樣比例換到
104 維 x T=24（真實系統的實際規模）時，誤拒率可能逼近 100%——
需要把 word 類別的樣板數拉高到 50 筆以上才能穩定壓低誤拒率
（見 `analysis/similarity/test_recognition_service.py` 的
`test_reject_tof_still_works_at_w1_after_full_service_wiring` 註解）。
`E04`/`E05` 規劃真實 enrollment 錄製次數時，這個「高維度需要更多樣板」
的效應要一併考慮，不能只沿用低維度合成測試調出來的比例。
"""
from pathlib import Path

import numpy as np

from analysis.similarity.scoring import class_distances, fit_reject_threshold

EXCLUDED_QUALITY = {"rejected"}
BAD_CONTRIBUTION_THRESHOLD = 0.0  # 貢獻度 > 0：移除它之後準確率沒變差、甚至更好
IMBALANCE_RATIO_HIGH = 1.3
IMBALANCE_RATIO_LOW = 0.7


# --------------------------------------------------------------------------
# 品質篩選
# --------------------------------------------------------------------------

def filter_enrollment_trials(trials):
    """trials: list of dict，至少含 `"quality"` 鍵。

    排除 `quality == "rejected"`；`"low"` 保留（見模組 docstring）。
    回傳 (kept, quality_counts)：quality_counts 記錄篩選前每個 quality
    值的原始筆數，供報告與「參數/資料被自動調整要記錄」用。
    """
    quality_counts = {}
    for t in trials:
        quality_counts[t["quality"]] = quality_counts.get(t["quality"], 0) + 1
    kept = [t for t in trials if t["quality"] not in EXCLUDED_QUALITY]
    return kept, quality_counts


# --------------------------------------------------------------------------
# 存 / 載樣板
# --------------------------------------------------------------------------

def template_path(root, subject, wear_id):
    """`templates/<subject>_<wear_id>.npz`（D08.md 明訂的存檔慣例）。"""
    return Path(root) / "templates" / f"{subject}_{wear_id}.npz"


def save_templates(templates_by_class, path, subject, wear_id):
    """存成 npz。同一類別的樣板假設同形狀（同一套 D03 `FeatureSeq.data`
    流程產出，理論上本來就該同形狀），疊成 `(n_templates, T, D)` 陣列。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {f"class__{label}": np.stack(templates) for label, templates in templates_by_class.items()}
    arrays["_meta_subject"] = np.array(str(subject))
    arrays["_meta_wear_id"] = np.array(int(wear_id))
    np.savez(path, **arrays)


def load_templates(path, expected_wear_id=None):
    """載入 npz 樣板。

    `expected_wear_id` 給定且與檔案內存的 `wear_id` 不同時，回傳的
    warning 非 None——**跨 session（跨 wear_id）載入要明確警告**，
    不要靜默載入了事（D08.md 明文要求）。
    """
    path = Path(path)
    data = np.load(path, allow_pickle=False)

    templates_by_class = {
        key[len("class__"):]: [row for row in data[key]]
        for key in data.files if key.startswith("class__")
    }
    stored_subject = str(data["_meta_subject"])
    stored_wear_id = int(data["_meta_wear_id"])

    warning = None
    if expected_wear_id is not None and stored_wear_id != expected_wear_id:
        warning = (
            f"樣板存檔於 wear_id={stored_wear_id}，目前指定的是 wear_id={expected_wear_id}"
            "——非同次戴上，準確率可能下降。"
        )
    return templates_by_class, {"subject": stored_subject, "wear_id": stored_wear_id}, warning


# --------------------------------------------------------------------------
# LOOCV
# --------------------------------------------------------------------------

def loocv_accuracy(templates_by_class, dist_fn):
    """Leave-one-out：每一筆樣板當 query，用同一類別「其餘樣板」+
    其他類別全部樣板重新分類，看猜不猜得對真正的類別。

    某一類別只剩 0 筆樣板可比對時（該類別原本就只有 1 筆樣板），
    這筆樣板無法參與 LOOCV，跳過並記錄在 `skipped`，不是靜默漏算。

    回傳 (accuracy, n_evaluated, skipped, details)。
    """
    correct = 0
    total = 0
    skipped = []
    details = []

    for true_label, templates in templates_by_class.items():
        n = len(templates)
        for i in range(n):
            remaining = dict(templates_by_class)
            remaining[true_label] = [t for j, t in enumerate(templates) if j != i]
            if not remaining[true_label]:
                skipped.append({"label": true_label, "index": i})
                continue

            classes, d_class = class_distances(templates[i], remaining, dist_fn)
            pred = classes[int(np.argmin(d_class))]
            is_correct = pred == true_label
            correct += int(is_correct)
            total += 1
            details.append({"label": true_label, "index": i, "predicted": pred, "correct": is_correct})

    accuracy = correct / total if total else float("nan")
    return accuracy, total, skipped, details


def template_contributions(templates_by_class, dist_fn):
    """逐樣板貢獻度：`acc_without - acc_full`。

    貢獻度為正（移除它反而更準）就是壞樣板的訊號——通常是錄的時候
    咳嗽、講錯、或戴法剛好跑掉（D08.md）。

    這是 O(n_templates) 次完整 LOOCV（每次都要重跑一遍所有樣板的
    leave-one-out），不是只看那筆樣板自己猜不猜得對——貢獻度問的是
    「它對其他樣板的判斷有沒有幫助」，兩者是不同的問題。

    回傳 (acc_full, contributions)：contributions 是
    {label: [每筆樣板的貢獻度，NaN 代表該類別只剩這一筆無法計算]}。
    """
    acc_full, _, _, _ = loocv_accuracy(templates_by_class, dist_fn)

    contributions = {}
    for label, templates in templates_by_class.items():
        n = len(templates)
        contribs = []
        for k in range(n):
            reduced_templates = [t for j, t in enumerate(templates) if j != k]
            if not reduced_templates:
                contribs.append(float("nan"))
                continue
            reduced = dict(templates_by_class)
            reduced[label] = reduced_templates
            acc_without, _, _, _ = loocv_accuracy(reduced, dist_fn)
            contribs.append(acc_without - acc_full)
        contributions[label] = contribs

    return acc_full, contributions


def flag_bad_templates(contributions, threshold=BAD_CONTRIBUTION_THRESHOLD):
    """回傳 {label: [壞樣板索引...]}，貢獻度 > threshold 的樣板。"""
    return {
        label: [k for k, c in enumerate(vals) if not np.isnan(c) and c > threshold]
        for label, vals in contributions.items()
    }


def exclude_templates(templates_by_class, bad_indices_by_class):
    """回傳排除壞樣板後的新 dict（不修改原資料）。"""
    return {
        label: [t for k, t in enumerate(templates) if k not in bad_indices_by_class.get(label, [])]
        for label, templates in templates_by_class.items()
    }


def replace_template(templates_by_class, label, index, new_template):
    """把 `label` 底下第 `index` 筆樣板換成 `new_template`，回傳新 dict
    （不修改原資料）。供 C14「單筆重錄」呼叫。
    """
    updated = dict(templates_by_class)
    templates = list(updated[label])
    templates[index] = new_template
    updated[label] = templates
    return updated


# --------------------------------------------------------------------------
# theta_reject 校準（分開校 ToF / Mel，記錄樣板數失衡警告）
# --------------------------------------------------------------------------

def calibrate_reject_threshold(templates_by_class, reject_templates, dist_fn, percentile=95.0):
    """對單一模態的 `dist_fn` 校準 theta_reject，回傳 dict：
    theta、實際用的 percentile、樣板數、樣板數失衡時的警告訊息。
    """
    theta = fit_reject_threshold(reject_templates, dist_fn, percentile=percentile)

    n_reject = len(reject_templates)
    avg_n_word = float(np.mean([len(t) for t in templates_by_class.values()]))
    ratio = n_reject / avg_n_word if avg_n_word > 0 else float("inf")

    warning = None
    if ratio > IMBALANCE_RATIO_HIGH:
        warning = (
            f"_reject 樣板數（{n_reject}）明顯多於詞類別平均樣板數（{avg_n_word:.1f}），"
            "theta_reject 可能校準得過緊，誤拒真詞的機率偏高（D06 的發現）。"
        )
    elif ratio < IMBALANCE_RATIO_LOW:
        warning = (
            f"_reject 樣板數（{n_reject}）明顯少於詞類別平均樣板數（{avg_n_word:.1f}），"
            "theta_reject 可能校準得過鬆，誤放靜止的機率偏高。"
        )

    return {
        "theta": theta,
        "percentile_used": percentile,
        "n_reject_templates": n_reject,
        "avg_n_word_templates": avg_n_word,
        "warning": warning,
    }


def calibrate_tri_reject_thresholds(templates_by_class, reject_templates, slices, dist_fn, percentile=95.0):
    """分別校準 `theta_reject_tof` 與 `theta_reject_mel`（對應 D07
    `TriResult` 的兩個獨立欄位，CONTRACTS §4.3）。

    templates_by_class / reject_templates: 完整 104 維序列（未切片）
    slices: 對應 D03 `FeatureSeq.slices`
    """
    def _modality_dist(sl):
        return lambda a, b: dist_fn(a[:, sl], b[:, sl])

    result_tof = calibrate_reject_threshold(
        templates_by_class, reject_templates, _modality_dist(slices["tof"]), percentile
    )
    result_mel = calibrate_reject_threshold(
        templates_by_class, reject_templates, _modality_dist(slices["mel"]), percentile
    )
    return {"tof": result_tof, "mel": result_mel}
