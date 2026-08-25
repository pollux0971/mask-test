"""`host/vad/audio_vad.py`（B15）的測試。

驗收條件裡的「20 筆**人工標註**的錄音」需要真實錄音與人工標註，本專案目前
兩者都沒有。這裡改用**合成 RMS 軌跡**：起訖時間是已知的，所以邊界誤差算得
出來——但它驗的是**演算法**，不是真實語音。真實錄音的驗證列在完成回報的
「需要人工驗證」。合成的部分測試會明講自己是合成的，不會混淆兩者。
"""
import numpy as np
import pytest

from host.vad.audio_vad import (
    DEFAULT_SPEAKING_MODE,
    HANGOVER_MS,
    MIN_SEGMENT_MS,
    SPEAKING_MODES,
    VadResult,
    detect_from_events,
    detect_voice_activity,
    thresholds_for,
)

# $M 在 mic_hop=512 @16kHz 下是 31.25 Hz（CONTRACTS §1.1.2 / §1.3.1）。
FRAME_US = 32_000
NOISE_MU = 300.0
NOISE_SIGMA = 30.0


def times(n, start_us=1_000_000, frame_us=FRAME_US):
    return [start_us + i * frame_us for i in range(n)]


def synth_recording(rng, *, n_frames=120, onset_frame=40, n_voiced=25,
                    peak_sigma=20.0, ramp_frames=1,
                    mu=NOISE_MU, sigma=NOISE_SIGMA):
    """一段合成的 `$M` RMS 軌跡：底噪 + 一個有上升／下降沿的詞。

    回傳 `(rms, t_us, truth_start_us, truth_end_us)`。真值定義為**斜坡的
    中點**——那是人工標註者會畫線的地方，也讓上升沿不會偏袒偵測器。
    """
    values = rng.normal(mu, sigma, n_frames)
    peak = peak_sigma * sigma
    for k in range(ramp_frames):
        gain = (k + 1) / (ramp_frames + 1)
        values[onset_frame + k] += peak * gain
        values[onset_frame + n_voiced - 1 - k] += peak * gain
    for i in range(onset_frame + ramp_frames, onset_frame + n_voiced - ramp_frames):
        values[i] += peak * rng.uniform(0.75, 1.0)

    t = times(n_frames)
    half = ramp_frames / 2.0
    truth_start = int(t[onset_frame] + half * FRAME_US)
    truth_end = int(t[onset_frame + n_voiced - 1] - half * FRAME_US)
    return list(values), t, truth_start, truth_end


# ------------------------------------------------ 驗收條件 3：閾值自動計算


def test_thresholds_come_from_session_noise_floor():
    """換環境只要底噪統計換掉，閾值就跟著換——沒有任何寫死的數字。"""
    quiet_enter, quiet_exit = thresholds_for(300.0, 30.0)
    assert quiet_enter == pytest.approx(300 + 3 * 30)
    assert quiet_exit == pytest.approx(300 + 1.5 * 30)

    noisy_enter, noisy_exit = thresholds_for(1200.0, 250.0)   # 吵雜的房間
    assert noisy_enter == pytest.approx(1200 + 3 * 250)
    assert noisy_exit == pytest.approx(1200 + 1.5 * 250)
    assert noisy_enter > quiet_enter                          # 自動跟著抬高


def test_whisper_lowers_only_the_enter_threshold():
    """進入閾值管靈敏度（關於**說話者**），離開閾值管「安靜要多安靜」
    （關於**底噪**）。底噪不會因為人改用氣音就變小，所以離開閾值不跟著降。
    實測把 whisper 的離開降到 1.0σ 會讓邊界誤差從 80 ms 飆到 240 ms。"""
    normal_enter, normal_exit = thresholds_for(NOISE_MU, NOISE_SIGMA, "normal")
    whisper_enter, whisper_exit = thresholds_for(NOISE_MU, NOISE_SIGMA, "whisper")
    assert whisper_enter < normal_enter
    assert whisper_exit == normal_exit


def test_zero_sigma_does_not_produce_inf_or_nan():
    """麥克風壞掉或整段零變異時 sigma=0，除下去會靜默壞掉。"""
    enter, exit_ = thresholds_for(300.0, 0.0)
    assert np.isfinite(enter) and np.isfinite(exit_)
    assert enter > exit_ > 300.0


def test_thresholds_reject_silent_mode():
    with pytest.raises(ValueError, match="silent"):
        thresholds_for(NOISE_MU, NOISE_SIGMA, "silent")


# ------------------------------------------------------------ 基本端點偵測


