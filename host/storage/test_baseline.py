import h5py
import numpy as np
import pytest

from host.storage.baseline import (
    SIGMA_INSTABILITY_THRESHOLD_MM,
    capture_baseline_trial,
    check_noise_floor_contamination,
    check_zone_quality,
    compute_noise_floor,
    compute_zone_stats,
    evaluate_baseline,
)
from host.storage.test_session_writer import _sample_meta

N_ZONES = 16
T = 900  # 30s @ 30Hz


def _stable_tof(distance_base=500.0, signal_base=100.0, noise=0.02, rng=None):
    """模擬「靜止不動」的 baseline：每個 zone 一個略帶量測雜訊、但幾乎
    固定的距離值（真實 baseline 應該長這樣）。"""
    rng = rng or np.random.default_rng(0)
    distance = distance_base + rng.normal(scale=noise, size=(T, N_ZONES))
    signal = np.full((T, N_ZONES), signal_base, dtype=np.float32)
    values = np.concatenate([distance, signal], axis=1).astype(np.float32)
    valid = np.ones((T, N_ZONES), dtype=bool)
    return values, valid


# ---------------------------------------------------------------------------
# compute_zone_stats / compute_noise_floor


def test_compute_zone_stats_ignores_nan_invalid_entries():
    values = np.full((10, 32), 100.0, dtype=np.float32)
    values[3, 5] = np.nan
    values[3, 5 + 16] = np.nan

    mu, sigma = compute_zone_stats(values)

    assert mu[5] == pytest.approx(100.0)  # 其他 9 個樣本都還在，NaN 被排除
    assert np.isfinite(mu).all()


def test_compute_zone_stats_all_invalid_zone_is_nan():
    values = np.full((10, 32), 100.0, dtype=np.float32)
    values[:, 7] = np.nan
    values[:, 7 + 16] = np.nan

    mu, sigma = compute_zone_stats(values)

    assert np.isnan(mu[7])
    assert np.isnan(sigma[7])


def test_compute_noise_floor_basic():
    rms = np.array([100.0, 110.0, 90.0, 100.0], dtype=np.float32)
    mu, sigma = compute_noise_floor(rms)
    assert mu == pytest.approx(100.0)
    assert sigma > 0


# ---------------------------------------------------------------------------
# check_zone_quality — 三種情況


def test_stable_zones_pass():
    sigma = np.full(N_ZONES, 0.02)
    valid_counts = np.full(N_ZONES, T)
    report = check_zone_quality(sigma, valid_counts)
    assert report.ok
    assert report.unstable_zones == []
    assert report.no_signal_zones == []


def test_unstable_zone_is_flagged_and_blocks():
    """驗收條件：刻意晃動能被偵測並要求重錄。"""
    sigma = np.full(N_ZONES, 0.02)
    sigma[4] = SIGMA_INSTABILITY_THRESHOLD_MM + 1.0  # 明顯超標
    valid_counts = np.full(N_ZONES, T)

    report = check_zone_quality(sigma, valid_counts)

    assert not report.ok
    assert report.unstable_zones == [4]


def test_no_signal_zone_blocks_even_if_sigma_looks_fine():
    sigma = np.full(N_ZONES, 0.02)
    sigma[9] = np.nan  # 整段無效 -> nanstd 給 NaN
    valid_counts = np.full(N_ZONES, T)
    valid_counts[9] = 0

    report = check_zone_quality(sigma, valid_counts)

    assert not report.ok
    assert report.no_signal_zones == [9]
    assert report.unstable_zones == []  # NaN 不該被誤判成 unstable


def test_near_zero_sigma_is_a_warning_not_a_block():
    """sigma 幾乎是 0（有樣本、但完全不變）：警告，但不強制重錄。"""
    sigma = np.full(N_ZONES, 0.02)
    sigma[2] = 0.0
    valid_counts = np.full(N_ZONES, T)

    report = check_zone_quality(sigma, valid_counts)

    assert report.ok
    assert report.suspect_zero_variance_zones == [2]


def test_valid_zone_ratio_reflects_no_signal_zones():
    sigma = np.full(N_ZONES, 0.02)
    valid_counts = np.full(N_ZONES, T)
    valid_counts[0] = 0
    valid_counts[1] = 0

    report = check_zone_quality(sigma, valid_counts)

    assert report.valid_zone_ratio == pytest.approx(1.0 - 2 / N_ZONES)


# ---------------------------------------------------------------------------
# evaluate_baseline — 整合 A/B 兩顆感測器 + 音訊底噪


