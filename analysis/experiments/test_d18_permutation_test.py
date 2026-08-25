"""測試 `d18_permutation_test.py`。

跟 `test_exp_c_silhouette.py` 同樣分兩類：人工構造數字測本模組自己的邏輯，
加一個真的走 D01->D02->D03 管線的整合測試。`n_permutations` 在測試裡都用
遠小於 story 要求的 1000（那是給真正分析用的預設值，不是拿來灌爆 CI 的）
——這裡驗證的是「函式邏輯對不對」，不是「1000 次夠不夠精準」。
"""
import matplotlib
matplotlib.use("Agg")  # 無頭環境，不需要真的顯示視窗

import numpy as np
import pytest

from analysis.experiments.d18_permutation_test import (
    DEFAULT_N_PERMUTATIONS,
    P_VALUE_THRESHOLD,
    format_report,
    permutation_report,
    plot_null_distribution,
    run_permutation_test,
)


# ---------------------------------------------------------------------------
# 基本行為 / shape / 錯誤處理
# ---------------------------------------------------------------------------

def test_default_n_permutations_matches_story_spec():
    """驗收條件依據：story 要求 1000 次——這裡只驗證預設值對，不是真的跑 1000 次。"""
    assert DEFAULT_N_PERMUTATIONS == 1000


def test_run_permutation_test_rejects_label_count_mismatch():
    feats = [np.zeros((3, 104)) for _ in range(6)]
    labels = [0, 1] * 2  # 長度 4 != 6
    with pytest.raises(ValueError):
        run_permutation_test(feats, labels, n_permutations=5, cv=2)


def test_run_permutation_test_rejects_single_member_class():
    rng = np.random.default_rng(0)
    feats = [rng.normal(size=(3, 104)) for _ in range(5)]
    labels = [0, 0, 1, 1, 2]  # class 2 只有 1 筆
    with pytest.raises(ValueError):
        run_permutation_test(feats, labels, n_permutations=5, cv=2)


def test_cv_folds_clamped_to_smallest_class_and_recorded():
    """`E05` 小樣本情境：cv 要求 5，但最小類別只有 3 筆，應該被夾到 3。"""
    rng = np.random.default_rng(0)
    feats, labels = [], []
    for cls, n in enumerate([3, 3]):  # 兩個 class，各只有 3 筆
        for _ in range(n):
            feats.append(rng.normal(size=(3, 104)))
            labels.append(cls)

    result = run_permutation_test(feats, labels, "all", n_permutations=5, cv=5)

    assert result["cv"] == 3
    assert result["cv"] < 5


def test_result_shape_and_pvalue_bounds():
    rng = np.random.default_rng(1)
    n_per_class = 8
    feats, labels = [], []
    for cls, center in enumerate([-3.0, 3.0]):
        for _ in range(n_per_class):
            fs = rng.normal(0, 0.3, size=(4, 104))
            fs[:, 0:32] += center
            feats.append(fs)
            labels.append(cls)

    result = run_permutation_test(feats, labels, "tof_l", n_permutations=20, cv=4, random_state=0)

    assert result["permutation_scores"].shape == (20,)
    assert 0.0 <= result["pvalue"] <= 1.0
    assert 0.0 <= result["score"] <= 1.0
    assert result["passed"] == (result["pvalue"] < P_VALUE_THRESHOLD)


# ---------------------------------------------------------------------------
# 隨機種子固定（驗收條件）
# ---------------------------------------------------------------------------

def _separable_dataset(seed=2, n_per_class=8):
    rng = np.random.default_rng(seed)
    feats, labels = [], []
    for cls, center in enumerate([-4.0, 4.0, 0.0]):
        for _ in range(n_per_class):
            fs = rng.normal(0, 0.3, size=(4, 104))
            fs[:, 0:32] += center
            feats.append(fs)
            labels.append(cls)
    return feats, labels


def test_same_random_state_gives_identical_results():
    feats, labels = _separable_dataset()

    r1 = run_permutation_test(feats, labels, "all", n_permutations=15, cv=3, random_state=42)
    r2 = run_permutation_test(feats, labels, "all", n_permutations=15, cv=3, random_state=42)

    assert r1["score"] == r2["score"]
    assert r1["pvalue"] == r2["pvalue"]
    np.testing.assert_array_equal(r1["permutation_scores"], r2["permutation_scores"])


def test_different_random_state_can_give_different_permutation_scores():
    feats, labels = _separable_dataset()

    r1 = run_permutation_test(feats, labels, "all", n_permutations=15, cv=3, random_state=1)
    r2 = run_permutation_test(feats, labels, "all", n_permutations=15, cv=3, random_state=2)

    assert not np.array_equal(r1["permutation_scores"], r2["permutation_scores"])


# ---------------------------------------------------------------------------
# 核心概念驗證：真訊號顯著、純噪音不顯著
# ---------------------------------------------------------------------------

def test_significant_pvalue_when_classes_are_truly_separable():
    """驗收條件核心：真的有可分性時，p 值要夠小（通過 p<0.01 門檻）。"""
    feats, labels = _separable_dataset(n_per_class=10)

    result = run_permutation_test(feats, labels, "all", n_permutations=100, cv=5, random_state=0)

    assert result["passed"] is True
    assert result["pvalue"] < P_VALUE_THRESHOLD


