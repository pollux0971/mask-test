"""實驗 E：Viseme 敏感度熱力圖（3 模態 × 6 個 viseme 類別）。

規格見 `stories/D-analysis/D14.md`。回答的問題：

    哪些音靠 ToF、哪些音靠音訊？

這張圖直接支持「多模態互補」的論證：**如果每個音素都是同一個模態最強，
那就不需要多模態。** 反過來說，只要有任何一個 viseme 在 ToF 上明顯勝過
Mel、而另一個相反，多模態就有存在的理由。

輸入是 `D03`（`analysis/features/feature_assembly.py`）組裝好的
`FeatureSeq.data`（T=24、104 維，§3.3：`[0:32]` tof_A、`[32:64]` tof_B、
`[64:104]` mel）。模態切片直接 import `D13` 的 `MODALITIES`——**兩支腳本
必須指向同一組通道**，各自寫一份遲早會漂掉，而漂掉之後兩張圖的結論就
不能互相對照了。

## 敏感度的定義

    sensitivity(sample, modality) = max |z| over (time × channels of that modality)

特徵在 §3.2 就已經是 per-zone z-score（除以 `baseline_sigma`），所以這個
數字的單位是「偏離自己的靜止基線幾個標準差」，跨模態可比。取 `max` 而不是
`mean`：一個 viseme 的辨識力來自**動得最多的那個通道**，全部平均會被大量
沒在動的通道稀釋成噪音。

每格回報 `n` 與**標準誤**（`std / sqrt(n)`，`ddof=1`）。`n == 1` 時標準誤是
`None` 而不是 `0`——單一樣本沒有離散度的資訊，填 0 會讓那一格看起來像
「量得非常準」。

## viseme 類別來自 `config/vocab.json`，不寫死

⚠️ **story 的預期表與實際詞彙集對不起來**，這是刻意處理過的：

| 來源 | 涵蓋的 viseme |
|---|---|
| story 的預期表 | A 雙唇、B 圓唇、C 展唇、D 開口、**E 舌音**、F 擦音 |
| `config/vocab.json`（§6） | A、B、C、D、F、**G 應用** |

也就是說 **E 舌音在目前的詞彙集裡一個詞都沒有**（永遠不會有樣本），而
**G 應用有三個詞（好／停／不要）卻不在預期表裡**。

本模組以 `vocab.json` 為準（它是契約 §6，也是實際會錄到的東西），所以
熱力圖的 6 列是 A/B/C/D/F/G。story 的預期表仍然完整保留在
`EXPECTED_PATTERN` 裡（含 E），比對時：

* E 舌音 → 有預期、無樣本 → 報告標 `no_samples`，並提醒它不在詞彙集裡
* G 應用 → 有樣本、無預期 → 報告標 `no_expectation`，不硬套一個猜的預期

**不捏造任何一邊。** 見完成回報的「CONTRACTS.md 的疑問」。

## 兩個一定要讀的判讀提醒

**ToF 在所有音素上都均勻地弱 → 回頭質疑實驗 A，不要質疑音素。**
均勻的弱通常代表訊號根本沒進來（戴法、距離、對焦），而不是「這些音素本來
就難」。`format_report()` 會在偵測到這個型態時印出提示。

**舌音（E）三個模態都弱是預期中的。** 那是系統的能力極限，誠實報告它比
隱藏它更有價值——只是以目前的詞彙集根本量不到。

「開發完成不等於有真實結果」：本模組用假資料開發並測過就是完整交付，
真實結論要等 `E05` 蒐集資料。`format_report()` 會標 `is_synthetic`。
"""
import json
import math
from pathlib import Path

import numpy as np
from scipy.special import ndtri

from analysis.experiments.exp_c_silhouette import MODALITIES as _ALL_MODALITIES
from analysis.reporting.plot_style import SEQUENTIAL_CMAP

# 本實驗只比較三個**互斥**的模態。`tof_combined` / `all` 是 D13 的組合式
# 比較，混進來會讓「哪個模態最強」的每一列出現重複計算。
MODALITY_ORDER = ("tof_l", "tof_r", "mel")
MODALITIES = {name: _ALL_MODALITIES[name] for name in MODALITY_ORDER}

# 報告用的英文標籤（圖表一律英文）。
MODALITY_LABELS = {"tof_l": "ToF_L", "tof_r": "ToF_R", "mel": "Mel"}

