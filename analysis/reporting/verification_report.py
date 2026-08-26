"""D15 — 驗證報告的組裝核心（純函式，不碰檔案、不跑實驗）。

`analysis/run_all.py` 負責讀 session、跑實驗、寫檔；這裡只負責把六個實驗
的結果收成一張**通過矩陣**與一份人看得懂的報告。拆開的理由：報告的正確性
（矩陣怎麼排、失敗怎麼警示、矛盾怎麼攤開）全部測得動，不需要真的跑一次
兩分鐘的實驗。

## 通過矩陣要置頂，因為它是每天最常看的東西

story 原文：「**這張表是你每天最常看的東西**，所以要置頂、要能一眼掃完。」
所以 `render_summary_markdown()` 的順序是刻意的：

    紅色警示（若有） → 通過矩陣 → 跨實驗一致性 → 已知限制 → 各實驗細節

警示在矩陣**之前**：矩陣本身已經有 ✗ FAIL，但「必通過項目掛了」跟
「某個描述性指標沒達標」是兩件事，前者代表**整份報告的數字都不能信**。

## 跨實驗一致性檢查

六個實驗從不同角度量同一件事。**它們對得上才可信。**

若 `D13` 說雙矩陣互補、`D16` 說資訊增益是負的、`D19` 的消融說拿掉第二顆
沒差——**那三個結論互相矛盾**，報告必須把矛盾攤出來，而不是各印各的然後
讓讀的人自己去發現（實務上沒有人會發現，因為三個結論分散在三個小節裡，
而且每一個單獨看都很合理）。

⚠️ 一致性檢查**不改變任何實驗的判定**。它只新增 `inconsistencies` 條目。
一個實驗的 PASS 不會因為跟別人對不上就變成 FAIL——那會讓「判定」這件事
失去意義，也會讓人搞不清楚到底是哪一個實驗有問題。

## `skipped` 與 `error` 不是 `fail`

三者在報告裡分得很開：

* `fail` —— 實驗跑了，指標沒達標。**這是一個結果。**
* `skipped` —— 資料不足以跑（例如 `D12` 需要跨次戴的資料，單一 session
  給不了）。**這不是結果，是缺口。**
* `error` —— 實驗炸了。**這是 bug，不是結論。**

把 `skipped` 混進 `fail` 會讓人以為「試過了但不行」；把它從矩陣裡拿掉更糟
——**沒有人會發現那一列從來沒跑過**。所以三者都佔一列，狀態欄不同。
"""
import html
from dataclasses import dataclass, field
from typing import Optional

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

STATUS_MARKS = {
    STATUS_PASS: "✓ PASS",
    STATUS_FAIL: "✗ **FAIL**",
    STATUS_SKIPPED: "— SKIPPED",
    STATUS_ERROR: "⚠ **ERROR**",
}

# 必通過項目：掛掉時整份報告的數字都不能信，所以要紅色警示。
#
# ⚠️ **story 沒有規定哪些是必通過的，這是本模組的判斷**，理由如下
# （見完成回報，等調度員裁決）：
#
# * `C0` 串擾 —— 硬體閘門。兩顆感測器互相干擾的話，**下游每一個數字都被
#   污染了**，包括 A/B/C/E 全部。
# * `A`  SNR  —— 訊號閘門。ToF 的 SNR < 3 代表訊號根本沒進來（`E03` 已經
#   把 SNR < 3 當成「至少換三種戴法」的觸發條件）。沒有訊號就沒有什麼好
#   分析的。
# * `C`  Silhouette —— **核心科學主張**本身：「ToF 到底有沒有帶來資訊」。
#
# `B`（跨次戴 CV）與 `E`（viseme 型態）刻意**不列入**：B 失敗代表「戴法
# 流程要改進」，E 失敗代表「預期型態沒出現」——兩者都是發現，不是「資料
# 不可信」。
MUST_PASS = ("C0", "A", "C")

