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

**VAD 裁切現在是選填的（`speech_window`）。** `mel_features()` 自己的
`vad_start`/`vad_end` 兩個參數仍然留空不用——裁切改在**對齊後的幀**這一層
做（見 `compute_speech_window()`），因為 ToF-A/ToF-B/Mel 已經是同一組
共用時間軸，在這裡裁一次就對三個模態同時生效，不用分別換算成三種各自
的幀索引。**不給 `speech_window` 就是舊行為（整段不裁切）**——這是
`reports/ALIGNMENT_MISMATCH.md`「按住多久會不會洩漏」章節量到的問題的
修法：hold-to-record 按鍵按多久，會直接改變講話動作在固定 `T=24` 幀裡的
相對位置，混進跟詞義無關的差異。

**幀選擇：只用 ToF-A、ToF-B、Mel 三者同時「有資料」的幀，其餘直接丟棄，
不補值。** `Aligner` 已經用 `*_present` 誠實標記「這個時間點附近沒有可信
樣本」；補一個假數值進去比丟掉那一幀更危險——會製造出看起來正常、實際上
不存在的訊號。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from analysis.features.audio_features import mel_features
from analysis.features.feature_assembly import FeatureSeq, DEFAULT_T_FIXED, assemble_feature_seq
from analysis.features.tof_features import tof_features
from host.align.aligner import AlignedFrame

MIN_USABLE_FRAMES = 2  # assemble_feature_seq()/resample_fixed_length() 的硬性下限

# `compute_speech_window()` 的 margin/下限預設值——見該函式 docstring 的
# 完整理由，這裡只列常數本身。
DEFAULT_PRE_MARGIN_US = 100_000    # 100 ms
DEFAULT_POST_MARGIN_US = 100_000   # 100 ms
DEFAULT_MIN_SPEECH_WINDOW_US = 300_000  # 300 ms ≈ 30 Hz 下 9 個原始 ToF 幀


@dataclass(frozen=True)
class SpeechWindowResult:
    """`compute_speech_window()` 的回傳值——**永遠有 `reason`，不管有沒有
    裁切**。`window_us` 為 `None` 代表沒有裁切（沒偵測到任何來源、或裁完
    太短），這兩種情況都必須被看見：一部分樣板裁過、一部分整段使用，
    它們在特徵空間裡不可比，而這正是這個功能存在之前一直沒有任何東西會
    講出來的坑（`reports/ALIGNMENT_MISMATCH.md`「按住多久會不會洩漏」）。
    """
    window_us: Optional[Tuple[int, int]]
    trimmed: bool
    source: str                    # "none" 或參與聯集的來源標籤（例如 "lip_A+voice"）
    reason: Optional[str]

    def to_dict(self) -> dict:
        return {
            "trimmed": self.trimmed,
            "source": self.source,
            "reason": self.reason,
            "window_us": list(self.window_us) if self.window_us else None,
        }


