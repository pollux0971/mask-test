"""測試 `d19_ablation_suite.py`。

跟其他 D 軌測試一樣分兩類，但這裡多一層：五項消融各自需要**不同的合成
資料設計**才能乾淨地驗證，硬塞進同一份資料很容易撞到天花板效應（訊號
太強，隨便拿掉什麼都還是 100% 準確率，看不出差異）——開發時實測過這件
事，細節見各測試前的說明註解。這不是敷衍，是誠實面對合成資料調參的
真實困難，跟 D13 的維度詛咒筆記是同一類坦白。

分三個資料集，各自demonstrat一種消融的 PASS 情境：
    A. 中等訊號、ToF 主導：驗證「雙矩陣 vs 單顆」「All vs Mel-only」
    B. 早/晚兩段不同方向性樣式：驗證「時間反轉測試」
    C. 訊號較弱且 ToF/Mel 強度相當：驗證「亂數通道測試」
"""
import numpy as np
import pytest

from analysis.experiments.d19_ablation_suite import (
    DEFAULT_RANDOM_STATE,
    RANDOM_CHANNEL_MAX_RELATIVE_DROP,
    RANDOM_CHANNEL_MIN_RELATIVE_DROP,
    TIME_REVERSAL_MAX_ACCURACY,
    format_report,
    reverse_time,
    run_ablation_suite,
    substitute_modality_with_noise,
    time_reversal_ablation,
)
from analysis.experiments.exp_c_silhouette import MODALITIES


# ---------------------------------------------------------------------------
# 純資料轉換函式：reverse_time / substitute_modality_with_noise
# ---------------------------------------------------------------------------

def test_reverse_time_flips_frame_order_not_values():
    fs = np.arange(4 * 104, dtype=np.float64).reshape(4, 104)
    reversed_fs = reverse_time([fs])[0]

    np.testing.assert_array_equal(reversed_fs[0], fs[3])
    np.testing.assert_array_equal(reversed_fs[3], fs[0])
    assert set(reversed_fs.flatten()) == set(fs.flatten())  # 值集合不變，順序變


def test_reverse_time_does_not_mutate_input():
    fs = np.arange(3 * 104, dtype=np.float64).reshape(3, 104)
    original = fs.copy()
    reverse_time([fs])
    np.testing.assert_array_equal(fs, original)


def test_substitute_modality_with_noise_only_touches_target_columns():
    rng = np.random.default_rng(0)
    fs = rng.normal(size=(4, 104))
    original = fs.copy()

    noised = substitute_modality_with_noise([fs], "mel", random_state=0)[0]

    mel_sl = MODALITIES["mel"]
    tof_sl = MODALITIES["tof_combined"]
    assert not np.array_equal(noised[:, mel_sl], original[:, mel_sl])
    np.testing.assert_array_equal(noised[:, tof_sl], original[:, tof_sl])


def test_substitute_modality_with_noise_reproducible():
    rng = np.random.default_rng(0)
    fs = rng.normal(size=(4, 104))
    n1 = substitute_modality_with_noise([fs], "mel", random_state=7)[0]
    n2 = substitute_modality_with_noise([fs], "mel", random_state=7)[0]
    np.testing.assert_array_equal(n1, n2)


# ---------------------------------------------------------------------------
# 時間反轉測試：正確方法學驗證（train=forward, test=forward vs reversed）
# ---------------------------------------------------------------------------

def _asymmetric_time_dataset(n_per_class=10, seed=0, n_classes=4):
    """早半段一個方向、晚半段另一個方向的樣式——反轉後「早/晚」對調，
    對一個用位置資訊分類的模型來說是真的會混淆的操作。4 類（不是 3 類）
    是刻意選的：3 類的隨機猜測基準線是 33.3%，會直接超過本模組
    `TIME_REVERSAL_MAX_ACCURACY=0.30` 這個門檻本身，不管反轉測試邏輯對
    不對都會誤判——4 類的隨機基準線 25% 才在門檻之下，测試才有意義。"""
    rng = np.random.default_rng(seed)
    t = 8
    half = t // 2
    feats, labels = [], []
    for cls in range(n_classes):
        early = rng.normal(0, 3.0, size=104)
        late = rng.normal(0, 3.0, size=104)
        for _ in range(n_per_class):
            fs = rng.normal(0, 0.2, size=(t, 104))
            fs[:half] += early
            fs[half:] += late
            feats.append(fs)
            labels.append(cls)
    return feats, labels


