"""測試 `d21_signal_ablation.py`。

分四類：
    1. 資料轉換函式（`remove_signal`/`noise_signal`/`_flatten`）的 shape 與
       數值正確性——人工構造陣列，不需要跑分類器。
    2. `bandwidth_conversion()`：驗證「有 signal」的數字精確重現 CONTRACTS
       §1.4 凍結的 54%/70%（不是「大概對」，是逐位數對）。
    3. `signal_ablation()`：一個真的走 D01->D02->D03 的整合測試，signal
       攜帶一部分 distance 沒有的資訊（模組 docstring 講的「建構出來的
       前提」），驗證消融框架真的量得到差異，且 ToF-only／All 兩組的
       貢獻不同。
    4. `plot_signal_ablation()`/`format_report()`：結構與英文圖表文字。
"""
import matplotlib
matplotlib.use("Agg")

import numpy as np
import pytest

from analysis.experiments.d21_signal_ablation import (
    DIST_A, DIST_B, SIG_A, SIG_B,
    P_VALUE_THRESHOLD,
    bandwidth_conversion,
    estimate_t_line_bytes,
    format_report,
    noise_signal,
    plot_signal_ablation,
    recommend,
    remove_signal,
    signal_ablation,
)


# ---------------------------------------------------------------------------
# remove_signal / noise_signal：純資料轉換
# ---------------------------------------------------------------------------

def test_remove_signal_tof_combined_drops_to_32_dims():
    fs = np.arange(3 * 104, dtype=np.float64).reshape(3, 104)
    out = remove_signal([fs], base="tof_combined")
    assert out[0].shape == (3, 32)
    np.testing.assert_array_equal(out[0][:, 0:16], fs[:, DIST_A])
    np.testing.assert_array_equal(out[0][:, 16:32], fs[:, DIST_B])


def test_remove_signal_all_drops_to_72_dims_and_keeps_mel():
    fs = np.arange(3 * 104, dtype=np.float64).reshape(3, 104)
    out = remove_signal([fs], base="all")
    assert out[0].shape == (3, 72)
    np.testing.assert_array_equal(out[0][:, 32:72], fs[:, 64:104])  # mel 原封不動


def test_remove_signal_rejects_unknown_base():
    fs = np.zeros((3, 104))
    with pytest.raises(ValueError):
        remove_signal([fs], base="mel")


def test_remove_signal_rejects_wrong_feature_dim():
    fs = np.zeros((3, 100))
    with pytest.raises(ValueError):
        remove_signal([fs])


def test_noise_signal_only_touches_signal_columns():
    rng = np.random.default_rng(0)
    fs = rng.normal(size=(4, 104))
    original = fs.copy()

    noised = noise_signal([fs], random_state=0)[0]

    assert noised.shape == (4, 104)
    assert not np.array_equal(noised[:, SIG_A], original[:, SIG_A])
    assert not np.array_equal(noised[:, SIG_B], original[:, SIG_B])
    np.testing.assert_array_equal(noised[:, DIST_A], original[:, DIST_A])
    np.testing.assert_array_equal(noised[:, DIST_B], original[:, DIST_B])
    np.testing.assert_array_equal(noised[:, 64:104], original[:, 64:104])  # mel 不變


def test_noise_signal_reproducible():
    rng = np.random.default_rng(0)
    fs = rng.normal(size=(4, 104))
    n1 = noise_signal([fs], random_state=7)[0]
    n2 = noise_signal([fs], random_state=7)[0]
    np.testing.assert_array_equal(n1, n2)


# ---------------------------------------------------------------------------
# bandwidth_conversion：跟 CONTRACTS §1.4 凍結值逐位數對齊
# ---------------------------------------------------------------------------

def test_bandwidth_with_signal_matches_frozen_contracts_percentages():
    """「有 signal」直接沿用 CONTRACTS 凍結值，不是估出來的，必須精確等於
    文件寫的 54%／70%（四捨五入到整數百分比）。"""
    bw = bandwidth_conversion()
    assert round(bw["4x4@30Hz"]["usage_with_signal"] * 100) == 54
    assert round(bw["8x8@10Hz"]["usage_with_signal"] * 100) == 70


