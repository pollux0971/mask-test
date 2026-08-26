"""`analysis/experiments/d14_viseme_sensitivity.py`（D14）的測試。

數值類的驗收條件都用具體案例驗證，不寫「應該正確」。合成資料的部分會
明講自己是合成的。
"""
import json

import numpy as np
import pytest

from analysis.experiments.d14_viseme_sensitivity import (
    EXPECTED_PATTERN,
    MODALITIES,
    MODALITY_ORDER,
    STRENGTH_ORDER,
    chance_max,
    compare_lip_lead_versions,
    compare_to_expected,
    format_lip_lead_report,
    format_report,
    fricative_check,
    lip_lead_samples,
    load_viseme_map,
    plot_viseme_sensitivity,
    sensitivity,
    sensitivity_table,
    sensitivity_with_excess,
    strength_for,
    uniform_weak_tof,
    viseme_sensitivity_report,
)
from analysis.reporting.session_loader import Trial

T_FIXED = 24
N_DIMS = 104


def sample(rng, *, tof_gain=1.0, mel_gain=1.0):
    """一筆合成的 `FeatureSeq.data`（(24, 104)，已是 z-score 單位）。"""
    data = rng.normal(0.0, 1.0, (T_FIXED, N_DIMS))
    data[:, 0:64] *= tof_gain
    data[:, 64:104] *= mel_gain
    return data


def build_samples(rng, n_per_word=4):
    """照 `config/vocab.json` 產生一整批：ToF 類的詞 ToF 強、擦音 Mel 強。"""
    word_to_viseme, _ = load_viseme_map()
    samples = []
    for word_id, key in word_to_viseme.items():
        for _ in range(n_per_word):
            if key in ("A", "B", "C", "D"):
                samples.append((word_id, sample(rng, tof_gain=6.0)))
            elif key == "F":
                samples.append((word_id, sample(rng, mel_gain=8.0)))
            else:
                samples.append((word_id, sample(rng)))
    return samples


# --------------------------------------------------- 機率地板（最關鍵的一環）


def test_chance_max_matches_simulation():
    """純雜訊的 `max|z|` 有一個機率地板。近似式必須貼得住模擬值，
    否則整張熱力圖會把雜訊判成「中等敏感」。"""
    rng = np.random.RandomState(0)
    for n in (100, 768, 960):
        simulated = np.abs(rng.normal(0, 1, (3000, n))).max(axis=1).mean()
        approx = chance_max(n)
        assert approx == pytest.approx(simulated, abs=0.12)
        # 近似值要略高（保守方向：地板估高 → excess 估低）
        assert approx >= simulated


def test_chance_max_grows_with_channel_count():
    """通道多的模態地板較高，所以必須逐模態算，不能共用一個常數。"""
    assert chance_max(768) < chance_max(960) < chance_max(5000)
    assert chance_max(1) == 0.0


def test_pure_noise_is_classified_weak_not_medium():
    """**這是這支模組最容易錯的地方。** 24×32=768 個純雜訊值的 max|z|
    期望就有 3.4——對原始值設 3.0 的門檻會把「什麼都沒發生」判成 medium。"""
    rng = np.random.RandomState(1)
    samples = [("ba", sample(rng)) for _ in range(8)]
    report = viseme_sensitivity_report(samples)
    cell = report["table"]["A"]["tof_l"]
    assert cell["mean"] > 3.0, "前提：原始 max|z| 確實高於 3.0"
    assert cell["strength"] == "weak", "扣掉機率地板之後應該是 weak"
    assert abs(cell["excess_mean"]) < 1.0


def test_sensitivity_with_excess_reports_both():
    rng = np.random.RandomState(2)
    data = sample(rng, tof_gain=6.0)
    raw, excess = sensitivity_with_excess(data, "tof_l")
    assert raw == sensitivity(data, "tof_l")
    assert excess == pytest.approx(raw - chance_max(T_FIXED * 32))
    assert excess < raw


# ------------------------------------------------------------ 敏感度定義


