"""B19 tests: threshold classification, each metric, and the transport alarm."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from host.capture.dropwatch import DropTracker
from host.quality.metrics import QualityAggregator, ThresholdTable

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_THRESHOLDS = REPO_ROOT / "config" / "quality_thresholds.json"


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


def write_thresholds(path, table):
    path.write_text(json.dumps(table), encoding="utf-8")
    return path


@pytest.fixture
def thresholds(tmp_path):
    return ThresholdTable(write_thresholds(tmp_path / "th.json", {
        "drop_rate": {"direction": "lower_better", "green": 0.01, "yellow": 0.05,
                      "yellow_hint": "y-drop", "red_hint": "r-drop"},
        "valid_zones": {"direction": "higher_better", "green": 0.8, "yellow": 0.5,
                        "yellow_hint": "y-zones", "red_hint": "r-zones"},
        "symmetry": {"direction": "lower_better", "green": 0.15, "yellow": 0.3,
                     "yellow_hint": "y-sym", "red_hint": "r-sym"},
        "clock_resid": {"direction": "lower_better", "green": 0.005, "yellow": 0.01},
        "noise_floor": {"direction": "lower_better", "green": 300, "yellow": 1000},
        "bandwidth": {"direction": "lower_better", "green": 0.6, "yellow": 0.8},
    }))


def tof_event(sensor, seq, valid, distance, dim=16, t_us=0):
    return {"type": "tof", "sensor": sensor, "seq": seq, "t_us": t_us,
            "has_timestamp": True, "dim": dim, "valid": valid,
            "distance": distance, "n_valid": sum(valid)}


# -- threshold table ----------------------------------------------------


def test_lower_better_bands(thresholds):
    assert thresholds.classify("drop_rate", 0.005)[0] == "green"
    assert thresholds.classify("drop_rate", 0.01)[0] == "green"   # boundary inclusive
    assert thresholds.classify("drop_rate", 0.03)[0] == "yellow"
    assert thresholds.classify("drop_rate", 0.2)[0] == "red"


def test_higher_better_bands(thresholds):
    assert thresholds.classify("valid_zones", 0.95)[0] == "green"
    assert thresholds.classify("valid_zones", 0.8)[0] == "green"
    assert thresholds.classify("valid_zones", 0.6)[0] == "yellow"
    assert thresholds.classify("valid_zones", 0.1)[0] == "red"


def test_hint_only_when_not_green(thresholds):
    assert thresholds.classify("drop_rate", 0.005)[1] is None
    assert thresholds.classify("drop_rate", 0.03)[1] == "y-drop"
    assert thresholds.classify("drop_rate", 0.2)[1] == "r-drop"


def test_unconfigured_metric_is_unknown_not_green(tmp_path):
    """A metric nobody set a threshold for is not a metric that is passing."""
    t = ThresholdTable(write_thresholds(tmp_path / "th.json", {
        "drop_rate": {"green": None, "yellow": None},
    }))
    assert t.classify("drop_rate", 0.0) == ("unknown", None)
    assert t.classify("never_heard_of_it", 1.0) == ("unknown", None)


def test_missing_value_is_unknown(thresholds):
    assert thresholds.classify("drop_rate", None) == ("unknown", None)


def test_thresholds_reload_when_the_file_changes(tmp_path):
    path = write_thresholds(tmp_path / "th.json", {
        "drop_rate": {"direction": "lower_better", "green": 0.01, "yellow": 0.05},
    })
    t = ThresholdTable(path)
    assert t.classify("drop_rate", 0.03)[0] == "yellow"

    time.sleep(0.01)  # ensure a distinct mtime
    write_thresholds(path, {
        "drop_rate": {"direction": "lower_better", "green": 0.5, "yellow": 0.9},
    })
    t.reload()
    assert t.classify("drop_rate", 0.03)[0] == "green"


def test_broken_json_keeps_the_previous_table(tmp_path):
    """Catching the editor mid-save must not blank the whole dashboard."""
    path = write_thresholds(tmp_path / "th.json", {
        "drop_rate": {"direction": "lower_better", "green": 0.01, "yellow": 0.05},
    })
    t = ThresholdTable(path)
    time.sleep(0.01)
    path.write_text('{"drop_rate": {"green": 0.0', encoding="utf-8")  # truncated
    t.reload()
    assert t.classify("drop_rate", 0.03)[0] == "yellow"  # still the old table
    assert t.load_error is not None


def test_shipped_config_is_complete_and_usable():
    """The file the bridge actually loads must classify every metric."""
    from host.quality.metrics import METRIC_ORDER
    t = ThresholdTable(SHIPPED_THRESHOLDS)
    assert t.load_error is None
    for metric in METRIC_ORDER:
        spec = t.spec(metric)
        assert spec is not None, f"{metric} has no usable thresholds"
        assert spec.get("direction") in ("lower_better", "higher_better")
        assert spec.get("yellow_hint") and spec.get("red_hint"), (
            f"{metric} is missing an actionable hint"
        )


def test_shipped_config_matches_the_story_example():
    """B19's worked example: 0.21 symmetry is yellow, 0.0031 resid is green."""
    t = ThresholdTable(SHIPPED_THRESHOLDS)
    assert t.classify("symmetry", 0.21)[0] == "yellow"
    assert t.classify("clock_resid", 0.0031)[0] == "green"
    assert t.classify("drop_rate", 0.003)[0] == "green"
    assert t.classify("valid_zones", 0.94)[0] == "green"
    assert t.classify("noise_floor", 142)[0] == "green"
    assert t.classify("bandwidth", 0.37)[0] == "green"


