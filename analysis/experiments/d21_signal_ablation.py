"""D21 — signal 通道消融：`signal_per_spad` 值不值得那一半 `$T` 頻寬？

規格見 `stories/D-analysis/D21.md`。`A02` 讓 `$T` 每個 zone 多送一個
signal 值，行長直接加倍——`CONTRACTS.md` §1.4 顯示 `$T` 是最大的單一
頻寬消費者，其中一半是 signal。這支模組回答：如果 signal 對辨識沒有
貢獻，關掉它省下來的頻寬（換算成 §1.4 的使用率）划不划算。

`D19`（消融套件）的驗收條件是固定的五項，明確不含 signal 通道——
本模組是它自己的 story，理由跟 `D19` 完成報告裡寫的一樣。

## 「移除」與「換雜訊」是兩個不同的問題

`CONTRACTS.md` §3.3：每顆感測器的 32 維裡 `[0:16]` 是距離、`[16:32]` 是
signal——本模組用 `[0:16]`（distance A）、`[16:32]`（signal A）、
`[32:48]`（distance B）、`[48:64]`（signal B）四段固定切法。

- **移除**（降維，`remove_signal()`）→ 回答「不送 signal 這件事本身」
  對應的正是韌體真的關掉 signal 傳輸能省下的頻寬與資訊。
- **換成雜訊**（維度不變，`noise_signal()`）→ 隔離「是 signal 這個物理量
  本身有資訊」還是「多出來的維度本身就有幫助」（純粹因為維度變多，
  分類器隨機也能多找到一點可利用的變異）。兩者的差距才是 signal 這個
  *物理量* 本身的貢獻，不是「維度變多」這個混淆變因的貢獻。

**只做移除，不換雜訊，會把這兩件事混在一起**——如果移除後準確率下降，
你不知道是因為丟了 signal 的資訊，還是單純丟了 16 維。

## 只 import，不重寫

分類器與 permutation p 值直接 import `d18_permutation_test.make_estimator()`。
`_run_on_matrix()` 自己另外寫一份 CV+permutation 的 orchestration（不是
`run_permutation_test()` 本身，那個函式的介面是「餵一個 modality key，
內部用 `stack_modality()` 現算」，signal 消融後的欄位組合不是
`exp_c_silhouette.MODALITIES` 裡任何一個既有 key，塞不進那個介面）——
跟 `D19` 的 `time_reversal_ablation()` 因為同樣理由自己寫 CV 是同一個
先例，不是本模組首創的例外。

## ⚠️ 合成資料最容易踩天花板效應（`D19` 的坑，這裡特別危險）

如果合成資料裡 distance 通道已經足以完美分類，signal 的貢獻**永遠是 0**，
而那個 0 是資料設計的產物，不是「signal 真的沒用」的結論。本模組的測試
資料刻意讓 **signal 攜帶一部分 distance 沒有的資訊**（不同的 zone 子集
帶詞彙訊號）——這是**建構出來的前提**，用來確保消融框架真的量得到
signal 的貢獻，不代表真實資料裡 signal 一定有用。真實結論待 `E05`。
"""
import numpy as np

DEFAULT_N_PERMUTATIONS = 1000
DEFAULT_CV_FOLDS = 5
DEFAULT_RANDOM_STATE = 0
P_VALUE_THRESHOLD = 0.01

# CONTRACTS.md §3.3：每顆感測器 32 維 = [0:16] 距離 + [16:32] signal。
# ToF_B 在串接後整段偏移 32（tof_l=[0:32], tof_r=[32:64]，各自内部同樣式）。
DIST_A = slice(0, 16)
SIG_A = slice(16, 32)
DIST_B = slice(32, 48)
SIG_B = slice(48, 64)

