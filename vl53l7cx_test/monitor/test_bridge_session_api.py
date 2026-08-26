"""HTTP wiring for B09 (session), B10 (baseline) and C10's PCA endpoint.

Driven against a running bridge with a real mock device attached, because
the parts most likely to be wrong here are the seams -- status codes, the
shape the panel destructures, and whether the buffered device data is
actually there when the baseline request arrives.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from test_bridge_sse import Rig, _of_type


def _request(rig, method, path, body=None):
    """Returns (status, parsed json or None). 4xx/5xx come back, not raise."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{rig.http_port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


VALID_METADATA = {
    "subject": "s01", "mode": "quiz", "distance_mm": 30.0,
    "angle_deg": 0.0, "ambient": "quiet room", "notes": "",
}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig("--proto", "v2")
    try:
        yield r
    finally:
        r.close()


# -- session lifecycle ---------------------------------------------------


def test_current_is_204_before_any_session(rig):
    status, body = _request(rig, "GET", "/session/current")
    assert status == 204 and body is None


def test_start_returns_the_session_info(rig):
    status, body = _request(rig, "POST", "/session/start", VALID_METADATA)
    assert status == 200, body
    assert body["subject"] == "s01"
    assert body["mode"] == "quiz"
    assert body["session_id"]
    assert body["started_at"]
    assert body["baseline_done"] is False


def test_missing_fields_are_named_in_the_400(rig):
    """The form needs to know which inputs to highlight, not just that it failed."""
    bad = {k: v for k, v in VALID_METADATA.items() if k not in ("distance_mm", "ambient")}
    status, body = _request(rig, "POST", "/session/start", bad)
    assert status == 400
    assert set(body["missing"]) == {"distance_mm", "ambient"}
    assert "distance_mm" in body["error"] and "ambient" in body["error"]


def test_zero_is_a_valid_value_not_a_missing_field(rig):
    """distance_mm/angle_deg of 0 must not be read as absent."""
    status, body = _request(rig, "POST", "/session/start",
                            {**VALID_METADATA, "distance_mm": 0.0, "angle_deg": 0.0})
    assert status == 200, body
    assert body["distance_mm"] == 0.0 and body["angle_deg"] == 0.0


def test_second_start_is_409(rig):
    assert _request(rig, "POST", "/session/start", VALID_METADATA)[0] == 200
    status, body = _request(rig, "POST", "/session/start", VALID_METADATA)
    assert status == 409
    assert body["session_id"]


def test_current_returns_the_session_after_start(rig):
    _request(rig, "POST", "/session/start", VALID_METADATA)
    status, body = _request(rig, "GET", "/session/current")
    assert status == 200
    assert body["subject"] == "s01"


def test_end_returns_the_session_and_clears_current(rig):
    _request(rig, "POST", "/session/start", VALID_METADATA)
    status, body = _request(rig, "POST", "/session/end")
    assert status == 200 and body["subject"] == "s01"
    assert _request(rig, "GET", "/session/current")[0] == 204


def test_end_without_a_session_is_409(rig):
    status, body = _request(rig, "POST", "/session/end")
    assert status == 409 and body["error"]


def test_prefill_is_always_200(rig):
    """Even with no history: the form asks for it before anything exists."""
    status, body = _request(rig, "GET", "/session/prefill")
    assert status == 200 and isinstance(body, dict)


def test_prefill_increments_wear_id(rig):
    _request(rig, "POST", "/session/start", {**VALID_METADATA, "wear_id": 7})
    _request(rig, "POST", "/session/end")
    status, body = _request(rig, "GET", "/session/prefill")
    assert status == 200
    assert body["wear_id"] == 8
    assert body["subject"] == "s01"


def test_target_check_is_not_configured_when_targets_are_null(rig):
    """config/session_targets.json is all null until E01 measures it.

    The rule this pins down: with no target geometry there is no basis for
    a deviation warning, so none is invented. A fabricated "distance normal"
    is worse than no check at all, because the operator would trust it.
    """
    status, body = _request(rig, "POST", "/session/start", VALID_METADATA)
    assert status == 200
    assert body["target_check"] == "not_configured"
    assert body["warnings"] == []
    assert body["note"]  # says why


