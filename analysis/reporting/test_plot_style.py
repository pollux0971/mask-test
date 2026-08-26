"""`analysis/reporting/plot_style.py`（D20）的測試。

驗收條件都用**可重跑的量測**驗，不是「看起來一致」：
* 「同一 style」→ 比對 rcParams
* 「中文無方框」→ 查字型 cmap 表，不是看圖
* 「PNG(300dpi) + PDF」→ 查檔案的 dpi 中繼資料與 PDF magic bytes
* 「黑白列印可辨」→ 算 WCAG 相對亮度
"""
import struct

import matplotlib
import numpy as np
import pytest

from analysis.reporting.plot_style import (
    CATEGORICAL_PALETTE,
    DIVERGING_CMAP,
    DPI,
    FONT_STACK,
    LINE_STYLES,
    MARKERS,
    MIN_LUMINANCE_GAP,
    SEQUENTIAL_ALT_CMAP,
    SEQUENTIAL_CMAP,
    assert_grayscale_safe,
    cjk_font_available,
    colormap_luminance,
    is_luminance_monotonic,
    missing_glyphs,
    palette_luminance_gaps,
    rc_params,
    relative_luminance,
    save_figure,
    styled,
)


def line_figure():
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    for i in range(4):
        ax.plot([0, 1, 2], np.array([0, 1, 2]) + i, label=f"series {i}")
    ax.set_title("Line figure")
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.legend()
    return fig


def heatmap_figure(cmap=SEQUENTIAL_CMAP, annotate=False):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    grid = np.arange(9).reshape(3, 3) - 4.0
    ax.imshow(grid, cmap=cmap)
    ax.set_title("Heatmap")
    if annotate:
        for i in range(3):
            for j in range(3):
                ax.text(j, i, f"{grid[i, j]:.1f}", ha="center", va="center")
    return fig


# ------------------------------------------------------------ 同一套樣式


def test_rc_params_returns_a_fresh_dict_each_time():
    """呼叫端覆蓋個別項目不該汙染下一次呼叫。"""
    first = rc_params()
    first["font.size"] = 99
    assert rc_params()["font.size"] != 99


def test_styled_context_restores_previous_rcparams():
    """全域改 rcParams 會外溢到同 process 的其他測試——matplotlib 的狀態是
    process 全域的。"""
    import matplotlib.pyplot as plt

    before = plt.rcParams["font.size"]
    with styled():
        assert plt.rcParams["font.size"] == rc_params()["font.size"]
    assert plt.rcParams["font.size"] == before


def test_style_sets_the_documented_values():
    with styled():
        assert matplotlib.rcParams["savefig.dpi"] == DPI
        assert matplotlib.rcParams["image.cmap"] == SEQUENTIAL_CMAP
        assert matplotlib.rcParams["axes.unicode_minus"] is False
        assert matplotlib.rcParams["font.sans-serif"][0] == FONT_STACK[0]
        assert matplotlib.rcParams["axes.axisbelow"] is True


def test_prop_cycle_varies_linestyle_and_marker_not_just_colour():
    """Okabe-Ito 是為**色相**可辨設計的，不是亮度——灰階下靠線型與標記。"""
    with styled():
        cycle = matplotlib.rcParams["axes.prop_cycle"]
        keys = set(cycle.keys)
    assert keys == {"color", "linestyle", "marker"}
    assert len(CATEGORICAL_PALETTE) == len(LINE_STYLES) == len(MARKERS)


def test_two_figures_share_the_same_style():
    """驗收條件：所有圖表套用同一 style。"""
    with styled():
        a, b = line_figure(), heatmap_figure()
        for fig in (a, b):
            ax = fig.axes[0]
            assert ax.xaxis.get_gridlines()[0].get_alpha() == pytest.approx(0.3)
            assert ax.spines["top"].get_visible() is False


# ------------------------------------------------------------ 中文無方框


def test_font_stack_puts_an_english_font_first():
    """英文規則管「我們寫什麼」，CJK 字型管「萬一出現時畫不畫得出來」。"""
    assert FONT_STACK[0] == "DejaVu Sans"
    assert any("CJK" in name or "Han" in name for name in FONT_STACK[1:])


def test_missing_glyphs_detects_a_genuinely_absent_character():
    """先證明這個檢查真的會抓——否則下面那條「中文沒缺字」可能只是永遠回空。"""
    assert missing_glyphs("") == [""]        # 私用區
    assert missing_glyphs("\U000204a2") == ["\U000204a2"]  # 罕見擴充 B
    assert missing_glyphs("ABC 123") == []


def test_default_font_alone_cannot_render_chinese():
    """這條證明**字型堆疊本身**是必要的，不是可有可無的裝飾。"""
    assert missing_glyphs("五", font_stack=["DejaVu Sans"]) == ["五"]


@pytest.mark.skipif(not cjk_font_available(),
                    reason="這台機器沒有安裝 CJK 字型；『中文無方框』無法在此驗證")
def test_vocab_words_render_without_tofu():
    """驗收條件：中文正確顯示無方框。用 `config/vocab.json` 的實際詞。"""
    import json
    from pathlib import Path

    vocab = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "vocab.json")
        .read_text(encoding="utf-8"))
    text = "".join(word["text"] for word in vocab["words"])
    assert missing_glyphs(text) == []


def test_unicode_minus_is_disabled():
    """CJK 字型常常缺 U+2212，而那個方框出現在座標軸上特別難察覺。"""
    assert rc_params()["axes.unicode_minus"] is False