# 實驗的顯示順序（照 story 的範例表）。
EXPERIMENT_ORDER = ("C0", "A", "B", "C", "E")


@dataclass(frozen=True)
class ExperimentOutcome:
    """一個實驗在通過矩陣裡佔的那一列 + 它自己的細節。"""

    key: str                       # "C0" / "A" / ...
    name: str                      # 人看的名稱
    metric: str                    # 指標名稱
    measured: str                  # 實測值（已格式化，含單位）
    criterion: str                 # 標準（例如 "< 2 mm"）
    status: str
    detail: dict = field(default_factory=dict)
    diagnosis: Optional[str] = None    # 失敗時的診斷建議
    reason: Optional[str] = None       # skipped/error 的原因
    report_md: Optional[str] = None    # 該實驗自己的完整 Markdown
    figures: tuple = ()                # (檔名, Figure) 的序列

    @property
    def is_must_pass(self) -> bool:
        return self.key in MUST_PASS

    @property
    def blocks_report(self) -> bool:
        """這一列是否讓整份報告的數字不可信。"""
        return self.is_must_pass and self.status in (STATUS_FAIL, STATUS_ERROR)

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "metric": self.metric,
            "measured": self.measured, "criterion": self.criterion,
            "status": self.status, "detail": self.detail,
            "diagnosis": self.diagnosis, "reason": self.reason,
            "is_must_pass": self.is_must_pass,
        }


def sort_outcomes(outcomes):
    """照 `EXPERIMENT_ORDER` 排；不在名單上的排在後面（依 key）。"""
    order = {key: i for i, key in enumerate(EXPERIMENT_ORDER)}
    return sorted(outcomes, key=lambda o: (order.get(o.key, len(order)), o.key))


def build_pass_matrix(outcomes):
    """通過矩陣的資料列。`render_*` 用它產表，測試也直接驗它。"""
    return [
        {
            "key": o.key, "name": o.name, "metric": o.metric,
            "measured": o.measured, "criterion": o.criterion,
            "status": o.status, "mark": STATUS_MARKS[o.status],
            "must_pass": o.is_must_pass,
        }
        for o in sort_outcomes(outcomes)
    ]


# ---------------------------------------------------------------- 一致性檢查


def cross_experiment_checks(outcomes, extras=None):
    """跨實驗一致性。回傳 `[{"topic", "severity", "message", "sources"}]`。

    `extras` 放不在通過矩陣裡、但會參與比對的實驗結果，目前是：

        {"d16_gain": float, "d19_dual_matrix": {...}}

    `severity`：`"conflict"`（結論互相矛盾）／`"note"`（值得注意但不矛盾）。

    **每一條都要指出是哪幾個來源在打架**，否則讀的人只知道「有問題」卻不
    知道該去看哪一節。
    """
    extras = extras or {}
    by_key = {o.key: o for o in outcomes}
    findings = []

    findings += _check_dual_matrix_agreement(by_key, extras)
    findings += _check_tof_signal_vs_separability(by_key)
    findings += _check_viseme_vs_snr(by_key)
    findings += _check_skips(outcomes)
    return findings


