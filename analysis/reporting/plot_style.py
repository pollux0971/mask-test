"""D20 — 圖表樣式統一：一套 rcParams、一組色盤、一個存檔函式。

D 軌有十個 story 會產圖（`D10` crosstalk、`D12` 箱型圖、`D14` 熱力圖、
`D17` t-SNE、`D18` null 分布…）。各自用 matplotlib 預設樣式的話，拼進論文
就是十種字級、十種色盤。統一一次比逐張調快得多。

## 字型：英文優先，但 CJK 一定要能畫

專案規則是**圖表文字一律英文**（`text_checks.assert_english_only()` 是守門員），
而 story 的驗收條件是「中文正確顯示無方框」。兩者不衝突，它們管的是不同的事：

* **英文規則**管的是**我們寫什麼**——標題、軸標籤、圖例一律英文。
* **CJK 字型**管的是**萬一出現時畫不畫得出來**——`config/vocab.json` 的詞
  是中文，探索性繪圖或臨時 debug 圖難免會把它們畫上去。此時沒有 CJK 字型
  就是一排「□□□」，**而且 matplotlib 只會噴一行 warning，圖照存**。

所以字型堆疊是「英文字型 → CJK fallback」：正常情況下英文由前面的字型
渲染，真的出現中文時後面的接手。這是防禦縱深，不是放寬英文規則。

## 色階：灰階存活是硬需求，而發散色階本質上做不到

論文會被印出來，投影也常常偏色。實測 WCAG 相對亮度（32 個取樣點）：

    viridis  單調遞增 ✅  0.019 → 0.783
    cividis  單調遞增 ✅  0.017 → 0.790
    RdBu_r   非單調   ❌  兩端亮度都是 0.030

**`RdBu_r` 的兩個極端在灰階下亮度一模一樣**——「+2 mm」與「−2 mm」印成
黑白後長得完全一樣。story 的實作建議自己踩了這個坑。

而這不是換一張色表能解的：**發散色階的正負號本來就靠色相承載**，任何
對稱的發散配色在灰階下都有同樣的問題。所以本模組的規則是：

> 用發散色階時**必須**在格子上標數值（或加符號網底），
> 由 `assert_grayscale_safe()` 擋住沒標的圖。

`D14` 的熱力圖本來就每格標數值，剛好合規。

## 分類色盤：Okabe-Ito，但**色相不夠**

`CATEGORICAL_PALETTE` 用 Okabe-Ito（色盲友善的事實標準）。但它是為**色相
可辨**設計的，不是為亮度可辨——實測相鄰兩色的最小亮度差只有 **0.011**，
灰階下會糊在一起。

所以 `apply_style()` 同時設定 `linestyle` 與 `marker` 的 cycler：**折線圖
在灰階下靠線型與標記區分，不是靠顏色。** 只設顏色的話，色盲友善只解決了
螢幕上的問題，印出來還是分不出來。
"""
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------- 字型

# 英文優先，CJK 在後面接手。`DejaVu Sans` 是 matplotlib 內建、一定存在的
# 保底；CJK 的幾個名字涵蓋 Linux / macOS / Windows 三種環境。
FONT_STACK = [
    "DejaVu Sans",
    "Noto Sans CJK TC", "Noto Sans CJK SC",
    "Source Han Sans TC", "PingFang TC", "Microsoft JhengHei",
    "WenQuanYi Zen Hei", "Droid Sans Fallback",
]

# --------------------------------------------------------------------- 色階

# 順序性資料**優先 cividis**。兩者的亮度都單調遞增（灰階同樣安全），差別在
# 色相：`cividis` 是**專為紅綠色盲設計**的（藍→黃，兩個對二型色覺者都可辨），
# `viridis` 的綠色段落對紅綠色盲的辨識度較低。論文與 Demo 投影都一定會有人
# 看不清紅綠，所以預設選對他們較友善的那個。
#
# `viridis` 保留為別名：`analysis/experiments/` 底下其他 agent 的圖仍然寫死
# 用它（我不能改那些檔案）。兩者灰階同樣安全，所以混用不會壞掉，只是配色
# 不一致——要全部統一，那些檔案得由它們的擁有者改成引用 `SEQUENTIAL_CMAP`。
SEQUENTIAL_CMAP = "cividis"
SEQUENTIAL_ALT_CMAP = "viridis"
DIVERGING_CMAP = "RdBu_r"        # ⚠️ 灰階下正負不可辨，必須標數值

# Okabe-Ito 色盲友善分類色盤（去掉純黑，留給文字與座標軸）。
CATEGORICAL_PALETTE = [
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#56B4E9",  # sky blue
    "#D55E00",  # vermillion
    "#F0E442",  # yellow
    "#000000",  # black（最後才用）
]

