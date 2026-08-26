"""VAD_FUSION -- 雙 ToF 感測器唇動偵測融合策略的合成資料實驗。

規格見 `reports/VAD_FUSION_OPTIONS.md`。這裡只做風險分析用的合成資料
實驗，**不改 `host/vad/` 任何檔案**——直接重用 `host/vad/tof_vad.py`
的 `detect_lip_activity()`／`analytic_energy_floor()`，融合策略全部在
這個檔案裡另外組裝，不重新實作偵測邏輯本身。

## 為什麼用理論（analytic）能量分布，不用 MAD 自估

`tof_vad.estimate_energy_floor()` 本身已知有 ~23% 偏嚴的殘餘偏差
（`tof_vad.py` 自己的文件字串有推導）。若這裡也用自估，融合策略造成的
偏差會跟那 23% 混在一起，分不清哪個是誰的。**這裡改用
`analytic_energy_floor(n_zones)`**（理論下界 mu=0.7979、
sigma=sqrt(0.3634/n)）當作「baseline 期間量好、乾乾淨淨餵進去」的理想
情況——`tof_vad.py` 自己的文件也說這是**建議的用法**（「拿得到乾淨的
靜止資料時，請把那段算好的 energy_mu/energy_sigma 明確傳給
detect_lip_activity」），不是為了讓數字好看而作弊。**這代表這裡量到的
融合偏差是「假設閾值估計本身已經乾淨」下的下限**，真實情況（自估）疊上
那 23% 之後，方向會不會疊加或抵消——這是本報告明講的限制之一，不是
沒想到。

## 避開的合成資料陷阱（`D22.md` 「合成資料的已知陷阱」）

- **天花板效應**：Z_TARGET（主動 zone 的目標 z-score）從「勉強能偵測到」
  掃到「非常強」，不是只測一個高訊號的點。
- **單一幾何的抽樣運氣**：每個 (Z_TARGET, coverage_B) 組合對 3 組獨立的
  「哪些 zone 會動」幾何取平均，每組幾何再對多個雜訊種子取平均——
  幾何與雜訊種子是分開的兩層隨機性，不會混在一起（避開 `D09` 踩過的
  「生成幾何」與「抽樣樣板」沒分開的陷阱）。
"""
from __future__ import annotations

import numpy as np

from host.vad.tof_vad import (
    QUANTIZATION_SIGMA_MM,
    analytic_energy_floor,
    detect_lip_activity,
)

FPS = 30.0
FRAME_US = int(round(1e6 / FPS))
N_ZONES = 16                      # 4x4，跟 mock_device/真實系統一致

BASELINE_MU_MM = 500.0            # 隨便取一個典型戴距，數值本身不影響 z
TISSUE_BASELINE_SIGMA_MM = 1.5    # 貼著皮膚/組織，會有自然微動（呼吸/脈搏）
RIGID_BASELINE_SIGMA_MM = QUANTIZATION_SIGMA_MM * 1.05  # 貼著硬表面，幾乎只剩量化雜訊

TRIAL_MS = 2500.0
ONSET_MS = 1000.0                 # 真實（ground-truth）唇動起點


def _make_sensor_tof(rng, n_zones, active_zones, z_target, baseline_sigma_mm,
                      ramp_ms=0.0):
    """造一顆感測器的合成 `(T, 2*n_zones)` ToF 陣列＋baseline。

    只有 `active_zones` 這幾格在 `ONSET_MS` 之後線性爬升到 `z_target`
    個標準差，其餘 zone 全程只有雜訊（z ~ N(0,1)）。回傳
    `(tof, t_us, baseline_mu, baseline_sigma)`。
    """
    n_frames = int(round(TRIAL_MS / 1000.0 * FPS))
    t_us = (np.arange(n_frames) * FRAME_US).astype(np.int64)
    onset_frame = int(round(ONSET_MS / 1000.0 * FPS))

    z_true = np.zeros((n_frames, n_zones))
    ramp_frames = max(1, int(round(ramp_ms / 1000.0 * FPS))) if ramp_ms > 0 else 1
    for zi in active_zones:
        for i in range(n_frames - onset_frame):
            frac = min(1.0, (i + 1) / ramp_frames)
            z_true[onset_frame + i, zi] = z_target * frac

    noise = rng.normal(size=(n_frames, n_zones))
    z_obs = z_true + noise
    distance = BASELINE_MU_MM + z_obs * baseline_sigma_mm

    tof = np.concatenate([distance, np.zeros_like(distance)], axis=1)
    baseline_mu = np.full(n_zones, BASELINE_MU_MM)
    baseline_sigma = np.full(n_zones, baseline_sigma_mm)
    return tof, t_us, baseline_mu, baseline_sigma


