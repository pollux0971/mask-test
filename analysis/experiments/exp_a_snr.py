"""實驗 A：逐 zone SNR 分析。

規格見 `stories/D-analysis/D11.md`；資料蒐集流程見 `stories/E-hardware/E03.md`。

實驗設計（E03，同一次戴上依序錄製）：
    baseline   靜止 30 秒
    round      圓唇保持 3 秒 x 10 次
    spread     展唇保持 3 秒 x 10 次

SNR 定義：
    snr_zone = |Δ_round - Δ_spread| / σ_baseline

其中 Δ_round / Δ_spread 是各自相對同一個 baseline 的偏移量。兩者相減時
baseline 均值本來就會抵消，等價於直接比較 round 與 spread 兩組動作的
平均值差，所以這裡直接算 `|mean(round) - mean(spread)|`，數學上完全等價，
不需要額外算兩次「減 baseline」再相減。

距離與 signal rate 各算一份：這裡的函式只處理單一 16 維通道（距離或
signal rate 其中一種），呼叫端對同一顆感測器呼叫兩次，分別傳入
`tof[:, 0:16]`（距離）與 `tof[:, 16:32]`（signal rate）。
"""
import numpy as np

from analysis.reporting.plot_style import SEQUENTIAL_CMAP, styled

N_ZONES = 16
# `$T` 的所有通道都是整數量化值，均勻量化誤差的 σ 是 Δ/√12（Δ=1 個單位）——
# 這是「有意義的最小 σ」的理論下限，1e-3 只擋得住除以零，擋不住「小到沒有
# 意義」（見 CONTRACTS §3.2.2、`analysis/features/tof_features.py` 的
# SIGMA_FLOOR 註解）。這裡的 SNR 是直接餵給 D01 活躍 zone 篩選的指標，
# 剛性表面貼合的 zone 若守衛不足會算出虛高的 SNR、被錯誤選為活躍 zone。
SIGMA_FLOOR = 1.0 / 12 ** 0.5  # ≈ 0.28868，量化雜訊的理論下限，不是任意小數
DEFAULT_OVERALL_THRESHOLD = 3.0  # 與 E03「SNR < 3 時已嘗試至少 3 種戴法調整」一致

VERDICT_PASS = "pass_both"
VERDICT_ADJUST = "adjust_wear"
VERDICT_DIAGNOSE = "hardware_diagnose"

VERDICT_ACTIONS = {
    VERDICT_PASS: "雙通過，可進入下一階段",
    VERDICT_ADJUST: "單邊通過，調整戴法",
    VERDICT_DIAGNOSE: "雙失敗，硬體診斷",
}


def _check_zone_dim(name, arr):
    if arr.shape[-1] != N_ZONES:
        raise ValueError(f"{name} 最後一維應為 {N_ZONES}，收到 {arr.shape[-1]}")


def zone_snr(baseline, round_trials, spread_trials):
    """回傳 (16,) 逐 zone SNR，單一感測器、單一通道類型（距離或 signal rate）。

    baseline / round_trials / spread_trials: (T, 16)，T 各自可以不同長度。
    """
    baseline = np.asarray(baseline, dtype=np.float64)
    round_trials = np.asarray(round_trials, dtype=np.float64)
    spread_trials = np.asarray(spread_trials, dtype=np.float64)

    _check_zone_dim("baseline", baseline)
    _check_zone_dim("round_trials", round_trials)
    _check_zone_dim("spread_trials", spread_trials)

    sigma_baseline = np.maximum(baseline.std(axis=0), SIGMA_FLOOR)
    delta = round_trials.mean(axis=0) - spread_trials.mean(axis=0)
    return np.abs(delta) / sigma_baseline


def overall_snr(snr_zone_arr):
    """相容簡報的單一數字判定標準：逐 zone SNR 的平均。"""
    return float(np.mean(snr_zone_arr))


def symmetry(snr_a, snr_b):
    """左右對稱性：|SNR_L - SNR_R| / max(|SNR_L|, |SNR_R|)。

    可以吃逐 zone 陣列（回傳逐 zone 對稱性向量）或整體 SNR 純量
    （回傳單一數字）——兩種情境用同一個 elementwise 公式即可。
    """
    snr_a = np.asarray(snr_a, dtype=np.float64)
    snr_b = np.asarray(snr_b, dtype=np.float64)
    denom = np.maximum(np.maximum(np.abs(snr_a), np.abs(snr_b)), SIGMA_FLOOR)
    return np.abs(snr_a - snr_b) / denom


