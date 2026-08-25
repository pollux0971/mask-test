from host.control.device_state import build_device_state
from host.control.resolution import ResolutionController


def test_build_device_state_with_no_status_yet():
    """驗收條件：/device/state 反映真實狀態——連一行 $STATUS 都還沒收到時，
    該老實回報「不知道」而不是瞎猜一個值。"""
    ctrl = ResolutionController()
    state = build_device_state(None, ctrl)

    assert state.resolution == ctrl.current_resolution  # 用 controller 的初始值頂著
    assert state.proto_version is None
    assert state.fw_sha is None
    assert state.mel_enabled is None
    assert state.ambient_enabled is None


def test_build_device_state_reflects_latest_status_event():
    status_event = {
        "type": "status", "res": 8, "dim": 64, "proto": 2, "fw": "a1b2c3d",
        "compatible": True, "mel": True, "amb": False,
        "sr": 16000, "mel_win": 512, "mel_hop": 256, "mic_hop": 512,
    }
    ctrl = ResolutionController(current_resolution=8)

    state = build_device_state(status_event, ctrl)

    assert state.resolution == 8
    assert state.proto_version == 2
    assert state.fw_sha == "a1b2c3d"
    assert state.mel_enabled is True
    assert state.ambient_enabled is False


def test_build_device_state_amb_field_missing_is_none_not_false():
    """protocol.py 目前還沒解析 $STATUS 的 amb= 欄位（見完成回報）；
    這支函式對「缺欄位」要老實回 None，不能悄悄當成 False。"""
    status_event = {"res": 4, "proto": 2, "fw": "x", "mel": True}
    ctrl = ResolutionController()

    state = build_device_state(status_event, ctrl)

    assert state.ambient_enabled is None


def test_sensor_state_is_host_tracked_and_marked_unconfirmed():
    ctrl = ResolutionController()
    state = build_device_state(None, ctrl, sensor_a_enabled=True, sensor_b_enabled=False)

    assert state.sensor_a_enabled is True
    assert state.sensor_b_enabled is False
    assert state.sensor_state_confirmed is False


def test_resolution_during_reflash_prefers_controller_over_stale_status():
    """重燒中，controller 才知道「現在其實正在變」，舊的 $STATUS.res
    可能還是變更前的值，不該被拿來覆蓋。"""
    status_event = {"res": 4, "proto": 2, "fw": "x"}  # 重燒前的舊狀態
    ctrl = ResolutionController(current_resolution=4)
    ctrl.request_change(8)
    ctrl.advance_to_building()

    state = build_device_state(status_event, ctrl)

    assert state.resolution == 4  # controller 的 current_resolution，還沒變完
    assert state.resolution_change_in_progress is True
    assert state.resolution_change_state == "building"


def test_resolution_after_reflash_completes_uses_new_value_immediately():
    ctrl = ResolutionController(current_resolution=4)
    ctrl.request_change(8)
    ctrl.advance_to_building()
    ctrl.advance_to_flashing()
    ctrl.advance_to_resuming()
    ctrl.complete()

    state = build_device_state(None, ctrl)  # 還沒收到重燒後的新 $STATUS

    assert state.resolution == 8
    assert state.resolution_change_in_progress is False


def test_to_dict_is_json_serializable_shape():
    ctrl = ResolutionController()
    state = build_device_state(None, ctrl)
    d = state.to_dict()

    assert set(d.keys()) == {
        "resolution", "proto_version", "fw_sha", "mel_enabled", "ambient_enabled",
        "sensor_a_enabled", "sensor_b_enabled", "sensor_state_confirmed",
        "resolution_change_in_progress", "resolution_change_state",
    }
