"""特徵組裝與固定長度重採樣。

規格見 `stories/D-analysis/D03.md`；串接順序凍結於 CONTRACTS.md §3.3
（104 維／幀：`[0:32] tof_A`、`[32:64] tof_B`、`[64:104] mel`）。

**前提：跨模態時間對齊不在本模組範圍內。** 傳進來的 `tof_a_z` / `tof_b_z` /
`mel_cmn` / `t_us` 必須已經由 B06（多模態時間對齊器）對到同一組共用幀
（ToF @30 Hz 與 Mel @62.5 Hz 本來就不同步，B06 負責把它們對齊）。本模組
只驗證四者幀數是否一致，不做任何猜測性的對齊或內插去湊出一致的幀數。

固定長度版與原始長度版同時保留：
    - 固定長度（T=24）給 D04 的餘弦距離用——餘弦距離要求等長向量。
    - 原始長度給 D05 的 DTW 用——DTW 正是要用原始時間軸做時間扭曲比對，
      重採樣成固定長度會抹掉這個資訊，所以不能只留一種。
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from analysis.features.tof_features import TOF_DIM

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from reference_mel import N_MELS as MEL_BANDS  # noqa: E402  單一事實來源：CONTRACTS.md §3.1

FEATURE_DIM = 2 * TOF_DIM + MEL_BANDS  # 104
DEFAULT_T_FIXED = 24


@dataclass
class FeatureSeq:
    data: np.ndarray       # (T_fixed, 104) 固定長度版，供 D04 餘弦距離使用
    slices: dict           # {"tof": slice(0,64), "mel": slice(64,104)}
    t_us: np.ndarray       # (T_fixed,) 固定長度版對應的時間戳（線性內插）
    data_raw: np.ndarray   # (T_raw, 104) 原始長度版，供 D05 DTW 使用
    t_us_raw: np.ndarray   # (T_raw,) 原始長度版時間戳（等於輸入的 t_us）


def resample_fixed_length(x, t_us, t_fixed=DEFAULT_T_FIXED):
    """把 (T, D) 序列線性重採樣到 (t_fixed, D)，t_us 一併內插。

    重採樣是在「幀序」上等距取樣，不是在真實時間上——四個輸入已經是
    共用同一組對齊過的幀，這裡只需要把幀數從 T 壓縮/擴張到 t_fixed。
    """
    x = np.asarray(x, dtype=np.float64)
    t_us = np.asarray(t_us, dtype=np.float64)
    T = x.shape[0]
    if T < 2:
        raise ValueError(f"重採樣需要至少 2 幀原始資料，收到 {T}")

    src_idx = np.linspace(0, T - 1, T)
    dst_idx = np.linspace(0, T - 1, t_fixed)

    x_resampled = np.empty((t_fixed, x.shape[1]), dtype=np.float64)
    for d in range(x.shape[1]):
        x_resampled[:, d] = np.interp(dst_idx, src_idx, x[:, d])
    t_us_resampled = np.interp(dst_idx, src_idx, t_us)

    return x_resampled, t_us_resampled


def assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=DEFAULT_T_FIXED):
    """依 CONTRACTS.md §3.3 順序串接 ToF_A / ToF_B / Mel，回傳 `FeatureSeq`。

    tof_a_z, tof_b_z: (T, 32) D01 `tof_features()` 的輸出（z-score 後）
    mel_cmn:          (T, 40) D02 `mel_features()` 的輸出（CMN 後）
    t_us:             (T,) 三者共用的時間戳（已由 B06 對齊）

    四個輸入的 T 必須相等；不相等代表對齊沒做好，直接丟例外，不猜測。
    """
    tof_a_z = np.asarray(tof_a_z, dtype=np.float64)
    tof_b_z = np.asarray(tof_b_z, dtype=np.float64)
    mel_cmn = np.asarray(mel_cmn, dtype=np.float64)
    t_us = np.asarray(t_us)

    if tof_a_z.shape[1] != TOF_DIM:
        raise ValueError(f"tof_a_z 最後一維應為 {TOF_DIM}，收到 {tof_a_z.shape[1]}")
    if tof_b_z.shape[1] != TOF_DIM:
        raise ValueError(f"tof_b_z 最後一維應為 {TOF_DIM}，收到 {tof_b_z.shape[1]}")
    if mel_cmn.shape[1] != MEL_BANDS:
        raise ValueError(f"mel_cmn 最後一維應為 {MEL_BANDS}，收到 {mel_cmn.shape[1]}")

    T = tof_a_z.shape[0]
    if not (tof_b_z.shape[0] == mel_cmn.shape[0] == t_us.shape[0] == T):
        raise ValueError(
            "tof_a_z / tof_b_z / mel_cmn / t_us 幀數必須相等（假設已由 B06 對齊到同一組幀），"
            f"收到 {T} / {tof_b_z.shape[0]} / {mel_cmn.shape[0]} / {t_us.shape[0]}"
        )

    data_raw = np.concatenate([tof_a_z, tof_b_z, mel_cmn], axis=1)  # (T, 104)
    slices = {"tof": slice(0, 2 * TOF_DIM), "mel": slice(2 * TOF_DIM, FEATURE_DIM)}

    data_fixed, t_us_fixed = resample_fixed_length(data_raw, t_us, t_fixed)

    return FeatureSeq(
        data=data_fixed,
        slices=slices,
        t_us=t_us_fixed,
        data_raw=data_raw,
        t_us_raw=t_us,
    )


def fit_pca(frames_2d, n_components=2):
    """對一批攤平的 (N, 104) frame 擬合 PCA，供 C10 做即時軌跡投影。

    frames_2d 建議用 baseline + 一部分 trial 的 frame 疊在一起（見 D03.md）。
    """
    from sklearn.decomposition import PCA

    pca = PCA(n_components=n_components)
    pca.fit(np.asarray(frames_2d, dtype=np.float64))
    return pca


def project_pca(pca, data):
    """把 (T, 104) 序列逐幀投影到 PCA 空間，回傳 (T, n_components)。"""
    return pca.transform(np.asarray(data, dtype=np.float64))


def save_pca(pca, path):
    """存檔供 C10 跨 session 載入。"""
    import joblib

    joblib.dump(pca, path)


def load_pca(path):
    import joblib

    return joblib.load(path)
