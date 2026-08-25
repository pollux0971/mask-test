"""`host/vad/tof_vad.py` 與 `host/vad/onset.py`（B16）的測試。

真實錄音與人工標註本專案還沒有，所以邊界相關的驗證用**合成 ToF 軌跡**
（起訖已知）。合成的部分會明講自己是合成的。
"""
import math

import numpy as np
import pytest

from host.storage.baseline import ZoneQualityReport
from host.vad.audio_vad import detect_voice_activity
from host.vad.onset import (
    QUANTIZATION_RMS_US,
    LipLead,
    measure_lip_lead,
    summarize_lip_lead,
)
from host.vad.tof_vad import (
    BASELINE_SUSPECT_SIGMA_RATIO,
    MATCHED_ENTER_SIGMA,
    MATCHED_EXIT_SIGMA,
    analytic_energy_floor,
    detect_from_events,
    detect_lip_activity,
    estimate_energy_floor,
    excluded_from_quality,
    zone_energy,
)

N_ZONES = 16
TOF_FRAME_US = 33_333            # 30 Hz
MIC_FRAME_US = 32_000            # 31.25 Hz
# 合成資料用的靜止距離與量測雜訊。σ 必須**大於**量化下限
# （`QUANTIZATION_SIGMA_MM ≈ 0.289 mm`），否則 z 不再是 N(0,1)，
# 底下那個「理論分布」的檢驗就沒有意義——真實感測器在幾十 mm 的距離
# 上雜訊本來就是 mm 量級。
BASE_MM = 40.0
BASE_SIGMA_MM = 1.2


def baseline(n_zones=N_ZONES, mu=BASE_MM, sigma=BASE_SIGMA_MM):
    return np.full(2 * n_zones, mu), np.full(2 * n_zones, sigma)


def tof_times(n, start_us=1_000_000):
    return [start_us + i * TOF_FRAME_US for i in range(n)]


def synth_tof(rng, *, n_frames=120, onset_frame=40, n_active=25,
              amplitude_sigma=12.0, n_zones=N_ZONES, ramp_frames=1):
    """合成 ToF 軌跡：靜止底噪 + 一段唇動。回傳 `(tof, t_us, truth_start, truth_end)`。

    唇動讓中央 zone 的距離變近（嘴唇靠近感測器），與 `mock_device.py` 的
    `round` scenario 同方向。
    """
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (n_frames, 2 * n_zones))
    # 中央那幾個 zone 動得最多，邊緣的動得少——真實唇動就是這樣。
    weight = np.array([1.0 if 4 <= z < 12 else 0.35 for z in range(n_zones)])
    delta = amplitude_sigma * BASE_SIGMA_MM
    for k in range(ramp_frames):
        gain = (k + 1) / (ramp_frames + 1)
        tof[onset_frame + k, :n_zones] -= delta * gain * weight
        tof[onset_frame + n_active - 1 - k, :n_zones] -= delta * gain * weight
    for i in range(onset_frame + ramp_frames, onset_frame + n_active - ramp_frames):
        tof[i, :n_zones] -= delta * rng.uniform(0.75, 1.0) * weight

    t = tof_times(n_frames)
    half = ramp_frames / 2.0
    return (tof, t,
            int(t[onset_frame] + half * TOF_FRAME_US),
            int(t[onset_frame + n_active - 1] - half * TOF_FRAME_US))


# ------------------------------------------------------ 能量訊號與底噪估計


