import time

import numpy as np
import pytest

from analysis.features.feature_assembly import FEATURE_DIM
from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.enrollment import (
    BAD_CONTRIBUTION_THRESHOLD,
    calibrate_reject_threshold,
    calibrate_tri_reject_thresholds,
    exclude_templates,
    filter_enrollment_trials,
    flag_bad_templates,
    load_templates,
    loocv_accuracy,
    replace_template,
    save_templates,
    template_contributions,
    template_path,
)


def _random_direction(rng, n_dims, magnitude=10.0):
    v = rng.normal(size=n_dims)
    return v / np.linalg.norm(v) * magnitude


def _make_trial(rng, center, T=3, noise=0.15):
    return center[None, :] + noise * rng.normal(size=(T, center.shape[0]))


def test_filter_enrollment_trials_excludes_rejected_keeps_low():
    trials = [
        {"quality": "ok", "id": 1},
        {"quality": "low", "id": 2},
        {"quality": "rejected", "id": 3},
        {"quality": "ok", "id": 4},
    ]
    kept, counts = filter_enrollment_trials(trials)

    assert [t["id"] for t in kept] == [1, 2, 4]
    assert counts == {"ok": 2, "low": 1, "rejected": 1}


def test_save_and_load_templates_round_trip(tmp_path):
    templates_by_class = {
        "ba": [np.full((3, 4), 1.0), np.full((3, 4), 1.1)],
        "wu": [np.full((3, 4), 2.0)],
    }
    path = template_path(tmp_path, subject="alice", wear_id=1)
    save_templates(templates_by_class, path, subject="alice", wear_id=1)

    assert path.exists()
    loaded, meta, warning = load_templates(path, expected_wear_id=1)

    assert meta == {"subject": "alice", "wear_id": 1}
    assert warning is None
    np.testing.assert_array_equal(loaded["ba"][0], templates_by_class["ba"][0])
    np.testing.assert_array_equal(loaded["wu"][0], templates_by_class["wu"][0])


def test_load_templates_warns_on_cross_wear_id(tmp_path):
    """驗收條件：跨 wear_id 載入時有警告。"""
    templates_by_class = {"ba": [np.zeros((3, 4))]}
    path = template_path(tmp_path, subject="alice", wear_id=1)
    save_templates(templates_by_class, path, subject="alice", wear_id=1)

    _, meta, warning = load_templates(path, expected_wear_id=2)

    assert meta["wear_id"] == 1
    assert warning is not None
    assert "非同次戴上" in warning


def test_load_templates_no_warning_when_wear_id_matches_or_unspecified(tmp_path):
    templates_by_class = {"ba": [np.zeros((3, 4))]}
    path = template_path(tmp_path, subject="alice", wear_id=1)
    save_templates(templates_by_class, path, subject="alice", wear_id=1)

    _, _, warning_match = load_templates(path, expected_wear_id=1)
    _, _, warning_unspecified = load_templates(path)

    assert warning_match is None
    assert warning_unspecified is None


def test_loocv_accuracy_well_separated_classes_is_perfect():
    rng = np.random.default_rng(0)
    centers = {f"w{i}": _random_direction(rng, 16) for i in range(4)}
    templates_by_class = {
        label: [_make_trial(rng, center) for _ in range(6)]
        for label, center in centers.items()
    }

    accuracy, n_evaluated, skipped, details = loocv_accuracy(templates_by_class, cosine_dist)

    assert accuracy == pytest.approx(1.0)
    assert n_evaluated == 24
    assert skipped == []
    assert all(d["correct"] for d in details)


def test_loocv_accuracy_skips_class_with_single_template():
    rng = np.random.default_rng(1)
    centers = {f"w{i}": _random_direction(rng, 16) for i in range(3)}
    templates_by_class = {label: [_make_trial(rng, center)] for label, center in centers.items()}
    # w0 只有 1 筆樣板，leave-one-out 會讓它自己那一類變空——應該跳過不是報錯
    templates_by_class["w1"] = [_make_trial(rng, centers["w1"]) for _ in range(3)]

    accuracy, n_evaluated, skipped, details = loocv_accuracy(templates_by_class, cosine_dist)

    assert {"label": "w0", "index": 0} in skipped
    assert n_evaluated == 3  # 只有 w1 的 3 筆真的被評估
    assert np.isfinite(accuracy)


