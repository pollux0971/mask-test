import numpy as np
import pytest

from analysis.features.feature_assembly import FEATURE_DIM
from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.fusion import TriResult, compute_tri_result
from analysis.similarity.scoring import softmax_scores


def _tri(d_tof, d_mel, tau=0.5, reject_tof=False, reject_mel=False):
    return TriResult(
        classes=["a", "b", "c"],
        d_tof=np.asarray(d_tof, dtype=np.float64),
        d_mel=np.asarray(d_mel, dtype=np.float64),
        reject_tof=reject_tof,
        reject_mel=reject_mel,
        theta_reject_tof=1.0,
        theta_reject_mel=1.0,
        tau=tau,
    )


def test_fuse_w1_equals_pure_tof():
    """驗收條件：fuse(w) 在 w=1 時等於純 ToF。"""
    d_tof = np.array([0.0, 1.0, 2.0])
    d_mel = np.array([2.0, 0.0, 1.0])  # 刻意跟 d_tof 完全不同，確認沒被混進來
    tri = _tri(d_tof, d_mel)

    np.testing.assert_allclose(tri.fuse(1.0), softmax_scores(d_tof, tau=tri.tau))


def test_fuse_w0_equals_pure_mel():
    """驗收條件：fuse(w) 在 w=0 時等於純 Mel。"""
    d_tof = np.array([0.0, 1.0, 2.0])
    d_mel = np.array([2.0, 0.0, 1.0])
    tri = _tri(d_tof, d_mel)

    np.testing.assert_allclose(tri.fuse(0.0), softmax_scores(d_mel, tau=tri.tau))


def test_fuse_half_with_identical_distributions_matches_either_pure():
    """兩軌給完全相同的分數分布時，w=0.5 的結果要跟兩軌都一樣
    （調度員特別交代要釘死的案例）。"""
    same = np.array([0.3, 1.7, 0.9, 2.4])
    tri = _tri(same, same)

    scores_half = tri.fuse(0.5)
    scores_pure = softmax_scores(same, tau=tri.tau)

    np.testing.assert_allclose(scores_half, scores_pure)
    np.testing.assert_allclose(tri.fuse(0.0), tri.fuse(1.0))
    np.testing.assert_allclose(tri.fuse(0.0), scores_half)


def test_fuse_rejects_w_outside_unit_interval():
    tri = _tri([0.0, 1.0], [1.0, 0.0])
    with pytest.raises(ValueError):
        tri.fuse(-0.1)
    with pytest.raises(ValueError):
        tri.fuse(1.1)


def test_top1_matches_argmax_of_fuse():
    d_tof = np.array([0.0, 5.0, 5.0])  # 類別 0 最接近
    d_mel = np.array([5.0, 5.0, 0.0])  # 類別 2 最接近
    tri = _tri(d_tof, d_mel)

    assert tri.top1(1.0) == "a"
    assert tri.top1(0.0) == "c"


def test_frontend_can_recompute_many_w_without_recomputing_distances():
    """驗收條件：前端能只用回傳結構重算任意 w（不需要重新比對）。"""
    d_tof = np.array([0.2, 3.0, 1.5])
    d_mel = np.array([2.5, 0.1, 1.0])
    tri = _tri(d_tof, d_mel)

    results = {w: tri.top1(w) for w in np.linspace(0, 1, 11)}
    # 兩端點必須是各自的最佳類別，且沒有任何一次呼叫改到 d_tof/d_mel 本身
    assert results[0.0] == "b"
    assert results[1.0] == "a"
    np.testing.assert_array_equal(tri.d_tof, d_tof)
    np.testing.assert_array_equal(tri.d_mel, d_mel)


def _make_seq(base_1d, rng, T=3, noise=0.1):
    """把一個 1D 中心向量變成 (T, D) 序列，逐幀加一點雜訊——
    所有距離函式（cosine_dist / dtw_dist）都吃 (T, D)，不是 1D 向量。"""
    return base_1d[None, :] + noise * rng.normal(size=(T, base_1d.shape[0]))


