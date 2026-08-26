import numpy as np
import pytest
import h5py

from host.storage.session_writer import (
    REQUIRED_META_KEYS,
    REQUIRED_TRIAL_ATTRS,
    SessionWriter,
)


def _sample_meta(**overrides):
    clock_slope = overrides.get("clock_slope", 1.0000234)
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
        "clock_slope": clock_slope,
        "clock_offset": 1756000000.0,
        "clock_residual_p95": 0.0031,
        # B05 兩點法；預設跟 clock_slope 換算後一致，讓 cross-check 預設過關
        # （不是「剛好湊出來」——真實系統裡兩個方法本來就該對得上）。
        "clock_drift_ppm": (clock_slope - 1.0) * 1e6,
        "clock_drift_us": 700.0,
        "clock_sync_span_us": 30_000_000,
        "clock_sync_confirmed": True,
        "session_start_device_us": 0,
        "session_start_host_us": 1_756_000_000_000_000,
        "session_start_rtt_min_us": 800,
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
        # F（mel 幀數）刻意跟 M 不同，證明 mel 用自己的時間軸，不是巧合對齊。
        F = M + 7
        kwargs["mel"] = rng.normal(size=(F, 40)).astype(np.float32)
        kwargs["mel_t_us"] = np.arange(F, dtype=np.int64) * 8_000
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
        assert trial["mel"].shape == (87, 40)  # F = M+7，刻意跟 mic 的 M 不同
        assert trial["mel_t_us"].shape == (87,)
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


def test_mel_and_mel_t_us_must_be_given_together(tmp_path):
    """mel 有自己的時間軸（F），不再跟 mic_t_us 比長度；但 mel/mel_t_us
    彼此缺一不可。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10, M=12, include_mel=True)
        del kwargs["mel_t_us"]
        with pytest.raises(ValueError, match="mel_t_us"):
            w.write_trial(0, **kwargs)


def test_mel_length_must_match_its_own_mel_t_us_not_mic(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10, M=12, include_mel=True)
        kwargs["mel"] = kwargs["mel"][:5]  # 現在只跟 mel_t_us 比，這樣會對不上
        with pytest.raises(ValueError):
            w.write_trial(0, **kwargs)


def test_mel_frame_count_is_allowed_to_differ_from_mic(tmp_path):
    """反向驗證：mel 幀數（F）跟 mic 幀數（M）不同是正常情況，不該被拒絕
    ——這正是這輪把 mel 從 (M,40) 改成 (F,40) 的原因。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=10, M=12, include_mel=True))  # 不應該 raise

    with h5py.File(path, "r") as f:
        assert f["trial_000"]["mel"].shape[0] != f["trial_000"]["mic_rms"].shape[0]


# ---------------------------------------------------------------------------
# tof_ambient_A/B/t_us：全有或全無，各自時間軸


def test_ambient_trio_partial_is_rejected(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10, M=12, include_mel=False, include_audio=False)
        kwargs["tof_ambient_A"] = np.zeros((5, 16), dtype=np.float32)
        # 沒給 tof_ambient_B / tof_ambient_t_us
        with pytest.raises(ValueError, match="tof_ambient"):
            w.write_trial(0, **kwargs)


def test_ambient_trio_written_with_its_own_time_axis(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10, M=12, include_mel=False, include_audio=False)
        kwargs["tof_ambient_A"] = np.full((3, 16), 100.0, dtype=np.float32)
        kwargs["tof_ambient_B"] = np.full((3, 16), 110.0, dtype=np.float32)
        kwargs["tof_ambient_t_us"] = np.array([0, 1_000_000, 2_000_000], dtype=np.int64)
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        trial = f["trial_000"]
        assert trial["tof_ambient_A"].shape == (3, 16)
        assert trial["tof_ambient_B"].shape == (3, 16)
        assert trial["tof_ambient_t_us"].shape == (3,)
        # 跟 tof_t_us（T=10）長度不同也完全合法
        assert trial["tof_ambient_t_us"].shape[0] != trial["tof_t_us"].shape[0]


def test_ambient_invalid_zone_becomes_nan(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10, M=12, include_mel=False, include_audio=False)
        a = [[100.0] * 16, [100.0] * 16]
        a[1][4] = None
        kwargs["tof_ambient_A"] = a
        kwargs["tof_ambient_B"] = np.full((2, 16), 100.0, dtype=np.float32)
        kwargs["tof_ambient_t_us"] = np.array([0, 1_000_000], dtype=np.int64)
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        assert np.isnan(f["trial_000"]["tof_ambient_A"][1, 4])