def _symmetric_time_dataset(n_per_class=10, seed=0):
    """整段序列本身是回文（palindrome）——反轉後跟原本一模一樣，這是
    「時間反轉測試不該誤判」的負對照：不管模型好不好，反轉根本沒有
    改變輸入，reversed_accuracy 理論上必須等於 forward_accuracy。"""
    rng = np.random.default_rng(seed)
    t = 8
    feats, labels = [], []
    for cls in range(3):
        pattern = rng.normal(0, 1.0, size=104) * (cls + 1)
        for _ in range(n_per_class):
            half_noise = rng.normal(0, 0.05, size=(t // 2, 104))
            noise = np.concatenate([half_noise, half_noise[::-1]], axis=0)  # 回文噪音
            fs = pattern[None, :] + noise  # 訊號本身沒有時間變化，天生回文
            feats.append(fs)
            labels.append(cls)
    return feats, labels


def test_time_reversal_ablation_passes_on_genuinely_asymmetric_signal():
    feats, labels = _asymmetric_time_dataset()

    result = time_reversal_ablation(feats, labels, cv=5, random_state=0)

    assert result["forward_accuracy"] > 0.8
    assert result["reversed_accuracy"] < TIME_REVERSAL_MAX_ACCURACY
    assert result["passed"] is True


def test_time_reversal_ablation_does_not_falsely_flag_symmetric_signal():
    """負對照：訊號本身回文（反轉後跟原本相同）時，反轉準確率不該掉——
    如果這裡也判定 FAIL，代表測試邏輯本身有問題，不是資料的問題。"""
    feats, labels = _symmetric_time_dataset()

    result = time_reversal_ablation(feats, labels, cv=5, random_state=0)

    assert result["reversed_accuracy"] == pytest.approx(result["forward_accuracy"], abs=0.05)


def test_time_reversal_ablation_rejects_label_count_mismatch():
    feats = [np.zeros((3, 104)) for _ in range(5)]
    with pytest.raises(ValueError):
        time_reversal_ablation(feats, [0, 1, 0, 1])


def test_time_reversal_cv_clamped_to_smallest_class():
    rng = np.random.default_rng(0)
    feats = [rng.normal(size=(4, 104)) for _ in range(6)]
    labels = [0, 0, 0, 1, 1, 1]
    result = time_reversal_ablation(feats, labels, cv=5, random_state=0)
    assert result["cv"] == 3


# ---------------------------------------------------------------------------
# format_report：結構與標示
# ---------------------------------------------------------------------------

def _minimal_suite_for_report():
    """繞開真的跑分類器，直接手造一份符合 `run_ablation_suite()` 回傳
    形狀的最小資料，只測 `format_report()` 自己的排版邏輯。"""
    def r(score):
        return {"score": score, "pvalue": 0.001, "n_permutations": 10, "cv": 5,
                "random_state": 0, "passed": True, "modality": "x"}

    return {
        "is_synthetic": True,
        "dual_matrix_vs_single": {
            "tof_l": r(0.6), "tof_r": r(0.65), "tof_combined": r(0.85),
            "gain": 0.2, "passed": True,
        },
        "all_vs_mel_only": {
            "all": r(0.8), "mel_only": r(0.3), "gain": 0.5, "passed": True,
        },
        "all_vs_tof_only": {
            "all": r(0.8), "tof_only": r(0.85), "gain": -0.05, "passed": None,
        },
        "time_reversal": {
            "forward_accuracy": 0.8, "reversed_accuracy": 0.1, "cv": 5, "passed": True,
        },
        "random_channel": {
            "noised_modality": "mel", "baseline": r(0.8), "noised": r(0.6),
            "relative_drop": 0.25, "passed": True,
        },
    }


def test_format_report_marks_all_vs_mel_as_core_metric():
    """驗收條件：All vs Mel-only 的增益明確標示。"""
    text = format_report(_minimal_suite_for_report())
    assert "新核心指標" in text
    assert "+0.500" in text or "0.500" in text


def test_format_report_shows_pass_fail_for_all_five():
    text = format_report(_minimal_suite_for_report())
    assert text.count("PASS") + text.count("只記錄") >= 4  # 至少四項有明確 PASS 或記錄字樣


def test_format_report_shows_overreliance_hint_above_70_percent():
    suite = _minimal_suite_for_report()
    suite["random_channel"]["relative_drop"] = 0.8
    suite["random_channel"]["passed"] = False
    text = format_report(suite)
    assert "過度依賴" in text


# ---------------------------------------------------------------------------
# 整合測試：真的走 D01 -> D02 -> D03 -> D19
# ---------------------------------------------------------------------------

N_WORDS = 4
N_REPEATS = 10
T_RAW = 15


def _make_trials(tof_amp, mel_amp, noise, seed_pattern=11, seed_data=42):
    from analysis.features.audio_features import mel_features
    from analysis.features.feature_assembly import assemble_feature_seq
    from analysis.features.tof_features import tof_features

    pattern_rng = np.random.default_rng(seed_pattern)
    tof_a_patterns = pattern_rng.normal(0, tof_amp, size=(N_WORDS, 32))
    tof_b_patterns = pattern_rng.normal(0, tof_amp, size=(N_WORDS, 32))
    mel_patterns = pattern_rng.normal(0, mel_amp, size=(N_WORDS, 40))
    envelope = np.sin(np.linspace(0, np.pi, T_RAW))

    valid = np.ones((T_RAW, 16), dtype=bool)
    baseline_mu = np.zeros(32)
    baseline_sigma = np.ones(32)
    rng = np.random.default_rng(seed_data)

    feats, labels = [], []
    for word_idx in range(N_WORDS):
        for _ in range(N_REPEATS):
            tof_a_raw = rng.normal(0, noise, size=(T_RAW, 32)) + tof_a_patterns[word_idx]
            tof_b_raw = rng.normal(0, noise, size=(T_RAW, 32)) + tof_b_patterns[word_idx]
            tof_a_z = tof_features(tof_a_raw, valid, baseline_mu, baseline_sigma)
            tof_b_z = tof_features(tof_b_raw, valid, baseline_mu, baseline_sigma)

            mel_raw = rng.normal(0, noise, size=(T_RAW, 40)) + envelope[:, None] * mel_patterns[word_idx]
            mel_cmn = mel_features(mel_raw, vad_start=None, vad_end=None, cvn=False)

            t_us = np.arange(T_RAW) * 1000
            data = assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=24).data
            feats.append(data)
            labels.append(word_idx)
    return feats, labels


def test_dual_matrix_and_mel_only_ablations_pass_on_tof_dominant_dataset():
    """資料集 A：中等訊號、ToF 主導——驗證雙矩陣互補性與 All vs Mel-only
    這個新核心指標。"""
    feats, labels = _make_trials(tof_amp=0.15, mel_amp=0.13, noise=1.0)

    suite = run_ablation_suite(feats, labels, n_permutations=50, cv=5,
                                random_state=0, noise_modality="mel")

    assert suite["dual_matrix_vs_single"]["passed"] is True
    assert suite["all_vs_mel_only"]["passed"] is True
    assert suite["all_vs_mel_only"]["gain"] > 0.05
    # all_vs_tof_only 只記錄，不強制 PASS/FAIL
    assert suite["all_vs_tof_only"]["passed"] is None


def test_random_channel_ablation_passes_on_balanced_weak_dataset():
    """資料集 C：訊號較弱、ToF/Mel 強度相當——驗證亂數通道測試落在
    10%-50% 的合理下降窗口（不是掉太少代表沒用到，也不是掉太多代表
    過度依賴單一模態）。"""
    feats, labels = _make_trials(tof_amp=0.10, mel_amp=0.10, noise=1.0)

    suite = run_ablation_suite(feats, labels, n_permutations=50, cv=5,
                                random_state=0, noise_modality="mel")

    rc = suite["random_channel"]
    assert RANDOM_CHANNEL_MIN_RELATIVE_DROP <= rc["relative_drop"] <= RANDOM_CHANNEL_MAX_RELATIVE_DROP
    assert rc["passed"] is True


def test_time_reversal_ablation_passes_through_full_pipeline():
    """時間反轉測試也走一次真正的 D01->D02->D03 管線（不是只測手造陣列），
    確認 `assemble_feature_seq()` 產出的 `FeatureSeq.data` 餵進
    `time_reversal_ablation()` 一樣正確運作。"""
    from analysis.features.audio_features import mel_features
    from analysis.features.feature_assembly import assemble_feature_seq
    from analysis.features.tof_features import tof_features

    pattern_rng = np.random.default_rng(21)
    half = T_RAW // 2
    is_late = (np.arange(T_RAW) >= half).astype(np.float64)[:, None]
    tof_a_early = pattern_rng.normal(0, 6.0, size=(N_WORDS, 32))
    tof_a_late = pattern_rng.normal(0, 6.0, size=(N_WORDS, 32))
    mel_early = pattern_rng.normal(0, 6.0, size=(N_WORDS, 40))
    mel_late = pattern_rng.normal(0, 6.0, size=(N_WORDS, 40))

    valid = np.ones((T_RAW, 16), dtype=bool)
    baseline_mu = np.zeros(32)
    baseline_sigma = np.ones(32)
    rng = np.random.default_rng(42)

    feats, labels = [], []
    for word_idx in range(N_WORDS):
        for _ in range(N_REPEATS):
            tof_a_raw = (rng.normal(0, 0.3, size=(T_RAW, 32))
                         + (1 - is_late) * tof_a_early[word_idx] + is_late * tof_a_late[word_idx])
            tof_b_raw = rng.normal(0, 0.3, size=(T_RAW, 32))  # B 軌沒有訊號，純噪音也無妨
            tof_a_z = tof_features(tof_a_raw, valid, baseline_mu, baseline_sigma)
            tof_b_z = tof_features(tof_b_raw, valid, baseline_mu, baseline_sigma)

            mel_raw = (rng.normal(0, 0.2, size=(T_RAW, 40))
                       + (1 - is_late) * mel_early[word_idx] + is_late * mel_late[word_idx])
            mel_cmn = mel_features(mel_raw, vad_start=None, vad_end=None, cvn=False)

            t_us = np.arange(T_RAW) * 1000
            data = assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=24).data
            feats.append(data)
            labels.append(word_idx)

    result = time_reversal_ablation(feats, labels, cv=5, random_state=0)

    assert result["forward_accuracy"] > 0.8
    assert result["passed"] is True


