"""測試 `exp_c_silhouette.py`。

分兩類：
    1. 直接用人工構造的數字測 `exp_c_silhouette` 自己的邏輯
       （門檻判定、shape 檢查、互補性公式、PCA 維度上限）。
    2. 一個完整管線的整合測試：真的呼叫 D01（`tof_features`）、
       D02（`mel_features`）、D03（`assemble_feature_seq`）產生
       `FeatureSeq`，驗證「silent 模式下 Mel 幾乎無資訊、ToF 不受影響」
       這個 D13 存在的理由本身是可測的（用假資料模擬這個現象，不是
       宣稱這就是真實結果——真實結果待 E05）。
"""
import numpy as np
import pytest

from analysis.experiments.exp_c_silhouette import (
    MODALITIES,
    complementarity_check,
    format_report,
    silhouette_for_modality,
    silhouette_report,
    silhouette_table,
    stack_modality,
    tof_vs_mel_gap,
    verdict_for_score,
)


# ---------------------------------------------------------------------------
# verdict_for_score
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "score, expected",
    [
        (0.9, "strong"),
        (0.5, "strong"),          # 邊界：等於門檻算通過上一級
        (0.499, "standard_pass"),
        (0.3, "standard_pass"),
        (0.299, "marginal"),
        (0.15, "marginal"),
        (0.149, "fail"),
        (0.0, "fail"),
        (-0.5, "fail"),
    ],
)
def test_verdict_for_score_boundaries(score, expected):
    """驗收條件：判定門檻 >0.5 強分離 / 0.3-0.5 標準通過 / 0.15-0.3 邊緣可分 / <0.15 失敗。"""
    assert verdict_for_score(score) == expected


# ---------------------------------------------------------------------------
# stack_modality
# ---------------------------------------------------------------------------

def test_stack_modality_selects_correct_columns():
    """驗收條件基礎：六種組合的欄位切法要對。"""
    fs1 = np.arange(3 * 104, dtype=np.float64).reshape(3, 104)
    fs2 = fs1 + 1000.0

    stacked = stack_modality([fs1, fs2], "tof_l")
    assert stacked.shape == (2, 3 * 32)
    np.testing.assert_array_equal(stacked[0], fs1[:, 0:32].reshape(-1))
    np.testing.assert_array_equal(stacked[1], fs2[:, 0:32].reshape(-1))

    stacked_mel = stack_modality([fs1, fs2], "mel")
    assert stacked_mel.shape == (2, 3 * 40)
    np.testing.assert_array_equal(stacked_mel[0], fs1[:, 64:104].reshape(-1))

    stacked_all = stack_modality([fs1, fs2], "all")
    assert stacked_all.shape == (2, 3 * 104)


def test_stack_modality_rejects_unknown_modality():
    with pytest.raises(ValueError):
        stack_modality([np.zeros((3, 104))], "tof_center")


def test_stack_modality_rejects_wrong_feature_dim():
    with pytest.raises(ValueError):
        stack_modality([np.zeros((3, 100))], "all")


def test_stack_modality_rejects_inconsistent_t():
    with pytest.raises(ValueError):
        stack_modality([np.zeros((3, 104)), np.zeros((4, 104))], "all")


# ---------------------------------------------------------------------------
# silhouette_for_modality: 人工構造的乾淨案例
# ---------------------------------------------------------------------------

def _two_tight_clusters(n_per_class=10, t_fixed=4, seed=0):
    """兩個在 tof_l 欄位上完全分開、其他欄位純噪音的 class，用來驗證
    silhouette 分數在「明顯可分」情境下真的很高（而不是隨便一個數字）。"""
    rng = np.random.default_rng(seed)
    feats, labels = [], []
    for cls, center in enumerate([-5.0, 5.0]):
        for _ in range(n_per_class):
            fs = rng.normal(0, 0.1, size=(t_fixed, 104))
            fs[:, 0:32] += center  # tof_l 整段偏移，其餘欄位純噪音
            feats.append(fs)
            labels.append(cls)
    return feats, labels


