"""HTTP wiring for `/verify/*` —— 背景跑 `D15` 的驗證套件（B19/C23）。

跑一輪要幾秒到兩分鐘，所以介面是「202 立刻回 + 輪詢 `/verify/state`」。
這裡驗的是**那個非同步契約**：狀態怎麼變、衝突怎麼擋、報告怎麼取回。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

import pytest

from test_bridge_session_api import _request, VALID_METADATA
from test_bridge_sse import Rig


@pytest.fixture
def rig(tmp_path):
    """報告輸出導到 tmp_path——**不能寫進 repo 的工作樹**（`Rig` 已經為了
    同樣的理由把 sessions 與 last_session 沙箱化了）。"""
    r = Rig("--proto", "v2",
            bridge_args=("--verification-dir", str(tmp_path / "verification")))
    try:
        yield r
    finally:
        r.close()


def _raw_get(rig, path):
    """回 `(status, content_type, body_bytes)`——靜態檔那條路要看 MIME。"""
    url = f"http://127.0.0.1:{rig.http_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def _recorded_session(rig):
    """錄一筆真的 trial，回傳它在 server 上的路徑。"""
    rig.read_events(3.5)
    assert _request(rig, "POST", "/session/start", VALID_METADATA)[0] == 200
    status, body = _request(rig, "POST", "/session/baseline?seconds=2")
    if status != 200:
        pytest.skip(f"baseline gate rejected the synthetic scene: {body.get('reason')}")
    _request(rig, "POST", "/trial/hold/start", {})
    time.sleep(1.2)
    state = _request(rig, "POST", "/trial/hold/stop")[1]["state"]
    if state == "CONFIRM":
        # 機器忙的時候（例如整套測試一起跑），1.2 秒的按壓會被判定成
        # 「太短」而走到 CONFIRM 而不是直接存檔。那不是失敗——狀態機刻意
        # 不猜，改問使用者。這裡確認它，trial 一樣會存起來。
        #
        # 同一個處理已經套到另外三個 helper（`test_e2e_pipeline.py`、
        # `test_bridge_trial_api.py`、`test_bridge_replay_api.py`）——原本
        # 它們硬斷言 `== "REST"`，在高負載下會偽陽性失敗。
        assert _request(rig, "POST", "/trial/confirm")[0] == 200
    elif state != "REST":
        pytest.fail(f"hold/stop 回到未預期的狀態: {state}")
    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)

    status, listing = _request(rig, "GET", "/replay/sessions")
    assert status == 200 and listing, "剛錄的 session 沒有出現在清單裡"
    return listing[0]["path"]


def _wait_until_idle(rig, timeout=180.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = _request(rig, "GET", "/verify/state")
        assert status == 200
        if not body["running"]:
            return body
        time.sleep(0.5)
    pytest.fail("驗證跑超過逾時仍未結束")


# -- state ----------------------------------------------------------------


def test_state_is_200_and_idle_before_anything_runs(rig):
    """「沒有在跑」不是錯誤，所以一律 200。"""
    status, body = _request(rig, "GET", "/verify/state")
    assert status == 200
    assert body["running"] is False
    assert body["run_id"] is None
    assert body["last_run"] is None
    assert body["elapsed_s"] is None


def test_reports_list_is_empty_before_anything_runs(rig):
    status, body = _request(rig, "GET", "/verify/reports")
    assert status == 200 and body == []


# -- 參數驗證 --------------------------------------------------------------


@pytest.mark.parametrize("body,expected", [
    ({}, "sessions"),
    ({"sessions": []}, "sessions"),
    ({"sessions": "not-a-list"}, "sessions"),
])
def test_run_rejects_a_missing_or_empty_session_list(rig, body, expected):
    status, payload = _request(rig, "POST", "/verify/run", body)
    assert status == 400
    assert expected in payload["error"]


def test_run_rejects_a_path_outside_the_sessions_directory(rig):
    """body 裡的路徑跟 query string 一樣是外來輸入——`..` 會讀到這個
    process 摸得到的任何檔案。"""
    status, payload = _request(rig, "POST", "/verify/run",
                               {"sessions": ["../../../../etc/passwd"]})
    assert status == 400
    assert "不允許" in payload["error"] or "找不到" in payload["error"]


def test_run_rejects_a_non_integer_permutation_count(rig):
    status, payload = _request(rig, "POST", "/verify/run",
                               {"sessions": ["x.h5"], "ablation_permutations": "many"})
    assert status == 400


# -- 完整流程 --------------------------------------------------------------


def test_run_returns_202_and_the_state_follows_through(rig):
    """202 立刻回 → `running=True` → 結束後 `last_run` 有東西。"""
    session = _recorded_session(rig)

    status, accepted = _request(rig, "POST", "/verify/run", {
        "sessions": [session], "ablation_permutations": 0, "fast": True})
    assert status == 202
    assert accepted["run_id"]
    # 前端要能顯示「這一輪是用什麼參數跑的」
    assert accepted["ablation_permutations"] == 0
    assert accepted["fast"] is True
    assert accepted["real"] is False

    final = _wait_until_idle(rig)
    assert final["running"] is False
    assert final["last_error"] is None, final["last_error"]
    assert final["last_run"] is not None
    assert final["last_run"]["run_id"] == accepted["run_id"]
    assert final["last_run"]["is_synthetic"] is True     # 沒有傳 real
    assert final["last_run"]["matrix"], "通過矩陣不該是空的"


def test_three_states_reach_the_client_unmerged(rig):
    """🔴 `fail` / `skipped` / `error` 意思完全不同，**序列化層不可合併**。

    `skipped` 是「資料不足」（一個缺口）、`fail` 是「跑了沒達標」（一個
    結果）。併成「不 OK」之後前端就再也分不出來，而使用者會把缺口讀成失敗。
    """
    session = _recorded_session(rig)
    _request(rig, "POST", "/verify/run",
             {"sessions": [session], "ablation_permutations": 0})
    final = _wait_until_idle(rig)

    statuses = {row["status"] for row in final["last_run"]["matrix"]}
    assert statuses <= {"pass", "fail", "skipped", "error"}
    # 單一 session 一定有跑不動的實驗（跨次戴 CV 至少要兩個 wear_id）
    assert "skipped" in statuses
    assert set(final["last_run"]["counts"]) == {"failed", "skipped", "errored"}

    outcomes = {o["key"]: o for o in final["last_run"]["outcomes"]}
    for outcome in outcomes.values():
        if outcome["status"] == "skipped":
            assert outcome["reason"], "SKIPPED 一定要說明缺什麼"


def test_a_second_run_while_one_is_in_flight_is_409(rig):
    session = _recorded_session(rig)
    first, accepted = _request(rig, "POST", "/verify/run",
                               {"sessions": [session], "ablation_permutations": 0})
    assert first == 202

    second, conflict = _request(rig, "POST", "/verify/run",
                                {"sessions": [session], "ablation_permutations": 0})
    if second == 202:
        pytest.skip("第一輪跑太快，來不及製造衝突")
    assert second == 409
    assert conflict["run_id"] == accepted["run_id"]
    _wait_until_idle(rig)


def test_last_run_survives_the_next_run_starting(rig):
    """⚠️ 跑到一半時 `last_run` **保留上一次的結果不清空**——不然使用者在
    等新結果的期間，畫面會變成一片空白，看起來像「結果不見了」。"""
    session = _recorded_session(rig)
    _request(rig, "POST", "/verify/run",
             {"sessions": [session], "ablation_permutations": 0})
    first = _wait_until_idle(rig)["last_run"]
    assert first is not None

    _request(rig, "POST", "/verify/run",
             {"sessions": [session], "ablation_permutations": 0})
    status, mid = _request(rig, "GET", "/verify/state")
    assert status == 200
    assert mid["last_run"] is not None, "跑到一半不該把上一次的結果清空"
    if mid["running"]:
        assert mid["last_run"]["run_id"] == first["run_id"]
        assert mid["elapsed_s"] is not None
    _wait_until_idle(rig)


# -- 歷史與靜態檔 -----------------------------------------------------------


def test_each_run_gets_its_own_directory(rig):
    """⚠️ 固定目錄的話 `C23` 的「並排比較兩份」永遠只有一份。"""
    session = _recorded_session(rig)
    for _ in range(2):
        _request(rig, "POST", "/verify/run",
                 {"sessions": [session], "ablation_permutations": 0})
        _wait_until_idle(rig)
        time.sleep(1.1)          # run_id 是秒級時間戳

    status, runs = _request(rig, "GET", "/verify/reports")
    assert status == 200
    assert len(runs) >= 2, runs
    assert [r["run_id"] for r in runs] == sorted(
        (r["run_id"] for r in runs), reverse=True), "要新到舊"
    assert all(r["has_summary"] for r in runs)
    assert "summary.html" in runs[0]["files"]


def test_report_files_are_served_with_the_right_mime(rig):
    session = _recorded_session(rig)
    _request(rig, "POST", "/verify/run",
             {"sessions": [session], "ablation_permutations": 0})
    run_id = _wait_until_idle(rig)["last_run"]["run_id"]

    status, ctype, body = _raw_get(rig, f"/verify/reports/{run_id}/summary.md")
    assert status == 200
    assert ctype.startswith("text/markdown")
    assert "通過矩陣".encode() in body

    status, ctype, _ = _raw_get(rig, f"/verify/reports/{run_id}/summary.html")
    assert status == 200 and ctype.startswith("text/html")


def test_report_subdirectories_are_reachable(rig):
    """`figures/` 是真的子目錄——「只取檔名」那種擋法會把它擋掉。"""
    session = _recorded_session(rig)
    _request(rig, "POST", "/verify/run",
             {"sessions": [session], "ablation_permutations": 0})
    run_id = _wait_until_idle(rig)["last_run"]["run_id"]

    status, _, _ = _raw_get(rig, f"/verify/reports/{run_id}/figures/")
    assert status == 404          # 目錄本身不服務，但不能是 500

    status, _, _ = _raw_get(rig, f"/verify/reports/{run_id}/nope.md")
    assert status == 404


@pytest.mark.parametrize("attack", [
    "../../../../etc/passwd",
    "..%2f..%2f..%2f..%2fetc%2fpasswd",
    "/etc/passwd",
])
def test_report_route_blocks_directory_traversal(rig, attack):
    status, _, _ = _raw_get(rig, f"/verify/reports/{attack}")
    assert status in (403, 404), f"穿越沒有被擋: {attack}"


# -- figures: listed as well as served ------------------------------------


def _seed_run(tmp_path, run_id="20260826_120000"):
    """Write a run directory by hand, shaped like write_outputs() leaves it."""
    run = tmp_path / "verification" / run_id
    (run / "figures").mkdir(parents=True, exist_ok=True)
    (run / "summary.md").write_text("# summary\n", encoding="utf-8")
    (run / "summary.html").write_text("<h1>summary</h1>", encoding="utf-8")
    # A one-pixel PNG, so the served bytes are a real image not a text file.
    (run / "figures" / "c_silhouette.png").write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000"
                      "001f15c4890000000a49444154789c6300010000050001"
                      "0d0a2db40000000049454e44ae426082"))
    (run / "figures" / "c_silhouette.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return run


def test_reports_listing_includes_figures(tmp_path):
    """The figures existed and were servable; nothing could learn their names.

    `files` only ever listed the run directory's own entries, and every plot
    goes into a `figures/` subdirectory -- so the panel had no way to render
    a single figure, which is the part of the verification page people
    actually look at.
    """
    _seed_run(tmp_path)
    r = Rig("--proto", "v2",
            bridge_args=("--verification-dir", str(tmp_path / "verification")))
    try:
        status, body = _request(r, "GET", "/verify/reports")
        assert status == 200, body
        assert body, "the seeded run was not listed"
        run = body[0]
        assert run["run_id"] == "20260826_120000"
        assert set(run["figures"]) == {"figures/c_silhouette.png",
                                       "figures/c_silhouette.pdf"}, run["figures"]
        # Relative to the run, so it can be appended to the run URL as-is.
        for fig in run["figures"]:
            assert fig.startswith("figures/")
    finally:
        r.close()


def test_a_listed_figure_is_actually_fetchable(tmp_path):
    """The listing is only useful if the path it hands back resolves.

    Subdirectories are the thing to check: a basename-only guard (the one
    /voice/ uses) would flatten `figures/` and 404 every plot.
    """
    _seed_run(tmp_path)
    r = Rig("--proto", "v2",
            bridge_args=("--verification-dir", str(tmp_path / "verification")))
    try:
        run = _request(r, "GET", "/verify/reports")[1][0]
        for fig in run["figures"]:
            status, ctype, body = _raw_get(
                r, f"/verify/reports/{run['run_id']}/{fig}")
            assert status == 200, (fig, status)
            assert body, f"{fig} served empty"
            if fig.endswith(".png"):
                assert ctype.startswith("image/png"), ctype
                assert body.startswith(b"\x89PNG"), "not actually a PNG"
            elif fig.endswith(".pdf"):
                # C23 hands the PDF to the user for the write-up, so the
                # browser must be told it is one rather than downloading it
                # as an opaque blob.
                assert ctype.startswith("application/pdf"), ctype
                assert body.startswith(b"%PDF"), "not actually a PDF"
    finally:
        r.close()


def test_a_run_with_no_figures_lists_an_empty_array(tmp_path):
    """Absent, not missing: the panel should not have to guard for undefined."""
    run = tmp_path / "verification" / "20260826_090000"
    run.mkdir(parents=True)
    (run / "summary.md").write_text("# summary\n", encoding="utf-8")
    r = Rig("--proto", "v2",
            bridge_args=("--verification-dir", str(tmp_path / "verification")))
    try:
        body = _request(r, "GET", "/verify/reports")[1]
        assert body[0]["figures"] == []
    finally:
        r.close()


# -- extras: the "this is not luck" evidence ------------------------------


def test_serialize_carries_extras_through(tmp_path):
    """D19's permutation p-value lives in extras and never left the backend.

    build_report() has always computed it; the serializer simply did not
    list the key, and a whitelist that does not list a key drops it silently.
    That is the same shape as four other bugs in this file's history -- see
    the _LAST_GATE_NOTE comment.
    """
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "bs_extras", Path(__file__).resolve().parent / "bridge_server.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)

    report = {
        "is_synthetic": True,
        "session_paths": ["x.h5"],
        "matrix": [],
        "outcomes": [],
        "inconsistencies": [],
        "limitations": [],
        "blocking": [],
        "failed": [], "skipped": [], "errored": [],
        "extras": {"D19 消融": {"p_value": 0.004, "n_permutations": 200}},
    }
    out = bs.serialize_verify_report(report, "20260826_120000", tmp_path, 1.5)
    assert "extras" in out, "extras was dropped by the serializer"
    assert out["extras"]["D19 消融"]["p_value"] == 0.004


def test_serialize_tolerates_a_report_without_extras(tmp_path):
    """Older/partial reports must not blow up the response."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "bs_extras2", Path(__file__).resolve().parent / "bridge_server.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)

    report = {
        "is_synthetic": True, "session_paths": [], "matrix": [], "outcomes": [],
        "inconsistencies": [], "limitations": [], "blocking": [],
        "failed": [], "skipped": [], "errored": [],
    }
    out = bs.serialize_verify_report(report, "r", tmp_path, 0.1)
    assert out["extras"] == {}
