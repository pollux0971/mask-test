"""`analysis/reporting/verification_report.py`（D15）的測試。

報告核心是純函式，所以這裡不需要真的跑實驗——矩陣怎麼排、失敗怎麼警示、
矛盾怎麼攤開，全部驗得到。
"""
import pytest

from analysis.reporting.verification_report import (
    EXPERIMENT_ORDER,
    MUST_PASS,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    ExperimentOutcome,
    build_pass_matrix,
    build_report,
    cross_experiment_checks,
    known_limitations,
    render_summary_html,
    render_summary_markdown,
    sort_outcomes,
)


def outcome(key, status=STATUS_PASS, **kwargs):
    defaults = {
        "name": f"實驗{key}", "metric": "指標", "measured": "1.0",
        "criterion": "> 0",
    }
    defaults.update(kwargs)
    return ExperimentOutcome(key=key, status=status, **defaults)


def all_pass():
    return [outcome(key) for key in EXPERIMENT_ORDER]


# ------------------------------------------------------------ 通過矩陣置頂


def test_pass_matrix_is_the_first_section():
    """驗收條件：summary 的通過矩陣置頂。"""
    text = render_summary_markdown(build_report(all_pass(), is_synthetic=False))
    heading_order = [line for line in text.split("\n") if line.startswith("## ")]
    assert heading_order[0] == "## 通過矩陣"


def test_matrix_rows_follow_the_story_order():
    """story 的範例表順序是 C₀ → A → B → C → E。"""
    shuffled = [outcome(key) for key in ("E", "C", "C0", "B", "A")]
    assert [row["key"] for row in build_pass_matrix(shuffled)] == list(EXPERIMENT_ORDER)


def test_unknown_experiments_sort_last_instead_of_vanishing():
    rows = build_pass_matrix([outcome("Z"), outcome("A")])
    assert [row["key"] for row in rows] == ["A", "Z"]


def test_matrix_has_all_five_columns():
    rows = build_pass_matrix(all_pass())
    for row in rows:
        assert set(row) >= {"key", "name", "metric", "measured", "criterion", "mark"}


# -------------------------------------------------------------- 紅色警示


def test_must_pass_failure_produces_a_banner_above_the_matrix():
    """驗收條件：失敗時有紅色警示。**而且要在矩陣之前**——矩陣裡的 ✗ 跟
    「整份報告不可信」不是同一個層級的事。"""
    outcomes = all_pass()
    outcomes[1] = outcome("A", STATUS_FAIL, diagnosis="換一種戴法再試")
    text = render_summary_markdown(build_report(outcomes, is_synthetic=False))

    banner_pos = text.index("🔴")
    matrix_pos = text.index("## 通過矩陣")
    assert banner_pos < matrix_pos
    assert "必通過項目失敗" in text
    assert "A 實驗A" in text


def test_non_must_pass_failure_does_not_produce_the_blocking_banner():
    """`B`（跨次戴 CV）失敗代表「戴法流程要改進」，不是「資料不可信」。"""
    outcomes = all_pass()
    outcomes[2] = outcome("B", STATUS_FAIL)
    report = build_report(outcomes, is_synthetic=False)
    assert report["blocking"] == []
    text = render_summary_markdown(report)
    assert "必通過項目失敗" not in text
    assert "✗ **FAIL**" in text            # 但矩陣裡仍然是 FAIL


def test_error_also_blocks_when_it_is_a_must_pass_experiment():
    """`ERROR` 是 bug 不是結論，但必通過項目炸掉一樣不能信下游。"""
    outcomes = all_pass()
    outcomes[0] = outcome("C0", STATUS_ERROR, reason="ZeroDivisionError")
    report = build_report(outcomes, is_synthetic=False)
    assert [o.key for o in report["blocking"]] == ["C0"]
    text = render_summary_markdown(report)
    assert "執行時發生錯誤" in text
    assert "這是程式問題不是實驗結論" in text


def test_diagnosis_is_rendered_for_failures():
    """驗收條件：失敗時有**診斷建議**。"""
    outcomes = all_pass()
    outcomes[1] = outcome("A", STATUS_FAIL, diagnosis="先確認裝置有戴好、距離對不對")
    text = render_summary_markdown(build_report(outcomes, is_synthetic=False))
    assert "## 診斷建議" in text
    assert "先確認裝置有戴好" in text


def test_no_diagnosis_section_when_everything_passes():
    text = render_summary_markdown(build_report(all_pass(), is_synthetic=False))
    assert "## 診斷建議" not in text


def test_must_pass_set_is_documented_in_the_matrix():
    text = render_summary_markdown(build_report(all_pass(), is_synthetic=False))
    assert "🔒" in text
    assert "必通過項目" in text
    for key in MUST_PASS:
        row = next(r for r in build_pass_matrix(all_pass()) if r["key"] == key)
        assert row["must_pass"] is True


# ------------------------------------------- skipped ≠ fail ≠ error


