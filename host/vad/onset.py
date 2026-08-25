"""B16 — 唇動先行量測（`lip_onset_us - voice_onset_us`）。

**這個數字本身就是研究結果。** 如果 ToF 通道能在聲音出現**之前**就偵測到
動作，那是「ToF 不只是麥克風的附庸」最直接的證據，也是 `D14`（Viseme
敏感度熱力圖）的前置。

## 為什麼這個數字很容易量錯

兩個模態各自跑自己的 VAD，然後把兩個起點相減。問題是：**相減之後，兩邊
偵測器的任何差異都會變成一個看起來像研究發現的數字。**

`B16` 的 story 明講了最明顯的一種：

> 如果 ToF-VAD 的閾值比音訊 VAD 寬鬆，會系統性地產生「唇動比較早」的
> 假結果。

所以本模組的設計前提是：**兩邊必須用同一個偵測器、同一組 σ 倍數**
（見 `hysteresis.py` 與 `tof_vad.py`）。`measure_lip_lead()` 會檢查這件事，
不一致時在結果裡標 `comparable=False`——**不擋，但講出來**，因為刻意調
過閾值的比較還是有它的用途，只是不能拿去寫結論。

## 誤差預算（重要）

story 預期唇動先行 50–150 ms。兩個模態各自的時間量化誤差是：

    ToF  30    Hz → 33.3 ms/幀
    $M   31.25 Hz → 32.0 ms/幀

單筆量測的量化誤差因此是 **±33 ms（ToF）與 ±32 ms（音訊）**，兩者獨立，
合成約 **±46 ms（RMS）或最壞 ±65 ms**。也就是說：

* **單筆**的先行量若是 50 ms，量化誤差就跟訊號本身同量級——**單筆數字沒有
  意義**，不可以拿一筆去講結論。
* 但量化誤差是**零均值**的，所以 N 筆平均後會以 1/sqrt(N) 收斂：20 筆
  平均的量化誤差約 **±10 ms**，這時候 50–150 ms 的先行量才站得住。

`summarize_lip_lead()` 因此把「有幾筆」「符號是否一致」一起回報，而不是
只給一個平均值。驗收條件「20 筆樣本的時間差有一致的正負號」量的正是這件
事——**符號一致性比平均值本身更能說明問題**，因為它不依賴任何校正。

⚠️ 這裡只處理**量化**誤差。兩個模態之間若有系統性的時間偏移（韌體取樣點
定義、`B04`/`B05` 的時鐘對齊殘差），那是**偏差**不是雜訊，平均再多筆也
不會消失。§1.3 已經凍結了兩種模態的取樣點定義正是為了壓這一項，但實際
殘差要上機才知道——見完成回報的「需要人工驗證」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

# §1.1.2 / §1.3：ToF 30 Hz、`$M` 31.25 Hz（mic_hop=512 @16kHz）。
TOF_FRAME_US = 1_000_000 / 30.0
MIC_FRAME_US = 1_000_000 / 31.25

# 單筆量測的量化誤差（兩者獨立，取平方和開根號）。
QUANTIZATION_RMS_US = math.sqrt(TOF_FRAME_US ** 2 + MIC_FRAME_US ** 2)
QUANTIZATION_WORST_US = TOF_FRAME_US + MIC_FRAME_US


@dataclass(frozen=True)
class LipLead:
    """單筆 trial 的唇動先行量。

    `lead_us > 0` 代表**唇動比發聲早**（`voice_onset - lip_onset`）。符號
    刻意這樣定，因為 story 的敘述是「唇動比出聲早多久」，正值讀起來才符合
    直覺；`to_trial_attrs()` 存的是兩個原始時間戳，不存差值，下游要什麼符號
    自己減。
    """

    lip_onset_us: Optional[int]
    lip_offset_us: Optional[int]
    voice_onset_us: Optional[int]
    voice_offset_us: Optional[int]
    comparable: bool
    reason: Optional[str] = None
    lip_confidence: float = 0.0
    voice_confidence: float = 0.0
    speaking_mode: Optional[str] = None

    @property
    def lead_us(self) -> Optional[int]:
        if self.lip_onset_us is None or self.voice_onset_us is None:
            return None
        return int(self.voice_onset_us - self.lip_onset_us)

    @property
    def lead_ms(self) -> Optional[float]:
        lead = self.lead_us
        return None if lead is None else lead / 1000.0

    @property
    def quantization_us(self) -> float:
        """這一筆的量化誤差（RMS）。與 `lead_us` 同量級時，這一筆不能單獨解讀。"""
        return QUANTIZATION_RMS_US

    @property
    def resolvable(self) -> bool:
        """先行量是否大於自身的量化誤差。`False` 不代表沒有先行，只代表
        **這一筆分辨不出來**——要靠多筆平均。"""
        lead = self.lead_us
        return lead is not None and abs(lead) > QUANTIZATION_RMS_US

    def to_trial_attrs(self) -> dict:
        """`B11` 在等的四個 `/trial_NNN` attrs（CONTRACTS.md §2）。

        偵測不到就是 `None`，**不填 0、也不填 capture 視窗的邊界**——0 是
        一個合法的 `t_us`，視窗邊界則會讓「完全沒偵測到」看起來像「整段都
        在動」，兩者都會讓下游安靜地算出錯的統計。
        """
        return {
            "vad_start_us": self.voice_onset_us,
            "vad_end_us": self.voice_offset_us,
            "lip_onset_us": self.lip_onset_us,
            "voice_onset_us": self.voice_onset_us,
        }

    def to_dict(self) -> dict:
        return {
            **self.to_trial_attrs(),
            "lip_offset_us": self.lip_offset_us,
            "lead_us": self.lead_us,
            "lead_ms": self.lead_ms,
            "comparable": self.comparable,
            "resolvable": self.resolvable,
            "reason": self.reason,
            "lip_confidence": self.lip_confidence,
            "voice_confidence": self.voice_confidence,
            "speaking_mode": self.speaking_mode,
            "quantization_rms_us": QUANTIZATION_RMS_US,
        }


def measure_lip_lead(tof_result, audio_result) -> LipLead:
    """把一次 trial 的 ToF VAD 與音訊 VAD 結果合成一筆先行量測。

    `audio_result` 可以是 `applicable=False`（silent 模式、或缺底噪統計）
    ——此時仍會回傳唇動的起訖，只是 `lead_us` 是 `None`。**silent 模式下
    切出動作區間本來就是這個 story 的第一條驗收條件**，不該因為沒有語音
    就整筆丟掉。
    """
    lip = tof_result.primary if getattr(tof_result, "applicable", False) else None
    voice = audio_result.primary if getattr(audio_result, "applicable", False) else None

    comparable, reason = _comparability(tof_result, audio_result, lip, voice)

    return LipLead(
        lip_onset_us=lip.start_us if lip else None,
        lip_offset_us=lip.end_us if lip else None,
        voice_onset_us=voice.start_us if voice else None,
        voice_offset_us=voice.end_us if voice else None,
        comparable=comparable,
        reason=reason,
        lip_confidence=getattr(tof_result, "confidence", 0.0),
        voice_confidence=getattr(audio_result, "confidence", 0.0),
        speaking_mode=getattr(audio_result, "speaking_mode", None),
    )


def _comparability(tof_result, audio_result, lip, voice):
    """兩邊的結果能不能拿來相減。回傳 `(comparable, reason)`。"""
    if lip is None or voice is None:
        missing = []
        if lip is None:
            missing.append("唇動")
        if voice is None:
            missing.append("發聲")
        return False, f"缺少{'與'.join(missing)}起點，無法相減"

    tof_thresholds = (
        getattr(tof_result, "enter_threshold", None),
        getattr(tof_result, "exit_threshold", None),
    )
    if None in tof_thresholds:
        return False, "ToF 端沒有回報閾值，無法確認兩邊可比"

    # 兩邊的 σ 倍數必須一致，否則差值裡混了演算法差異（見模組說明）。
    tof_sigmas = _sigma_multipliers(tof_result)
    audio_sigmas = _sigma_multipliers(audio_result)
    if tof_sigmas is None or audio_sigmas is None:
        return False, "算不出其中一邊的 σ 倍數，無法確認兩邊可比"
    if not _close(tof_sigmas, audio_sigmas):
        return False, (
            f"兩邊的 σ 倍數不同（ToF {tof_sigmas[0]:.2f}/{tof_sigmas[1]:.2f}、"
            f"音訊 {audio_sigmas[0]:.2f}/{audio_sigmas[1]:.2f}）——相減會把"
            "偵測器的差異當成唇動先行量"
        )
    if getattr(tof_result, "baseline_suspect", False):
        return False, "ToF 的 baseline 可疑（見 tof_vad 的 baseline_suspect），不宜納入統計"
    return True, None


def _sigma_multipliers(result):
    """從 `(enter_threshold, exit_threshold, mu, sigma)` 反推 σ 倍數。

    直接比對兩邊實際用的倍數，而不是相信呼叫端「應該」用了預設值——
    呼叫端可以明確傳入不同的值，那正是我們要偵測的情況。
    """
    enter = getattr(result, "enter_threshold", None)
    exit_ = getattr(result, "exit_threshold", None)
    mu = getattr(result, "energy_mu", None)
    sigma = getattr(result, "energy_sigma", None)
    if mu is None:
        mu = getattr(result, "noise_floor_mu", None)
        sigma = getattr(result, "noise_floor_sigma", None)
    if None in (enter, exit_, mu, sigma) or not sigma:
        return None
    return (enter - mu) / sigma, (exit_ - mu) / sigma


def _close(a, b, tol=1e-6):
    return all(abs(x - y) <= tol * max(1.0, abs(x), abs(y)) for x, y in zip(a, b))


@dataclass(frozen=True)
class LipLeadSummary:
    """一批 trial 的先行量統計。

    刻意**不只給平均值**。單筆的量化誤差（±46 ms RMS）與預期的先行量
    （50–150 ms）同量級，所以真正能說明問題的是「有幾筆」與「符號是否
    一致」——後者不依賴任何校正，也不受固定偏移影響的程度最小。
    """

    n_total: int
    n_comparable: int
    n_positive: int
    n_negative: int
    n_zero: int
    mean_us: Optional[float]
    median_us: Optional[float]
    std_us: Optional[float]
    min_us: Optional[int]
    max_us: Optional[int]

    @property
    def sign_consistency(self) -> Optional[float]:
        """多數符號佔可比樣本的比例。1.0 = 全部同號。"""
        if not self.n_comparable:
            return None
        return max(self.n_positive, self.n_negative) / self.n_comparable

    @property
    def dominant_sign(self) -> Optional[int]:
        if not self.n_comparable:
            return None
        if self.n_positive == self.n_negative:
            return 0
        return 1 if self.n_positive > self.n_negative else -1

    @property
    def mean_quantization_us(self) -> Optional[float]:
        """N 筆平均之後的量化誤差。零均值誤差以 1/sqrt(N) 收斂。"""
        if not self.n_comparable:
            return None
        return QUANTIZATION_RMS_US / math.sqrt(self.n_comparable)

    @property
    def mean_resolvable(self) -> Optional[bool]:
        """平均先行量是否大於平均後的量化誤差。**這才是可以拿去講的判準。**"""
        if self.mean_us is None or self.mean_quantization_us is None:
            return None
        return abs(self.mean_us) > self.mean_quantization_us

    def to_dict(self) -> dict:
        return {
            "n_total": self.n_total,
            "n_comparable": self.n_comparable,
            "n_positive": self.n_positive,
            "n_negative": self.n_negative,
            "n_zero": self.n_zero,
            "mean_us": self.mean_us,
            "median_us": self.median_us,
            "std_us": self.std_us,
            "min_us": self.min_us,
            "max_us": self.max_us,
            "sign_consistency": self.sign_consistency,
            "dominant_sign": self.dominant_sign,
            "mean_quantization_us": self.mean_quantization_us,
            "mean_resolvable": self.mean_resolvable,
        }


def summarize_lip_lead(leads: Sequence[LipLead]) -> LipLeadSummary:
    """彙總一批先行量測。只有 `comparable` 且兩個起點都有的才納入統計。"""
    values = [
        lead.lead_us for lead in leads
        if lead.comparable and lead.lead_us is not None
    ]
    n_total = len(leads)
    if not values:
        return LipLeadSummary(
            n_total=n_total, n_comparable=0, n_positive=0, n_negative=0, n_zero=0,
            mean_us=None, median_us=None, std_us=None, min_us=None, max_us=None,
        )

    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n
    variance = sum((v - mean) ** 2 for v in ordered) / n
    mid = n // 2
    median = float(ordered[mid]) if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0

    return LipLeadSummary(
        n_total=n_total,
        n_comparable=n,
        n_positive=sum(1 for v in values if v > 0),
        n_negative=sum(1 for v in values if v < 0),
        n_zero=sum(1 for v in values if v == 0),
        mean_us=mean,
        median_us=median,
        std_us=math.sqrt(variance),
        min_us=ordered[0],
        max_us=ordered[-1],
    )
