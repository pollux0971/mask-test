#!/usr/bin/env python3
"""產生一個結構正確、內容為空的 session HDF5 檔，供 D 軌對著 schema 開發。

Schema 定義見 CONTRACTS.md 第 2 章。此檔只建立正確的 group / dataset /
attrs 骨架，不寫入任何真實量測值（除了 shape 為 0 的空陣列）。

用法：
    python3 schema_example.py [輸出路徑]

預設輸出路徑為 ./session_example.h5。
"""
import sys

import h5py
import numpy as np

SCHEMA_VERSION = 1

TOF_ZONE_DIM = 32  # [0:16] 距離 mm, [16:32] signal/100
TOF_VALID_DIM = 16
MEL_BANDS = 40


def _make_meta(f: h5py.File) -> None:
    meta = f.create_group("meta")
    attrs = meta.attrs

    attrs["schema_version"] = SCHEMA_VERSION
    attrs["subject"] = "example_subject"
    attrs["session_date"] = "1970-01-01"
    attrs["wear_id"] = 0
    attrs["mode"] = "example"
    attrs["distance_mm"] = 0.0
    attrs["angle_deg"] = 0.0
    attrs["ambient"] = ""
    attrs["notes"] = ""

    attrs["fw_sha"] = "0" * 40
    attrs["proto_version"] = 2
    attrs["tof_dim"] = 8

    # B04 產出，range/session 校時結果
    attrs["clock_slope"] = 1.0
    attrs["clock_offset"] = 0.0
    attrs["clock_residual_p95"] = 0.0

    # 同一次戴上時錄的 baseline，每顆感測器各一份
    attrs["baseline_mu_A"] = np.zeros(TOF_ZONE_DIM, dtype=np.float32)
    attrs["baseline_sigma_A"] = np.ones(TOF_ZONE_DIM, dtype=np.float32)
    attrs["baseline_mu_B"] = np.zeros(TOF_ZONE_DIM, dtype=np.float32)
    attrs["baseline_sigma_B"] = np.ones(TOF_ZONE_DIM, dtype=np.float32)

    attrs["noise_floor_mu"] = 0.0
    attrs["noise_floor_sigma"] = 1.0


def _make_trial(f: h5py.File, idx: int, *, n_tof: int = 0, n_mic: int = 0,
                 n_audio: int = 0, with_mel: bool = True,
                 with_audio: bool = True) -> None:
    grp = f.create_group(f"trial_{idx:03d}")

    grp.create_dataset("tof_A", shape=(n_tof, TOF_ZONE_DIM), dtype=np.float32)
    grp.create_dataset("tof_B", shape=(n_tof, TOF_ZONE_DIM), dtype=np.float32)
    grp.create_dataset("tof_t_us", shape=(n_tof,), dtype=np.int64)
    grp.create_dataset("tof_valid_A", shape=(n_tof, TOF_VALID_DIM), dtype=np.bool_)
    grp.create_dataset("tof_valid_B", shape=(n_tof, TOF_VALID_DIM), dtype=np.bool_)

    grp.create_dataset("mic_rms", shape=(n_mic,), dtype=np.float32)
    grp.create_dataset("mic_peak", shape=(n_mic,), dtype=np.int16)
    grp.create_dataset("mic_t_us", shape=(n_mic,), dtype=np.int64)

    if with_mel:
        grp.create_dataset("mel", shape=(n_mic, MEL_BANDS), dtype=np.float32)

    if with_audio:
        grp.create_dataset("audio", shape=(n_audio,), dtype=np.int16)
        grp.attrs["audio_t0_us"] = np.int64(0)

    attrs = grp.attrs
    attrs["label"] = "_reject"
    attrs["trial_idx"] = idx
    attrs["wear_id"] = 0
    attrs["mode"] = "example"
    attrs["valid_zone_ratio"] = 0.0
    attrs["drop_count"] = 0
    attrs["vad_start_us"] = np.int64(-1)
    attrs["vad_end_us"] = np.int64(-1)
    attrs["lip_onset_us"] = np.int64(-1)
    attrs["voice_onset_us"] = np.int64(-1)
    attrs["quality"] = "ok"


def build(path: str) -> None:
    with h5py.File(path, "w") as f:
        _make_meta(f)
        # 一個選填欄位齊全的 trial，一個選填欄位省略的 trial，
        # 讓讀取端兩種情況都能練到。
        _make_trial(f, 0, with_mel=True, with_audio=True)
        _make_trial(f, 1, with_mel=False, with_audio=False)


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "session_example.h5"
    build(out_path)
    print(f"寫入完成: {out_path}")