TOTAL_BAUD_BYTES_PER_S = 46000  # CONTRACTS §1.4：460800 baud ≈ 46 KB/s
# 非 $T 的固定貢獻，取自 CONTRACTS §1.4 凍結表格（Mel 256 hop 那一列）。
# signal 拿不拿掉不影響這兩個數字。
MEL256_M_KBPS = 0.5
MEL256_F_KBPS = 15.6
# CONTRACTS §1.4 凍結表格的 $T x2（含 signal）既有數字，本模組直接沿用
# 當「有 signal」的基準，不重新估——`estimate_t_line_bytes()` 只用來算
# 「移除 signal 後行長變成幾倍」這個比例，再拿比例去縮放這兩個凍結值。
# 這樣做的理由：直接用假設的數值分布（均勻隨機 0-4000mm/0-200）從零估出
# 絕對 KB/s，量出來的「有 signal」數字會跟這兩個凍結值對不上（實測差了
# 30-40%，CONTRACTS 的 §1.3「行長上限」本來就講明是保守的上界估計，不是
# 平均行長）——用比例縮放可以避開「兩組數字互相打架」的問題，讓「移除後
# 省多少」的結論穩固地建立在已經凍結的預算表上。
T_X2_KBPS_WITH_SIGNAL = {
    "4x4@30Hz": 8.7,
    "8x8@10Hz": 16.0,
}


def _flatten(feature_seqs):
    """把一批 (T, D) 陣列攤平堆疊成 (N, T*D)——`exp_c_silhouette.stack_modality()`
    做的事，但那個函式假設輸入固定是 104 維並用 `MODALITIES` 查表切欄位；
    `remove_signal()` 的輸出維度已經變了（32 或 72 維，不是 104），套不上
    那個查表，所以這裡單獨處理攤平這一步（純 reshape，不是特徵計算）。"""
    rows = []
    t_ref = None
    for i, fs in enumerate(feature_seqs):
        arr = np.asarray(fs, dtype=np.float64)
        if t_ref is None:
            t_ref = arr.shape[0]
        elif arr.shape[0] != t_ref:
            raise ValueError(f"feature_seqs[{i}] 的 T={arr.shape[0]} 與前面的 T={t_ref} 不一致")
        rows.append(arr.reshape(-1))
    return np.stack(rows, axis=0)


def remove_signal(feature_seqs, base="tof_combined"):
    """移除 signal 兩段，回傳降維後的 (T, D') 陣列列表。

    base="tof_combined": 64 維 -> 32 維（只留兩顆感測器的距離）
    base="all":          104 維 -> 72 維（距離 32 維 + mel 40 維）
    """
    if base not in ("tof_combined", "all"):
        raise ValueError(f"base 應為 'tof_combined' 或 'all'，收到 '{base}'")

    out = []
    for fs in feature_seqs:
        arr = np.asarray(fs, dtype=np.float64)
        if arr.shape[-1] != 104:
            raise ValueError(f"輸入最後一維應為 104，收到 {arr.shape[-1]}")
        dist = np.concatenate([arr[:, DIST_A], arr[:, DIST_B]], axis=1)
        if base == "all":
            dist = np.concatenate([dist, arr[:, 64:104]], axis=1)
        out.append(dist)
    return out


def noise_signal(feature_seqs, random_state=DEFAULT_RANDOM_STATE):
    """把 signal 兩段換成同尺度隨機雜訊，維度不變（104 維）。

    雜訊標準差取自該段資料本身的標準差（跟 D19 `substitute_modality_with_noise()`
    同一種手法），這樣「雜訊尺度不合理」不會是這個測試失敗的原因。回傳的
    陣列仍是完整 104 維，之後可以直接用 `exp_c_silhouette.stack_modality()`
    切出 "tof_combined" 或 "all"。
    """
    rng = np.random.default_rng(random_state)
    out = []
    for fs in feature_seqs:
        arr = np.asarray(fs, dtype=np.float64).copy()
        if arr.shape[-1] != 104:
            raise ValueError(f"輸入最後一維應為 104，收到 {arr.shape[-1]}")
        for sl in (SIG_A, SIG_B):
            block = arr[:, sl]
            scale = float(np.std(block))
            if scale <= 0:
                scale = 1.0
            arr[:, sl] = rng.normal(0, scale, size=block.shape)
        out.append(arr)
    return out


