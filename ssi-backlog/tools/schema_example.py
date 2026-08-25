#!/usr/bin/env python3
"""產生一個結構正確、內容為空的 session HDF5 檔，供 D 軌對著 schema 開發。

Schema 定義見 CONTRACTS.md 第 2 章。**直接呼叫 `host/storage/session_writer.py`
的 `SessionWriter` 產生檔案，不在這裡另外維護一份 schema 知識**——這支工具
之前自己刻了一份 `h5py.create_dataset` 的骨架，`B07`/`B11` 陸續幫 schema
加了時鐘漂移欄位、`mel_t_us`、`tof_ambient_*` 之後，這裡沒有跟著更新，
D 軌拿舊格式的空檔測過、全綠，換真實資料一來卻對不上（`mel` 幀數就是
一個活生生的例子）。改成呼叫 `SessionWriter` 之後，只要 `SessionWriter`
本身正確，這支工具就不可能再不同步。

用法：
    python3 schema_example.py [輸出路徑]

預設輸出路徑為 ./session_example.h5。
"""
import sys
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[2]  # ssi-backlog/tools/ -> ssi-backlog -> repo root
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
from host.storage.session_writer import SessionWriter, TOF_VALID_DIM, TOF_VALUES_DIM  # noqa: E402

SCHEMA_VERSION = 1
MEL_BANDS = 40


def _example_meta() -> dict:
    """`SessionWriter` 建構時必填的全部 `/meta` 欄位（CONTRACTS.md §2）。
    數值都是佔位——這支工具的目的是「結構正確」，不是「內容有意義」。
    `session_end_*` 三個欄位刻意不寫（`finalize_session_end()` 沒呼叫）：
    真實情況下它們要等 session 結束才有值，讀取端本來就該把它們當選填。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "subject": "example_subject",
        "session_date": "1970-01-01",
        "wear_id": 0,
        "mode": "example",
        "distance_mm": 0.0,
        "angle_deg": 0.0,
        "ambient": "",
        "notes": "",
        "fw_sha": "0" * 7,
        "proto_version": 2,
        "tof_dim": 8,
        # B04（回歸法）
        "clock_slope": 1.0,
        "clock_offset": 0.0,
        "clock_residual_p95": 0.0,
        # B05（兩點法）；跟 clock_slope 換算後一致，讓 SessionWriter 算出的
        # clock_cross_check_ok 預設是 True——這是個空範例檔，不是要示範
        # 「時鐘可疑」長什麼樣。
        "clock_drift_us": 0.0,
        "clock_drift_ppm": 0.0,
        "clock_sync_span_us": 0,
        "clock_sync_confirmed": True,
        "session_start_device_us": 0,
        "session_start_host_us": 0,
        "session_start_rtt_min_us": 0,
        # 同一次戴上時錄的 baseline，每顆感測器各一份
        "baseline_mu_A": np.zeros(TOF_VALUES_DIM, dtype=np.float32),
        "baseline_sigma_A": np.ones(TOF_VALUES_DIM, dtype=np.float32),
        "baseline_mu_B": np.zeros(TOF_VALUES_DIM, dtype=np.float32),
        "baseline_sigma_B": np.ones(TOF_VALUES_DIM, dtype=np.float32),
        "noise_floor_mu": 0.0,
        "noise_floor_sigma": 1.0,
    }


def _example_trial_kwargs(idx: int, *, with_optional: bool) -> dict:
    """`with_optional=True`：`mel`/`audio`/`tof_ambient_*` 全部都在（形狀正確、
    長度為 0）。`with_optional=False`：全部省略。這兩個 trial 涵蓋「全有」
    與「全無」——選填欄位變多之後，這個對照組的意義沒有變。
    """
    kwargs = dict(
        label="_reject",
        tof_A=np.zeros((0, TOF_VALUES_DIM), dtype=np.float32),
        tof_B=np.zeros((0, TOF_VALUES_DIM), dtype=np.float32),
        tof_t_us=np.zeros((0,), dtype=np.int64),
        tof_valid_A=np.zeros((0, TOF_VALID_DIM), dtype=bool),
        tof_valid_B=np.zeros((0, TOF_VALID_DIM), dtype=bool),
        mic_rms=np.zeros((0,), dtype=np.float32),
        mic_peak=np.zeros((0,), dtype=np.int16),
        mic_t_us=np.zeros((0,), dtype=np.int64),
        wear_id=0, mode="example", valid_zone_ratio=0.0, drop_count=0,
        vad_start_us=-1, vad_end_us=-1, lip_onset_us=-1, voice_onset_us=-1,
        quality="ok",
    )
    if with_optional:
        kwargs["mel"] = np.zeros((0, MEL_BANDS), dtype=np.float32)
        kwargs["mel_t_us"] = np.zeros((0,), dtype=np.int64)
        kwargs["tof_ambient_A"] = np.zeros((0, TOF_VALID_DIM), dtype=np.float32)
        kwargs["tof_ambient_B"] = np.zeros((0, TOF_VALID_DIM), dtype=np.float32)
        kwargs["tof_ambient_t_us"] = np.zeros((0,), dtype=np.int64)
        kwargs["audio"] = np.zeros((0,), dtype=np.int16)
        kwargs["audio_t0_us"] = 0
    return kwargs


def build(path: str) -> None:
    with SessionWriter(path, _example_meta()) as w:
        # 一個選填欄位齊全的 trial，一個選填欄位省略的 trial，
        # 讓讀取端兩種情況都能練到。
        w.write_trial(0, **_example_trial_kwargs(0, with_optional=True))
        w.write_trial(1, **_example_trial_kwargs(1, with_optional=False))


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "session_example.h5"
    build(out_path)
    print(f"寫入完成: {out_path}")
