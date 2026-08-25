"""B07 — HDF5 session writer（CONTRACTS.md 第 2 章，FROZEN 2026-08-26；
`mel`/`tof_ambient_*` 的時間軸與 `/meta` 校時欄位是後續調度決議追加的，
見變更紀錄）。

`SessionWriter` 只負責「把已經備妥的陣列，依 schema 正確地寫進 HDF5」
這一件事——不做特徵抽取、不做時間對齊、不做 trial 切分：

* 每個模態幾點取樣、取幾個點，是 `B06`（時間對齊器）跟上游 capture 決定的；
  `tof_A`/`tof_B` 是 `(T, 32)`、`mic_*` 是 `(M,)`、`mel`/`mel_t_us` 是
  `(F,)`、`tof_ambient_*` 是 `(Ta,)`——**這四個時間軸長度互不相同**
  （ToF/Mic/Mel/ambient 是頻率互不相同的獨立串流，§1.1.1），schema 沒有
  把它們對成同一個時間軸——那是 `analysis/` 的 D 軌拿到資料之後才做的事。
  **不可以用長度相等去驗證彼此，也不可以互相內插湊出「看起來對齊」的假資料。**
* `/meta` 的 `clock_slope`/`clock_offset`/`clock_residual_p95`（`B04` 回歸法）
  由呼叫端自己跑 `host/clock/align.py` 的 `ClockAligner.freeze()` 算好；
  `clock_drift_us`/`clock_drift_ppm`/`clock_sync_span_us`/`clock_sync_confirmed`
  （`B05` 兩點法）與 `session_start_*` 三個校時欄位在建構時就要有；
  `session_end_*` 三個要等 session 真的結束才存在，用 `finalize_session_end()`
  補寫，**不列進建構時必填**（見該方法的說明）。

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

from host.clock.align import SLOPE_TOLERANCE_PPM

SCHEMA_VERSION = 1

TOF_VALUES_DIM = 32   # [0:16] 距離 mm, [16:32] signal/100
TOF_VALID_DIM = 16
MEL_BANDS = 40

GZIP_LEVEL = 4

# session 一開始就量得到、建構 SessionWriter 時必填。
REQUIRED_META_KEYS = (
    "schema_version", "subject", "session_date", "wear_id", "mode",
    "distance_mm", "angle_deg", "ambient", "notes",
    "fw_sha", "proto_version", "tof_dim",
    "clock_slope", "clock_offset", "clock_residual_p95",
    "clock_drift_us", "clock_drift_ppm", "clock_sync_span_us", "clock_sync_confirmed",
    "session_start_device_us", "session_start_host_us", "session_start_rtt_min_us",
    "baseline_mu_A", "baseline_sigma_A", "baseline_mu_B", "baseline_sigma_B",
    "noise_floor_mu", "noise_floor_sigma",
)

# session 結束才量得到，`finalize_session_end()` 補寫，不在上面那份清單裡
# ——列進建構時必填的話，`SessionWriter` 連第一個 trial 都還沒寫就開不起來。
SESSION_END_META_KEYS = (
    "session_end_device_us", "session_end_host_us", "session_end_rtt_min_us",
)

# clock_drift_ppm（B05 兩點法）與 clock_slope 換算成 ppm（B04 回歸法）
# 應該互相印證；門檻沿用 B04 自己驗收條件用的 ±200ppm（host/clock/align.py），
# 同一個「時鐘多準才算準」的定義只在一個地方寫，不另外發明一個數字。
CLOCK_CROSS_CHECK_TOLERANCE_PPM = SLOPE_TOLERANCE_PPM

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

    def finalize_session_end(self, session_end_device_us, session_end_host_us,
                              session_end_rtt_min_us) -> None:
        """session 真正結束時呼叫，補寫 `/meta` 的三個收尾校時欄位。這三個
        值在 session 開始時根本不存在（校時要等結束才做第二次量測），所以
        不在建構時的 `REQUIRED_META_KEYS` 裡——不是可以晚點補的「選填」，
        是「這個時間點必然還沒發生」。

        沒呼叫這個就 `close()`（或 `with` 區塊正常/異常結束）完全沒問題，
        已寫入的 trial 一樣完整可讀，只是 `/meta` 少這三個欄位；`B19`／
        呼叫端要保證正常結束的 session 都會呼叫它，不是這裡的責任。
        """
        meta_group = self._file["meta"]
        meta_group.attrs["session_end_device_us"] = np.int64(session_end_device_us)
        meta_group.attrs["session_end_host_us"] = np.int64(session_end_host_us)
        meta_group.attrs["session_end_rtt_min_us"] = np.int64(session_end_rtt_min_us)
        self._file.flush()

    def _write_meta(self) -> None:
        meta_group = self._file.create_group("meta")
        for key in REQUIRED_META_KEYS:
            meta_group.attrs[key] = self._meta[key]

        # B05（兩點法漂移）與 B04（回歸法 slope）是兩個獨立方法量同一件事
        # ——對得上，兩邊才都可信；差太多代表其中一邊（或兩邊都）有問題，
        # 不應該悄悄放過。門檻沿用 B04 驗收條件的 ±200ppm。
        expected_ppm_from_slope = (self._meta["clock_slope"] - 1.0) * 1e6
        ppm_diff = abs(expected_ppm_from_slope - self._meta["clock_drift_ppm"])
        meta_group.attrs["clock_cross_check_ppm_diff"] = float(ppm_diff)
        meta_group.attrs["clock_cross_check_ok"] = bool(ppm_diff <= CLOCK_CROSS_CHECK_TOLERANCE_PPM)

    def write_trial(
        self, idx: int, *, label: str,
        tof_A, tof_B, tof_t_us, tof_valid_A, tof_valid_B,
        mic_rms, mic_peak, mic_t_us,
        mel=None, mel_t_us=None,
        tof_ambient_A=None, tof_ambient_B=None, tof_ambient_t_us=None,
        audio=None, audio_t0_us=None,
        wear_id, mode, valid_zone_ratio: float, drop_count: int,
        vad_start_us: int, vad_end_us: int, lip_onset_us: int, voice_onset_us: int,
        quality: str,
    ) -> None:
        """寫一個完整的 trial，寫完立刻 flush。`idx` 重複寫會覆蓋掉舊的
        （用 `del` 先移除舊 group 再建新的，避免 h5py 對已存在 group 報錯）。

        `mel`/`mel_t_us` 與 `tof_ambient_A`/`tof_ambient_B`/`tof_ambient_t_us`
        都是各自獨立的時間軸（`F`、`Ta`），**不會**、也**不可以**拿去跟
        `tof_t_us`/`mic_t_us` 比長度——那是兩種不同取樣率的獨立串流。
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

        mel_arr, mel_t_us_arr = _validate_mel(mel, mel_t_us)
        ambient_A_arr, ambient_B_arr, ambient_t_us_arr = _validate_ambient_trio(
            tof_ambient_A, tof_ambient_B, tof_ambient_t_us,
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
            _create_dataset(grp, "mel_t_us", mel_t_us_arr)
        if ambient_A_arr is not None:
            _create_dataset(grp, "tof_ambient_A", ambient_A_arr)
            _create_dataset(grp, "tof_ambient_B", ambient_B_arr)
            _create_dataset(grp, "tof_ambient_t_us", ambient_t_us_arr)
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


def _validate_mel(mel, mel_t_us):
    """`mel`/`mel_t_us` 成對出現、缺一不可（CONTRACTS.md §2）；`mel` 自己的
    幀數 `F` 只跟 `mel_t_us` 比，**不跟 `mic_t_us` 比**——兩者取樣率不同
    （`$F` 62.5Hz、`$M` 31.25Hz），長度本來就不會一樣。"""
    if (mel is None) != (mel_t_us is None):
        raise ValueError("mel 和 mel_t_us 必須同時提供或同時省略")
    if mel is None:
        return None, None

    mel_arr = np.asarray(mel, dtype=np.float32)
    mel_t_us_arr = np.asarray(mel_t_us, dtype=np.int64)
    if mel_arr.ndim != 2 or mel_arr.shape[1] != MEL_BANDS:
        raise ValueError(f"mel 必須是 (F, {MEL_BANDS})，收到 {mel_arr.shape}")
    if mel_t_us_arr.shape != (mel_arr.shape[0],):
        raise ValueError(
            f"mel_t_us 必須是 ({mel_arr.shape[0]},)（跟 mel 的幀數 F 一致），收到 {mel_t_us_arr.shape}"
        )
    return mel_arr, mel_t_us_arr


def _validate_ambient_trio(tof_ambient_A, tof_ambient_B, tof_ambient_t_us):
    """`tof_ambient_A`/`tof_ambient_B`/`tof_ambient_t_us` 三個全有或全無
    （CONTRACTS.md §1.1.3/§2）：ambient 是獨立的第五條串流（`Ta` 自己的
    時間軸，通常 1Hz），**不跟 `tof_t_us` 比長度**。無效 zone 跟
    `tof_A`/`tof_B` 一樣填 `NaN`。"""
    given = (tof_ambient_A is not None, tof_ambient_B is not None, tof_ambient_t_us is not None)
    if len(set(given)) != 1:
        raise ValueError("tof_ambient_A / tof_ambient_B / tof_ambient_t_us 必須同時提供或同時省略")
    if tof_ambient_A is None:
        return None, None, None

    t_us_arr = np.asarray(tof_ambient_t_us, dtype=np.int64)
    ta = t_us_arr.shape[0]
    a_arr = _to_float32_nan_safe(tof_ambient_A)
    b_arr = _to_float32_nan_safe(tof_ambient_B)
    for name, arr in (("tof_ambient_A", a_arr), ("tof_ambient_B", b_arr)):
        if arr.shape != (ta, TOF_VALID_DIM):
            raise ValueError(f"{name} 必須是 ({ta}, {TOF_VALID_DIM})（跟 tof_ambient_t_us 長度一致），收到 {arr.shape}")
    return a_arr, b_arr, t_us_arr
