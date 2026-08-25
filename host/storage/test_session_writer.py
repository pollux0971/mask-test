import numpy as np
import pytest
import h5py

from host.storage.session_writer import (
    REQUIRED_META_KEYS,
    REQUIRED_TRIAL_ATTRS,
    SessionWriter,
)


def _sample_meta(**overrides):
    meta = {
        "schema_version": 1,
        "subject": "s01",
        "session_date": "2026-08-26",
        "wear_id": 3,
        "mode": "quiz",
        "distance_mm": 30.0,
        "angle_deg": 0.0,
        "ambient": "quiet room",
        "notes": "",
        "fw_sha": "a1b2c3d",
        "proto_version": 2,
        "tof_dim": 16,
        "clock_slope": 1.0000234,
        "clock_offset": 1756000000.0,
        "clock_residual_p95": 0.0031,
        "baseline_mu_A": np.zeros(32, dtype=np.float32),
        "baseline_sigma_A": np.ones(32, dtype=np.float32),
        "baseline_mu_B": np.zeros(32, dtype=np.float32),
        "baseline_sigma_B": np.ones(32, dtype=np.float32),
        "noise_floor_mu": 0.0,
        "noise_floor_sigma": 1.0,
    }
    meta.update(overrides)
    return meta


def _sample_trial_kwargs(T=60, M=80, include_mel=True, include_audio=True, rng=None):
    rng = rng or np.random.default_rng(0)
    kwargs = dict(
        label="五", tof_A=rng.uniform(0, 4000, size=(T, 32)).astype(np.float32),
        tof_B=rng.uniform(0, 4000, size=(T, 32)).astype(np.float32),
        tof_t_us=np.arange(T, dtype=np.int64) * 33_333,
        tof_valid_A=np.ones((T, 16), dtype=bool),
        tof_valid_B=np.ones((T, 16), dtype=bool),
        mic_rms=rng.uniform(0, 32767, size=M).astype(np.float32),
        mic_peak=rng.integers(0, 32767, size=M).astype(np.int16),
        mic_t_us=np.arange(M, dtype=np.int64) * 16_000,
        wear_id=3, mode="quiz", valid_zone_ratio=0.9, drop_count=2,
        vad_start_us=1000, vad_end_us=500_000, lip_onset_us=1200, voice_onset_us=1500,
        quality="ok",
    )
    if include_mel:
        kwargs["mel"] = rng.normal(size=(M, 40)).astype(np.float32)
    if include_audio:
        kwargs["audio"] = rng.integers(-32768, 32767, size=16000, dtype=np.int16)
        kwargs["audio_t0_us"] = 0
    return kwargs


# ---------------------------------------------------------------------------
# 基本寫入 + 讀回驗證


def test_write_single_trial_with_all_optional_fields(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=60, M=80))

    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["schema_version"] == 1
        assert f["meta"].attrs["clock_slope"] == pytest.approx(1.0000234)

        trial = f["trial_000"]
        assert trial["tof_A"].shape == (60, 32)
        assert trial["tof_A"].dtype == np.float32
        assert trial["tof_valid_A"].shape == (60, 16)
        assert trial["tof_valid_A"].dtype == np.bool_
        assert trial["mic_rms"].shape == (80,)
        assert trial["mic_t_us"].dtype == np.int64
        assert trial["mel"].shape == (80, 40)
        assert trial["audio"].shape == (16000,)
        assert trial.attrs["audio_t0_us"] == 0
        assert trial.attrs["label"] == "五"
        assert trial.attrs["trial_idx"] == 0
        assert trial.attrs["quality"] == "ok"