def _detect(tof, t_us, baseline_mu, baseline_sigma):
    n_zones = baseline_mu.shape[0]
    mu, sigma = analytic_energy_floor(n_zones)
    return detect_lip_activity(
        tof, t_us, baseline_mu, baseline_sigma,
        energy_mu=mu, energy_sigma=sigma,
    )


def _onset_us(result):
    seg = result.primary
    return None if seg is None else seg.start_us


def run_one_trial(rng, coverage_a, coverage_b, z_target, n_zones=N_ZONES,
                   active_idx_a=None, active_idx_b=None,
                   baseline_sigma_a=TISSUE_BASELINE_SIGMA_MM,
                   baseline_sigma_b=TISSUE_BASELINE_SIGMA_MM):
    """跑一次合成 trial，回傳每種融合策略的（偵測到與否, onset_us）。

    `active_idx_a`/`active_idx_b`：這次幾何裡哪些 zone 真的會動——同一組
    幾何要能被多個雜訊種子重複使用，所以由呼叫端決定並傳進來，這裡不
    重新抽。
    """
    tof_a, t_us, mu_a, sig_a = _make_sensor_tof(
        rng, n_zones, active_idx_a, z_target, baseline_sigma_a)
    tof_b, _, mu_b, sig_b = _make_sensor_tof(
        rng, n_zones, active_idx_b, z_target, baseline_sigma_b)

    result_a = _detect(tof_a, t_us, mu_a, sig_a)
    result_b = _detect(tof_b, t_us, mu_b, sig_b)
    onset_a, onset_b = _onset_us(result_a), _onset_us(result_b)

    # 合併能量：兩顆感測器的 zone 併成一組（2*n_zones），跑同一次偵測。
    tof_merged = np.concatenate([tof_a[:, :n_zones], tof_b[:, :n_zones],
                                  np.zeros((tof_a.shape[0], 2 * n_zones))], axis=1)
    mu_merged = np.concatenate([mu_a, mu_b])
    sig_merged = np.concatenate([sig_a, sig_b])
    result_merged = _detect(tof_merged, t_us, mu_merged, sig_merged)
    onset_merged = _onset_us(result_merged)

    # 品質門控：baseline_sigma 貼近量化下限的那顆，視為「沒有真的貼著
    # 組織」，融合時整顆排除。
    def _looks_rigid(sigma_arr):
        return bool(np.median(sigma_arr) < 1.5 * QUANTIZATION_SIGMA_MM)

    a_rigid, b_rigid = _looks_rigid(sig_a), _looks_rigid(sig_b)
    if a_rigid and not b_rigid:
        onset_gated = onset_b
    elif b_rigid and not a_rigid:
        onset_gated = onset_a
    else:
        onset_gated = onset_merged   # 兩顆都正常，或兩顆都可疑 -> 照樣合併

    onset_union = None
    if onset_a is not None and onset_b is not None:
        onset_union = min(onset_a, onset_b)
    elif onset_a is not None:
        onset_union = onset_a
    elif onset_b is not None:
        onset_union = onset_b

    onset_intersection = None
    if onset_a is not None and onset_b is not None:
        onset_intersection = max(onset_a, onset_b)

    return {
        "single_a": onset_a,
        "union_min": onset_union,
        "intersection_max": onset_intersection,
        "merged_energy": onset_merged,
        "quality_gated": onset_gated,
    }


