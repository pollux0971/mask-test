"""B16 — ToF VAD（唇動起訖）。

silent 模式下音訊 VAD 完全失效，但那正是這個裝置最有價值的應用場景。
而且唇動通常比發聲早——**那個時間差本身就是研究結果**（見 `onset.py`）。

## 訊號源

story 給的式子：

    energy = np.abs(zscore(tof, baseline_mu, baseline_sigma)).mean(axis=1)

也就是「每個 zone 偏離自己 baseline 幾個 σ」的逐幀平均。z-score 已經用
`B10` 的 per-zone `baseline_mu`/`baseline_sigma` 正規化過，所以不同 zone
的量測雜訊被拉到同一個尺度上，可以直接平均。

**只用前半的距離通道，不含後半的 signal 通道。** ToF 陣列是 `(T, 2*Z)`
（4×4 → Z=16、8×8 → Z=64），前半距離、後半 `signal/100`。理由：

* `tof_valid_A/B` 與 `B10` 的三種壞 zone 判定都是 **per-zone**，不是
  per-channel。用全部 `2*Z` 個通道就沒有一致的排除依據。
* signal 與距離高度相關（距離變近，`signal_per_spad` 跟著上升）。把它一起
  平均進去，等於同一份資訊算兩次——zone 數看起來變成兩倍，實際獨立度
  沒變，下面那個「靜止時的理論 σ」就會被低估，閾值跟著設得太鬆。

要含進來的話 `include_signal=True`，但預設不含。

## 靜止時的能量分布是**算得出來**的

靜止時每個 zone 的 z 應該 ~ N(0,1)，所以 |z| 是半常態分布：

    E[|z|]   = sqrt(2/pi)      ≈ 0.7979
    Var[|z|] = 1 - 2/pi        ≈ 0.3634

N 個 zone 平均之後：

    mu_E    ≈ 0.7979
    sigma_E ≈ sqrt(0.3634 / N)      （N=16 → 0.1507、N=64 → 0.0754）

用 20000 幀的合成靜止資料核對過，理論與實測吻合：
N=16 實測 σ=0.1496（理論 0.1507）、N=64 實測 σ=0.0748（理論 0.0754）。

**這是下界，不是真值**：相鄰 zone 會一起看到同一片嘴唇，實際相關，所以
真實的 `sigma_E` 會比它大。所以預設用**穩健的經驗估計**（median + MAD），
把理論值當成**交叉檢查**放進結果裡——經驗值遠大於理論值，代表 baseline
過期了或戴法變了，那是「這次量測不能信」的訊號，不是靜靜地繼續算。

（這也是我在 `B15` 回報過的問題的解法：`compute_noise_floor()` 用
mean/std，餵到含動作的資料會被拉高、閾值靜默地變太鬆。MAD 對 20% 左右
的活動工作週期不敏感。）

## 閾值刻意與音訊 VAD **完全相同**

`B16` 的 story 明講了這個陷阱：

> 如果 ToF-VAD 的閾值比音訊 VAD 寬鬆，會系統性地產生「唇動比較早」的
> 假結果。

所以兩邊共用 `hysteresis.py` 的同一個偵測器，σ 倍數也刻意用同一組
（3σ 進入 / 1.5σ 離開）。剩下的差異只有輸入訊號本身——那才是我們要量的
東西。要調 ToF 的靈敏度請**明確地**傳 `enter_sigma`/`exit_sigma`，並且
知道那會讓 `lip_onset - voice_onset` 不再是乾淨的比較。

**不包含**：活躍 zone 篩選（`D01`，此處先用全部 zone）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import numpy as np

from host.vad.hysteresis import (
    DEFAULT_HANGOVER_MS,
    DEFAULT_MAX_ONSET_BACKOFF_MS,
    DEFAULT_MIN_SEGMENT_MS,
    DEFAULT_SMOOTH_FRAMES,
    Segment,
    detect_segments,
)

# §2：`tof_A` 是 (T, 2*Z)，前半距離、後半 signal/100。Z 從形狀推，不寫死
# ——4×4 是 16、8×8 是 64。

# 與 `audio_vad.py` 的 `normal` 模式相同。**改這裡之前先讀模組說明的
# 「閾值刻意與音訊 VAD 完全相同」那一段。**
MATCHED_ENTER_SIGMA = 3.0
MATCHED_EXIT_SIGMA = 1.5

# 靜止時 |z| 的半常態統計量。
REST_MEAN_ABS_Z = math.sqrt(2.0 / math.pi)          # ≈ 0.7979
REST_VAR_ABS_Z = 1.0 - 2.0 / math.pi                # ≈ 0.3634

# 經驗 sigma 超過理論下界這麼多倍，就認為 baseline 不可信並警告。
# 2 倍是給 zone 間相關性的寬裕空間——完全相關（等於只有一個 zone）時
# sigma 會是理論值的 sqrt(N) 倍，16 個 zone 是 4 倍，所以 2 倍還在
# 「部分相關」的合理範圍內，再高就不只是相關性能解釋的了。
BASELINE_SUSPECT_SIGMA_RATIO = 2.0

# MAD → sigma 的換算常數（常態分布下）。
MAD_TO_SIGMA = 1.4826

# 距離的 sigma 下限。**這裡刻意不用 §3.2 的 1e-3。**
#
# `$T` 的距離是 **i16、單位 mm**（§1.1），也就是量化到整數。一個量化到
# 1 mm 的量測，其雜訊的 sigma 不可能有意義地小於量化本身：均勻量化誤差的
# sigma 是 `1/sqrt(12) ≈ 0.289 mm`。若某個 zone 的 baseline sigma 被記成
# 0.01 mm（例如它整段都貼著一面牆、每一幀都回同一個整數），除下去 z 會
# 直接炸到上萬——**那個 zone 會單獨主宰整條能量訊號**，而且不會報錯。
#
# 實測：`mock_device.py` 的 baseline 是 17.0 mm ± 0.15 mm，四捨五入後幾乎
# 每一幀都是整數 17，sigma 趨近 0，z 爆到 9.4e5。用 1e-3 當下限完全擋不住。
#
# §3.2 的 `1e-3` 是給 `D01` 的特徵 z-score 用的（那裡的輸入已經過其他處理），
# 這裡是 VAD 能量，用途不同、輸入是原始整數 mm，所以用量化下限才對。
# 要沿用契約字面值的話明確傳 `sigma_floor=1e-3`。
QUANTIZATION_SIGMA_MM = 1.0 / math.sqrt(12.0)

# ToF 是 30 Hz。這是本模組能分辨的時間下限，見 `quantization_us`。
TOF_NOMINAL_FPS = 30.0


@dataclass(frozen=True)
class TofVadResult:
    """一次 ToF 唇動偵測的結果。形狀刻意與 `audio_vad.VadResult` 對齊，
    好讓 `onset.py` 可以一視同仁地相減。"""

    applicable: bool
    segments: tuple[Segment, ...] = ()
    reason: Optional[str] = None
    enter_threshold: Optional[float] = None
    exit_threshold: Optional[float] = None
    energy_mu: Optional[float] = None
    energy_sigma: Optional[float] = None
    analytic_sigma: Optional[float] = None
    baseline_suspect: bool = False
    n_frames: int = 0
    n_frames_dropped: int = 0        # 整幀所有 zone 都無效而丟掉的
    n_zones_used: int = 0
    excluded_zones: tuple[int, ...] = ()
    discarded_short_segments: int = 0

    @property
    def primary(self) -> Optional[Segment]:
        """最長的那一段。理由同 `audio_vad`：一次 trial 錄一個詞，偶爾夾雜
        調整坐姿之類的短動作，取最長的比取第一個穩健。"""
        if not self.segments:
            return None
        return max(self.segments, key=lambda s: s.duration_us)

    @property
    def detected(self) -> bool:
        return self.primary is not None

    @property
    def confidence(self) -> float:
        """定義與 `audio_vad.VadResult.confidence` 完全相同——兩個模態的
        信心度要能互相比較，用不同公式就不能比。"""
        seg = self.primary
        if seg is None:
            return 0.0
        return float(min(1.0, max(0.0,
                                  (seg.peak_z - MATCHED_ENTER_SIGMA) / MATCHED_ENTER_SIGMA)))

    @property
    def quantization_us(self) -> int:
        """單幀時距，也就是本模組能分辨的時間下限。

        ToF 30 Hz = 33.3 ms/幀，比 `$M` 的 32 ms 略粗。唇動先行量若真的
        只有 50–150 ms，**光是兩個模態各自的量化誤差就佔了 33 + 32 = 65 ms**
        ——那不是可以忽略的小數。見 `onset.py` 的誤差預算。
        """
        return int(round(1e6 / TOF_NOMINAL_FPS))

    def to_dict(self) -> dict:
        seg = self.primary
        return {
            "applicable": self.applicable,
            "detected": self.detected,
            "reason": self.reason,
            "lip_onset_us": seg.start_us if seg else None,
            "lip_offset_us": seg.end_us if seg else None,
            "lip_confidence": self.confidence if seg else None,
            "enter_threshold": self.enter_threshold,
            "exit_threshold": self.exit_threshold,
            "energy_mu": self.energy_mu,
            "energy_sigma": self.energy_sigma,
            "analytic_sigma": self.analytic_sigma,
            "baseline_suspect": self.baseline_suspect,
            "n_frames": self.n_frames,
            "n_frames_dropped": self.n_frames_dropped,
            "n_zones_used": self.n_zones_used,
            "excluded_zones": list(self.excluded_zones),
            "n_segments": len(self.segments),
            "discarded_short_segments": self.discarded_short_segments,
            "quantization_us": self.quantization_us,
        }


def excluded_from_quality(report) -> "list[int]":
    """從 `B10` 的 `ZoneQualityReport` 取出所有不該參與偵測的 zone。

    三種都要排除，尤其 `no_signal`：那些 zone 的 `baseline_mu`/`sigma` 是
    `NaN`（`compute_zone_stats()` 對整段無效的 zone 就是這個結果），除下去
    整條能量訊號會變成 `NaN`，而 `NaN` 比較永遠是 False——狀態機會安靜地
    什麼都偵測不到，不會有任何錯誤訊息。
    """
    if report is None:
        return []
    zones = set()
    for name in ("no_signal_zones", "unstable_zones", "suspect_zero_variance_zones"):
        zones.update(getattr(report, name, None) or [])
    return sorted(int(z) for z in zones)


def analytic_energy_floor(n_zones: int) -> "tuple[float, float]":
    """靜止時能量訊號的理論 `(mu, sigma)`。見模組說明。

    **這是 sigma 的下界**：zone 之間相關會讓真實值更大。當成交叉檢查用，
    不要直接拿來當閾值——理論值偏小，閾值就會偏鬆。
    """
    n = max(1, int(n_zones))
    return REST_MEAN_ABS_Z, math.sqrt(REST_VAR_ABS_Z / n)


def estimate_energy_floor(energy: Sequence[float]) -> "tuple[float, float]":
    """用 median + MAD 穩健估計能量訊號的靜止分布。

    **刻意不用 mean/std。** 一段 trial 裡有 20% 左右的時間在動作，mean/std
    會被那段拉高，閾值跟著變鬆，於是動作偵測不到——而且不會報錯。中位數
    與 MAD 對這種程度的污染不敏感。

    **穩健不等於免疫。** 動作佔比 d 時，中位數會落在靜止分布的
    `0.5/(1-d)` 分位數上：d=0.2 → 62.5 分位 ≈ +0.32σ。也就是估出來的
    `mu` 會**略高於**真正的靜止值，閾值跟著**略嚴**。

    `sigma` 同樣會被撐大（實測 d=0.2 時 0.15 → 0.22，約 1.5 倍）。兩者
    合起來讓進入閾值從 `0.80+3×0.15=1.25` 變成 `0.86+3×0.22=1.54`，
    **嚴了約 23%**。

    這個殘餘偏差的方向是對的：對 `B16` 而言，ToF 端的**假觸發**才是致命的
    （它會直接偽造出「唇動比較早」的結論），寧可偏嚴而漏掉一點，也不要偏
    鬆而多出來。對照組是同一批資料的 mean/std——平均值會被拉到 1.8，
    閾值鬆到什麼都偵測不到。

    **但偏嚴 23% 仍然是偏差。** 拿得到乾淨的靜止資料時（`B10` 的 baseline
    期間保證沒有動作），請把那段算好的 `energy_mu`/`energy_sigma` 明確傳給
    `detect_lip_activity()`，那比從含動作的 trial 自己估準得多。
    """
    values = np.asarray([v for v in energy if np.isfinite(v)], dtype=np.float64)
    if values.size == 0:
        return float("nan"), float("nan")
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    return median, mad * MAD_TO_SIGMA


def zone_energy(
    tof: np.ndarray,
    baseline_mu: np.ndarray,
    baseline_sigma: np.ndarray,
    *,
    excluded_zones: Iterable[int] = (),
    include_signal: bool = False,
    sigma_floor: float = QUANTIZATION_SIGMA_MM,
) -> "tuple[np.ndarray, np.ndarray, int]":
    """把 `(T, 2*Z)` 的 ToF 幀轉成 `(T,)` 的逐幀能量。

    回傳 `(energy, usable_mask, n_zones_used)`。`usable_mask` 標出哪些幀
    至少還有一個有效 zone——整幀全無效時 `energy` 是 `NaN`，呼叫端要把
    那些幀**連同時間戳一起丟掉**，不要補值。

    σ 的下限守衛見 `QUANTIZATION_SIGMA_MM`——**不是** §3.2 的 `1e-3`。
    """
    tof = np.asarray(tof, dtype=np.float64)
    if tof.ndim != 2:
        raise ValueError(f"tof 必須是 (T, C) 二維陣列，收到 {tof.shape}")
    if tof.shape[1] % 2 != 0:
        raise ValueError(
            f"tof 的通道數必須是偶數（前半距離、後半 signal），收到 {tof.shape[1]}"
        )

    # zone 數從實際形狀推，**不寫死 16**：4×4 是 (T, 32)、8×8 是 (T, 128)。
    # 寫死的話 8×8 只會取到 64 個距離通道裡的前 16 個，安靜地少看四分之三
    # 的網格。
    n_zones = tof.shape[1] // 2
    n_channels = tof.shape[1] if include_signal else n_zones
    mu = np.asarray(baseline_mu, dtype=np.float64)[:n_channels]
    sigma = np.asarray(baseline_sigma, dtype=np.float64)[:n_channels]
    if mu.shape[0] != n_channels or sigma.shape[0] != n_channels:
        raise ValueError(
            f"baseline 維度不符：需要至少 {n_channels}，收到 mu={mu.shape} sigma={sigma.shape}"
        )

    keep = np.ones(n_channels, dtype=bool)
    for zone in excluded_zones:
        zone = int(zone)
        # signal 通道與距離通道共用 zone 編號：排除 zone k 就把 k 與
        # k+n_zones 一起排除（含 signal 時才有後者）。
        for idx in (zone, zone + n_zones):
            if 0 <= idx < n_channels:
                keep[idx] = False
    # baseline 本身是 NaN 的 zone 一律排除——那是 `no_signal_zones` 的樣子，
    # 留著會讓整條能量訊號變 NaN。
    keep &= np.isfinite(mu) & np.isfinite(sigma)

    if not keep.any():
        empty = np.full(tof.shape[0], np.nan)
        return empty, np.zeros(tof.shape[0], dtype=bool), 0

    values = tof[:, :n_channels][:, keep]
    z = (values - mu[keep]) / np.maximum(sigma[keep], sigma_floor)

    with np.errstate(invalid="ignore", all="ignore"):
        import warnings as _warnings

        with _warnings.catch_warnings():
            # 整幀全無效時 nanmean 對空 slice 取平均——預期中會發生，
            # 由 usable_mask 處理，不是程式錯誤。
            _warnings.simplefilter("ignore", category=RuntimeWarning)
            energy = np.nanmean(np.abs(z), axis=1)

    usable = np.isfinite(energy)
    # 回報「用了幾個 zone」而不是「幾個通道」——理論 sigma 是對 zone 數算的。
    return energy, usable, int(keep[:n_zones].sum())


def detect_lip_activity(
    tof: np.ndarray,
    t_us: Sequence[int],
    baseline_mu: np.ndarray,
    baseline_sigma: np.ndarray,
    *,
    excluded_zones: Iterable[int] = (),
    include_signal: bool = False,
    sigma_floor: float = QUANTIZATION_SIGMA_MM,
    energy_mu: Optional[float] = None,
    energy_sigma: Optional[float] = None,
    enter_sigma: float = MATCHED_ENTER_SIGMA,
    exit_sigma: float = MATCHED_EXIT_SIGMA,
    hangover_ms: float = DEFAULT_HANGOVER_MS,
    min_segment_ms: float = DEFAULT_MIN_SEGMENT_MS,
    smooth_frames: int = DEFAULT_SMOOTH_FRAMES,
    max_onset_backoff_ms: float = DEFAULT_MAX_ONSET_BACKOFF_MS,
) -> TofVadResult:
    """在一段 ToF 幀上偵測唇動起訖。

    `energy_mu`/`energy_sigma` 不給就用 `estimate_energy_floor()` 從這段
    資料自己穩健估計。給的話（例如從 baseline 期間算好的）就用給的——那
    比較乾淨，因為 baseline 期間保證沒有動作。

    **不拋例外，除非參數本身不合法。** 資料不足、baseline 缺漏都回一個
    `applicable=False` 的結果並說明原因。
    """
    tof = np.asarray(tof, dtype=np.float64)
    times = np.asarray(t_us, dtype=np.int64)
    if tof.ndim != 2:
        raise ValueError(f"tof 必須是 (T, C) 二維陣列，收到 {tof.shape}")
    if tof.shape[0] != times.shape[0]:
        raise ValueError(f"tof 幀數與 t_us 長度不符：{tof.shape[0]} vs {times.shape[0]}")

    excluded = tuple(sorted({int(z) for z in excluded_zones}))
    if baseline_mu is None or baseline_sigma is None:
        return TofVadResult(
            applicable=False, n_frames=int(tof.shape[0]), excluded_zones=excluded,
            reason="缺少 session baseline（/meta 的 baseline_mu_*/baseline_sigma_*），"
                   "無法算 z-score；請先跑 B10 的 baseline 錄製",
        )

    energy, usable, n_zones = zone_energy(
        tof, baseline_mu, baseline_sigma,
        excluded_zones=excluded, include_signal=include_signal,
        sigma_floor=sigma_floor,
    )
    if n_zones == 0:
        return TofVadResult(
            applicable=False, n_frames=int(tof.shape[0]), excluded_zones=excluded,
            reason="沒有任何可用的 zone（全部被品質檢查排除，或 baseline 全是 NaN）",
        )

    # 整幀全無效的幀連同時間戳一起丟掉，**不補值**。掛延遲是用時間算的
    # （見 hysteresis.py），所以少掉幾幀只會讓判斷變保守，不會變錯。
    n_dropped = int((~usable).sum())
    energy = energy[usable]
    times = times[usable]

    if energy.size < 2:
        return TofVadResult(
            applicable=False, n_frames=int(tof.shape[0]), n_frames_dropped=n_dropped,
            n_zones_used=n_zones, excluded_zones=excluded,
            reason=f"可用的 ToF 幀不足（{energy.size}），至少要 2 幀才談得上端點",
        )

    if np.any(np.diff(times) < 0):
        order = np.argsort(times, kind="stable")
        energy, times = energy[order], times[order]

    est_mu, est_sigma = estimate_energy_floor(energy)
    mu = float(energy_mu) if energy_mu is not None else est_mu
    sigma = float(energy_sigma) if energy_sigma is not None else est_sigma
    if not np.isfinite(mu) or not np.isfinite(sigma):
        return TofVadResult(
            applicable=False, n_frames=int(tof.shape[0]), n_frames_dropped=n_dropped,
            n_zones_used=n_zones, excluded_zones=excluded,
            reason="能量訊號的靜止分布估不出來（資料全是 NaN？）",
        )

    _, analytic_sigma = analytic_energy_floor(n_zones)

    # 估出來的 sigma 低於理論下界，代表**估計器失效**，不是「資料超級乾淨」。
    # 最常見的成因是量化：`$T` 的距離是整數 mm，當某段資料超過一半的幀是
    # 同一個整數時，MAD 會**剛好是 0**，閾值跟著塌成 mu + 3×1e-3——幾乎任何
    # 東西都會觸發。這裡夾到理論下界，並在 reason 講出來。
    degenerate = not (sigma > analytic_sigma)
    if degenerate:
        sigma = analytic_sigma

    suspect = bool(sigma > BASELINE_SUSPECT_SIGMA_RATIO * analytic_sigma)

    segments, discarded, enter_thr, exit_thr = detect_segments(
        energy, times, mu, sigma,
        enter_sigma=enter_sigma, exit_sigma=exit_sigma,
        hangover_ms=hangover_ms, min_segment_ms=min_segment_ms,
        smooth_frames=smooth_frames, max_onset_backoff_ms=max_onset_backoff_ms,
    )

    notes = []
    if degenerate:
        notes.append(
            f"能量 sigma 的穩健估計（{est_sigma:.4f}）低於理論下界"
            f"（{analytic_sigma:.4f}），多半是距離被量化成整數 mm 導致 MAD 塌掉；"
            f"已夾到理論下界"
        )
    if suspect:
        # 放在最前面：baseline 不可信往往**正是**什麼都偵測不到（或偵測到
        # 假東西）的原因，被「沒偵測到」蓋掉的話就查不出根因了。
        notes.append(
            f"能量的 sigma（{sigma:.3f}）超過理論下界（{analytic_sigma:.3f}）的 "
            f"{BASELINE_SUSPECT_SIGMA_RATIO:.0f} 倍——baseline 可能過期或戴法變了，"
            "偵測結果仍然回傳，但先別拿去做時間差統計"
        )
    if not segments:
        notes.append("沒有任何幀越過進入閾值（靜止，或動作太小）")
    reason = "；".join(notes) or None

    return TofVadResult(
        applicable=True,
        segments=tuple(segments),
        reason=reason,
        enter_threshold=enter_thr,
        exit_threshold=exit_thr,
        energy_mu=mu,
        energy_sigma=sigma,
        analytic_sigma=analytic_sigma,
        baseline_suspect=suspect,
        n_frames=int(tof.shape[0]),
        n_frames_dropped=n_dropped,
        n_zones_used=n_zones,
        excluded_zones=excluded,
        discarded_short_segments=discarded,
    )


def detect_from_events(
    events: Sequence[dict],
    baseline_mu: np.ndarray,
    baseline_sigma: np.ndarray,
    *,
    sensor: str = "A",
    **kwargs,
) -> TofVadResult:
    """吃 `host/capture/protocol.py` 解出來的事件串，挑出某一顆感測器的
    `$T` 幀來做偵測。

    無效 zone 在事件裡是 `None`（§1.1 的 `-1` 成對語意），這裡轉成 `NaN`
    ——`NaN` 會被 `nanmean` 正確忽略，而 `None` 進 numpy 會變 object 陣列。
    協定 v1 的 `$T` 沒有 `t_us`，同 `B15` 的處理：直接回 `applicable=False`。
    """
    frames = [e for e in events if e.get("type") == "tof" and e.get("sensor") == sensor]
    if not frames:
        return TofVadResult(applicable=False, reason=f"事件串裡沒有感測器 {sensor} 的 $T 幀")
    if any(e.get("t_us") is None for e in frames):
        return TofVadResult(
            applicable=False, n_frames=len(frames),
            reason="協定 v1 的 $T 沒有 t_us，無法做端點偵測（請升級韌體到 proto=2）",
        )

    n_zones = frames[0]["dim"]
    rows = []
    for event in frames:
        distance = [np.nan if v is None else float(v) for v in event["distance"]]
        signal = [np.nan if v is None else float(v) for v in event["signal"]]
        rows.append(distance + signal)
    tof = np.asarray(rows, dtype=np.float64)
    if tof.shape[1] != 2 * n_zones:
        return TofVadResult(
            applicable=False, n_frames=len(frames),
            reason=f"$T 的 zone 數在這段期間變動過（dim={n_zones}），請分段處理",
        )
    return detect_lip_activity(
        tof, [e["t_us"] for e in frames], baseline_mu, baseline_sigma, **kwargs
    )