def test_sigma_floor_stops_a_near_zero_baseline_from_exploding():
    """整數 mm 的量測，sigma 不可能有意義地小於量化本身。某個 zone 的
    baseline sigma 被記成 0.01 mm 時，除下去 z 會炸到上萬，**那個 zone 會
    單獨主宰整條能量訊號而且不會報錯**。實測 mock 的 idle 串流就是這樣
    （17.0 ± 0.15 mm 四捨五入後幾乎全是整數 17，z 爆到 9.4e5）。"""
    rng = np.random.RandomState(30)
    mu, sigma = baseline()
    sigma[7] = 0.01                       # 一個「完美穩定」的 zone
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (200, 2 * N_ZONES))
    tof[:, :N_ZONES] = np.round(tof[:, :N_ZONES])      # $T 的距離是整數 mm
    energy, _, _ = zone_energy(tof, mu, sigma)
    # 有下限守衛時能量仍在個位數；沒有的話那個 zone 會把平均拉到上百
    assert energy.max() < 10.0

    unguarded, _, _ = zone_energy(tof, mu, sigma, sigma_floor=1e-3)
    assert unguarded.max() > 10.0         # 對照：§3.2 的 1e-3 擋不住


def test_analytic_energy_floor_matches_simulation():
    """靜止時 |z| 的平均是半常態，mu/sigma 算得出來。20000 幀核對。"""
    rng = np.random.RandomState(1)
    for n_zones in (16, 64):
        mu, sigma = baseline(n_zones)
        tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (20_000, 2 * n_zones))
        energy, usable, n_used = zone_energy(tof, mu, sigma)
        assert n_used == n_zones
        a_mu, a_sigma = analytic_energy_floor(n_used)
        assert energy.mean() == pytest.approx(a_mu, abs=0.01)
        assert energy.std() == pytest.approx(a_sigma, rel=0.05)


def test_zone_energy_derives_zone_count_from_shape_not_hardcoded():
    """4×4 是 (T,32)、8×8 是 (T,128)。寫死 16 的話 8×8 只會看到四分之一。"""
    rng = np.random.RandomState(2)
    mu, sigma = baseline(64)
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (50, 128))
    _, _, n_used = zone_energy(tof, mu, sigma)
    assert n_used == 64


def test_estimate_energy_floor_is_robust_to_activity():
    """一段 trial 有 20% 的時間在動作。mean/std 會被拉高，median/MAD 不會。"""
    rng = np.random.RandomState(3)
    energy = np.abs(rng.normal(0, 1, 500)) * 0 + rng.normal(0.8, 0.15, 500)
    energy[200:300] += 5.0                      # 20% 的時間在動
    robust_mu, robust_sigma = estimate_energy_floor(energy)

    # 穩健不等於免疫：動作佔比 d 時中位數落在 0.5/(1-d) 分位，
    # d=0.2 → 62.5 分位 ≈ 真值 +0.32σ ≈ 0.85。偏高一點點，閾值跟著偏嚴
    # ——對 B16 來說偏嚴是對的方向（假觸發會直接偽造出結論）。
    assert 0.80 <= robust_mu <= 0.90
    # sigma 同樣被撐大約 1.5 倍（0.15 → 0.22）。方向一樣是偏嚴。
    assert 0.15 <= robust_sigma <= 0.28

    # 對照：非穩健估計被污染得完全不能用
    assert energy.mean() > 1.7
    assert energy.std() > robust_sigma * 5


def test_explicit_energy_floor_beats_self_estimation():
    """穩健估計偏嚴 23%。拿得到乾淨的靜止資料時（B10 的 baseline 期間）
    應該明確傳進來。"""
    rng = np.random.RandomState(31)
    mu, sigma = baseline()
    rest = rng.normal(BASE_MM, BASE_SIGMA_MM, (300, 2 * N_ZONES))
    rest_energy, _, n_used = zone_energy(rest, mu, sigma)
    clean_mu, clean_sigma = estimate_energy_floor(rest_energy)

    tof, t, truth_start, _ = synth_tof(rng, amplitude_sigma=5.0)
    self_est = detect_lip_activity(tof, t, mu, sigma)
    explicit = detect_lip_activity(tof, t, mu, sigma,
                                   energy_mu=clean_mu, energy_sigma=clean_sigma)

    # 乾淨底噪 → 閾值較低（不被 trial 內的動作撐高）→ 至少一樣敏感
    assert explicit.enter_threshold <= self_est.enter_threshold
    assert explicit.detected
    assert abs(explicit.primary.start_us - truth_start) < 100_000


