import time

import pytest

from host.trial.state_machine import TrialState
from host.trigger.auto_vad_trigger import (
    AudioTriggerNotApplicableError,
    AutoVadTrigger,
    InvalidTriggerSourceError,
    TriggerConfig,
)

NOISE_MU = 300.0
NOISE_SIGMA = 30.0  # normal: enter=390, exit=345


class _FakeStateMachine:
    """跟 `TrialStateMachine` 的介面對得上、但不做任何真的 I/O 的測試替身
    ——這支測試只在乎 `AutoVadTrigger` 有沒有在對的時間點呼叫
    `hold_start()`/`hold_stop()`，不在乎它們背後真的落盤與否（那是
    `test_auto_vad_trigger_mock_device.py` 的整合測試在驗證的事）。
    """

    def __init__(self):
        self.state = TrialState.IDLE
        self.calls = []

    def hold_start(self, device_t_us):
        assert self.state == TrialState.IDLE
        self.state = TrialState.CAPTURE
        self.calls.append(("hold_start", device_t_us))
        return {"type": "trial", "state": "CAPTURE", "device_t_us": device_t_us}

    def hold_stop(self, device_t_us):
        assert self.state == TrialState.CAPTURE
        self.state = TrialState.IDLE
        self.calls.append(("hold_stop", device_t_us))
        return {"type": "trial", "state": "SAVE", "device_t_us": device_t_us}


def _burst(trigger, sm, t0_us, plateau_us=500_000, step_us=32_000, peak_rms=900.0,
           trailing_silence_us=600_000):
    """模擬一段「大聲平台 -> 足夠長的安靜」，回傳收到的事件列表。安靜尾巴
    刻意比 `DEFAULT_SILENCE_CONFIRM_MS`（400ms）長，確保真的能觸發
    `hold_stop()`，不是被截斷在確認窗口中間。"""
    events = []
    t = t0_us
    end_plateau = t0_us + plateau_us
    while t <= end_plateau:
        ev = trigger.push_mic(t, peak_rms)
        if ev is not None:
            events.append(ev)
        t += step_us

    end_silence = end_plateau + trailing_silence_us
    while t <= end_silence:
        ev = trigger.push_mic(t, NOISE_MU)
        if ev is not None:
            events.append(ev)
        t += step_us
    return events


# ---------------------------------------------------------------------------
# 建構期驗證


def test_invalid_trigger_source_rejected():
    with pytest.raises(InvalidTriggerSourceError):
        TriggerConfig(trigger_source="ultrasonic")


def test_audio_source_with_silent_mode_raises_immediately():
    """驗收條件的核心：silent 模式下音訊 VAD 不適用要明確報出來，
    不能悄悄變成「一直觸發失敗」。"""
    sm = _FakeStateMachine()
    with pytest.raises(AudioTriggerNotApplicableError):
        AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA,
                        TriggerConfig(trigger_source="audio", speaking_mode="silent"))


def test_audio_source_without_noise_floor_raises():
    sm = _FakeStateMachine()
    with pytest.raises(AudioTriggerNotApplicableError):
        AutoVadTrigger(sm, None, None, TriggerConfig(trigger_source="audio"))


def test_either_source_with_silent_mode_does_not_raise():
    """`either` 有 tof 這條退路，不該因為音訊不適用就整個報錯。"""
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA,
                              TriggerConfig(trigger_source="either", speaking_mode="silent"))
    assert trigger is not None


def test_tof_source_does_not_need_noise_floor():
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, None, None, TriggerConfig(trigger_source="tof"))
    assert trigger is not None


# ---------------------------------------------------------------------------
# 正常觸發（驗收條件：觸發到擷取開始的延遲 < 50ms —— 這裡驗證的是決策
# 本身沒有額外延遲，是 CPU 內的同步呼叫，不含任何 I/O 或排程等待）