DEFAULT_VOCAB_PATH = Path(__file__).resolve().parents[2] / "config" / "vocab.json"
DEFAULT_DPI = 300

# story 的預期型態表。**含 E 舌音**（目前的詞彙集沒有它，保留是為了讓
# 「為什麼量不到」有出處）。強度等級由弱到強：weak < medium < medium_strong
# < strong。
EXPECTED_PATTERN = {
    "A": {"tof_l": "strong", "tof_r": "strong", "mel": "medium_strong"},
    "B": {"tof_l": "medium_strong", "tof_r": "strong", "mel": "medium"},
    "C": {"tof_l": "medium", "tof_r": "medium_strong", "mel": "medium"},
    "D": {"tof_l": "medium_strong", "tof_r": "medium_strong", "mel": "medium_strong"},
    "E": {"tof_l": "weak", "tof_r": "weak", "mel": "medium"},
    "F": {"tof_l": "weak", "tof_r": "weak", "mel": "strong"},
}

STRENGTH_ORDER = ("weak", "medium", "medium_strong", "strong")

# 強度等級的門檻，套用在**超出機率地板的量**（`excess`）上，不是原始的
# `max|z|`。理由見 `chance_max()`：768 個純雜訊值的最大值本來就有 3.4，
# 直接對原始值設 3.0 的門檻，會把「什麼都沒發生」判成 medium。
#
# ⚠️ **這四個門檻是暫定值，不是契約。** 沒有任何實測資料可以校準它們——
# `E05` 之後應該回頭用真實分布重訂（例如取全體樣本的四分位）。目前的取法
# 是：3 對齊 `B15`/`B16` 的 VAD 進入閾值（「明顯偏離基線」的既有定義），
# 其餘等比往上。報告會把原始數值與 excess 一起印出來，不必只看等級。
STRENGTH_THRESHOLDS = (
    (9.0, "strong"),
    (6.0, "medium_strong"),
    (3.0, "medium"),
    (float("-inf"), "weak"),
)

# 「ToF 均勻地弱」的判定：所有 viseme 的兩個 ToF 模態都落在 weak。
UNIFORM_WEAK_LEVEL = "weak"

# 高於這個值的 `max|z|` 在物理上講不通，多半代表**上游的 σ 下限出了問題**
# 而不是嘴巴動得特別大（§3.2.1／3.2.2）。
#
# 一個貼著剛性表面的 zone，量化後 `baseline_sigma` 可以低到 0.026 mm；
# 用 `1e-3` 當下限等於沒有下限，除下去該 zone 的 z 會衝到幾十甚至上萬，
# 而 `max` 只取最大的那一個——**一個壞掉的 zone 就足以決定整格的顏色**，
# 而且整張圖看起來只是「這個 viseme 特別敏感」。
#
# 30 的由來：本模組的 `strong` 門檻是 excess ≥ 9（約 raw 12.4）。30 已經是
# 它的兩倍多，真實唇動不該達到——`B16` 在合成資料上量到的最大值約 23。
IMPLAUSIBLE_MAX_ABS_Z = 30.0


def strength_for(value):
    """把一個敏感度數值對應到四級強度字串。"""
    if value is None or not math.isfinite(value):
        return None
    for threshold, label in STRENGTH_THRESHOLDS:
        if value >= threshold:
            return label
    raise AssertionError("unreachable: 最後一個門檻是 -inf")


def load_viseme_map(vocab_path=DEFAULT_VOCAB_PATH):
    """讀 `config/vocab.json`（§6），回傳 `(word_to_viseme, viseme_labels)`。

    `word_to_viseme` 是 `{word_id: "A"}`；`viseme_labels` 是
    `{"A": "A 雙唇"}`，依字母排序，供報告顯示原始標籤用。

    viseme 鍵取標籤的第一個字母——`vocab.json` 的格式是 `"A 雙唇"`，
    字母是穩定的分類鍵，後面的中文是給人看的。
    """
    data = json.loads(Path(vocab_path).read_text(encoding="utf-8"))
    word_to_viseme = {}
    labels = {}
    for word in data.get("words", []):
        raw = str(word.get("viseme", "")).strip()
        if not raw:
            continue
        key = raw.split()[0]
        word_to_viseme[word["id"]] = key
        labels.setdefault(key, raw)
    return word_to_viseme, dict(sorted(labels.items()))


