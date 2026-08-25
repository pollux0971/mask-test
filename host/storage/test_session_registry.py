import json

import pytest
from datetime import datetime

from host.storage.session_registry import (
    MissingFieldsError,
    NoActiveSessionError,
    SessionAlreadyActiveError,
    SessionRegistry,
    load_session_targets,
)

# B10 補的測試都放在檔案最下面，見 "baseline_done" 區段。

NOT_CONFIGURED = {
    "target_distance_mm": None, "target_angle_deg": None,
    "tolerance_distance_mm": None, "tolerance_angle_deg": None,
}

CONFIGURED = {
    "target_distance_mm": 30.0, "target_angle_deg": 0.0,
    "tolerance_distance_mm": 5.0, "tolerance_angle_deg": 10.0,
}


def _valid_metadata(**overrides):
    metadata = {
        "subject": "s01", "mode": "quiz", "distance_mm": 30.0,
        "angle_deg": 0.0, "ambient": "quiet room",
    }
    metadata.update(overrides)
    return metadata


def _registry(tmp_path, targets=NOT_CONFIGURED):
    return SessionRegistry(tmp_path / "last_session.json", session_targets=targets)


# ---------------------------------------------------------------------------
# 驗收條件：缺必填欄位回 400（這裡是丟一個能對應 400 的例外）且訊息指名欄位


def test_missing_field_raises_with_field_name(tmp_path):
    reg = _registry(tmp_path)
    incomplete = _valid_metadata()
    del incomplete["distance_mm"]

    with pytest.raises(MissingFieldsError) as exc_info:
        reg.start(incomplete)

    assert exc_info.value.fields == ["distance_mm"]
    assert "distance_mm" in str(exc_info.value)


