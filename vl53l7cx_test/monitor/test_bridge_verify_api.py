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
    assert _request(rig, "POST", "/trial/hold/stop")[1]["state"] == "REST"
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