def test_sensitivity_is_max_abs_over_the_modality_slice():
    data = np.zeros((T_FIXED, N_DIMS))
    data[3, 5] = -7.5                 # tof_l
    data[9, 40] = 4.0                 # tof_r
    data[1, 70] = 2.0                 # mel
    assert sensitivity(data, "tof_l") == pytest.approx(7.5)
    assert sensitivity(data, "tof_r") == pytest.approx(4.0)
    assert sensitivity(data, "mel") == pytest.approx(2.0)


def test_modality_slices_come_from_d13():
    """兩支腳本必須指向同一組通道，否則兩張圖的結論不能互相對照。"""
    from analysis.experiments.exp_c_silhouette import MODALITIES as D13
    for name in MODALITY_ORDER:
        assert MODALITIES[name] == D13[name]
    assert MODALITIES["tof_l"] == slice(0, 32)
    assert MODALITIES["mel"] == slice(64, 104)


def test_all_nan_modality_is_nan_not_zero():
    """0 會被讀成「完全沒動」——那是關於嘴唇的結論，但實際發生的事是
    關於感測器的。"""
    data = np.zeros((T_FIXED, N_DIMS))
    data[:, 0:32] = np.nan
    assert np.isnan(sensitivity(data, "tof_l"))
    assert sensitivity(data, "mel") == pytest.approx(0.0)


def test_partial_nan_is_ignored_not_fatal():
    data = np.zeros((T_FIXED, N_DIMS))
    data[:, 0:16] = np.nan            # 半數 zone 沒有效回波
    data[2, 20] = 9.0
    assert sensitivity(data, "tof_l") == pytest.approx(9.0)


# ------------------------------------------- viseme 類別來自 vocab.json


def test_viseme_classes_come_from_the_vocab_file():
    word_to_viseme, labels = load_viseme_map()
    assert set(labels) == {"A", "B", "C", "D", "F", "G"}
    assert labels["A"] == "A 雙唇"
    assert word_to_viseme["si"] == "F"          # 「四」= 擦音
    assert word_to_viseme["wu"] == "B"          # 「五」= 圓唇
    assert list(labels) == sorted(labels)       # 依字母排序


def test_vocab_has_no_tongue_viseme_but_expected_table_does():
    """story 的預期表有 E 舌音，`config/vocab.json` 沒有——**目前的詞彙集
    一個舌音詞都沒有，所以那一列永遠不會有樣本。**"""
    _, labels = load_viseme_map()
    assert "E" not in labels
    assert "E" in EXPECTED_PATTERN
    assert "G" in labels
    assert "G" not in EXPECTED_PATTERN


def test_load_viseme_map_reads_a_custom_file(tmp_path):
    path = tmp_path / "vocab.json"
    path.write_text(json.dumps({"words": [
        {"id": "x", "text": "X", "viseme": "Z 測試"},
        {"id": "y", "text": "Y"},                       # 沒有 viseme 欄位
    ]}), encoding="utf-8")
    word_to_viseme, labels = load_viseme_map(path)
    assert word_to_viseme == {"x": "Z"}
    assert labels == {"Z": "Z 測試"}


def test_grid_is_three_by_six():
    """驗收條件：3×6 熱力圖。"""
    rng = np.random.RandomState(3)
    report = viseme_sensitivity_report(build_samples(rng))
    assert len(report["modality_order"]) == 3
    assert len(report["viseme_order"]) == 6
    for key in report["viseme_order"]:
        assert set(report["table"][key]) == set(MODALITY_ORDER)


def test_viseme_with_no_samples_still_appears_as_an_empty_cell():
    """空格子要看得見——靜靜消失的話沒有人會發現那個類別從來沒被錄到。"""
    rng = np.random.RandomState(4)
    samples = [("ba", sample(rng)) for _ in range(3)]      # 只有 viseme A
    report = viseme_sensitivity_report(samples)
    assert len(report["viseme_order"]) == 6
    assert report["table"]["A"]["tof_l"]["n"] == 3
    for key in ("B", "C", "D", "F", "G"):
        cell = report["table"][key]["mel"]
        assert cell["n"] == 0
        assert cell["mean"] is None and cell["strength"] is None


def test_unknown_words_are_reported_not_silently_dropped():
    rng = np.random.RandomState(5)
    samples = [("ba", sample(rng)), ("not_a_word", sample(rng))]
    report = viseme_sensitivity_report(samples)
    assert report["unknown_words"] == ["not_a_word"]
    assert "not_a_word" in format_report(report)


