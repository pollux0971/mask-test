import numpy as np
import pytest

from analysis.features.tof_features import (
    N_ZONES,
    TOF_DIM,
    active_zone_indices,
    active_zone_mask,
    tof_features,
)


def _baseline_stats(rng, n_zones=N_ZONES, mu_scale=100.0, sigma_scale=10.0):
    mu = rng.uniform(50, 500, size=2 * n_zones)
    sigma = rng.uniform(1, sigma_scale, size=2 * n_zones)
    return mu, sigma


def test_zscore_std_near_one_during_baseline():
    """驗收條件：z-score 後各 zone 的標準差接近 1（baseline 期間）。"""
    rng = np.random.default_rng(0)
    mu = rng.uniform(50, 500, size=TOF_DIM)
    sigma = rng.uniform(2, 20, size=TOF_DIM)

    T = 5000
    tof = rng.normal(loc=mu, scale=sigma, size=(T, TOF_DIM))
    valid = np.ones((T, N_ZONES), dtype=bool)

    z = tof_features(tof, valid, mu, sigma)

    assert z.shape == (T, TOF_DIM)
    np.testing.assert_allclose(z.std(axis=0), 1.0, atol=0.05)
    np.testing.assert_allclose(z.mean(axis=0), 0.0, atol=0.05)


def test_invalid_zone_produces_no_spurious_signal():
    """驗收條件：無效 zone 不產生假訊號（人工注入無效資料驗證）。

    把一個明顯偏離 baseline 的假資料標成無效，z 結果該通道必須是 0，
    而不是漏網把偏離值算進去。
    """
    T = 3
    mu = np.zeros(TOF_DIM)
    sigma = np.ones(TOF_DIM)

    tof = np.zeros((T, TOF_DIM))
    tof[:, 3] = 9999.0   # 距離通道 zone 3：離譜偏離值
    tof[:, 19] = -9999.0  # signal 通道 zone 3 (3+16)：同一 zone 也離譜偏離

    valid = np.ones((T, N_ZONES), dtype=bool)
    valid[:, 3] = False  # zone 3 標記無效

    z = tof_features(tof, valid, mu, sigma)

    assert np.all(z[:, 3] == 0.0)
    assert np.all(z[:, 19] == 0.0)
    # 其他 zone 沒被誤傷
    assert np.all(z[:, 0] == 0.0)  # mu=0, tof=0 -> z=0 本來就該是 0（非因遮罩）


def test_invalid_zone_mask_applies_to_both_distance_and_signal_channel():
    """valid (T,16) 要同時蓋住距離通道 (0-15) 與 signal 通道 (16-31)。"""
    T = 1
    mu = np.zeros(TOF_DIM)
    sigma = np.ones(TOF_DIM)
    tof = np.full((T, TOF_DIM), 123.0)
    valid = np.zeros((T, N_ZONES), dtype=bool)
    valid[0, 5] = True  # 只有 zone 5 有效

    z = tof_features(tof, valid, mu, sigma)

    assert z[0, 5] == 123.0        # zone 5 距離通道：有效，保留 z 值
    assert z[0, 5 + N_ZONES] == 123.0  # zone 5 signal 通道：同樣有效
    other_zones = [i for i in range(N_ZONES) if i != 5]
    for i in other_zones:
        assert z[0, i] == 0.0
        assert z[0, i + N_ZONES] == 0.0


def test_active_zone_indices_selects_correct_zones():
    """驗收條件：活躍 zone 索引正確輸出。"""
    snr = np.array([0.5, 3.0, 1.9, 2.1, 10.0] + [0.0] * (N_ZONES - 5))
    idx = active_zone_indices(snr, threshold=2.0)
    np.testing.assert_array_equal(idx, np.array([1, 3, 4]))

    mask = active_zone_mask(snr, threshold=2.0)
    assert mask.sum() == 3
    assert mask[1] and mask[3] and mask[4]
    assert not mask[0] and not mask[2]


def test_tof_features_active_zones_filters_both_channel_halves():
    T = 2
    mu = np.zeros(TOF_DIM)
    sigma = np.ones(TOF_DIM)
    tof = np.tile(np.arange(TOF_DIM, dtype=float), (T, 1))
    valid = np.ones((T, N_ZONES), dtype=bool)

    active = np.array([2, 7])
    z = tof_features(tof, valid, mu, sigma, active_zones=active)

    assert z.shape == (T, 4)
    # 通道順序：先所有選中 zone 的距離通道，再所有選中 zone 的 signal 通道
    np.testing.assert_array_equal(z[0], np.array([2.0, 7.0, 18.0, 23.0]))


def test_sigma_near_zero_does_not_produce_nan_or_inf():
    """驗收條件：σ ≈ 0 的 zone 不造成 NaN 或 inf。"""
    T = 4
    mu = np.zeros(TOF_DIM)
    sigma = np.zeros(TOF_DIM)  # 極端情況：baseline 完全穩定
    tof = np.ones((T, TOF_DIM)) * 5.0
    valid = np.ones((T, N_ZONES), dtype=bool)

    z = tof_features(tof, valid, mu, sigma)

    assert np.all(np.isfinite(z))
    # sigma 被 floor 到 1e-3，(5-0)/1e-3 = 5000
    np.testing.assert_allclose(z, 5000.0)


def test_active_zone_mask_rejects_wrong_shape():
    with pytest.raises(ValueError):
        active_zone_mask(np.zeros(N_ZONES - 1))


def test_tof_features_rejects_wrong_shape():
    mu = np.zeros(TOF_DIM)
    sigma = np.ones(TOF_DIM)
    valid = np.ones((1, N_ZONES), dtype=bool)
    with pytest.raises(ValueError):
        tof_features(np.zeros((1, TOF_DIM - 1)), valid, mu, sigma)
