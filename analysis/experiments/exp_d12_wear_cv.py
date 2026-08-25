"""D12 — 實驗 B：同次戴 vs 跨次戴的變異係數（CV）。

規格見 `stories/D-analysis/D12.md`。用 manifest 的 `wear_id` 分組
（`host/storage/manifest.read_manifest()`，唯讀 import，不改；
`session_path` 是相對於某個 `root`，`n_frames` 是 ToF 幀數，見 CONTRACTS §2.2）。

**這個實驗回答：同一個人、同一個詞，脫下來再戴回去，特徵還像不像？**
如果跨次戴的變異遠大於同次戴，enrollment 的樣板換一次戴法就可能失效。

包含兩種互補的分析：
1. story 原本的純量 CV 公式（std/mean），分模態：`tof_L`/`tof_R` 距離、
   signal rate、Mel 總能量（`scalar_cv_within_between` + `extract_scalar_features`）。
2. 距離比值法：用 D04 `cosine_dist` / D05 `dtw_dist` 算整段特徵序列的
   組內（同一次戴）vs 組間（不同次戴）兩兩距離，比純量 CV 保留更多資訊，
   也是箱型圖的資料來源（`distance_based_wear_ratio`）。

---

## 合成測資的已知陷阱（延續 `fusion.py` 記錄的那個坑，這裡是第一個受益者）

如果把「跨次戴」的變異合成成**整體平移**（同一個常數加到所有維度），
用 cosine 距離量測時會完全看不到這個變異——cosine 只看方向、不看長度，
於是會得出「跨次戴沒問題」這個**完全錯誤**的結論。

戴法改變在物理上比較像**各 zone 不等量的偏移＋旋轉**，不是均勻平移：
戴歪一點，有些 zone 的距離讀值會變近、有些變遠，變化量也不一樣，
而不是所有 zone 一起加減同一個數字。本模組的測試刻意用這種
「不等量、多方向」的合成擾動，見 `test_distance_based_wear_ratio_*`。

## 另一個尚未確認的假設

`ToF_L`/`ToF_R`（story 用詞）對應到 schema 的 `tof_A`/`tof_B` 中哪一個，
目前**假設 L=A、R=B**，未經硬體端確認（跟 D11 的 zone layout 是同一類
問題）。`extract_scalar_features()` 的回傳鍵名跟著 story 用 L/R，
但輸入參數仍用 schema 原本的 A/B 命名，避免混淆兩件事。
"""
import numpy as np

CV_PASS_THRESHOLD = 0.30
RATIO_IMPROVEMENT_THRESHOLD = 1.5
CV_EPS = 1e-9

IMPROVEMENT_SUGGESTIONS = [
    "骨架與臉部的接觸點增加（三點支撐 → 更穩定）",
    "加定位標記（讓每次戴的位置一致）",
    "SOP 加入「戴上後先看 C09 的對稱性指標，調到綠燈才開始」",
]


def _safe_cv(x):
    """std/mean，mean 接近 0 時回傳 NaN 而不是除以 0 爆炸。"""
    x = np.asarray(x, dtype=np.float64)
    mean = x.mean()
    if abs(mean) < CV_EPS:
        return np.nan
    return float(x.std() / mean)


def scalar_cv_within_between(df, wear_col, value_col):
    """依 D12.md 的公式分別算 within／between CV。

    within：每個 `wear_id` 各自的 CV（`std/mean`），取平均當代表值。
    between：各 `wear_id` 平均值之間的 CV。

    df: 至少含 `wear_col`、`value_col` 兩欄的 DataFrame。
    回傳 dict：cv_within、cv_between、cv_within_per_wear（逐 wear 明細）、ratio。
    """
    within_per_wear = df.groupby(wear_col)[value_col].agg(_safe_cv)
    cv_within = float(within_per_wear.mean())

    wear_means = df.groupby(wear_col)[value_col].mean()
    cv_between = _safe_cv(wear_means.to_numpy())

    ratio = cv_between / cv_within if cv_within > CV_EPS else np.nan
    return {
        "cv_within": cv_within,
        "cv_between": cv_between,
        "cv_within_per_wear": within_per_wear,
        "ratio": ratio,
    }