def test_ambient_shape_mismatch_rejected(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=10, M=12, include_mel=False, include_audio=False)
        kwargs["tof_ambient_A"] = np.zeros((3, 16), dtype=np.float32)
        kwargs["tof_ambient_B"] = np.zeros((3, 16), dtype=np.float32)
        kwargs["tof_ambient_t_us"] = np.array([0, 1], dtype=np.int64)  # 長度對不上 (2 vs 3)
        with pytest.raises(ValueError):
            w.write_trial(0, **kwargs)


# ---------------------------------------------------------------------------
# finalize_session_end：收尾三欄位


def test_finalize_session_end_writes_three_attrs(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))
        w.finalize_session_end(
            session_end_device_us=30_000_000, session_end_host_us=1_756_000_030_000_000,
            session_end_rtt_min_us=750,
        )

    with h5py.File(path, "r") as f:
        meta = f["meta"]
        assert meta.attrs["session_end_device_us"] == 30_000_000
        assert meta.attrs["session_end_host_us"] == 1_756_000_030_000_000
        assert meta.attrs["session_end_rtt_min_us"] == 750


def test_session_end_attrs_absent_if_finalize_never_called(tmp_path):
    """沒呼叫 finalize_session_end() 也不該報錯——已寫的 trial 一樣完整，
    只是 /meta 少這三個收尾欄位（story 說明裡的「不是這裡的責任」）。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert "session_end_device_us" not in f["meta"].attrs


# ---------------------------------------------------------------------------
# clock_cross_check：B04 回歸法 vs B05 兩點法互相印證


def test_clock_cross_check_ok_when_methods_agree(tmp_path):
    path = tmp_path / "session.h5"
    # _sample_meta() 預設就讓 clock_drift_ppm 跟 clock_slope 換算後一致
    with SessionWriter(path, _sample_meta()) as w:
        pass

    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["clock_cross_check_ok"] == True  # noqa: E712 (h5py bool attr)
        assert f["meta"].attrs["clock_cross_check_ppm_diff"] < 1.0


def test_clock_cross_check_flags_disagreement_between_methods(tmp_path):
    """驗收條件（B05 交叉檢查）：兩個獨立方法的時鐘估計差太多要標出來，
    不能默默放過。"""
    path = tmp_path / "session.h5"
    meta = _sample_meta(clock_slope=1.0000234)  # ~23.4 ppm
    meta["clock_drift_ppm"] = 5000.0  # 跟回歸法差了快 5000 ppm，明顯不一致

    with SessionWriter(path, meta) as w:
        pass

    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["clock_cross_check_ok"] == False  # noqa: E712
        assert f["meta"].attrs["clock_cross_check_ppm_diff"] > 1000.0


# ---------------------------------------------------------------------------
# mode="a"：baseline（mode="w"）建檔後，trial machine 重開繼續寫，
# 不能砍掉 baseline 的 trial_000（急件：ed 實測過會被 mode="w" 截斷）


def test_append_mode_preserves_baseline_trial(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:  # 模擬 B10 的 baseline 建檔
        w.write_trial(0, label="_baseline", **{k: v for k, v in _sample_trial_kwargs(T=10, M=12).items() if k != "label"})

    with SessionWriter(path, mode="a") as w:  # 模擬 B11 的 trial machine 重開
        w.write_trial(1, **_sample_trial_kwargs(T=20, M=25))

    with h5py.File(path, "r") as f:
        assert "trial_000" in f  # baseline 沒有被砍掉
        assert f["trial_000"].attrs["label"] == "_baseline"
        assert "trial_001" in f
        assert f["meta"].attrs["schema_version"] == 1  # /meta 也還在


def test_append_mode_does_not_rewrite_meta():
    """重開時 `/meta` 不該被重新計算——尤其是 `clock_cross_check_*` 這種
    衍生欄位，重算應該得到同一個值，但這裡驗證的是「根本沒有再跑一次
    `_write_meta()`」，不是「算出來的值剛好一樣」。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/session.h5"
        with SessionWriter(path, _sample_meta()) as w:
            pass

        with h5py.File(path, "a") as f:
            f["meta"].attrs["notes"] = "手動改過，重開不該被蓋掉"

        with SessionWriter(path, mode="a") as w:
            pass

        with h5py.File(path, "r") as f:
            assert f["meta"].attrs["notes"] == "手動改過，重開不該被蓋掉"


