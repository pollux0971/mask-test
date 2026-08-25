"""B07 — HDF5 session writer（CONTRACTS.md 第 2 章，FROZEN 2026-08-26）。

`SessionWriter` 只負責「把已經備妥的陣列，依 schema 正確地寫進 HDF5」
這一件事——不做特徵抽取、不做時間對齊、不做 trial 切分：

* 每個模態幾點取樣、取幾個點，是 `B06`（時間對齊器）跟上游 capture 決定的；
  `tof_A`/`tof_B` 是 `(T, 32)`、`mic_*` 是 `(M,)`，**T 跟 M 本來就不同**
  （ToF/Mic 是不同頻率的原始串流，schema 沒有把它們對成同一個時間軸——
  那是 `analysis/` 的 D 軌拿到資料之後才做的事）。
* `/meta` 的 `clock_slope`/`clock_offset`/`clock_residual_p95` 由呼叫端
  自己跑 `host/clock/align.py` 的 `ClockAligner.freeze()` 算好，這裡只管
  寫進 attrs。

**增量寫入 = 每個 trial 結束就 flush，不是每個 sample 都 flush。**
`write_trial()` 一次吃一個 trial 完整的陣列（呼叫端已經知道這個 trial
錄了多長），寫完立刻 `f.flush()`——這樣 session 中途當掉，已經寫完的
trial 仍然是一份結構完整、可以直接用 `h5py.File(path, "r")` 讀出來的檔案，
不會因為最後一個沒寫完的 trial 拖累前面的。

`compression="gzip", compression_opts=4`：ToF 資料重複性高，壓縮比通常
3–5×，level 4 的 CPU 成本可忽略（story 原文的判斷，這裡直接照做）。
"""
from __future__ import annotations

import h5py
import numpy as np

SCHEMA_VERSION = 1

TOF_VALUES_DIM = 32   # [0:16] 距離 mm, [16:32] signal/100
TOF_VALID_DIM = 16
MEL_BANDS = 40

GZIP_LEVEL = 4

REQUIRED_META_KEYS = (
    "schema_version", "subject", "session_date", "wear_id", "mode",
    "distance_mm", "angle_deg", "ambient", "notes",
    "fw_sha", "proto_version", "tof_dim",
    "clock_slope", "clock_offset", "clock_residual_p95",
    "baseline_mu_A", "baseline_sigma_A", "baseline_mu_B", "baseline_sigma_B",
    "noise_floor_mu", "noise_floor_sigma",
)

REQUIRED_TRIAL_ATTRS = (
    "wear_id", "mode", "valid_zone_ratio", "drop_count",
    "vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us", "quality",
)

VALID_QUALITY_VALUES = ("ok", "low", "rejected")