def _synthetic_templates(rng, n_classes=4):
    """104 維合成樣板：ToF 段（0:64）與 Mel 段（64:104）各自獨立編碼類別，
    讓 ToF-only 與 Mel-only 都各自能正確辨識，用來驗證三軌分數各自正確。"""
    slices = {"tof": slice(0, 64), "mel": slice(64, FEATURE_DIM)}
    templates_by_class = {}
    class_bases = {}
    for c in range(n_classes):
        base = np.zeros(FEATURE_DIM)
        base[slices["tof"]] = rng.normal(size=64) + c * 5.0   # 每類 ToF 段中心不同
        base[slices["mel"]] = rng.normal(size=40) + c * 5.0   # 每類 Mel 段中心也不同
        class_bases[f"class_{c}"] = base
        templates_by_class[f"class_{c}"] = [_make_seq(base, rng) for _ in range(5)]
    reject_templates = [_make_seq(np.zeros(FEATURE_DIM), rng, noise=0.05) for _ in range(10)]
    return slices, templates_by_class, class_bases, reject_templates


def test_three_tracks_each_correct_end_to_end():
    """驗收條件：三軌分數各自正確——用合成資料驗證 ToF-only 與 Mel-only
    各自都能正確辨識出真正的類別（不是只有融合後才對）。"""
    rng = np.random.default_rng(0)
    slices, templates_by_class, class_bases, reject_templates = _synthetic_templates(rng)

    query = _make_seq(class_bases["class_2"], rng)
    tri = compute_tri_result(query, templates_by_class, reject_templates, slices, cosine_dist)

    assert tri.top1(1.0) == "class_2"  # 純 ToF
    assert tri.top1(0.0) == "class_2"  # 純 Mel
    assert tri.top1(0.5) == "class_2"  # 融合


def test_normalization_brings_mismatched_raw_scales_to_comparable_range():
    """驗收條件：距離正規化後兩模態量級相當。

    人工構造 ToF 段量級是 Mel 段的 100 倍，確認正規化後 d_tof/d_mel
    的數值範圍/標準差彼此相當，不會出現「w=0.5 其實幾乎只用其中一個」。
    """
    rng = np.random.default_rng(1)
    slices = {"tof": slice(0, 64), "mel": slice(64, FEATURE_DIM)}
    scale = np.array([100.0] * 64 + [0.01] * 40)
    templates_by_class = {}
    class_bases = {}
    for c in range(4):
        base = np.zeros(FEATURE_DIM)
        base[slices["tof"]] = rng.normal(size=64) * 100.0 + c * 500.0  # ToF 量級大
        base[slices["mel"]] = rng.normal(size=40) * 0.01 + c * 0.05    # Mel 量級小 100 倍
        class_bases[f"class_{c}"] = base
        templates_by_class[f"class_{c}"] = [
            base[None, :] + rng.normal(size=(3, FEATURE_DIM)) * scale for _ in range(5)
        ]
    reject_scale = np.array([1.0] * 64 + [0.001] * 40)
    reject_templates = [
        rng.normal(size=(3, FEATURE_DIM)) * reject_scale for _ in range(10)
    ]

    query = _make_seq(class_bases["class_1"], rng, noise=0.0)
    tri = compute_tri_result(query, templates_by_class, reject_templates, slices, cosine_dist)

    # 正規化前兩模態原始距離量級差 100 倍；正規化後標準差都被拉到 ~1（D06 normalize_distances 的定義）
    assert tri.d_tof.std() == pytest.approx(1.0, abs=0.3)
    assert tri.d_mel.std() == pytest.approx(1.0, abs=0.3)
    assert tri.d_tof.min() == pytest.approx(0.0, abs=1e-9)
    assert tri.d_mel.min() == pytest.approx(0.0, abs=1e-9)