def test_a_tab_opened_mid_session_is_told_about_it(rig):
    """C11/C12 follow the session over SSE rather than polling.

    The broadcast at start only reaches tabs that were already connected, so
    the opening snapshot has to carry the current session too -- otherwise a
    panel opened during a session shows an idle UI over a live recording.
    """
    _request(rig, "POST", "/session/start", VALID_METADATA)
    sessions = _of_type(rig.read_events(1.5), "session")
    assert sessions, "a tab opened mid-session was not told a session is running"
    assert sessions[-1]["state"] == "started"
    assert sessions[-1]["session"]["subject"] == "s01"


def test_session_end_is_broadcast_to_connected_tabs(rig):
    """The live fan-out path, as opposed to the opening snapshot."""
    import threading
    _request(rig, "POST", "/session/start", VALID_METADATA)
    collected = []

    def watch():
        collected.extend(rig.read_events(3.0))

    t = threading.Thread(target=watch)
    t.start()
    time.sleep(1.0)
    _request(rig, "POST", "/session/end")
    t.join()

    states = [e["state"] for e in _of_type(collected, "session")]
    assert "ended" in states, f"only saw {states}"


# -- baseline ------------------------------------------------------------


def test_baseline_before_a_session_is_409(rig):
    status, body = _request(rig, "POST", "/session/baseline?seconds=2")
    assert status == 409 and "session" in body["error"]


def test_get_baseline_is_204_before_capture(rig):
    assert _request(rig, "GET", "/baseline")[0] == 204


def test_baseline_capture_produces_the_shape_c06_draws(rig):
    rig.read_events(3.0)  # let the aligner fill with device data
    _request(rig, "POST", "/session/start", VALID_METADATA)
    status, body = _request(rig, "POST", "/session/baseline?seconds=2")
    assert status in (200, 422), body

    assert body["source"] == "session"
    for key in ("mu_A", "sigma_A", "mu_B", "sigma_B"):
        assert key in body
    # The three flags C06 renders as distinct outlines, per sensor.
    for key in ("unstable_zones", "no_signal_zones", "suspect_zero_variance_zones"):
        assert set(body[key]) == {"A", "B"}, key
        assert isinstance(body[key]["A"], list)

    if status == 200:
        assert body["ok"] is True
        assert len(body["mu_A"]) == 32
        # A captured baseline unblocks trials; a rejected one must not.
        assert _request(rig, "GET", "/session/current")[1]["baseline_done"] is True
    else:
        assert body["ok"] is False and body["reason"]
        assert _request(rig, "GET", "/session/current")[1]["baseline_done"] is False


def test_baseline_json_never_contains_a_bare_nan(rig):
    """NaN is not valid JSON; a single one would break the whole response.

    A no-signal zone legitimately has a NaN mean, so it crosses the wire as
    null and the zone-flag arrays say why. The check here is simply that the
    body parses at all -- urllib + json.loads would have raised otherwise --
    plus that nothing quietly became 0.0.
    """
    rig.read_events(3.0)
    _request(rig, "POST", "/session/start", VALID_METADATA)
    status, body = _request(rig, "POST", "/session/baseline?seconds=2")
    assert status in (200, 422)
    raw = json.dumps(body)
    assert "NaN" not in raw and "Infinity" not in raw
    for key in ("mu_A", "mu_B"):
        for zone, value in enumerate(body[key] or []):
            assert value is None or isinstance(value, (int, float)), (key, zone)


def test_baseline_without_buffered_data_explains_itself(rig):
    """Asking immediately, before any device data has been buffered."""
    _request(rig, "POST", "/session/start", VALID_METADATA)
    status, body = _request(rig, "POST", "/session/baseline?seconds=120")
    if status == 409:
        assert "資料" in body["error"] or "幀" in body["error"]


# -- PCA ------------------------------------------------------------------


def test_pca_is_204_when_no_model_has_been_fitted(rig):
    """C10 keeps its own stub and retries; a fabricated model would be worse."""
    status, body = _request(rig, "GET", "/pca?model=tof_only")
    assert status == 204 and body is None


def test_pca_rejects_an_unknown_model_name(rig):
    status, body = _request(rig, "GET", "/pca?model=nonsense")
    assert status == 400 and body["error"]


# -- B05: PING clock sync -------------------------------------------------