# ----------------------------------------------------- 樣本數與標準誤


def test_each_cell_has_n_and_standard_error():
    """驗收條件：每格有樣本數與標準誤。"""
    rng = np.random.RandomState(6)
    report = viseme_sensitivity_report(build_samples(rng, n_per_word=5))
    for key in report["viseme_order"]:
        for modality in MODALITY_ORDER:
            cell = report["table"][key][modality]
            assert cell["n"] >= 5
            assert cell["sem"] is not None and cell["sem"] > 0
            assert cell["sem"] == pytest.approx(cell["std"] / np.sqrt(cell["n"]))


def test_single_sample_has_no_standard_error():
    """`n == 1` 時標準誤是 `None` 而不是 0——填 0 會讓那一格看起來像
    「量得非常準」。"""
    rng = np.random.RandomState(7)
    word_to_viseme, labels = load_viseme_map()
    table, _ = sensitivity_table([("ba", sample(rng))], word_to_viseme, labels)
    cell = table["A"]["tof_l"]
    assert cell["n"] == 1
    assert cell["sem"] is None
    assert cell["std"] is None
    assert cell["mean"] is not None


def test_sem_shrinks_with_more_samples():
    rng = np.random.RandomState(8)
    word_to_viseme, labels = load_viseme_map()
    few, _ = sensitivity_table([("ba", sample(rng, tof_gain=6.0)) for _ in range(4)],
                               word_to_viseme, labels)
    many, _ = sensitivity_table([("ba", sample(rng, tof_gain=6.0)) for _ in range(64)],
                                word_to_viseme, labels)
    assert many["A"]["tof_l"]["sem"] < few["A"]["tof_l"]["sem"]


# ------------------------------------------------------- 強度與預期比對


def test_strength_levels_are_ordered():
    assert strength_for(20.0) == "strong"
    assert strength_for(7.0) == "medium_strong"
    assert strength_for(4.0) == "medium"
    assert strength_for(0.5) == "weak"
    assert strength_for(-2.0) == "weak"
    assert strength_for(None) is None
    assert strength_for(float("nan")) is None
    assert STRENGTH_ORDER == ("weak", "medium", "medium_strong", "strong")


def test_expected_comparison_marks_missing_and_unexpected_classes():
    rng = np.random.RandomState(9)
    report = viseme_sensitivity_report(build_samples(rng))
    comparison = report["expected_comparison"]
    assert comparison["E"]["status"] == "no_samples"        # 不在詞彙集裡
    assert comparison["G"]["status"] == "no_expectation"    # 有樣本、無預期
    assert comparison["G"]["expected"] is None              # 不硬套猜的預期
    assert comparison["A"]["status"] == "ok"


def test_expected_comparison_reports_level_deltas():
    table = {"A": {
        "tof_l": {"n": 3, "mean": 20.0, "sem": 1.0, "std": 1.7,
                  "excess_mean": 16.0, "strength": "strong"},
        "tof_r": {"n": 3, "mean": 20.0, "sem": 1.0, "std": 1.7,
                  "excess_mean": 16.0, "strength": "strong"},
        "mel": {"n": 3, "mean": 3.4, "sem": 0.1, "std": 0.2,
                "excess_mean": 0.0, "strength": "weak"},
    }}
    comparison = compare_to_expected(table)
    cells = comparison["A"]["cells"]
    assert cells["tof_l"]["match"] is True and cells["tof_l"]["delta_levels"] == 0
    # 預期 medium_strong、觀測 weak → 低兩級
    assert cells["mel"]["match"] is False and cells["mel"]["delta_levels"] == -2


# ------------------------------------------------- 驗收條件：擦音檢查


def test_fricative_is_stronger_on_mel_than_tof():
    """驗收條件：擦音在 Mel 上明顯強於 ToF。這是「五／四」設計的核心。"""
    rng = np.random.RandomState(10)
    report = viseme_sensitivity_report(build_samples(rng))
    check = report["fricative_check"]
    assert check["pass"] is True
    assert check["mel_mean"] > check["tof_best_mean"]
    assert check["basis"] == "mean + max(sem)"
    assert "✅ 通過" in format_report(report)