def extract_scalar_features(tof_a, valid_a, tof_b, valid_b, mel):
    """從一筆 trial 的原始資料算出 4 個純量特徵（分模態）。

    tof_a/tof_b: (T, 32)，[0:16]=距離mm，[16:32]=signal_per_spad/100
    valid_a/valid_b: (T, 16) bool，對應 CONTRACTS §2 的 tof_valid_A/B
    mel: (M, 40) log-mel

    回傳 dict：tof_L_distance、tof_R_distance、signal_rate、mel_total_energy。
    """
    def _masked_mean(x, valid):
        return float(np.nanmean(np.where(valid, x, np.nan)))

    tof_a = np.asarray(tof_a, dtype=np.float64)
    tof_b = np.asarray(tof_b, dtype=np.float64)
    valid_a = np.asarray(valid_a, dtype=bool)
    valid_b = np.asarray(valid_b, dtype=bool)

    dist_a, sig_a = tof_a[:, :16], tof_a[:, 16:]
    dist_b, sig_b = tof_b[:, :16], tof_b[:, 16:]

    return {
        "tof_L_distance": _masked_mean(dist_a, valid_a),
        "tof_R_distance": _masked_mean(dist_b, valid_b),
        "signal_rate": float(np.nanmean([
            _masked_mean(sig_a, valid_a), _masked_mean(sig_b, valid_b),
        ])),
        "mel_total_energy": float(np.asarray(mel, dtype=np.float64).sum()),
    }


def distance_based_wear_ratio(trials_by_wear, dist_fn):
    """組內（同一次戴）vs 組間（不同次戴，同一個 label）的兩兩距離比值。

    trials_by_wear: dict {wear_id: [完整特徵序列...]}，全部同一個 label。
    dist_fn: (a, b) -> float（例如 D04 `cosine_dist` 或 D05 `dtw_dist`）。
    """
    wear_ids = list(trials_by_wear.keys())
    if len(wear_ids) < 2:
        raise ValueError("至少需要 2 個 wear_id 才能算組間距離")

    within_dists = []
    for wid in wear_ids:
        trials = trials_by_wear[wid]
        if len(trials) < 2:
            continue
        for i in range(len(trials)):
            for j in range(i + 1, len(trials)):
                within_dists.append(dist_fn(trials[i], trials[j]))
    if not within_dists:
        raise ValueError("每個 wear_id 至少需要 2 筆 trial 才能算組內距離")

    between_dists = []
    for a in range(len(wear_ids)):
        for b in range(a + 1, len(wear_ids)):
            for x in trials_by_wear[wear_ids[a]]:
                for y in trials_by_wear[wear_ids[b]]:
                    between_dists.append(dist_fn(x, y))

    within_mean = float(np.mean(within_dists))
    between_mean = float(np.mean(between_dists))
    return {
        "within_mean": within_mean,
        "between_mean": between_mean,
        "ratio": between_mean / max(within_mean, CV_EPS),
        "within_distances": np.array(within_dists),
        "between_distances": np.array(between_dists),
    }


def plot_within_between_boxplot(within_distances, between_distances):
    """驗收條件：箱型圖清楚呈現兩組分布（圖表文字一律英文）。"""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 4))
    ax.boxplot([within_distances, between_distances], tick_labels=["within-wear", "between-wear"])
    ax.set_title("Within-wear vs between-wear distance")
    ax.set_ylabel("distance")
    fig.tight_layout()
    return fig


def wear_verdict(cv_within, cv_between, cv_threshold=CV_PASS_THRESHOLD,
                  ratio_threshold=RATIO_IMPROVEMENT_THRESHOLD):
    """PASS/FAIL 判定 + 是否需要列出改進建議，記錄實際用掉的門檻值。

    PASS 的判準是 `cv_between < cv_threshold`：between 代表跨次戴的變異，
    是「戴法重複性是不是瓶頸」這個問題真正要看的數字；within 主要是
    感測器/雜訊底線，理論上一直都該很小，拿來算 ratio 當分母。
    """
    ratio = cv_between / cv_within if cv_within > CV_EPS else float("inf")
    return {
        "passed": cv_between < cv_threshold,
        "cv_within": cv_within,
        "cv_between": cv_between,
        "cv_threshold": cv_threshold,
        "ratio": ratio,
        "ratio_threshold": ratio_threshold,
        "needs_improvement": ratio > ratio_threshold,
    }