def compute_speech_window(
    segments: Sequence[Tuple[str, Optional[int], Optional[int]]],
    *,
    pre_margin_us: int = DEFAULT_PRE_MARGIN_US,
    post_margin_us: int = DEFAULT_POST_MARGIN_US,
    min_span_us: int = DEFAULT_MIN_SPEECH_WINDOW_US,
) -> SpeechWindowResult:
    """從一批 VAD 偵測到的區段，算出「真的在講話」的時間窗，供
    `assemble_query_from_aligned_frames()` 的 `speech_window` 用——讓固定
    長度重採樣之前先把 hold-to-record 按鍵按住多久造成的「講完話還按著」
    那段濾掉。背景與量到的落差見 `reports/ALIGNMENT_MISMATCH.md`「按住
    多久會不會洩漏進特徵向量」章節。

    segments: `(來源標籤, start_us, end_us)` 的清單。每個 VAD 來源（唇動
    A、唇動 B、語音……）各自一筆；沒偵測到的來源可以整個不放進清單，或放
    `(label, None, None)`——這裡會自動濾掉，兩種寫法效果一樣。

    ## 取聯集，不是交集

    窗口是**所有來源裡最早的 start，到最晚的 end**。唇動本來就該比發聲
    早——那正是這個專案要量的東西本身（`host/vad/onset.py`），取交集會
    直接把唇動領先的那一段切掉，而且切完看起來完全正常，不會有任何錯誤
    訊息。兩顆 ToF 感測器（A/B）同理：只要有一邊偵測到動作起點，就不該
    被另一邊「還沒偵測到」蓋掉。

    ## margin 的理由（不是隨便訂的數字）

    - **前緣 `pre_margin_us`（預設 100 ms）**：`host/vad/onset.py` 算過的
      跨模態量化誤差 RMS 約 46 ms，這裡抓 2 倍當安全空間；同時**遠低於**
      story 預期的唇動先行量（50–150 ms），不會把要量的東西本身裁掉。
      `host/vad/hysteresis.py` 的 onset 偵測本身已經有回退到「上升沿
      起腳」的邏輯（最多 96 ms）——這裡的 100 ms 是在那之上**再留**一點
      量化安全空間，不是重複的保護。
    - **後緣 `post_margin_us`（預設 100 ms）**：離開閾值本身已經有
      200 ms 掛延遲（`hysteresis.py` 的 `DEFAULT_HANGOVER_MS`），這裡再留
      100 ms 給收尾的量化誤差。

    ## 兩種「不裁切」的情況，都要記錄原因，不能安靜發生

    - **完全沒有任何來源偵測到**：回 `source="none"`。silent 模式下沒有
      語音是預期中的正常狀況，但連唇動都沒偵測到通常代表這筆錄音本身有
      問題——不該被裁切邏輯悄悄吃掉，變得跟「這筆錄音很正常、只是沒被
      選中裁切」看起來一樣。
    - **裁切窗太短**：`min_span_us`（預設 300 ms，約 30 Hz 下 9 個原始
      ToF 幀）是重採樣到 `T=24` 還有意義的下限，低於這個值裁切反而比不
      裁切更失真，這裡選擇退回整段而不是報錯——跟這份報告一路以來
      「結構壞掉才 STOP，數字異常只回報」的原則一致。
    """
    usable = [(label, s, e) for label, s, e in segments if s is not None and e is not None]
    if not usable:
        return SpeechWindowResult(
            window_us=None, trimmed=False, source="none",
            reason="沒有任何來源偵測到起訖（唇動與語音都沒有），退回使用整段錄音（未裁切）",
        )

    start = min(s for _, s, _ in usable) - pre_margin_us
    end = max(e for _, _, e in usable) + post_margin_us
    source = "+".join(sorted({label for label, _, _ in usable}))

    span = end - start
    if span < min_span_us:
        return SpeechWindowResult(
            window_us=None, trimmed=False, source=source,
            reason=(f"裁切窗口只剩 {span / 1000:.0f} ms（來源：{source}），"
                    f"低於下限 {min_span_us / 1000:.0f} ms，退回使用整段錄音（未裁切）"),
        )

    return SpeechWindowResult(window_us=(int(start), int(end)), trimmed=True, source=source, reason=None)


class InsufficientFramesError(ValueError):
    """可用幀數（三個模態同時 present）低於 MIN_USABLE_FRAMES。"""


@dataclass(frozen=True)
class SensorCoverage:
    """一次組裝裡，每個模態實際「有資料」的幀數——**不是拒絕的理由，是讓
    呼叫端自己判斷這筆向量該多信任**（`esp-mask-test-ad` 2026-08-26 的明確
    要求：真機上 ToF-A 會間歇性斷線，`union_min` 的設計前提本來就是一顆
    可能瞎掉，這裡不擅自決定「少一顆就拒絕」，只負責讓這件事不再是啞的）。

    `total_frames`：`Aligner` 吐出的原始幀數（含任何模態缺席的那些）。
    `usable_frames`：三個模態同時 present、真正被拿去組向量的幀數
    （`assemble_query_from_aligned_frames` 內部已經驗證過 >= `MIN_USABLE_FRAMES`）。
    `present_frames`：`{"tof_A": n, "tof_B": n, "mel": n}`——各模態單獨
    present 的幀數（分母是 `total_frames`），跟 `usable_frames`（三者的交集）
    不同：一顆感測器在整段窗口只活了一半，`present_frames` 看得出來，
    `usable_frames` 只會告訴你「窗口變窄了」，看不出是哪一顆的問題。

    **實測過的真實風險**（見對應調查報告）：ToF-A 中途斷線但沒有斷到完全
    掛掉時，`assemble_query_from_aligned_frames` 不會丟例外——它會安靜地
    只用兩者重疊的那一小段窗口組向量，量出來的距離可能明顯偏移、甚至讓
    `top1` 選錯類別。`SensorCoverage` 就是讓呼叫端能看見「這一小段」到底
    有多小，自己決定要不要顯示警告、要不要在 UI 上標示、或要不要照樣用。
    """
    total_frames: int
    usable_frames: int
    present_frames: Dict[str, int]

    def fraction(self, key: str) -> float:
        """`key` in {"tof_A", "tof_B", "mel"}。`total_frames == 0` 時回 0.0
        （沒有任何輸入幀，跟「完全沒訊號」是同一件事，不是除以零的例外）。"""
        if self.total_frames == 0:
            return 0.0
        return self.present_frames.get(key, 0) / self.total_frames

    def usable_fraction(self) -> float:
        return (self.usable_frames / self.total_frames) if self.total_frames else 0.0