def test_detects_a_single_word():
    rng = np.random.RandomState(0)
    rms, t, truth_start, truth_end = synth_recording(rng)
    result = detect_voice_activity(rms, t, NOISE_MU, NOISE_SIGMA)

    assert result.applicable and result.detected
    seg = result.primary
    assert abs(seg.start_us - truth_start) < 100_000
    assert abs(seg.end_us - truth_end) < 100_000
    assert len(result.segments) == 1
    assert result.confidence == pytest.approx(1.0)      # 20σ 遠超進入閾值


def test_silence_only_returns_no_segments_but_is_applicable():
    """「跑了但沒找到」不等於「沒跑」。"""
    rng = np.random.RandomState(1)
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, 60))
    result = detect_voice_activity(rms, times(60), NOISE_MU, NOISE_SIGMA)
    assert result.applicable is True
    assert result.detected is False
    assert result.confidence == 0.0
    assert result.to_dict()["vad_start_us"] is None      # 不填 0
    assert "沒有任何幀越過進入閾值" in result.reason


# --------------------------------------------------------------- 遲滯行為


def test_hysteresis_does_not_fragment_a_wobbly_word():
    """音量在進入閾值附近抖動時，單一閾值會把一個詞切成好幾段。"""
    rng = np.random.RandomState(2)
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, 100))
    enter, _ = thresholds_for(NOISE_MU, NOISE_SIGMA)
    for i in range(30, 60):
        # 在進入閾值上下 ±0.4σ 之間來回，但始終高於離開閾值
        rms[i] = enter + (0.4 if i % 2 else -0.4) * NOISE_SIGMA
    result = detect_voice_activity(rms, times(100), NOISE_MU, NOISE_SIGMA)
    assert len(result.segments) == 1, [s.to_dict() for s in result.segments]


def test_exit_requires_the_full_hangover():
    """短暫的停頓（詞中的塞音閉鎖）不該把詞切開。"""
    rng = np.random.RandomState(3)
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, 100))
    for i in range(30, 60):
        rms[i] += 20 * NOISE_SIGMA
    for i in (44, 45):            # 64 ms 的閉鎖，遠短於 200 ms 掛延遲
        rms[i] = NOISE_MU
    result = detect_voice_activity(rms, times(100), NOISE_MU, NOISE_SIGMA)
    assert len(result.segments) == 1


def test_long_gap_does_split_two_words():
    rng = np.random.RandomState(4)
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, 140))
    for i in list(range(20, 40)) + list(range(80, 100)):
        rms[i] += 20 * NOISE_SIGMA
    result = detect_voice_activity(rms, times(140), NOISE_MU, NOISE_SIGMA)
    assert len(result.segments) == 2


def test_hangover_is_measured_in_time_not_frames():
    """$M 會掉幀。用「連續 N 幀低於閾值」判斷離開，在掉幀時會**提早**結束
    ——掉掉的那幾幀被當成持續安靜。這裡刻意做出一段掉幀。"""
    rng = np.random.RandomState(5)
    n = 100
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, n))
    for i in range(30, 60):
        rms[i] += 20 * NOISE_SIGMA
    t = times(n)
    # 第 44、45 幀壓到底噪（短閉鎖），並且讓 46 幀之後有一個掉幀造成的大跳
    rms[44] = rms[45] = NOISE_MU
    keep = [i for i in range(n) if i not in (46, 47, 48, 49)]
    rms_gap = [rms[i] for i in keep]
    t_gap = [t[i] for i in keep]

    result = detect_voice_activity(rms_gap, t_gap, NOISE_MU, NOISE_SIGMA)
    # 掉幀讓 45 → 50 之間出現 160 ms 的時間跳；用幀數算會誤判成「安靜了很久」
    assert len(result.segments) == 1, [s.to_dict() for s in result.segments]
    assert result.primary.end_us >= t[59] - FRAME_US


def test_onset_backoff_keeps_a_soft_consonant_onset():
    """子音起始爬得快但峰值不高。只取越過進入閾值的那一幀會切掉起音。"""
    rng = np.random.RandomState(6)
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, 80))
    enter, exit_ = thresholds_for(NOISE_MU, NOISE_SIGMA)
    rms[30] = exit_ + 0.1 * NOISE_SIGMA        # 剛過離開閾值：起音的腳
    rms[31] = (enter + exit_) / 2
    for i in range(32, 55):
        rms[i] = enter + 10 * NOISE_SIGMA
    result = detect_voice_activity(rms, times(80), NOISE_MU, NOISE_SIGMA)
    # 起點要退到第 30 幀，不是第 32 幀
    assert result.primary.start_us == times(80)[30]


