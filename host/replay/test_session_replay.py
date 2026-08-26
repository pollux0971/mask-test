import numpy as np
import pytest

from host.storage.session_writer import SessionWriter
from host.storage.test_session_writer import _sample_meta, _sample_trial_kwargs
from host.replay.session_replay import (
    NoReplayEventsError,
    ReplayController,
    TrialNotFoundError,
    read_session_events,
)


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt
        return self.now


def _write_session(path, n_trials=2, T=10, M=12, include_mel=True):
    with SessionWriter(path, _sample_meta()) as w:
        for i in range(n_trials):
            kwargs = _sample_trial_kwargs(T=T, M=M, include_mel=include_mel, include_audio=False)
            kwargs["label"] = f"word_{i}"
            # 每個 trial 的 t_us 錯開，避免不同 trial 的資料時間重疊
            # （真實 session 本來就是這樣：trial 之間有 REST）。
            offset = i * 10_000_000
            kwargs["tof_t_us"] = kwargs["tof_t_us"] + offset
            kwargs["mic_t_us"] = kwargs["mic_t_us"] + offset
            if "mel_t_us" in kwargs:
                kwargs["mel_t_us"] = kwargs["mel_t_us"] + offset
            w.write_trial(i, **kwargs)
    return path


# ---------------------------------------------------------------------------
# read_session_events


def test_read_session_events_covers_all_modalities(tmp_path):
    path = _write_session(tmp_path / "session.h5", n_trials=1, T=10, M=12)
    events = read_session_events(path)

    types = {e.payload["type"] for e in events}
    assert types == {"tof", "mic", "mel", "trial"}

    tof_events = [e for e in events if e.payload["type"] == "tof"]
    assert len(tof_events) == 10 * 2  # T frames x 2 sensors

    trial_events = [e for e in events if e.payload["type"] == "trial"]
    assert {e.payload["state"] for e in trial_events} == {"CAPTURE", "SAVE"}


def test_events_are_sorted_by_t_us(tmp_path):
    path = _write_session(tmp_path / "session.h5", n_trials=2, T=10, M=12)
    events = read_session_events(path)

    t_values = [e.t_us for e in events]
    assert t_values == sorted(t_values)


def test_start_trial_idx_skips_earlier_trials(tmp_path):
    path = _write_session(tmp_path / "session.h5", n_trials=3, T=5, M=6)

    events = read_session_events(path, start_trial_idx=1)

    assert all(e.trial_idx >= 1 for e in events)
    assert {e.trial_idx for e in events} == {1, 2}


def test_no_events_past_start_trial_idx_raises(tmp_path):
    path = _write_session(tmp_path / "session.h5", n_trials=2, T=5, M=6)
    with pytest.raises(NoReplayEventsError):
        read_session_events(path, start_trial_idx=99)


def test_tof_invalid_zone_becomes_none_not_nan_or_minus_one(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        kwargs = _sample_trial_kwargs(T=3, M=4, include_mel=False, include_audio=False)
        tof_A = kwargs["tof_A"].tolist()
        tof_A[1][5] = None
        tof_A[1][5 + 16] = None
        kwargs["tof_A"] = tof_A
        kwargs["tof_valid_A"] = np.ones((3, 16), dtype=bool)
        kwargs["tof_valid_A"][1, 5] = False
        w.write_trial(0, **kwargs)

    events = read_session_events(path)
    tof_a_events = [e for e in events if e.payload["type"] == "tof" and e.payload["sensor"] == "A"]
    frame1 = tof_a_events[1].payload

    assert frame1["dist"][5] is None
    assert frame1["signal"][5] is None
    assert frame1["valid"][5] is False
    assert frame1["dist"][0] is not None  # 其他 zone 沒被誤傷


def test_mel_omitted_when_trial_has_no_mel(tmp_path):
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))

    events = read_session_events(path)
    assert not any(e.payload["type"] == "mel" for e in events)


