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
from host.vad.tof_vad import estimate_energy_floor, zone_energy

BASELINE_DURATION_S = 30.0

# 距離通道（[0:16]）的穩定度門檻，story 原文給的具體數字。signal 通道
# （[16:32]）不是用 mm 量測，這個門檻不適用於它。
SIGMA_INSTABILITY_THRESHOLD_MM = 2.0

# sigma 小到這個程度，通常不是「非常穩定」，而是這個 zone 整段根本沒有
# 真正的回波在變化（例如量測到牆壁而不是嘴唇，或感測器沒對準）。
# 這是警告，不強制重錄——跟「太不穩定」是相反方向的可疑訊號。
SIGMA_NEAR_ZERO_THRESHOLD_MM = 1e-6

# `compute_noise_floor()` 用 mean/std：baseline 期間如果混進了語音（受試者
# 忍不住講了話、旁邊有人說話），少數幾幀突然變大聲的樣本會把 mean/std
# 明顯拉高，但幾乎不會動到 median/MAD（對離群值不敏感的穩健估計）。
# 兩者差太多，就是「這段錄音看起來不像純底噪」的第四種訊號——只警告，
# 不強制重錄（跟 sigma 太大/太小/沒訊號那三種一樣是獨立的判準，見
# `check_zone_quality`；這個判準是 B15 的作者事後指出的，針對音訊，
# story 原文沒有要求，`B10` 的三級品質判斷不變）。
NOISE_FLOOR_CONTAMINATION_RATIO = 1.5
# 常態分布下，median absolute deviation 換算成 sigma 的係數。
MAD_TO_SIGMA = 1.4826


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
    noise_floor_warning: Optional[str] = None  # 見 NOISE_FLOOR_CONTAMINATION_RATIO；不影響 ok
    # B21：ToF 唇動偵測（host/vad/tof_vad.py 的 detect_lip_activity()）的
    # 能量門檻，從 baseline 期間（保證沒有動作）算的，不是從含動作的 trial
    # 自己估——後者 B16 量過會偏嚴約 23%，唇動起點被系統性判太晚，直接
    # 影響 D14「唇動先行量」的結論。只算 sensor A（B21 目前只用 sensor A
    # 做偵測，沒有雙感測器融合策略）。
    energy_mu: Optional[float] = None
    energy_sigma: Optional[float] = None

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
            "noise_floor_warning": self.noise_floor_warning,
            "energy_mu": self.energy_mu, "energy_sigma": self.energy_sigma,
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