def test_single_frame_spike_is_discarded_as_too_short():
    rng = np.random.RandomState(7)
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, 60))
    rms[25] += 30 * NOISE_SIGMA                # 椅子聲：一幀
    result = detect_voice_activity(rms, times(60), NOISE_MU, NOISE_SIGMA)
    assert result.detected is False
    assert result.discarded_short_segments == 1


def test_primary_is_the_longest_segment_not_the_first():
    """一次 trial 錄一個詞，偶爾夾雜咳嗽。取最長的比取第一個穩健。"""
    rng = np.random.RandomState(8)
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, 160))
    for i in range(20, 26):                    # 短：咳嗽
        rms[i] += 20 * NOISE_SIGMA
    for i in range(90, 125):                   # 長：真正的詞
        rms[i] += 20 * NOISE_SIGMA
    result = detect_voice_activity(rms, times(160), NOISE_MU, NOISE_SIGMA)
    assert len(result.segments) == 2
    assert result.primary.start_us == times(160)[90]


# ---------------------------------------------------------- 模式與可用性


def test_silent_mode_is_not_applicable():
    """`silent` 是「沒出聲」，不是「沒偵測到」——下游要分得出來。"""
    rng = np.random.RandomState(9)
    rms, t, _, _ = synth_recording(rng)
    result = detect_voice_activity(rms, t, NOISE_MU, NOISE_SIGMA, speaking_mode="silent")
    assert result.applicable is False
    assert result.detected is False
    assert "silent" in result.reason and "B16" in result.reason


def test_missing_noise_floor_is_not_applicable_with_a_useful_reason():
    rng = np.random.RandomState(10)
    rms, t, _, _ = synth_recording(rng)
    for mu, sigma in [(None, NOISE_SIGMA), (NOISE_MU, None), (None, None)]:
        result = detect_voice_activity(rms, t, mu, sigma)
        assert result.applicable is False
        assert "B10" in result.reason


def test_too_few_frames_is_not_applicable():
    result = detect_voice_activity([500.0], [1000], NOISE_MU, NOISE_SIGMA)
    assert result.applicable is False and "幀數不足" in result.reason


def test_unknown_speaking_mode_raises():
    with pytest.raises(ValueError, match="未知的 speaking_mode"):
        detect_voice_activity([1, 2], [0, 1], NOISE_MU, NOISE_SIGMA, speaking_mode="shout")


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError, match="長度不符"):
        detect_voice_activity([1, 2, 3], [0, 1], NOISE_MU, NOISE_SIGMA)


def test_out_of_order_timestamps_are_sorted_not_rejected():
    rng = np.random.RandomState(11)
    rms, t, _, _ = synth_recording(rng)
    order = list(range(len(t)))
    order[10], order[11] = order[11], order[10]
    shuffled_rms = [rms[i] for i in order]
    shuffled_t = [t[i] for i in order]
    result = detect_voice_activity(shuffled_rms, shuffled_t, NOISE_MU, NOISE_SIGMA)
    assert result.applicable and result.detected


# ------------------------------------------------------ detect_from_events


def test_detect_from_events_filters_mic_frames():
    rng = np.random.RandomState(12)
    rms, t, truth_start, _ = synth_recording(rng)
    events = [{"type": "tof", "t_us": t[0], "seq": 0}]
    events += [{"type": "mic", "t_us": ts, "rms": v, "has_timestamp": True}
               for ts, v in zip(t, rms)]
    events += [{"type": "heartbeat", "t_us": t[-1]}]
    result = detect_from_events(events, NOISE_MU, NOISE_SIGMA)
    assert result.detected
    assert abs(result.primary.start_us - truth_start) < 100_000


def test_detect_from_events_refuses_v1_frames_without_timestamps():
    """v1 的 `$M` 沒有 t_us。用幀索引冒充時間會產出看起來合理、完全錯誤的
    邊界——那比直接說「做不了」危險得多。"""
    events = [{"type": "mic", "t_us": None, "rms": 500.0, "proto": 1}] * 40
    result = detect_from_events(events, NOISE_MU, NOISE_SIGMA)
    assert result.applicable is False
    assert "v1" in result.reason and "t_us" in result.reason


def test_detect_from_events_without_mic_frames():
    result = detect_from_events([{"type": "tof"}], NOISE_MU, NOISE_SIGMA)
    assert result.applicable is False and "$M" in result.reason


# -------------------------------------------------------------- 信心度


def test_confidence_is_monotone_in_peak_level():
    rng = np.random.RandomState(13)
    scores = []
    for peak in (3.5, 4.5, 6.0, 12.0):
        rms, t, _, _ = synth_recording(rng, peak_sigma=peak)
        scores.append(detect_voice_activity(rms, t, NOISE_MU, NOISE_SIGMA).confidence)
    assert scores == sorted(scores)
    assert scores[0] < 1.0 and scores[-1] == pytest.approx(1.0)


