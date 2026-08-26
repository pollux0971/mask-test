"""效果量與信賴區間 —— 回答「這個差異有多大」，不只是「有沒有差異」。

## 為什麼需要這支模組

驗證報告目前給的是 **p 值與 PASS/FAIL**。那答得了「有沒有差異」，
答不了口試委員一定會問的下一句：**「這個差異有多大？」**

* **只給 p 值的話，樣本一多什麼都會顯著。**
* 反過來，樣本少的時候（`E05` 第一批可能每詞只有 3 筆）**什麼都不顯著**，
  但那不代表沒有效果——只代表**檢定力不足**。
* **效果量與信賴區間同時回答這兩個方向。**

## 🔴 `wilson_interval()` 是從前端**移植**過來的，不是重寫

`vl53l7cx_test/monitor/panel/js/modes/quiz.js:602` 的 `wilsonInterval(k, n)`
是 `C21` 為了同一個目的寫的。**這裡照抄它的公式，不重新推導**——
同一個統計量有兩份獨立實作，遲早會漂移一份，而漂移的那份不會報錯，
只會讓「Demo 上的數字」跟「報告上的數字」對不起來。

⚠️ **兩邊要一致。** 改動任何一邊時**兩邊一起改**，並確認
`test_matches_the_c21_worked_example` 仍然通過——那條測試用的是
`C21.md` 裡的實例（n=12, k=10 → 83.3%, 95% CI [55.2%, 95.3%]），
**它同時釘住 Python 與 JS 兩份實作**。

## 🔴 為什麼一定要 Wilson，不能用常態近似

`C21.md` 的原始立論：**常態近似在 n < 30 時會給出荒謬的區間**
（上界 > 100%、下界 < 0%）。`E05` 的樣本數正好落在那個區間裡，
而**分組之後每組樣本更少**。

## ⚠️ 小樣本的區間會很寬，**那個寬度是重點不是瑕疵**

3 筆全對，Wilson 95% CI 大約是 **[43.8%, 100%]**——看起來很難看，
但它誠實地說出「三筆全對還不足以宣稱高準確率」。

🔴 **不要為了好看而縮小它**（例如改用 90% 信心水準、或改回常態近似）。
**寬的區間正是「多錄一點」這個建議的量化依據。**

## 每個效果量都附「怎麼解讀」

`d = 0.4` 對不熟這個領域的人沒有意義。所有回傳值都帶一個
`interpretation` 字串，講白話那個數字代表什麼、以及**它的已知限制**。
"""
import math

# `quiz.js` 的 `WILSON_Z95`。**兩邊必須是同一個數字。**
WILSON_Z95 = 1.959963985

# Cohen 的慣例分界（1988）。⚠️ 那是**跨領域的經驗法則不是定律**，
# 每個 `interpretation` 都會註明這件事。
COHEN_D_BOUNDS = ((0.2, "negligible"), (0.5, "small"), (0.8, "medium"))

# `D13` 用的 Silhouette 四級（照簡報，見 `exp_c_silhouette._VERDICT_THRESHOLDS`）。
# 這裡只負責**解釋尺度**，不重新定義門檻。
SILHOUETTE_BOUNDS = ((0.15, "fail"), (0.3, "marginal"), (0.5, "standard_pass"))


def wilson_interval(k, n, z=WILSON_Z95):
    """二項比例的 Wilson score 區間。回傳 `(lower, upper)`，範圍 `[0, 1]`。

    **從 `panel/js/modes/quiz.js:602` 移植，公式逐行對應。**
    見模組說明「不是重寫」那一段。

    `n == 0` 回 `(0.0, 1.0)`——**沒有資料時區間就是整個值域**，
    那是唯一誠實的答案（跟前端的行為一致）。
    """
    n = int(n)
    if n == 0:
        return 0.0, 1.0
    if not 0 <= k <= n:
        raise ValueError(f"k 必須落在 [0, n]，收到 k={k} n={n}")

    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = phat + z2 / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n))
    return (max(0.0, (center - margin) / denom),
            min(1.0, (center + margin) / denom))