def chance_max(n_values):
    """`n_values` 個獨立 N(0,1) 取 `max|z|` 的期望值（近似）。

    **這是本模組最容易被忽略、但會讓整張圖誤導的一件事。** 敏感度定義成
    「所有時間 × 所有通道的 `max|z|`」，而最大值本身有一個純機率造成的
    地板：ToF 一個模態是 24 幀 × 32 通道 = 768 個值，**即使完全沒有動作**，
    其中最大的那個 |z| 期望就有約 3.4。直接對原始值設 3.0 的門檻，
    「什麼都沒發生」會被判成 medium——一張全部中等強度、看起來很有內容的
    熱力圖，實際上量的是雜訊。

    近似式取 `|Z|` 的 `1 - 1/(2n)` 分位（超出次數期望為 0.5 的水準）：
    `ndtri(1 - 1/(4n))`。與模擬比對（3000 次抽樣）：

        n=100  模擬 2.738 / 近似 2.807
        n=768  模擬 3.359 / 近似 3.409
        n=960  模擬 3.418 / 近似 3.470
        n=5000 模擬 3.850 / 近似 3.891

    近似值一致地略高（約 +0.05），方向是保守的：地板估高一點，`excess`
    就估低一點，寧可低估敏感度也不要無中生有。

    通道數不同的模態地板也不同（ToF 768 → 3.41、Mel 960 → 3.47），所以
    **必須逐模態算**，不能共用一個常數，否則跨模態的比較會有系統性偏差。
    """
    n = int(n_values)
    if n < 2:
        return 0.0
    return float(ndtri(1.0 - 1.0 / (4.0 * n)))


def sensitivity(data, modality):
    """單筆樣本在某個模態上的敏感度 = 該模態通道的 `max |z|`（story 的定義）。

    `data` 是 `FeatureSeq.data`，`(T, 104)`。§3.2 已經 z-score 過，所以
    這個數字是「偏離靜止基線幾個 sigma」，跨模態可比。

    無效 zone 在特徵裡是 `NaN`（§2），用 `nanmax` 忽略；整個模態全 `NaN`
    （例如那顆感測器整段沒有效回波）回 `NaN` 而不是 0——**0 會被讀成
    「完全沒動」，那是一個關於嘴唇的結論，但實際發生的事是關於感測器的。**
    """
    raw, _ = sensitivity_with_excess(data, modality)
    return raw


def sensitivity_with_excess(data, modality):
    """回傳 `(raw_max_abs_z, excess_above_chance)`。

    `raw` 是 story 定義的敏感度，照實回報；`excess` 扣掉該模態通道數對應
    的機率地板（見 `chance_max()`），是強度分級真正該看的量。
    """
    values = np.asarray(data, dtype=np.float64)[:, MODALITIES[modality]]
    finite = np.isfinite(values)
    n_finite = int(finite.sum())
    if n_finite == 0:
        return float("nan"), float("nan")
    raw = float(np.nanmax(np.abs(values)))
    return raw, raw - chance_max(n_finite)


def _cell(pairs):
    """一格的統計量。`pairs` 是 `[(raw, excess), ...]`。

    `n == 1` 時標準誤是 `None`，見模組說明。強度分級看 `excess` 不看
    `mean`——原始值含機率地板。
    """
    finite = [(r, e) for r, e in pairs
              if r is not None and math.isfinite(r) and math.isfinite(e)]
    n = len(finite)
    if n == 0:
        return {"n": 0, "mean": None, "sem": None, "std": None,
                "excess_mean": None, "strength": None}
    raws = [r for r, _ in finite]
    excesses = [e for _, e in finite]
    mean = float(np.mean(raws))
    excess_mean = float(np.mean(excesses))
    if n == 1:
        return {"n": 1, "mean": mean, "sem": None, "std": None,
                "excess_mean": excess_mean, "strength": strength_for(excess_mean)}
    std = float(np.std(raws, ddof=1))
    return {
        "n": n, "mean": mean, "std": std, "sem": std / math.sqrt(n),
        "excess_mean": excess_mean, "strength": strength_for(excess_mean),
    }