def test_nan_zones_do_not_poison_the_whole_signal():
    """`no_signal_zones` 的 baseline 是 NaN。除下去整條能量會變 NaN，
    而 NaN 比較永遠是 False——狀態機會安靜地什麼都偵測不到。"""
    rng = np.random.RandomState(4)
    mu, sigma = baseline()
    mu[3] = np.nan
    sigma[3] = np.nan
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (60, 2 * N_ZONES))
    energy, usable, n_used = zone_energy(tof, mu, sigma)
    assert n_used == N_ZONES - 1
    assert np.all(np.isfinite(energy))


def test_excluded_from_quality_collects_all_three_kinds():
    report = ZoneQualityReport(
        ok=False, unstable_zones=[1, 2], no_signal_zones=[2, 7],
        suspect_zero_variance_zones=[9], valid_zone_ratio=0.5,
    )
    assert excluded_from_quality(report) == [1, 2, 7, 9]
    assert excluded_from_quality(None) == []


def test_excluded_zones_are_dropped_from_the_energy():
    rng = np.random.RandomState(5)
    mu, sigma = baseline()
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (60, 2 * N_ZONES))
    tof[:, 5] = BASE_MM + 50 * BASE_SIGMA_MM        # 一個壞掉的 zone 一直大叫
    with_bad, _, _ = zone_energy(tof, mu, sigma)
    without_bad, _, n_used = zone_energy(tof, mu, sigma, excluded_zones=[5])
    assert n_used == N_ZONES - 1
    assert without_bad.mean() < with_bad.mean() / 2


def test_frames_with_all_zones_invalid_are_dropped_not_filled():
    rng = np.random.RandomState(6)
    mu, sigma = baseline()
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (60, 2 * N_ZONES))
    tof[10, :N_ZONES] = np.nan
    result = detect_lip_activity(tof, tof_times(60), mu, sigma)
    assert result.n_frames == 60
    assert result.n_frames_dropped == 1


# ------------------------------------------------------------ 唇動偵測


def test_detects_lip_motion():
    rng = np.random.RandomState(10)
    tof, t, truth_start, truth_end = synth_tof(rng)
    result = detect_lip_activity(tof, t, *baseline())

    assert result.applicable and result.detected
    seg = result.primary
    assert abs(seg.start_us - truth_start) < 100_000
    assert abs(seg.end_us - truth_end) < 100_000
    assert result.n_zones_used == N_ZONES
    assert result.baseline_suspect is False


def test_stillness_produces_no_segments():
    rng = np.random.RandomState(11)
    mu, sigma = baseline()
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (100, 2 * N_ZONES))
    result = detect_lip_activity(tof, tof_times(100), mu, sigma)
    assert result.applicable is True
    assert result.detected is False
    assert result.to_dict()["lip_onset_us"] is None      # 不填 0


def test_playback_control_does_not_false_trigger():
    """**story 明訂的對照組**：播放錄音給裝置聽——有聲音，但沒有唇動。
    ToF 端必須不觸發，否則「唇動比較早」全部是假的。"""
    rng = np.random.RandomState(12)
    mu, sigma = baseline()
    n = 200
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (n, 2 * N_ZONES))
    t = tof_times(n)

    tof_result = detect_lip_activity(tof, t, mu, sigma)
    assert tof_result.detected is False, tof_result.to_dict()

    # 同一時間音訊端有明確的語音
    mic_t = [t[0] + i * MIC_FRAME_US for i in range(n)]
    rms = list(rng.normal(300.0, 30.0, n))
    for i in range(60, 100):
        rms[i] += 20 * 30.0
    audio_result = detect_voice_activity(rms, mic_t, 300.0, 30.0)
    assert audio_result.detected is True

    lead = measure_lip_lead(tof_result, audio_result)
    assert lead.lip_onset_us is None
    assert lead.lead_us is None
    assert lead.comparable is False and "唇動" in lead.reason