def test_silhouette_for_modality_high_score_when_classes_separated_on_that_axis():
    feats, labels = _two_tight_clusters()

    result = silhouette_for_modality(feats, labels, "tof_l", n_pca_components=50)
    assert result["score"] > 0.5
    assert verdict_for_score(result["score"]) == "strong"
    assert result["n_samples"] == 20
    assert result["n_classes"] == 2


def test_silhouette_for_modality_low_score_on_pure_noise_modality():
    """同一批資料，換一個跟 class 無關的欄位（mel）看分數——應該遠低於
    tof_l 那個真的有訊號的欄位。"""
    feats, labels = _two_tight_clusters()

    result_signal = silhouette_for_modality(feats, labels, "tof_l", n_pca_components=50)
    result_noise = silhouette_for_modality(feats, labels, "mel", n_pca_components=50)

    assert result_noise["score"] < result_signal["score"]
    assert result_noise["score"] < 0.15


def test_silhouette_for_modality_rejects_too_few_samples():
    feats, labels = _two_tight_clusters(n_per_class=1)
    with pytest.raises(ValueError):
        silhouette_for_modality(feats, labels, "all")


def test_silhouette_for_modality_rejects_single_class():
    feats, labels = _two_tight_clusters()
    labels = [0] * len(labels)
    with pytest.raises(ValueError):
        silhouette_for_modality(feats, labels, "all")


def test_silhouette_for_modality_rejects_label_count_mismatch():
    feats, labels = _two_tight_clusters()
    with pytest.raises(ValueError):
        silhouette_for_modality(feats, labels[:-1], "all")


def test_pca_components_capped_by_sample_count_and_recorded():
    """驗收條件關鍵：PCA 實際維度要記錄，因為資料量不足 50 維可用時
    （這裡故意只給 6 筆樣本）必須被夾住，不能默默失敗或用錯維度。"""
    feats, labels = _two_tight_clusters(n_per_class=3)  # 6 筆樣本
    result = silhouette_for_modality(feats, labels, "all", n_pca_components=50)
    # n_components <= min(50, n_samples-1=5, n_features=104) = 5
    assert result["n_components"] == 5
    assert result["n_components"] < 50


# ---------------------------------------------------------------------------
# complementarity_check / tof_vs_mel_gap：人工構造的已知數值
# ---------------------------------------------------------------------------

def _table_with_scores(**scores):
    """建一個假的 silhouette_table() 結果，只填測試需要的分數欄位。"""
    return {m: {"score": scores.get(m, 0.0), "n_components": 1, "n_samples": 1, "n_classes": 1}
            for m in MODALITIES}


def test_complementarity_check_passes_when_combined_clears_margin():
    table = _table_with_scores(tof_l=0.3, tof_r=0.35, tof_combined=0.45)
    result = complementarity_check(table, margin=0.05)
    # max(0.3, 0.35) + 0.05 = 0.40 < 0.45 -> 通過
    assert result["passed"] is True
    assert result["s_l"] == 0.3
    assert result["s_r"] == 0.35
    assert result["s_combined"] == 0.45


def test_complementarity_check_fails_exactly_at_margin():
    """嚴格大於：剛好等於 max+margin 不算通過。"""
    table = _table_with_scores(tof_l=0.3, tof_r=0.2, tof_combined=0.35)
    result = complementarity_check(table, margin=0.05)
    assert result["passed"] is False


def test_complementarity_check_fails_when_combined_no_better_than_best_single():
    table = _table_with_scores(tof_l=0.7, tof_r=0.75, tof_combined=0.76)
    result = complementarity_check(table, margin=0.05)
    assert result["passed"] is False


def test_tof_vs_mel_gap_known_value():
    table = _table_with_scores(tof_combined=0.6, mel=0.2)
    assert tof_vs_mel_gap(table) == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# format_report(): 維度詛咒提示
# ---------------------------------------------------------------------------