def sensitivity_table(samples, word_to_viseme, viseme_labels):
    """`samples` 是 `[(word_id, FeatureSeq.data), ...]`。

    回傳 `{viseme_key: {modality: cell}}`，列的順序是 `viseme_labels` 的
    順序（依字母）。詞彙集裡有的 viseme 一定會出現在結果裡，即使這批樣本
    一筆都沒有——**空格子要看得見**，靜靜消失的話沒有人會發現那個類別
    從來沒被錄到。
    """
    buckets = {key: {m: [] for m in MODALITY_ORDER} for key in viseme_labels}
    unknown_words = set()
    for word_id, data in samples:
        key = word_to_viseme.get(word_id)
        if key is None:
            unknown_words.add(word_id)
            continue
        for modality in MODALITY_ORDER:
            buckets[key][modality].append(sensitivity_with_excess(data, modality))

    table = {
        key: {modality: _cell(pairs) for modality, pairs in row.items()}
        for key, row in buckets.items()
    }
    return table, sorted(unknown_words)


def compare_to_expected(table):
    """把觀測到的強度等級跟 story 的預期表逐格比對。

    回傳 `{viseme: {"status": ..., "cells": {modality: {...}}}}`。

    `status`：
    * `ok` —— 有樣本也有預期，逐格比對
    * `no_samples` —— 有預期但這批樣本沒有（例如 E 舌音不在詞彙集裡）
    * `no_expectation` —— 有樣本但 story 沒給預期（例如 G 應用）

    **不對 `no_expectation` 的格子硬套一個猜的預期。** 猜出來的預期一旦
    寫進報告，下一個人會把它當成簡報上的原始論述。
    """
    result = {}
    for key in sorted(set(table) | set(EXPECTED_PATTERN)):
        expected_row = EXPECTED_PATTERN.get(key)
        observed_row = table.get(key)

        if observed_row is None or all(c["n"] == 0 for c in observed_row.values()):
            result[key] = {"status": "no_samples", "cells": {},
                           "expected": expected_row}
            continue
        if expected_row is None:
            result[key] = {"status": "no_expectation", "cells": {}, "expected": None}
            continue

        cells = {}
        for modality in MODALITY_ORDER:
            observed = observed_row[modality]["strength"]
            expected = expected_row[modality]
            cells[modality] = {
                "observed": observed,
                "expected": expected,
                "match": observed == expected,
                "delta_levels": _level_delta(observed, expected),
            }
        result[key] = {"status": "ok", "cells": cells, "expected": expected_row}
    return result


def _level_delta(observed, expected):
    """觀測比預期高／低幾級。任一邊缺就回 `None`。"""
    if observed is None or expected is None:
        return None
    return STRENGTH_ORDER.index(observed) - STRENGTH_ORDER.index(expected)


def fricative_check(table, viseme="F"):
    """驗收條件：**擦音在 Mel 上明顯強於 ToF**。

    「明顯」的定義：Mel 的平均敏感度高於兩個 ToF 模態的**平均加一個標準誤**
    ——用標準誤而不是硬性倍數，樣本少的時候自動變保守。兩邊都只有一個樣本
    （標準誤是 `None`）時退回純比大小，並在 `basis` 標明。
    """
    row = table.get(viseme)
    if row is None or row["mel"]["n"] == 0:
        return {"pass": None, "reason": f"viseme {viseme} 沒有樣本", "basis": None}

    mel = row["mel"]
    tof_means = [row[m]["mean"] for m in ("tof_l", "tof_r") if row[m]["n"] > 0]
    if not tof_means:
        return {"pass": None, "reason": f"viseme {viseme} 沒有 ToF 樣本", "basis": None}

    tof_best = max(tof_means)
    sems = [row[m]["sem"] for m in ("tof_l", "tof_r", "mel")
            if row[m]["sem"] is not None]
    if sems:
        margin = max(sems)
        basis = "mean + max(sem)"
    else:
        margin = 0.0
        basis = "mean only (n=1, no dispersion available)"

    return {
        "pass": bool(mel["mean"] > tof_best + margin),
        "mel_mean": mel["mean"],
        "tof_best_mean": tof_best,
        "margin": margin,
        "basis": basis,
        "reason": None,
    }


def uniform_weak_tof(table):
    """所有有樣本的 viseme，兩個 ToF 模態是否都落在 `weak`。

    story：**均勻的弱通常代表訊號根本沒進來（戴法、距離、對焦），
    不是「這些音素本來就難」。** 回頭質疑實驗 A，不要質疑音素。
    """
    checked = 0
    for row in table.values():
        for modality in ("tof_l", "tof_r"):
            cell = row[modality]
            if cell["n"] == 0:
                continue
            checked += 1
            if cell["strength"] != UNIFORM_WEAK_LEVEL:
                return False
    return checked > 0


