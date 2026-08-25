import numpy as np
import pytest

from host.clock.align import (
    ClockAligner,
    ClockSample,
    fit_clock_alignment,
)


RESIDUAL_ANOMALY_US_FOR_TEST = 20_000.0


def _make_samples(n_buckets, true_slope=1.0, true_offset=1_000_000.0,
                   bucket_seconds=1.0, extra_delay_us=0.0, rng=None):
    """模擬 n_buckets 秒的資料，每個 bucket 裡放好幾個樣本、延遲隨機，
    但一定有一個延遲恰好是 0（= 這個 bucket 的「乾淨」樣本），
    另外可疊加一個固定的額外排隊延遲來模擬「排隊延遲不是常數」。"""
    rng = rng or np.random.default_rng(0)
    samples = []
    for b in range(n_buckets):
        device_us = int(b * bucket_seconds * 1e6 + 500_000)
        clean_host_us = int(true_slope * device_us + true_offset)
        samples.append(ClockSample(device_us=device_us, host_us=clean_host_us))
        for _ in range(5):
            noisy_delay = int(rng.uniform(0, 5000)) + int(extra_delay_us)
            samples.append(ClockSample(device_us=device_us, host_us=clean_host_us + noisy_delay))
    rng.shuffle(samples)
    return samples


def test_fit_recovers_slope_and_offset_from_noisy_samples():
    samples = _make_samples(n_buckets=20, true_slope=1.00005, true_offset=2_000_000.0)

    alignment = fit_clock_alignment(samples)

    assert alignment.slope == pytest.approx(1.00005, abs=1e-6)
    assert alignment.offset_us == pytest.approx(2_000_000.0, abs=50.0)
    assert alignment.residual_p95_us < 1000.0
    assert not alignment.anomaly


def test_fit_flags_anomaly_when_slope_out_of_tolerance():
    """驗收條件：斜率落在 1 ± 200 ppm，超出範圍代表擬合有問題。"""
    samples = _make_samples(n_buckets=20, true_slope=1.001)  # 1000 ppm，遠超門檻

    alignment = fit_clock_alignment(samples)

    assert alignment.anomaly
    assert "slope" in alignment.anomaly_reason


def test_fit_flags_anomaly_on_large_residual_without_crashing():
    """模擬 clock-jump：把資料切成兩段，中間插入一次性大位移的 offset，
    對「同一條線」的假設來說殘差會爆大，但不應該丟例外，只需要標記異常。"""
    rng = np.random.default_rng(1)
    first_half = _make_samples(n_buckets=10, true_offset=1_000_000.0, rng=rng)
    second_half = _make_samples(n_buckets=10, true_offset=1_000_000.0 + 300_000.0, rng=rng)
    # 讓第二段的 device_us 接在第一段之後，模擬真實時間軸
    shifted_second_half = [
        ClockSample(device_us=s.device_us + 10_000_000, host_us=s.host_us + 10_000_000)
        for s in second_half
    ]
    samples = first_half + shifted_second_half

    alignment = fit_clock_alignment(samples)  # 不應該 raise

    assert alignment.anomaly
    assert alignment.residual_p95_us > RESIDUAL_ANOMALY_US_FOR_TEST


def test_fit_requires_at_least_two_buckets():
    samples = [ClockSample(device_us=0, host_us=1_000_000)]
    with pytest.raises(ValueError):
        fit_clock_alignment(samples)


def test_min_delay_filtering_ignores_high_delay_outliers():
    """核心假設：只要每個 bucket 裡有至少一個接近零延遲的樣本，
    大量高延遲的雜訊樣本不該把擬合結果拖走（OLS 會被拖走，這個不該）。"""
    rng = np.random.default_rng(2)
    samples = []
    for b in range(15):
        device_us = int(b * 1e6 + 500_000)
        clean_host_us = device_us + 1_000_000  # slope=1, offset=1_000_000
        samples.append(ClockSample(device_us=device_us, host_us=clean_host_us))
        # 每個 bucket 塞 20 個延遲很大的雜訊樣本（100ms~400ms 排隊延遲）
        for _ in range(20):
            samples.append(ClockSample(
                device_us=device_us,
                host_us=clean_host_us + int(rng.uniform(100_000, 400_000)),
            ))

    alignment = fit_clock_alignment(samples)

    assert alignment.slope == pytest.approx(1.0, abs=1e-6)
    assert alignment.offset_us == pytest.approx(1_000_000.0, abs=10.0)
    assert not alignment.anomaly


def test_clock_aligner_incremental_matches_batch_fit():
    samples = _make_samples(n_buckets=15, true_slope=0.99998, true_offset=500_000.0)

    aligner = ClockAligner()
    for s in samples:
        aligner.add_sample(s.device_us, s.host_us)

    incremental = aligner.fit()
    batch = fit_clock_alignment(samples)

    assert incremental.slope == pytest.approx(batch.slope)
    assert incremental.offset_us == pytest.approx(batch.offset_us)
    assert aligner.n_buckets == batch.n_samples_used


def test_clock_aligner_freeze_matches_t02_schema_keys():
    aligner = ClockAligner()
    for s in _make_samples(n_buckets=10):
        aligner.add_sample(s.device_us, s.host_us)

    meta = aligner.freeze()

    assert set(meta.keys()) == {"clock_slope", "clock_offset", "clock_residual_p95"}