def accuracy_with_ci(k, n, *, n_classes=None):
    """準確率 + Wilson 95% CI + 與隨機基準的比較。

    `n_classes` 給了的話會算出隨機猜的基準（`1/n_classes`），並判斷
    **CI 的下界有沒有高過它**——那才是「比隨機好」的正確說法。

    🔴 **「準確率 70% 聽起來很高」在 8 選 1（隨機 12.5%）與 2 選 1
    （隨機 50%）是完全不同的兩件事。** 沒有基準的準確率是不能解讀的。
    """
    n = int(n)
    point = k / n if n else float("nan")
    lower, upper = wilson_interval(k, n)

    chance = None
    above_chance = None
    if n_classes:
        chance = 1.0 / int(n_classes)
        # 用 CI 下界比，不是用點估計比——點估計比基準高但 CI 蓋住基準時，
        # **那不叫「比隨機好」**。
        above_chance = lower > chance

    return {
        "k": int(k), "n": n,
        "accuracy": point,
        "ci_lower": lower, "ci_upper": upper,
        "ci_width": upper - lower,
        "chance_level": chance,
        "above_chance": above_chance,
        "method": "wilson_score_95",
        "interpretation": _accuracy_interpretation(point, lower, upper, n, chance,
                                                   above_chance),
    }


def _accuracy_interpretation(point, lower, upper, n, chance, above_chance):
    if n == 0:
        return "沒有樣本，無法估計準確率。"
    parts = [f"{n} 筆中對 {round(point * n)} 筆（{point:.1%}），"
             f"95% 信賴區間 [{lower:.1%}, {upper:.1%}]。"]

    width = upper - lower
    if width > 0.4:
        parts.append(
            f"⚠️ **這個區間很寬（{width:.0%}），因為樣本只有 {n} 筆。**"
            "寬度本身就是結論：**目前的資料還不足以把準確率釘在一個窄範圍內**，"
            "要縮小它只能多錄。**這不是計算的瑕疵。**")
    if chance is not None:
        parts.append(f"隨機猜的基準是 {chance:.1%}。")
        if above_chance:
            parts.append(
                f"**信賴區間的下界（{lower:.1%}）高於基準**——"
                "「比隨機好」這句話站得住。")
        else:
            parts.append(
                f"🔴 **信賴區間蓋住了基準**（下界 {lower:.1%} ≤ {chance:.1%}）"
                "——**目前還不能宣稱比隨機猜好**，即使點估計看起來比較高。")
    return " ".join(parts)


def permutation_effect_size(score, permutation_scores):
    """置換檢定的**標準化效果量**：觀測值距離虛無分布幾個標準差。

        z = (observed − mean(null)) / std(null)

    **為什麼需要它**：p 值只說「觀測值落在虛無分布的尾巴」，
    但**尾巴多遠是 p 值答不了的**——尤其置換次數有限時 p 會觸底
    （見 `d18_permutation_test.p_value_floor()`）。**兩個 p 值同樣是
    0.001 的結果，效果量可能差好幾倍。**

    🔴 **虛無分布的標準差是 0 時回 `None`，不回一個假數字。**
    那代表所有置換都拿到同一個分數（樣本太少、或分類器退化成常數輸出），
    此時「距離幾個標準差」沒有定義——**填一個數字進去會讓下游算出
    看起來正常的結論。**
    """
    import numpy as np

    null = np.asarray(list(permutation_scores), dtype=np.float64)
    if null.size == 0:
        return {"z": None, "reason": "沒有置換分數", "interpretation": "無法計算。"}

    null_mean = float(np.mean(null))
    null_std = float(np.std(null, ddof=1)) if null.size > 1 else 0.0
    if null_std <= 0.0:
        return {
            "z": None,
            "null_mean": null_mean, "null_std": null_std,
            "n_permutations": int(null.size),
            "reason": "虛無分布的標準差是 0——所有置換都拿到同一個分數",
            "interpretation": (
                "🔴 **無法計算標準化效果量。** 所有置換都得到同一個分數，"
                "代表樣本太少或分類器退化成常數輸出。"
                "**這個情況下 p 值本身也不可信**——請先確認資料量。"),
        }

    z = (float(score) - null_mean) / null_std
    return {
        "z": z,
        "null_mean": null_mean, "null_std": null_std,
        "n_permutations": int(null.size),
        "reason": None,
        "interpretation": (
            f"觀測到的分數（{score:.3f}）比打亂標籤後的平均（{null_mean:.3f}）"
            f"高出 **{z:.1f} 個標準差**。"
            + (" 這是很大的分離。" if z >= 3 else
               " 這是中等的分離。" if z >= 2 else
               " 🔴 **這個分離很小**——即使 p 值通過，實際差距不大。")
            + " ⚠️ 這個 z 不是 p 值的替代品，是它的補充："
              "**p 值會因置換次數不足而觸底，z 不會。**"),
    }