def test_fricative_check_fails_when_tof_wins():
    """反例：擦音若在 ToF 上比較強，這條驗收就該紅，不能永遠回 pass。"""
    rng = np.random.RandomState(11)
    word_to_viseme, labels = load_viseme_map()
    samples = [("si", sample(rng, tof_gain=8.0)) for _ in range(5)]
    table, _ = sensitivity_table(samples, word_to_viseme, labels)
    check = fricative_check(table)
    assert check["pass"] is False


def test_fricative_check_is_undecidable_without_samples():
    rng = np.random.RandomState(12)
    word_to_viseme, labels = load_viseme_map()
    table, _ = sensitivity_table([("ba", sample(rng))], word_to_viseme, labels)
    check = fricative_check(table)
    assert check["pass"] is None and "沒有樣本" in check["reason"]
    assert "無法判定" in format_report(viseme_sensitivity_report([("ba", sample(rng))]))


def test_fricative_check_falls_back_when_only_one_sample():
    rng = np.random.RandomState(13)
    word_to_viseme, labels = load_viseme_map()
    table, _ = sensitivity_table([("si", sample(rng, mel_gain=8.0))],
                                 word_to_viseme, labels)
    check = fricative_check(table)
    assert check["pass"] is True
    assert check["basis"].startswith("mean only")
    assert check["margin"] == 0.0


# ------------------------------------------------ 「ToF 均勻地弱」的警示


def test_uniform_weak_tof_fires_when_nothing_reaches_the_sensor():
    """story：均勻的弱通常代表訊號根本沒進來，**不是**「這些音素本來就難」。"""
    rng = np.random.RandomState(14)
    word_to_viseme, labels = load_viseme_map()
    samples = [(word_id, sample(rng)) for word_id in word_to_viseme] * 3
    table, _ = sensitivity_table(samples, word_to_viseme, labels)
    assert uniform_weak_tof(table) is True

    report = viseme_sensitivity_report(samples)
    text = format_report(report)
    assert "均勻地弱" in text
    assert "實驗 A" in text


def test_uniform_weak_tof_is_false_when_any_viseme_is_strong():
    rng = np.random.RandomState(15)
    report = viseme_sensitivity_report(build_samples(rng))
    assert report["uniform_weak_tof"] is False
    assert "均勻地弱" not in format_report(report)


def test_uniform_weak_tof_is_false_without_any_samples():
    """沒有資料不等於「均勻地弱」——那會變成憑空的警告。"""
    word_to_viseme, labels = load_viseme_map()
    table, _ = sensitivity_table([], word_to_viseme, labels)
    assert uniform_weak_tof(table) is False


# ------------------------------------------------------------- 報告與圖


def test_report_defaults_to_synthetic():
    """假資料才是目前的常態；預設 `False` 會讓忘記傳的人產出一份看起來
    像真實結論的報告。"""
    rng = np.random.RandomState(16)
    report = viseme_sensitivity_report(build_samples(rng))
    assert report["is_synthetic"] is True
    assert "假資料" in format_report(report)

    real = viseme_sensitivity_report(build_samples(rng), is_synthetic=False)
    assert "假資料" not in format_report(real)


def test_report_carries_the_zone_layout_caveat():
    """`D11` 發現 zone 佈局 row-major 仍是未驗證假設。**這張圖的結論是
    「哪個位置對哪個嘴型敏感」，佈局錯了結論就是空間反的。**"""
    rng = np.random.RandomState(17)
    report = viseme_sensitivity_report(build_samples(rng))
    assert "ASSUMED, unverified" in report["zone_layout_note"]
    assert "ASSUMED, unverified" in format_report(report)


def test_report_discusses_the_gap_to_the_expected_pattern():
    """驗收條件：與預期模式的落差有討論。"""
    rng = np.random.RandomState(18)
    text = format_report(viseme_sensitivity_report(build_samples(rng)))
    assert "與預期型態的落差" in text
    assert "Viseme E" in text and "詞彙集根本錄不到" in text
    assert "不硬套一個猜的預期" in text