def test_append_mode_rejects_meta_argument():
    """mode='a' 不該讓呼叫端誤以為可以順便更新 meta——/meta 已經固定了。"""
    with pytest.raises(ValueError, match="mode='a'"):
        SessionWriter("/nonexistent/path.h5", _sample_meta(), mode="a")


def test_append_mode_requires_existing_meta_group(tmp_path):
    """重開一個根本不是 SessionWriter 建的（或還沒建過 baseline 的）檔案
    要明確報錯，不能悄悄建一個空的 /meta 或整個炸掉。"""
    path = tmp_path / "not_a_session.h5"
    with pytest.raises(ValueError, match="/meta"):
        with SessionWriter(path, mode="a"):
            pass


def test_append_mode_rejects_incomplete_meta(tmp_path):
    path = tmp_path / "session.h5"
    with h5py.File(path, "w") as f:
        meta = f.create_group("meta")
        meta.attrs["schema_version"] = 1
        # 故意漏掉其他必填欄位

    with pytest.raises(ValueError, match="缺少必填欄位"):
        with SessionWriter(path, mode="a"):
            pass


def test_invalid_mode_rejected():
    with pytest.raises(ValueError, match="mode"):
        SessionWriter("/tmp/whatever.h5", _sample_meta(), mode="r+")


