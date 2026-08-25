"""B14 — WAV -> log-Mel，主機端備援路線（CONTRACTS.md §3）。

參數本身不在這裡定義：一律呼叫 `ssi-backlog/tools/reference_mel.py`（T03 凍結
的標準答案實作），確保 B14（主機路線）跟 A12（裝置路線）永遠對著同一份參數，
不會在兩邊各自實作時悄悄長歪。
"""
import sys
import time
from pathlib import Path

import librosa
import numpy as np

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from reference_mel import SR, compute_log_mel  # noqa: E402


def load_wav_mono16k(wav_path) -> np.ndarray:
    y, sr = librosa.load(str(wav_path), sr=None, mono=True)
    if sr != SR:
        raise ValueError(f"預期取樣率 {SR} Hz，{wav_path} 是 {sr} Hz")
    return y


def wav_to_log_mel(wav_path) -> np.ndarray:
    """回傳 (n_frames, N_MELS) float32 log-mel，參數與 reference_mel 完全一致。"""
    y = load_wav_mono16k(wav_path)
    return compute_log_mel(y, SR)


def wav_to_log_mel_timed(wav_path) -> tuple[np.ndarray, float]:
    """同 `wav_to_log_mel`，多回傳計算耗時（秒），供延遲驗收用。"""
    t0 = time.monotonic()
    log_mel = wav_to_log_mel(wav_path)
    elapsed = time.monotonic() - t0
    return log_mel, elapsed