def test_skipped_keeps_its_row_and_is_not_a_failure():
    """「沒跑」不是「通過」也不是「失敗」——而且**不能從矩陣裡消失**。"""
    outcomes = all_pass()
    outcomes[2] = outcome("B", STATUS_SKIPPED, reason="需要至少 2 個 wear_id")
    report = build_report(outcomes, is_synthetic=False)

    assert [o.key for o in report["skipped"]] == ["B"]
    assert report["failed"] == [] and report["blocking"] == []
    rows = {row["key"]: row for row in report["matrix"]}
    assert rows["B"]["mark"] == "— SKIPPED"
    assert len(report["matrix"]) == len(EXPERIMENT_ORDER)


def test_skipped_must_pass_does_not_block_but_is_called_out():
    """必通過項目沒跑 ≠ 失敗，但一致性檢查要講出來。"""
    outcomes = all_pass()
    outcomes[0] = outcome("C0", STATUS_SKIPPED, reason="需要兩種擷取組態")
    report = build_report(outcomes, is_synthetic=False)
    assert report["blocking"] == []
    text = render_summary_markdown(report)
    assert "因資料不足而未執行" in text
    assert "「沒跑」不是「通過」也不是「失敗」" in text


# ------------------------------------------------------- 跨實驗一致性


def test_dual_matrix_conflict_is_surfaced():
    """`D13` 說互補、`D16` 說增益為負、`D19` 說消融沒差 → 三者矛盾。"""
    outcomes = all_pass()
    outcomes[3] = outcome("C", STATUS_PASS, detail={"complementary": True})
    findings = cross_experiment_checks(
        outcomes, {"d16_gain": -0.2, "d19_dual_matrix": {"passed": False}})
    conflict = next(f for f in findings if f["severity"] == "conflict")
    assert conflict["topic"] == "第二顆 ToF 是否帶來額外資訊"
    assert len(conflict["sources"]) == 3
    assert "負增益" in conflict["message"] or "低估" in conflict["message"]


def test_dual_matrix_agreement_produces_no_conflict():
    outcomes = all_pass()
    outcomes[3] = outcome("C", STATUS_PASS, detail={"complementary": True})
    findings = cross_experiment_checks(
        outcomes, {"d16_gain": 0.4, "d19_dual_matrix": {"passed": True}})
    assert not [f for f in findings
                if f["topic"] == "第二顆 ToF 是否帶來額外資訊"]


def test_single_opinion_is_reported_as_missing_data_not_as_agreement():
    """只有一個來源時沒有「矛盾」可言——**但也不能安靜地什麼都不說**。

    這條原本斷言「回空的」。開發 `D20` 時真的踩到那個坑：`D13` 那一票因為
    讀錯鍵名（`complementary` vs `passed`）永遠是 `None`，交叉檢查就永遠只有
    一票、永遠不會發現矛盾——**而報告看起來完全正常**。

    「這個檢查沒有資料」跟「這個檢查通過了」必須分得出來，所以現在會回一條
    `note` 講明缺了幾個來源。
    """
    outcomes = all_pass()
    outcomes[3] = outcome("C", STATUS_PASS, detail={"complementary": True})
    findings = cross_experiment_checks(outcomes, {})
    vote = next(f for f in findings if f["topic"] == "第二顆 ToF 是否帶來額外資訊")
    assert vote["severity"] == "note"          # 不是 conflict——沒得比就不是矛盾
    assert "只有 1 個來源" in vote["message"]
    assert "「沒有資料」不等於「沒有矛盾」" in vote["message"]


def test_snr_fail_but_tof_separable_is_the_dangerous_conflict():
    """**SNR 量不到訊號、Silhouette 卻分得開** → 分離度多半來自 Mel。
    那正是這個專案最想避免的誤讀。"""
    outcomes = all_pass()
    outcomes[1] = outcome("A", STATUS_FAIL)
    outcomes[3] = outcome("C", STATUS_PASS, detail={"tof_separable": True})
    findings = cross_experiment_checks(outcomes)
    conflict = next(f for f in findings if f["topic"] == "ToF 訊號強度與可分離性")
    assert conflict["severity"] == "conflict"
    assert "不要直接把它讀成" in conflict["message"]


def test_snr_pass_but_not_separable_is_only_a_note():
    outcomes = all_pass()
    outcomes[1] = outcome("A", STATUS_PASS)
    outcomes[3] = outcome("C", STATUS_FAIL, detail={"tof_separable": False})
    findings = cross_experiment_checks(outcomes)
    note = next(f for f in findings if f["topic"] == "ToF 訊號強度與可分離性")
    assert note["severity"] == "note"


def test_uniform_weak_tof_conflicts_with_a_passing_snr():
    outcomes = all_pass()
    outcomes[1] = outcome("A", STATUS_PASS)
    outcomes[4] = outcome("E", STATUS_PASS, detail={"uniform_weak_tof": True})
    findings = cross_experiment_checks(outcomes)
    conflict = next(f for f in findings
                    if f["topic"] == "ToF 在所有 viseme 上均勻地弱")
    assert conflict["severity"] == "conflict"
    assert "先確認兩個實驗吃的是同一批資料" in conflict["message"]


