"""`first_session_check.py` 的測試——用 `SessionWriter` 造合成 session，
刻意造幾個壞掉的（baseline 被蓋掉、`t_us` 往回跳、VAD 鏈路沒接、
沒有 `energy_mu`），確認結構壞掉的項目真的被歸類成 STOP，
數字類的項目不會被誤判成 STOP。

不直接開 h5py 驗證——跟 `first_session_check.py` 本身一樣，全部走
`analysis/reporting/session_loader.py`／`host/storage/session_writer.py`。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from host.storage.session_writer import SessionWriter, TOF_VALUES_DIM, TOF_VALID_DIM  # noqa: E402
from first_session_check import check_session, main, DEFAULT_WORD_TARGET  # noqa: E402


def _base_meta(**overrides):
    meta = {
        "schema_version": 1,
        "subject": "test_subject",
        "session_date": "2026-08-26",
        "wear_id": 0,
        "mode": "record",
        "distance_mm": 30.0,
        "angle_deg": 0.0,
        "ambient": "indoor",
        "notes": "",
        "fw_sha": "abc1234",
        "proto_version": 2,
        "tof_dim": 8,
        "clock_slope": 1.0,
        "clock_offset": 0.0,
        "clock_residual_p95": 0.0,
        "clock_drift_us": 0.0,
        "clock_drift_ppm": 0.0,
        "clock_sync_span_us": 0,
        "clock_sync_confirmed": True,
        "session_start_device_us": 0,
        "session_start_host_us": 0,
        "session_start_rtt_min_us": 0,
        "baseline_mu_A": np.zeros(TOF_VALUES_DIM, dtype=np.float32),
        "baseline_sigma_A": np.ones(TOF_VALUES_DIM, dtype=np.float32),
        "baseline_mu_B": np.zeros(TOF_VALUES_DIM, dtype=np.float32),
        "baseline_sigma_B": np.ones(TOF_VALUES_DIM, dtype=np.float32),
        "noise_floor_mu": 450.0,
        "noise_floor_sigma": 30.0,
    }
    meta.update(overrides)
    return meta


def _trial_arrays(n_tof=10, n_mic=10, t_us_start=0, t_us_step=1000,
                   invalid_zone_ratio=0.0, mic_peak_value=400, force_backward_at=None,
                   mic_rms_value=500.0):
    t_us = np.arange(t_us_start, t_us_start + n_tof * t_us_step, t_us_step, dtype=np.int64)
    if force_backward_at is not None:
        t_us[force_backward_at] = t_us[force_backward_at - 1] - 500
    tof_A = np.zeros((n_tof, TOF_VALUES_DIM), dtype=np.float32)
    tof_B = np.zeros((n_tof, TOF_VALUES_DIM), dtype=np.float32)
    valid_A = np.ones((n_tof, TOF_VALID_DIM), dtype=bool)
    valid_B = np.ones((n_tof, TOF_VALID_DIM), dtype=bool)
    n_invalid = int(round(TOF_VALID_DIM * invalid_zone_ratio))
    if n_invalid:
        valid_A[:, :n_invalid] = False
        valid_B[:, :n_invalid] = False
    mic_rms = np.full(n_mic, mic_rms_value, dtype=np.float32)
    mic_peak = np.full(n_mic, mic_peak_value, dtype=np.int16)
    mic_t_us = np.arange(n_mic, dtype=np.int64) * 2000
    return dict(
        tof_A=tof_A, tof_B=tof_B, tof_t_us=t_us,
        tof_valid_A=valid_A, tof_valid_B=valid_B,
        mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
    )


def _write_session(path, meta, trials):
    """`trials`：list of (idx, label, kwargs_overrides)。"""
    with SessionWriter(str(path), meta) as w:
        for idx, label, overrides in trials:
            kwargs = dict(
                wear_id=0, mode="record", valid_zone_ratio=1.0, drop_count=0, quality="ok",
            )
            kwargs.update(_trial_arrays())
            kwargs.update(overrides)
            w.write_trial(idx, label=label, **kwargs)


def test_good_session_passes_with_no_stop(tmp_path):
    path = tmp_path / "good.h5"
    meta = _base_meta(energy_mu=100.0, energy_sigma=10.0)
    _write_session(path, meta, [
        (0, "_baseline", {}),
        (1, "八", {
            "voice_onset_us": 20000, "lip_onset_us": 10000,
            "vad_start_us": 5000, "vad_end_us": 40000,
            "comparable": True, "speaking_mode": "normal",
        }),
        (2, "五", {
            "voice_onset_us": 21000, "lip_onset_us": 11000,
            "comparable": True, "speaking_mode": "normal",
        }),
    ])

    v = check_session(str(path), target=5)

    assert v.ok
    assert v.stop_reasons == []
    assert any("VAD 有值" in line for line in v.info_lines)
    assert any("energy_mu" in line for line in v.info_lines)
    assert any("八" in line and "五" in line for line in v.info_lines)


def test_missing_baseline_is_a_stop(tmp_path):
    """模擬撞號：整個 session 裡沒有任何一筆 `_baseline`
    ——第一筆真實錄音把它蓋掉的結果就長這樣。"""
    path = tmp_path / "no_baseline.h5"
    meta = _base_meta()
    _write_session(path, meta, [
        (0, "八", {}),
    ])

    v = check_session(str(path), target=5)

    assert not v.ok
    assert any("_baseline" in reason for reason in v.stop_reasons)


def test_backward_timestamp_is_a_stop(tmp_path):
    path = tmp_path / "time_travel.h5"
    meta = _base_meta()
    _write_session(path, meta, [
        (0, "_baseline", {}),
        (1, "八", {**_trial_arrays(force_backward_at=5)}),
    ])

    v = check_session(str(path), target=5)

    assert not v.ok
    assert any("往回跳" in reason for reason in v.stop_reasons)


def test_missing_vad_chain_is_a_warning_not_a_stop(tmp_path):
    path = tmp_path / "no_vad.h5"
    meta = _base_meta()
    _write_session(path, meta, [
        (0, "_baseline", {}),
        (1, "八", {}),
        (2, "五", {}),
    ])

    v = check_session(str(path), target=5)

    assert v.ok
    assert any("VAD 鏈路可能沒接上" in w for w in v.warnings)


def test_missing_energy_mu_is_a_warning_not_a_stop(tmp_path):
    path = tmp_path / "no_energy.h5"
    meta = _base_meta()  # 沒有 energy_mu/energy_sigma
    _write_session(path, meta, [
        (0, "_baseline", {}),
        (1, "八", {}),
    ])

    v = check_session(str(path), target=5)

    assert v.ok
    assert any("energy_mu/energy_sigma" in w for w in v.warnings)


def test_invalid_zone_ratio_and_clipping_are_reported_as_numbers_not_stops(tmp_path):
    path = tmp_path / "messy_numbers.h5"
    meta = _base_meta()
    _write_session(path, meta, [
        (0, "_baseline", {}),
        (1, "八", _trial_arrays(invalid_zone_ratio=0.5, mic_peak_value=32767)),
    ])

    v = check_session(str(path), target=5)

    assert v.ok, f"數字異常不該變成 STOP，但拿到 {v.stop_reasons}"
    assert any("無效 zone 比例" in line for line in v.info_lines)
    assert any("削波" in line for line in v.info_lines)


def test_mic_signal_level_is_reported_as_numbers_not_a_stop(tmp_path):
    """真板子第一手資料 RMS 4-6，遠低於 300 的門檻——這種「數字很低但
    不是恆為 0」的情況必須只印數字，不能變成 STOP（還不知道真實正常值
    該是多少）。"""
    path = tmp_path / "quiet_mic.h5"
    meta = _base_meta()
    _write_session(path, meta, [
        (0, "_baseline", {}),
        (1, "八", _trial_arrays(mic_rms_value=5.0)),
    ])

    v = check_session(str(path), target=5)

    assert v.ok, f"RMS 偏低不該變成 STOP，但拿到 {v.stop_reasons}"
    assert any("麥克風 RMS 分布" in line and "min=5.0" in line for line in v.info_lines)


def test_mic_rms_constantly_zero_is_a_stop(tmp_path):
    """麥克風完全沒接上訊號——查過 host/quality/metrics.py，現行的即時
    品質儀表板對 noise_floor 只有「太吵」的方向，RMS=0 會被判成全綠。
    這裡是目前唯一能抓到「麥克風可能沒接上」的地方，必須是 STOP。"""
    path = tmp_path / "dead_mic.h5"
    meta = _base_meta()
    _write_session(path, meta, [
        (0, "_baseline", _trial_arrays(mic_rms_value=0.0)),
        (1, "八", _trial_arrays(mic_rms_value=0.0)),
        (2, "五", _trial_arrays(mic_rms_value=0.0)),
    ])

    v = check_session(str(path), target=5)

    assert not v.ok
    assert any("恆為 0" in reason for reason in v.stop_reasons)


def test_mic_rms_zero_in_only_some_trials_is_not_a_stop(tmp_path):
    """只有部分 trial 是 0（例如某一筆真的沒講話）不該被當成整個
    session 的麥克風沒接上——STOP 只保留給「整個 session 恆為 0」。"""
    path = tmp_path / "partial_silence.h5"
    meta = _base_meta()
    _write_session(path, meta, [
        (0, "_baseline", {}),
        (1, "八", _trial_arrays(mic_rms_value=0.0)),
        (2, "五", _trial_arrays(mic_rms_value=500.0)),
    ])

    v = check_session(str(path), target=5)

    assert v.ok, f"部分為 0 不該變成 STOP，但拿到 {v.stop_reasons}"
    assert any("恆為 0" not in line for line in v.info_lines)


def test_drop_count_is_summed_and_reported(tmp_path):
    path = tmp_path / "drops.h5"
    meta = _base_meta()
    _write_session(path, meta, [
        (0, "_baseline", {}),
        (1, "八", {"drop_count": 3}),
        (2, "五", {"drop_count": 4}),
    ])

    v = check_session(str(path), target=5)

    assert v.ok
    assert any("累計 drop_count" in line and "7" in line for line in v.info_lines)


def test_read_failure_on_missing_file_is_a_stop(tmp_path):
    v = check_session(str(tmp_path / "does_not_exist.h5"), target=5)

    assert not v.ok
    assert any("讀檔失敗" in reason for reason in v.stop_reasons)


def test_main_exit_code_reflects_worst_session(tmp_path, capsys):
    good_path = tmp_path / "good.h5"
    bad_path = tmp_path / "bad.h5"
    _write_session(good_path, _base_meta(), [(0, "_baseline", {}), (1, "八", {})])
    _write_session(bad_path, _base_meta(), [(0, "八", {})])  # 沒有 baseline

    exit_code = main([str(good_path), str(bad_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "🔴 停" in captured.out
    assert str(good_path) in captured.out
    assert str(bad_path) in captured.out


def test_main_exit_code_zero_when_all_sessions_ok(tmp_path, capsys):
    path = tmp_path / "good.h5"
    _write_session(path, _base_meta(), [(0, "_baseline", {}), (1, "八", {})])

    exit_code = main([str(path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "✅ 資料看起來可用，可以繼續錄" in captured.out


def test_default_word_target_is_documented_and_overridable(tmp_path):
    path = tmp_path / "good.h5"
    _write_session(path, _base_meta(), [(0, "_baseline", {}), (1, "八", {})])

    v_default = check_session(str(path), target=DEFAULT_WORD_TARGET)
    v_override = check_session(str(path), target=1)

    assert any(f"/{DEFAULT_WORD_TARGET}" in line for line in v_default.info_lines)
    assert any("/1" in line for line in v_override.info_lines)