def test_baseline_suspect_flag_fires_on_a_stale_baseline():
    """baseline 過期／戴法變了 → 能量的 sigma 遠大於理論下界。"""
    rng = np.random.RandomState(13)
    mu, sigma = baseline()
    # 實際量測的雜訊是 baseline 記錄值的 5 倍
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM * 5, (200, 2 * N_ZONES))
    result = detect_lip_activity(tof, tof_times(200), mu, sigma)
    assert result.baseline_suspect is True
    assert result.energy_sigma > BASELINE_SUSPECT_SIGMA_RATIO * result.analytic_sigma
    assert "baseline" in result.reason


def test_missing_baseline_is_not_applicable():
    rng = np.random.RandomState(14)
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (60, 2 * N_ZONES))
    result = detect_lip_activity(tof, tof_times(60), None, None)
    assert result.applicable is False and "B10" in result.reason


def test_all_zones_excluded_is_not_applicable():
    rng = np.random.RandomState(15)
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (60, 2 * N_ZONES))
    result = detect_lip_activity(tof, tof_times(60), *baseline(),
                                 excluded_zones=range(N_ZONES))
    assert result.applicable is False and "zone" in result.reason


def test_quantization_us_is_reported():
    rng = np.random.RandomState(16)
    tof, t, _, _ = synth_tof(rng)
    result = detect_lip_activity(tof, t, *baseline())
    assert result.quantization_us == pytest.approx(33_333, abs=2)


def test_mismatched_lengths_raise():
    rng = np.random.RandomState(17)
    tof = rng.normal(BASE_MM, BASE_SIGMA_MM, (10, 2 * N_ZONES))
    with pytest.raises(ValueError, match="不符"):
        detect_lip_activity(tof, tof_times(9), *baseline())


def test_odd_channel_count_raises():
    with pytest.raises(ValueError, match="偶數"):
        zone_energy(np.zeros((5, 31)), np.zeros(31), np.ones(31))


# ------------------------------------------------------ detect_from_events


def test_detect_from_events_converts_none_to_nan():
    """事件裡無效 zone 是 `None`（§1.1 的 -1 成對語意）。`None` 進 numpy
    會變 object 陣列，必須轉成 `NaN` 才會被 nanmean 正確忽略。"""
    rng = np.random.RandomState(18)
    tof, t, truth_start, _ = synth_tof(rng)
    events = []
    for row, ts in zip(tof, t):
        distance = list(row[:N_ZONES])
        signal = list(row[N_ZONES:])
        distance[2] = None                  # 這個 zone 每一幀都無效
        signal[2] = None
        events.append({"type": "tof", "sensor": "A", "seq": len(events),
                       "t_us": ts, "dim": N_ZONES,
                       "distance": distance, "signal": signal})
    events.append({"type": "mic", "t_us": t[0], "rms": 1, "seq": 0})

    result = detect_from_events(events, *baseline())
    assert result.applicable and result.detected
    assert abs(result.primary.start_us - truth_start) < 100_000


def test_detect_from_events_picks_the_right_sensor():
    rng = np.random.RandomState(19)
    tof, t, _, _ = synth_tof(rng)
    events = []
    for row, ts in zip(tof, t):
        for sensor in ("A", "B"):
            events.append({"type": "tof", "sensor": sensor, "seq": 0, "t_us": ts,
                           "dim": N_ZONES, "distance": list(row[:N_ZONES]),
                           "signal": list(row[N_ZONES:])})
    a = detect_from_events(events, *baseline(), sensor="A")
    b = detect_from_events(events, *baseline(), sensor="B")
    assert a.n_frames == b.n_frames == len(t)