def implausible_cells(table):
    """回傳所有 `mean max|z|` 大到物理上講不通的格子。

    這是對**上游**的檢查，不是對這批樣本的檢查：見 `IMPLAUSIBLE_MAX_ABS_Z`。
    本模組吃的是 `D03` 組好的特徵，那些 z-score 是 `D01` 用
    `baseline_sigma` 除出來的——如果那裡的 σ 下限是 `1e-3` 而不是 §3.2.1
    的 `1/√12`，一個剛性表面的 zone 就會把整格撐爆，**而且這張圖看起來
    只會像「這個 viseme 特別敏感」。**
    """
    flagged = []
    for viseme, row in sorted(table.items()):
        for modality, cell in row.items():
            if cell["mean"] is not None and cell["mean"] > IMPLAUSIBLE_MAX_ABS_Z:
                flagged.append({"viseme": viseme, "modality": modality,
                                "mean": cell["mean"], "n": cell["n"]})
    return flagged


def viseme_sensitivity_report(samples, *, vocab_path=DEFAULT_VOCAB_PATH,
                              is_synthetic=True):
    """完整報告。`samples` 是 `[(word_id, FeatureSeq.data), ...]`。

    `is_synthetic` 預設 `True`——**假資料才是目前的常態**，預設 `False`
    會讓忘記傳的人產出一份看起來像真實結論的報告。
    """
    word_to_viseme, viseme_labels = load_viseme_map(vocab_path)
    table, unknown_words = sensitivity_table(samples, word_to_viseme, viseme_labels)
    return {
        "is_synthetic": bool(is_synthetic),
        "viseme_labels": viseme_labels,
        "viseme_order": list(viseme_labels),
        "modality_order": list(MODALITY_ORDER),
        "table": table,
        "expected_comparison": compare_to_expected(table),
        "fricative_check": fricative_check(table),
        "uniform_weak_tof": uniform_weak_tof(table),
        "unknown_words": unknown_words,
        "implausible_cells": implausible_cells(table),
        "n_samples": len(samples),
        "zone_layout_note": "zone layout: row-major (ASSUMED, unverified — see A track/E01)",
    }


def plot_viseme_sensitivity(report, dpi=DEFAULT_DPI):
    """3 模態 × N viseme 的熱力圖。回傳 matplotlib Figure；存檔交給呼叫端。

    每格標 `mean ± sem` 與 `n`（驗收條件）。沒有樣本的格子留白並標 `n=0`
    ——**不畫成 0**，0 是一個關於敏感度的陳述，空白才是「沒有資料」。
    """
    import matplotlib.pyplot as plt

    order = report["viseme_order"]
    modalities = report["modality_order"]
    table = report["table"]

    grid = np.full((len(order), len(modalities)), np.nan)
    for i, viseme in enumerate(order):
        for j, modality in enumerate(modalities):
            mean = table[viseme][modality]["mean"]
            if mean is not None:
                grid[i, j] = mean

    fig, ax = plt.subplots(figsize=(1.9 * len(modalities) + 2.4,
                                    0.85 * len(order) + 2.2), dpi=dpi)
    masked = np.ma.masked_invalid(grid)
    # 色表跟著 `D20` 的專案樣式走，**不寫死**——寫死的話這張圖就會是
    # 「十種來源、十種配色」裡的一種，那正是 D20 要消除的東西。
    cmap = plt.get_cmap(SEQUENTIAL_CMAP).copy()
    cmap = cmap.with_extremes(bad="0.9")
    image = ax.imshow(masked, cmap=cmap, aspect="auto")

    ax.set_xticks(range(len(modalities)))
    ax.set_xticklabels([MODALITY_LABELS[m] for m in modalities])
    ax.set_yticks(range(len(order)))
    # 只用 viseme 的字母鍵：`vocab.json` 的完整標籤含 CJK，圖表一律英文。
    ax.set_yticklabels([f"Viseme {key}" for key in order])
    ax.set_xlabel("modality")
    ax.set_ylabel("viseme class")

    title = "Viseme sensitivity (mean max|z|)"
    if report["is_synthetic"]:
        title += " — SYNTHETIC DATA, NOT A RESULT"
    ax.set_title(title)

    finite = masked.compressed()
    midpoint = (finite.min() + finite.max()) / 2 if finite.size else 0.0
    for i, viseme in enumerate(order):
        for j, modality in enumerate(modalities):
            cell = table[viseme][modality]
            if cell["n"] == 0:
                ax.text(j, i, "n=0", ha="center", va="center", fontsize=8, color="0.35")
                continue
            sem = "n/a" if cell["sem"] is None else f"{cell['sem']:.2f}"
            colour = "white" if cell["mean"] < midpoint else "black"
            ax.text(j, i, f"{cell['mean']:.2f}\n±{sem}\nn={cell['n']}",
                    ha="center", va="center", fontsize=8, color=colour)

    fig.colorbar(image, ax=ax, label="mean max|z| (sigma from rest baseline)")
    fig.tight_layout()
    return fig


