import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd
import pytest

from analysis.experiments.exp_d12_wear_cv import (
    CV_PASS_THRESHOLD,
    RATIO_IMPROVEMENT_THRESHOLD,
    distance_based_wear_ratio,
    extract_scalar_features,
    format_report,
    plot_within_between_boxplot,
    scalar_cv_within_between,
    wear_verdict,
)
from analysis.similarity.cosine_baseline import cosine_dist


def test_scalar_cv_within_between_known_case():
    """人工構造：組內幾乎不變（小雜訊），組間有明顯位移，驗證 within << between。"""
    rng = np.random.default_rng(0)
    rows = []
    wear_means = {1: 100.0, 2: 130.0, 3: 70.0}
    for wear_id, mean in wear_means.items():
        for _ in range(10):
            rows.append({"wear_id": wear_id, "value": mean + rng.normal(0, 1.0)})
    df = pd.DataFrame(rows)

    result = scalar_cv_within_between(df, "wear_id", "value")

    assert result["cv_within"] < 0.05  # 組內只有 std=1 的小雜訊，CV 很小
    assert result["cv_between"] > 0.10  # 組間平均值差到 70~130，CV 明顯大
    assert result["ratio"] > 2.0


def test_scalar_cv_within_between_zero_mean_guard():
    df = pd.DataFrame({
        "wear_id": [1, 1, 2, 2],
        "value": [0.0, 0.0, 0.0, 0.0],
    })
    result = scalar_cv_within_between(df, "wear_id", "value")
    assert np.isnan(result["cv_within"])
    assert np.isnan(result["cv_between"])


def test_extract_scalar_features_masks_invalid_zones():
    T, M = 5, 8
    tof_a = np.zeros((T, 32))
    tof_b = np.zeros((T, 32))
    tof_a[:, :16] = 500.0   # 距離
    tof_a[:, 16:] = 50.0    # signal
    tof_b[:, :16] = 600.0
    tof_b[:, 16:] = 60.0

    # 注入離譜的無效值，若沒被遮蔽，平均值會被嚴重污染
    tof_a[0, 3] = 999999.0
    valid_a = np.ones((T, 16), dtype=bool)
    valid_a[0, 3] = False
    valid_b = np.ones((T, 16), dtype=bool)

    mel = np.full((M, 40), 2.0)

    feats = extract_scalar_features(tof_a, valid_a, tof_b, valid_b, mel)

    assert feats["tof_L_distance"] == pytest.approx(500.0, abs=1e-6)
    assert feats["tof_R_distance"] == pytest.approx(600.0, abs=1e-6)
    assert feats["signal_rate"] == pytest.approx(55.0, abs=1e-6)
    assert feats["mel_total_energy"] == pytest.approx(2.0 * M * 40, abs=1e-6)


def _make_trial(rng, base, n_frames=20, n_dims=None, noise=0.05):
    n_dims = n_dims if n_dims is not None else base.shape[0]
    return base[None, :] + noise * rng.normal(size=(n_frames, n_dims))


def test_distance_based_wear_ratio_detects_non_uniform_shift_not_global_translation():
    """驗收條件的核心 + 調度員特別交代的陷阱驗證：

    跨次戴的變異刻意做成「各 zone 不等量的偏移」，不是均勻平移——
    如果測資錯誤地用了均勻平移，cosine 距離會完全量不到差異，
    這個測試就是要證明目前的合成資料設計沒有踩到那個坑
    （between 距離必須明顯大於 within 距離）。
    """
    rng = np.random.default_rng(1)
    n_dims = 16
    base = rng.normal(size=n_dims) * 3 + 20.0  # 同一個 label 的基準特徵

    trials_by_wear = {}
    for wear_id in range(3):
        # 每次戴的擾動：每一維各自獨立的隨機偏移量（不等量、非均勻），
        # 不是「base + 同一個常數」。
        wear_shift = rng.normal(0, 2.0, size=n_dims)
        wear_center = base + wear_shift
        trials_by_wear[wear_id] = [
            _make_trial(rng, wear_center, n_dims=n_dims) for _ in range(4)
        ]

    result = distance_based_wear_ratio(trials_by_wear, cosine_dist)

    assert result["ratio"] > 1.2
    assert result["between_mean"] > result["within_mean"]