STRATEGIES = ("single_a", "union_min", "intersection_max", "merged_energy", "quality_gated")


def sweep_coverage_and_amplitude(
    coverage_a=0.5, coverage_b_values=(0.5, 0.3, 0.15, 0.0),
    z_target_values=(3.5, 5.0, 8.0, 15.0),
    n_geometries=3, n_noise_seeds=30, n_zones=N_ZONES, geometry_seed_base=1000,
):
    """主掃描：對每個 (coverage_b, z_target) 組合，算每種策略的偵測率與
    onset 偏差（相對 `ONSET_MS` 的 ground truth，單位 ms，正值代表偵測
    偏晚）。

    幾何（哪些 zone 會動）與雜訊種子分開抽樣——幾何固定 `n_geometries`
    組，每組幾何底下再跑 `n_noise_seeds` 個獨立雜訊實現，全部平均。
    """
    n_active_a = max(1, int(round(coverage_a * n_zones)))
    rows = []
    for z_target in z_target_values:
        for coverage_b in coverage_b_values:
            n_active_b = max(0, int(round(coverage_b * n_zones)))
            per_strategy_bias = {s: [] for s in STRATEGIES}
            per_strategy_detected = {s: 0 for s in STRATEGIES}
            total = 0
            for g in range(n_geometries):
                geo_rng = np.random.default_rng(geometry_seed_base + g)
                active_idx_a = geo_rng.choice(n_zones, size=n_active_a, replace=False)
                active_idx_b = (geo_rng.choice(n_zones, size=n_active_b, replace=False)
                                if n_active_b > 0 else np.array([], dtype=int))
                for s_i in range(n_noise_seeds):
                    rng = np.random.default_rng(geometry_seed_base * 100_000 + g * 10_000 + s_i)
                    onsets = run_one_trial(
                        rng, coverage_a, coverage_b, z_target, n_zones,
                        active_idx_a=active_idx_a, active_idx_b=active_idx_b,
                    )
                    total += 1
                    for strat, onset_us in onsets.items():
                        if onset_us is not None:
                            per_strategy_detected[strat] += 1
                            bias_ms = (onset_us - ONSET_MS * 1000.0) / 1000.0
                            per_strategy_bias[strat].append(bias_ms)
            row = {"z_target": z_target, "coverage_a": coverage_a, "coverage_b": coverage_b}
            for s in STRATEGIES:
                biases = per_strategy_bias[s]
                row[f"{s}_detect_rate"] = per_strategy_detected[s] / total
                row[f"{s}_bias_ms_mean"] = float(np.mean(biases)) if biases else None
                row[f"{s}_bias_ms_std"] = float(np.std(biases)) if biases else None
            rows.append(row)
    return rows


def sweep_rigid_sensor_scenario(
    n_geometries=3, n_noise_seeds=30, n_zones=N_ZONES, geometry_seed_base=2000,
    z_target=8.0, coverage_a=0.5,
):
    """假設 2 的具體情境：sensor B 完全沒貼到組織（`coverage_b=0`），
    baseline_sigma 從「貼組織」一路掃到「貼硬表面」，看
    `merged_energy`（不知道 B 是瞎的）跟 `quality_gated`（用 baseline
    自己判斷）差多少。

    這裡才是真正測 Q3（baseline 品質門控）的地方——主掃描
    （`sweep_coverage_and_amplitude`）兩顆感測器 baseline_sigma 恆定，
    `quality_gated` 在那裡永遠等於 `merged_energy`，測不出差異。
    """
    sigma_b_values = (
        TISSUE_BASELINE_SIGMA_MM,
        TISSUE_BASELINE_SIGMA_MM * 0.5,
        QUANTIZATION_SIGMA_MM * 3.0,
        RIGID_BASELINE_SIGMA_MM,
    )
    n_active_a = max(1, int(round(coverage_a * n_zones)))
    rows = []
    for sigma_b in sigma_b_values:
        per_strategy_bias = {s: [] for s in STRATEGIES}
        per_strategy_detected = {s: 0 for s in STRATEGIES}
        total = 0
        for g in range(n_geometries):
            geo_rng = np.random.default_rng(geometry_seed_base + g)
            active_idx_a = geo_rng.choice(n_zones, size=n_active_a, replace=False)
            active_idx_b = np.array([], dtype=int)   # B 完全不動：真的瞎了
            for s_i in range(n_noise_seeds):
                rng = np.random.default_rng(geometry_seed_base * 100_000 + g * 10_000 + s_i)
                onsets = run_one_trial(
                    rng, coverage_a, 0.0, z_target, n_zones,
                    active_idx_a=active_idx_a, active_idx_b=active_idx_b,
                    baseline_sigma_a=TISSUE_BASELINE_SIGMA_MM,
                    baseline_sigma_b=sigma_b,
                )
                total += 1
                for strat, onset_us in onsets.items():
                    if onset_us is not None:
                        per_strategy_detected[strat] += 1
                        bias_ms = (onset_us - ONSET_MS * 1000.0) / 1000.0
                        per_strategy_bias[strat].append(bias_ms)
        row = {"sigma_b_mm": sigma_b}
        for s in STRATEGIES:
            biases = per_strategy_bias[s]
            row[f"{s}_detect_rate"] = per_strategy_detected[s] / total
            row[f"{s}_bias_ms_mean"] = float(np.mean(biases)) if biases else None
        rows.append(row)
    return rows