def test_plot_returns_a_figure_with_annotated_cells():
    rng = np.random.RandomState(19)
    report = viseme_sensitivity_report(build_samples(rng))
    fig = plot_viseme_sensitivity(report, dpi=100)
    ax = fig.axes[0]
    texts = [t.get_text() for t in ax.texts]
    assert len(texts) == 6 * 3                       # 每格都有標註
    assert all("n=" in t for t in texts)
    assert any("±" in t for t in texts)


def test_plot_marks_empty_cells_as_n_zero_not_zero():
    """沒有樣本的格子不畫成 0——0 是一個關於敏感度的陳述，空白才是
    「沒有資料」。"""
    rng = np.random.RandomState(20)
    samples = [("ba", sample(rng, tof_gain=6.0)) for _ in range(3)]
    fig = plot_viseme_sensitivity(viseme_sensitivity_report(samples), dpi=100)
    texts = [t.get_text() for t in fig.axes[0].texts]
    assert texts.count("n=0") == 5 * 3               # 只有 viseme A 有資料


def test_plot_titles_and_labels_are_english_only():
    """圖表文字一律英文（調度員規則）。用 `analysis/reporting/text_checks.py`
    的共用 helper——原本 D14/D17 各寫一份一模一樣的 `has_cjk`，兩份相同的
    實作遲早漂掉一份，而漂掉的那份不會報錯，只會安靜地少檢查一些東西。"""
    from analysis.reporting.text_checks import assert_english_only

    rng = np.random.RandomState(21)
    fig = plot_viseme_sensitivity(viseme_sensitivity_report(build_samples(rng)), dpi=100)
    assert_english_only(fig)


def test_plot_title_warns_when_synthetic():
    rng = np.random.RandomState(22)
    synthetic = plot_viseme_sensitivity(viseme_sensitivity_report(build_samples(rng)),
                                        dpi=100)
    assert "SYNTHETIC" in synthetic.axes[0].get_title()
    real = plot_viseme_sensitivity(
        viseme_sensitivity_report(build_samples(rng), is_synthetic=False), dpi=100)
    assert "SYNTHETIC" not in real.axes[0].get_title()


def test_report_is_markdown_and_ends_with_a_newline():
    rng = np.random.RandomState(23)
    text = format_report(viseme_sensitivity_report(build_samples(rng)))
    assert text.endswith("\n")
    assert text.count("|---") >= 1


def test_empty_sample_set_does_not_crash():
    report = viseme_sensitivity_report([])
    assert report["n_samples"] == 0
    assert report["fricative_check"]["pass"] is None
    assert report["uniform_weak_tof"] is False
    text = format_report(report)
    assert "n=0" in text
    fig = plot_viseme_sensitivity(report, dpi=100)
    assert len(fig.axes[0].texts) == 6 * 3


# ------------------------------------- 上游 σ 下限出問題時的自我防護（§3.2.2）


def test_implausible_max_abs_z_is_flagged_as_an_upstream_problem():
    """一個貼著剛性表面的 zone，若上游用 `1e-3` 當 σ 下限，z 會衝到幾十
    ——而 `max` 只取最大的那一個，**一個壞掉的 zone 就決定整格的顏色**，
    整張圖看起來只像「這個 viseme 特別敏感」。"""
    from analysis.experiments.d14_viseme_sensitivity import (
        IMPLAUSIBLE_MAX_ABS_Z,
        implausible_cells,
    )
    rng = np.random.RandomState(30)
    samples = []
    for _ in range(4):
        data = sample(rng)
        data[5, 7] = 400.0                    # 一個 σ≈0 的 zone 爆掉
        samples.append(("ba", data))
    report = viseme_sensitivity_report(samples)

    flagged = report["implausible_cells"]
    assert flagged, "沒有標記出來的話，這張圖會被當成真實的敏感度差異"
    assert flagged[0]["viseme"] == "A" and flagged[0]["modality"] == "tof_l"
    assert flagged[0]["mean"] > IMPLAUSIBLE_MAX_ABS_Z

    text = format_report(report)
    assert "物理上講不通" in text
    assert "先查上游的 σ 下限" in text


def test_normal_magnitudes_are_not_flagged():
    """正常的強訊號不能被誤報——誤報會讓人忽略真的警告。"""
    rng = np.random.RandomState(31)
    report = viseme_sensitivity_report(build_samples(rng))
    assert report["implausible_cells"] == []
    assert "物理上講不通" not in format_report(report)