def test_normal_burst_triggers_hold_start_and_hold_stop():
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig())

    events = _burst(trigger, sm, t0_us=0)

    kinds = [c[0] for c in sm.calls]
    assert kinds == ["hold_start", "hold_stop"]
    assert len(events) == 2


def test_trigger_uses_original_onset_time_not_confirmation_time():
    """`hold_start()` 的 `device_t_us` 要對齊真正越過閾值的那一刻，不是
    confirm 視窗跑完、軟體才確定「真的是語音」的那一刻——已經內建在
    `hold_stop()` 裡的 300ms pre-roll 是從這個時間點往回算，軟體確認多花
    的那幾十毫秒不該讓實際擷取到的資料也跟著晚 64ms。"""
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig())

    trigger.push_mic(0, 900.0)  # 上升沿起點
    trigger.push_mic(64_000, 900.0)  # confirm 視窗滿足，這時才真的觸發

    assert sm.calls[0] == ("hold_start", 0)


def test_trigger_decision_latency_is_negligible():
    """驗收條件：觸發到擷取開始的延遲 < 50ms。量的是這支函式本身的 CPU
    決策時間——confirm 視窗是「等真實世界過了這麼久」，不是這支函式自己
    拖的，兩者不能混為一談（見上一個測試：資料時間戳用的是原始上升沿）。"""
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig())

    t0 = time.perf_counter()
    trigger.push_mic(0, 900.0)
    trigger.push_mic(64_000, 900.0)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert sm.calls and sm.calls[0][0] == "hold_start"
    assert elapsed_ms < 50.0


def test_brief_dip_during_speech_does_not_end_segment_early():
    """遲滯設計的重點：講話中間一個短暫的停頓（沒有到 400ms 靜音確認）
    不該提前結束段落——這正是遲滯／掛延遲要解決的問題。"""
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig())

    trigger.push_mic(0, 900.0)  # 上升沿起點
    trigger.push_mic(64_000, 900.0)  # confirm 視窗滿足，正式觸發 hold_start
    trigger.push_mic(150_000, 320.0)  # 短暫掉到接近底噪，但 < 400ms
    trigger.push_mic(300_000, 900.0)  # 又拉回來，取消待確認的結束
    trigger.push_mic(900_000, 320.0)  # 開始真正的靜音
    trigger.push_mic(1_350_000, 320.0)  # 累積 450ms 靜音，超過門檻

    kinds = [c[0] for c in sm.calls]
    assert kinds == ["hold_start", "hold_stop"]  # 中途沒有被誤判成結束


# ---------------------------------------------------------------------------
# 冷卻期（驗收條件：正常音量下誤觸發率 < 1 次/分鐘——冷卻期是防止
# 「一個詞被切成兩段」變成兩次誤觸發的機制）


def test_cooldown_blocks_immediate_retrigger():
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA,
                              TriggerConfig(cooldown_ms=800.0))

    _burst(trigger, sm, t0_us=0)
    assert len(sm.calls) == 2  # 第一次 start+stop

    # 冷卻期內（< 800ms）馬上又大聲，不該觸發第二次
    trigger.push_mic(1_100_000, 900.0)
    assert len(sm.calls) == 2

    # 冷卻期過了之後應該可以再觸發
    events = _burst(trigger, sm, t0_us=2_000_000)
    assert len(sm.calls) == 4
    assert events  # 第二段真的有觸發到


def test_no_trigger_on_quiet_stream():
    """驗收條件：安靜環境誤觸發率 < 1 次/分鐘。純底噪（沒有超過 enter
    閾值的樣本）一次都不該觸發。"""
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig())

    import random
    rng = random.Random(0)
    for i in range(2000):  # 2000 幀 @ ~31Hz ≈ 64 秒的安靜錄音
        t_us = i * 32_000
        rms = NOISE_MU + rng.gauss(0, NOISE_SIGMA)
        trigger.push_mic(t_us, max(0.0, rms))

    assert sm.calls == []


