"""ToF 特徵：per-zone z-score 正規化與活躍 zone 篩選。

規格凍結於 CONTRACTS.md §3.2「ToF 特徵」。

資料形狀（單一感測器 A 或 B，皆對應 CONTRACTS.md §2 的 HDF5 schema）：
    tof            (T, 32) float  — [0:16] 距離 mm, [16:32] signal_per_spad/100
    valid          (T, 16) bool   — 對應 tof_valid_A / tof_valid_B，
                                    每個 zone 一個值，同時套用到該 zone
                                    的距離與 signal 兩個通道（同一次量測，
                                    有效性跟著 zone 走，不分距離/signal）
    baseline_mu    (32,) float    — /meta 的 baseline_mu_A 或 baseline_mu_B
    baseline_sigma (32,) float    — /meta 的 baseline_sigma_A 或 baseline_sigma_B

SNR 本身的計算不在本模組範圍內（D11 負責）；本模組只負責用給定的
SNR 陣列做門檻篩選，回傳通過的 zone 索引。
"""
import numpy as np

N_ZONES = 16          # 每顆感測器的 zone 數
TOF_DIM = 32           # 每顆感測器的特徵通道數（距離 16 + signal 16）
SIGMA_FLOOR = 1e-3     # baseline 期間可能完全穩定（sigma≈0），除下去會爆炸
DEFAULT_SNR_THRESHOLD = 2.0


def tof_features(tof, valid, baseline_mu, baseline_sigma, active_zones=None):
    """把原始 ToF 通道轉成 per-zone z-score 特徵。

    無效 zone 填 0（= z 空間裡「等於基線」，最中性的假設；不要填距離的
    平均值，那會製造假訊號）。

    active_zones: 可選，zone 索引陣列（值域 0..N_ZONES-1）。給定時只回傳
    這些 zone 的距離+signal 通道；為 None 時回傳全部 32 個通道。

    回傳 (T, 32) 或 (T, 2*len(active_zones)) 的 float 陣列。
    """
    tof = np.asarray(tof, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    baseline_mu = np.asarray(baseline_mu, dtype=np.float64)
    baseline_sigma = np.asarray(baseline_sigma, dtype=np.float64)

    if tof.shape[-1] != TOF_DIM:
        raise ValueError(f"tof 最後一維應為 {TOF_DIM}，收到 {tof.shape[-1]}")
    if valid.shape[-1] != N_ZONES:
        raise ValueError(f"valid 最後一維應為 {N_ZONES}，收到 {valid.shape[-1]}")

    sigma_safe = np.maximum(baseline_sigma, SIGMA_FLOOR)
    z = (tof - baseline_mu) / sigma_safe

    valid32 = np.concatenate([valid, valid], axis=-1)
    z = np.where(valid32, z, 0.0)

    if active_zones is not None:
        active_zones = np.asarray(active_zones)
        channel_idx = np.concatenate([active_zones, active_zones + N_ZONES])
        z = z[..., channel_idx]

    return z


def active_zone_mask(snr, threshold=DEFAULT_SNR_THRESHOLD):
    """回傳 (N_ZONES,) 布林遮罩，True 表示該 zone 的 SNR 通過門檻。

    snr: (N_ZONES,) 由 D11 計算後傳入，這裡只做門檻比較。
    """
    snr = np.asarray(snr, dtype=np.float64)
    if snr.shape[-1] != N_ZONES:
        raise ValueError(f"snr 最後一維應為 {N_ZONES}，收到 {snr.shape[-1]}")
    return snr > threshold


def active_zone_indices(snr, threshold=DEFAULT_SNR_THRESHOLD):
    """回傳通過 SNR 門檻的 zone 索引陣列，供視覺化/報告使用。"""
    return np.flatnonzero(active_zone_mask(snr, threshold))