def test_implausible_cells_on_an_empty_table():
    from analysis.experiments.d14_viseme_sensitivity import implausible_cells
    word_to_viseme, labels = load_viseme_map()
    table, _ = sensitivity_table([], word_to_viseme, labels)
    assert implausible_cells(table) == []


# ---------------------------------------------------------------------------
# 唇動先行量讀取端（`lip_lead_samples`/`compare_lip_lead_versions`）
# ---------------------------------------------------------------------------

def _make_trial(**attrs):
    """最小可用的 `Trial`——這裡只測 `.attrs` 篩選邏輯，其餘欄位給空陣列
    即可，不需要真的形狀正確的 ToF/mic 資料。"""
    empty2d = np.zeros((0, 0))
    empty1d = np.zeros((0,))
    return Trial(
        key="trial_000", label="wu", wear_id=1, mode="quiz",
        speaking_mode=attrs.pop("speaking_mode", "normal"),
        quality="ok",
        tof_a=empty2d, tof_b=empty2d,
        tof_valid_a=empty2d.astype(bool), tof_valid_b=empty2d.astype(bool),
        tof_t_us=empty1d.astype(np.int64), mic_rms=empty1d,
        mel=None, attrs=attrs,
    )


def test_lip_lead_samples_excludes_non_comparable_trials():
    """`comparable` 不是明確 `True`（`None` 或 `False`）都要排除。"""
    trials = [
        _make_trial(comparable=True, voice_onset_us=200_000, lip_onset_us=100_000),
        _make_trial(comparable=False, voice_onset_us=200_000, lip_onset_us=100_000),
        _make_trial(voice_onset_us=200_000, lip_onset_us=100_000),  # comparable 缺席
    ]
    samples = lip_lead_samples(trials)
    assert samples["fused"] == [100_000.0]  # 只有第一筆被納入


def test_lip_lead_samples_excludes_trials_without_voice_onset():
    """`voice_onset_us` 缺席（silent 模式必然如此）要整筆跳過，不能補 0。"""
    trials = [_make_trial(comparable=True, lip_onset_us=100_000, speaking_mode="silent")]
    samples = lip_lead_samples(trials)
    assert samples == {"fused": [], "single_a": [], "single_b": []}


def test_lip_lead_samples_handles_missing_sensor_b_independently():
    """`lip_onset_us_B` 缺席是 `union_min` 設計本身允許的正常狀況——
    `single_b` 跳過，不影響 `fused`/`single_a`。"""
    trials = [_make_trial(
        comparable=True, voice_onset_us=250_000,
        lip_onset_us=100_000, lip_onset_us_A=100_000,
        # lip_onset_us_B 故意不給
    )]
    samples = lip_lead_samples(trials)
    assert samples["fused"] == [150_000.0]
    assert samples["single_a"] == [150_000.0]
    assert samples["single_b"] == []


def test_compare_lip_lead_versions_reports_stats_and_marks_synthetic():
    """煙霧測試：合成資料餵進去，三個版本的統計量都算得出來，且格式沒有
    宣稱這是真實結論。"""
    rng = np.random.default_rng(0)
    trials = []
    for _ in range(20):
        lip = 100_000
        voice = lip + int(rng.normal(100_000, 10_000))  # 平均先行量約 100ms
        trials.append(_make_trial(
            comparable=True, voice_onset_us=voice,
            lip_onset_us=lip, lip_onset_us_A=lip, lip_onset_us_B=lip,
        ))
    result = compare_lip_lead_versions(trials)
    for version in ("fused", "single_a", "single_b"):
        assert result[version]["n"] == 20
        assert result[version]["median_ms"] is not None

    report = format_lip_lead_report(result, is_synthetic=True)
    assert "合成資料" in report
    assert "不是真實結論" in report


def test_compare_lip_lead_versions_with_no_data_reports_zero_not_crash():
    result = compare_lip_lead_versions([])
    for version in ("fused", "single_a", "single_b"):
        assert result[version]["n"] == 0
        assert result[version]["median_ms"] is None
    # 不該炸掉，且報告要能印出「0 筆」而不是拋例外
    format_lip_lead_report(result)
