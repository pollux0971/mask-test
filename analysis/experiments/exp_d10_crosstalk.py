"""D10 — 實驗 C₀：Crosstalk 分析。

規格見 `stories/D-analysis/D10.md`。比較「solo」（單一感測器獨立運作）與
「dual」（兩顆同時運作）兩種組態下的讀數差異，量化兩顆 VL53L7CX 是否
互相干擾。

**`ambient_per_spad` 的資料形狀凍結於 CONTRACTS.md §1.1.3 / §2**
（2026-08-26 為了這個 story 追加，`A16`＋`B07` 的韌體/寫入端實作尚未完工，
`ssi-backlog/tools/schema_example.py` 也還沒有 `tof_ambient_A/B/t_us`
這三個 dataset）——本模組的 pytest 自行合成這三個陣列，`A16`＋`B07`
補完後改用真實假檔即可，介面不需要變動。

**`ambient_per_spad` 是 crosstalk 最靈敏的指標，比距離偏移更早顯現。**
另一顆感測器的雷射會被接收器當成環境光，所以 ambient 上升是干擾的
直接證據，即使距離讀值還沒受影響。distance Δ < 2mm 但 ambient 明顯
上升，是「目前還沒事，但邊際很小」的訊號，報告裡要註明。

**距離 Δ 用穩態平均值而非逐幀最大值**：crosstalk 是系統性的穩態偏移，
不是瞬間雜訊尖峰；用平均值比較能反映真正的偏移，不會被單一雜訊幀誤導。
"""
import numpy as np

from analysis.experiments.exp_a_snr import zone_snr_grid

N_ZONES = 16
DISTANCE_PASS_THRESHOLD_MM = 2.0
AMBIENT_RATE_EPS = 1e-6  # solo 平均值接近 0 時的除以 0 保護

FALLBACK_RECOMMENDATION = (
    "Fallback：改用時間多工（一次只開一顆感測器）。"
    "代價：4×4 模式的幀率會從 30 Hz 掉到 15 Hz；"
    "15 Hz 對一個 500 ms 的詞只能拿到 7-8 幀，"
    "會顯著影響 D05 的 DTW 品質（幀數太少，時間扭曲的可對齊空間變小）。"
)


def _check_zone_dim(name, arr):
    if arr.shape[-1] != N_ZONES:
        raise ValueError(f"{name} 最後一維應為 {N_ZONES}，收到 {arr.shape[-1]}")


def zone_distance_delta(dist_solo, valid_solo, dist_dual, valid_dual):
    """逐 zone 的距離差：|mean(dual) - mean(solo)|，單位 mm。

    dist_*: (T, 16)；valid_*: (T, 16) bool，對應 CONTRACTS §2 的
    `tof_valid_A`/`tof_valid_B`——無效幀不列入平均。solo 與 dual 是兩次
    獨立錄音，時間長度可以不同，不需要逐幀對齊。
    """
    dist_solo = np.asarray(dist_solo, dtype=np.float64)
    dist_dual = np.asarray(dist_dual, dtype=np.float64)
    valid_solo = np.asarray(valid_solo, dtype=bool)
    valid_dual = np.asarray(valid_dual, dtype=bool)
    for name, arr in (("dist_solo", dist_solo), ("dist_dual", dist_dual)):
        _check_zone_dim(name, arr)

    masked_solo = np.where(valid_solo, dist_solo, np.nan)
    masked_dual = np.where(valid_dual, dist_dual, np.nan)
    mean_solo = np.nanmean(masked_solo, axis=0)
    mean_dual = np.nanmean(masked_dual, axis=0)
    return np.abs(mean_dual - mean_solo)


def zone_ambient_delta(ambient_solo, ambient_dual):
    """逐 zone `ambient_per_spad` 的平均值差與相對變化率。

    ambient_*: (Ta, 16)，無效 zone 依 CONTRACTS §2 已經是 NaN
    （不像距離有獨立的 valid 陣列），直接用 `nanmean` 忽略即可。
    solo 與 dual 各自的 `Ta`（ambient 取樣數）可以不同，不需要對齊。

    回傳 (delta, rate)：
        delta: (16,) 絕對差
        rate:  (16,) 相對 solo 的變化率（例如 0.3 代表上升 30%），
               solo 平均值接近 0 時用 eps 守衛避免除以 0。
    """
    ambient_solo = np.asarray(ambient_solo, dtype=np.float64)
    ambient_dual = np.asarray(ambient_dual, dtype=np.float64)
    _check_zone_dim("ambient_solo", ambient_solo)
    _check_zone_dim("ambient_dual", ambient_dual)

    mean_solo = np.nanmean(ambient_solo, axis=0)
    mean_dual = np.nanmean(ambient_dual, axis=0)
    delta = mean_dual - mean_solo
    rate = delta / np.maximum(np.abs(mean_solo), AMBIENT_RATE_EPS)
    return delta, rate


def crosstalk_verdict(zone_delta_mm, threshold_mm=DISTANCE_PASS_THRESHOLD_MM):
    """PASS/FAIL 判定：所有 zone 的距離 Δ 都要 < threshold_mm 才算通過。

    回傳 dict，含實際用掉的門檻值（若呼叫端有調整，這裡記錄下來，
    不要只回傳布林值讓人猜用的是哪個門檻）。
    """
    zone_delta_mm = np.asarray(zone_delta_mm, dtype=np.float64)
    worst_zone = int(np.argmax(zone_delta_mm))
    worst_delta = float(zone_delta_mm[worst_zone])
    return {
        "passed": worst_delta < threshold_mm,
        "worst_zone": worst_zone,
        "worst_delta_mm": worst_delta,
        "threshold_mm": threshold_mm,
    }