def _check_dual_matrix_agreement(by_key, extras):
    """「第二顆 ToF 有沒有用」——三個實驗各自給答案，必須一致。

    * `D13` 的 `complementarity`：Combined 是否明顯優於單顆
    * `D16` 的 `dual_matrix_gain`：`I(Combined) - max(I(L), I(R))`
    * `D19` 的 `dual_matrix_vs_single` 消融：拿掉第二顆準確率掉多少
    """
    votes = {}
    silhouette = by_key.get("C")
    if silhouette is not None and silhouette.status in (STATUS_PASS, STATUS_FAIL):
        value = silhouette.detail.get("complementary")
        if value is not None:
            votes["D13 Silhouette 互補性"] = bool(value)

    gain = extras.get("d16_gain")
    if gain is not None:
        votes["D16 雙矩陣資訊增益"] = bool(gain > 0)

    ablation = extras.get("d19_dual_matrix")
    if isinstance(ablation, dict) and ablation.get("passed") is not None:
        votes["D19 雙矩陣消融"] = bool(ablation["passed"])

    if len(votes) < 2:
        # **票數不足要講出來，不能安靜地回空的。**
        # 開發時真的踩到：`D13` 那一票因為讀錯鍵名而永遠是 `None`，交叉檢查
        # 就永遠只有一票、永遠不會發現矛盾——而報告看起來完全正常。
        # 「這個檢查沒有資料」跟「這個檢查通過了」必須分得出來。
        have = sorted(votes) or ["（無）"]
        return [{
            "topic": "第二顆 ToF 是否帶來額外資訊",
            "severity": "note",
            "sources": sorted(votes),
            "message": (
                f"**這個交叉檢查目前只有 {len(votes)} 個來源**（{'、'.join(have)}），"
                "至少要 2 個才比得出矛盾。`D13` 的互補性、`D16` 的資訊增益、"
                "`D19` 的消融應該三者都在——缺的那幾個請確認有沒有跑起來。"
                "**「沒有資料」不等於「沒有矛盾」。**"
            ),
        }]
    if len(set(votes.values())) < 2:
        return []

    yes = [name for name, v in votes.items() if v]
    no = [name for name, v in votes.items() if not v]
    return [{
        "topic": "第二顆 ToF 是否帶來額外資訊",
        "severity": "conflict",
        "sources": sorted(votes),
        "message": (
            f"**{'、'.join(yes)}** 說有，**{'、'.join(no)}** 說沒有。"
            "三者量的是同一件事，結論必須一致——"
            "先看 `D16` 的負增益警示（逐主成分 MI 加總隱含各主成分資訊互不"
            "重疊的假設，該假設不成立時增益會被低估甚至為負），再用 `D13` 的"
            "Silhouette 與 `D19` 的消融交叉驗證，**不要只憑其中一個下結論**。"
        ),
    }]


def _check_tof_signal_vs_separability(by_key):
    """SNR 說「有訊號」但 Silhouette 說「分不開」，或反過來。

    反過來那個方向更值得警覺：**SNR 量不到訊號、Silhouette 卻分得很開**，
    通常代表分離度來自 Mel 而不是 ToF——那正是這個專案最想避免的誤讀。
    """
    snr = by_key.get("A")
    sil = by_key.get("C")
    if snr is None or sil is None:
        return []
    if snr.status not in (STATUS_PASS, STATUS_FAIL) or sil.status not in (STATUS_PASS, STATUS_FAIL):
        return []

    tof_separable = sil.detail.get("tof_separable")
    if tof_separable is None:
        return []

    if snr.status == STATUS_FAIL and tof_separable:
        return [{
            "topic": "ToF 訊號強度與可分離性",
            "severity": "conflict",
            "sources": ["A SNR", "C Silhouette"],
            "message": (
                "**SNR 量不到 ToF 訊號，但 ToF 的 Silhouette 卻分得開。**"
                "兩者矛盾。最可能的解釋是分離度其實來自別的地方"
                "（特徵組裝有沒有混到 Mel？標籤有沒有洩漏？），"
                "**不要直接把它讀成「ToF 有效」**——那正是這個專案最想避免的誤讀。"
            ),
        }]
    if snr.status == STATUS_PASS and tof_separable is False:
        return [{
            "topic": "ToF 訊號強度與可分離性",
            "severity": "note",
            "sources": ["A SNR", "C Silhouette"],
            "message": (
                "SNR 顯示 ToF 有訊號，但 Silhouette 分不開。訊號存在不等於"
                "**類別間**有差異——先看 `D14` 的 viseme 熱力圖是不是所有音素"
                "長得都一樣，再考慮特徵或降維的問題。"
            ),
        }]
    return []


