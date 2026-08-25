"""D18 — Permutation Test：打亂標籤 1000 次的顯著性檢定。

規格見 `stories/D-analysis/D18.md`。回答 D13 的 Silhouette 分數本身答不了
的問題：**這個分數是不是隨機也能達到？** 高維空間裡少量樣本（`E05` 大概
每詞 5-10 次）很容易「看起來分得開」——真正的統計證據是把標籤打亂重跑
N 次，看真實準確率落在 null distribution 的哪個位置。

輸入跟 `D13`（`exp_c_silhouette.py`）一樣是 `D03` 組裝好的
`FeatureSeq.data`。**本模組重用 `exp_c_silhouette.stack_modality()` 做
模態欄位切法，不重寫一份**——欄位切法只應該有一個地方定義。

**ToF-only 的結果是「ToF 有訊號」最強的統計證據，比任何準確率數字都難以
反駁。** 如果 ToF-only 的 p < 0.01，那不論分類準確率高低（準確率會被
樣本數、類別數等因素影響，容易被質疑），「ToF 攜帶了與詞彙相關的資訊」
這個結論本身在統計上站得住——這正是整個專案最需要的那句話。

「開發完成不等於有真實結果」：跟 `D13` 一樣，本模組用假資料開發並測過
就是完整交付，真實結論待 `E05`。`permutation_report()` 一樣有
`is_synthetic` 欄位。
"""
import numpy as np

DEFAULT_N_PERMUTATIONS = 1000
DEFAULT_CV_FOLDS = 5
DEFAULT_RANDOM_STATE = 0
P_VALUE_THRESHOLD = 0.01  # 驗收條件：p < 0.01 算通過


def make_estimator():
    """StandardScaler + SVM，跟 story 實作段落一致（無額外 PCA——這是監督式
    分類顯著性檢定，跟 D13 的非監督分群分數是不同工具，不用照搬 D13 的
    PCA(50) 前處理）。"""
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC

    return make_pipeline(StandardScaler(), SVC())


def run_permutation_test(feature_seqs, labels, modality="all",
                          n_permutations=DEFAULT_N_PERMUTATIONS,
                          cv=DEFAULT_CV_FOLDS, random_state=DEFAULT_RANDOM_STATE,
                          n_jobs=-1):
    """單一模態的 permutation test：5-fold CV 準確率 vs. 打亂標籤 N 次的 null。

    modality: `exp_c_silhouette.MODALITIES` 的其中一個 key
              （ToF-only 對應 "tof_combined"，見驗收條件「必須對 ToF-only
              單獨做一次」）。

    random_state 同時控制 CV 折的切法（`StratifiedKFold(shuffle=True, ...)`）
    與標籤打亂的隨機性——兩者都固定，同樣的輸入永遠得到同樣的 p 值
    （驗收條件：隨機種子固定）。

    `cv` 會被夾在 `[2, min(cv, 該資料集最小類別的樣本數)]`——`StratifiedKFold`
    要求每一折都至少能分到一個樣本，folds 數不能超過最小類別的樣本數。
    `E05` 每詞大概只錄 5-10 次（story 原文），folds=5 剛好卡在邊界，真實資料
    的類別數量不平衡時很容易撞到這個限制，夾住比讓 sklearn 报警或報錯更
    好處理。**實際用掉的 cv 折數記在回傳值的 `cv` 裡**，跟 D13 記錄實際
    PCA 維度是同一個理由：這個值會影響結果，不能默默改掉又不讓人知道。

    回傳 dict: {"modality", "score", "permutation_scores", "pvalue",
                "n_permutations", "cv", "random_state", "passed"}
    """
    from sklearn.model_selection import StratifiedKFold, permutation_test_score

    from analysis.experiments.exp_c_silhouette import stack_modality

    X = stack_modality(feature_seqs, modality)
    y = np.asarray(labels)
    if y.shape[0] != X.shape[0]:
        raise ValueError(f"labels 長度 {y.shape[0]} 與 trial 數 {X.shape[0]} 不一致")

    min_class_count = int(np.min(np.unique(y, return_counts=True)[1]))
    if min_class_count < 2:
        raise ValueError(f"每個類別至少需要 2 筆樣本才能做 CV，最小類別只有 {min_class_count} 筆")
    cv = max(2, min(cv, min_class_count))

    cv_splitter = StratifiedKFold(n_splits=cv, shuffle=True, random_state=random_state)
    estimator = make_estimator()

    score, permutation_scores, pvalue = permutation_test_score(
        estimator, X, y,
        cv=cv_splitter,
        n_permutations=n_permutations,
        n_jobs=n_jobs,
        random_state=random_state,
    )

    return {
        "modality": modality,
        "score": float(score),
        "permutation_scores": permutation_scores,
        "pvalue": float(pvalue),
        "n_permutations": n_permutations,
        "cv": cv,
        "random_state": random_state,
        "passed": bool(pvalue < P_VALUE_THRESHOLD),
    }