def test_reject_tof_still_works_after_wiring_into_d07():
    """調度員特別交代：D06 的拒識機制融合進 D07 之後，w=1.0（純 ToF）
    還能不能正常運作——不是只驗證分數，要驗證 reject_tof 這個判定本身。

    跟 D06 自己的測試一樣用統計方式驗證（很多次試驗看整體比例），不是斷言
    單一一次抽樣的結果——D06 已經證實這個門檻機制本身是機率性的，單一樣本
    斷言碰到尾端就會不穩定 (flaky)，用整體比例驗證「機制還有沒有正常運作」
    才是誠實的做法。

    **合成資料的坑（值得記錄）：** 一開始我用「每個類別中心 = 隨機向量 +
    `i*某常數`（常數加到每一維）」，結果所有類別的向量幾乎指向同一個方向
    （常數項在所有維度上是共同的，隨機項相對很小），cosine 距離幾乎分不出
    類別，導致這裡的拒識判定整個崩潰（100% 誤拒真詞）。改成每個類別中心是
    各自獨立、正規化到相同量級的隨機方向（`_random_direction`）才正確。
    """
    rng = np.random.default_rng(2)
    slices = {"tof": slice(0, 8), "mel": slice(8, 12)}
    n_dims = 12
    n_trials = 100
    n_templates = 30
    noise = 0.15

    def _random_direction(rng, dims, magnitude=10.0):
        v = rng.normal(size=dims)
        return v / np.linalg.norm(v) * magnitude

    word_centers = {f"w{i}": _random_direction(rng, n_dims) for i in range(4)}
    templates_by_class = {
        label: [_make_seq(center, rng, noise=noise) for _ in range(n_templates)]
        for label, center in word_centers.items()
    }
    reject_center = _random_direction(rng, n_dims)
    reject_templates = [_make_seq(reject_center, rng, noise=noise) for _ in range(n_templates)]

    # 情境一：query 真的是某個詞 -> ToF 大多數情況不應該拒識
    word_rejects = []
    for _ in range(n_trials):
        word_query = _make_seq(word_centers["w2"], rng, noise=noise)
        tri = compute_tri_result(word_query, templates_by_class, reject_templates, slices, cosine_dist)
        word_rejects.append(tri.reject_tof)
    word_false_reject_rate = np.mean(word_rejects)
    assert word_false_reject_rate < 0.20, f"真詞的 ToF 誤拒率高達 {word_false_reject_rate:.1%}"

    # 情境二：query 真的是靜止（貼近 reject 中心）-> ToF 大多數情況應該拒識
    rest_rejects = []
    for _ in range(n_trials):
        rest_query = _make_seq(reject_center, rng, noise=noise)
        tri = compute_tri_result(rest_query, templates_by_class, reject_templates, slices, cosine_dist)
        rest_rejects.append(tri.reject_tof)
    rest_reject_rate = np.mean(rest_rejects)
    assert rest_reject_rate > 0.80, f"靜止的 ToF 拒識率只有 {rest_reject_rate:.1%}"


def test_theta_reject_fused_linear_interpolation_and_endpoints():
    """CONTRACTS §4.3：theta_reject_fused(w) = w*theta_tof + (1-w)*theta_mel，
    w=1/w=0 精確退化成單模態閾值。"""
    tri = _tri([0.0, 1.0], [1.0, 0.0])
    tri.theta_reject_tof = 4.0
    tri.theta_reject_mel = 2.0

    assert tri.theta_reject_fused(1.0) == pytest.approx(4.0)
    assert tri.theta_reject_fused(0.0) == pytest.approx(2.0)
    assert tri.theta_reject_fused(0.5) == pytest.approx(3.0)


def test_theta_reject_fused_rejects_invalid_w():
    tri = _tri([0.0, 1.0], [1.0, 0.0])
    with pytest.raises(ValueError):
        tri.theta_reject_fused(1.5)


def test_reject_fused_without_raw_distances_raises_clear_error():
    """手動建構（沒有 d_tof_raw/d_mel_raw）的 TriResult 不支援 reject_fused，
    要明確報錯而不是靜默算出錯的答案。"""
    tri = _tri([0.0, 1.0], [1.0, 0.0])
    with pytest.raises(ValueError):
        tri.reject_fused(0.5)


def test_reject_fused_degenerates_exactly_to_reject_tof_and_reject_mel_at_endpoints():
    """調度員定義的核心性質：w=1 時 reject_fused 必須跟 reject_tof 完全一樣
    （不只是「差不多」），w=0 時跟 reject_mel 完全一樣——因為兩者理當是
    用同一組原始距離、同一個比較方式算出來的，不是碰巧一致。"""
    rng = np.random.default_rng(0)
    slices, templates_by_class, class_bases, reject_templates = _synthetic_templates(rng)

    for query_class in ("class_0", "class_2"):
        query = _make_seq(class_bases[query_class], rng)
        tri = compute_tri_result(query, templates_by_class, reject_templates, slices, cosine_dist)

        assert tri.reject_fused(1.0) == tri.reject_tof
        assert tri.reject_fused(0.0) == tri.reject_mel


def test_reject_fused_rejects_invalid_w():
    rng = np.random.default_rng(1)
    slices, templates_by_class, class_bases, reject_templates = _synthetic_templates(rng)
    query = _make_seq(class_bases["class_1"], rng)
    tri = compute_tri_result(query, templates_by_class, reject_templates, slices, cosine_dist)

    with pytest.raises(ValueError):
        tri.reject_fused(-0.1)
