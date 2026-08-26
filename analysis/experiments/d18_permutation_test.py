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

## 🔴 分組驗證（`groups=`）：為什麼要，以及它改變了什麼

`E05` 的資料是**同一個受試者、同一次戴上、連續錄很多筆**。同一次戴上的
兩筆錄音共享戴法幾何、環境光、感測器溫度、當下的說話習慣——**它們不是
獨立樣本**。隨機切 CV 會把同一次戴上的樣本**同時放進訓練集與測試集**，
模型可以靠「認出這是哪一次戴的」而不是「認出這個詞」來得分。

傳 `groups=`（實務上就是每筆 trial 的 `wear_id`）之後有**兩件事**同時改變：

1. **CV 改用 `StratifiedGroupKFold`** —— 一次戴上的樣本要嘛全在訓練、
   要嘛全在測試，堵住上面那個洩漏。
2. ⚠️ **虛無假設也跟著變**：`sklearn` 的 `permutation_test_score` 在給了
   `groups` 之後，標籤是**在同一個 group 內部**打亂，不是全體打亂。

第 2 點是刻意的、而且更保守：全體打亂會把「戴法與詞彙的關聯」也一起
破壞掉，讓檢定變得比較好過。**組內打亂問的是「在同一次戴上之內，特徵
還分得出詞嗎」**——那才是我們真正想證明的事。

> ⚠️ **這個改動會讓準確率下降，而那是對的。** 舊數字可能被戴法洩漏灌水。
> **在真實資料到手之前改，是嚴謹；拿到數字之後再改，是可疑。**

**只有一個 group 時（第一批資料很可能只戴一次）分組驗證做不到**——
此時回傳的 `grouping` 是 `"ungrouped_single_group"`，報告會明講
「**分組驗證無法進行**」。**絕不安靜退回舊行為**：宣稱一個沒做的方法學
保證，比不做還糟。

## p 值的解析度

