"""給 `tools/compare_mel.py` 用的比對邏輯：讀兩份 log-mel（來源可以是
wav / npy / csv 任一種），算 DTW 距離與（幀數相同時的）相關係數。

CLI 包裝本身在根目錄 `tools/compare_mel.py`；這裡只放純函式方便寫測試，
也讓其他軌道（例如 D 軌）需要同樣的比對邏輯時可以直接 import，不用重寫。
"""
from pathlib import Path

import numpy as np

from host.features.dtw_compare import dtw_distance, pearson_corr
from host.features.mel_pipeline import wav_to_log_mel

N_MELS = 40


def load_log_mel(path) -> np.ndarray:
    """讀一份 (frames, 40) 的 log-mel，依副檔名決定讀法：
    `.wav` 現場算、`.npy` 直接載、`.csv` 逐行載入（`reference_mel.py --csv`
    輸出的格式）。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".wav":
        arr = wav_to_log_mel(path)
    elif suffix == ".npy":
        arr = np.load(path)
    elif suffix == ".csv":
        arr = np.loadtxt(path, delimiter=",")
    else:
        raise ValueError(f"不支援的副檔名: {suffix}（支援 .wav / .npy / .csv）")

    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != N_MELS:
        raise ValueError(f"{path} 的 log-mel 必須是 (frames, {N_MELS})，讀到 {arr.shape}")
    return arr


def compare_log_mels(a: np.ndarray, b: np.ndarray) -> dict:
    """DTW 距離永遠算得出來（允許不同長度）；相關係數只有在兩邊幀數一致
    （同一段輸入分別跑兩條管線）時才有意義，否則回傳 None。"""
    result = {
        "n_frames_a": int(a.shape[0]),
        "n_frames_b": int(b.shape[0]),
        "dtw_distance": dtw_distance(a, b),
        "pearson_corr": pearson_corr(a, b) if a.shape == b.shape else None,
    }
    return result
