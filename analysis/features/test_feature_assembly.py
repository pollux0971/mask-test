import numpy as np
import pytest

from analysis.features.feature_assembly import (
    DEFAULT_T_FIXED,
    FEATURE_DIM,
    MEL_BANDS,
    assemble_feature_seq,
    fit_pca,
    load_pca,
    project_pca,
    resample_fixed_length,
    save_pca,
)
from analysis.features.tof_features import TOF_DIM


def _synthetic_inputs(T=40, seed=0):
    rng = np.random.default_rng(seed)
    tof_a = rng.normal(size=(T, TOF_DIM))
    tof_b = rng.normal(size=(T, TOF_DIM))
    mel = rng.normal(size=(T, MEL_BANDS))
    t_us = np.arange(T, dtype=np.int64) * 16000  # 假設已對齊、等間隔
    return tof_a, tof_b, mel, t_us


def test_concatenation_order_matches_contracts_3_3():
    """驗收條件：串接順序符合 T03。逐段斷言索引範圍，不是只斷言總長度。"""
    tof_a, tof_b, mel, t_us = _synthetic_inputs()
    seq = assemble_feature_seq(tof_a, tof_b, mel, t_us)

    assert seq.data_raw.shape == (40, FEATURE_DIM)
    assert FEATURE_DIM == 2 * TOF_DIM + MEL_BANDS == 104

    np.testing.assert_array_equal(seq.data_raw[:, 0:32], tof_a)
    np.testing.assert_array_equal(seq.data_raw[:, 32:64], tof_b)
    np.testing.assert_array_equal(seq.data_raw[:, 64:104], mel)


def test_slices_correspond_to_each_modality():
    """驗收條件：slices 正確對應各模態。"""
    tof_a, tof_b, mel, t_us = _synthetic_inputs()
    seq = assemble_feature_seq(tof_a, tof_b, mel, t_us)

    assert seq.slices["tof"] == slice(0, 64)
    assert seq.slices["mel"] == slice(64, 104)
    np.testing.assert_array_equal(seq.data_raw[:, seq.slices["tof"]],
                                   np.concatenate([tof_a, tof_b], axis=1))
    np.testing.assert_array_equal(seq.data_raw[:, seq.slices["mel"]], mel)


def test_mismatched_frame_counts_raise_instead_of_guessing():
    tof_a, tof_b, mel, t_us = _synthetic_inputs(T=40)
    mel_wrong_len = mel[:-1]  # 少一幀，模擬對齊沒做好
    with pytest.raises(ValueError):
        assemble_feature_seq(tof_a, tof_b, mel_wrong_len, t_us)


def test_wrong_channel_width_raises():
    tof_a, tof_b, mel, t_us = _synthetic_inputs()
    with pytest.raises(ValueError):
        assemble_feature_seq(tof_a[:, :-1], tof_b, mel, t_us)


def test_raw_length_output_preserved_unchanged():
    """D05 用的原始長度版：資料與時間戳都不能被重採樣動到。"""
    tof_a, tof_b, mel, t_us = _synthetic_inputs(T=17)
    seq = assemble_feature_seq(tof_a, tof_b, mel, t_us)

    assert seq.data_raw.shape[0] == 17
    np.testing.assert_array_equal(seq.t_us_raw, t_us)


def test_fixed_length_output_is_t24_by_default():
    """驗收條件：重採樣後 T=24。"""
    tof_a, tof_b, mel, t_us = _synthetic_inputs(T=17)
    seq = assemble_feature_seq(tof_a, tof_b, mel, t_us)

    assert seq.data.shape == (DEFAULT_T_FIXED, FEATURE_DIM)
    assert seq.t_us.shape == (DEFAULT_T_FIXED,)


def test_resample_preserves_time_series_shape():
    """驗收條件的「時序形狀保持」數值化版本：一個平滑波形重採樣後，
    在對應的相對時間點上應該跟原始連續函數的值高度吻合，而不只是
    「看起來差不多」。"""
    T_raw = 50
    t_frac = np.linspace(0, 1, T_raw)
    waveform = np.sin(2 * np.pi * 2 * t_frac)  # 兩個週期的正弦波
    x = np.tile(waveform[:, None], (1, 3))  # 3 個通道都放同一個波形
    t_us = (t_frac * 1_000_000).astype(np.int64)

    x_resampled, t_us_resampled = resample_fixed_length(x, t_us, t_fixed=24)

    dst_frac = np.linspace(0, 1, 24)
    expected = np.sin(2 * np.pi * 2 * dst_frac)
    np.testing.assert_allclose(x_resampled[:, 0], expected, atol=0.05)
    np.testing.assert_allclose(t_us_resampled, dst_frac * 1_000_000, atol=1.0)


def test_resample_known_linear_interpolation_values():
    """人工小案例，手算內插值。"""
    x = np.array([[0.0], [10.0], [20.0]])  # T=3
    t_us = np.array([0, 100, 200])

    x_out, t_out = resample_fixed_length(x, t_us, t_fixed=5)

    np.testing.assert_allclose(x_out[:, 0], [0.0, 5.0, 10.0, 15.0, 20.0])
    np.testing.assert_allclose(t_out, [0.0, 50.0, 100.0, 150.0, 200.0])


def test_resample_rejects_too_short_input():
    with pytest.raises(ValueError):
        resample_fixed_length(np.zeros((1, 4)), np.array([0]), t_fixed=24)


def test_pca_fit_project_recovers_known_2d_structure():
    """驗收條件的前置：PCA 擬合與投影要正確——用已知的低秩結構驗證。"""
    rng = np.random.default_rng(42)
    n_frames = 500
    latent = rng.normal(size=(n_frames, 2))
    basis = rng.normal(size=(2, FEATURE_DIM))
    frames = latent @ basis + 0.01 * rng.normal(size=(n_frames, FEATURE_DIM))

    pca = fit_pca(frames, n_components=2)
    projected = project_pca(pca, frames)

    assert projected.shape == (n_frames, 2)
    assert sum(pca.explained_variance_ratio_) > 0.95  # 兩個主成分幾乎解釋全部變異


def test_pca_model_can_be_saved_and_reloaded(tmp_path):
    """驗收條件：PCA 模型可存檔重載。"""
    rng = np.random.default_rng(1)
    frames = rng.normal(size=(200, FEATURE_DIM))
    pca = fit_pca(frames, n_components=2)

    path = tmp_path / "pca_model.joblib"
    save_pca(pca, path)
    assert path.exists()

    loaded = load_pca(path)
    query = rng.normal(size=(10, FEATURE_DIM))
    np.testing.assert_allclose(project_pca(pca, query), project_pca(loaded, query))