def test_bandwidth_without_signal_is_lower_but_not_negative():
    bw = bandwidth_conversion()
    for cfg in bw.values():
        assert 0 < cfg["usage_without_signal"] < cfg["usage_with_signal"]
        assert 0 < cfg["line_byte_ratio_without_over_with"] < 1


def test_estimate_t_line_bytes_without_signal_is_shorter():
    for dim in (16, 64):
        with_sig = estimate_t_line_bytes(dim, with_signal=True)
        without_sig = estimate_t_line_bytes(dim, with_signal=False)
        assert without_sig < with_sig


def test_estimate_t_line_bytes_reproducible():
    a = estimate_t_line_bytes(16, with_signal=True, seed=3)
    b = estimate_t_line_bytes(16, with_signal=True, seed=3)
    assert a == b


# ---------------------------------------------------------------------------
# recommend()：人工構造的已知案例
# ---------------------------------------------------------------------------

def _fake_result(with_score, removed_score, noised_score, pvalue=0.001):
    def r(score):
        return {"score": score, "pvalue": pvalue, "n_permutations": 10, "cv": 5,
                "random_state": 0, "passed": pvalue < P_VALUE_THRESHOLD}
    return {
        "base": "x",
        "with_signal": r(with_score),
        "signal_removed": r(removed_score),
        "signal_noised": r(noised_score),
        "removed_gain": with_score - removed_score,
        "noised_gain": with_score - noised_score,
    }


def test_recommend_keeps_signal_when_gain_is_large():
    tof_result = _fake_result(0.5, 0.48, 0.47)   # 小差距
    all_result = _fake_result(0.85, 0.60, 0.55)  # 大差距 (>5pp)
    bw = bandwidth_conversion()

    rec = recommend(tof_result, all_result, bw)

    assert rec["keep_signal"] is True
    assert rec["all_matters"] is True
    assert rec["tof_only_matters"] is False


def test_recommend_drops_signal_when_gain_is_small_everywhere():
    tof_result = _fake_result(0.5, 0.49, 0.485)
    all_result = _fake_result(0.85, 0.84, 0.835)
    bw = bandwidth_conversion()

    rec = recommend(tof_result, all_result, bw)

    assert rec["keep_signal"] is False
    assert rec["tof_only_matters"] is False
    assert rec["all_matters"] is False


# ---------------------------------------------------------------------------
# format_report / plot_signal_ablation
# ---------------------------------------------------------------------------

def test_format_report_contains_key_sections_and_synthetic_warning():
    tof_result = _fake_result(0.7, 0.68, 0.65)
    all_result = _fake_result(0.85, 0.6, 0.55)
    bw = bandwidth_conversion()
    rec = recommend(tof_result, all_result, bw)

    text = format_report(tof_result, all_result, bw, rec)

    assert "合成資料" in text  # D21 沿用 D10 的措辭（見 format_report docstring）
    assert "頻寬換算" in text
    assert "建議與代價" in text
    assert "ToF-only" in text


def test_plot_signal_ablation_has_three_bars_and_english_text():
    result = _fake_result(0.8, 0.6, 0.55)
    fig = plot_signal_ablation(result)

    ax = fig.axes[0]
    assert len(ax.patches) == 3  # 三根長條

    def has_cjk(s):
        return any("一" <= ch <= "鿿" for ch in s)

    texts = [ax.get_title(), ax.get_ylabel()] + [t.get_text() for t in ax.get_xticklabels()]
    for t in texts:
        assert not has_cjk(t), f"圖表文字含 CJK 字元: {t!r}"


# ---------------------------------------------------------------------------
# 整合測試：真的走 D01 -> D02 -> D03 -> D21
# ---------------------------------------------------------------------------

N_WORDS = 4
N_REPEATS = 10
T_RAW = 15