def check_noise_floor_contamination(
    mic_rms: np.ndarray, sigma: float,
    ratio_threshold: float = NOISE_FLOOR_CONTAMINATION_RATIO,
) -> Optional[str]:
    """`mean`/`std` 對離群值很敏感：baseline 期間如果混進幾句話、或旁邊
    有人講話，這幾幀會把 `sigma` 明顯拉高，但幾乎不會動到 median/MAD
    （中位數與中位數絕對偏差，對離群值不敏感的穩健估計）。兩者差太多，
    就是「這段錄音看起來不像純底噪」的訊號——回一句警告字串，`None`
    代表沒有異狀。**只警告，不影響 `evaluate_baseline()` 的 `ok`。**
    """
    median = float(np.median(mic_rms))
    mad = float(np.median(np.abs(mic_rms - median)))
    robust_sigma = MAD_TO_SIGMA * mad

    if robust_sigma < 1e-9:
        # 穩健 sigma 幾乎是 0（樣本幾乎都一樣），用比例會除以接近 0 爆掉；
        # 這時只看 sigma 本身是否明顯偏大（絕對門檻，不是這裡的重點路徑，
        # 純粹避免除零讓函式壞掉）。
        return (
            f"mic_rms 的標準差（{sigma:.1f}）跟穩健估計（MAD 換算 σ≈{robust_sigma:.2f}）差異很大，"
            "底噪錄音可能混進了語音，建議檢查這段是否真的完全安靜"
        ) if sigma > 50.0 else None

    ratio = sigma / robust_sigma
    if ratio > ratio_threshold:
        return (
            f"mic_rms 的標準差（{sigma:.1f}）是穩健估計（MAD 換算 σ≈{robust_sigma:.1f}）的 "
            f"{ratio:.1f} 倍，底噪錄音可能混進了語音，建議檢查這段是否真的完全安靜"
        )
    return None


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
    noise_floor_warning = check_noise_floor_contamination(mic_rms, noise_sigma)

    valid_counts_A = tof_valid_A.sum(axis=0)
    valid_counts_B = tof_valid_B.sum(axis=0)
    report_A = check_zone_quality(sigma_A[:TOF_VALID_DIM], valid_counts_A)
    report_B = check_zone_quality(sigma_B[:TOF_VALID_DIM], valid_counts_B)

    overall_valid_ratio = float(np.concatenate([tof_valid_A, tof_valid_B]).mean())

    # B21：能量門檻用 sensor A 自己這段乾淨的靜止資料算——跟
    # detect_lip_activity() 內部自估用的是同一組函式（zone_energy() +
    # estimate_energy_floor()），不是抄一份邏輯。baseline 期間沒有動作，
    # 這裡估出來的分布不會有 detect_lip_activity() 自己在含動作的 trial
    # 資料上估計時那 23% 的系統性偏嚴。
    energy_A, _, _ = zone_energy(tof_A, mu_A, sigma_A)
    energy_mu, energy_sigma = estimate_energy_floor(energy_A)

    return BaselineOutcome(
        ok=report_A.ok and report_B.ok,
        reason=_reason_from_reports(report_A, report_B),
        quality={"A": report_A.to_dict(), "B": report_B.to_dict()},
        baseline_mu_A=mu_A, baseline_sigma_A=sigma_A,
        baseline_mu_B=mu_B, baseline_sigma_B=sigma_B,
        noise_floor_mu=noise_mu, noise_floor_sigma=noise_sigma,
        valid_zone_ratio=overall_valid_ratio,
        noise_floor_warning=noise_floor_warning,
        energy_mu=energy_mu, energy_sigma=energy_sigma,
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

    `session_meta_base` 不能已經帶 `baseline_mu_A` 等四個 baseline 欄位、
    `noise_floor_*`，或 `energy_mu`/`energy_sigma`（B21）——這支就是負責算出
    並填入它們的地方，重複給會被覆蓋，容易誤以為呼叫端要自己算，所以刻意
    不接受呼叫端傳這些鍵。

    ⚠️ `energy_mu`/`energy_sigma`（B21，給 `host.vad.tof_vad.detect_lip_activity()`
    用）：目前 `session_writer.py` 還沒有這兩個 `/meta` 欄位的寫入邏輯
    （18 正在加），這裡先把它們放進 `meta` dict——在那個改動落地前，
    `SessionWriter._write_meta()` 只認 `REQUIRED_META_KEYS`/`OPTIONAL_META_KEYS`
    裡列的鍵，多出來的鍵會被安靜地忽略（不報錯，只是不落盤），兩邊各自
    完成後自動接上，不用互相等待。
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
    meta["energy_mu"] = outcome.energy_mu
    meta["energy_sigma"] = outcome.energy_sigma

    with SessionWriter(path, meta) as writer:
        writer.write_trial(
            0, label="_baseline",
            tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
            tof_valid_A=tof_valid_A, tof_valid_B=tof_valid_B,
            mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
            wear_id=wear_id, mode=mode,
            valid_zone_ratio=outcome.valid_zone_ratio, drop_count=0,
            # baseline 沒有語音、沒有跑 VAD——四個時間戳跟 speaking_mode
            # 一律不給（SessionWriter 就整個 attr 不寫），不是填 -1 假裝
            # 「偵測過但沒找到」（B17 的調度決議：偵測不到就整個 attr 不
            # 寫入，不是填 0/-1/邊界值）。
            quality="ok",
        )

    return outcome