def test_detect_from_events_refuses_v1_frames():
    events = [{"type": "tof", "sensor": "A", "t_us": None, "dim": N_ZONES,
               "distance": [1.0] * N_ZONES, "signal": [1.0] * N_ZONES}] * 40
    result = detect_from_events(events, *baseline())
    assert result.applicable is False and "v1" in result.reason


def test_detect_from_events_without_frames():
    result = detect_from_events([{"type": "mic"}], *baseline())
    assert result.applicable is False and "$T" in result.reason


# --------------------------------------------------------- 唇動先行量測


def make_pair(rng, *, lip_onset_frame=40, voice_lag_frames=3, amplitude_sigma=12.0):
    """做一組「同一次 trial」的 ToF + 音訊，唇動比發聲早 `voice_lag_frames` 個 ToF 幀。"""
    tof, t, _, _ = synth_tof(rng, onset_frame=lip_onset_frame,
                             amplitude_sigma=amplitude_sigma)
    tof_result = detect_lip_activity(tof, t, *baseline())

    n = 160
    mic_t = [t[0] + i * MIC_FRAME_US for i in range(n)]
    rms = list(rng.normal(300.0, 30.0, n))
    voice_start_us = t[lip_onset_frame] + voice_lag_frames * TOF_FRAME_US
    first = next(i for i, ts in enumerate(mic_t) if ts >= voice_start_us)
    for i in range(first, first + 25):
        rms[i] += 20 * 30.0
    audio_result = detect_voice_activity(rms, mic_t, 300.0, 30.0)
    return tof_result, audio_result


def test_lip_lead_is_positive_when_lips_move_first():
    rng = np.random.RandomState(20)
    tof_result, audio_result = make_pair(rng, voice_lag_frames=4)
    lead = measure_lip_lead(tof_result, audio_result)
    assert lead.comparable is True
    assert lead.lead_us > 0                         # 正 = 唇動比較早
    assert lead.lead_ms == pytest.approx(lead.lead_us / 1000.0)


def test_lip_lead_sign_flips_when_voice_comes_first():
    rng = np.random.RandomState(21)
    tof_result, audio_result = make_pair(rng, voice_lag_frames=-5)
    lead = measure_lip_lead(tof_result, audio_result)
    assert lead.lead_us < 0


def test_lead_is_not_comparable_when_sigmas_differ():
    """**這是 story 明講的陷阱。** ToF 閾值放寬會系統性地產生「唇動比較
    早」的假結果，所以兩邊 σ 倍數不同時要標出來。"""
    rng = np.random.RandomState(22)
    tof, t, _, _ = synth_tof(rng)
    loose = detect_lip_activity(tof, t, *baseline(), enter_sigma=1.5, exit_sigma=0.75)
    _, audio_result = make_pair(np.random.RandomState(22))
    lead = measure_lip_lead(loose, audio_result)
    assert lead.comparable is False
    assert "σ 倍數不同" in lead.reason
    assert lead.lead_us is not None                 # 數字還是給，只是不可比


def test_matched_sigmas_are_the_default_on_both_sides():
    """預設就必須一致，否則每個呼叫端都得自己記得對齊。"""
    rng = np.random.RandomState(23)
    tof_result, audio_result = make_pair(rng)
    assert (MATCHED_ENTER_SIGMA, MATCHED_EXIT_SIGMA) == (3.0, 1.5)
    lead = measure_lip_lead(tof_result, audio_result)
    assert lead.comparable is True