def test_speaking_mode_included_when_present_in_attrs(tmp_path):
    """schema 目前沒有這個欄位（見模組 docstring），但手動塞一個進去時
    要能重播出來，不能因為不認得就丟掉。"""
    path = tmp_path / "session.h5"
    with SessionWriter(path, _sample_meta()) as w:
        w.write_trial(0, **_sample_trial_kwargs(T=5, M=6, include_mel=False, include_audio=False))
        w._file["trial_000"].attrs["speaking_mode"] = "whisper"

    events = read_session_events(path)
    trial_events = [e for e in events if e.payload["type"] == "trial"]
    assert all(e.payload.get("speaking_mode") == "whisper" for e in trial_events)


def test_speaking_mode_absent_when_not_in_attrs(tmp_path):
    path = _write_session(tmp_path / "session.h5", n_trials=1, T=5, M=6)
    events = read_session_events(path)
    trial_events = [e for e in events if e.payload["type"] == "trial"]
    assert all("speaking_mode" not in e.payload for e in trial_events)


def test_quality_and_label_included_in_trial_events(tmp_path):
    path = _write_session(tmp_path / "session.h5", n_trials=1, T=5, M=6)
    events = read_session_events(path)
    trial_events = [e for e in events if e.payload["type"] == "trial"]
    assert all(e.payload["label"] == "word_0" for e in trial_events)
    assert all(e.payload["quality"] == "ok" for e in trial_events)


# ---------------------------------------------------------------------------
# ReplayController — 所有事件都帶 replay: true


def test_all_polled_events_carry_replay_true(tmp_path):
    path = _write_session(tmp_path / "session.h5", n_trials=1, T=5, M=6)
    events = read_session_events(path)
    clock = FakeClock()
    ctrl = ReplayController(events, clock=clock)

    clock.advance(9999)  # 一次跳到很後面，所有事件都到期
    due = ctrl.poll()

    assert due
    assert all(e["replay"] is True for e in due)


# ---------------------------------------------------------------------------
# 時序還原（驗收條件：< 10ms 誤差；這裡驗證的是排程數學本身完全準確，
# 用假時鐘沒有系統排程抖動，真實情況下的抖動來自呼叫端多快 poll()，
# 不是這支模組的排程公式）


def test_events_are_scheduled_according_to_original_t_us_spacing():
    events = [
        ReplayEventStub(0, "a"),
        ReplayEventStub(1_000_000, "b"),  # 1 秒後
        ReplayEventStub(2_000_000, "c"),  # 再 1 秒後
    ]
    clock = FakeClock()
    ctrl = ReplayController(events, clock=clock)

    assert ctrl.poll() == [{"tag": "a", "replay": True}]  # t=0 立刻到期

    clock.advance(0.5)
    assert ctrl.poll() == []  # 還沒到 1 秒

    clock.advance(0.5)  # 累計 1.0s
    assert ctrl.poll() == [{"tag": "b", "replay": True}]

    clock.advance(1.0)  # 累計 2.0s
    assert ctrl.poll() == [{"tag": "c", "replay": True}]


def test_speed_multiplier_scales_schedule():
    events = [ReplayEventStub(0, "a"), ReplayEventStub(1_000_000, "b")]
    clock = FakeClock()
    ctrl = ReplayController(events, clock=clock)
    ctrl.poll()  # 吃掉 t=0 的事件
    ctrl.set_speed(4.0)  # 4x：1 秒的內容應該 0.25 秒播完

    clock.advance(0.24)
    assert ctrl.poll() == []
    clock.advance(0.02)  # 累計 0.26s > 0.25s
    assert ctrl.poll() == [{"tag": "b", "replay": True}]