def test_consistency_checks_never_change_a_verdict():
    """一致性檢查只新增條目，**不改判定**——否則沒人知道到底是哪個實驗有問題。"""
    outcomes = all_pass()
    outcomes[3] = outcome("C", STATUS_PASS, detail={"complementary": True})
    report = build_report(outcomes, extras={"d16_gain": -0.5,
                                            "d19_dual_matrix": {"passed": False}})
    assert report["inconsistencies"]
    assert all(row["status"] == STATUS_PASS for row in report["matrix"])
    assert report["failed"] == []


def test_every_finding_names_its_sources():
    outcomes = all_pass()
    outcomes[1] = outcome("A", STATUS_FAIL)
    outcomes[3] = outcome("C", STATUS_PASS,
                          detail={"tof_separable": True, "complementary": True})
    outcomes[4] = outcome("E", STATUS_PASS, detail={"uniform_weak_tof": True})
    findings = cross_experiment_checks(outcomes, {"d16_gain": -0.1})
    assert findings
    for item in findings:
        assert item["sources"], item
        assert item["severity"] in ("conflict", "note")


# ------------------------------------------------------------- 已知限制


def test_synthetic_warning_is_a_limitation_and_a_banner():
    report = build_report(all_pass(), is_synthetic=True)
    text = render_summary_markdown(report)
    assert "合成資料，不是真實結論" in text
    assert any("合成資料" in item for item in report["limitations"])


def test_real_data_drops_the_synthetic_warning():
    report = build_report(all_pass(), is_synthetic=False)
    assert not any("合成資料" in item for item in report["limitations"])
    assert "合成資料" not in render_summary_markdown(report)


def test_tongue_viseme_limitation_is_always_present():
    """§6 的裁決：接受舌音為空列，但**報告必須寫出這條限制**。"""
    for synthetic in (True, False):
        report = build_report(all_pass(), is_synthetic=synthetic)
        text = render_summary_markdown(report)
        assert "舌音" in text
        assert "vocab.json" in text


def test_zone_layout_caveat_is_always_present():
    text = render_summary_markdown(build_report(all_pass(), is_synthetic=False))
    assert "ASSUMED, unverified" in text


def test_d16_negative_gain_caveat_appears_when_d16_ran():
    """調度員指定：`D16` 的負增益警示必須進報告。"""
    report = build_report(all_pass(), extras={"d16_gain": -0.3})
    text = render_summary_markdown(report)
    assert "逐主成分 MI 加總隱含" in text
    assert "交叉驗證" in text


def test_d16_caveat_absent_when_d16_did_not_run():
    """沒跑 `D16` 就不要放它的警語——無關的警語會讓人略過所有警語。"""
    report = build_report(all_pass(), extras={})
    assert not any("逐主成分" in item for item in known_limitations(report))


# ------------------------------------------------------------- HTML 輸出


def test_html_is_produced_and_self_contained():
    """驗收條件：同時輸出 HTML。**不可引用外部資源**——`C23` 的 iframe
    與本機開檔在離線時都會變破圖。"""
    html_text = render_summary_html(build_report(all_pass(), is_synthetic=False))
    assert html_text.startswith("<!DOCTYPE html>")
    assert "<table>" in html_text
    for external in ("http://", "https://", "<script"):
        assert external not in html_text


def test_html_marks_failures_and_banners():
    outcomes = all_pass()
    outcomes[1] = outcome("A", STATUS_FAIL)
    html_text = render_summary_html(build_report(outcomes, is_synthetic=False))
    assert 'class="banner error"' in html_text
    assert '<tr class="fail">' in html_text
    assert "✗ FAIL" in html_text
    assert "**" not in html_text          # HTML 不該漏出 Markdown 標記


def test_html_escapes_experiment_text():
    outcomes = [outcome("A", STATUS_FAIL, name="<script>alert(1)</script>")]
    html_text = render_summary_html(build_report(outcomes, is_synthetic=False))
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;" in html_text


def test_html_and_markdown_agree_on_the_matrix():
    outcomes = all_pass()
    outcomes[2] = outcome("B", STATUS_SKIPPED, reason="缺資料")
    report = build_report(outcomes, is_synthetic=False)
    md = render_summary_markdown(report)
    html_text = render_summary_html(report)
    for row in report["matrix"]:
        assert row["measured"] in md
        assert row["measured"] in html_text


# ------------------------------------------------------------------ 其他


def test_report_records_sessions_and_elapsed_time():
    report = build_report(all_pass(), is_synthetic=False,
                          session_paths=["a.h5", "b.h5"], elapsed_s=12.5)
    text = render_summary_markdown(report)
    assert "`a.h5`" in text and "`b.h5`" in text
    assert "12.5 秒" in text


def test_markdown_ends_with_a_single_newline():
    text = render_summary_markdown(build_report(all_pass(), is_synthetic=False))
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_empty_outcome_list_does_not_crash():
    report = build_report([], is_synthetic=True)
    assert report["matrix"] == []
    assert render_summary_markdown(report)
    assert render_summary_html(report)


def test_sort_outcomes_is_stable_for_unknown_keys():
    outcomes = [outcome("Y"), outcome("X"), outcome("A")]
    assert [o.key for o in sort_outcomes(outcomes)] == ["A", "X", "Y"]