def test_shipped_config_bandwidth_matches_contract_budget():
    """CONTRACTS #1.4: 54% is a working config, 70% is the one to watch."""
    t = ThresholdTable(SHIPPED_THRESHOLDS)
    assert t.classify("bandwidth", 0.54)[0] == "green"   # 4x4 @30Hz + Mel 256
    assert t.classify("bandwidth", 0.70)[0] == "yellow"  # 8x8 @10Hz + Mel 256
    assert t.classify("bandwidth", 0.92)[0] == "red"     # recording dump


def test_shipped_config_clock_resid_uses_b04_bound():
    t = ThresholdTable(SHIPPED_THRESHOLDS)
    assert t.classify("clock_resid", 0.005)[0] == "green"   # exactly B04's bound
    assert t.classify("clock_resid", 0.006)[0] == "yellow"


# -- metric computation -------------------------------------------------


def test_valid_zone_ratio(thresholds):
    agg = QualityAggregator(thresholds, clock=FakeClock())
    agg.observe_tof(tof_event("A", 0, [True] * 12 + [False] * 4, [100] * 16))
    agg.observe_tof(tof_event("B", 0, [True] * 16, [100] * 16))
    m = agg.snapshot()["metrics"]
    assert m["valid_zones"]["value"] == pytest.approx(28 / 32)
    assert m["valid_zones"]["level"] == "green"


def test_symmetry_is_relative_not_absolute(thresholds):
    agg = QualityAggregator(thresholds, clock=FakeClock())
    agg.observe_tof(tof_event("A", 0, [True] * 16, [100] * 16))
    agg.observe_tof(tof_event("B", 0, [True] * 16, [120] * 16))
    # |100-120| / 110 = 0.1818
    assert agg.snapshot()["metrics"]["symmetry"]["value"] == pytest.approx(20 / 110)


def test_symmetry_needs_both_sensors(thresholds):
    agg = QualityAggregator(thresholds, clock=FakeClock())
    agg.observe_tof(tof_event("A", 0, [True] * 16, [100] * 16))
    m = agg.snapshot()["metrics"]["symmetry"]
    assert m["value"] is None and m["level"] == "unknown"


def test_symmetry_ignores_invalid_zones(thresholds):
    """A -1 zone must not be averaged in as a distance."""
    agg = QualityAggregator(thresholds, clock=FakeClock())
    agg.observe_tof(tof_event("A", 0, [True] * 8 + [False] * 8,
                              [100] * 8 + [None] * 8))
    agg.observe_tof(tof_event("B", 0, [True] * 16, [100] * 16))
    assert agg.snapshot()["metrics"]["symmetry"]["value"] == pytest.approx(0.0)


def test_noise_floor_is_a_low_percentile_not_the_mean(thresholds):
    """Speech bursts must not raise the reported floor."""
    agg = QualityAggregator(thresholds, clock=FakeClock())
    for rms in [100] * 9 + [20000]:  # quiet, with one shout
        agg.observe_mic({"type": "mic", "rms": rms, "peak": rms, "t_us": 0})
    assert agg.snapshot()["metrics"]["noise_floor"]["value"] == 100


def test_bandwidth_is_a_fraction_of_link_capacity(thresholds):
    clock = FakeClock()
    agg = QualityAggregator(thresholds, baud=460800, clock=clock)
    agg.note_bytes(0)                 # t=0, opens the window
    clock.advance(1.0)
    agg.note_bytes(23040)             # half of 46080 B/s
    assert agg.snapshot()["metrics"]["bandwidth"]["value"] == pytest.approx(0.5)


def test_metrics_with_no_data_are_unknown(thresholds):
    agg = QualityAggregator(thresholds, clock=FakeClock())
    metrics = agg.snapshot()["metrics"]
    from host.quality.metrics import METRIC_ORDER
    assert set(metrics) == set(METRIC_ORDER)
    for name, entry in metrics.items():
        assert entry["value"] is None, name
        assert entry["level"] == "unknown", name
        assert "hint" not in entry, name