def test_run_ablation_suite_is_synthetic_flag_propagates():
    feats, labels = _make_trials(tof_amp=0.15, mel_amp=0.13, noise=1.0)
    suite = run_ablation_suite(feats, labels, n_permutations=20, cv=5,
                                random_state=0, is_synthetic=True)
    assert suite["is_synthetic"] is True
    text = format_report(suite)
    assert "假資料" in text


# ---------------------------------------------------------------------------
# groups：六個檢定要嘛全部分組、要嘛全部沒分組，不能各講各的
# ---------------------------------------------------------------------------


def test_groups_reach_all_six_underlying_permutation_tests():
    """`all`/`mel`/`tof_combined`/`tof_l`/`tof_r`/雜訊化後的 `all`——
    六個 run_permutation_test() 呼叫，同一次 run_ablation_suite() 裡
    必須拿到同一個分組狀態，不能有的分組了、有的沒有。"""
    feats, labels = _make_trials(tof_amp=0.15, mel_amp=0.13, noise=1.0)
    # 兩個 wear_id，各佔一半——不必是真實配戴語意，只需要 >= 2 個 group。
    groups = [0] * (len(labels) // 2) + [1] * (len(labels) - len(labels) // 2)

    suite = run_ablation_suite(feats, labels, n_permutations=20, cv=5,
                                random_state=0, groups=groups)

    assert suite["grouping"] == "grouped"
    assert suite["n_groups"] == 2
    underlying = [
        suite["dual_matrix_vs_single"]["tof_l"],
        suite["dual_matrix_vs_single"]["tof_r"],
        suite["dual_matrix_vs_single"]["tof_combined"],
        suite["all_vs_mel_only"]["all"],
        suite["all_vs_mel_only"]["mel_only"],
        suite["random_channel"]["noised"],
    ]
    assert all(r["grouping"] == "grouped" for r in underlying)


def test_groups_default_to_ungrouped_when_not_given():
    feats, labels = _make_trials(tof_amp=0.15, mel_amp=0.13, noise=1.0)
    suite = run_ablation_suite(feats, labels, n_permutations=20, cv=5, random_state=0)
    assert suite["grouping"] == "ungrouped_no_groups_given"


def test_groups_with_a_single_group_is_reported_not_silently_dropped():
    """第一批資料很可能只戴一次——分組驗證要不到，但必須明確說做不到，
    不能安靜退回未分組的舊行為卻看起來若無其事。"""
    feats, labels = _make_trials(tof_amp=0.15, mel_amp=0.13, noise=1.0)
    groups = [0] * len(labels)  # 只有一個 wear_id

    suite = run_ablation_suite(feats, labels, n_permutations=20, cv=5,
                                random_state=0, groups=groups)

    assert suite["grouping"] == "ungrouped_single_group"
    assert suite["grouping_note"] is not None


def test_report_shows_grouping_status_once_not_six_times():
    """六個消融小節共用同一個分組結果——狀態只該在報告裡出現一次，
    六句一模一樣的警語會讓人更容易略過，不是更容易注意到。"""
    suite = _minimal_suite_for_report()
    suite["grouping"] = "ungrouped_single_group"
    suite["n_groups"] = 1
    suite["grouping_note"] = "要求了分組驗證，但資料裡只有 1 個 group"

    text = format_report(suite)
    assert text.count("分組驗證無法進行") == 1


def test_report_warns_time_reversal_is_not_group_aware_when_grouped():
    suite = _minimal_suite_for_report()
    suite["grouping"] = "grouped"
    suite["n_groups"] = 3
    suite["grouping_note"] = None

    text = format_report(suite)
    assert "時間反轉測試" in text.split("## 4.")[0]  # 出現在頂部摘要，不是只在第 4 節標題


def test_report_omits_time_reversal_caveat_when_not_grouped():
    suite = _minimal_suite_for_report()  # 預設沒有 grouping 欄位 -> ungrouped_no_groups_given
    text = format_report(suite)
    summary = text.split("## 1.")[0]
    assert "不吃 `groups`" not in summary
