"""`analysis/reporting/effect_size.py` 的測試。

最重要的一條是 `test_matches_the_c21_worked_example`——它同時釘住 Python
與 JS 兩份 Wilson 實作，**兩邊漂移時會紅**。
"""
import math

import numpy as np
import pytest

from analysis.reporting.effect_size import (
    WILSON_Z95,
    accuracy_with_ci,
    cohens_d,
    format_effect_size_section,
    permutation_effect_size,
    silhouette_interpretation,
    wilson_interval,
)


# ------------------------------------------------------- Wilson：移植正確性


def test_matches_the_c21_worked_example():
    """🔴 **這條同時釘住 Python 與 JS 兩份實作。**

    `C21.md` 的實例（也寫在 `quiz.js:594` 的註解裡）：
    n=12, k=10 → 83.3%，95% CI [55.2%, 95.3%]。

    **兩邊漂移時這條會紅。** 改動任一邊時兩邊一起改。
    """
    lower, upper = wilson_interval(10, 12)
    assert 10 / 12 == pytest.approx(0.8333, abs=5e-5)
    assert lower == pytest.approx(0.552, abs=5e-4)
    assert upper == pytest.approx(0.953, abs=5e-4)


def test_z_constant_matches_the_front_end():
    """`quiz.js` 的 `WILSON_Z95 = 1.959963985`。**必須是同一個數字。**"""
    assert WILSON_Z95 == 1.959963985


def test_no_samples_gives_the_whole_range():
    """沒有資料時區間就是整個值域——那是唯一誠實的答案（跟前端一致）。"""
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_interval_never_leaves_zero_to_one():
    """🔴 **常態近似在小樣本會給出 >100% / <0% 的區間，Wilson 不會。**
    那正是 `C21` 選它的理由。"""
    for n in range(1, 40):
        for k in range(n + 1):
            lower, upper = wilson_interval(k, n)
            assert 0.0 <= lower <= upper <= 1.0, (k, n)


def test_normal_approximation_would_have_been_nonsense():
    """對照組：同一組數字用常態近似會超出值域——**證明換 Wilson 不是形式主義**。"""
    k, n = 3, 3
    phat = k / n
    normal_upper = phat + WILSON_Z95 * math.sqrt(phat * (1 - phat) / n)
    assert normal_upper == pytest.approx(1.0)      # 常態近似退化成 [1,1]
    lower, upper = wilson_interval(k, n)
    assert lower < 0.5, "Wilson 會誠實地說『三筆全對還不夠』"


def test_interval_narrows_as_n_grows():
    widths = [wilson_interval(int(0.8 * n), n)[1] - wilson_interval(int(0.8 * n), n)[0]
              for n in (5, 20, 100, 500)]
    assert widths == sorted(widths, reverse=True)


def test_k_out_of_range_raises():
    with pytest.raises(ValueError, match=r"\[0, n\]"):
        wilson_interval(5, 3)


# ---------------------------------------------- 準確率：小樣本的寬度是重點


def test_three_of_three_gives_a_very_wide_interval():
    """⚠️ **那個寬度是結論不是瑕疵**：三筆全對還不足以宣稱高準確率。"""
    result = accuracy_with_ci(3, 3)
    assert result["accuracy"] == 1.0
    assert result["ci_lower"] < 0.5
    assert result["ci_width"] > 0.4
    assert "寬度本身就是結論" in result["interpretation"]
    assert "不是計算的瑕疵" in result["interpretation"]


def test_above_chance_uses_the_ci_lower_bound_not_the_point_estimate():
    """🔴 **點估計高過基準但 CI 蓋住基準時，那不叫「比隨機好」。**"""
    # 4 選 1（基準 25%），2/3 正確：點估計 66.7% 但 CI 下界很低
    result = accuracy_with_ci(2, 3, n_classes=4)
    assert result["accuracy"] > result["chance_level"]
    assert result["ci_lower"] < result["chance_level"]
    assert result["above_chance"] is False
    assert "還不能宣稱比隨機猜好" in result["interpretation"]


def test_above_chance_is_true_when_the_bound_clears_it():
    result = accuracy_with_ci(45, 50, n_classes=8)
    assert result["above_chance"] is True
    assert "站得住" in result["interpretation"]


def test_chance_level_changes_the_meaning_of_the_same_accuracy():
    """🔴 **「70% 準確率」在 8 選 1 與 2 選 1 是完全不同的兩件事。**"""
    eight = accuracy_with_ci(70, 100, n_classes=8)
    two = accuracy_with_ci(70, 100, n_classes=2)
    assert eight["accuracy"] == two["accuracy"]
    assert eight["above_chance"] is True
    assert two["above_chance"] is True          # 兩者都高過各自基準
    assert eight["chance_level"] < two["chance_level"]
    assert f"{eight['chance_level']:.1%}" in eight["interpretation"]


def test_no_chance_level_when_class_count_unknown():
    result = accuracy_with_ci(7, 10)
    assert result["chance_level"] is None and result["above_chance"] is None
    assert "隨機猜的基準" not in result["interpretation"]


# ------------------------------------------------- 置換檢定的標準化效果量


def test_permutation_effect_size_is_distance_in_null_sigmas():
    rng = np.random.RandomState(0)
    null = rng.normal(0.25, 0.05, 500)
    result = permutation_effect_size(0.55, null)
    expected = (0.55 - np.mean(null)) / np.std(null, ddof=1)
    assert result["z"] == pytest.approx(expected)
    assert result["n_permutations"] == 500


