"""B10 — Session baseline 自動錄製。

baseline 必須是**同一次戴上**時錄的（CONTRACTS.md §2.1 的兩個不可妥協設計
決定之一）：`baseline_mu_A/B`、`baseline_sigma_A/B`（各 32 維）與
`noise_floor_mu/sigma` 是整條分析鏈的地基——`D01` 的 per-zone z-score
直接除以 `baseline_sigma`，`C06` 的 Δ 熱力圖是「當前 − baseline_mu」，
`D11`/`D13` 全部建立在 z-score 之上。**baseline 錄壞了，後面每一個數字
都是錯的，而且不會有任何地方報錯**——這是這支模組存在的唯一理由。

**不依賴 VAD（B15 還沒做）。** baseline 期間是固定 30 秒 + 明確的使用者
提示「保持不動、不要出聲」，不是自動偵測靜止/靜音再開始計時。`B15`
完成後，可以升級成「偵測到穩定靜止才開始算 30 秒」，這裡先用最簡單、
確定能動的版本。

**不用 `config/session_targets.json` 的目標距離判斷 baseline 好不好**
（那組數字在 `E01` 上機量測前是 `null`，見 `B09`）。這裡的品質判準是
`valid_zone_ratio`（有多少 zone-frame 組合是有效回波）跟 per-zone 的
`sigma`（穩不穩），兩者都不需要知道「應該量到多遠」。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from host.storage.session_writer import SessionWriter, TOF_VALID_DIM

BASELINE_DURATION_S = 30.0

# 距離通道（[0:16]）的穩定度門檻，story 原文給的具體數字。signal 通道
# （[16:32]）不是用 mm 量測，這個門檻不適用於它。
SIGMA_INSTABILITY_THRESHOLD_MM = 2.0

# sigma 小到這個程度，通常不是「非常穩定」，而是這個 zone 整段根本沒有
# 真正的回波在變化（例如量測到牆壁而不是嘴唇，或感測器沒對準）。
# 這是警告，不強制重錄——跟「太不穩定」是相反方向的可疑訊號。
SIGMA_NEAR_ZERO_THRESHOLD_MM = 1e-6


@dataclass
class ZoneQualityReport:
    """一顆感測器（16 個 zone）的 baseline 品質檢查結果。"""
    ok: bool
    unstable_zones: List[int]
    no_signal_zones: List[int]
    suspect_zero_variance_zones: List[int]
    valid_zone_ratio: float

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "unstable_zones": list(self.unstable_zones),
            "no_signal_zones": list(self.no_signal_zones),
            "suspect_zero_variance_zones": list(self.suspect_zero_variance_zones),
            "valid_zone_ratio": self.valid_zone_ratio,
        }


@dataclass
class BaselineOutcome:
    ok: bool
    reason: Optional[str]
    quality: dict  # {"A": ZoneQualityReport.to_dict(), "B": ...}
    baseline_mu_A: Optional[np.ndarray] = None
    baseline_sigma_A: Optional[np.ndarray] = None
    baseline_mu_B: Optional[np.ndarray] = None
    baseline_sigma_B: Optional[np.ndarray] = None
    noise_floor_mu: Optional[float] = None
    noise_floor_sigma: Optional[float] = None
    valid_zone_ratio: Optional[float] = None

    def to_dict(self) -> dict:
        def _list_or_none(x):
            return None if x is None else np.asarray(x).tolist()

        return {
            "ok": self.ok, "reason": self.reason, "quality": self.quality,
            "baseline_mu_A": _list_or_none(self.baseline_mu_A),
            "baseline_sigma_A": _list_or_none(self.baseline_sigma_A),
            "baseline_mu_B": _list_or_none(self.baseline_mu_B),
            "baseline_sigma_B": _list_or_none(self.baseline_sigma_B),
            "noise_floor_mu": self.noise_floor_mu, "noise_floor_sigma": self.noise_floor_sigma,
            "valid_zone_ratio": self.valid_zone_ratio,
        }


def compute_zone_stats(tof_values: np.ndarray) -> "tuple[np.ndarray, np.ndarray]":
    """`tof_values`: `(T, 32)` float32，`[0:16]` 距離 mm、`[16:32]` signal/100，
    無效值是 `NaN`（B07 的約定）。回傳 `(mu, sigma)`，各 `(32,)`，用 nan-aware
    統計自動忽略無效樣本。整段都無效的 zone，`mu`/`sigma` 會是 `NaN`——
    呼叫端（`check_zone_quality`）要處理這個情況，不能直接拿去用。
    """
    with np.errstate(invalid="ignore", all="ignore"), warnings.catch_warnings():
        # 整段無效的 zone 讓 nanmean/nanstd 對空 slice 取平均——這是預期
        # 中會發生的情況（no_signal_zones 就是靠這個判斷），不是程式錯誤。
        warnings.simplefilter("ignore", category=RuntimeWarning)
        mu = np.nanmean(tof_values, axis=0)
        sigma = np.nanstd(tof_values, axis=0)
    return mu, sigma


def compute_noise_floor(mic_rms: np.ndarray) -> "tuple[float, float]":
    """音訊底噪的 μ/σ，供 `B15`（VAD）的閾值使用。不需要無效值處理——
    `mic_rms` 沒有「無效樣本」的概念（§1.1 沒有為 `$M` 定義哨兵值）。"""
    return float(np.mean(mic_rms)), float(np.std(mic_rms))


def check_zone_quality(
    sigma_distance: np.ndarray, valid_counts: np.ndarray,
    instability_threshold_mm: float = SIGMA_INSTABILITY_THRESHOLD_MM,
    near_zero_threshold_mm: float = SIGMA_NEAR_ZERO_THRESHOLD_MM,
) -> ZoneQualityReport:
    """`sigma_distance`/`valid_counts` 都是 `(16,)`——只看距離通道，
    signal 通道不是用 mm 量測，2mm 門檻對它沒有意義。

    三種情況：
    * `unstable_zones`：sigma 太大，真的在動 → **擋，要求重錄**。
    * `no_signal_zones`：整段完全沒有效樣本（沒對準/太遠）→ **也擋**，
      這種 zone 的 `mu`/`sigma` 是 `NaN`，寫進 `/meta` 只會讓下游的
      z-score 靜默壞掉，等於重演 story 想避免的那個問題。
    * `suspect_zero_variance_zones`：有樣本、但幾乎零變異 → **警告，不擋**，
      story 沒有要求為這個重錄，只要求「偵測並警告」。
    """
    n_zones = len(sigma_distance)
    no_signal = [i for i in range(n_zones) if valid_counts[i] == 0]
    with np.errstate(invalid="ignore"):
        unstable = [
            i for i in range(n_zones)
            if valid_counts[i] > 0 and sigma_distance[i] > instability_threshold_mm
        ]
        suspect = [
            i for i in range(n_zones)
            if valid_counts[i] > 0 and sigma_distance[i] < near_zero_threshold_mm
        ]

    valid_zone_ratio = 1.0 - (len(no_signal) / n_zones) if n_zones else 0.0

    return ZoneQualityReport(
        ok=not unstable and not no_signal,
        unstable_zones=unstable, no_signal_zones=no_signal,
        suspect_zero_variance_zones=suspect, valid_zone_ratio=valid_zone_ratio,
    )


def _reason_from_reports(report_A: ZoneQualityReport, report_B: ZoneQualityReport) -> Optional[str]:
    reasons = []
    if report_A.unstable_zones or report_B.unstable_zones:
        reasons.append("baseline unstable")
    if report_A.no_signal_zones or report_B.no_signal_zones:
        reasons.append("no signal in some zones")
    return "; ".join(reasons) if reasons else None


def evaluate_baseline(tof_A: np.ndarray, tof_valid_A: np.ndarray,
                       tof_B: np.ndarray, tof_valid_B: np.ndarray,
                       mic_rms: np.ndarray) -> BaselineOutcome:
    """純函式：算 baseline 統計量、判斷品質，**不寫任何檔案**。
    `ok=False` 時 `baseline_*`/`noise_floor_*` 一樣算好回傳（方便除錯、
    前端顯示），但呼叫端（`capture_baseline_trial`）不會拿去寫進 HDF5。
    """
    mu_A, sigma_A = compute_zone_stats(tof_A)
    mu_B, sigma_B = compute_zone_stats(tof_B)
    noise_mu, noise_sigma = compute_noise_floor(mic_rms)

    valid_counts_A = tof_valid_A.sum(axis=0)
    valid_counts_B = tof_valid_B.sum(axis=0)
    report_A = check_zone_quality(sigma_A[:TOF_VALID_DIM], valid_counts_A)
    report_B = check_zone_quality(sigma_B[:TOF_VALID_DIM], valid_counts_B)

    overall_valid_ratio = float(np.concatenate([tof_valid_A, tof_valid_B]).mean())

    return BaselineOutcome(
        ok=report_A.ok and report_B.ok,
        reason=_reason_from_reports(report_A, report_B),
        quality={"A": report_A.to_dict(), "B": report_B.to_dict()},
        baseline_mu_A=mu_A, baseline_sigma_A=sigma_A,
        baseline_mu_B=mu_B, baseline_sigma_B=sigma_B,
        noise_floor_mu=noise_mu, noise_floor_sigma=noise_sigma,
        valid_zone_ratio=overall_valid_ratio,
    )


def capture_baseline_trial(
    path, session_meta_base: dict, *,
    tof_A, tof_B, tof_t_us, tof_valid_A, tof_valid_B,
    mic_rms, mic_peak, mic_t_us,
    wear_id, mode,
) -> BaselineOutcome:
    """算 baseline，品質不過就回 `ok=False`（不建立/不動任何檔案，呼叫端
    照 story 要求「回報並要求重錄」）；品質過關就把 baseline 統計併進
    `session_meta_base`（`clock_*`/`subject`/... 這些 `/meta` 其他必填欄位
    由呼叫端準備好傳進來，這支不負責湊）、開一個新的 `SessionWriter`，
    以 `label="_baseline"` 寫成 `trial_000`。

    `session_meta_base` 不能已經帶 `baseline_mu_A` 等四個 baseline 欄位或
    `noise_floor_*`——這支就是負責算出並填入它們的地方，重複給會被覆蓋，
    容易誤以為呼叫端要自己算，所以刻意不接受呼叫端傳這些鍵。
    """
    outcome = evaluate_baseline(tof_A, tof_valid_A, tof_B, tof_valid_B, mic_rms)
    if not outcome.ok:
        return outcome

    meta = dict(session_meta_base)
    meta["baseline_mu_A"] = outcome.baseline_mu_A
    meta["baseline_sigma_A"] = outcome.baseline_sigma_A
    meta["baseline_mu_B"] = outcome.baseline_mu_B
    meta["baseline_sigma_B"] = outcome.baseline_sigma_B
    meta["noise_floor_mu"] = outcome.noise_floor_mu
    meta["noise_floor_sigma"] = outcome.noise_floor_sigma

    with SessionWriter(path, meta) as writer:
        writer.write_trial(
            0, label="_baseline",
            tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
            tof_valid_A=tof_valid_A, tof_valid_B=tof_valid_B,
            mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
            wear_id=wear_id, mode=mode,
            valid_zone_ratio=outcome.valid_zone_ratio, drop_count=0,
            vad_start_us=-1, vad_end_us=-1, lip_onset_us=-1, voice_onset_us=-1,
            quality="ok",
        )

    return outcome