def test_finalize_session_end_works_after_reopen(tmp_path):
    """急件裡明確要求：`finalize_session_end()` 在 append 模式下也要能用。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, label="_baseline", **{k: v for k, v in _sample_trial_kwargs(T=5, M=6).items() if k != "label"})

    with SessionWriter(path, mode="a") as w:
        w.write_trial(1, **_sample_trial_kwargs(T=5, M=6))
        w.finalize_session_end(
            session_end_device_us=99_000_000, session_end_host_us=1_756_000_099_000_000,
            session_end_rtt_min_us=760,
        )

    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["session_end_device_us"] == 99_000_000
        assert f["meta"].attrs["session_end_rtt_min_us"] == 760


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
# 無效 zone：None -> NaN，不是 -1


def test_invalid_tof_zone_none_becomes_nan_not_minus_one(tmp_path):
    """`host/align/aligner.py` 的 `TofSample.values` 用 Python `None`
    標記無效 zone；HDF5 的 float32 dataset 沒有 `None`，寫入時要轉成
    `NaN`，不能悄悄變成 `-1` 或 `0`（T02 的核心設計決定）。"""
    path = tmp_path / "session.h5"
    kwargs = _sample_trial_kwargs(T=3, M=5, include_mel=False, include_audio=False)
    tof_A = [[float(x) for x in row] for row in kwargs["tof_A"].tolist()]
    tof_A[1][5] = None
    tof_A[1][5 + 16] = None
    kwargs["tof_A"] = tof_A
    kwargs["tof_valid_A"] = np.ones((3, 16), dtype=bool)
    kwargs["tof_valid_A"][1, 5] = False

    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        values = f["trial_000"]["tof_A"][:]
        assert np.isnan(values[1, 5])
        assert np.isnan(values[1, 5 + 16])
        assert not np.any(values[1, 5] == -1)
        assert np.isfinite(values[0]).all()  # 其他幀沒被誤傷


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
# 驗收條件：「產出的檔案通過 T02 的 schema 驗證腳本」


def _assert_matches_t02_schema(path):
    """比照 T02 story 的驗收精神，逐項斷言 CONTRACTS.md §2 列出的所有
    attrs / datasets / shape / dtype。"""
    with h5py.File(path, "r") as f:
        assert "meta" in f
        meta = f["meta"]
        for key in REQUIRED_META_KEYS:
            assert key in meta.attrs, f"/meta 缺少 {key}"
        assert meta.attrs["schema_version"] == 1

        trial_names = sorted(k for k in f.keys() if k.startswith("trial_"))
        assert trial_names
        for name in trial_names:
            trial = f[name]
            for ds in ("tof_A", "tof_B", "tof_t_us", "tof_valid_A", "tof_valid_B",
                       "mic_rms", "mic_peak", "mic_t_us"):
                assert ds in trial, f"{name} 缺少 dataset {ds}"
            for attr in REQUIRED_TRIAL_ATTRS + ("label", "trial_idx"):
                assert attr in trial.attrs, f"{name} 缺少 attr {attr}"

            assert trial["tof_A"].shape[1] == 32
            assert trial["tof_valid_A"].shape[1] == 16
            assert trial["tof_valid_A"].dtype == np.bool_
            assert trial["tof_A"].dtype == np.float32
            assert trial["mic_t_us"].dtype == np.int64
            if "mel" in trial:
                assert trial["mel"].shape[1] == 40
                assert "mel_t_us" in trial
                assert trial["mel_t_us"].shape == (trial["mel"].shape[0],)
            if "tof_ambient_A" in trial:
                assert "tof_ambient_B" in trial and "tof_ambient_t_us" in trial
                assert trial["tof_ambient_A"].shape[1] == 16
            if "audio" in trial:
                assert "audio_t0_us" in trial.attrs

        assert "clock_cross_check_ok" in meta.attrs
        assert "clock_cross_check_ppm_diff" in meta.attrs


def test_output_passes_t02_schema_validation(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=30, M=40, include_mel=True, include_audio=True))
        w.write_trial(1, **_sample_trial_kwargs(T=15, M=20, include_mel=False, include_audio=False))

    _assert_matches_t02_schema(path)


# ---------------------------------------------------------------------------
# 完整性檢查：REQUIRED_* 常數跟 CONTRACTS.md 的欄位清單要對得上
# （防止改 schema 時漏改常數，或改常數時漏改 schema）


def test_required_meta_keys_matches_contracts_field_list():
    expected = {
        "schema_version", "subject", "session_date", "wear_id", "mode",
        "distance_mm", "angle_deg", "ambient", "notes",
        "fw_sha", "proto_version", "tof_dim",
        "clock_slope", "clock_offset", "clock_residual_p95",
        "clock_drift_us", "clock_drift_ppm", "clock_sync_span_us", "clock_sync_confirmed",
        "session_start_device_us", "session_start_host_us", "session_start_rtt_min_us",
        "baseline_mu_A", "baseline_sigma_A", "baseline_mu_B", "baseline_sigma_B",
        "noise_floor_mu", "noise_floor_sigma",
    }
    assert set(REQUIRED_META_KEYS) == expected


def test_session_end_meta_keys_matches_contracts_field_list():
    from host.storage.session_writer import SESSION_END_META_KEYS
    assert set(SESSION_END_META_KEYS) == {
        "session_end_device_us", "session_end_host_us", "session_end_rtt_min_us",
    }


def test_required_trial_attrs_matches_contracts_field_list():
    """`vad_start_us`/`vad_end_us`/`lip_onset_us`/`voice_onset_us` 不在這裡
    ——B17 的調度決議之後它們是「偵測到才寫」，不是每個 trial 都必填。"""
    expected = {"wear_id", "mode", "valid_zone_ratio", "drop_count", "quality"}
    assert set(REQUIRED_TRIAL_ATTRS) == expected


# ---------------------------------------------------------------------------
# VAD 時間戳缺席時整個 attr 不寫入；speaking_mode／vad_confidence（B17 調度決議）


def test_vad_timing_attrs_omitted_when_none(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["vad_start_us"] = None
        kwargs["vad_end_us"] = None
        kwargs["lip_onset_us"] = None
        kwargs["voice_onset_us"] = None
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        attrs = f["trial_000"].attrs
        for key in ("vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us"):
            assert key not in attrs
            with pytest.raises(KeyError):
                attrs[key]  # noqa: B018 -- 故意觸發，驗證「大聲失敗」


def test_vad_timing_attrs_independently_optional(tmp_path):
    """驗收條件的核心：silent 模式下 voice_onset_us 必然缺席，但
    lip_onset_us 仍應該有——四個欄位不是綁在一起的。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["voice_onset_us"] = None  # 只有這個缺席
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        attrs = f["trial_000"].attrs
        assert "voice_onset_us" not in attrs
        assert "vad_start_us" in attrs
        assert "vad_end_us" in attrs
        assert "lip_onset_us" in attrs