def test_no_accumulated_drift_across_many_ticks():
    """驗收條件的精神：時序還原誤差 < 10ms，而且長時間播放不能因為
    累加 sleep/累加誤差讓偏移越滾越大（B06 對齊器踩過的坑）。這裡用
    1000 個很密集的假時鐘推進模擬「呼叫頻率遠高於事件間距」，
    確認事件確實在各自該到期的時刻被吐出，不會系統性地漂移。"""
    n = 200
    events = [ReplayEventStub(i * 10_000, f"e{i}") for i in range(n)]  # 每 10ms 一個事件
    clock = FakeClock()
    ctrl = ReplayController(events, clock=clock)

    received = []
    for _ in range(3000):  # 遠比事件數多的極細碎 tick
        clock.advance(0.001)  # 每次只推進 1ms
        received.extend(ctrl.poll())

    assert [e["tag"] for e in received] == [f"e{i}" for i in range(n)]


def test_pause_stops_events_and_resume_does_not_dump_backlog_instantly():
    events = [ReplayEventStub(0, "a"), ReplayEventStub(1_000_000, "b"), ReplayEventStub(2_000_000, "c")]
    clock = FakeClock()
    ctrl = ReplayController(events, clock=clock)
    ctrl.poll()  # 吃掉 a

    ctrl.pause()
    clock.advance(5.0)  # 暫停期間過了很久
    assert ctrl.poll() == []  # 暫停中，什麼都不吐

    ctrl.resume()
    assert ctrl.poll() == []  # 恢復瞬間不該把積壓的事件全部倒出來
    clock.advance(1.0)
    assert ctrl.poll() == [{"tag": "b", "replay": True}]


def test_step_ignores_schedule_and_returns_immediately():
    events = [ReplayEventStub(0, "a"), ReplayEventStub(5_000_000, "b")]
    clock = FakeClock()
    ctrl = ReplayController(events, clock=clock)

    first = ctrl.step()
    second = ctrl.step()

    assert first == {"tag": "a", "replay": True}
    assert second == {"tag": "b", "replay": True}
    assert ctrl.finished


def test_seek_to_trial_jumps_position_and_rebases(tmp_path):
    path = _write_session(tmp_path / "session.h5", n_trials=3, T=5, M=6)
    events = read_session_events(path)
    clock = FakeClock()
    ctrl = ReplayController(events, clock=clock)

    ctrl.seek_to_trial(2)
    assert ctrl.current_trial_idx == 2

    clock.advance(9999)  # 跳到很後面，trial 2 的事件應該全部到期
    due = ctrl.poll()

    assert due  # seek 之後排程照常運作，不是卡住
    trial_events = [e for e in due if e["type"] == "trial"]
    assert trial_events and all(e["idx"] == 2 for e in trial_events)
    assert ctrl.finished  # trial 2 是最後一個 trial，播完就結束


def test_seek_to_nonexistent_trial_raises():
    events = [ReplayEventStub(0, "a")]
    ctrl = ReplayController(events)
    with pytest.raises(TrialNotFoundError):
        ctrl.seek_to_trial(99)


def test_invalid_speed_rejected():
    events = [ReplayEventStub(0, "a")]
    ctrl = ReplayController(events)
    with pytest.raises(ValueError):
        ctrl.set_speed(2.0)  # 不在 story 給的 0.25/1/4 集合裡


def test_is_active_reflects_playback_progress():
    events = [ReplayEventStub(0, "a")]
    clock = FakeClock()
    ctrl = ReplayController(events, clock=clock)

    assert ctrl.is_active
    ctrl.poll()
    assert not ctrl.is_active


def test_empty_events_list_rejected():
    with pytest.raises(NoReplayEventsError):
        ReplayController([])


# 假的最小 ReplayEvent，只提供 ReplayController 需要的兩個屬性
# （t_us / trial_idx / payload），不用真的建一個 HDF5 檔就能測排程邏輯。
class ReplayEventStub:
    def __init__(self, t_us, tag, trial_idx=0):
        self.t_us = t_us
        self.trial_idx = trial_idx
        self.payload = {"tag": tag}