# 灰階下真正在區分曲線的東西。
LINE_STYLES = ["-", "--", "-.", ":", (0, (3, 1, 1, 1)), (0, (5, 2)), (0, (1, 1)), "-"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*"]

# --------------------------------------------------------------------- 尺寸

DPI = 300                        # story 驗收條件
VECTOR_FORMATS = ("pdf",)        # 論文用向量圖；SVG 由 `save_figure` 選配

BASE_FONT_SIZE = 9               # 單欄論文圖縮到 ~85 mm 仍讀得到
FIGURE_SIZE = (5.2, 3.4)         # 英吋；約單欄寬

# 相鄰分類色的最小亮度差，低於它就不能只靠顏色區分（見模組說明）。
MIN_LUMINANCE_GAP = 0.05


def rc_params():
    """本專案的 rcParams。**回一份新的 dict**，呼叫端可以再覆蓋個別項目
    而不會汙染下一次呼叫。"""
    from cycler import cycler

    return {
        "font.family": "sans-serif",
        "font.sans-serif": list(FONT_STACK),
        # 負號用 ASCII 的 `-`：CJK 字型的 U+2212 常常缺字，變成方框，
        # 而那個方框出現在座標軸上特別難察覺（軸標籤沒人會細看）。
        "axes.unicode_minus": False,

        "figure.figsize": FIGURE_SIZE,
        "figure.dpi": 100,               # 螢幕預覽；存檔另外指定 300
        "savefig.dpi": DPI,
        "savefig.bbox": "tight",
        "savefig.transparent": False,    # 投影片背景多半不是白的，留白底比較安全

        "font.size": BASE_FONT_SIZE,
        "axes.titlesize": BASE_FONT_SIZE + 1,
        "axes.labelsize": BASE_FONT_SIZE,
        "xtick.labelsize": BASE_FONT_SIZE - 1,
        "ytick.labelsize": BASE_FONT_SIZE - 1,
        "legend.fontsize": BASE_FONT_SIZE - 1,

        "axes.grid": True,
        "grid.alpha": 0.3,
        "grid.linewidth": 0.6,
        "axes.axisbelow": True,          # 網格畫在資料下面，不然會蓋住點
        "axes.spines.top": False,
        "axes.spines.right": False,

        "image.cmap": SEQUENTIAL_CMAP,
        # 顏色 **和** 線型 **和** 標記一起循環：灰階下靠後兩者區分。
        "axes.prop_cycle": (cycler(color=CATEGORICAL_PALETTE)
                            + cycler(linestyle=LINE_STYLES)
                            + cycler(marker=MARKERS)),
        "lines.linewidth": 1.6,
        "lines.markersize": 4,

        "legend.frameon": False,
        "figure.autolayout": False,      # 用 tight_layout/bbox_inches 手動控制
    }


def apply_style():
    """把樣式套到全域 rcParams。`analysis/run_all.py` 在產圖前呼叫一次。"""
    import matplotlib.pyplot as plt

    plt.rcParams.update(rc_params())


class styled:
    """`with styled():` —— 只在區塊內套用，離開就還原。

    測試與 notebook 用這個比 `apply_style()` 安全：全域改 rcParams 會外溢到
    同一個 process 裡的其他測試，而 matplotlib 的狀態是 process 全域的。
    """

    def __enter__(self):
        import matplotlib as mpl

        self._ctx = mpl.rc_context(rc_params())
        self._ctx.__enter__()
        return self

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


# ----------------------------------------------------------------- 灰階檢查


def relative_luminance(color):
    """WCAG 2.x 的相對亮度（0=黑、1=白）。`color` 可以是任何 matplotlib 色。"""
    import matplotlib.colors as mcolors

    rgb = np.asarray(mcolors.to_rgb(color), dtype=np.float64)
    linear = np.where(rgb <= 0.03928, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return float(0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2])


def colormap_luminance(name, n=32):
    """一張色表沿途的亮度序列。"""
    from matplotlib import colormaps

    cmap = colormaps[name]
    return [relative_luminance(cmap(x)) for x in np.linspace(0.0, 1.0, n)]


def is_luminance_monotonic(name, n=32, tolerance=1e-3):
    """色表的亮度是不是單調的——**這就是「灰階下還讀得出來」的定義**。

    非單調代表兩個不同的值會印成同一個灰階，而且不會有任何警告。
    """
    values = colormap_luminance(name, n)
    deltas = np.diff(values)
    return bool((deltas >= -tolerance).all() or (deltas <= tolerance).all())


def palette_luminance_gaps(palette=None):
    """分類色盤排序後相鄰兩色的亮度差。最小值太小就不能只靠顏色區分。"""
    palette = palette or CATEGORICAL_PALETTE
    values = sorted(relative_luminance(c) for c in palette)
    return [b - a for a, b in zip(values, values[1:])]


def assert_grayscale_safe(fig, *, diverging_opt_out=None):
    """圖在黑白列印下仍可辨讀（story 驗收條件）。

    兩條規則：

    1. **單調亮度的色表**（`cividis`/`viridis` 之類）直接過。
    2. **發散色表**（`RdBu_r` 之類）本質上做不到——正負號靠色相承載，
       灰階下兩端亮度相同。此時**必須有文字標註**（每格數值或等高線標籤），
       否則擋下來。

    這不是吹毛求疵：`D10` 的 crosstalk 熱力圖畫的正是 Δ 距離，
    「+2 mm」與「−2 mm」印成黑白後長得一模一樣，而讀者不會知道。

    ## opt-out：要能關掉，但要吵

    純示意圖之類真的不需要標數值時，傳 `diverging_opt_out="<理由>"`。

    **參數刻意收字串而不是布林值。** `diverging_opt_out=True` 可以無腦傳，
    半年後沒有人知道當初為什麼關掉；一個必填的理由會逼寫的人講清楚，而且
    那句話會留在原始碼裡。空字串一樣會被拒絕——**「關掉檢查」本身必須是
    一個有記錄的決定**，不是一個預設值。
    """
    if diverging_opt_out is not None:
        if not isinstance(diverging_opt_out, str) or not diverging_opt_out.strip():
            raise ValueError(
                "diverging_opt_out 必須是一句說明理由的字串，不能是 True/空字串"
                "——關掉檢查必須是一個有記錄的決定"
            )
        return

    offenders = []
    for ax in fig.axes:
        for image in ax.get_images():
            name = image.get_cmap().name
            if is_luminance_monotonic(name):
                continue
            if not ax.texts:
                offenders.append(
                    f"{ax.get_title() or '(no title)'}: 用了非單調亮度的色表 "
                    f"{name!r} 卻沒有數值標註——灰階下正負不可辨"
                )
    assert not offenders, "圖在黑白列印下不可辨讀：" + "; ".join(offenders)


# ------------------------------------------------------------------- 存檔


def save_figure(fig, path, *, formats=("png",) + VECTOR_FORMATS, dpi=DPI,
                check_english=True, check_grayscale=True, diverging_opt_out=None):
    """存成 PNG(300dpi) + PDF（story 驗收條件），回傳實際寫出的路徑。

    `path` 給不帶副檔名的路徑；每個 format 各存一份。

    **兩個檢查預設是開的**：英文 only 與灰階可辨。理由：這兩件事在存檔的
    當下最容易修，等到論文排版時才發現一張圖有中文標籤或印出來看不懂，
    要回頭重跑整個實驗。要關掉必須明確傳 `check_*=False`——**預設關掉的
    檢查等於沒有檢查**。
    """
    from analysis.reporting.text_checks import assert_english_only

    if check_english:
        assert_english_only(fig)
    if check_grayscale:
        assert_grayscale_safe(fig, diverging_opt_out=diverging_opt_out)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in formats:
        target = path.with_suffix(f".{fmt}")
        # 向量格式忽略 dpi，但傳了也無害；PNG 一定要 300。
        fig.savefig(target, format=fmt, dpi=dpi, bbox_inches="tight")
        written.append(target)
    return written


# ----------------------------------------------------------------- 字型檢查


def missing_glyphs(text, font_stack=None):
    """`text` 裡有哪些字元在整個字型堆疊裡都找不到（會畫成方框）。

    matplotlib 遇到缺字**只會噴一行 warning 然後照樣存檔**，所以「圖上有沒有
    方框」不能靠肉眼在 CI 裡檢查——這裡直接查字型的 cmap 表。
    """
    from matplotlib import font_manager

    stack = font_manager.FontProperties(
        family=list(font_stack or FONT_STACK)).get_family()

    charsets = []
    for family in stack:
        try:
            path = font_manager.findfont(
                font_manager.FontProperties(family=family), fallback_to_default=False)
        except Exception:                       # noqa: BLE001 — 沒裝就跳過
            continue
        try:
            from matplotlib import ft2font

            charsets.append(set(ft2font.FT2Font(path).get_charmap().keys()))
        except Exception:                       # noqa: BLE001 — 讀不到就跳過
            continue

    if not charsets:
        return list(dict.fromkeys(text))        # 一個字型都載不到 → 全部算缺

    missing = []
    for ch in dict.fromkeys(text):
        code = ord(ch)
        if not any(code in charset for charset in charsets):
            missing.append(ch)
    return missing


def cjk_font_available():
    """環境裡有沒有能畫中文的字型。**沒有時要誠實回報，不要假裝圖是好的。**"""
    return not missing_glyphs("五八一四不要")
