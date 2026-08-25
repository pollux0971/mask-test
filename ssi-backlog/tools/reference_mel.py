#!/usr/bin/env python3
"""
reference_mel.py — CONTRACTS.md §3 特徵向量規格的標準答案產生器

用途:
    A 軌（A11/A12 裝置端 int16 Mel）與 D 軌（辨識引擎）都對著同一份
    librosa 標準答案做交叉驗證，避免 Slaney/HTK 或窗函數週期性這類
    細節在兩邊各自實作時悄悄長歪。

歸屬:
    本檔是 CONTRACTS.md §3.4 定義的契約產物，由 T 軌維護。
    A/B/D 三軌唯讀引用，不得各自複製一份修改 —— 複製出去的版本
    會跟這裡的凍結參數逐漸漂移，等於重新製造「規格不一致」的原始問題。
    要改參數，先改 CONTRACTS.md §3.1/§3.2，再回來改這裡。

用法:
    python3 reference_mel.py some.wav                  # 印出全幀摘要統計
    python3 reference_mel.py some.wav --frame 10        # 印出第 10 幀的完整向量
    python3 reference_mel.py some.wav --csv out.csv     # 存成 CSV 供比對腳本讀

需求: pip install librosa numpy soundfile
"""
import argparse
import sys

import numpy as np

try:
    import librosa
except ImportError:
    print("需要 librosa：pip install librosa numpy soundfile", file=sys.stderr)
    raise

# ── 以下參數凍結於 CONTRACTS.md §3.1，不要在此檔外另行定義一份 ──
SR = 16000
N_FFT = 512          # 32 ms @ 16 kHz
HOP_LENGTH = 256     # 16 ms @ 16 kHz -> 62.5 Hz 幀率
N_MELS = 40
FMIN = 80.0
FMAX = 8000.0
LOG_FLOOR = 1e-10


def compute_log_mel(y, sr=SR):
    """回傳 (n_frames, N_MELS) float32 log-mel。

    center=False：裝置端即時分幀沒有前後 padding 可用（沒有未來的樣本），
    主機端標準答案必須用同一種切法逐幀對齊，比對才有意義；
    若用 librosa 預設的 center=True，幀的邊界會對不上裝置端輸出。
    """
    if sr != SR:
        raise ValueError(f"預期取樣率 {SR} Hz，收到 {sr} Hz")

    window = librosa.filters.get_window("hann", N_FFT, fftbins=True)  # periodic Hann

    stft = librosa.stft(
        y.astype(np.float32),
        n_fft=N_FFT,
        hop_length=HOP_LENGTH,
        window=window,
        center=False,
    )
    power = np.abs(stft) ** 2

    mel_fb = librosa.filters.mel(
        sr=sr,
        n_fft=N_FFT,
        n_mels=N_MELS,
        fmin=FMIN,
        fmax=FMAX,
        htk=False,       # ⚠ Slaney，不是 HTK —— 見 CONTRACTS.md §3.1
        norm="slaney",
    )

    mel_power = mel_fb @ power                       # (N_MELS, n_frames)
    log_mel = np.log10(np.maximum(mel_power, LOG_FLOOR))
    return log_mel.T.astype(np.float32)              # (n_frames, N_MELS)


def to_device_int16(log_mel):
    """裝置端傳輸格式：int16 = round(log_mel * 100)，見 CONTRACTS.md §3.1。"""
    return np.round(log_mel * 100).astype(np.int16)


def main():
    ap = argparse.ArgumentParser(description="產生 CONTRACTS.md §3 的 mel 標準答案")
    ap.add_argument("wav", help="16 kHz mono WAV 檔路徑")
    ap.add_argument("--frame", type=int, default=None,
                     help="只印出第幾幀（0-based）的完整向量，預設印全幀摘要統計")
    ap.add_argument("--csv", help="把完整 log-mel 矩陣存成 CSV（每列一幀）")
    args = ap.parse_args()

    y, sr = librosa.load(args.wav, sr=None, mono=True)
    log_mel = compute_log_mel(y, sr)
    device_int16 = to_device_int16(log_mel)

    print(f"輸入: {args.wav}  sr={sr}  samples={len(y)}")
    print(f"輸出: {log_mel.shape[0]} 幀 x {N_MELS} mel bands")

    if args.frame is not None:
        i = args.frame
        if not (0 <= i < log_mel.shape[0]):
            sys.exit(f"frame {i} 超出範圍 [0, {log_mel.shape[0]})")
        print(f"\n--- frame {i} ---")
        print("log_mel   :", np.array2string(log_mel[i], precision=4, max_line_width=200))
        print("device_i16:", np.array2string(device_int16[i], max_line_width=200))
    else:
        print("\nlog_mel 統計: min={:.4f} max={:.4f} mean={:.4f}".format(
            log_mel.min(), log_mel.max(), log_mel.mean()))
        print("device_int16 統計: min={} max={}".format(
            device_int16.min(), device_int16.max()))

    if args.csv:
        np.savetxt(args.csv, log_mel, delimiter=",", fmt="%.6f")
        print(f"\n完整 log-mel 矩陣已寫入 {args.csv}")


if __name__ == "__main__":
    main()