def plot_crosstalk_heatmap(zone_delta_mm, sensor_label="A", threshold_mm=DISTANCE_PASS_THRESHOLD_MM,
                            n_rows=4, n_cols=4):
    """畫逐 zone 距離 Δ 熱力圖（圖表文字一律英文）。

    zone layout: row-major (ASSUMED, unverified — see A track/E01)，
    沿用 `exp_a_snr.zone_snr_grid` 的假設與命名，不重新定義一份。
    """
    import matplotlib.pyplot as plt

    grid = zone_snr_grid(zone_delta_mm, n_rows, n_cols)

    fig, ax = plt.subplots(figsize=(4.5, 4))
    im = ax.imshow(grid, cmap="magma")
    ax.set_title(f"Crosstalk distance delta - sensor {sensor_label} (mm)")
    ax.set_xlabel("zone column (row-major, unverified)")
    ax.set_ylabel("zone row (row-major, unverified)")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="delta (mm)")
    fig.suptitle(f"threshold={threshold_mm} mm")
    fig.tight_layout()
    return fig


def format_report(verdict_a, verdict_b, ambient_rate_a, ambient_rate_b, is_synthetic):
    """輸出 D10 的完整報告（PASS/FAIL、ambient 變化率、fallback 建議）。

    verdict_a/b: `crosstalk_verdict()` 的回傳 dict
    ambient_rate_a/b: (16,) 該感測器逐 zone 的 ambient 變化率
    is_synthetic: True 時在報告最前面加醒目警示——合成資料的數字不是結論
    """
    lines = ["# D10 — Crosstalk 分析報告"]

    if is_synthetic:
        lines += [
            "",
            "> ⚠️ **本報告使用合成資料，數字不是真實結論。**"
            "真實結論待 `A16`（韌體 `$A`/`AMB` 支援）與 `E02`（Crosstalk 資料蒐集）完成後，"
            "用真實錄音重跑本模組。",
        ]

    lines += ["", "## PASS/FAIL 判定"]
    for label, verdict in (("A", verdict_a), ("B", verdict_b)):
        status = "PASS" if verdict["passed"] else "FAIL"
        lines.append(
            f"- 感測器 {label}：**{status}**（最差 zone {verdict['worst_zone']}，"
            f"Δ={verdict['worst_delta_mm']:.3f} mm，門檻 {verdict['threshold_mm']} mm）"
        )

    lines += ["", "## Ambient 變化率（越靈敏、越早顯現的干擾指標）"]
    for label, rate in (("A", ambient_rate_a), ("B", ambient_rate_b)):
        worst_idx = int(np.argmax(np.abs(rate)))
        lines.append(
            f"- 感測器 {label}：最大變化率在 zone {worst_idx}，"
            f"{rate[worst_idx]:+.1%}"
        )
        borderline = verdict_a["passed"] if label == "A" else verdict_b["passed"]
        if borderline and abs(rate[worst_idx]) > 0.10:
            lines.append(
                f"  ⚠️ 距離 Δ 通過門檻，但 ambient 變化率達 {rate[worst_idx]:+.1%}，"
                "屬於「目前還沒事，但邊際很小」的訊號，值得留意。"
            )

    any_failed = not verdict_a["passed"] or not verdict_b["passed"]
    lines += ["", "## Fallback 建議"]
    lines.append(FALLBACK_RECOMMENDATION if any_failed else "兩顆感測器皆 PASS，不需要 fallback。")

    return "\n".join(lines)


def _demo_synthetic_run(rng, n_t=500, n_ta=60):
    """產生一組合成的 solo/dual 資料跑一次完整流程，供 `__main__` 示範用。
    合成資料只驗證流程與圖表正確，數字不是真實結論（見 `format_report`
    的 `is_synthetic` 警示）。"""
    base = 500.0 + rng.normal(0, 1.0, size=N_ZONES)
    bias = np.zeros(N_ZONES)
    bias[[3, 9]] = [1.2, 0.8]  # 兩個 zone 有溫和的合成偏移，其餘接近 0

    dist_solo = base[None, :] + rng.normal(0, 0.5, size=(n_t, N_ZONES))
    dist_dual = base[None, :] + bias[None, :] + rng.normal(0, 0.5, size=(n_t, N_ZONES))
    valid = np.ones((n_t, N_ZONES), dtype=bool)

    ambient_base = 20.0 + rng.normal(0, 1.0, size=N_ZONES)
    ambient_bias = np.zeros(N_ZONES)
    ambient_bias[9] = 6.0  # zone 9 的 ambient 上升比例明顯，示範「邊際訊號」
    ambient_solo = ambient_base[None, :] + rng.normal(0, 0.5, size=(n_ta, N_ZONES))
    ambient_dual = (ambient_base + ambient_bias)[None, :] + rng.normal(0, 0.5, size=(n_ta, N_ZONES))

    delta_dist = zone_distance_delta(dist_solo, valid, dist_dual, valid)
    verdict = crosstalk_verdict(delta_dist)
    _, rate = zone_ambient_delta(ambient_solo, ambient_dual)
    return delta_dist, verdict, rate


if __name__ == "__main__":
    from pathlib import Path

    rng = np.random.default_rng(0)
    delta_a, verdict_a, rate_a = _demo_synthetic_run(rng)
    delta_b, verdict_b, rate_b = _demo_synthetic_run(rng)

    report = format_report(verdict_a, verdict_b, rate_a, rate_b, is_synthetic=True)
    print(report)

    out_path = Path(__file__).with_name("d10_crosstalk_report.md")
    out_path.write_text(report + "\n")
    print(f"\n報告已寫入 {out_path}")