def test_speaking_mode_and_vad_confidence_written_when_given(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["speaking_mode"] = "whisper"
        kwargs["vad_confidence"] = 0.82
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        attrs = f["trial_000"].attrs
        assert attrs["speaking_mode"] == "whisper"
        assert attrs["vad_confidence"] == pytest.approx(0.82, abs=1e-5)


def test_speaking_mode_and_vad_confidence_omitted_by_default(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        attrs = f["trial_000"].attrs
        assert "speaking_mode" not in attrs
        assert "vad_confidence" not in attrs


def test_silent_mode_vad_confidence_is_none_not_a_fabricated_number(tmp_path):
    """`silent` 模式沒有跑音訊 VAD，`vad_confidence` 應該是 None（不寫），
    不是隨便給一個看起來合理的數字。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["speaking_mode"] = "silent"
        kwargs["vad_confidence"] = None
        kwargs["voice_onset_us"] = None
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        attrs = f["trial_000"].attrs
        assert attrs["speaking_mode"] == "silent"
        assert "vad_confidence" not in attrs
        assert "voice_onset_us" not in attrs
        assert "lip_onset_us" in attrs  # silent 模式唇動仍然存在


def test_invalid_speaking_mode_rejected(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["speaking_mode"] = "shouting"
        with pytest.raises(ValueError):
            w.write_trial(0, **kwargs)


# -- sensors_enabled (D15's C0 pairing fix) --------------------------------


def test_sensors_enabled_omitted_by_default(tmp_path):
    """_sample_meta() 不給這個欄位——現有呼叫端（bridge_server.py）還沒
    接這條線，這條路徑必須繼續正常運作，不能因為新欄位變成必填而斷線。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        attrs = f["meta"].attrs
        assert "sensors_enabled" not in attrs
        assert "sensors_enabled_confirmed" not in attrs


def test_sensors_enabled_written_when_given_defaults_unconfirmed(tmp_path):
    """給了值但沒給 confirmed，預設 False——不能悄悄變成「已確認」。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta(sensors_enabled="AB")) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        attrs = f["meta"].attrs
        assert attrs["sensors_enabled"] == "AB"
        assert bool(attrs["sensors_enabled_confirmed"]) is False


def test_sensors_enabled_confirmed_can_be_set_true(tmp_path):
    path = tmp_path / "session.h5"
    meta = _sample_meta(sensors_enabled="A", sensors_enabled_confirmed=True)
    with SessionWriter(path, meta) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["sensors_enabled"] == "A"
        assert bool(f["meta"].attrs["sensors_enabled_confirmed"]) is True


def test_invalid_sensors_enabled_rejected(tmp_path):
    with pytest.raises(ValueError, match="sensors_enabled"):
        with SessionWriter(tmp_path / "session.h5", _sample_meta(sensors_enabled="C")):
            pass


def test_sensors_enabled_confirmed_alone_is_rejected(tmp_path):
    """沒有 sensors_enabled，「確認」這件事無所依附。"""
    with pytest.raises(ValueError, match="sensors_enabled"):
        with SessionWriter(tmp_path / "session.h5", _sample_meta(sensors_enabled_confirmed=True)):
            pass


def test_old_files_without_sensors_enabled_still_reopen_in_append_mode(tmp_path):
    """這是選填而非必填的重點：既有 session 檔（這個欄位加進來之前建的）
    重開繼續寫 trial 不能因為缺這個新欄位就報錯。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:  # 沒給 sensors_enabled，模擬舊檔
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with SessionWriter(path, mode="a") as w:
        w.write_trial(1, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert "sensors_enabled" not in f["meta"].attrs
        assert "trial_001" in f


# -- energy_mu/energy_sigma (4f's B21 lip-lead bias fix) -------------------


def test_energy_mu_sigma_omitted_by_default(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert "energy_mu" not in f["meta"].attrs
        assert "energy_sigma" not in f["meta"].attrs


def test_energy_mu_sigma_written_together(tmp_path):
    path = tmp_path / "session.h5"
    meta = _sample_meta(energy_mu=123.5, energy_sigma=17.25)
    with SessionWriter(path, meta) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["energy_mu"] == pytest.approx(123.5)
        assert f["meta"].attrs["energy_sigma"] == pytest.approx(17.25)


def test_energy_mu_without_sigma_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="energy_mu"):
        with SessionWriter(tmp_path / "session.h5", _sample_meta(energy_mu=123.5)):
            pass


def test_energy_sigma_without_mu_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="energy_mu"):
        with SessionWriter(tmp_path / "session.h5", _sample_meta(energy_sigma=17.25)):
            pass


# -- comparable (4f's measure_lip_lead() flag) -----------------------------


def test_comparable_omitted_when_none(tmp_path):
    """沒算過就整個 attr 不寫入——跟 VAD 四個時間戳同一個原則，不能跟
    「算過了、結論是不可比」（`False`）混在一起。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert "comparable" not in f["trial_000"].attrs
        with pytest.raises(KeyError):
            f["trial_000"].attrs["comparable"]  # noqa: B018 -- 故意觸發


def test_comparable_true_and_false_both_written_when_given(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs_true = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs_true["comparable"] = True
        w.write_trial(0, **kwargs_true)

        kwargs_false = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs_false["comparable"] = False
        w.write_trial(1, **kwargs_false)

    with h5py.File(path, "r") as f:
        assert bool(f["trial_000"].attrs["comparable"]) is True
        assert bool(f["trial_001"].attrs["comparable"]) is False


def test_comparable_raw_h5py_read_is_numpy_bool_not_python_bool(tmp_path):
    """`comparable` 是這個 schema 第一個 bool 型的選填 trial attr。直接用
    h5py 讀（不經過 session_loader.py 的 `_as_scalar()`）拿到的是
    `numpy.bool_`，不是 Python `bool`——`numpy.bool_(True) is True` 是
    `False`，任何下游程式碼如果寫 `if trial.attrs["comparable"] is True:`
    會靜默失效，恆真恆假都測不出來，因為它從來不會拋例外。這裡把這個陷阱
    釘住：直接讀 h5py 確實會踩到；session_loader.py 那邊的對應測試
    （test_e2e_pipeline.py 的 type-seam 那節）確認 `_as_scalar()` 有接住它。
    """
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["comparable"] = True
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        raw = f["trial_000"].attrs["comparable"]
        assert isinstance(raw, np.bool_)
        assert raw == True  # noqa: E712 -- 值本身是對的
        assert (raw is True) is False  # 陷阱本身：身分比較會静默失效


# -- lip_onset_us_A/B (union_min 融合前的各感測器 onset) -------------------


def test_lip_onset_per_sensor_omitted_when_none(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        attrs = f["trial_000"].attrs
        assert "lip_onset_us_A" not in attrs
        assert "lip_onset_us_B" not in attrs


def test_lip_onset_per_sensor_written_when_given(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["lip_onset_us"] = 1500       # 融合後結果
        kwargs["lip_onset_us_A"] = 1500
        kwargs["lip_onset_us_B"] = 1620
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        attrs = f["trial_000"].attrs
        assert attrs["lip_onset_us"] == 1500
        assert attrs["lip_onset_us_A"] == 1500
        assert attrs["lip_onset_us_B"] == 1620


def test_lip_onset_b_missing_is_a_legal_state_not_corruption(tmp_path):
    """union_min 的設計本身就假設 B 感測器可能整段偵測不到——`lip_onset_us`
    (融合後) 跟 `lip_onset_us_A` 都在，`lip_onset_us_B` 缺席，這是合法組合，
    不是三個欄位必須綁在一起有/一起沒有。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["lip_onset_us"] = 1500
        kwargs["lip_onset_us_A"] = 1500
        # lip_onset_us_B 不給
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        attrs = f["trial_000"].attrs
        assert attrs["lip_onset_us_A"] == 1500
        assert "lip_onset_us_B" not in attrs
        with pytest.raises(KeyError):
            attrs["lip_onset_us_B"]  # noqa: B018 -- 故意觸發，驗證「大聲失敗」


# -- source (B19 發現的落差：呼叫端在傳，寫入端以前沒接住) ------------------


def test_source_omitted_by_default(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert "source" not in f["meta"].attrs


def test_source_written_when_given(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta(source="mock")) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["source"] == "mock"


@pytest.mark.parametrize("value", ["live", "mock", "replay-log", "replay-session"])
def test_source_accepts_every_value_bridge_server_can_send(tmp_path, value):
    """跟 bridge_server.py 的 VALID_SOURCES 同一份值域，四個都要能收。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta(source=value)) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))
    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["source"] == value


def test_invalid_source_rejected(tmp_path):
    with pytest.raises(ValueError, match="source"):
        with SessionWriter(tmp_path / "session.h5", _sample_meta(source="usb")):
            pass


# -- baseline_age_s (E05 過期偵測需要事後可查的年齡，不是判定結果) --------


def test_baseline_age_s_omitted_when_none(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    with h5py.File(path, "r") as f:
        assert "baseline_age_s" not in f["trial_000"].attrs


def test_baseline_age_s_written_when_given(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False)
        kwargs["baseline_age_s"] = 1834.5
        w.write_trial(0, **kwargs)

    with h5py.File(path, "r") as f:
        assert f["trial_000"].attrs["baseline_age_s"] == pytest.approx(1834.5, abs=1e-2)
