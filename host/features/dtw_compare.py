"""比對用的純函式：DTW 距離（同詞 vs 跨詞）與逐幀相關係數（host vs device）。

`dtw_distance` 給 B14 自己的驗收條件用（同詞 5 次錄音彼此距離應顯著小於
跨詞距離）。`pearson_corr` 是給未來 A12 完成後的比對工具用的（host 端
log-mel 跟裝置端 dequantize 回來的 log-mel，逐幀比對相關係數）。
"""
import numpy as np
from librosa.sequence import dtw


def dtw_distance(a: np.ndarray, b: np.ndarray) -> float:
    """兩個 (frames, n_mels) log-mel 矩陣的正規化 DTW 距離（除以路徑長度，
    讓不同長度的錄音也能比較）。距離越小代表兩段語音的音型越接近。"""
    if a.ndim != 2 or b.ndim != 2 or a.shape[1] != b.shape[1]:
        raise ValueError(f"shape 不相容: {a.shape} vs {b.shape}")
    cost, wp = dtw(X=a.T, Y=b.T, metric="euclidean")
    return float(cost[-1, -1] / len(wp))


def pearson_corr(a: np.ndarray, b: np.ndarray) -> float:
    """兩個形狀相同的 log-mel 矩陣，攤平後的皮爾森相關係數。"""
    if a.shape != b.shape:
        raise ValueError(f"shape 不一致: {a.shape} vs {b.shape}")
    af = a.reshape(-1).astype(np.float64)
    bf = b.reshape(-1).astype(np.float64)
    return float(np.corrcoef(af, bf)[0, 1])