def format_rigid_report(rows):
    lines = ["", "## sensor B 完全沒貼組織（coverage_b=0），baseline_sigma 掃描", "",
             "z_target=8.0, coverage_A=0.5，B 的 baseline_sigma 從「貼組織」掃到「貼硬表面」", ""]
    header = ("| sigma_B (mm) | " +
              " | ".join(f"{s} 偵測率" for s in STRATEGIES) + " | " +
              " | ".join(f"{s} 偏差(ms)" for s in STRATEGIES) + " |")
    lines.append(header)
    lines.append("|" + "---|" * (1 + 2 * len(STRATEGIES)))
    for row in rows:
        cells = [f"{row['sigma_b_mm']:.3f}"]
        for s in STRATEGIES:
            cells.append(f"{row[f'{s}_detect_rate']*100:.0f}%")
        for s in STRATEGIES:
            b = row[f"{s}_bias_ms_mean"]
            cells.append("N/A" if b is None else f"{b:+.1f}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def format_report(rows, is_synthetic=True):
    lines = ["# VAD 融合策略：合成資料掃描結果"]
    if is_synthetic:
        lines += [
            "",
            "> ⚠️ **本報告使用合成資料，數字不是真實結論。** "
            "融合策略的相對排序與偏差方向是本輪重點，絕對的 ms 數字受"
            "合成訊號形狀（線性爬升、固定雜訊 sigma）影響，不要直接引用。",
        ]
    lines += ["", "## 每種策略：偵測率／onset 偏差（ms，正值=偏晚）", ""]
    header = ("| z_target | coverage_B | " +
              " | ".join(f"{s} 偵測率" for s in STRATEGIES) + " | " +
              " | ".join(f"{s} 偏差(ms)" for s in STRATEGIES) + " |")
    lines.append(header)
    lines.append("|" + "---|" * (2 + 2 * len(STRATEGIES)))
    for row in rows:
        cells = [f"{row['z_target']:.1f}", f"{row['coverage_b']:.2f}"]
        for s in STRATEGIES:
            cells.append(f"{row[f'{s}_detect_rate']*100:.0f}%")
        for s in STRATEGIES:
            b = row[f"{s}_bias_ms_mean"]
            cells.append("N/A" if b is None else f"{b:+.1f}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    from pathlib import Path

    rows = sweep_coverage_and_amplitude()
    report = format_report(rows)

    rigid_rows = sweep_rigid_sensor_scenario()
    report += "\n" + format_rigid_report(rigid_rows)

    print(report)

    out_path = Path(__file__).resolve().parents[2] / "reports" / "vad_fusion_scan.md"
    out_path.write_text(report + "\n")
    print(f"\n報告已寫入 {out_path}")