# --------------------------------------------------------- 黑白列印可辨


def test_relative_luminance_matches_known_values():
    assert relative_luminance("#000000") == pytest.approx(0.0)
    assert relative_luminance("#ffffff") == pytest.approx(1.0)
    assert 0.2 < relative_luminance("#808080") < 0.25


def test_sequential_colormaps_are_luminance_monotonic():
    """**這就是「灰階下還讀得出來」的定義。**"""
    for name in (SEQUENTIAL_CMAP, SEQUENTIAL_ALT_CMAP):
        assert is_luminance_monotonic(name), name
        values = colormap_luminance(name)
        assert values[-1] - values[0] > 0.5, f"{name} 的亮度跨度太小"


def test_diverging_colormap_is_not_grayscale_safe_by_itself():
    """**這條測試釘住的是一個限制，不是一個功能。**

    `RdBu_r` 的兩端亮度幾乎相同——「+2 mm」與「−2 mm」印成黑白後長得一樣。
    而且這不是配色選錯：發散色階的正負號本來就靠色相承載。
    """
    assert not is_luminance_monotonic(DIVERGING_CMAP)
    values = colormap_luminance(DIVERGING_CMAP)
    assert abs(values[0] - values[-1]) < 0.05, "兩端亮度應該幾乎相同"


def test_categorical_palette_luminances_are_too_close_for_grayscale():
    """記錄一個真實限制：Okabe-Ito 的相鄰亮度差只有約 0.011。"""
    gaps = palette_luminance_gaps()
    assert min(gaps) < MIN_LUMINANCE_GAP
    assert min(gaps) == pytest.approx(0.0112, abs=0.002)


def test_grayscale_check_passes_for_a_sequential_heatmap():
    with styled():
        assert_grayscale_safe(heatmap_figure(SEQUENTIAL_CMAP))


def test_grayscale_check_rejects_an_unannotated_diverging_heatmap():
    with styled():
        with pytest.raises(AssertionError, match="灰階下正負不可辨"):
            assert_grayscale_safe(heatmap_figure(DIVERGING_CMAP, annotate=False))


def test_grayscale_check_accepts_an_annotated_diverging_heatmap():
    """標了數值就過——`D14` 的熱力圖本來就每格標，剛好合規。"""
    with styled():
        assert_grayscale_safe(heatmap_figure(DIVERGING_CMAP, annotate=True))


def test_opt_out_requires_a_written_reason():
    """`True` 可以無腦傳，半年後沒人知道為什麼關掉。**必填的理由會留在
    原始碼裡。**"""
    with styled():
        fig = heatmap_figure(DIVERGING_CMAP, annotate=False)
        assert_grayscale_safe(fig, diverging_opt_out="純示意圖，不表達實際數值")
        for bad in (True, "", "   ", 1):
            with pytest.raises(ValueError, match="說明理由"):
                assert_grayscale_safe(fig, diverging_opt_out=bad)


# ------------------------------------------------------- PNG + PDF 雙輸出


def _png_dpi(path):
    data = path.read_bytes()
    index = data.find(b"pHYs")
    pixels_per_metre = struct.unpack(">I", data[index + 4:index + 8])[0]
    return round(pixels_per_metre * 0.0254)


def test_save_figure_writes_png_300dpi_and_pdf(tmp_path):
    """驗收條件：PNG(300dpi) + PDF 雙輸出。"""
    with styled():
        written = save_figure(line_figure(), tmp_path / "fig")

    names = {path.suffix for path in written}
    assert names == {".png", ".pdf"}
    png = tmp_path / "fig.png"
    pdf = tmp_path / "fig.pdf"
    assert _png_dpi(png) == DPI
    assert pdf.read_bytes()[:5] == b"%PDF-"
    assert pdf.stat().st_size > 0


def test_save_figure_can_add_svg(tmp_path):
    with styled():
        written = save_figure(line_figure(), tmp_path / "fig",
                              formats=("png", "pdf", "svg"))
    assert (tmp_path / "fig.svg").exists()
    assert len(written) == 3
    assert b"<svg" in (tmp_path / "fig.svg").read_bytes()[:400]


def test_save_figure_creates_missing_directories(tmp_path):
    with styled():
        save_figure(line_figure(), tmp_path / "deep" / "nested" / "fig")
    assert (tmp_path / "deep" / "nested" / "fig.pdf").exists()


def test_save_figure_blocks_a_cjk_labelled_figure(tmp_path):
    """存檔當下最容易修——等到論文排版才發現一張圖有中文標籤，
    要回頭重跑整個實驗。"""
    with styled():
        fig = line_figure()
        fig.axes[0].set_title("中文標題")
        with pytest.raises(AssertionError, match="CJK"):
            save_figure(fig, tmp_path / "bad")
    assert not (tmp_path / "bad.png").exists()


def test_save_figure_blocks_an_unreadable_diverging_figure(tmp_path):
    with styled():
        with pytest.raises(AssertionError, match="灰階"):
            save_figure(heatmap_figure(DIVERGING_CMAP), tmp_path / "bad")


def test_save_figure_checks_are_on_by_default():
    """**預設關掉的檢查等於沒有檢查。**"""
    import inspect

    signature = inspect.signature(save_figure)
    assert signature.parameters["check_english"].default is True
    assert signature.parameters["check_grayscale"].default is True
