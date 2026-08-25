#!/usr/bin/env python3
"""比對兩份 log-mel 特徵（來源可以是 .wav / .npy / .csv 任意組合）。

歸屬：由 B 軌 `B14` 產出、維護（見 `tools/OWNER.md`），放在這裡是因為
A12（裝置端 log-Mel）與 D 軌都要用它比對「裝置端輸出 vs host 端標準答案」
——見 `ssi-backlog/CONTRACTS.md` §3.4。比對邏輯本身在
`host/features/mel_compare.py`，這支只是薄的 CLI 包裝，方便獨立呼叫。

用法：
    python3 tools/compare_mel.py device_mel.npy host_mel.csv
    python3 tools/compare_mel.py a.wav b.wav

驗收門檻（CONTRACTS.md §3.1 / A12）：相關係數 > 0.95（僅在兩邊幀數相同時計算）。
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from host.features.mel_compare import compare_log_mels, load_log_mel  # noqa: E402

CORR_THRESHOLD = 0.95


def main():
    ap = argparse.ArgumentParser(description="比對兩份 log-mel（.wav / .npy / .csv）")
    ap.add_argument("a", help="第一份 log-mel")
    ap.add_argument("b", help="第二份 log-mel")
    args = ap.parse_args()

    mel_a = load_log_mel(args.a)
    mel_b = load_log_mel(args.b)
    result = compare_log_mels(mel_a, mel_b)

    print(f"A: {args.a}  shape={mel_a.shape}")
    print(f"B: {args.b}  shape={mel_b.shape}")
    print(f"DTW 距離: {result['dtw_distance']:.4f}")

    if result["pearson_corr"] is not None:
        corr = result["pearson_corr"]
        verdict = "PASS" if corr > CORR_THRESHOLD else "FAIL"
        print(f"相關係數: {corr:.4f}（門檻 > {CORR_THRESHOLD}） -> {verdict}")
        sys.exit(0 if corr > CORR_THRESHOLD else 1)
    else:
        print(f"幀數不同（{mel_a.shape[0]} vs {mel_b.shape[0]}），跳過相關係數，只看 DTW 距離")


if __name__ == "__main__":
    main()
