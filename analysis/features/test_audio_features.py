import sys
from pathlib import Path

import numpy as np
import pytest

from analysis.features.audio_features import check_device_consistency, mel_features
from host.features.dtw_compare import dtw_distance

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from reference_mel import to_device_int16  # noqa: E402


def _synthetic_mel(n_frames=40, n_mels=40, seed=0, offset=0.0):
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 1, n_frames)
    band_idx = np.arange(1, n_mels + 1)
    base = np.sin(2 * np.pi * np.outer(t, band_idx)) + 0.05 * rng.standard_normal((n_frames, n_mels))
    return base + offset


def test_cmn_band_mean_near_zero():
    """驗收條件：CMN 後各 band 的均值接近 0。"""
    mel = _synthetic_mel(seed=1, offset=7.3)  # 隨便一個非零通道偏移
    out = mel_features(mel)
    np.testing.assert_allclose(out.mean(axis=0), 0.0, atol=1e-10)


def test_cmn_removes_constant_volume_offset_exactly():
    mel = _synthetic_mel(seed=2)
    shifted = mel + 5.0  # 模擬「同一段話，音量/接觸鬆緊不同」的整體位移
    np.testing.assert_allclose(mel_features(mel), mel_features(shifted), atol=1e-10)


def test_cvn_divides_by_std_with_floor_guard():
    mel = np.zeros((10, 4))
    mel[:, 0] = 1.0  # 完全不變的 band -> std=0，要靠 floor 保護不炸掉
    mel[:, 1] = np.linspace(0, 9, 10)

    out = mel_features(mel, cvn=True)

    assert np.all(np.isfinite(out))
    np.testing.assert_allclose(out[:, 0], 0.0)  # CMN 後本來就是 0，除以 floor 還是 0


def test_vad_crop_applies_before_cmn():
    mel = _synthetic_mel(seed=3)
    cropped_then_cmn = mel_features(mel, vad_start=5, vad_end=15)
    manual = mel[5:15]
    manual = manual - manual.mean(axis=0, keepdims=True)
    np.testing.assert_allclose(cropped_then_cmn, manual)


def test_empty_vad_range_raises():
    mel = _synthetic_mel(seed=4)
    with pytest.raises(ValueError):
        mel_features(mel, vad_start=10, vad_end=10)


def test_cmn_shrinks_dtw_distance_for_same_word_different_volume():
    """驗收條件：同一個詞不同音量錄兩次，CMN 後的 DTW 距離明顯縮小。"""
    quiet = _synthetic_mel(seed=5, offset=0.0)
    loud = _synthetic_mel(seed=5, offset=6.0)  # 同一個詞，音量造成的整體位移

    dist_before = dtw_distance(quiet, loud)
    dist_after = dtw_distance(mel_features(quiet), mel_features(loud))

    assert dist_after < dist_before * 0.1  # 明顯縮小，不是微幅改善


def test_check_device_consistency_with_quantized_stand_in():
    """A12 尚未完成，用 reference_mel.to_device_int16() 量化再還原的資料
    當「裝置端輸出」的替身，驗證一致性檢查工具本身邏輯正確。"""
    host_mel = _synthetic_mel(seed=6).astype(np.float32)
    device_like = (to_device_int16(host_mel).astype(np.float64) / 100.0)

    corr, passed = check_device_consistency(host_mel, device_like, threshold=0.95)

    assert passed
    assert corr > 0.95


def test_check_device_consistency_fails_on_unrelated_signals():
    rng = np.random.default_rng(7)
    host_mel = _synthetic_mel(seed=8)
    unrelated = rng.standard_normal(host_mel.shape)

    corr, passed = check_device_consistency(host_mel, unrelated, threshold=0.95)

    assert not passed