def test_write_trial_without_optional_fields_omits_datasets(tmp_path):
    """驗收條件（衍生自 T02）：選填欄位要真的可以不存在。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(1, **_sample_trial_kwargs(T=10, M=12, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        trial = f["trial_001"]
        assert "mel" not in trial
        assert "audio" not in trial
        assert "audio_t0_us" not in trial.attrs
        # 必填欄位還是要在
        assert trial["tof_A"].shape == (10, 32)


def test_write_multiple_trials_all_present_after_close(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        for i in range(5):
            w.write_trial(i, **_sample_trial_kwargs(T=20 + i, M=25 + i))

    with h5py.File(path, "r") as f:
        trial_groups = sorted(k for k in f.keys() if k.startswith("trial_"))
        assert trial_groups == [f"trial_{i:03d}" for i in range(5)]
        for i in range(5):
            assert f[f"trial_{i:03d}"]["tof_t_us"].shape == (20 + i,)


def test_rewriting_same_trial_idx_overwrites_cleanly(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=10, M=12))
        w.write_trial(0, **_sample_trial_kwargs(T=99, M=50))  # 蓋掉

    with h5py.File(path, "r") as f:
        assert f["trial_000"]["tof_t_us"].shape == (99,)


# ---------------------------------------------------------------------------
# 驗證失敗案例


def test_missing_meta_field_raises_before_any_write(tmp_path):
    incomplete = _sample_meta()
    del incomplete["clock_slope"]
    path = tmp_path / "session.h5"

    with pytest.raises(ValueError, match="clock_slope"):
        SessionWriter(path, incomplete)


def test_wrong_schema_version_rejected(tmp_path):
    path = tmp_path / "session.h5"
    with pytest.raises(ValueError):
        SessionWriter(path, _sample_meta(schema_version=2))


def test_invalid_quality_rejected(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs()
        kwargs["quality"] = "great"
        with pytest.raises(ValueError):
            w.write_trial(0, **kwargs)


def test_tof_shape_mismatch_rejected(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10)
        kwargs["tof_A"] = kwargs["tof_A"][:, :16]  # 錯的維度 (10,16) 而不是 (10,32)
        with pytest.raises(ValueError):
            w.write_trial(0, **kwargs)


def test_tof_length_mismatch_with_t_us_rejected(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10)
        kwargs["tof_t_us"] = kwargs["tof_t_us"][:5]  # T 對不上
        with pytest.raises(ValueError):
            w.write_trial(0, **kwargs)


def test_audio_without_t0_rejected(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(include_audio=False)
        kwargs["audio"] = np.zeros(100, dtype=np.int16)
        with pytest.raises(ValueError, match="audio_t0_us"):
            w.write_trial(0, **kwargs)


def test_mel_frame_count_must_match_mic(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10, M=12, include_mel=True)
        kwargs["mel"] = kwargs["mel"][:5]  # 幀數對不上 mic_t_us
        with pytest.raises(ValueError):
            w.write_trial(0, **kwargs)


# ---------------------------------------------------------------------------
# 異常中斷：已寫入的 trial 仍可讀（正常路徑，context manager 提早 raise）


def test_exception_after_some_trials_still_leaves_them_readable(tmp_path):
    path = tmp_path / "session.h5"
    with pytest.raises(RuntimeError):
        with SessionWriter(path, _sample_meta()) as w:
            w.write_trial(0, **_sample_trial_kwargs(T=10, M=12))
            w.write_trial(1, **_sample_trial_kwargs(T=10, M=12))
            raise RuntimeError("模擬 session 中途出錯")

    with h5py.File(path, "r") as f:
        assert "trial_000" in f
        assert "trial_001" in f
        assert "trial_002" not in f


# ---------------------------------------------------------------------------
# 邊界：0 幀的 trial（呼應 T02 schema_example.py 的 placeholder 案例）


def test_zero_length_trial_does_not_crash():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/session.h5"
        with SessionWriter(path, _sample_meta()) as w:
            w.write_trial(0, **_sample_trial_kwargs(T=0, M=0, include_mel=False, include_audio=False))

        with h5py.File(path, "r") as f:
            assert f["trial_000"]["tof_A"].shape == (0, 32)
            assert f["trial_000"]["mic_rms"].shape == (0,)


# ---------------------------------------------------------------------------
# 完整性檢查：REQUIRED_* 常數跟 CONTRACTS.md 的欄位清單要對得上
# （防止改 schema 時漏改常數，或改常數時漏改 schema）


def test_required_meta_keys_matches_contracts_field_list():
    expected = {
        "schema_version", "subject", "session_date", "wear_id", "mode",
        "distance_mm", "angle_deg", "ambient", "notes",
        "fw_sha", "proto_version", "tof_dim",
        "clock_slope", "clock_offset", "clock_residual_p95",
        "baseline_mu_A", "baseline_sigma_A", "baseline_mu_B", "baseline_sigma_B",
        "noise_floor_mu", "noise_floor_sigma",
    }
    assert set(REQUIRED_META_KEYS) == expected


def test_required_trial_attrs_matches_contracts_field_list():
    expected = {
        "wear_id", "mode", "valid_zone_ratio", "drop_count",
        "vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us", "quality",
    }
    assert set(REQUIRED_TRIAL_ATTRS) == expected