def test_confidence_is_zero_when_nothing_detected():
    rng = np.random.RandomState(14)
    rms = list(rng.normal(NOISE_MU, NOISE_SIGMA, 60))
    assert detect_voice_activity(rms, times(60), NOISE_MU, NOISE_SIGMA).confidence == 0.0


def test_to_dict_shape_matches_trial_attrs():
    """`/trial_NNN` 的 attrs 要的是 `vad_start_us` / `vad_end_us`（§2）。"""
    rng = np.random.RandomState(15)
    rms, t, _, _ = synth_recording(rng)
    d = detect_voice_activity(rms, t, NOISE_MU, NOISE_SIGMA).to_dict()
    assert set(["vad_start_us", "vad_end_us", "vad_confidence"]) <= set(d)
    assert isinstance(d["vad_start_us"], int)
    assert 0.0 <= d["vad_confidence"] <= 1.0


# ------------------------------- 驗收條件 1／2：批次邊界誤差與漏偵率


def test_boundary_error_under_100ms_over_20_synthetic_recordings():
    """驗收條件 1 的**合成版**。真實人工標註錄音的驗證見完成回報。"""
    rng = np.random.RandomState(100)
    errors = []
    for i in range(20):
        rms, t, truth_start, truth_end = synth_recording(
            rng,
            onset_frame=rng.randint(20, 50),
            n_voiced=rng.randint(15, 40),
            peak_sigma=rng.uniform(6.0, 25.0),
        )
        result = detect_voice_activity(rms, t, NOISE_MU, NOISE_SIGMA)
        assert result.detected, f"第 {i} 筆完全沒偵測到"
        seg = result.primary
        errors.append(abs(seg.start_us - truth_start) / 1000.0)
        errors.append(abs(seg.end_us - truth_end) / 1000.0)

    assert max(errors) < 100.0, f"最大邊界誤差 {max(errors):.1f} ms"


def test_whisper_miss_rate_under_10_percent_on_synthetic_low_snr():
    """驗收條件 2 的**合成版**。whisper 的音量遠低於一般說話，這裡用
    2–3σ 的峰值模擬——normal 的 3σ 進入閾值在這個區間會大量漏掉
    （實測 32%），正是 story 要求為 whisper 放寬門檻的理由。"""
    rng = np.random.RandomState(200)
    cases = []
    for _ in range(50):
        cases.append(synth_recording(
            rng, onset_frame=rng.randint(20, 50), n_voiced=rng.randint(15, 40),
            peak_sigma=rng.uniform(2.0, 3.0),
        ))

    whisper_hits = sum(
        detect_voice_activity(r, t, NOISE_MU, NOISE_SIGMA, speaking_mode="whisper").detected
        for r, t, _, _ in cases
    )
    normal_hits = sum(
        detect_voice_activity(r, t, NOISE_MU, NOISE_SIGMA, speaking_mode="normal").detected
        for r, t, _, _ in cases
    )
    whisper_miss_rate = 1 - whisper_hits / len(cases)
    assert whisper_miss_rate < 0.10, f"whisper 漏偵率 {whisper_miss_rate:.1%}"
    # 這才是為什麼 whisper 要放寬閾值：同一批資料 normal 漏掉將近三分之一
    assert normal_hits < whisper_hits
    assert 1 - normal_hits / len(cases) > 0.20


def test_whisper_boundaries_stay_within_budget():
    rng = np.random.RandomState(300)
    errors = []
    for _ in range(20):
        rms, t, truth_start, truth_end = synth_recording(
            rng, onset_frame=rng.randint(20, 50), n_voiced=rng.randint(20, 40),
            peak_sigma=rng.uniform(2.5, 6.0),
        )
        result = detect_voice_activity(rms, t, NOISE_MU, NOISE_SIGMA, speaking_mode="whisper")
        if not result.detected:
            continue
        errors.append(abs(result.primary.start_us - truth_start) / 1000.0)
        errors.append(abs(result.primary.end_us - truth_end) / 1000.0)
    assert errors, "whisper 一筆都沒偵測到"
    assert max(errors) < 100.0, f"whisper 最大邊界誤差 {max(errors):.1f} ms"


def test_constants_match_the_story():
    assert SPEAKING_MODES["normal"] == (3.0, 1.5)
    assert SPEAKING_MODES["whisper"] == (2.0, 1.5)
    assert SPEAKING_MODES["silent"] is None
    assert HANGOVER_MS == 200.0
    assert MIN_SEGMENT_MS < 1000.0 / 31.25 * 2      # 至多兩幀
    assert DEFAULT_SPEAKING_MODE == "normal"