def test_large_pvalue_when_labels_are_unrelated_to_features():
    """負對照：標籤跟特徵完全無關時，不應該宣稱顯著——這是本模組不能
    有偽陽性的底線測試。用「打亂同一組平衡標籤」製造負對照，維持跟正
    對照一樣的類別平衡，才是公平的比較（不是另外抽樣出不平衡的隨機標籤）。"""
    feats, labels = _separable_dataset(n_per_class=10)
    rng = np.random.default_rng(123)
    shuffled_labels = list(labels)
    rng.shuffle(shuffled_labels)
    # 資料本身（feats）維持原樣（其實帶有可分性），但標籤重新洗牌後
    # 跟哪筆資料對應已經沒有關係——這才是「標籤與特徵無關」的正確做法，
    # 而不是換一批全新資料。

    result = run_permutation_test(feats, shuffled_labels, "all", n_permutations=100, cv=5, random_state=0)

    assert result["pvalue"] >= P_VALUE_THRESHOLD
    assert result["passed"] is False


# ---------------------------------------------------------------------------
# permutation_report / format_report
# ---------------------------------------------------------------------------

def test_permutation_report_runs_both_all_and_tof_only():
    feats, labels = _separable_dataset(n_per_class=10)

    report = permutation_report(feats, labels, n_permutations=20, cv=5, random_state=0)

    assert set(report) == {"is_synthetic", "all", "tof_only"}
    assert report["all"]["modality"] == "all"
    assert report["tof_only"]["modality"] == "tof_combined"


def test_format_report_discusses_tof_only_explicitly():
    """驗收條件：ToF-only 的結果明確標示與討論。"""
    feats, labels = _separable_dataset(n_per_class=10)
    report = permutation_report(feats, labels, n_permutations=20, cv=5, random_state=0)

    text = format_report(report)

    assert "ToF-only" in text
    assert "全模態" in text
    assert "假資料" in text  # is_synthetic 標示


def test_plot_null_distribution_draws_true_score_line():
    feats, labels = _separable_dataset(n_per_class=10)
    result = run_permutation_test(feats, labels, "all", n_permutations=20, cv=5, random_state=0)

    fig = plot_null_distribution(result)

    ax = fig.axes[0]
    assert len(ax.patches) > 0                 # 直方圖有畫出長條
    assert len(ax.get_lines()) >= 1             # 真實準確率那條垂直線
    vline = ax.get_lines()[0]
    xs = vline.get_xdata()
    assert xs[0] == pytest.approx(result["score"])


# ---------------------------------------------------------------------------
# 整合測試：真的走 D01 -> D02 -> D03 -> D18，reuse D13 的 stack_modality
# ---------------------------------------------------------------------------

N_WORDS = 4
N_REPEATS = 10
T_RAW = 15


def _make_trials(mode):
    """跟 `test_exp_c_silhouette.py` 用同一套合成資料手法：多個 zone/band
    上一致的固定 pattern，不是單一通道——見 D13 的維度詛咒筆記。"""
    from analysis.features.audio_features import mel_features
    from analysis.features.feature_assembly import assemble_feature_seq
    from analysis.features.tof_features import tof_features

    pattern_rng = np.random.default_rng(7)
    tof_patterns_a = pattern_rng.normal(0, 3.0, size=(N_WORDS, 32))
    tof_patterns_b = pattern_rng.normal(0, 3.0, size=(N_WORDS, 32))
    mel_patterns = pattern_rng.normal(0, 3.0, size=(N_WORDS, 40))
    envelope = np.sin(np.linspace(0, np.pi, T_RAW))

    valid = np.ones((T_RAW, 16), dtype=bool)
    baseline_mu = np.zeros(32)
    baseline_sigma = np.ones(32)

    mode_gain = {"normal": 1.0, "whisper": 0.4, "silent": 0.0}[mode]
    rng = np.random.default_rng(42)

    feats, labels = [], []
    for word_idx in range(N_WORDS):
        for _ in range(N_REPEATS):
            tof_a_raw = rng.normal(0, 0.5, size=(T_RAW, 32)) + tof_patterns_a[word_idx]
            tof_b_raw = rng.normal(0, 0.5, size=(T_RAW, 32)) + tof_patterns_b[word_idx]
            tof_a_z = tof_features(tof_a_raw, valid, baseline_mu, baseline_sigma)
            tof_b_z = tof_features(tof_b_raw, valid, baseline_mu, baseline_sigma)

            mel_raw = (rng.normal(0, 0.3, size=(T_RAW, 40))
                       + envelope[:, None] * mode_gain * mel_patterns[word_idx])
            mel_cmn = mel_features(mel_raw, vad_start=None, vad_end=None, cvn=False)

            t_us = np.arange(T_RAW) * 1000
            data = assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=24).data
            feats.append(data)
            labels.append(word_idx)
    return feats, labels


def test_tof_only_significant_even_in_silent_mode():
    """整合測試核心：這是 D18 存在的理由本身——不出聲時 ToF-only 的
    permutation test 仍然顯著，代表「ToF 帶有詞彙資訊」不需要靠聲音才成立。
    （假資料模擬，不是真實結論——is_synthetic 標示見 format_report。）"""
    feats, labels = _make_trials("silent")

    report = permutation_report(feats, labels, n_permutations=100, cv=5, random_state=0)

    assert report["tof_only"]["passed"] is True
    assert report["tof_only"]["pvalue"] < P_VALUE_THRESHOLD