def test_window_forgets_old_observations(thresholds):
    clock = FakeClock()
    agg = QualityAggregator(thresholds, window_s=30.0, clock=clock)
    agg.observe_tof(tof_event("A", 0, [False] * 16, [None] * 16))
    assert agg.snapshot()["metrics"]["valid_zones"]["value"] == 0.0
    clock.advance(31.0)
    agg.observe_tof(tof_event("A", 1, [True] * 16, [100] * 16))
    assert agg.snapshot()["metrics"]["valid_zones"]["value"] == 1.0


def test_drop_rate_comes_from_the_tracker(thresholds):
    tracker = DropTracker()
    tracker.observe("tof_A", 0)
    tracker.observe("tof_A", 10)  # 9 of 11 lost
    agg = QualityAggregator(thresholds, drop_tracker=tracker, clock=FakeClock())
    m = agg.snapshot()["metrics"]["drop_rate"]
    assert m["value"] == pytest.approx(9 / 11)
    assert m["level"] == "red"
    assert m["hint"] == "r-drop"


# -- the transport alarm (B03's conclusion) -----------------------------


def test_host_ahead_of_device_raises_a_transport_alarm(thresholds):
    """delta > 0 is a fault signal, not an error bar."""
    tracker = DropTracker()
    for seq in range(100):
        tracker.observe("tof_A", seq)  # host saw everything...
    tracker.observe("tof_A", 110)      # ...then 100-109 go missing
    agg = QualityAggregator(thresholds, drop_tracker=tracker, clock=FakeClock())
    agg.observe_heartbeat({"type": "heartbeat", "drop_A": 0, "drop_B": 0, "drop_M": 0})

    event = agg.snapshot()
    assert event["alarms"], "host counted drops the device denies; no alarm raised"
    alarm = event["alarms"][0]
    assert alarm["stream"] == "tof_A" and alarm["delta"] == 10
    assert event["metrics"]["drop_rate"]["level"] == "red"
    assert "傳輸" in event["metrics"]["drop_rate"]["hint"]


def test_host_behind_device_is_not_an_alarm(thresholds):
    """The normal case: the host trails by the trailing drop run (B03)."""
    tracker = DropTracker()
    for seq in range(100):
        tracker.observe("tof_A", seq)
    agg = QualityAggregator(thresholds, drop_tracker=tracker, clock=FakeClock())
    agg.observe_heartbeat({"type": "heartbeat", "drop_A": 3, "drop_B": 0, "drop_M": 0})
    assert "alarms" not in agg.snapshot()


def test_no_alarm_before_any_heartbeat(thresholds):
    tracker = DropTracker()
    tracker.observe("tof_A", 0)
    tracker.observe("tof_A", 50)
    agg = QualityAggregator(thresholds, drop_tracker=tracker, clock=FakeClock())
    assert "alarms" not in agg.snapshot()


# -- clock residual -----------------------------------------------------


def test_clock_resid_needs_enough_buckets(thresholds):
    from host.clock.align import ClockAligner
    agg = QualityAggregator(thresholds, clock_aligner=ClockAligner(), clock=FakeClock())
    agg.observe_mic({"type": "mic", "rms": 10, "t_us": 0, "has_timestamp": True})
    assert agg.snapshot()["metrics"]["clock_resid"]["value"] is None


def test_clock_resid_reported_in_seconds(thresholds):
    from host.clock.align import ClockAligner
    clock = FakeClock(0.0)
    aligner = ClockAligner()
    agg = QualityAggregator(thresholds, clock_aligner=aligner, clock=clock)
    # A clean, perfectly linear link: device and host tick together with a
    # fixed offset, so the residual should be ~0.
    for i in range(20):
        clock.t = float(i)
        agg.observe_mic({"type": "mic", "rms": 10, "has_timestamp": True,
                         "t_us": int(i * 1e6) + 12345})
    value = agg.snapshot()["metrics"]["clock_resid"]["value"]
    assert value is not None
    assert value < 0.001
    assert agg.snapshot()["metrics"]["clock_resid"]["level"] == "green"


def test_events_without_timestamps_are_not_fed_to_the_aligner(thresholds):
    """v1 lines carry no t_us; feeding them would poison the fit."""
    from host.clock.align import ClockAligner
    aligner = ClockAligner()
    agg = QualityAggregator(thresholds, clock_aligner=aligner, clock=FakeClock())
    for _ in range(10):
        agg.observe_mic({"type": "mic", "rms": 10, "has_timestamp": False})
    assert aligner.n_buckets == 0


# -- event shape --------------------------------------------------------


def test_event_matches_the_contract_shape(thresholds):
    agg = QualityAggregator(thresholds, clock=FakeClock())
    agg.observe_tof(tof_event("A", 0, [True] * 16, [100] * 16))
    event = agg.snapshot()
    assert event["type"] == "quality"
    assert isinstance(event["t"], float)
    for entry in event["metrics"].values():
        assert set(entry) <= {"value", "level", "hint"}
        assert entry["level"] in ("green", "yellow", "red", "unknown")