def _fake_report(mode_score_kwargs, is_synthetic=True):
    """用 `_table_with_scores()` 手動組一份 `format_report()` 需要的最小
    report dict，不用真的跑資料——只測 format_report 自己的邏輯。"""
    modes = {}
    for mode, scores in mode_score_kwargs.items():
        table = _table_with_scores(**scores)
        modes[mode] = {
            "table": table,
            "complementarity": complementarity_check(table),
            "tof_vs_mel_gap": tof_vs_mel_gap(table),
        }
    verdicts = {
        mode: {m: verdict_for_score(r["score"]) for m, r in data["table"].items()}
        for mode, data in modes.items()
    }
    return {"is_synthetic": is_synthetic, "modes": modes, "verdicts": verdicts}


def test_format_report_low_score_hint_appears_when_scores_mostly_low():
    low = dict(tof_l=0.1, tof_r=0.1, tof_combined=0.12, mel=0.05, all=0.08)
    report = _fake_report({"normal": low, "whisper": low})

    text = format_report(report)

    assert "維度詛咒" in text
    assert "reports/D13_silhouette_notes.md" in text


def test_format_report_low_score_hint_absent_when_scores_mostly_high():
    high = dict(tof_l=0.7, tof_r=0.7, tof_combined=0.8, mel=0.6, all=0.75)
    report = _fake_report({"normal": high, "whisper": high})

    text = format_report(report)

    assert "維度詛咒" not in text


def test_format_report_low_score_hint_ignores_expected_silent_mel_low_score():
    """(silent, mel) 接近 0 是預期現象，不該單獨觸發維度詛咒提示。"""
    high = dict(tof_l=0.7, tof_r=0.7, tof_combined=0.8, mel=0.6, all=0.75)
    silent = dict(tof_l=0.7, tof_r=0.7, tof_combined=0.8, mel=-0.01, all=0.75)
    report = _fake_report({"normal": high, "whisper": high, "silent": silent})

    text = format_report(report)

    assert "維度詛咒" not in text


# ---------------------------------------------------------------------------
# 整合測試：真的走 D01 -> D02 -> D03 -> D13，不重寫任何特徵計算
# ---------------------------------------------------------------------------

N_WORDS = 4
N_REPEATS = 10
T_RAW = 15


def _make_trials_by_mode():
    """合成資料，模擬「ToF 對唇形有反應（不論有沒有出聲），Mel 只在真的
    出聲時有辨識力」這個要驗證的現象。每個詞給 A/B 兩顆感測器與 Mel
    各自一組固定隨機 pattern（而不是只動一個 zone/一個 band）——單一
    通道的訊號在 T x D 攤平後會被其餘幾百維純噪音稀釋到 PCA 幾乎看不見，
    這不是本模組的邏輯錯誤，只是「多數維度是噪音時歐氏距離不管用」的
    維度詛咒本身，跟 story 提到的一樣。用多通道 pattern 才能讓這個
    整合測試在合理的樣本數下跑出有意義的分數。"""
    from analysis.features.audio_features import mel_features
    from analysis.features.feature_assembly import assemble_feature_seq
    from analysis.features.tof_features import tof_features

    pattern_rng = np.random.default_rng(7)
    tof_patterns_a = pattern_rng.normal(0, 3.0, size=(N_WORDS, 32))
    tof_patterns_b = pattern_rng.normal(0, 3.0, size=(N_WORDS, 32))
    mel_patterns = pattern_rng.normal(0, 3.0, size=(N_WORDS, 40))
    envelope = np.sin(np.linspace(0, np.pi, T_RAW))  # 0 頭尾、1 中間；避免被 CMN 的時間均值抹平

    valid = np.ones((T_RAW, 16), dtype=bool)
    baseline_mu = np.zeros(32)
    baseline_sigma = np.ones(32)

    def make_trial(rng, word_idx, mode):
        tof_a_raw = rng.normal(0, 0.5, size=(T_RAW, 32)) + tof_patterns_a[word_idx]
        tof_b_raw = rng.normal(0, 0.5, size=(T_RAW, 32)) + tof_patterns_b[word_idx]
        tof_a_z = tof_features(tof_a_raw, valid, baseline_mu, baseline_sigma)
        tof_b_z = tof_features(tof_b_raw, valid, baseline_mu, baseline_sigma)

        mode_gain = {"normal": 1.0, "whisper": 0.4, "silent": 0.0}[mode]
        mel_raw = (rng.normal(0, 0.3, size=(T_RAW, 40))
                   + envelope[:, None] * mode_gain * mel_patterns[word_idx])
        mel_cmn = mel_features(mel_raw, vad_start=None, vad_end=None, cvn=False)

        t_us = np.arange(T_RAW) * 1000
        return assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=24).data

    rng = np.random.default_rng(42)
    trials_by_mode = {}
    for mode in ("normal", "whisper", "silent"):
        feats, labels = [], []
        for word_idx in range(N_WORDS):
            for _ in range(N_REPEATS):
                feats.append(make_trial(rng, word_idx, mode))
                labels.append(word_idx)
        trials_by_mode[mode] = (feats, labels)
    return trials_by_mode