def _check_viseme_vs_snr(by_key):
    """`D14` 說「ToF 均勻地弱」時，那是實驗 A 的問題不是音素的問題。"""
    viseme = by_key.get("E")
    snr = by_key.get("A")
    if viseme is None or not viseme.detail.get("uniform_weak_tof"):
        return []
    snr_note = ""
    if snr is not None and snr.status == STATUS_PASS:
        snr_note = ("⚠️ 而且**實驗 A 的 SNR 是通過的**——兩者對不上，"
                    "先確認兩個實驗吃的是同一批資料。")
    return [{
        "topic": "ToF 在所有 viseme 上均勻地弱",
        "severity": "conflict" if snr_note else "note",
        "sources": ["E Viseme", "A SNR"],
        "message": (
            "`D14` 顯示 ToF 在**所有** viseme 上都弱。均勻的弱通常代表訊號"
            "根本沒進來（戴法、距離、對焦），**不是**「這些音素本來就難」。"
            + snr_note
        ),
    }]


def _check_skips(outcomes):
    skipped = [o for o in outcomes if o.status == STATUS_SKIPPED]
    if not skipped:
        return []
    names = "、".join(f"{o.key} {o.name}" for o in sort_outcomes(skipped))
    return [{
        "topic": "有實驗沒有跑",
        "severity": "note",
        "sources": [o.key for o in sort_outcomes(skipped)],
        "message": (
            f"{names} 因資料不足而未執行。**「沒跑」不是「通過」也不是「失敗」**"
            "——在補齊資料之前，這份報告對這幾項沒有任何結論。"
        ),
    }]


# ---------------------------------------------------------------- 已知限制


def known_limitations(report):
    """每份報告都必須寫出來的已知限制。

    這些不是「這次跑出來的問題」，而是**系統層級的、每次都成立的**限制。
    寫死在這裡而不是靠人記得加：忘記寫的那一次，就是有人把限制當成結論
    引用的那一次。
    """
    limits = []
    if report.get("is_synthetic", True):
        limits.append(
            "**本報告的所有數字來自合成資料，不是真實結論。** 真實結論待 `E05` "
            "蒐集資料後重跑。"
        )
    limits.append(
        "**舌音（viseme E）未涵蓋。** `config/vocab.json`（§6）的八個詞不含任何 "
        "/t,d,k/ 類的詞，因此 `D14` 的舌音那一列永遠是空的——本系統**無法驗證**"
        "該類別的表現。這是詞彙集的設計取捨，不是量測失敗。"
    )
    limits.append(
        "**zone 佈局 row-major 仍是未驗證假設**（ASSUMED, unverified — see A track/E01）。"
        "所有逐 zone 的空間結論（`D11` 的活躍 zone、`D14` 的熱力圖）在佈局確認前"
        "**方向可能是反的**。"
    )
    if report.get("extras", {}).get("d16_gain") is not None:
        limits.append(
            "**`D16` 的雙矩陣增益為負時不可直接讀成「雙矩陣無互補性」。**"
            "逐主成分 MI 加總隱含各主成分資訊互不重疊的假設；該假設不成立時"
            "增益會被低估甚至為負。請以 `D13` 的 Silhouette 與 `D19` 的消融"
            "交叉驗證。"
        )
    return limits


# ---------------------------------------------------------------- 報告組裝


def build_report(outcomes, *, is_synthetic=True, extras=None, session_paths=(),
                 elapsed_s=None):
    """把一批 `ExperimentOutcome` 收成完整的報告結構。

    `is_synthetic` 預設 `True`——假資料是目前的常態，預設 `False` 會讓忘記
    傳的人產出一份看起來像真實結論的報告。
    """
    outcomes = sort_outcomes(outcomes)
    extras = dict(extras or {})
    report = {
        "is_synthetic": bool(is_synthetic),
        "session_paths": [str(p) for p in session_paths],
        "elapsed_s": elapsed_s,
        "outcomes": outcomes,
        "matrix": build_pass_matrix(outcomes),
        "extras": extras,
        "blocking": [o for o in outcomes if o.blocks_report],
        "failed": [o for o in outcomes if o.status == STATUS_FAIL],
        "errored": [o for o in outcomes if o.status == STATUS_ERROR],
        "skipped": [o for o in outcomes if o.status == STATUS_SKIPPED],
    }
    report["inconsistencies"] = cross_experiment_checks(outcomes, extras)
    report["limitations"] = known_limitations(report)
    return report


