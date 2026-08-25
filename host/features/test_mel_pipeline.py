import numpy as np
import pytest
import soundfile as sf

from host.features.mel_pipeline import wav_to_log_mel, wav_to_log_mel_timed
from reference_mel import compute_log_mel  # ssi-backlog/tools on sys.path via mel_pipeline import


def _write_tone_wav(path, seconds=2.0, freq=440.0, sr=16000):
    t = np.arange(int(seconds * sr)) / sr
    y = 0.3 * np.sin(2 * np.pi * freq * t).astype(np.float32)
    sf.write(str(path), y, sr, subtype="PCM_16")
    return y


def test_wav_to_log_mel_matches_reference_directly(tmp_path):
    """驗收條件：跟 reference_mel.compute_log_mel 的輸出完全一致（同一份參數）。

    比對用的 y 要從寫出去的 wav 讀回來，而不是用寫檔前的 float 陣列——
    PCM_16 量化會引入極小的雜訊，兩邊如果不是讀同一份量化後資料，
    在 log10 的高頻無聲段會被放大成看似很大的差異。
    """
    wav_path = tmp_path / "tone.wav"
    _write_tone_wav(wav_path)
    y_roundtrip, sr = sf.read(str(wav_path), dtype="float32")

    log_mel = wav_to_log_mel(wav_path)
    expected = compute_log_mel(y_roundtrip, sr)

    assert log_mel.shape == expected.shape
    np.testing.assert_allclose(log_mel, expected, atol=1e-4)


def test_wav_to_log_mel_shape_is_frames_by_40(tmp_path):
    """驗收條件：錄 2 秒 -> 拿到 (M, 40) 的 log-Mel。"""
    wav_path = tmp_path / "tone.wav"
    _write_tone_wav(wav_path, seconds=2.0)

    log_mel = wav_to_log_mel(wav_path)

    assert log_mel.ndim == 2
    assert log_mel.shape[1] == 40
    assert log_mel.shape[0] > 0


def test_wav_to_log_mel_end_to_end_under_5_seconds(tmp_path):
    """驗收條件：端到端（解碼 + MFCC）< 5 秒（story 預期單純算圖只要 ~0.1s，
    5 秒的餘裕留給裝置端錄音 + 傳輸；這裡只驗證主機端算圖本身不是瓶頸）。"""
    wav_path = tmp_path / "tone.wav"
    _write_tone_wav(wav_path, seconds=2.0)

    log_mel, elapsed = wav_to_log_mel_timed(wav_path)

    assert log_mel.shape[1] == 40
    assert elapsed < 5.0


def test_wav_to_log_mel_rejects_wrong_sample_rate(tmp_path):
    wav_path = tmp_path / "wrong_rate.wav"
    y = np.zeros(8000, dtype=np.float32)
    sf.write(str(wav_path), y, 8000, subtype="PCM_16")

    with pytest.raises(ValueError):
        wav_to_log_mel(wav_path)