def _make_trials():
    """distance 與 signal 各自帶**獨立**的詞彙 pattern（不是同一份訊號
    重複兩次）——這是模組 docstring 講的「建構出來的前提」：確保 signal
    真的攜帶 distance 沒有的資訊，用來驗證消融框架量得到差異，不是宣稱
    真實資料裡 signal 一定有用。振幅取 D19 已經驗證過、不會踩天花板效應
    的量級（見 D19 的維度詛咒/天花板效應筆記）。"""
    from analysis.features.audio_features import mel_features
    from analysis.features.feature_assembly import assemble_feature_seq
    from analysis.features.tof_features import tof_features

    pattern_rng = np.random.default_rng(13)
    dist_a_pat = pattern_rng.normal(0, 0.15, size=(N_WORDS, 16))
    sig_a_pat = pattern_rng.normal(0, 0.13, size=(N_WORDS, 16))
    dist_b_pat = pattern_rng.normal(0, 0.15, size=(N_WORDS, 16))
    sig_b_pat = pattern_rng.normal(0, 0.13, size=(N_WORDS, 16))
    mel_pat = pattern_rng.normal(0, 0.13, size=(N_WORDS, 40))
    envelope = np.sin(np.linspace(0, np.pi, T_RAW))

    valid = np.ones((T_RAW, 16), dtype=bool)
    baseline_mu = np.zeros(32)
    baseline_sigma = np.ones(32)
    rng = np.random.default_rng(42)

    feats, labels = [], []
    for word_idx in range(N_WORDS):
        for _ in range(N_REPEATS):
            tof_a_raw = rng.normal(0, 1.0, size=(T_RAW, 32))
            tof_a_raw[:, 0:16] += dist_a_pat[word_idx]
            tof_a_raw[:, 16:32] += sig_a_pat[word_idx]
            tof_b_raw = rng.normal(0, 1.0, size=(T_RAW, 32))
            tof_b_raw[:, 0:16] += dist_b_pat[word_idx]
            tof_b_raw[:, 16:32] += sig_b_pat[word_idx]

            tof_a_z = tof_features(tof_a_raw, valid, baseline_mu, baseline_sigma)
            tof_b_z = tof_features(tof_b_raw, valid, baseline_mu, baseline_sigma)

            mel_raw = rng.normal(0, 1.0, size=(T_RAW, 40)) + envelope[:, None] * mel_pat[word_idx]
            mel_cmn = mel_features(mel_raw, vad_start=None, vad_end=None, cvn=False)

            t_us = np.arange(T_RAW) * 1000
            data = assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=24).data
            feats.append(data)
            labels.append(word_idx)
    return feats, labels


def test_signal_ablation_detects_contribution_and_differs_by_modality():
    """核心驗收：三組準確率與 p 值都拿得到，而且 ToF-only 與 All
    （融合）的 signal 貢獻不同——這正是 story 要求分開報告的理由。"""
    feats, labels = _make_trials()

    r_tof = signal_ablation(feats, labels, base="tof_combined",
                             n_permutations=100, cv=5, random_state=0)
    r_all = signal_ablation(feats, labels, base="all",
                             n_permutations=100, cv=5, random_state=0)

    for result in (r_tof, r_all):
        for key in ("with_signal", "signal_removed", "signal_noised"):
            assert 0.0 <= result[key]["pvalue"] <= 1.0

    # signal 真的有貢獻（合成資料刻意設計成這樣）：移除/換雜訊都掉分
    assert r_tof["removed_gain"] > 0
    assert r_tof["noised_gain"] > 0
    assert r_all["removed_gain"] > 0
    assert r_all["noised_gain"] > 0

    # 分模態討論的意義所在：融合後 signal 的貢獻應該跟 ToF-only 不同
    # （不強制哪邊比較大，只要求「不是巧合般的完全相等」）
    assert r_tof["removed_gain"] != pytest.approx(r_all["removed_gain"], abs=1e-9)


def test_signal_ablation_report_end_to_end():
    feats, labels = _make_trials()
    r_tof = signal_ablation(feats, labels, base="tof_combined",
                             n_permutations=30, cv=5, random_state=0)
    r_all = signal_ablation(feats, labels, base="all",
                             n_permutations=30, cv=5, random_state=0)
    bw = bandwidth_conversion()
    rec = recommend(r_tof, r_all, bw)

    text = format_report(r_tof, r_all, bw, rec)

    assert "D21" in text
    assert len(text) > 200
