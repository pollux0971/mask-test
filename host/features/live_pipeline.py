"""B06→D01→D02→D03，串成一條「對齊好的即時幀 進、104 維查詢向量 出」的
純函式管線。

**這條路徑先前不存在。** `host/align/aligner.py`（B06）、
`analysis/features/tof_features.py`（D01）、
`analysis/features/audio_features.py`（D02）、
`analysis/features/feature_assembly.py`（D03）各自都有實作、各自都有測試，
但只在各自的合成測試資料裡被獨立呼叫過——沒有任何地方把四塊接成一條真的
「裝置資料進、可以餵給 `RecognitionService.recognize()` 的向量出」的路。
這個模組就是那條路，且刻意做成不依賴 `bridge_server.py`／HTTP／真裝置，
可以獨立測試（見 `test_live_pipeline.py`）。

**baseline mu/sigma、cvn 都是明確的參數，不是這個模組自己的假設**——
呼叫端（`bridge_server.py`）自己決定要餵哪一份 baseline（例如 B10 現場
擷取的、或某個 session 存好的），這個模組不去猜、不去讀檔案、不去連
資料庫，純粹是「給我對齊好的幀跟 baseline，我吐一個 104 維序列」。

**這裡不處理 ambient。** `ambient_per_spad`（A16）是 D10 串擾偵測用的
獨立資料，不是 CONTRACTS.md §3.3 104 維特徵向量的一部分（那裡就只有
`tof_A`(32) + `tof_B`(32) + `mel`(40)），所以這個管線完全不碰它。

**這裡也不處理 VAD。** `mel_features()` 的 `vad_start`/`vad_end` 兩個參數
留空（跟 `analysis/run_all.py` 的 `build_feature_seqs()`——目前唯一的
真實參考管線——做法一致，那邊也是整段不裁切），因為沒有 VAD 起訖點的
即時 producer（C08/C19 都各自碰過這個缺口，見對應報告）。

**幀選擇：只用 ToF-A、ToF-B、Mel 三者同時「有資料」的幀，其餘直接丟棄，
不補值。** `Aligner` 已經用 `*_present` 誠實標記「這個時間點附近沒有可信
樣本」；補一個假數值進去比丟掉那一幀更危險——會製造出看起來正常、實際上
不存在的訊號。
"""
from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np

from analysis.features.audio_features import mel_features
from analysis.features.feature_assembly import FeatureSeq, DEFAULT_T_FIXED, assemble_feature_seq
from analysis.features.tof_features import tof_features
from host.align.aligner import AlignedFrame

MIN_USABLE_FRAMES = 2  # assemble_feature_seq()/resample_fixed_length() 的硬性下限


class InsufficientFramesError(ValueError):
    """可用幀數（三個模態同時 present）低於 MIN_USABLE_FRAMES。"""


def _tof_sample_to_row(sample) -> List[float]:
    """`TofSample.values` 的無效項是 `None`（B06 的約定）；填 0.0 是安全的
    佔位值，不是「假裝有訊號」——`tof_features()` 隨後一定會用 `valid`
    遮罩把這些位置的 z-score 蓋成 0，這裡的 0.0 只是讓減法/除法不會在
    `None` 上炸掉，不會影響最終結果。"""
    return [0.0 if v is None else float(v) for v in sample.values]


def _extract_usable_frames(frames: Sequence[AlignedFrame]):
    """只保留 ToF-A / ToF-B / Mel 三者同時 present 的幀（見模組說明）。"""
    return [f for f in frames if f.tof_A_present and f.tof_B_present and f.mel_present]


def assemble_query_from_aligned_frames(
    frames: Sequence[AlignedFrame],
    baseline_mu_A, baseline_sigma_A,
    baseline_mu_B, baseline_sigma_B,
    t_fixed: int = DEFAULT_T_FIXED,
    cvn: bool = False,
    active_zones_A: Optional[Sequence[int]] = None,
    active_zones_B: Optional[Sequence[int]] = None,
) -> FeatureSeq:
    """把一段 `Aligner.frames()` 的輸出組成 `analysis.features.feature_assembly
    .FeatureSeq`——`.data`（固定 T=`t_fixed`）給 cosine 距離用，`.data_raw`
    給 DTW 用，跟 `RecognitionService.recognize(query, ...)` 期待的形狀
    直接對應。

    frames: `Aligner.frames(...)` 的輸出（或任何 `AlignedFrame` 序列）。
    baseline_mu_A/sigma_A, baseline_mu_B/sigma_B: 各 (32,)，B10 現場擷取
        或某個 session 存好的 baseline（見 `host/storage/baseline.py`
        `BaselineOutcome.baseline_mu_A` 等欄位）——呼叫端自己決定來源。
    t_fixed: 固定長度重採樣的目標幀數，預設跟 D03 一致（24）。
    cvn: 是否對 mel 額外做逐 band 除以標準差（見 D02 `mel_features`）。
    active_zones_A/B: 可選，只用這些 zone 的距離+signal 通道（見 D11
        `active_zone_indices`）；為 None 時用全部 16 個 zone。

    幀數不足（三個模態同時 present 的幀 < `MIN_USABLE_FRAMES`）時丟
    `InsufficientFramesError`，不猜測、不補值。
    """
    usable = _extract_usable_frames(frames)
    if len(usable) < MIN_USABLE_FRAMES:
        raise InsufficientFramesError(
            f"三個模態同時有資料的幀只有 {len(usable)} 個，至少需要 {MIN_USABLE_FRAMES} 個"
            f"（原始輸入 {len(frames)} 幀）"
        )

    tof_a_raw = np.array([_tof_sample_to_row(f.tof_A) for f in usable], dtype=np.float64)
    valid_a = np.array([f.tof_A.valid for f in usable], dtype=bool)
    tof_b_raw = np.array([_tof_sample_to_row(f.tof_B) for f in usable], dtype=np.float64)
    valid_b = np.array([f.tof_B.valid for f in usable], dtype=bool)
    mel_raw = np.array([f.mel for f in usable], dtype=np.float64)
    t_us = np.array([f.t_us for f in usable], dtype=np.int64)

    tof_a_z = tof_features(tof_a_raw, valid_a, baseline_mu_A, baseline_sigma_A, active_zones_A)
    tof_b_z = tof_features(tof_b_raw, valid_b, baseline_mu_B, baseline_sigma_B, active_zones_B)
    mel_cmn = mel_features(mel_raw, cvn=cvn)

    # active_zones 篩選過的話,tof_a_z/tof_b_z 通道數會 < TOF_DIM,
    # assemble_feature_seq() 的固定 104 維檢查會直接、明確地擋下這種呼叫
    # 方式（目前 RecognitionService 的 slices 假設全部 zone 都在）,
    # 不在這裡重複驗證。

    return assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=t_fixed)