def permutation_report(feature_seqs, labels, n_permutations=DEFAULT_N_PERMUTATIONS,
                        cv=DEFAULT_CV_FOLDS, random_state=DEFAULT_RANDOM_STATE,
                        n_jobs=-1, is_synthetic=True):
    """驗收條件「全模態與 ToF-only 各跑一次」的組合入口。

    回傳 dict: {"is_synthetic", "all": <run_permutation_test 結果>,
                "tof_only": <run_permutation_test 結果，modality="tof_combined">}
    """
    all_result = run_permutation_test(
        feature_seqs, labels, "all", n_permutations, cv, random_state, n_jobs)
    tof_result = run_permutation_test(
        feature_seqs, labels, "tof_combined", n_permutations, cv, random_state, n_jobs)
    return {"is_synthetic": is_synthetic, "all": all_result, "tof_only": tof_result}


def plot_null_distribution(result, ax=None):
    """null distribution 直方圖，標上真實準確率的位置（驗收條件）。

    回傳 matplotlib Figure；存檔或顯示交給呼叫端決定。

    圖表文字一律英文（不是中文說明的偏好問題）：這些圖最後會進論文／簡報，
    跑圖的機器不保證有 CJK 字型，字型缺字是靜默失敗——圖出得來，字變
    方塊，容易到簡報現場才發現。中文討論留在 `format_report()` 與
    `reports/*.md`，那裡是純文字，沒有字型問題。
    """
    import matplotlib.pyplot as plt

    if ax is None:
        fig, ax = plt.subplots()
    else:
        fig = ax.figure

    ax.hist(result["permutation_scores"], bins=30, alpha=0.7, color="steelblue",
             label="null (shuffled labels)")
    ax.axvline(result["score"], color="crimson", linestyle="--",
               label=f"true accuracy = {result['score']:.3f}")
    ax.set_xlabel("5-fold CV accuracy")
    ax.set_ylabel("count")
    ax.set_title(
        f"{result['modality']}: p={result['pvalue']:.4f} "
        f"(n_permutations={result['n_permutations']}, cv={result['cv']})"
    )
    ax.legend()
    fig.tight_layout()
    return fig


def format_report(report):
    """把 `permutation_report()` 的結果轉成人類可讀的 Markdown 字串。

    ToF-only 的結果會被特別標示與討論（驗收條件）。
    """
    lines = []
    if report["is_synthetic"]:
        lines.append("> ⚠️ **假資料（synthetic）產生的分數，不是真實結論。**"
                      " 真實結論待 `E05` 資料蒐集後重跑本模組取得。")
        lines.append("")

    lines.append("## Permutation Test 結果")
    lines.append("")
    lines.append("| 組合 | CV 準確率 | p 值 | n_permutations | cv folds | 判定 |")
    lines.append("|---|---|---|---|---|---|")
    for key, label in (("all", "全模態"), ("tof_only", "ToF-only")):
        r = report[key]
        status = f"p < {P_VALUE_THRESHOLD}（顯著）" if r["passed"] else "未達顯著"
        lines.append(
            f"| {label} | {r['score']:.3f} | {r['pvalue']:.4f} | "
            f"{r['n_permutations']} | {r['cv']} | {status} |"
        )
    lines.append("")

    tof = report["tof_only"]
    lines.append("## ToF-only 討論")
    lines.append("")
    if tof["passed"]:
        lines.append(
            f"ToF-only 的 p 值為 `{tof['pvalue']:.4f}`，低於 {P_VALUE_THRESHOLD}——"
            "不論分類準確率的絕對高低（準確率會被樣本數、類別數等因素影響，"
            "容易被質疑），**「ToF 攜帶了與詞彙相關的資訊」這個結論本身在"
            "統計上成立**。這是比任何準確率數字都難以反駁的證據。"
        )
    else:
        lines.append(
            f"ToF-only 的 p 值為 `{tof['pvalue']:.4f}`，未低於 {P_VALUE_THRESHOLD}——"
            "以目前這批資料，還不能排除「ToF 的分類表現是隨機達到的」這個"
            "可能性。真實資料（`E05`）上若也是如此，需要重新檢視特徵或"
            "樣本數，而不是宣稱 ToF 有效。"
        )
    lines.append("")

    return "\n".join(lines)