def format_report(verdicts_by_feature, distance_result, is_synthetic):
    """輸出 D12 的完整報告。

    verdicts_by_feature: dict {feature_name: wear_verdict() 的回傳 dict}
    distance_result: `distance_based_wear_ratio()` 的回傳 dict
    """
    lines = ["# D12 — 實驗 B：同次戴 vs 跨次戴 CV 分析報告"]

    if is_synthetic:
        lines += [
            "",
            "> ⚠️ **本報告使用合成資料，數字不是真實結論。**"
            "真實結論待 `E04`（20 次戴脫資料蒐集）完成後，用真實錄音重跑本模組。",
        ]

    lines += ["", "## 分模態 CV（within / between）"]
    any_needs_improvement = False
    for feature, v in verdicts_by_feature.items():
        status = "PASS" if v["passed"] else "FAIL"
        lines.append(
            f"- `{feature}`：**{status}**（within={v['cv_within']:.1%}，"
            f"between={v['cv_between']:.1%}，門檻 {v['cv_threshold']:.0%}，"
            f"between/within 比值={v['ratio']:.2f}）"
        )
        any_needs_improvement = any_needs_improvement or v["needs_improvement"]

    lines += [
        "",
        "## 距離比值法（組內 vs 組間，D04/D05 距離函式）",
        f"- within-wear 平均距離：{distance_result['within_mean']:.4f}",
        f"- between-wear 平均距離：{distance_result['between_mean']:.4f}",
        f"- 比值（between/within）：{distance_result['ratio']:.2f}",
    ]

    lines += ["", "## 改進建議"]
    if any_needs_improvement:
        lines.append(
            f"至少一個模態 `between/within > {RATIO_IMPROVEMENT_THRESHOLD}`，"
            "戴法重複性可能是瓶頸，建議："
        )
        lines.extend(f"- {s}" for s in IMPROVEMENT_SUGGESTIONS)
    else:
        lines.append("所有模態的 between/within 比值都在門檻內，戴法重複性暫不是主要問題。")

    return "\n".join(lines)


def _demo_synthetic_run(rng):
    """合成一組「同一個人、同一個詞、3 次戴脫」的資料跑一次完整流程，
    供 `__main__` 示範用。跨次戴的變異刻意做成各 zone 不等量的偏移，
    不是均勻平移（見模組 docstring 的陷阱說明）。數字不是真實結論。"""
    n_wears, n_trials_per_wear = 3, 5
    zone_base = 500.0 + rng.normal(0, 2.0, size=16)

    scalar_rows = []
    trials_by_wear = {}
    for wear_id in range(n_wears):
        # 每次戴的偏移：每個 zone 各自獨立、不等量——不是同一個常數。
        wear_offset = rng.normal(0, 3.0, size=16)
        trials_by_wear[wear_id] = []
        for _ in range(n_trials_per_wear):
            tof_a = np.zeros((20, 32))
            tof_b = np.zeros((20, 32))
            tof_a[:, :16] = zone_base + wear_offset + rng.normal(0, 0.5, size=16)
            tof_b[:, :16] = zone_base + wear_offset + rng.normal(0, 0.5, size=16)
            tof_a[:, 16:] = 50.0 + rng.normal(0, 1.0, size=16)
            tof_b[:, 16:] = 50.0 + rng.normal(0, 1.0, size=16)
            valid = np.ones((20, 16), dtype=bool)
            mel = 2.0 + rng.normal(0, 0.05, size=(10, 40))

            feats = extract_scalar_features(tof_a, valid, tof_b, valid, mel)
            scalar_rows.append({"wear_id": wear_id, **feats})
            trials_by_wear[wear_id].append(np.concatenate([tof_a, tof_b], axis=1))

    import pandas as pd

    from analysis.similarity.cosine_baseline import cosine_dist

    df = pd.DataFrame(scalar_rows)
    verdicts = {}
    for feat in ("tof_L_distance", "tof_R_distance", "signal_rate", "mel_total_energy"):
        cv = scalar_cv_within_between(df, "wear_id", feat)
        verdicts[feat] = wear_verdict(cv["cv_within"], cv["cv_between"])

    distance_result = distance_based_wear_ratio(trials_by_wear, cosine_dist)
    return verdicts, distance_result


if __name__ == "__main__":
    from pathlib import Path

    rng = np.random.default_rng(0)
    verdicts, distance_result = _demo_synthetic_run(rng)
    report = format_report(verdicts, distance_result, is_synthetic=True)
    print(report)

    out_path = Path(__file__).with_name("d12_wear_cv_report.md")
    out_path.write_text(report + "\n")
    print(f"\n報告已寫入 {out_path}")
