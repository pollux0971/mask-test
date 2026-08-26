"""`analysis/reporting/text_checks.py` 的測試。"""
import pytest

from analysis.reporting.text_checks import assert_english_only, figure_texts, has_cjk


@pytest.mark.parametrize("text,expected", [
    ("Viseme A", False),
    ("mean max|z| (sigma from rest baseline)", False),
    ("ToF_L / ToF_R", False),
    ("", False),
    (None, False),
    ("A 雙唇", True),
    ("敏感度", True),
    ("（括號）", True),          # 全形標點也算
    ("測試、逗號", True),
])
def test_has_cjk(text, expected):
    assert has_cjk(text) is expected


def _figure():
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1], label="series")
    ax.set_title("Title")
    ax.set_xlabel("x label")
    ax.set_ylabel("y label")
    ax.text(0.5, 0.5, "cell text")
    ax.legend(title="legend title")
    return fig


def test_figure_texts_collects_ticks_texts_and_legend():
    """只檢查標題與軸標籤會漏掉 tick label 與格內標註——`D14` 的 y 軸就是
    viseme 名稱、格內是數值標註。"""
    texts = figure_texts(_figure())
    for expected in ("Title", "x label", "y label", "cell text",
                     "series", "legend title"):
        assert expected in texts


def test_assert_english_only_passes_for_an_english_figure():
    assert_english_only(_figure())


def test_assert_english_only_names_the_offending_text():
    fig = _figure()
    fig.axes[0].set_title("中文標題")
    with pytest.raises(AssertionError, match="中文標題"):
        assert_english_only(fig)


def test_assert_english_only_catches_a_cjk_tick_label():
    fig = _figure()
    fig.axes[0].set_xticks([0, 1])
    fig.axes[0].set_xticklabels(["ok", "壞掉"])
    with pytest.raises(AssertionError, match="壞掉"):
        assert_english_only(fig)