class SessionWriter:
    """`with SessionWriter(path, meta) as w: w.write_trial(idx=0, label="五", ...)`

    建構時就把 `/meta` 寫進去並立刻 flush，所以 `meta` 缺欄位會在最早的
    時間點就報錯，而不是等第一個 trial 寫完才發現。
    """

    def __init__(self, path, meta: dict):
        missing = [k for k in REQUIRED_META_KEYS if k not in meta]
        if missing:
            raise ValueError(f"meta 缺少必填欄位: {missing}")
        if meta["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"schema_version 必須是 {SCHEMA_VERSION}，收到 {meta['schema_version']!r}")

        self._path = path
        self._meta = meta
        self._file = None
        self._trial_indices_written = set()

    def __enter__(self) -> "SessionWriter":
        self._file = h5py.File(self._path, "w")
        self._write_meta()
        self._file.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _write_meta(self) -> None:
        meta_group = self._file.create_group("meta")
        for key in REQUIRED_META_KEYS:
            meta_group.attrs[key] = self._meta[key]

    def write_trial(
        self, idx: int, *, label: str,
        tof_A, tof_B, tof_t_us, tof_valid_A, tof_valid_B,
        mic_rms, mic_peak, mic_t_us,
        mel=None, audio=None, audio_t0_us=None,
        wear_id, mode, valid_zone_ratio: float, drop_count: int,
        vad_start_us: int, vad_end_us: int, lip_onset_us: int, voice_onset_us: int,
        quality: str,
    ) -> None:
        """寫一個完整的 trial，寫完立刻 flush。`idx` 重複寫會覆蓋掉舊的
        （用 `del` 先移除舊 group 再建新的，避免 h5py 對已存在 group 報錯）。
        """
        if quality not in VALID_QUALITY_VALUES:
            raise ValueError(f"quality 必須是 {VALID_QUALITY_VALUES} 之一，收到 {quality!r}")

        tof_A = _to_float32_nan_safe(tof_A)
        tof_B = _to_float32_nan_safe(tof_B)
        tof_t_us = np.asarray(tof_t_us, dtype=np.int64)
        tof_valid_A = np.asarray(tof_valid_A, dtype=np.bool_)
        tof_valid_B = np.asarray(tof_valid_B, dtype=np.bool_)
        mic_rms = np.asarray(mic_rms, dtype=np.float32)
        mic_peak = np.asarray(mic_peak, dtype=np.int16)
        mic_t_us = np.asarray(mic_t_us, dtype=np.int64)

        _validate_tof_shapes(tof_A, tof_B, tof_t_us, tof_valid_A, tof_valid_B)
        _validate_mic_shapes(mic_rms, mic_peak, mic_t_us)

        mel_arr = None
        if mel is not None:
            mel_arr = np.asarray(mel, dtype=np.float32)
            if mel_arr.ndim != 2 or mel_arr.shape[1] != MEL_BANDS:
                raise ValueError(f"mel 必須是 (M, {MEL_BANDS})，收到 {mel_arr.shape}")
            if mel_arr.shape[0] != mic_t_us.shape[0]:
                raise ValueError(
                    f"mel 的幀數（{mel_arr.shape[0]}）必須跟 mic_t_us 的長度（{mic_t_us.shape[0]}）一致"
                )

        audio_arr = None
        if audio is not None:
            audio_arr = np.asarray(audio, dtype=np.int16)
            if audio_arr.ndim != 1:
                raise ValueError(f"audio 必須是一維，收到 shape {audio_arr.shape}")
            if audio_t0_us is None:
                raise ValueError("給了 audio 就必須給 audio_t0_us")

        group_name = f"trial_{idx:03d}"
        if group_name in self._file:
            del self._file[group_name]
        grp = self._file.create_group(group_name)

        _create_dataset(grp, "tof_A", tof_A)
        _create_dataset(grp, "tof_B", tof_B)
        _create_dataset(grp, "tof_t_us", tof_t_us)
        _create_dataset(grp, "tof_valid_A", tof_valid_A)
        _create_dataset(grp, "tof_valid_B", tof_valid_B)
        _create_dataset(grp, "mic_rms", mic_rms)
        _create_dataset(grp, "mic_peak", mic_peak)
        _create_dataset(grp, "mic_t_us", mic_t_us)
        if mel_arr is not None:
            _create_dataset(grp, "mel", mel_arr)
        if audio_arr is not None:
            _create_dataset(grp, "audio", audio_arr)
            grp.attrs["audio_t0_us"] = np.int64(audio_t0_us)

        grp.attrs["label"] = label
        grp.attrs["trial_idx"] = idx
        grp.attrs["wear_id"] = wear_id
        grp.attrs["mode"] = mode
        grp.attrs["valid_zone_ratio"] = float(valid_zone_ratio)
        grp.attrs["drop_count"] = int(drop_count)
        grp.attrs["vad_start_us"] = np.int64(vad_start_us)
        grp.attrs["vad_end_us"] = np.int64(vad_end_us)
        grp.attrs["lip_onset_us"] = np.int64(lip_onset_us)
        grp.attrs["voice_onset_us"] = np.int64(voice_onset_us)
        grp.attrs["quality"] = quality

        self._trial_indices_written.add(idx)
        self._file.flush()


def _to_float32_nan_safe(array_like) -> np.ndarray:
    """`tof_A`/`tof_B` 的來源（例如 `host/capture/protocol.py` 的解析結果、
    `host/align/aligner.py` 的 `TofSample.values`）用 Python `None` 標記無效
    zone——這是刻意的，跟 T02「無效值不要塞 -1」是同一個原則的延伸。但
    HDF5 的 `float32` dataset 沒有 `None` 這個值，所以這裡把 `None` 轉成
    `NaN`：跟 `-1` 不同，`NaN` 參與任何算術都會讓結果變成 `NaN` 而不是
    悄悄給一個看似合理的錯誤數字，忘記檢查 `tof_valid_*` 的下游會立刻
    看到異常，而不是像 `-1` 那樣被誤當成一個「很近的距離」。
    """
    arr = np.asarray(array_like, dtype=object)
    arr = np.where(arr == None, np.nan, arr)  # noqa: E711 -- identity check needed for object dtype
    return arr.astype(np.float32)


def _create_dataset(grp, name, array: np.ndarray):
    """長度 0 的軸不能用 h5py 的 chunked/gzip（chunk shape 不能有 0 維），
    這種邊界情況（例如一個 trial 錄到 0 幀）就退回不壓縮的一般 dataset。"""
    if array.shape and array.shape[0] == 0:
        grp.create_dataset(name, data=array)
    else:
        grp.create_dataset(name, data=array, compression="gzip", compression_opts=GZIP_LEVEL)


def _validate_tof_shapes(tof_A, tof_B, tof_t_us, tof_valid_A, tof_valid_B) -> None:
    t = tof_t_us.shape[0]
    for name, arr, expected_shape in (
        ("tof_A", tof_A, (t, TOF_VALUES_DIM)),
        ("tof_B", tof_B, (t, TOF_VALUES_DIM)),
        ("tof_valid_A", tof_valid_A, (t, TOF_VALID_DIM)),
        ("tof_valid_B", tof_valid_B, (t, TOF_VALID_DIM)),
    ):
        if arr.shape != expected_shape:
            raise ValueError(f"{name} 必須是 {expected_shape}（跟 tof_t_us 長度一致），收到 {arr.shape}")


def _validate_mic_shapes(mic_rms, mic_peak, mic_t_us) -> None:
    m = mic_t_us.shape[0]
    for name, arr in (("mic_rms", mic_rms), ("mic_peak", mic_peak)):
        if arr.shape != (m,):
            raise ValueError(f"{name} 必須是 ({m},)（跟 mic_t_us 長度一致），收到 {arr.shape}")