def test_template_contributions_identifies_deliberately_bad_template():
    """驗收條件：逐樣板貢獻度正確計算——人工放一筆離群樣板，
    貢獻度應該是正的（移除它準確率變好或不變）。"""
    rng = np.random.default_rng(2)
    centers = {f"w{i}": _random_direction(rng, 16) for i in range(4)}
    templates_by_class = {
        label: [_make_trial(rng, center, noise=0.1) for _ in range(6)]
        for label, center in centers.items()
    }
    # 把 w0 的第一筆樣板換成明顯偏向 w1 中心的離群值（模擬錄壞的樣板）
    templates_by_class["w0"][0] = _make_trial(rng, centers["w1"], noise=0.1)

    acc_full, contributions = template_contributions(templates_by_class, cosine_dist)

    assert contributions["w0"][0] > BAD_CONTRIBUTION_THRESHOLD
    # 其餘正常樣板的貢獻度不應該是正的（不是壞樣板）
    assert all(c <= BAD_CONTRIBUTION_THRESHOLD for c in contributions["w0"][1:])


def test_flag_and_exclude_bad_templates():
    contributions = {"w0": [0.1, -0.05, 0.0], "w1": [-0.02, 0.2]}
    bad = flag_bad_templates(contributions)
    assert bad == {"w0": [0], "w1": [1]}

    templates_by_class = {"w0": ["t0", "t1", "t2"], "w1": ["u0", "u1"]}
    remaining = exclude_templates(templates_by_class, bad)
    assert remaining == {"w0": ["t1", "t2"], "w1": ["u0"]}


def test_replace_template_does_not_mutate_original():
    templates_by_class = {"w0": [np.zeros(4), np.ones(4)]}
    new_template = np.full(4, 9.0)

    updated = replace_template(templates_by_class, "w0", 1, new_template)

    np.testing.assert_array_equal(updated["w0"][1], new_template)
    np.testing.assert_array_equal(templates_by_class["w0"][1], np.ones(4))  # 原資料沒被動到


def test_calibrate_reject_threshold_warns_on_imbalance():
    rng = np.random.default_rng(3)
    templates_by_class = {
        f"w{i}": [np.full(8, i * 10.0) + rng.normal(0, 0.1, size=8) for _ in range(10)]
        for i in range(4)
    }

    def scalar_dist(a, b):
        return float(np.abs(a - b).mean())

    reject_templates_many = [np.full(8, 100.0) + rng.normal(0, 0.1, size=8) for _ in range(30)]
    result_many = calibrate_reject_threshold(templates_by_class, reject_templates_many, scalar_dist)
    assert result_many["warning"] is not None
    assert "過緊" in result_many["warning"]

    reject_templates_few = [np.full(8, 100.0) + rng.normal(0, 0.1, size=8) for _ in range(3)]
    result_few = calibrate_reject_threshold(templates_by_class, reject_templates_few, scalar_dist)
    assert result_few["warning"] is not None
    assert "過鬆" in result_few["warning"]

    reject_templates_balanced = [np.full(8, 100.0) + rng.normal(0, 0.1, size=8) for _ in range(10)]
    result_balanced = calibrate_reject_threshold(templates_by_class, reject_templates_balanced, scalar_dist)
    assert result_balanced["warning"] is None
    assert result_balanced["percentile_used"] == 95.0


def test_calibrate_tri_reject_thresholds_are_independent():
    """調度員特別交代：theta_reject_tof / theta_reject_mel 要分別校準。"""
    rng = np.random.default_rng(4)
    slices = {"tof": slice(0, FEATURE_DIM - 40), "mel": slice(FEATURE_DIM - 40, FEATURE_DIM)}
    centers = {f"w{i}": _random_direction(rng, FEATURE_DIM) for i in range(3)}
    templates_by_class = {
        label: [_make_trial(rng, center) for _ in range(8)]
        for label, center in centers.items()
    }
    reject_templates = [_make_trial(rng, _random_direction(rng, FEATURE_DIM))[0] for _ in range(8)]
    reject_templates = [t[None, :] for t in reject_templates]

    result = calibrate_tri_reject_thresholds(
        templates_by_class, reject_templates, slices, cosine_dist
    )

    assert "tof" in result and "mel" in result
    # 兩個模態各自算出來的門檻不應該剛好完全一樣（不同切片、不同資料）
    assert result["tof"]["theta"] != result["mel"]["theta"]


def test_loocv_accuracy_72_templates_under_10_seconds():
    """驗收條件：LOOCV 在 72 筆樣板上 < 10 秒完成。"""
    rng = np.random.default_rng(5)
    n_classes = 8
    n_per_class = 9  # 8 * 9 = 72
    centers = {f"w{i}": _random_direction(rng, FEATURE_DIM) for i in range(n_classes)}
    templates_by_class = {
        label: [_make_trial(rng, center, T=24) for _ in range(n_per_class)]
        for label, center in centers.items()
    }

    t0 = time.perf_counter()
    accuracy, n_evaluated, skipped, _ = loocv_accuracy(templates_by_class, cosine_dist)
    elapsed = time.perf_counter() - t0

    assert n_evaluated == 72
    assert elapsed < 10.0, f"72 筆樣板 LOOCV 耗時 {elapsed:.2f} 秒，超過 10 秒門檻"
