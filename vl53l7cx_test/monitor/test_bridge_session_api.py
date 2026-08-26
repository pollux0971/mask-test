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

    # The session writer holds the file open (HDF5 takes an exclusive
    # lock), so close the session before reading it back.
    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)

    sessions = list((rig.workdir / "sessions").glob("*.h5"))
    assert sessions, "no session file was written"
    newest = max(sessions, key=lambda p: p.stat().st_mtime)
    with h5py.File(newest, "r") as f:
        meta = dict(f["/meta"].attrs) if "meta" in f else dict(f.attrs)
    # A real burst fills these in; without one they stay at the -1 sentinel
    # and clock_sync_confirmed stays False. Either is acceptable -- what is
    # not acceptable is a plausible number with confirmed=True behind it.
    assert "clock_sync_confirmed" in meta
    # NOT asserted: that `source` reached the file. SessionWriter._write_meta
    # writes only REQUIRED_META_KEYS, so the key the bridge passes is dropped
    # silently -- recording the link source in the HDF5 needs a T02 schema
    # change, not a bridge change. It does reach the panel over SSE (see
    # test_status_declares_what_the_link_is_connected_to); the gap is that
    # the file itself cannot say what it was captured against.
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


# -- config passthrough (single source of truth) --------------------------


def test_vocab_is_served_from_the_real_config_file(rig):
    """C15 was keeping its own copy; a second copy always rots."""
    status, body = _request(rig, "GET", "/config/vocab")
    assert status == 200
    words = [w["text"] for w in body["words"]]
    on_disk = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "vocab.json").read_text())
    assert words == [w["text"] for w in on_disk["words"]]
    assert body.get("reject")


def test_quality_thresholds_are_served_too(rig):
    status, body = _request(rig, "GET", "/config/quality_thresholds")
    assert status == 200
    assert "drop_rate" in body and "green" in body["drop_rate"]


def test_session_targets_are_served_too(rig):
    status, body = _request(rig, "GET", "/config/session_targets")
    assert status == 200 and isinstance(body, dict)


def test_unknown_config_file_is_404_and_lists_what_exists(rig):
    status, body = _request(rig, "GET", "/config/nonsense")
    assert status == 404
    assert "vocab" in body["available"]


# -- sensors_seen: what actually arrived ----------------------------------


def test_sensors_seen_reports_both_on_a_healthy_board(rig):
    rig.read_events(2.0)
    status = _of_type(rig.read_events(1.5), "status")
    assert status
    assert status[-1]["sensors_seen"] == "AB"


def test_sensors_seen_catches_a_board_with_one_silent_sensor():
    """The first real board's failure, reproduced at the derivation.

    Sensor A failed is_alive and never streamed; sensor B did -- but the
    frames it emitted were labelled `A`. From the host's side the difference
    is invisible by every other route: the failure is announced over
    ESP_LOGE (not a $ line, never parsed), and $H's drop_A/drop_B both stay
    at zero because a sensor nobody reads never fails a read. Counting
    labels on the wire is the only signal there is.

    Driven directly rather than through the mock, because silencing one of
    the mock's sensors needs the /sensor endpoint (B18), which is not wired
    yet -- and the thing worth pinning down is the derivation, not the
    device's ability to play dead.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bs_seen", Path(__file__).resolve().parent / "bridge_server.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)

    assert bs.sensors_seen_string() == "", "nothing received yet"

    for seq in range(5):
        bs.drop_tracker.observe("tof_A", seq)
    assert bs.sensors_seen_string() == "A", (
        "one stream arrived, labelled A -- note this does NOT mean the "
        "physical sensor A is alive; on the real board it was B emitting "
        "frames labelled A"
    )

    for seq in range(5):
        bs.drop_tracker.observe("tof_B", seq)
    assert bs.sensors_seen_string() == "AB"


def test_sensors_seen_only_counts_the_current_session():
    """A previous session's frames must not make a dead sensor look alive."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bs_session", Path(__file__).resolve().parent / "bridge_server.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)

    for seq in range(5):
        bs.drop_tracker.observe("tof_A", seq)
        bs.drop_tracker.observe("tof_B", seq)
    assert bs.sensors_seen_string() == "AB"

    # A new session starts here; only A streams from now on.
    baseline = bs.snapshot_frame_counts()
    for seq in range(5, 10):
        bs.drop_tracker.observe("tof_A", seq)

    assert bs.sensors_seen_string(baseline) == "A", (
        "frames from before the session started leaked into the count"
    )
    assert bs.sensors_seen_string() == "AB"   # since-boot view is unchanged


def test_sensors_seen_is_empty_not_missing_when_nothing_arrives(rig):
    """Nothing on the wire yet must read as "", not as an absent field.

    An absent field means "we never recorded this"; "" means "we recorded
    it, and the answer was none". A session where no ToF data arrived at
    all is the single most alarming case, so it must not be spelled the
    same way as an old file that predates the field.
    """
    status = _of_type(rig.read_events(0.3), "status")
    assert status
    assert "sensors_seen" in status[0], "the field must always be present"
    assert isinstance(status[0]["sensors_seen"], str)


def test_quality_event_carries_sensors_seen(rig):
    rig.read_events(2.0)
    quality = _of_type(rig.read_events(1.5), "quality")
    assert quality
    assert quality[-1]["sensors_seen"] == "AB"


def test_sensors_seen_is_recorded_in_the_session_meta(rig):
    import h5py
    rig.read_events(3.5)
    _request(rig, "POST", "/session/start", VALID_METADATA)
    status, body = _request(rig, "POST", "/session/baseline?seconds=2")
    if status != 200:
        pytest.skip(f"baseline gate rejected the synthetic scene: {body.get('reason')}")
    assert _request(rig, "POST", "/session/end")[0] == 200
    time.sleep(0.5)

    files = list((rig.workdir / "sessions").glob("*.h5"))
    with h5py.File(files[0], "r") as f:
        meta = dict(f["/meta"].attrs)
    seen = meta.get("sensors_seen")
    if seen is None:
        pytest.skip("sensors_seen is not in the schema yet (esp-mask-test-18)")
    if isinstance(seen, bytes):
        seen = seen.decode()
    assert seen == "AB", (
        f"both sensors streamed but /meta recorded {seen!r} -- the count "
        f"must include the buffered frames the baseline was computed from"
    )
