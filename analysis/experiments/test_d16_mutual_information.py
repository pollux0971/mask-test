"""測試 `d16_mutual_information.py`。

分三類：
    1. 校準與人工構造數字：`mutual_info_classif` 的 nats->bit 轉換係數、
       `dual_matrix_gain()`/`tof_vs_mel_ratio()` 的已知數值案例。
    2. XOR 複雜度案例：L、R 各自對標籤幾乎無資訊，但聯合起來完全決定
       標籤——這是資訊論教科書等級的「複雜度」範例，直接驗證本模組真的
       量得到複雜度，不是公式對但量不到現象。
    3. 一個真的走 D01->D02->D03 的整合測試，跟 D13/D18 用同一套合成資料
       手法（normal/silent 模式），驗證 Mel 的 MI 在 silent 模式崩潰、
       ToF vs Mel 比值上升。
"""
import numpy as np
import pytest

from analysis.experiments.d16_mutual_information import (
    MI_PASS_THRESHOLD_BITS,
    dual_matrix_gain,
    format_report,
    modality_mutual_information,
    mutual_information_table,
    tof_vs_mel_ratio,
)


# ---------------------------------------------------------------------------
# nats -> bit 校準
# ---------------------------------------------------------------------------

def test_mutual_info_units_calibrated_against_known_entropy():
    """校準案例：幾乎無雜訊、完全決定平衡二元標籤的單一特徵，理論 MI
    應該等於 H(label) = 1 bit（平衡二元類別的熵）。這個測試如果失敗，
    代表 nats->bit 的轉換係數（除以 ln(2)）用錯了，所有分數都會系統性
    偏差 ~1.44 倍。"""
    rng = np.random.default_rng(0)
    n = 400
    labels = rng.integers(0, 2, size=n)
    feats = []
    for lbl in labels:
        fs = np.zeros((2, 104))
        fs[:, 0:32] = float(lbl) + rng.normal(0, 0.01, size=32)  # 幾乎決定性
        feats.append(fs)

    result = modality_mutual_information(feats, labels.tolist(), "tof_l",
                                          n_pca_components=1, random_state=0)

    assert result["mi_bits"] == pytest.approx(1.0, abs=0.05)


# ---------------------------------------------------------------------------
# 基本行為 / shape / 錯誤處理
# ---------------------------------------------------------------------------

def test_modality_mutual_information_rejects_label_count_mismatch():
    feats = [np.zeros((3, 104)) for _ in range(6)]
    labels = [0, 1] * 2
    with pytest.raises(ValueError):
        modality_mutual_information(feats, labels, "all")


def test_modality_mutual_information_rejects_single_class():
    feats = [np.zeros((3, 104)) for _ in range(6)]
    labels = [0] * 6
    with pytest.raises(ValueError):
        modality_mutual_information(feats, labels, "all")


def test_pca_components_capped_by_sample_count_and_recorded():
    rng = np.random.default_rng(1)
    feats = [rng.normal(size=(3, 104)) for _ in range(6)]
    labels = [0, 0, 0, 1, 1, 1]

    result = modality_mutual_information(feats, labels, "all", n_pca_components=25)

    # n_components <= min(25, n_samples-1=5, n_features=104) = 5
    assert result["n_components"] == 5
    assert result["n_components"] < 25


def test_reproducible_with_fixed_random_state():
    rng = np.random.default_rng(2)
    feats = [rng.normal(size=(4, 104)) for _ in range(30)]
    labels = [i % 3 for i in range(30)]

    r1 = modality_mutual_information(feats, labels, "all", n_pca_components=10, random_state=7)
    r2 = modality_mutual_information(feats, labels, "all", n_pca_components=10, random_state=7)

    assert r1["mi_bits"] == r2["mi_bits"]
    np.testing.assert_array_equal(r1["per_component_mi_bits"], r2["per_component_mi_bits"])


# ---------------------------------------------------------------------------
# dual_matrix_gain / tof_vs_mel_ratio：已知數值
# ---------------------------------------------------------------------------

def _table_with_mi(**mi_bits):
    return {m: {"modality": m, "mi_bits": mi_bits.get(m, 0.0), "per_component_mi_bits": np.array([]),
                "n_components": 1, "n_samples": 1, "passed": mi_bits.get(m, 0.0) > MI_PASS_THRESHOLD_BITS}
            for m in ("tof_l", "tof_r", "tof_combined", "mel", "all")}


def test_dual_matrix_gain_known_value():
    table = _table_with_mi(tof_l=0.4, tof_r=0.5, tof_combined=0.8)
    gain = dual_matrix_gain(table)
    assert gain["gain"] == pytest.approx(0.8 - 0.5)