def test_session_start_triggers_a_ping_burst(rig):
    """The burst runs on the reader thread, so /session/start returns at once.

    Its result is not needed until the baseline is captured, which is a good
    thirty seconds later -- holding the request open for the ~2 s a burst can
    take would be paying latency for nothing.
    """
    import threading
    collected = []

    def watch():
        collected.extend(rig.read_events(6.0))

    t = threading.Thread(target=watch)
    t.start()
    time.sleep(0.5)
    status, _ = _request(rig, "POST", "/session/start", VALID_METADATA)
    assert status == 200
    t.join()

    syncs = _of_type(collected, "clock_sync")
    assert syncs, "no clock_sync event; the PING burst never ran"
    burst = syncs[0]
    assert burst["label"] == "session_start"
    assert burst["n_attempts"] > 0
    assert burst["n_ok"] > 0, "the mock answered no PINGs at all"
    assert isinstance(burst["confirmed"], bool)


def test_ping_burst_does_not_punch_a_hole_in_the_data_stream(rig):
    """PingSyncer reads lines while waiting; those must still reach the panel.

    Without on_event routing them back, every $T that arrived during the
    burst would be swallowed -- and B03 would report it as a burst of drops
    at the exact moment a session starts.
    """
    import threading
    collected = []

    def watch():
        collected.extend(rig.read_events(6.0))

    t = threading.Thread(target=watch)
    t.start()
    time.sleep(0.5)
    _request(rig, "POST", "/session/start", VALID_METADATA)
    t.join()

    assert _of_type(collected, "clock_sync"), "burst did not run"
    quality = _of_type(collected, "quality")
    assert quality
    last = quality[-1]
    assert last.get("alarms", []) == [], (
        f"the clock sync lost frames the device says it sent: {last.get('alarms')}"
    )


def test_session_end_triggers_the_closing_burst(rig):
    import threading
    collected = []

    def watch():
        collected.extend(rig.read_events(8.0))

    t = threading.Thread(target=watch)
    t.start()
    time.sleep(0.5)
    _request(rig, "POST", "/session/start", VALID_METADATA)
    time.sleep(3.0)
    _request(rig, "POST", "/session/end")
    t.join()

    labels = [e["label"] for e in _of_type(collected, "clock_sync")]
    assert "session_start" in labels
    assert "session_end" in labels, f"only saw {labels}"


def test_baseline_meta_carries_the_measured_clock_block(rig):
    """The session written to HDF5 must record a real sync, or admit it did not."""
    import h5py
    rig.read_events(4.0)
    _request(rig, "POST", "/session/start", VALID_METADATA)
    time.sleep(2.5)  # let the burst finish
    status, body = _request(rig, "POST", "/session/baseline?seconds=2")
    if status != 200:
        pytest.skip(f"baseline quality gate rejected the synthetic scene: {body.get('reason')}")

    sessions = list((rig.workdir / "sessions").glob("*.h5"))
    assert sessions, "no session file was written"
    newest = max(sessions, key=lambda p: p.stat().st_mtime)
    with h5py.File(newest, "r") as f:
        meta = dict(f["/meta"].attrs) if "meta" in f else dict(f.attrs)
    # A real burst fills these in; without one they stay at the -1 sentinel
    # and clock_sync_confirmed stays False. Either is acceptable -- what is
    # not acceptable is a plausible number with confirmed=True behind it.
    assert "clock_sync_confirmed" in meta
    if meta["session_start_rtt_min_us"] != -1:
        assert meta["session_start_device_us"] != -1
        assert meta["session_start_rtt_min_us"] >= 0
    else:
        assert not meta["clock_sync_confirmed"]

    # B04's regression must be plausible. This is the guard for the two-host
    # -clock bug: the aligner was being fed monotonic timestamps while B05
    # fed it wall-clock ones, and the fit read the gap between the two epochs
    # as slope. It produced clock_slope = 5.9e7 and a residual of 1e15 us --
    # numbers that are obviously wrong once seen, and completely invisible
    # while only one of the two sources was wired up.
    if meta["clock_residual_p95"] != -1:
        assert 0.9 < meta["clock_slope"] < 1.1, meta["clock_slope"]
        assert meta["clock_residual_p95"] < 5000, (
            f"residual {meta['clock_residual_p95']} us exceeds B04's 5 ms bound"
        )