def format_report(report):
    """把 `viseme_sensitivity_report()` 轉成人類可讀的 Markdown。"""
    lines = []
    if report["is_synthetic"]:
        lines += ["> ⚠️ **假資料（synthetic）產生的數字，不是真實結論。**"
                  " 真實結論待 `E05` 資料蒐集後重跑本模組取得。", ""]

    if report["uniform_weak_tof"]:
        lines += ["> 🔴 **ToF 在所有 viseme 上都均勻地弱。**"
                  " 均勻的弱通常代表訊號根本沒進來（戴法、距離、對焦），"
                  "**不是**「這些音素本來就難」——請先回頭質疑實驗 A（`D10`/`exp_a_snr`）"
                  "與配戴幾何，再討論音素。", ""]

    if report["implausible_cells"]:
        worst = max(report["implausible_cells"], key=lambda c: c["mean"])
        lines += [f"> 🔴 **有 {len(report['implausible_cells'])} 格的 mean max|z| 大到"
                  f"物理上講不通**（最大 {worst['mean']:.1f}，"
                  f"Viseme {worst['viseme']} / {MODALITY_LABELS[worst['modality']]}）。"
                  "真實唇動不該達到這個量級——**先查上游的 σ 下限**"
                  "（§3.2.1：一個貼著剛性表面的 zone 量化後 `baseline_sigma` 可以低到"
                  " 0.026 mm，用 `1e-3` 當下限等於沒有下限，那個 zone 會單獨撐爆整格），"
                  "再討論這個 viseme 是不是真的比較敏感。", ""]

    if report["unknown_words"]:
        lines += [f"> ⚠️ 有 {len(report['unknown_words'])} 個詞不在 `config/vocab.json` 裡，"
                  f"已從統計中排除：{', '.join(report['unknown_words'])}", ""]

    lines += ["## 敏感度表（mean max|z| ± SEM，n）", "",
              "單位是「偏離靜止基線幾個 sigma」（§3.2 的 per-zone z-score）。",
              "括號裡的 `Δ` 是**扣掉機率地板之後**的量（見 `chance_max()`）——"
              "強度等級看的是它，不是原始值。純雜訊的原始 max|z| 本來就有 3.4。", ""]
    header = "| Viseme | " + " | ".join(MODALITY_LABELS[m] for m in report["modality_order"]) + " |"
    lines += [header, "|---|" + "---|" * len(report["modality_order"])]
    for key in report["viseme_order"]:
        cells = []
        for modality in report["modality_order"]:
            cell = report["table"][key][modality]
            if cell["n"] == 0:
                cells.append("— (n=0)")
            elif cell["sem"] is None:
                cells.append(f"{cell['mean']:.2f} (Δ{cell['excess_mean']:+.2f}, "
                             f"{cell['strength']}, n=1)")
            else:
                cells.append(f"{cell['mean']:.2f} ± {cell['sem']:.2f} "
                             f"(Δ{cell['excess_mean']:+.2f}, {cell['strength']}, "
                             f"n={cell['n']})")
        lines.append(f"| {report['viseme_labels'][key]} | " + " | ".join(cells) + " |")
    lines.append("")

    lines += _expected_section(report)
    lines += _fricative_section(report)
    lines += ["> " + report["zone_layout_note"], ""]
    return "\n".join(lines).rstrip() + "\n"


