"""把算好的 log-mel 寫進既有 session HDF5 檔的 trial group（CONTRACTS.md §2）。

刻意做得很窄：只碰 `mel` 這個 dataset，不建立 trial group、不碰
`tof_*` / `mic_*` — 那是 B07 `SessionWriter` 的事。B07 完成後，這支函式
應該被併進 `SessionWriter`（或當作它寫完 mic 資料後呼叫的一個小步驟），
現在只是先讓 B14 能被驗收「特徵正確寫回 HDF5」。
"""
import h5py
import numpy as np


def write_mel_to_trial(h5_path, trial_idx: int, mel: np.ndarray) -> None:
    """把 (M, 40) float32 的 log-mel 寫進 `/trial_NNN/mel`。

    trial group 必須已存在（由 B07 的 writer 建立）；這支函式只負責 mel
    這個 dataset，若已存在會整份覆寫（B07 落地前的過渡行為，見上方說明）。
    """
    if mel.ndim != 2 or mel.shape[1] != 40:
        raise ValueError(f"mel 必須是 (M, 40)，收到 {mel.shape}")

    group_name = f"trial_{trial_idx:03d}"
    with h5py.File(h5_path, "a") as f:
        if group_name not in f:
            raise KeyError(f"{group_name} 在 {h5_path} 裡不存在，trial 要先建立")
        trial = f[group_name]
        if "mel" in trial:
            del trial["mel"]
        trial.create_dataset("mel", data=mel.astype(np.float32))