def test_missing_multiple_fields_names_all_of_them(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(MissingFieldsError) as exc_info:
        reg.start({"subject": "s01"})

    assert set(exc_info.value.fields) == {"mode", "distance_mm", "angle_deg", "ambient"}


def test_falsy_but_present_values_are_not_treated_as_missing(tmp_path):
    """`distance_mm=0` 或 `angle_deg=0` 是合法值，不該被 `if not value` 這種
    寫法誤判成缺欄位。"""
    reg = _registry(tmp_path)
    info = reg.start(_valid_metadata(distance_mm=0, angle_deg=0))
    assert info.distance_mm == 0
    assert info.angle_deg == 0


def test_missing_wear_id_is_not_a_validation_error(tmp_path):
    """wear_id 會自動遞增，沒填不算漏填。"""
    reg = _registry(tmp_path)
    info = reg.start(_valid_metadata())
    assert info.wear_id == 1


# ---------------------------------------------------------------------------
# 驗收條件：GET /session/current 在未開始時回 204（這裡是 current 為 None）


def test_current_is_none_before_any_session_starts(tmp_path):
    reg = _registry(tmp_path)
    assert reg.current is None


def test_current_reflects_active_session(tmp_path):
    reg = _registry(tmp_path)
    info = reg.start(_valid_metadata())
    assert reg.current is info


def test_current_is_none_again_after_end(tmp_path):
    reg = _registry(tmp_path)
    reg.start(_valid_metadata())
    reg.end()
    assert reg.current is None


def test_end_without_active_session_raises(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(NoActiveSessionError):
        reg.end()


# ---------------------------------------------------------------------------
# 驗收條件：重複 start 回 409


def test_starting_twice_without_end_raises(tmp_path):
    reg = _registry(tmp_path)
    reg.start(_valid_metadata())
    with pytest.raises(SessionAlreadyActiveError):
        reg.start(_valid_metadata())


def test_can_start_again_after_end(tmp_path):
    reg = _registry(tmp_path)
    reg.start(_valid_metadata())
    reg.end()
    info = reg.start(_valid_metadata())  # 不應該 raise
    assert info.session_id


# ---------------------------------------------------------------------------
# 驗收條件：上次設定正確預填


def test_prefill_is_empty_with_no_history(tmp_path):
    reg = _registry(tmp_path)
    assert reg.get_prefill() == {}


def test_prefill_uses_last_session_with_wear_id_plus_one(tmp_path):
    reg = _registry(tmp_path)
    reg.start(_valid_metadata(wear_id=3))

    prefill = reg.get_prefill()

    assert prefill["wear_id"] == 4
    assert prefill["subject"] == "s01"
    assert prefill["distance_mm"] == 30.0


def test_prefill_survives_registry_restart(tmp_path):
    """模擬 bridge_server 重啟：新的 SessionRegistry 指到同一個
    last_session.json，還是要讀得到預填。"""
    last_path = tmp_path / "last_session.json"
    reg1 = SessionRegistry(last_path, session_targets=NOT_CONFIGURED)
    reg1.start(_valid_metadata(wear_id=5))

    reg2 = SessionRegistry(last_path, session_targets=NOT_CONFIGURED)
    prefill = reg2.get_prefill()

    assert prefill["wear_id"] == 6


def test_last_session_json_is_valid_json_on_disk(tmp_path):
    last_path = tmp_path / "last_session.json"
    reg = SessionRegistry(last_path, session_targets=NOT_CONFIGURED)
    reg.start(_valid_metadata(wear_id=2))

    on_disk = json.loads(last_path.read_text())
    assert on_disk["wear_id"] == 2
    assert on_disk["subject"] == "s01"


# ---------------------------------------------------------------------------
# wear_id 自動遞增但可覆寫（story 核心難點）


def test_wear_id_defaults_to_one_with_no_history(tmp_path):
    reg = _registry(tmp_path)
    info = reg.start(_valid_metadata())
    assert info.wear_id == 1


def test_wear_id_auto_increments_from_last_session(tmp_path):
    reg = _registry(tmp_path)
    reg.start(_valid_metadata(wear_id=3))
    reg.end()

    info = reg.start(_valid_metadata())  # 沒給 wear_id -> 自動 +1

    assert info.wear_id == 4


def test_wear_id_can_be_overridden_to_same_value_for_same_donning(tmp_path):
    """「同次戴上繼續錄」：明確傳入跟上次一樣的 wear_id，不能被自動 +1 蓋掉。"""
    reg = _registry(tmp_path)
    reg.start(_valid_metadata(wear_id=3))
    reg.end()

    info = reg.start(_valid_metadata(wear_id=3))  # 明確覆寫成同一個值

    assert info.wear_id == 3


def test_wear_id_zero_is_a_valid_explicit_override(tmp_path):
    reg = _registry(tmp_path)
    info = reg.start(_valid_metadata(wear_id=0))
    assert info.wear_id == 0


# ---------------------------------------------------------------------------
# target_check / warnings：未設定時不捏造警告


def test_warnings_empty_and_not_configured_when_targets_are_all_null(tmp_path):
    reg = _registry(tmp_path, targets=NOT_CONFIGURED)
    info = reg.start(_valid_metadata(distance_mm=9999.0, angle_deg=9999.0))  # 誇張偏離也一樣

    assert info.warnings == []
    assert info.target_check == "not_configured"
    assert info.note


def test_warnings_generated_when_targets_configured_and_exceeded(tmp_path):
    reg = _registry(tmp_path, targets=CONFIGURED)
    info = reg.start(_valid_metadata(distance_mm=47.0, angle_deg=0.0))  # 偏離 17mm > 5mm 容許

    assert info.target_check == "warning"
    assert any("距離" in w for w in info.warnings)
    assert not any("角度" in w for w in info.warnings)


def test_target_check_ok_when_within_tolerance(tmp_path):
    reg = _registry(tmp_path, targets=CONFIGURED)
    info = reg.start(_valid_metadata(distance_mm=32.0, angle_deg=2.0))  # 都在容許範圍內

    assert info.target_check == "ok"
    assert info.warnings == []


def test_partial_configuration_only_checks_configured_axis(tmp_path):
    targets = dict(CONFIGURED)
    targets["target_angle_deg"] = None
    targets["tolerance_angle_deg"] = None
    reg = _registry(tmp_path, targets=targets)

    info = reg.start(_valid_metadata(distance_mm=30.0, angle_deg=9999.0))  # 角度沒目標，不該警告

    assert info.warnings == []
    assert info.target_check == "ok"
    assert "角度" in info.note


# ---------------------------------------------------------------------------
# load_session_targets：讀真正的 config 檔


def test_load_session_targets_missing_file_is_all_none(tmp_path):
    targets = load_session_targets(tmp_path / "does_not_exist.json")
    assert all(v is None for v in targets.values())


def test_load_session_targets_reads_real_config_file():
    """讀專案裡真正的 `config/session_targets.json`（E01 量測前應該全是 null）。"""
    from host.storage.session_registry import DEFAULT_SESSION_TARGETS_PATH
    targets = load_session_targets(DEFAULT_SESSION_TARGETS_PATH)
    assert set(targets.keys()) == {
        "target_distance_mm", "target_angle_deg", "tolerance_distance_mm", "tolerance_angle_deg",
    }


def test_load_session_targets_malformed_json_is_all_none(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json")
    targets = load_session_targets(bad)
    assert all(v is None for v in targets.values())


# ---------------------------------------------------------------------------
# session_id 格式


def test_session_id_format_matches_date_and_sequence(tmp_path):
    reg = _registry(tmp_path)
    now = datetime(2026, 9, 1, 10, 0, 0)
    info = reg.start(_valid_metadata(), now=now)
    assert info.session_id == "2026-09-01_S01"


def test_session_id_sequence_increments_within_same_day(tmp_path):
    reg = _registry(tmp_path)
    now = datetime(2026, 9, 1, 10, 0, 0)
    info1 = reg.start(_valid_metadata(), now=now)
    reg.end()
    info2 = reg.start(_valid_metadata(), now=now)

    assert info1.session_id == "2026-09-01_S01"
    assert info2.session_id == "2026-09-01_S02"


# ---------------------------------------------------------------------------
# B10：baseline_done — session start 後強制錄 baseline，未錄完不能進 trial
# （這裡只驗證狀態本身；「擋 trial 開始」是 B11 的事，B11 要檢查這個旗標）


def test_baseline_done_defaults_false_on_new_session(tmp_path):
    reg = _registry(tmp_path)
    info = reg.start(_valid_metadata())
    assert info.baseline_done is False


def test_mark_baseline_recorded_sets_flag(tmp_path):
    reg = _registry(tmp_path)
    reg.start(_valid_metadata())
    reg.mark_baseline_recorded()
    assert reg.current.baseline_done is True


def test_mark_baseline_recorded_without_active_session_raises(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(NoActiveSessionError):
        reg.mark_baseline_recorded()


def test_baseline_done_resets_for_new_session(tmp_path):
    reg = _registry(tmp_path)
    reg.start(_valid_metadata())
    reg.mark_baseline_recorded()
    reg.end()

    info = reg.start(_valid_metadata())

    assert info.baseline_done is False