def test_evaluate_baseline_stable_case_is_ok():
    rng = np.random.default_rng(1)
    tof_A, valid_A = _stable_tof(rng=rng)
    tof_B, valid_B = _stable_tof(rng=rng)
    mic_rms = rng.normal(loc=300, scale=10, size=930).astype(np.float32)

    outcome = evaluate_baseline(tof_A, valid_A, tof_B, valid_B, mic_rms)

    assert outcome.ok
    assert outcome.reason is None
    assert outcome.baseline_mu_A.shape == (32,)
    assert outcome.baseline_sigma_A.shape == (32,)
    assert outcome.noise_floor_mu == pytest.approx(300, abs=5)
    assert outcome.valid_zone_ratio == pytest.approx(1.0)


def test_evaluate_baseline_detects_shaking_on_either_sensor():
    rng = np.random.default_rng(2)
    tof_A, valid_A = _stable_tof(rng=rng)
    tof_B, valid_B = _stable_tof(rng=rng)
    # 模擬晃動：B 感測器某個 zone 距離大幅波動
    tof_B[:, 6] += rng.normal(scale=5.0, size=T)
    mic_rms = rng.normal(loc=300, scale=10, size=930).astype(np.float32)

    outcome = evaluate_baseline(tof_A, valid_A, tof_B, valid_B, mic_rms)

    assert not outcome.ok
    assert outcome.reason == "baseline unstable"
    assert 6 in outcome.quality["B"]["unstable_zones"]
    assert outcome.quality["A"]["ok"]


# ---------------------------------------------------------------------------
# capture_baseline_trial — 端到端寫檔


def test_capture_baseline_trial_writes_meta_and_trial_000(tmp_path):
    rng = np.random.default_rng(3)
    tof_A, valid_A = _stable_tof(rng=rng)
    tof_B, valid_B = _stable_tof(rng=rng)
    mic_rms = rng.normal(loc=300, scale=10, size=930).astype(np.float32)
    mic_peak = (mic_rms * 1.5).astype(np.int16)
    mic_t_us = np.arange(930, dtype=np.int64) * 32_258  # ~31.25Hz
    tof_t_us = np.arange(T, dtype=np.int64) * 33_333

    meta_base = _sample_meta()
    for k in ("baseline_mu_A", "baseline_sigma_A", "baseline_mu_B",
              "baseline_sigma_B", "noise_floor_mu", "noise_floor_sigma"):
        del meta_base[k]  # capture_baseline_trial 負責填這幾個

    path = tmp_path / "session.h5"
    outcome = capture_baseline_trial(
        path, meta_base,
        tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
        tof_valid_A=valid_A, tof_valid_B=valid_B,
        mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
        wear_id=1, mode="quiz",
    )

    assert outcome.ok
    with h5py.File(path, "r") as f:
        meta = f["meta"]
        np.testing.assert_allclose(meta.attrs["baseline_mu_A"], outcome.baseline_mu_A)
        np.testing.assert_allclose(meta.attrs["baseline_sigma_A"], outcome.baseline_sigma_A)
        assert meta.attrs["noise_floor_mu"] == pytest.approx(outcome.noise_floor_mu)

        trial = f["trial_000"]
        assert trial.attrs["label"] == "_baseline"
        assert trial.attrs["quality"] == "ok"
        assert trial["tof_A"].shape == (T, 32)


def test_capture_baseline_trial_does_not_create_file_when_unstable(tmp_path):
    """驗收條件的另一半：品質不過就要求重錄，不是靜默接受。"""
    rng = np.random.default_rng(4)
    tof_A, valid_A = _stable_tof(rng=rng)
    tof_B, valid_B = _stable_tof(rng=rng)
    tof_A[:, 0] += rng.normal(scale=5.0, size=T)  # 晃動
    mic_rms = rng.normal(loc=300, scale=10, size=930).astype(np.float32)
    mic_peak = (mic_rms * 1.5).astype(np.int16)
    mic_t_us = np.arange(930, dtype=np.int64) * 32_258
    tof_t_us = np.arange(T, dtype=np.int64) * 33_333

    meta_base = _sample_meta()
    for k in ("baseline_mu_A", "baseline_sigma_A", "baseline_mu_B",
              "baseline_sigma_B", "noise_floor_mu", "noise_floor_sigma"):
        del meta_base[k]

    path = tmp_path / "session.h5"
    outcome = capture_baseline_trial(
        path, meta_base,
        tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
        tof_valid_A=valid_A, tof_valid_B=valid_B,
        mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
        wear_id=1, mode="quiz",
    )

    assert not outcome.ok
    assert not path.exists()