@dataclass(frozen=True)
class QueryAssembly:
    """`assemble_query_from_aligned_frames()` 的回傳值。`.data`/`.data_raw`/
    `.slices`/`.t_us`/`.t_us_raw` 直接轉發底下的 `FeatureSeq`——**既有呼叫端
    （`bridge_server.py` 的 `_handle_recognize`，直接用 `query.data`）完全
    不用改就能繼續動**，這是刻意的：這條路徑改動時 `bridge_server.py` 不一定
    歸這一輪的人動。`.coverage`／`.trim` 都是新加的欄位，要不要看、看了
    要怎麼反應由呼叫端自己決定。

    `.trim`：呼叫端有傳 `speech_window`（`compute_speech_window()` 的
    結果）時原樣轉發，沒傳就是 `None`——`None` 代表「這次呼叫根本沒有
    嘗試裁切」，跟 `SpeechWindowResult(trimmed=False, ...)`（有嘗試但
    退回整段）是兩件不同的事，呼叫端要能分得出來。
    """
    feature_seq: FeatureSeq
    coverage: SensorCoverage
    trim: Optional["SpeechWindowResult"] = None

    @property
    def data(self):
        return self.feature_seq.data

    @property
    def data_raw(self):
        return self.feature_seq.data_raw

    @property
    def slices(self):
        return self.feature_seq.slices

    @property
    def t_us(self):
        return self.feature_seq.t_us

    @property
    def t_us_raw(self):
        return self.feature_seq.t_us_raw


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
    speech_window: Optional[SpeechWindowResult] = None,
) -> QueryAssembly:
    """把一段 `Aligner.frames()` 的輸出組成 `QueryAssembly`——`.data`
    （固定 T=`t_fixed`）給 cosine 距離用，`.data_raw` 給 DTW 用，跟
    `RecognitionService.recognize(query, ...)` 期待的形狀直接對應
    （`QueryAssembly` 轉發這些欄位，舊呼叫端不用改）。`.coverage` 是
    `SensorCoverage`，見該類別的說明——**少一顆感測器不會讓這裡拒絕，
    只會讓呼叫端看得到。**

    frames: `Aligner.frames(...)` 的輸出（或任何 `AlignedFrame` 序列）。
    baseline_mu_A/sigma_A, baseline_mu_B/sigma_B: 各 (32,)，B10 現場擷取
        或某個 session 存好的 baseline（見 `host/storage/baseline.py`
        `BaselineOutcome.baseline_mu_A` 等欄位）——呼叫端自己決定來源。
    t_fixed: 固定長度重採樣的目標幀數，預設跟 D03 一致（24）。
    cvn: 是否對 mel 額外做逐 band 除以標準差（見 D02 `mel_features`）。
    active_zones_A/B: 可選，只用這些 zone 的距離+signal 通道（見 D11
        `active_zone_indices`）；為 None 時用全部 16 個 zone。
    speech_window: 可選，`compute_speech_window()` 的結果——`.window_us`
        非 `None` 時只保留這個時間窗內的幀，再做固定長度重採樣（見
        `compute_speech_window()` 的完整理由）。`None`（預設）或
        `.window_us is None`（有嘗試但退回整段）都維持這個參數加入前的
        行為——整段都用，不裁切。**新功能預設關閉，不會讓沒有主動選用
        它的呼叫端（例如目前的 `build_templates_from_session.py`）被動
        改變行為。**

    幀數不足（三個模態同時 present 的幀 < `MIN_USABLE_FRAMES`，裁切之後
    也算）時丟 `InsufficientFramesError`，不猜測、不補值；**一顆感測器
    只是「間歇性」有資料，不會觸發這個例外，向量照樣組得出來，這正是
    `SensorCoverage` 存在的理由**：真機上 ToF-A 中途斷線的實測顯示，這種
    情況下距離量測會明顯偏移、`top1` 可能選錯類別，而且完全不會有任何
    例外或錯誤訊息。
    """
    usable = _extract_usable_frames(frames)
    if speech_window is not None and speech_window.window_us is not None:
        window_start_us, window_end_us = speech_window.window_us
        usable = [f for f in usable if window_start_us <= f.t_us <= window_end_us]
    if len(usable) < MIN_USABLE_FRAMES:
        raise InsufficientFramesError(
            f"三個模態同時有資料的幀只有 {len(usable)} 個，至少需要 {MIN_USABLE_FRAMES} 個"
            f"（原始輸入 {len(frames)} 幀"
            + (f"，裁切窗口 {speech_window.window_us}）" if speech_window and speech_window.window_us else "）")
        )

    coverage = SensorCoverage(
        total_frames=len(frames),
        usable_frames=len(usable),
        present_frames={
            "tof_A": sum(1 for f in frames if f.tof_A_present),
            "tof_B": sum(1 for f in frames if f.tof_B_present),
            "mel": sum(1 for f in frames if f.mel_present),
        },
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

    feature_seq = assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=t_fixed)
    return QueryAssembly(feature_seq=feature_seq, coverage=coverage, trim=speech_window)