def _run_on_matrix(X, y, n_permutations, cv, random_state, n_jobs):
    """跟 `d18_permutation_test.run_permutation_test()` 同樣的 CV+permutation
    邏輯（cv 依最小類別樣本數夾值、`StratifiedKFold(shuffle=True)`、
    `permutation_test_score`），但吃現成的 X 矩陣——見模組 docstring。
    分類器仍是 import 來的 `make_estimator()`，不重寫。
    """
    from sklearn.model_selection import StratifiedKFold, permutation_test_score

    from analysis.experiments.d18_permutation_test import make_estimator

    y = np.asarray(y)
    if y.shape[0] != X.shape[0]:
        raise ValueError(f"labels 長度 {y.shape[0]} 與樣本數 {X.shape[0]} 不一致")
    min_class_count = int(np.min(np.unique(y, return_counts=True)[1]))
    if min_class_count < 2:
        raise ValueError(f"每個類別至少需要 2 筆樣本才能做 CV，最小類別只有 {min_class_count} 筆")
    cv = max(2, min(cv, min_class_count))

    cv_splitter = StratifiedKFold(n_splits=cv, shuffle=True, random_state=random_state)
    estimator = make_estimator()

    score, _perm_scores, pvalue = permutation_test_score(
        estimator, X, y, cv=cv_splitter, n_permutations=n_permutations,
        n_jobs=n_jobs, random_state=random_state,
    )
    return {
        "score": float(score),
        "pvalue": float(pvalue),
        "n_permutations": n_permutations,
        "cv": cv,
        "random_state": random_state,
        "passed": bool(pvalue < P_VALUE_THRESHOLD),
    }


def signal_ablation(feature_seqs, labels, base="tof_combined",
                     n_permutations=DEFAULT_N_PERMUTATIONS, cv=DEFAULT_CV_FOLDS,
                     random_state=DEFAULT_RANDOM_STATE, n_jobs=-1):
    """「有 signal」vs「移除 signal」vs「signal 換雜訊」三組準確率與 p 值（驗收條件）。

    base: "tof_combined"（ToF-only）或 "all"（融合）——分開跑，因為 signal
    對兩者的貢獻可能不同（驗收條件：分別報告）。

    回傳 dict: {"base", "with_signal", "signal_removed", "signal_noised",
                "removed_gain", "noised_gain"}
    `removed_gain`/`noised_gain` 是 with_signal 分數減去對應組的分數，
    正值代表「有 signal 比較好」。
    """
    from analysis.experiments.exp_c_silhouette import stack_modality

    kwargs = dict(n_permutations=n_permutations, cv=cv, random_state=random_state, n_jobs=n_jobs)

    X_with = stack_modality(feature_seqs, base)
    X_removed = _flatten(remove_signal(feature_seqs, base))
    X_noised = stack_modality(noise_signal(feature_seqs, random_state), base)

    r_with = _run_on_matrix(X_with, labels, **kwargs)
    r_removed = _run_on_matrix(X_removed, labels, **kwargs)
    r_noised = _run_on_matrix(X_noised, labels, **kwargs)

    return {
        "base": base,
        "with_signal": r_with,
        "signal_removed": r_removed,
        "signal_noised": r_noised,
        "removed_gain": r_with["score"] - r_removed["score"],
        "noised_gain": r_with["score"] - r_noised["score"],
    }


def estimate_t_line_bytes(dim, with_signal, seq=105, t_us=1737863421123456, seed=0):
    """用實際字串格式化量測一行 `$T` 的位元組數（不是手算）。示例值的量級
    取自 `CONTRACTS.md` §1.1 的真實範例：距離 0-4000 mm、signal 實務值多
    落在 0-200。"""
    rng = np.random.default_rng(seed)
    dist = rng.integers(0, 4001, size=dim)
    sig = rng.integers(0, 201, size=dim)

    parts = [f"$T,A,{seq},{t_us},{dim}"]
    parts += [str(int(d)) for d in dist]
    if with_signal:
        parts += [str(int(s)) for s in sig]
    line = ",".join(parts) + "\n"
    return len(line.encode("utf-8"))