def test_distance_based_wear_ratio_uniform_translation_hides_variation():
    """反向驗證：如果測資錯誤地用均勻平移模擬跨次戴變異，`base` 向量本身
    已經是「大常數（20）+ 小雜訊（std=3）」主導方向的向量，均勻平移
    （所有維度加同一個常數）幾乎不改變方向——結果是 within 跟 between
    的 cosine 距離都塌縮到近乎 0（數值噪音量級，1e-6~1e-4），
    不是「比值接近 1」，而是**兩組距離都小到無法可靠分辨**。

    這才是這個坑真正危險的地方：不是比值剛好等於 1，而是整個量測
    塌縮到數值噪音區間，讓下游任何依賴這個距離的判斷都變得不穩定。
    跟前一個測試（不等量偏移）的 within/between 量級（0.0x~0.x）對比，
    這裡兩者都小了好幾個數量級。
    """
    rng = np.random.default_rng(2)
    n_dims = 16
    base = rng.normal(size=n_dims) * 3 + 20.0

    trials_by_wear = {}
    for wear_id in range(3):
        uniform_shift = np.full(n_dims, wear_id * 5.0)  # 均勻平移：所有維度加同一個常數
        wear_center = base + uniform_shift
        trials_by_wear[wear_id] = [
            _make_trial(rng, wear_center, n_dims=n_dims) for _ in range(4)
        ]

    result = distance_based_wear_ratio(trials_by_wear, cosine_dist)

    assert result["within_mean"] < 1e-3
    assert result["between_mean"] < 1e-2


def test_distance_based_wear_ratio_requires_multiple_wears_and_trials():
    with pytest.raises(ValueError):
        distance_based_wear_ratio({1: [np.zeros((5, 4))]}, cosine_dist)
    with pytest.raises(ValueError):
        distance_based_wear_ratio(
            {1: [np.zeros((5, 4))], 2: [np.zeros((5, 4))]}, cosine_dist
        )  # 每個 wear 只有 1 筆，算不出組內距離


def test_wear_verdict_pass_and_needs_improvement():
    """驗收條件：`between > within * 1.5` 時自動列出改進建議；`CV < 30%` 通過判定。"""
    v_pass = wear_verdict(cv_within=0.05, cv_between=0.06)
    assert v_pass["passed"] is True
    assert v_pass["needs_improvement"] is False
    assert v_pass["cv_threshold"] == CV_PASS_THRESHOLD

    v_fail = wear_verdict(cv_within=0.05, cv_between=0.35)
    assert v_fail["passed"] is False
    assert v_fail["ratio"] == pytest.approx(7.0)
    assert v_fail["needs_improvement"] is True
    assert v_fail["ratio_threshold"] == RATIO_IMPROVEMENT_THRESHOLD


def test_plot_within_between_boxplot_draws_two_groups_english_only():
    within = np.array([0.1, 0.2, 0.15])
    between = np.array([0.5, 0.6, 0.55, 0.52])

    fig = plot_within_between_boxplot(within, between)

    box_axes = [ax for ax in fig.axes if ax.patches or ax.lines]
    assert len(box_axes) >= 1

    texts = [fig._suptitle.get_text()] if fig._suptitle else []
    for ax in fig.axes:
        texts.append(ax.get_title())
        texts.append(ax.get_xlabel())
        texts.append(ax.get_ylabel())
        texts.extend(t.get_text() for t in ax.get_xticklabels())

    def has_cjk(s):
        return any("一" <= ch <= "鿿" for ch in s)

    for t in texts:
        assert not has_cjk(t), f"圖表文字含 CJK 字元: {t!r}"


def test_format_report_flags_synthetic_and_lists_suggestions_when_needed():
    verdicts = {
        "tof_L_distance": wear_verdict(0.05, 0.06),
        "tof_R_distance": wear_verdict(0.05, 0.40),  # 這個模態需要改進
    }
    distance_result = {"within_mean": 0.2, "between_mean": 0.5, "ratio": 2.5}

    report = format_report(verdicts, distance_result, is_synthetic=True)

    assert "合成資料" in report
    assert "改進建議" in report
    assert "三點支撐" in report


def test_format_report_no_suggestions_when_all_pass():
    verdicts = {"tof_L_distance": wear_verdict(0.05, 0.06)}
    distance_result = {"within_mean": 0.2, "between_mean": 0.22, "ratio": 1.1}

    report = format_report(verdicts, distance_result, is_synthetic=False)

    assert "暫不是主要問題" in report
    assert "合成資料" not in report
