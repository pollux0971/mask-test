import pytest

from host.control.resolution import (
    InvalidResolutionError,
    ResolutionChangeInProgressError,
    ResolutionController,
    ResolutionState,
)


def test_initial_state_is_idle():
    ctrl = ResolutionController()
    assert ctrl.state == ResolutionState.IDLE
    assert not ctrl.is_busy


def test_request_change_rejects_invalid_resolution():
    ctrl = ResolutionController()
    with pytest.raises(InvalidResolutionError):
        ctrl.request_change(5)


def test_request_change_to_same_resolution_is_a_noop():
    ctrl = ResolutionController(current_resolution=4)
    ctrl.request_change(4)
    assert ctrl.state == ResolutionState.IDLE
    assert not ctrl.is_busy


def test_full_happy_path_transitions():
    ctrl = ResolutionController(current_resolution=4)
    ctrl.request_change(8)
    assert ctrl.state == ResolutionState.EDITING
    assert ctrl.is_busy

    ctrl.advance_to_building()
    assert ctrl.state == ResolutionState.BUILDING

    ctrl.advance_to_flashing()
    assert ctrl.state == ResolutionState.FLASHING

    ctrl.advance_to_resuming()
    assert ctrl.state == ResolutionState.RESUMING
    assert ctrl.is_busy  # 還沒 complete()，仍算忙碌

    ctrl.complete()
    assert ctrl.state == ResolutionState.IDLE
    assert ctrl.current_resolution == 8
    assert ctrl.pending_resolution is None
    assert not ctrl.is_busy


def test_second_request_while_busy_raises_409_equivalent():
    """驗收條件：flashing 中回 409（這裡是丟一個能對應 409 的例外）。"""
    ctrl = ResolutionController()
    ctrl.request_change(8)
    with pytest.raises(ResolutionChangeInProgressError):
        ctrl.request_change(4)


def test_out_of_order_transition_raises():
    ctrl = ResolutionController()
    ctrl.request_change(8)
    with pytest.raises(ValueError):
        ctrl.advance_to_flashing()  # 還在 EDITING，不能跳過 BUILDING


def test_fail_can_happen_from_any_busy_state():
    ctrl = ResolutionController()
    ctrl.request_change(8)
    ctrl.advance_to_building()
    ctrl.fail("build failed: ...")

    assert ctrl.state == ResolutionState.ERROR
    assert ctrl.error_message == "build failed: ..."
    assert ctrl.pending_resolution is None
    assert ctrl.current_resolution == 4  # 失敗不改變目前解析度


def test_error_state_is_not_busy_but_blocks_new_requests_until_reset():
    ctrl = ResolutionController()
    ctrl.request_change(8)
    ctrl.fail("flash failed")

    assert not ctrl.is_busy  # ERROR 不算忙碌...
    ctrl.request_change(8)  # ...所以新請求應該被接受，不是 409
    assert ctrl.state == ResolutionState.EDITING


def test_reset_error_requires_error_state():
    ctrl = ResolutionController()
    with pytest.raises(ValueError):
        ctrl.reset_error()


def test_reset_error_returns_to_idle():
    ctrl = ResolutionController()
    ctrl.request_change(8)
    ctrl.fail("x")
    ctrl.reset_error()
    assert ctrl.state == ResolutionState.IDLE
    assert ctrl.error_message is None
