"""音訊特徵：CMN 與長度正規化。

規格見 `stories/D-analysis/D02.md`；log-Mel 定義凍結於 CONTRACTS.md §3.1。

輸入的 log-Mel 一律用 `host/features/mel_pipeline.wav_to_log_mel()` 算出來的，
不在這裡重寫一份 Mel。一致性檢查也一律重用 `host/features/dtw_compare.py`
（B14 產出的 `pearson_corr` / `dtw_distance`），不重寫一份——理由跟 B14
自己的 docstring 一樣：同一件事兩邊各自算一次，對不上時分不清是誰的問題。
"""
import numpy as np

CVN_FLOOR = 1e-6  # 除以標準差時的下限保護，避免 utterance 內某 band 完全不變時除以 0


def mel_features(mel, vad_start=None, vad_end=None, cvn=False):
    """CMN（一定做）+ 可選 CVN + VAD 裁切。

    mel: (n_frames, n_mels) log-mel
    vad_start/vad_end: B15/B16 提供的語音起訖幀索引；皆為 None 時視為整段 utterance
    cvn: True 則額外做逐 band 除以標準差

    CMN 在裁切後的 utterance 內部做，不是對整個 session 做——
    對 session 做會保留 utterance 之間的通道差異，那正是要消除的東西。
    """
    mel = np.asarray(mel, dtype=np.float64)
    m = mel[vad_start:vad_end]
    if m.shape[0] == 0:
        raise ValueError("VAD 裁切後長度為 0，無法計算 CMN")

    m = m - m.mean(axis=0, keepdims=True)
    if cvn:
        m = m / np.maximum(m.std(axis=0, keepdims=True), CVN_FLOOR)
    return m


def check_device_consistency(host_log_mel, device_log_mel, threshold=0.95):
    """A12 裝置端輸出與主機端 log-mel 的一致性檢查工具。

    相關係數計算重用 `host/features/dtw_compare.pearson_corr`，不重寫一份。
    A12 完成前，可以拿量化過的 host 輸出（`reference_mel.to_device_int16()`
    再還原）當替身，驗證這個檢查工具本身邏輯正確——見對應的測試。

    回傳 (corr, passed)。
    """
    from host.features.dtw_compare import pearson_corr

    corr = pearson_corr(host_log_mel, device_log_mel)
    return corr, corr > threshold
