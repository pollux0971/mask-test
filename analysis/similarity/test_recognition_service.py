import time

import numpy as np
import pytest

from analysis.features.feature_assembly import FEATURE_DIM
from analysis.similarity.recognition_service import DEFAULT_DIST_METHOD, RecognitionService


def _random_direction(rng, n_dims, magnitude=10.0):
    v = rng.normal(size=n_dims)
    return v / np.linalg.norm(v) * magnitude


def _make_trial(rng, center, T=24, noise=0.15):
    return center[None, :] + noise * rng.normal(size=(T, center.shape[0]))


def _build_service(rng, n_classes=8, n_templates=9, n_reject=20, dist_method=DEFAULT_DIST_METHOD,
                    n_dims=FEATURE_DIM, T=24, **service_kwargs):
    tof_dim = round(n_dims * 64 / FEATURE_DIM)  # 維持跟真實 64/104 一樣的 tof:mel 比例
    slices = {"tof": slice(0, tof_dim), "mel": slice(tof_dim, n_dims)}
    centers = {f"w{i}": _random_direction(rng, n_dims) for i in range(n_classes)}
    templates_by_class = {
        label: [_make_trial(rng, center, T=T) for _ in range(n_templates)]
        for label, center in centers.items()
    }
    reject_center = _random_direction(rng, n_dims)
    reject_templates = [_make_trial(rng, reject_center, T=T) for _ in range(n_reject)]

    service = RecognitionService(
        templates_by_class, reject_templates, slices,
        subject="alice", wear_id=1, dist_method=dist_method,
        **service_kwargs,
    )
    return service, centers, reject_center


def test_constructor_rejects_unknown_dist_method():
    with pytest.raises(ValueError):
        RecognitionService({}, [], {"tof": slice(0, 1), "mel": slice(1, 2)}, dist_method="not_a_method")


def test_list_templates_reports_loaded_enrollment():
    """驗收條件相關：GET /templates 對應的邏輯要列出已載入的樣板組。"""
    rng = np.random.default_rng(0)
    service, centers, _ = _build_service(rng, n_classes=4, n_templates=5, n_reject=12)

    info = service.list_templates()

    assert info["subject"] == "alice"
    assert info["wear_id"] == 1
    assert info["dist_method"] == "cosine"
    assert info["classes"] == {label: 5 for label in centers}
    assert info["n_reject_templates"] == 12
    assert info["theta_reject_tof"] is not None
    assert info["theta_reject_mel"] is not None


def test_recognize_returns_correct_class_and_latency_breakdown():
    rng = np.random.default_rng(1)
    service, centers, _ = _build_service(rng, n_classes=6, n_templates=8)

    query = _make_trial(rng, centers["w3"])
    tri, latency_ms = service.recognize(query, feature_latency_ms=12.0)

    assert tri.top1(0.5) == "w3"
    assert set(latency_ms.keys()) == {"feature", "dist", "total"}
    assert latency_ms["feature"] == pytest.approx(12.0)
    assert latency_ms["total"] == pytest.approx(latency_ms["feature"] + latency_ms["dist"])
    assert latency_ms["dist"] >= 0.0


def test_recognize_end_to_end_latency_under_100ms_with_default_cosine():
    """驗收條件：端到端延遲 < 100 ms（不含錄音與傳輸）。

    用預設 cosine（D09 的預設理由見模組 docstring），8 類 x 9 樣板，
    量測純距離計算＋融合的耗時（`latency_ms["dist"]`），
    這是這個服務層自己能控制、能實測的部分。
    """
    rng = np.random.default_rng(2)
    service, centers, _ = _build_service(rng, n_classes=8, n_templates=9)

    query = _make_trial(rng, centers["w0"])
    tri, latency_ms = service.recognize(query)

    assert latency_ms["dist"] < 100.0, f"距離+融合耗時 {latency_ms['dist']:.2f} ms，超過 100 ms"


def test_hot_reload_swaps_templates_without_reconstruction():
    """驗收條件：樣板熱載入不中斷服務——同一個 service 物件，
    換樣板後立刻可以用新樣板辨識，不需要重新建構。"""
    rng = np.random.default_rng(3)
    service, centers_v1, _ = _build_service(rng, n_classes=3, n_templates=6)

    query_v1 = _make_trial(rng, centers_v1["w1"])
    assert service.recognize(query_v1)[0].top1(0.5) == "w1"

    # 熱載入一組全新的樣板（不同的類別中心），同一個 service 物件繼續用
    centers_v2 = {f"x{i}": _random_direction(rng, FEATURE_DIM) for i in range(3)}
    templates_v2 = {label: [_make_trial(rng, c) for _ in range(6)] for label, c in centers_v2.items()}
    reject_v2 = [_make_trial(rng, _random_direction(rng, FEATURE_DIM)) for _ in range(15)]
    service.load_enrollment(templates_v2, reject_v2, subject="bob", wear_id=2)

    assert service.list_templates()["subject"] == "bob"
    assert service.list_templates()["wear_id"] == 2
    query_v2 = _make_trial(rng, centers_v2["x2"])
    assert service.recognize(query_v2)[0].top1(0.5) == "x2"


def test_set_dist_method_recalibrates_thresholds():
    """換距離度量後，門檻必須用新的距離函式重新校準，
    不能沿用舊尺度的門檻（cosine 跟 dtw 的原始距離尺度不同）。"""
    rng = np.random.default_rng(4)
    service, _, _ = _build_service(rng, n_classes=3, n_templates=6, dist_method="cosine")
    theta_before = service.list_templates()["theta_reject_tof"]

    service.set_dist_method("dtw")
    theta_after = service.list_templates()["theta_reject_tof"]

    assert service.list_templates()["dist_method"] == "dtw"
    assert theta_before != theta_after