def bandwidth_conversion():
    """換算成 §1.4 的頻寬使用率變化，4×4 與 8×8 各一組（驗收條件）。

    「有 signal」的 `$T`×2 直接沿用 `T_X2_KBPS_WITH_SIGNAL`（§1.4 凍結值）；
    「無 signal」用 `estimate_t_line_bytes()` 量出的**行長比例**去縮放那個
    凍結值，不是重新從零估一次絕對值——理由見模組頂部常數旁的說明。
    `$M`/`$F` 的貢獻同樣取 §1.4 凍結表格「Mel 256 hop」那一列，不受
    signal 影響，不重算。

    回傳 dict: {"4x4@30Hz": {...}, "8x8@10Hz": {...}}，每組含
    with/without signal 的 `$T` KB/s、總 KB/s、使用率，以及用來縮放的
    `line_byte_ratio_without_over_with`。
    """
    configs = [
        {"name": "4x4@30Hz", "dim": 16},
        {"name": "8x8@10Hz", "dim": 64},
    ]
    results = {}
    for cfg in configs:
        bytes_with = estimate_t_line_bytes(cfg["dim"], with_signal=True)
        bytes_without = estimate_t_line_bytes(cfg["dim"], with_signal=False)
        ratio = bytes_without / bytes_with

        t_kbps_with = T_X2_KBPS_WITH_SIGNAL[cfg["name"]]
        t_kbps_without = t_kbps_with * ratio

        total_with = t_kbps_with + MEL256_M_KBPS + MEL256_F_KBPS
        total_without = t_kbps_without + MEL256_M_KBPS + MEL256_F_KBPS

        results[cfg["name"]] = {
            "line_byte_ratio_without_over_with": ratio,
            "t_kbps_with_signal": t_kbps_with,
            "t_kbps_without_signal": t_kbps_without,
            "total_kbps_with_signal": total_with,
            "total_kbps_without_signal": total_without,
            "usage_with_signal": total_with * 1000 / TOTAL_BAUD_BYTES_PER_S,
            "usage_without_signal": total_without * 1000 / TOTAL_BAUD_BYTES_PER_S,
        }
    return results


def recommend(result_tof_only, result_all, bw):
    """建議 + 代價（格式與 `D10` 的 fallback 建議一致：明確判定 + 具體代價，
    不是只有「建議/不建議」）。

    判定邏輯：只要 ToF-only 或 All 任一組的 `signal_removed`／`signal_noised`
    p 值顯著（< 0.01）**且**移除後分數明顯下降，就建議保留 signal；
    兩組都不顯著或降幅很小，才建議關閉换頻寬。「明顯下降」用跟 D19 一致
    的 5 個百分點門檻。
    """
    GAIN_THRESHOLD = 0.05

    def is_significant_drop(result):
        return result["removed_gain"] > GAIN_THRESHOLD or result["noised_gain"] > GAIN_THRESHOLD

    tof_matters = is_significant_drop(result_tof_only)
    all_matters = is_significant_drop(result_all)
    keep_signal = tof_matters or all_matters

    bandwidth_saved = {
        name: cfg["usage_with_signal"] - cfg["usage_without_signal"]
        for name, cfg in bw.items()
    }

    return {
        "keep_signal": keep_signal,
        "tof_only_matters": tof_matters,
        "all_matters": all_matters,
        "bandwidth_saved_pct_points": bandwidth_saved,
    }


def plot_signal_ablation(result, ax=None):
    """三組（有 signal / 移除 / 換雜訊）的準確率長條圖，圖表文字英文。

    回傳 matplotlib Figure。
    """
    import matplotlib.pyplot as plt

    if ax is None:
        fig, ax = plt.subplots()
    else:
        fig = ax.figure

    labels = ["with signal", "signal removed", "signal -> noise"]
    scores = [result["with_signal"]["score"], result["signal_removed"]["score"],
              result["signal_noised"]["score"]]
    pvalues = [result["with_signal"]["pvalue"], result["signal_removed"]["pvalue"],
               result["signal_noised"]["pvalue"]]
    colors = ["steelblue", "indianred", "goldenrod"]

    bars = ax.bar(labels, scores, color=colors)
    for bar, p in zip(bars, pvalues):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"p={p:.3f}", ha="center", va="bottom", fontsize=8)

    ax.set_ylabel("CV accuracy")
    ax.set_ylim(0, 1.05)
    ax.set_title(f"signal channel ablation ({result['base']})")
    fig.tight_layout()
    return fig