def test_silent_mode_still_gives_lip_boundaries():
    """驗收條件 1：silent 模式下能正確切出動作區間。"""
    rng = np.random.RandomState(24)
    tof, t, truth_start, truth_end = synth_tof(rng)
    tof_result = detect_lip_activity(tof, t, *baseline())
    audio_result = detect_voice_activity([1.0] * 10, list(range(10)), 300.0, 30.0,
                                         speaking_mode="silent")
    lead = measure_lip_lead(tof_result, audio_result)

    assert lead.lip_onset_us is not None
    assert abs(lead.lip_onset_us - truth_start) < 100_000
    assert abs(lead.lip_offset_us - truth_end) < 100_000
    assert lead.lead_us is None                     # 沒有語音就沒有差值
    assert lead.speaking_mode == "silent"
    attrs = lead.to_trial_attrs()
    assert attrs["lip_onset_us"] is not None
    assert attrs["voice_onset_us"] is None          # 不捏造


def test_trial_attrs_are_none_not_zero_when_nothing_detected():
    """0 是一個合法的 t_us；capture 視窗邊界會讓「沒偵測到」看起來像
    「整段都在動」。兩者都會讓下游安靜地算出錯的統計。"""
    lead = LipLead(None, None, None, None, comparable=False)
    attrs = lead.to_trial_attrs()
    assert set(attrs) == {"vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us"}
    assert all(v is None for v in attrs.values())


def test_single_measurement_is_flagged_unresolvable_below_quantization():
    small = LipLead(1_000_000, 1_100_000, 1_020_000, 1_120_000, comparable=True)
    assert abs(small.lead_us) < QUANTIZATION_RMS_US
    assert small.resolvable is False                # 20 ms < 46 ms 量化誤差
    big = LipLead(1_000_000, 1_100_000, 1_150_000, 1_250_000, comparable=True)
    assert big.resolvable is True


# ------------------------------------------- 驗收條件 4：20 筆符號一致


def test_twenty_samples_have_consistent_sign():
    """驗收條件 4 的**合成版**。真實錄音的驗證見完成回報。"""
    rng = np.random.RandomState(500)
    leads = []
    for _ in range(20):
        tof_result, audio_result = make_pair(
            rng,
            lip_onset_frame=rng.randint(30, 55),
            voice_lag_frames=rng.randint(3, 6),      # 100–200 ms 的先行
            amplitude_sigma=rng.uniform(8.0, 20.0),
        )
        leads.append(measure_lip_lead(tof_result, audio_result))

    summary = summarize_lip_lead(leads)
    assert summary.n_total == 20
    assert summary.n_comparable == 20
    assert summary.sign_consistency == 1.0
    assert summary.dominant_sign == 1
    assert summary.mean_us > 0
    # 20 筆平均後的量化誤差約 10 ms，先行量遠大於它 → 這個結論站得住
    assert summary.mean_quantization_us == pytest.approx(QUANTIZATION_RMS_US / math.sqrt(20))
    assert summary.mean_resolvable is True


def test_summary_handles_no_comparable_samples():
    summary = summarize_lip_lead([LipLead(None, None, None, None, comparable=False)])
    assert summary.n_total == 1 and summary.n_comparable == 0
    assert summary.mean_us is None
    assert summary.sign_consistency is None
    assert summary.mean_resolvable is None
    assert summary.to_dict()["dominant_sign"] is None


def test_summary_reports_mixed_signs_honestly():
    leads = [
        LipLead(0, 100, 80_000, 100_100, comparable=True),      # 正
        LipLead(0, 100, -80_000, 100_100, comparable=True),     # 負
        LipLead(0, 100, 90_000, 100_100, comparable=True),      # 正
    ]
    summary = summarize_lip_lead(leads)
    assert summary.n_positive == 2 and summary.n_negative == 1
    assert summary.sign_consistency == pytest.approx(2 / 3)
    assert summary.dominant_sign == 1


def test_quantization_budget_numbers():
    """誤差預算：ToF 33.3 ms + $M 32.0 ms，單筆 RMS 約 46 ms。
    story 預期的先行量是 50–150 ms——**單筆數字沒有意義**。"""
    assert QUANTIZATION_RMS_US == pytest.approx(46_188, rel=1e-3)
    assert QUANTIZATION_RMS_US / math.sqrt(20) == pytest.approx(10_328, rel=1e-3)