def test_zero_null_spread_returns_none_not_a_fake_number():
    """🔴 **所有置換都同分時「距離幾個標準差」沒有定義。**
    填一個數字會讓下游算出看起來正常的結論。"""
    result = permutation_effect_size(0.9, [0.25] * 50)
    assert result["z"] is None
    assert "標準差是 0" in result["reason"]
    assert "資料量" in result["interpretation"]


def test_empty_permutation_scores_is_handled():
    assert permutation_effect_size(0.5, [])["z"] is None


def test_small_effect_is_called_out_even_when_positive():
    """⚠️ **即使 p 值通過，效果量小就要說。**"""
    rng = np.random.RandomState(1)
    null = rng.normal(0.5, 0.1, 400)
    weak = permutation_effect_size(0.61, null)
    assert 0 < weak["z"] < 2
    assert "這個分離很小" in weak["interpretation"]

    strong = permutation_effect_size(0.95, null)
    assert strong["z"] > 3
    assert "很大的分離" in strong["interpretation"]


def test_interpretation_says_z_complements_p_not_replaces_it():
    rng = np.random.RandomState(2)
    result = permutation_effect_size(0.8, rng.normal(0.25, 0.05, 100))
    assert "不是 p 值的替代品" in result["interpretation"]
    assert "觸底" in result["interpretation"]


# ------------------------------------------------------------- Cohen's d


def test_cohens_d_matches_the_pooled_formula():
    a = [0.80, 0.82, 0.78, 0.85, 0.79]
    b = [0.60, 0.63, 0.58, 0.65, 0.61]
    result = cohens_d(a, b)
    va, vb = np.var(a, ddof=1), np.var(b, ddof=1)
    pooled = math.sqrt((4 * va + 4 * vb) / 8)
    assert result["d"] == pytest.approx((np.mean(a) - np.mean(b)) / pooled)
    assert result["magnitude"] == "large"


def test_cohens_d_sign_follows_the_argument_order():
    a, b = [0.6, 0.62, 0.58], [0.8, 0.82, 0.78]
    assert cohens_d(a, b)["d"] < 0
    assert cohens_d(b, a)["d"] > 0


def test_zero_pooled_variance_returns_none_not_inf():
    """🔴 **不要回 `inf`**——那在下游會變成「效果無限大」的假結論。"""
    result = cohens_d([0.7] * 5, [0.5] * 5)
    assert result["d"] is None
    assert "pooled standard deviation 是 0" in result["reason"]
    assert "樣本太少" in result["interpretation"]


def test_too_few_values_returns_none():
    assert cohens_d([0.5], [0.6, 0.7])["d"] is None


def test_interpretation_warns_that_cv_folds_are_not_independent():
    """⚠️ CV 折之間不獨立，所以這個 d 不能拿來做顯著性檢定。"""
    text = cohens_d([0.8, 0.82, 0.78], [0.6, 0.63, 0.58])["interpretation"]
    assert "不獨立" in text
    assert "不能拿來做顯著性檢定" in text
    assert "經驗法則" in text          # Cohen 的分界不是這個領域的標準


# ----------------------------------------------------------- Silhouette


@pytest.mark.parametrize("score,level", [
    (0.05, "fail"), (0.2, "marginal"), (0.4, "standard_pass"), (0.7, "strong"),
])
def test_silhouette_levels_match_d13(score, level):
    assert silhouette_interpretation(score)["level"] == level


def test_silhouette_is_described_as_an_effect_size_already():
    text = silhouette_interpretation(0.42)["interpretation"]
    assert "本身就是效果量" in text
    assert "不需要再換算" in text


def test_silhouette_interpretation_separates_size_from_significance():
    """⚠️ **不要把 Silhouette 跟 p 值放在一起讀成「顯著性」。**"""
    text = silhouette_interpretation(0.42)["interpretation"]
    assert "不回答" in text and "隨機也做得到" in text
    assert "一高一低" in text


def test_negative_silhouette_gets_a_stronger_warning():
    """負值不只是「分不開」，是分類方向本身有問題。"""
    text = silhouette_interpretation(-0.2)["interpretation"]
    assert "分類方向本身有問題" in text
    assert "分類方向本身有問題" not in silhouette_interpretation(0.2)["interpretation"]


# ------------------------------------------------------------- 報告輸出


def test_section_prints_every_interpretation():
    """**每一項都要印出解讀文字，不只印數字。**"""
    entries = [
        ("ToF-only 準確率", accuracy_with_ci(18, 24, n_classes=8)),
        ("Silhouette", silhouette_interpretation(0.42)),
        ("ToF vs Mel", cohens_d([0.8, 0.82, 0.78], [0.6, 0.63, 0.58])),
    ]
    text = format_effect_size_section(entries)
    for label, result in entries:
        assert f"### {label}" in text
        assert result["interpretation"] in text
    assert "p 值回答「有沒有差異」" in text


def test_section_shows_why_a_value_could_not_be_computed():
    text = format_effect_size_section([("退化案例", cohens_d([0.7] * 4, [0.5] * 4))])
    assert "無法計算的原因" in text
    assert "pooled standard deviation 是 0" in text
