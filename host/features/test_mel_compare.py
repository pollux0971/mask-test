import numpy as np
import pytest

from host.features.mel_compare import compare_log_mels, load_log_mel


def test_load_log_mel_npy(tmp_path):
    arr = np.random.default_rng(0).normal(size=(20, 40)).astype(np.float32)
    path = tmp_path / "mel.npy"
    np.save(path, arr)

    loaded = load_log_mel(path)

    np.testing.assert_allclose(loaded, arr)


def test_load_log_mel_csv(tmp_path):
    arr = np.random.default_rng(0).normal(size=(20, 40)).astype(np.float32)
    path = tmp_path / "mel.csv"
    np.savetxt(path, arr, delimiter=",", fmt="%.6f")

    loaded = load_log_mel(path)

    np.testing.assert_allclose(loaded, arr, atol=1e-5)


def test_load_log_mel_rejects_wrong_band_count(tmp_path):
    arr = np.zeros((10, 39), dtype=np.float32)
    path = tmp_path / "mel.npy"
    np.save(path, arr)

    with pytest.raises(ValueError):
        load_log_mel(path)


def test_load_log_mel_rejects_unknown_extension(tmp_path):
    path = tmp_path / "mel.txt"
    path.write_text("not a mel file")

    with pytest.raises(ValueError):
        load_log_mel(path)


def test_compare_log_mels_same_shape_includes_corr():
    a = np.random.default_rng(0).normal(size=(20, 40)).astype(np.float32)
    result = compare_log_mels(a, a.copy())

    assert result["pearson_corr"] == pytest.approx(1.0)
    assert result["dtw_distance"] == pytest.approx(0.0, abs=1e-4)


def test_compare_log_mels_different_shape_skips_corr():
    a = np.zeros((20, 40), dtype=np.float32)
    b = np.zeros((25, 40), dtype=np.float32)

    result = compare_log_mels(a, b)

    assert result["pearson_corr"] is None
    assert result["n_frames_a"] == 20
    assert result["n_frames_b"] == 25