見 `p_value_floor()`。**報告一律印出置換次數與 p 值下限**——
N=200 時 p 最小只能到 0.005，而通過門檻是 0.01，**只差兩格**。
"""
import numpy as np

DEFAULT_N_PERMUTATIONS = 1000
DEFAULT_CV_FOLDS = 5
DEFAULT_RANDOM_STATE = 0
P_VALUE_THRESHOLD = 0.01  # 驗收條件：p < 0.01 算通過


def p_value_floor(n_permutations):
    """這個置換次數下，p 值**最小可能**是多少。

    `sklearn.permutation_test_score` 算的是 `p = (C + 1) / (N + 1)`，
    所以即使一次置換都沒贏過真實分數，p 也不會小於 `1/(N+1)`。

        N=200   → 0.00498     ← `run_all` 的預設
        N=1000  → 0.000999    ← 本模組的預設
        N=9999  → 0.0001

    **這個數字必須印在報告上。** N=200 時「p = 0.005」與「p 小到量不出來」
    在數值上長得一模一樣，而通過門檻是 0.01——**只差兩格**。讀報告的人
    看不到 N 就無從判斷這個 p 值的解析度。

    ⚠️ **絕對不要宣稱 `p < 0.0001`**，除非 N ≥ 9999。
    """
    return 1.0 / (int(n_permutations) + 1)


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
                          n_jobs=-1, groups=None):
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

    grouping, note, groups_array = _resolve_grouping(groups, y.shape[0])
    if grouping == "grouped":
        from sklearn.model_selection import StratifiedGroupKFold

        n_groups = int(np.unique(groups_array).size)
        # `StratifiedGroupKFold` 的折數不能超過 group 數——每一折至少要拿得到
        # 一個完整的 group。
        cv = max(2, min(cv, n_groups))
        cv_splitter = StratifiedGroupKFold(n_splits=cv, shuffle=True,
                                            random_state=random_state)
    else:
        cv_splitter = StratifiedKFold(n_splits=cv, shuffle=True, random_state=random_state)

    estimator = make_estimator()

    score, permutation_scores, pvalue = permutation_test_score(
        estimator, X, y,
        groups=groups_array,
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
        "p_floor": p_value_floor(n_permutations),
        "cv": cv,
        "random_state": random_state,
        "grouping": grouping,
        "n_groups": (int(np.unique(groups_array).size)
                     if groups_array is not None else None),
        "grouping_note": note,
        "passed": bool(pvalue < P_VALUE_THRESHOLD),
    }


def _resolve_grouping(groups, n_samples):
    """決定這次要不要（能不能）做分組驗證。回傳 `(grouping, note, groups_array)`。

    三種結果，**在報告裡看起來完全不同**：

    * `"grouped"` —— 真的做了分組驗證
    * `"ungrouped_no_groups_given"` —— 呼叫端沒給 group，**沒有要求過**
    * `"ungrouped_single_group"` —— 🔴 **要求了但做不到**（只有一個 group）

    🔴 **第三種絕對不能安靜地退回舊行為。** 使用者的第一批資料很可能
    只戴一次（`wear_id` 只有一個），此時「分組驗證」根本沒發生——
    如果報告看起來跟真的做了分組一樣，那是**最糟的失敗方式**：
    宣稱了一個沒做的方法學保證。所以這裡回一個明確的狀態 + 一句說明，
    `format_report()` 會把它印出來。
    """
    if groups is None:
        return "ungrouped_no_groups_given", None, None

    groups_array = np.asarray(groups)
    if groups_array.shape[0] != n_samples:
        raise ValueError(
            f"groups 長度 {groups_array.shape[0]} 與樣本數 {n_samples} 不一致")

    n_groups = int(np.unique(groups_array).size)
    if n_groups < 2:
        return ("ungrouped_single_group",
                f"要求了分組驗證，但資料裡只有 {n_groups} 個 group"
                f"（例如只戴了一次），**分組驗證無法進行**——這一輪跑的是"
                f"未分組的 CV，結果**可能被組內洩漏灌水**。要做分組驗證，"
                f"需要至少 2 個不同的 wear_id。",
                None)
    return "grouped", None, groups_array


def permutation_report(feature_seqs, labels, n_permutations=DEFAULT_N_PERMUTATIONS,
                        cv=DEFAULT_CV_FOLDS, random_state=DEFAULT_RANDOM_STATE,
                        n_jobs=-1, is_synthetic=True, groups=None):
    """驗收條件「全模態與 ToF-only 各跑一次」的組合入口。

    回傳 dict: {"is_synthetic", "all": <run_permutation_test 結果>,
                "tof_only": <run_permutation_test 結果，modality="tof_combined">}
    """
    all_result = run_permutation_test(
        feature_seqs, labels, "all", n_permutations, cv, random_state, n_jobs,
        groups=groups)
    tof_result = run_permutation_test(
        feature_seqs, labels, "tof_combined", n_permutations, cv, random_state, n_jobs,
        groups=groups)
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


def _resolution_lines(report):
    """把 p 值的解析度講出來。**沒有這一段，讀報告的人無從判斷 p 值有多細。**"""
    n = report["all"]["n_permutations"]
    floor = p_value_floor(n)
    lines = [
        f"> **p 值的解析度**：置換 {n} 次，所以 p 的**最小可能值是 "
        f"{floor:.5f}**（`p = (C+1)/(N+1)`）。",
    ]
    if floor >= P_VALUE_THRESHOLD / 2:
        lines.append(
            f"> 🔴 **這個下限（{floor:.5f}）與通過門檻（{P_VALUE_THRESHOLD}）"
            f"只差不到一個數量級**——「p 剛好壓在門檻下」與「p 小到量不出來」"
            f"在這個解析度下**分不出來**。要寫進論文的那一輪請提高置換次數"
            f"（1000 次的下限是 {p_value_floor(1000):.5f}）。"
        )
    lines.append(f"> ⚠️ **不可宣稱 `p < {floor:.5f}` 以下的任何數字。**")
    lines.append("")
    return lines


def _grouping_lines(report):
    """分組驗證做了沒有——**三種狀態在報告裡看起來必須完全不同**。"""
    grouping = report["all"].get("grouping", "ungrouped_no_groups_given")
    lines = []
    if grouping == "grouped":
        n_groups = report["all"].get("n_groups")
        lines += [
            f"> ✅ **有做分組驗證**（{n_groups} 個 group，`StratifiedGroupKFold`）。"
            "同一次戴上的樣本不會同時出現在訓練與測試集，"
            "而且標籤是**在同一個 group 內部**打亂——"
            "問的是「在同一次戴上之內，特徵還分得出詞嗎」。",
            "",
        ]
    elif grouping == "ungrouped_single_group":
        lines += [
            "> 🔴 **分組驗證無法進行。** "
            + (report["all"].get("grouping_note") or ""),
            "> **這一輪的準確率與 p 值可能被組內洩漏灌水**"
            "（模型可能靠「認出這是哪一次戴的」得分）。",
            "",
        ]
    else:
        lines += [
            "> ⚠️ **這一輪沒有做分組驗證**（呼叫端沒有提供 `groups`）。"
            "同一次戴上的多筆錄音**不是獨立樣本**——隨機切 CV 會讓同一次"
            "戴上的樣本同時出現在訓練與測試集。要做的話請傳每筆 trial 的 "
            "`wear_id` 當 `groups`。",
            "",
        ]
    return lines


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
    lines.append("| 組合 | CV 準確率 | p 值 | n_permutations | **p 下限** | cv folds | 判定 |")
    lines.append("|---|---|---|---|---|---|---|")
    for key, label in (("all", "全模態"), ("tof_only", "ToF-only")):
        r = report[key]
        status = f"p < {P_VALUE_THRESHOLD}（顯著）" if r["passed"] else "未達顯著"
        floor = r.get("p_floor", p_value_floor(r["n_permutations"]))
        lines.append(
            f"| {label} | {r['score']:.3f} | {r['pvalue']:.4f} | "
            f"{r['n_permutations']} | {floor:.5f} | {r['cv']} | {status} |"
        )
    lines.append("")
    lines += _resolution_lines(report)
    lines += _grouping_lines(report)

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