@pytest.fixture(scope="module")
def synthetic_report():
    trials_by_mode = _make_trials_by_mode()
    return silhouette_report(trials_by_mode, n_pca_components=50, is_synthetic=True)


def test_report_structure(synthetic_report):
    assert synthetic_report["is_synthetic"] is True
    assert set(synthetic_report["modes"]) == {"normal", "whisper", "silent"}
    for mode_data in synthetic_report["modes"].values():
        assert set(mode_data["table"]) == set(MODALITIES)
        assert "complementarity" in mode_data
        assert "tof_vs_mel_gap" in mode_data


def test_tof_score_stable_across_modes(synthetic_report):
    """驗收條件核心假設：ToF 的可分性不應該因為有沒有出聲而崩潰
    ——這是 D13 存在的理由，也是「無聲介面」是否可行的直接證據。"""
    scores = [synthetic_report["modes"][m]["table"]["tof_combined"]["score"]
              for m in ("normal", "whisper", "silent")]
    for s in scores:
        assert verdict_for_score(s) == "strong"
    # 三個 mode 之間差距應該遠小於 ToF 分數本身的量級
    assert max(scores) - min(scores) < 0.05


def test_mel_score_degrades_as_mode_gets_quieter(synthetic_report):
    """驗收條件：silent 模式的 Mel 分數應明確偏低（幾乎無資訊）。"""
    s_normal = synthetic_report["modes"]["normal"]["table"]["mel"]["score"]
    s_whisper = synthetic_report["modes"]["whisper"]["table"]["mel"]["score"]
    s_silent = synthetic_report["modes"]["silent"]["table"]["mel"]["score"]

    assert s_normal > s_whisper > s_silent
    assert verdict_for_score(s_silent) == "fail"
    assert s_silent < 0.15


def test_tof_vs_mel_gap_widens_toward_silent(synthetic_report):
    gaps = [synthetic_report["modes"][m]["tof_vs_mel_gap"] for m in ("normal", "whisper", "silent")]
    assert gaps[0] < gaps[1] < gaps[2]
    assert gaps[2] > 0  # silent 模式下 ToF 應明顯優於 Mel


def test_format_report_contains_silent_discussion(synthetic_report):
    """驗收條件：silent 模式的 ToF 分數要明確標示並討論——這裡驗證報告
    格式真的把這段話寫出來，不是只有分數表。"""
    text = format_report(synthetic_report)
    assert "silent 模式討論" in text
    assert "假資料" in text  # is_synthetic 標示，避免被誤讀成真實結論
    assert "雙矩陣互補性" in text
    assert "ToF vs Mel 差距" in text


def test_silhouette_table_standalone_call_matches_report_shape():
    """`silhouette_table()` 單獨呼叫（不透過 `silhouette_report()`）也要能
    對同一份資料跑出結構正確、分數落在合法範圍內的結果。"""
    feats, labels = _make_trials_by_mode()["normal"]

    table = silhouette_table(feats, labels, n_pca_components=50)

    assert set(table) == set(MODALITIES)
    for r in table.values():
        assert -1.0 <= r["score"] <= 1.0