def _expected_section(report):
    lines = ["## 與預期型態的落差", ""]
    comparison = report["expected_comparison"]

    mismatches = []
    for key in sorted(comparison):
        entry = comparison[key]
        if entry["status"] == "no_samples":
            note = "這批樣本沒有這個 viseme"
            if key not in report["viseme_labels"]:
                note += "——**它不在 `config/vocab.json` 裡，目前的詞彙集根本錄不到**"
            lines.append(f"* **Viseme {key}**：{note}。")
            continue
        if entry["status"] == "no_expectation":
            lines.append(
                f"* **Viseme {report['viseme_labels'].get(key, key)}**："
                "有樣本但 story 的預期表沒有這一類，**不硬套一個猜的預期**。"
            )
            continue
        for modality, cell in entry["cells"].items():
            if not cell["match"]:
                mismatches.append((key, modality, cell))

    if mismatches:
        lines.append("")
        lines.append("| Viseme | 模態 | 預期 | 觀測 | 差距（級） |")
        lines.append("|---|---|---|---|---|")
        for key, modality, cell in mismatches:
            delta = cell["delta_levels"]
            arrow = "" if delta is None else (f"+{delta}" if delta > 0 else str(delta))
            lines.append(f"| {key} | {MODALITY_LABELS[modality]} | {cell['expected']} "
                         f"| {cell['observed']} | {arrow} |")
    else:
        lines.append("")
        lines.append("有樣本且有預期的格子**全部符合預期型態**。")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 唇動先行量的讀取端（`reports/VAD_FUSION_OPTIONS.md` 問題 4 的事後檢驗）
#
# 不在 `D14.md` 原始範圍內（那份 story 只講 viseme 敏感度熱力圖）——放在
# 這個檔案是 esp-mask-test-ad 的路由決定：`CONTRACTS.md` 的變更記錄把
# `measure_lip_lead()`（`host/vad/onset.py`，B16）算出來的唇動領先量稱為
# 「`D14` 唯一要量的東西」，`comparable`/`lip_onset_us_A`/`_B` 這幾個
# schema 欄位也是為了讓這裡能做「`union_min` 融合策略選得對不對」的事後
# 檢驗而存在（見 `reports/SCHEMA_SUPPLY_DEMAND.md`「一個欄位的三道關卡」）。
#
# **一律走 `session_loader.load_session()`，不要直接開 h5py 讀 attrs**——
# h5py 原生讀 bool attr 會是 `numpy.bool_`，`numpy.bool_(True) is True`
# 是 `False`，`comparable` 的篩選條件會靜默失效（`CONTRACTS.md` §2 的
# 變更紀錄親自踩過這個坑）。`session_loader._as_scalar()` 已經把它正規化
# 成 Python `bool`，這裡拿到的 `Trial.attrs` 是安全的。
# ---------------------------------------------------------------------------

LIP_LEAD_VERSIONS = ("fused", "single_a", "single_b")
_LIP_ONSET_ATTR = {
    "fused": "lip_onset_us",
    "single_a": "lip_onset_us_A",
    "single_b": "lip_onset_us_B",
}


def lip_lead_samples(trials):
    """從一批 `Trial`（例如 `SessionData.trials`，跨多個 session 可以自己
    先串起來再傳進來）收集「有意義的唇動先行量」樣本，依融合前後三個
    版本分開回傳。

    篩選規則（每一條都是 `CONTRACTS.md` §2 明訂的語意，不是這裡自己
    發明的）：

    - **`comparable` 必須明確是 `True`**（`is True`，不是 truthy）——
      `None`（`measure_lip_lead()` 沒算過，例如根本沒偵測到唇動或語音）
      跟 `False`（算過了，但兩邊的結果不能拿去相減）都要排除。這正是
      `comparable` 這個欄位存在的理由：不可比的兩個時間戳硬相減，算出來
      的數字看起來完全正常，不會有任何錯誤訊息。
    - **`voice_onset_us` 缺席就整筆跳過**（三個版本都跳過）——`silent`
      模式下這個欄位必然缺席，此時「唇動比發聲早多久」這個問題本身沒有
      意義，不是「早了 0 ms」。
    - **`single_b` 允許自己缺席，不影響 `fused`/`single_a`**：
      `lip_onset_us_B` 缺席是 `union_min` 融合策略設計本身假設的正常
      狀況（那顆感測器那次沒偵測到），不代表資料損毀，三個版本各自獨立
      判斷自己的必要欄位是否存在。

    回傳 `{version: [lead_us, ...]}`（`lead_us = voice_onset_us -
    lip_onset_us`，符號跟 `host/vad/onset.py` 的 `LipLead.lead_us` 一致：
    正值代表唇動比發聲早）。

    ⚠️ **簡化說明**：`comparable` 是 `measure_lip_lead()` 針對**融合後**
    （`fused`）版本判定的可比性；這裡把同一個閘門也套用在 `single_a`/
    `single_b` 上，是這個讀取端的簡化，不是分別判斷每顆感測器自己的
    可比性——schema 裡沒有逐感測器的 `comparable_A`/`comparable_B`。
    """
    out = {version: [] for version in LIP_LEAD_VERSIONS}
    for trial in trials:
        attrs = trial.attrs
        if attrs.get("comparable") is not True:
            continue
        voice_onset = attrs.get("voice_onset_us")
        if voice_onset is None:
            continue
        for version in LIP_LEAD_VERSIONS:
            lip_onset = attrs.get(_LIP_ONSET_ATTR[version])
            if lip_onset is None:
                continue
            out[version].append(float(voice_onset) - float(lip_onset))
    return out


