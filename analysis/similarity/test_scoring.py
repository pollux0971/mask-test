import numpy as np
import pytest

from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.scoring import (
    DEFAULT_TAU,
    class_distances,
    fit_reject_threshold,
    normalize_distances,
    recognize_scores,
    softmax_scores,
)


def _scalar_dist(a, b):
    """簡單的純量距離函式，只用來測試 D06 的聚合/校準邏輯本身，
    刻意不牽扯 cosine_dist 的細節，讓統計結果好推導、好驗證。"""
    return abs(float(a) - float(b))


def test_scores_sum_to_one_for_various_inputs():
    """驗收條件：分數總和為 1。"""
    rng = np.random.default_rng(0)
    for _ in range(20):
        d_class = rng.uniform(0, 10, size=rng.integers(2, 10))
        scores = softmax_scores(normalize_distances(d_class), tau=rng.uniform(0.1, 3.0))
        assert scores.sum() == pytest.approx(1.0, abs=1e-9)
        assert np.all(scores >= 0)


def test_class_distances_uses_min_not_mean():
    """驗收條件（實作要求）：每類距離用 min 不用 mean。"""
    templates_by_class = {
        "A": [0.1, 5.0, 5.0],   # min=0.1，若用 mean 會是 ~3.4
        "B": [3.0, 3.0, 3.0],
    }
    classes, d_class = class_distances(2.0, templates_by_class, _scalar_dist)
    assert classes == ["A", "B"]
    np.testing.assert_allclose(d_class, [1.9, 1.0])  # |2-0.1|=1.9, |2-3|=1.0


def test_tau_controls_confidence_as_expected():
    """驗收條件：τ 可調且效果符合預期——τ 小分數極端，τ 大分數平坦。"""
    d_norm = np.array([0.0, 1.0, 2.0])  # 第一類明顯最接近

    scores_confident = softmax_scores(d_norm, tau=0.05)
    scores_flat = softmax_scores(d_norm, tau=5.0)

    assert scores_confident[0] > scores_flat[0]
    assert scores_confident[0] > 0.95   # tau 很小時幾乎All-in在最接近的類別
    assert scores_flat.max() - scores_flat.min() < 0.3  # tau 很大時接近均勻分布


def test_default_tau_is_documented_starting_value():
    assert DEFAULT_TAU == 0.5


def test_fit_reject_threshold_requires_at_least_two_templates():
    with pytest.raises(ValueError):
        fit_reject_threshold([1.0], _scalar_dist)


def test_fit_reject_threshold_known_case():
    # 5 筆彼此間距 0.1 的樣板，leave-one-out 最小距離全部是 0.1 -> p95 也是 0.1
    templates = [10.0, 10.1, 10.2, 10.3, 10.4]
    theta = fit_reject_threshold(templates, _scalar_dist)
    assert theta == pytest.approx(0.1, abs=1e-9)


def test_reject_rate_for_rest_and_false_reject_rate_for_words():
    """驗收條件：靜止輸入的拒識率 > 90%；真實語音的誤拒率 < 10%。

    用簡單的一維合成資料：8 個詞的中心彼此距離 10（隔得很開），
    _reject 類別中心離所有詞極遠（1000）。同一個雜訊尺度（std=0.2）
    同時決定「詞內部變異」與「靜止內部變異」，theta_reject 從
    _reject 類別自己的 leave-one-out 分布校準出來。

    每一類（含 _reject）用相同的樣板數 —— 這是實際 enrollment 最自然
    的假設（同一套錄製流程，詞跟靜止收集的次數量級相同）。實測發現
    這個比例會影響誤拒率的高低（樣板數少時，min-over-N 的統計量本來
    就偏大），數字對不上時要調的是 enrollment 樣板數，不是門檻公式本身。
    """
    rng = np.random.default_rng(7)
    noise_std = 0.2
    n_word_templates = 20
    n_reject_templates = 20
    n_trials = 300

    word_centers = {f"word_{i}": i * 10.0 for i in range(8)}
    templates_by_class = {
        label: list(center + noise_std * rng.standard_normal(n_word_templates))
        for label, center in word_centers.items()
    }
    reject_center = 1000.0
    reject_templates = list(reject_center + noise_std * rng.standard_normal(n_reject_templates))

    theta_reject = fit_reject_threshold(reject_templates, _scalar_dist)

    # 情境一：真的是靜止 -> 應該大多數被拒識
    rest_queries = reject_center + noise_std * rng.standard_normal(n_trials)
    rest_rejects = [
        recognize_scores(q, templates_by_class, _scalar_dist, theta_reject=theta_reject)[2]
        for q in rest_queries
    ]
    rest_reject_rate = np.mean(rest_rejects)
    assert rest_reject_rate > 0.90, f"靜止拒識率只有 {rest_reject_rate:.2%}"

    # 情境二：真的是某個詞 -> 誤拒率應該很低
    true_label = "word_3"
    word_queries = word_centers[true_label] + noise_std * rng.standard_normal(n_trials)
    word_rejects = [
        recognize_scores(q, templates_by_class, _scalar_dist, theta_reject=theta_reject)[2]
        for q in word_queries
    ]
    false_reject_rate = np.mean(word_rejects)
    assert false_reject_rate < 0.10, f"真實語音誤拒率高達 {false_reject_rate:.2%}"


def test_recognize_scores_integration_with_real_cosine_dist():
    """跟 D04 的 cosine_dist 串起來的整合測試，確認介面真的相容。"""
    rng = np.random.default_rng(9)
    T, D = 24, 104
    word_bases = {f"w{i}": rng.normal(size=(T, D)) for i in range(4)}
    templates_by_class = {
        label: [base + 0.05 * rng.normal(size=(T, D)) for _ in range(5)]
        for label, base in word_bases.items()
    }
    query = word_bases["w2"] + 0.05 * rng.normal(size=(T, D))

    classes, scores, reject = recognize_scores(query, templates_by_class, cosine_dist)

    assert classes == list(templates_by_class.keys())
    assert scores.sum() == pytest.approx(1.0, abs=1e-9)
    assert reject is None  # 沒給 theta_reject 就不判斷
    assert classes[int(np.argmax(scores))] == "w2"  # 最高分應該落在真正的類別