def test_reject_tof_still_works_at_w1_after_full_service_wiring():
    """調度員特別交代：Demo 第 4 步「純 ToF 念『四』→ 認不出來」要成立。
    統計方式驗證（跟 D06/D07 一樣），不是斷言單一樣本。

    用低維度、小 T（跟 D07 `test_reject_tof_still_works_after_wiring_into_d07`
    同樣的規模），不是完整的 104 維 x T=24——那個規模下 D06 發現的
    「LOO 校準系統性偏差」在高維度會被放大（樣板數需要到 50+ 才穩定，
    見完成回報的說明），這裡的目的是驗證「wiring 沒有破壞 D06/D07 的
    機制」，不是重新調校真實系統的 enrollment 樣板數。
    """
    rng = np.random.default_rng(5)
    service, centers, reject_center = _build_service(
        rng, n_classes=4, n_templates=30, n_reject=30, n_dims=12, T=3
    )

    word_rejects = []
    for _ in range(100):
        q = _make_trial(rng, centers["w2"], T=3)
        tri, _ = service.recognize(q)
        word_rejects.append(tri.reject_tof)
    assert np.mean(word_rejects) < 0.20

    rest_rejects = []
    for _ in range(100):
        q = _make_trial(rng, reject_center, T=3)
        tri, _ = service.recognize(q)
        rest_rejects.append(tri.reject_tof)
    assert np.mean(rest_rejects) > 0.80


def test_recognize_is_data_source_agnostic_for_replay():
    """驗收條件：可用回放資料測試——服務層不區分即時或回放來源，
    只要形狀對就能辨識，這裡直接用一個「假裝是回放重建出來的」陣列驗證。
    """
    rng = np.random.default_rng(6)
    service, centers, _ = _build_service(rng, n_classes=3, n_templates=5)

    live_like_query = _make_trial(rng, centers["w1"])
    replay_like_query = np.array(live_like_query, copy=True)  # 模擬從 B17 回放重建出的同形狀陣列

    tri_live, _ = service.recognize(live_like_query)
    tri_replay, _ = service.recognize(replay_like_query)

    assert tri_live.top1(0.5) == tri_replay.top1(0.5) == "w1"


def test_w_change_does_not_recompute_distances():
    """延續 D07 的性質：TriResult 拿到後，前端對任意 w 重算不需要
    再呼叫一次 recognize()。"""
    rng = np.random.default_rng(7)
    service, centers, _ = _build_service(rng, n_classes=4, n_templates=8)

    query = _make_trial(rng, centers["w0"])
    tri, _ = service.recognize(query)

    d_tof_before = tri.d_tof.copy()
    d_mel_before = tri.d_mel.copy()
    for w in np.linspace(0, 1, 11):
        tri.top1(float(w))
    np.testing.assert_array_equal(tri.d_tof, d_tof_before)
    np.testing.assert_array_equal(tri.d_mel, d_mel_before)


# ---------------------------------------------------------------------------
# 極值統計量稽核（見 reports/D_extremum_audit.md）：這個服務原本仍呼叫
# D06/D08 的單邊 LOO 校準——D09 完成時 D22 還不存在，但 D22 證明單邊 LOO
# 在真實規模下有結構性缺陷（誤拒率不隨樣板數改善），且已被定為系統預設。
# 這裡的測試釘死「預設值已經是 roc」，不是只測「roc 這個選項存在」。

def test_default_reject_calibration_method_is_roc():
    rng = np.random.default_rng(10)
    service, _, _ = _build_service(rng, n_classes=4, n_templates=20, n_reject=20)
    assert service.list_templates()["reject_calibration_method"] == "roc"


def test_loo_single_still_selectable_as_a_fallback():
    """D22 的原則：舊方法保留為對照，不刪除。"""
    rng = np.random.default_rng(11)
    service, _, _ = _build_service(
        rng, n_classes=4, n_templates=20, n_reject=20,
        reject_calibration_method="loo_single",
    )
    assert service.list_templates()["reject_calibration_method"] == "loo_single"


def test_constructor_rejects_unknown_reject_calibration_method():
    with pytest.raises(ValueError):
        RecognitionService(
            {}, [], {"tof": slice(0, 1), "mel": slice(1, 2)},
            reject_calibration_method="not_a_method",
        )


def test_roc_calibration_gives_far_lower_false_reject_than_loo_single_at_real_scale():
    """在真實規模（104 維、T=24、8 類）下，roc 校準的誤拒率應該遠低於
    loo_single——這就是這次稽核發現「服務仍用舊方法」而動手修的理由，
    不是空口說改了比較好。"""
    n_trials = 60

    def false_reject_rate(method):
        rng_local = np.random.default_rng(1)
        service, centers, _ = _build_service(
            rng_local, n_classes=8, n_templates=10, n_reject=10,
            reject_calibration_method=method,
        )
        rejects = []
        for _ in range(n_trials):
            q = _make_trial(rng_local, centers["w2"])
            tri, _ = service.recognize(q)
            rejects.append(tri.reject_tof)
        return np.mean(rejects)

    rate_roc = false_reject_rate("roc")
    rate_loo = false_reject_rate("loo_single")

    assert rate_roc < rate_loo - 0.10, (
        f"roc 誤拒率 {rate_roc:.1%} 應該明顯低於 loo_single 的 {rate_loo:.1%}"
    )