def render_summary_markdown(report):
    """`summary.md`。**通過矩陣置頂**（story 驗收條件）。"""
    lines = ["# 驗證報告 summary", ""]
    lines += _banner_lines(report)

    lines += ["## 通過矩陣", ""]
    lines += ["| 實驗 | 指標 | 實測 | 標準 | 判定 |", "|---|---|---|---|---|"]
    for row in report["matrix"]:
        name = f"{row['key']} {row['name']}"
        if row["must_pass"]:
            name += " 🔒"
        lines.append(f"| {name} | {row['metric']} | {row['measured']} "
                     f"| {row['criterion']} | {row['mark']} |")
    lines += ["", "🔒 = 必通過項目（失敗時整份報告的數字都不可信）。", ""]

    lines += _diagnosis_lines(report)
    lines += _inconsistency_lines(report)

    lines += ["## 已知限制", ""]
    lines += [f"* {item}" for item in report["limitations"]]
    lines.append("")

    lines += _footer_lines(report)
    return "\n".join(lines).rstrip() + "\n"


def _banner_lines(report):
    """紅色警示。**在矩陣之前**——必通過項目掛掉是「整份報告不可信」，
    跟矩陣裡某一格是 ✗ 不是同一個層級的事。"""
    lines = []
    if report["errored"]:
        names = "、".join(f"{o.key} {o.name}" for o in report["errored"])
        lines += [f"> 🔴 **{len(report['errored'])} 個實驗執行時發生錯誤：{names}。**"
                  " 這是程式問題不是實驗結論——先修好再看下面的數字。", ""]
    if report["blocking"]:
        names = "、".join(f"{o.key} {o.name}" for o in report["blocking"])
        lines += [f"> 🔴 **必通過項目失敗：{names}。**"
                  " 這幾項是下游所有數字的前提，**在修好之前，本報告其餘的結論"
                  "都不可信**——不要拿去比較「這次調整有沒有變好」。", ""]
    if report["is_synthetic"]:
        lines += ["> ⚠️ **合成資料，不是真實結論。** 真實結論待 `E05`。", ""]
    return lines


def _diagnosis_lines(report):
    problems = [o for o in report["outcomes"]
                if o.status in (STATUS_FAIL, STATUS_ERROR) and (o.diagnosis or o.reason)]
    if not problems:
        return []
    lines = ["## 診斷建議", ""]
    for outcome in problems:
        mark = STATUS_MARKS[outcome.status]
        lines.append(f"### {outcome.key} {outcome.name} — {mark}")
        lines.append("")
        if outcome.reason:
            lines += [f"原因：{outcome.reason}", ""]
        if outcome.diagnosis:
            lines += [outcome.diagnosis, ""]
    return lines


def _inconsistency_lines(report):
    if not report["inconsistencies"]:
        return ["## 跨實驗一致性", "",
                "沒有偵測到互相矛盾的結論。", ""]
    lines = ["## 跨實驗一致性", "",
             "六個實驗從不同角度量同一件事，**它們對得上才可信**。", ""]
    for item in report["inconsistencies"]:
        icon = "🔴" if item["severity"] == "conflict" else "⚠️"
        lines += [f"### {icon} {item['topic']}", "",
                  f"來源：{'、'.join(item['sources'])}", "",
                  item["message"], ""]
    return lines


def _footer_lines(report):
    lines = ["---", ""]
    if report["session_paths"]:
        lines.append("Session：" + "、".join(f"`{p}`" for p in report["session_paths"]))
    if report["elapsed_s"] is not None:
        lines.append(f"執行時間：{report['elapsed_s']:.1f} 秒")
    lines.append("")
    return lines