def cohens_d(group_a, group_b):
    """兩組數值的 Cohen's d（pooled standard deviation 版本）。

    用途：比較兩個模態在**每一折 CV** 上的準確率，例如
    `cohens_d(tof_fold_scores, mel_fold_scores)`。

    🔴 **pooled std 是 0 時回 `None`。** 兩組都完全沒有變異（例如每一折
    都拿到一樣的分數），差距沒有可以拿來標準化的尺度——
    **不要回 `inf`，那會在下游變成一個「效果無限大」的假結論。**

    ⚠️ **CV 折之間不獨立**（同一批資料的重複切分），所以這個 d
    **不能拿來做顯著性檢定**，只能當描述性的效果量。這一點寫在
    `interpretation` 裡。
    """
    import numpy as np

    a = np.asarray(list(group_a), dtype=np.float64)
    b = np.asarray(list(group_b), dtype=np.float64)
    if a.size < 2 or b.size < 2:
        return {"d": None, "reason": f"每組至少需要 2 個值，收到 {a.size} 與 {b.size}",
                "interpretation": "樣本不足，無法計算效果量。"}

    var_a, var_b = float(np.var(a, ddof=1)), float(np.var(b, ddof=1))
    pooled = math.sqrt(((a.size - 1) * var_a + (b.size - 1) * var_b)
                       / (a.size + b.size - 2))
    if pooled <= 0.0:
        return {
            "d": None, "mean_a": float(np.mean(a)), "mean_b": float(np.mean(b)),
            "reason": "pooled standard deviation 是 0——兩組都完全沒有變異",
            "interpretation": (
                "🔴 **無法計算效果量。** 兩組的每一個值都相同，"
                "沒有可以拿來標準化差距的尺度。**這通常代表樣本太少。**"),
        }

    d = (float(np.mean(a)) - float(np.mean(b))) / pooled
    return {
        "d": d,
        "mean_a": float(np.mean(a)), "mean_b": float(np.mean(b)),
        "pooled_std": pooled,
        "magnitude": _cohen_magnitude(abs(d)),
        "reason": None,
        "interpretation": (
            f"兩組平均差 {np.mean(a) - np.mean(b):+.3f}，標準化後 "
            f"**d = {d:+.2f}**（{_cohen_magnitude(abs(d))}）。"
            " ⚠️ 「大／中／小」用的是 Cohen (1988) 的**跨領域經驗法則**，"
            "不是這個領域的標準——**同一個 d 在不同領域的實務意義不同**。"
            " ⚠️ CV 折之間**不獨立**（同一批資料的重複切分），"
            "所以這個 d **只能當描述性指標，不能拿來做顯著性檢定**。"),
    }


def _cohen_magnitude(abs_d):
    for bound, name in COHEN_D_BOUNDS:
        if abs_d < bound:
            return name
    return "large"


def silhouette_interpretation(score):
    """Silhouette 分數的解讀。**它本身就是效果量**，這裡只解釋尺度。

    ⚠️ **不要把它跟 p 值放在一起讀成「顯著性」。** 它回答的是
    「**分得多開**」，p 值回答的是「**這個分開是不是隨機也做得到**」——
    兩個不同的問題，而且**一個高一個低是完全可能的**（樣本少時常見）。
    """
    score = float(score)
    for bound, name in SILHOUETTE_BOUNDS:
        if score < bound:
            level = name
            break
    else:
        level = "strong"

    return {
        "score": score,
        "level": level,
        "scale": "[-1, 1]；越接近 1 代表同類越聚、異類越分",
        "interpretation": (
            f"Silhouette = **{score:.3f}**（{level}）。"
            " **這個數字本身就是效果量**——它已經是標準化的（值域 [-1, 1]），"
            "不需要再換算。"
            " ⚠️ 它回答「分得多開」，**不回答「這個分開是不是隨機也做得到」**"
            "（那是置換檢定的工作）。**兩者可以一高一低**，樣本少時尤其常見。"
            + ("" if score >= 0 else
               " 🔴 **負值代表樣本平均而言離「別的類別」比離「自己的類別」更近**"
               "——那不只是「分不開」，是**分類方向本身有問題**。")),
    }


def format_effect_size_section(entries, title="效果量與信賴區間"):
    """把一批效果量結果轉成報告用的 Markdown。

    `entries` 是 `[(label, result_dict), ...]`；每個 `result_dict` 是本模組
    任一函式的回傳值（都有 `interpretation`）。

    **每一項都印出解讀文字**，不只印數字——見模組說明最後一段。
    """
    lines = [f"## {title}", "",
             "> **p 值回答「有沒有差異」，這一節回答「差異有多大」。**"
             " 兩個都要看：樣本多的時候 p 值容易顯著但差距可能很小，"
             "樣本少的時候差距可能很大但 p 值不顯著。", ""]
    for label, result in entries:
        lines.append(f"### {label}")
        lines.append("")
        lines.append(result.get("interpretation", "（沒有解讀文字）"))
        lines.append("")
        if result.get("reason"):
            lines.append(f"> ⚠️ 無法計算的原因：{result['reason']}")
            lines.append("")
    return "\n".join(lines)