def zone_snr_grid(snr_zone_arr, n_rows=4, n_cols=4):
    """把 (16,) 逐 zone SNR 攤成 (n_rows, n_cols) 網格供熱力圖使用。

    zone 索引 -> (row, col) 採 row-major：zone i 對應 (i // n_cols, i % n_cols)。

    zone layout: row-major (ASSUMED, unverified — see A 軌/E01)。此排列
    尚未經硬體端確認，需要 E01 冒煙測試時核對 vl53l7cx 實際的 zone 掃描
    順序是否也是 row-major，否則熱力圖的方向會跟實體佈局對不上。
    """
    snr_zone_arr = np.asarray(snr_zone_arr)
    if snr_zone_arr.shape[-1] != n_rows * n_cols:
        raise ValueError(f"需要 {n_rows * n_cols} 個 zone，收到 {snr_zone_arr.shape[-1]}")
    return snr_zone_arr.reshape(n_rows, n_cols)


def three_way_verdict(overall_snr_a, overall_snr_b, threshold=DEFAULT_OVERALL_THRESHOLD):
    """三分法判定（照簡報）：雙通過 / 單邊通過(調整戴法) / 雙失敗(硬體診斷)。

    回傳 (verdict, action, detail)。
    """
    pass_a = overall_snr_a >= threshold
    pass_b = overall_snr_b >= threshold
    detail = {
        "snr_a": overall_snr_a, "snr_b": overall_snr_b,
        "pass_a": pass_a, "pass_b": pass_b, "threshold": threshold,
    }

    if pass_a and pass_b:
        verdict = VERDICT_PASS
    elif pass_a or pass_b:
        verdict = VERDICT_ADJUST
    else:
        verdict = VERDICT_DIAGNOSE

    return verdict, VERDICT_ACTIONS[verdict], detail


def plot_zone_snr_heatmaps(snr_distance, snr_signal, n_rows=4, n_cols=4, threshold=None):
    """畫距離與 signal rate 的逐 zone SNR 熱力圖，並排。

    回傳 matplotlib Figure；存檔或顯示交給呼叫端決定
    （`fig.savefig(...)` 或在 notebook 裡直接顯示）。
    """
    grid_d = zone_snr_grid(snr_distance, n_rows, n_cols)
    grid_s = zone_snr_grid(snr_signal, n_rows, n_cols)

    # D20：所有圖共用同一套樣式（字型/網格/色盤），`styled()` 只在區塊內生效，
    # 不會汙染呼叫端 process 裡其他測試的全域 rcParams。SNR 是非負量值（見
    # `zone_snr` 的 `np.abs`），單調亮度的 SEQUENTIAL_CMAP 就夠，不是發散資料。
    with styled():
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 2, figsize=(8, 4))
        for ax, grid, title in zip(axes, (grid_d, grid_s), ("Distance SNR", "Signal-rate SNR")):
            im = ax.imshow(grid, cmap=SEQUENTIAL_CMAP)
            ax.set_title(title)
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        fig.suptitle(
            "zone layout: row-major (ASSUMED, unverified - see A track/E01)"
            + (f"  |  threshold={threshold}" if threshold is not None else "")
        )
        fig.tight_layout()
    return fig


if __name__ == "__main__":
    from pathlib import Path

    from analysis.reporting.plot_style import save_figure

    rng = np.random.default_rng(0)
    # 合成兩組逐 zone SNR（距離／signal rate），只為了示範存檔走
    # save_figure()（D20：PNG 300dpi + PDF，並在存檔當下驗證英文 only／
    # 灰階可辨）——數字不是真實結論。
    snr_distance = np.abs(rng.normal(3.0, 1.5, size=N_ZONES))
    snr_signal = np.abs(rng.normal(3.0, 1.5, size=N_ZONES))

    fig = plot_zone_snr_heatmaps(snr_distance, snr_signal, threshold=DEFAULT_OVERALL_THRESHOLD)
    out_path = Path(__file__).with_name("a_snr_heatmap")
    written = save_figure(fig, out_path)
    print("圖已寫入：", ", ".join(str(p) for p in written))