# ---------------------------------------------------------------------- HTML


def render_summary_html(report):
    """`summary.html`（`C23` 檢視用）。

    刻意**不引用外部 CSS/JS**：這份 HTML 會被丟進 `C23` 的 iframe，也可能
    被直接用瀏覽器開啟本機檔案，任何外部資源都會在離線時變成破圖。
    """
    rows = []
    for row in report["matrix"]:
        name = html.escape(f"{row['key']} {row['name']}")
        if row["must_pass"]:
            name += ' <span class="lock" title="must pass">&#128274;</span>'
        rows.append(
            f'<tr class="{row["status"]}">'
            f"<td>{name}</td><td>{html.escape(row['metric'])}</td>"
            f"<td>{html.escape(row['measured'])}</td>"
            f"<td>{html.escape(row['criterion'])}</td>"
            f'<td class="verdict">{html.escape(_plain_mark(row["status"]))}</td></tr>'
        )

    banners = []
    if report["errored"]:
        banners.append(_html_banner("error", "有實驗執行時發生錯誤",
                                    "、".join(f"{o.key} {o.name}" for o in report["errored"])))
    if report["blocking"]:
        banners.append(_html_banner(
            "error", "必通過項目失敗",
            "、".join(f"{o.key} {o.name}" for o in report["blocking"])
            + "。在修好之前，本報告其餘的結論都不可信。"))
    if report["is_synthetic"]:
        banners.append(_html_banner("warn", "合成資料，不是真實結論",
                                    "真實結論待 E05 蒐集資料後重跑。"))

    inconsistencies = "".join(
        f'<li class="{item["severity"]}"><strong>{html.escape(item["topic"])}</strong>'
        f'（{html.escape("、".join(item["sources"]))}）</li>'
        for item in report["inconsistencies"]
    ) or "<li>沒有偵測到互相矛盾的結論。</li>"

    limitations = "".join(f"<li>{html.escape(_strip_md(item))}</li>"
                          for item in report["limitations"])

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>驗證報告 summary</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
        line-height: 1.6; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; }}
th {{ background: #f2f2f2; }}
tr.fail td, tr.error td {{ background: #fdecec; }}
tr.pass td {{ background: #f0f9f0; }}
tr.skipped td {{ background: #f6f6f6; color: #666; }}
td.verdict {{ font-weight: bold; white-space: nowrap; }}
.banner {{ padding: 0.8rem 1rem; border-radius: 4px; margin: 0.6rem 0; }}
.banner.error {{ background: #ffe1e1; border-left: 6px solid #c00; }}
.banner.warn {{ background: #fff6e0; border-left: 6px solid #d99000; }}
li.conflict {{ color: #c00; }}
</style>
</head>
<body>
<h1>驗證報告 summary</h1>
{"".join(banners)}
<h2>通過矩陣</h2>
<table>
<thead><tr><th>實驗</th><th>指標</th><th>實測</th><th>標準</th><th>判定</th></tr></thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
<p>&#128274; = 必通過項目（失敗時整份報告的數字都不可信）。</p>
<h2>跨實驗一致性</h2>
<ul>{inconsistencies}</ul>
<h2>已知限制</h2>
<ul>{limitations}</ul>
</body>
</html>
"""


def _html_banner(kind, title, body):
    return (f'<div class="banner {kind}"><strong>{html.escape(title)}</strong>'
            f"<br>{html.escape(body)}</div>")


def _plain_mark(status):
    """HTML 用的判定文字：不含 Markdown 的粗體標記。"""
    return {
        STATUS_PASS: "✓ PASS", STATUS_FAIL: "✗ FAIL",
        STATUS_SKIPPED: "— SKIPPED", STATUS_ERROR: "⚠ ERROR",
    }[status]


def _strip_md(text):
    """把 Markdown 的粗體/行內碼標記去掉，HTML 那邊用純文字（已 escape）。"""
    return text.replace("**", "").replace("`", "")