def format_report(result_tof_only, result_all, bw, rec, is_synthetic=True):
    """把整份分析轉成 Markdown 字串，格式仿照 `D10` 的報告結構。"""
    lines = ["# D21 — signal 通道消融：它值得那一半頻寬嗎？", ""]
    if is_synthetic:
        lines.append(
            "> ⚠️ **本報告使用合成資料，數字不是真實結論。** 合成資料刻意讓 "
            "signal 攜帶一部分 distance 沒有的資訊，這是建構出來的前提，"
            "真實資料未必成立。真實結論待 `E05`。"
        )
        lines.append("")

    lines.append("## 三組準確率與 p 值")
    lines.append("")
    lines.append("| 分模態 | 組合 | CV 準確率 | p 值 | 判定 |")
    lines.append("|---|---|---|---|---|")
    for label, result in (("ToF-only", result_tof_only), ("All（融合）", result_all)):
        for key, name in (("with_signal", "有 signal"),
                           ("signal_removed", "移除 signal"),
                           ("signal_noised", "signal 換雜訊")):
            r = result[key]
            status = f"p < {P_VALUE_THRESHOLD}（顯著）" if r["passed"] else "未達顯著"
            lines.append(f"| {label} | {name} | {r['score']:.3f} | {r['pvalue']:.4f} | {status} |")
    lines.append("")

    lines.append("## signal 的貢獻（有 signal 減去對應組）")
    lines.append("")
    for label, result in (("ToF-only", result_tof_only), ("All（融合）", result_all)):
        lines.append(
            f"- **{label}**：移除後掉 {result['removed_gain']:+.3f}，"
            f"換雜訊後掉 {result['noised_gain']:+.3f}"
        )
    lines.append("")

    lines.append("## 頻寬換算（§1.4）")
    lines.append("")
    lines.append("| 組態 | 有 signal 使用率 | 無 signal 使用率 | 省下 |")
    lines.append("|---|---|---|---|")
    for name, cfg in bw.items():
        saved = cfg["usage_with_signal"] - cfg["usage_without_signal"]
        lines.append(
            f"| {name} | {cfg['usage_with_signal']:.1%} | "
            f"{cfg['usage_without_signal']:.1%} | {saved:.1%} |"
        )
    lines.append("")

    lines.append("## 建議與代價")
    lines.append("")
    if rec["keep_signal"]:
        lines.append(
            "**建議：保留 signal 傳輸。** "
            f"ToF-only {'有' if rec['tof_only_matters'] else '沒有'}顯著貢獻，"
            f"All（融合）{'有' if rec['all_matters'] else '沒有'}顯著貢獻。"
            "代價：維持目前的頻寬使用率（見上表「有 signal」欄），"
            "8×8 模式下錄音 dump 期間仍有超載風險，需要繼續依 §1.4 的既有做法處理。"
        )
    else:
        pts = "、".join(f"{name} {v:.1%}" for name, v in rec["bandwidth_saved_pct_points"].items())
        lines.append(
            "**建議：可以考慮關閉 signal 傳輸。** "
            f"ToF-only 與 All 兩組都沒有顯著貢獻（p 值未達顯著，或降幅 < 5 個百分點）。"
            f"省下的頻寬（{pts}）能讓 8×8 模式的餘裕更寬，降低錄音 dump 期間 FIFO overrun 的風險。"
            "**代價**：這個結論建立在合成資料上（見上方假資料警示），關閉需要新的協定組態旗標"
            "（本 story 範圍不含改協定），且真實資料若走向不同結論，之前錄的 session 會缺這欄資料，"
            "回頭補不了——建議先用 `E05` 真實資料重跑本模組確認，再決定要不要真的關閉。"
        )
    lines.append("")

    return "\n".join(lines)