def compare_lip_lead_versions(trials):
    """三個版本（融合後 `lip_onset_us`、單顆 A、單顆 B）的先行量分布統計
    ——`reports/VAD_FUSION_OPTIONS.md` 問題 4 提出的事後檢驗方法。

    ⚠️ **這是事後檢驗方法本身的煙霧測試，不是真實結論。** `E05` 之前
    沒有任何真實資料會流進這裡；這個函式現階段只能證明「篩選邏輯正確、
    程式跑得動」，**不能**拿它在合成資料上的輸出去判斷 `union_min`
    當初選得對不對——那個問題只有真實資料能回答。
    """
    samples = lip_lead_samples(trials)
    result = {}
    for version in LIP_LEAD_VERSIONS:
        values = samples[version]
        n = len(values)
        result[version] = {
            "n": n,
            "median_ms": float(np.median(values)) / 1000.0 if n else None,
            "mean_ms": float(np.mean(values)) / 1000.0 if n else None,
            # `E07.md`/`E06.md` 引用的預期先行量區間，見 story 原文；
            # 這裡只回報落在區間內的比例，不下任何「這代表策略選對了」
            # 的結論。
            "frac_within_50_150ms": (
                sum(1 for v in values if 50_000.0 <= v <= 150_000.0) / n
                if n else None
            ),
        }
    return result


def format_lip_lead_report(result, is_synthetic=True):
    """把 `compare_lip_lead_versions()` 的結果轉成 Markdown 片段。"""
    lines = ["## 唇動先行量：融合前後三個版本的事後檢驗（煙霧測試）", ""]
    if is_synthetic:
        lines += [
            "> ⚠️ **本節使用合成資料，只驗證讀取端程式邏輯正確，"
            "不是真實結論。** 真實資料到手後才能回答「`union_min` "
            "當初選得對不對」，見 `reports/VAD_FUSION_OPTIONS.md` 問題 4。",
            "",
        ]
    lines += ["| 版本 | n | median (ms) | mean (ms) | 落在 50–150ms 的比例 |",
              "|---|---|---|---|---|"]
    labels = {"fused": "融合後 (lip_onset_us)", "single_a": "單顆 A (lip_onset_us_A)",
              "single_b": "單顆 B (lip_onset_us_B)"}
    for version in LIP_LEAD_VERSIONS:
        cell = result[version]
        if cell["n"] == 0:
            lines.append(f"| {labels[version]} | 0 | — | — | — |")
            continue
        lines.append(
            f"| {labels[version]} | {cell['n']} | {cell['median_ms']:.1f} | "
            f"{cell['mean_ms']:.1f} | {cell['frac_within_50_150ms']:.0%} |"
        )
    lines.append("")
    return "\n".join(lines)


def _fricative_section(report):
    check = report["fricative_check"]
    lines = ["## 擦音檢查（驗收條件）", ""]
    if check["pass"] is None:
        lines += [f"無法判定：{check['reason']}。", ""]
        return lines
    verdict = "✅ 通過" if check["pass"] else "❌ 未通過"
    lines += [
        f"{verdict}：擦音（F）在 Mel 上的敏感度 **{check['mel_mean']:.2f}**，"
        f"兩個 ToF 模態中較強者 **{check['tof_best_mean']:.2f}**"
        f"（判準：{check['basis']}，margin {check['margin']:.2f}）。",
        "",
        "> 這一格是「五／四」設計的核心（§6）：「四」ToF 弱、音訊強。"
        "**這格若不成立，多模態融合的論證就少了最直接的一半證據。**",
        "",
    ]
    return lines
