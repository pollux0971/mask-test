"""圖表文字檢查的共用 helper。

專案規則：**圖表文字一律英文**。原本 `D14`/`D17` 各自寫了一份一模一樣的
`has_cjk`，這裡收成一份——兩份相同的字面實作遲早會漂掉一份，而漂掉的那份
不會報錯，只會安靜地少檢查一些東西。

`analysis/experiments/` 底下其他實驗（`exp_a_snr.py`、`exp_d05_*`、
`exp_d09_*`、`exp_d10_*`、`exp_d12_*`）目前仍是各自一份，那些檔案屬於
其他 agent，改由調度員通知換過來。
"""

# CJK 統一表意文字（含擴充 A）與常用全形標點。只要出現任何一個就算不合格。
_CJK_RANGES = (
    ("㐀", "䶿"),   # 擴充 A
    ("一", "鿿"),   # 統一表意文字
    ("豈", "﫿"),   # 相容表意文字
    ("　", "〿"),   # CJK 標點（、。「」等）
    ("！", "｠"),   # 全形 ASCII 變體
)


def has_cjk(text):
    """字串裡有沒有 CJK 字元或全形標點。"""
    if not text:
        return False
    return any(lo <= ch <= hi for ch in str(text) for lo, hi in _CJK_RANGES)


def figure_texts(fig):
    """把一個 matplotlib Figure 上所有**人看得到的字串**收集起來。

    刻意包含 tick label 與格內標註，不只標題與軸標籤——`D14` 的 y 軸就是
    viseme 名稱、格內是數值標註，只檢查標題會整個漏掉。
    """
    texts = []
    if getattr(fig, "_suptitle", None) is not None:
        texts.append(fig._suptitle.get_text())
    for ax in fig.axes:
        texts.append(ax.get_title())
        texts.append(ax.get_xlabel())
        texts.append(ax.get_ylabel())
        texts += [label.get_text() for label in ax.get_xticklabels()]
        texts += [label.get_text() for label in ax.get_yticklabels()]
        texts += [t.get_text() for t in ax.texts]
        legend = ax.get_legend()
        if legend is not None:
            texts.append(legend.get_title().get_text())
            texts += [t.get_text() for t in legend.get_texts()]
    return [t for t in texts if t]


def assert_english_only(fig):
    """圖上沒有任何 CJK 字元。不合格就 `AssertionError` 並指出是哪一段。"""
    offenders = [t for t in figure_texts(fig) if has_cjk(t)]
    assert not offenders, f"圖表文字含 CJK 字元: {offenders!r}"