def test_dual_matrix_gain_negative_when_combined_worse_than_best_single():
    """加總法是近似值，允許出現負增益（見模組 docstring）——這裡只驗證
    公式本身算對，不是「gain 一定要是正的」。"""
    table = _table_with_mi(tof_l=0.9, tof_r=0.6, tof_combined=0.85)
    gain = dual_matrix_gain(table)
    assert gain["gain"] == pytest.approx(0.85 - 0.9)
    assert gain["gain"] < 0


def test_tof_vs_mel_ratio_known_value():
    table = _table_with_mi(tof_combined=0.6, mel=0.3)
    assert tof_vs_mel_ratio(table) == pytest.approx(2.0)


def test_tof_vs_mel_ratio_large_when_mel_near_zero():
    """story 情境：Mel 幾乎無資訊時（例如 silent 模式），比值應該是一個
    很大的數字，而不是 inf 或 NaN（除以 0 的保護）。"""
    table = _table_with_mi(tof_combined=0.6, mel=0.0)
    ratio = tof_vs_mel_ratio(table)
    assert np.isfinite(ratio)
    assert ratio > 100


# ---------------------------------------------------------------------------
# XOR 案例：複雜度的教科書範例——L、R 各自無資訊，聯合起來完全決定標籤
# ---------------------------------------------------------------------------

def test_xor_case_shows_positive_complementarity_gain():
    """驗收條件核心：雙矩陣資訊增益要能量到真的複雜度，不只是公式對。
    b1 塞進 tof_l、b2 塞進 tof_r，label = b1 XOR b2——單獨一邊幾乎猜不到
    label，兩邊一起看才能完全決定，是資訊論教科書等級的複雜度範例。"""
    rng = np.random.default_rng(0)
    n = 200
    b1 = rng.integers(0, 2, size=n)
    b2 = rng.integers(0, 2, size=n)
    label = b1 ^ b2

    feats = []
    for i in range(n):
        fs = rng.normal(0, 0.3, size=(4, 104))
        fs[:, 0:32] += (2 * b1[i] - 1) * 3.0
        fs[:, 32:64] += (2 * b2[i] - 1) * 3.0
        feats.append(fs)

    table = mutual_information_table(feats, label.tolist(), n_pca_components=10, random_state=0)
    gain = dual_matrix_gain(table)

    assert table["tof_l"]["mi_bits"] < 0.5   # 單獨幾乎無資訊
    assert table["tof_r"]["mi_bits"] < 0.5
    assert table["tof_combined"]["mi_bits"] > 1.5   # 合起來接近 H(label)=1 bit 甚至更高
    assert gain["gain"] > 1.0   # 明確的正增益


# ---------------------------------------------------------------------------
# 整合測試：真的走 D01 -> D02 -> D03 -> D16
# ---------------------------------------------------------------------------

N_WORDS = 4
N_REPEATS = 10
T_RAW = 15


def _make_trials(mode):
    """跟 D13/D18 同一套合成資料手法。"""
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


def test_mel_mi_collapses_and_ratio_rises_toward_silent():
    """驗收條件的實際意義：silent 模式下 Mel 的資訊貢獻應明顯低於 normal
    模式，ToF vs Mel 比值應明顯上升——跟 D13 的 Silhouette 趨勢一致，
    兩個獨立指標互相印證同一個現象。"""
    feats_normal, labels_normal = _make_trials("normal")
    feats_silent, labels_silent = _make_trials("silent")

    table_normal = mutual_information_table(feats_normal, labels_normal,
                                              n_pca_components=25, random_state=0)
    table_silent = mutual_information_table(feats_silent, labels_silent,
                                              n_pca_components=25, random_state=0)

    assert table_silent["mel"]["mi_bits"] < table_normal["mel"]["mi_bits"]
    assert tof_vs_mel_ratio(table_silent) > tof_vs_mel_ratio(table_normal)
    # ToF 不靠聲音，兩模式下都應該通過門檻
    assert table_normal["tof_combined"]["passed"] is True
    assert table_silent["tof_combined"]["passed"] is True


def test_format_report_warns_on_negative_gain():
    table = _table_with_mi(tof_l=0.9, tof_r=0.6, tof_combined=0.85, mel=0.4, all=0.9)
    text = format_report(table)
    assert "負增益不能直接讀成" in text


def test_format_report_no_warning_on_positive_gain():
    table = _table_with_mi(tof_l=0.4, tof_r=0.5, tof_combined=0.8, mel=0.4, all=0.9)
    text = format_report(table)
    assert "負增益不能直接讀成" not in text


def test_format_report_contains_key_sections():
    feats, labels = _make_trials("normal")
    table = mutual_information_table(feats, labels, n_pca_components=25, random_state=0)

    text = format_report(table)

    assert "假資料" in text
    assert "雙矩陣資訊增益" in text
    assert "ToF vs Mel 比值" in text
    for modality in ("tof_l", "tof_r", "tof_combined", "mel", "all"):
        assert modality in text