def test_false_trigger_rate_under_one_per_minute_across_seeds():
    """驗收條件的統計版：單一種子跑一次不夠有說服力，這裡用 8 個種子各
    跑 60 秒安靜錄音，總觸發次數 / 總分鐘數要 < 1。"""
    import random

    total_minutes = 0.0
    total_triggers = 0
    for seed in range(8):
        sm = _FakeStateMachine()
        trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig())
        rng = random.Random(seed)
        n_frames = 1875  # ~60s @ 31.25Hz
        for i in range(n_frames):
            t_us = i * 32_000
            rms = max(0.0, NOISE_MU + rng.gauss(0, NOISE_SIGMA))
            trigger.push_mic(t_us, rms)
        total_triggers += sum(1 for c in sm.calls if c[0] == "hold_start")
        total_minutes += (n_frames * 32_000) / 1_000_000 / 60.0

    rate_per_minute = total_triggers / total_minutes
    assert rate_per_minute < 1.0, f"誤觸發率 {rate_per_minute:.3f}/分鐘"


# ---------------------------------------------------------------------------
# either 模式：聯集


def test_either_mode_triggers_from_tof_alone_when_audio_is_quiet():
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig(trigger_source="either"))

    # 音訊全程安靜
    trigger.push_mic(0, NOISE_MU)
    trigger.push_tof_activity(0, True)  # ToF 說有唇動
    trigger.push_tof_activity(64_000, True)  # confirm 視窗滿足

    assert sm.calls and sm.calls[0][0] == "hold_start"


def test_either_mode_ends_only_when_both_sources_are_inactive():
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig(trigger_source="either"))

    trigger.push_tof_activity(0, True)  # 觸發（tof）
    trigger.push_mic(100_000, 900.0)  # 音訊也變大聲
    trigger.push_tof_activity(200_000, False)  # tof 說結束了，但音訊還在講
    trigger.push_mic(600_000, 900.0)

    assert [c[0] for c in sm.calls] == ["hold_start"]  # 還沒結束，因為音訊仍活躍

    trigger.push_mic(700_000, NOISE_MU)  # 音訊也安靜下來
    trigger.push_mic(1_150_000, NOISE_MU)  # 累積 450ms 都安靜

    assert [c[0] for c in sm.calls] == ["hold_start", "hold_stop"]


def test_tof_only_mode_ignores_audio_pushes():
    sm = _FakeStateMachine()
    trigger = AutoVadTrigger(sm, None, None, TriggerConfig(trigger_source="tof"))

    trigger.push_mic(0, 99999.0)  # 音訊爆表也不該觸發，因為來源是 tof-only
    assert sm.calls == []

    trigger.push_tof_activity(0, True)
    trigger.push_tof_activity(64_000, True)  # confirm 視窗滿足
    assert sm.calls and sm.calls[0][0] == "hold_start"


# ---------------------------------------------------------------------------
# 狀態機忙碌時不搶著觸發


def test_does_not_trigger_when_state_machine_is_not_idle():
    sm = _FakeStateMachine()
    sm.state = TrialState.PROMPT  # 手動流程正在進行中
    trigger = AutoVadTrigger(sm, NOISE_MU, NOISE_SIGMA, TriggerConfig())

    # 講話講得夠久、confirm 視窗早就滿足了，但狀態機忙碌中，不該被搶著觸發
    # ——這次呼叫會把 pending 起點重設成 100_000（狀態機空出來後重新起算）。
    trigger.push_mic(0, 900.0)
    event = trigger.push_mic(100_000, 900.0)

    assert event is None
    assert sm.calls == []

    # 狀態機空出來之後，confirm 視窗（從 100_000 起算）滿足就觸發，
    # 用的是重新起算的時間戳，不是最一開始那個已經過期很久的上升沿。
    sm.state = TrialState.IDLE
    event = trigger.push_mic(200_000, 900.0)

    assert event is not None
    assert sm.calls == [("hold_start", 100_000)]
