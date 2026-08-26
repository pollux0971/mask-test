import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from analysis.reporting.session_loader import load_session, usable_trials
from analysis.similarity.closed_set_probe import (
    DEFAULT_FUSE_W,
    NoSpeechDetectedError,
    TrackRanking,
    build_probe_vector,
    expected_random_rank,
    format_probe_report,
    pairwise_distance_matrix,
    probe_three_track,
    require_speech_window,
    separability_ratio,
    trial_speech_window,
)
from analysis.similarity.euclidean_baseline import euclidean_dist
from host.storage.session_writer import SessionWriter

# ---------------------------------------------------------------------------
# pairwise_distance_matrix


def test_pairwise_distance_matrix_diagonal_is_zero_and_symmetric():
    vectors = [np.array([[0.0, 0.0]]), np.array([[3.0, 4.0]]), np.array([[0.0, 8.0]])]
    m = pairwise_distance_matrix(vectors, euclidean_dist)
    assert m.shape == (3, 3)
    np.testing.assert_allclose(np.diag(m), [0.0, 0.0, 0.0])
    np.testing.assert_allclose(m, m.T)
    assert m[0, 1] == pytest.approx(5.0)   # 3-4-5
    assert m[0, 2] == pytest.approx(8.0)
    assert m[1, 2] == pytest.approx(np.sqrt(3**2 + 4**2))


# ---------------------------------------------------------------------------
# separability_ratio


def test_separability_ratio_known_case():
    # class "a"：兩筆自距離 = 2；class "b"：一筆，跟 "a" 最近距離 = 10 - 1 = 9（用最近的那筆）
    templates = {
        "a": [np.array([[0.0]]), np.array([[2.0]])],
        "b": [np.array([[10.0]])],
    }
    ratios = separability_ratio(templates, euclidean_dist)
    # a 的同類自距離中位數 = |0-2| = 2；跟 b 最近距離 = min(|0-10|, |2-10|) = 8
    assert ratios["a"] == pytest.approx(8.0 / 2.0)
    # b 只有 1 筆，算不出同類自距離
    assert ratios["b"] is None


def test_separability_ratio_above_threshold_means_separable():
    templates = {
        "round_word": [np.array([[0.0, 0.0]]), np.array([[0.1, 0.0]]), np.array([[-0.1, 0.0]])],
        "spread_word": [np.array([[10.0, 0.0]]), np.array([[10.1, 0.0]])],
    }
    ratios = separability_ratio(templates, euclidean_dist)
    assert ratios["round_word"] > 1.5
    assert ratios["spread_word"] > 1.5


# ---------------------------------------------------------------------------
# expected_random_rank / TrackRanking


def test_expected_random_rank_matches_story_example():
    assert expected_random_rank(8) == pytest.approx(4.5)
    assert expected_random_rank(1) == pytest.approx(1.0)
    assert expected_random_rank(3) == pytest.approx(2.0)


def test_track_ranking_rank_of():
    tr = TrackRanking(classes=["a", "b", "c"], d_raw=np.array([0.5, 0.1, 0.9]),
                       ranked=[("b", 0.1), ("a", 0.5), ("c", 0.9)])
    assert tr.rank_of("b") == 1
    assert tr.rank_of("a") == 2
    assert tr.rank_of("c") == 3
    assert tr.rank_of("nope") is None


# ---------------------------------------------------------------------------
# trial_speech_window / require_speech_window：唇動+語音取聯集、都缺席要報錯


def _fake_trial(attrs):
    return SimpleNamespace(key="trial_000", attrs=attrs)


def test_trial_speech_window_unions_lip_and_voice_sources():
    trial = _fake_trial({
        "lip_onset_us_A": 1_000_000, "lip_onset_us_B": 1_100_000,
        "voice_onset_us": 1_300_000, "vad_end_us": 1_800_000,
    })
    result = trial_speech_window(trial, pre_margin_us=0, post_margin_us=0)
    assert result.trimmed is True
    assert result.window_us == (1_000_000, 1_800_000)  # 最早的唇動 A 開始，到 vad_end


def test_trial_speech_window_silent_mode_uses_lips_only():
    """silent 模式下 voice_onset_us 是 None（B15/B16 慣例），只用唇動。"""
    trial = _fake_trial({
        "lip_onset_us_A": 500_000, "lip_onset_us_B": None,
        "voice_onset_us": None, "vad_end_us": 900_000,
    })
    result = trial_speech_window(trial, pre_margin_us=0, post_margin_us=0)
    assert result.trimmed is True
    assert result.source == "lip_A"
    assert result.window_us == (500_000, 900_000)


def test_require_speech_window_raises_when_nothing_detected():
    trial = _fake_trial({
        "lip_onset_us_A": None, "lip_onset_us_B": None,
        "voice_onset_us": None, "vad_end_us": None,
    })
    with pytest.raises(NoSpeechDetectedError):
        require_speech_window(trial)


def test_require_speech_window_does_not_raise_on_short_window_fallback():
    """裁切窗太短時退回整段，不是「沒偵測到」，不應該觸發硬性報錯。"""
    trial = _fake_trial({
        "lip_onset_us_A": 1_000_000, "lip_onset_us_B": None,
        "voice_onset_us": None, "vad_end_us": 1_010_000,  # 只有 10ms，遠低於 min_span
    })
    result = require_speech_window(trial, pre_margin_us=0, post_margin_us=0)
    assert result.trimmed is False
    assert result.window_us is None
    assert result.source == "lip_A"


# ---------------------------------------------------------------------------
# probe_three_track：手算過的小案例


def _vec(tof_val, mel_val, t=2):
    """(t, 104) 向量，ToF 段全填 tof_val，Mel 段全填 mel_val——方便手算距離。"""
    v = np.zeros((t, 104))
    v[:, :64] = tof_val
    v[:, 64:] = mel_val
    return v


def test_probe_three_track_ranks_nearest_template_first():
    query = _vec(0.0, 0.0)
    templates_by_class = {
        "round_word": [_vec(0.0, 0.0)],   # 完全相同
        "spread_word": [_vec(5.0, 5.0)],  # 明顯較遠
        "click_word": [_vec(2.0, 2.0)],   # 中等距離
    }
    tracks = probe_three_track(query, templates_by_class, "euclidean")
    for track_name in ("tof", "mel", "fused"):
        ranked_labels = [label for label, _ in tracks[track_name].ranked]
        assert ranked_labels[0] == "round_word"
        assert ranked_labels[-1] == "spread_word"


def test_probe_three_track_tof_only_ignores_mel_differences():
    """`tof` 軌只該看 ToF 那 64 維，不受 Mel 段差異影響。"""
    query = _vec(0.0, 0.0)
    templates_by_class = {
        "a": [_vec(0.0, 100.0)],  # ToF 相同，Mel 差很多
        "b": [_vec(1.0, 0.0)],    # ToF 差一點，Mel 相同
    }
    tracks = probe_three_track(query, templates_by_class, "euclidean")
    assert tracks["tof"].rank_of("a") == 1  # ToF 完全相同 -> 排第一
    assert tracks["mel"].rank_of("b") == 1  # Mel 完全相同 -> 排第一


def test_probe_three_track_unknown_dist_method_raises():
    with pytest.raises(KeyError):
        probe_three_track(_vec(0, 0), {"a": [_vec(0, 0)]}, "dtw")


def test_format_probe_report_includes_rank_and_random_baseline():
    query = _vec(0.0, 0.0)
    templates_by_class = {"round_word": [_vec(0.0, 0.0)], "spread_word": [_vec(5.0, 5.0)]}
    tracks = {"euclidean": probe_three_track(query, templates_by_class, "euclidean")}
    report = format_probe_report(tracks, true_label="round_word", n_candidates=2)
    assert "round_word" in report
    assert "隨機基準" in report
    assert "正確答案排名: 1/2" in report


# ---------------------------------------------------------------------------
# 整合測試：合成 HDF5 session，示範「不裁切會擠成一團、裁切後會分開」
# 這就是驗收條件要求的「未裁切 vs 已裁切對照實驗」的具體數字證據。

N_ZONES = 16
TOF_FRAME_US = 33_333   # ~30Hz
MEL_FRAME_US = 8_000    # ~125Hz
T_TOF_TOTAL = 105        # ~3.5s @ 30Hz（story 的例子）
# Mel 幀數要涵蓋跟 ToF 一樣長的真實時間（3.5s），不是隨便挑一個數字——
# 之前用短錄音測試（T=60）留下的 "F=M+7" 慣例只在同一個時長下才成立，
# 直接套到 3.5s 的長錄音會讓 Mel 軌道在 ~0.9s 就結束，後面全部
# `mel_present=False`，第一次寫這個 fixture 時真的踩到（`Aligner` 對齊
# 之後裁切窗口落在 mel 已經斷軌的區間，`usable frames` 變成 0）。
T_MEL_TOTAL = int(T_TOF_TOTAL * TOF_FRAME_US / MEL_FRAME_US) + 5
SPEECH_START_FRAME = 42   # ToF 幀索引，約 1.4s 處開始（story: 1400ms 靜音）
SPEECH_END_FRAME = 57     # 約 1.9s 處結束（~500ms 語音，story 的例子）
TOF_BASELINE_MM = 300.0
TOF_NOISE_STD_MM = 5.0
TOF_SIGNAL_AMP_MM = 30.0
MEL_BASELINE = -3.0
MEL_NOISE_STD = 0.15
MEL_SIGNAL_AMP = 1.2


def _class_signature(rng, n_dims):
    v = rng.normal(size=n_dims)
    return v / np.linalg.norm(v)


def _gen_long_trial_raw(rng, tof_sig_A, tof_sig_B, mel_sig):
    """3.5 秒的 trial，訊號只出現在 [SPEECH_START_FRAME, SPEECH_END_FRAME)
    這一小段，其餘全是 baseline + 雜訊（模擬 hold-to-record 按鍵頭尾的
    靜音）——跟 story 的 ASCII 圖完全對應。"""
    envelope = np.zeros(T_TOF_TOTAL)
    envelope[SPEECH_START_FRAME:SPEECH_END_FRAME] = 1.0

    def _tof_channel(sig_dist):
        dist = TOF_BASELINE_MM + np.outer(envelope, sig_dist) * TOF_SIGNAL_AMP_MM
        dist += rng.normal(0, TOF_NOISE_STD_MM, size=(T_TOF_TOTAL, N_ZONES))
        strength = 500.0 + rng.normal(0, 20.0, size=(T_TOF_TOTAL, N_ZONES))
        return np.concatenate([dist, strength], axis=1).astype(np.float32)

    tof_A = _tof_channel(tof_sig_A)
    tof_B = _tof_channel(tof_sig_B)

    envelope_m = np.zeros(T_MEL_TOTAL)
    m_start = int(SPEECH_START_FRAME * T_MEL_TOTAL / T_TOF_TOTAL)
    m_end = int(SPEECH_END_FRAME * T_MEL_TOTAL / T_TOF_TOTAL)
    envelope_m[m_start:m_end] = 1.0
    mel = MEL_BASELINE + np.outer(envelope_m, mel_sig) * MEL_SIGNAL_AMP
    mel += rng.normal(0, MEL_NOISE_STD, size=(T_MEL_TOTAL, len(mel_sig)))
    return tof_A, tof_B, mel.astype(np.float32), m_start, m_end


def _meta():
    baseline_mu = np.concatenate([np.full(N_ZONES, TOF_BASELINE_MM, dtype=np.float32),
                                   np.full(N_ZONES, 500.0, dtype=np.float32)])
    baseline_sigma = np.concatenate([np.full(N_ZONES, TOF_NOISE_STD_MM, dtype=np.float32),
                                      np.full(N_ZONES, 20.0, dtype=np.float32)])
    return {
        "schema_version": 1, "subject": "synthetic", "session_date": "2026-08-26",
        "wear_id": 1, "mode": "quiz", "distance_mm": 30.0, "angle_deg": 0.0,
        "ambient": "quiet room", "notes": "D21 closed-set-probe fixture",
        "fw_sha": "0000000", "proto_version": 2, "tof_dim": N_ZONES,
        "clock_slope": 1.0, "clock_offset": 0.0, "clock_residual_p95": 0.0,
        "clock_drift_ppm": 0.0, "clock_drift_us": 0.0, "clock_sync_span_us": 30_000_000,
        "clock_sync_confirmed": True, "session_start_device_us": 0,
        "session_start_host_us": 1_756_000_000_000_000, "session_start_rtt_min_us": 800,
        "baseline_mu_A": baseline_mu, "baseline_sigma_A": baseline_sigma,
        "baseline_mu_B": baseline_mu, "baseline_sigma_B": baseline_sigma,
        "noise_floor_mu": 0.0, "noise_floor_sigma": 1.0,
    }


@pytest.fixture(scope="module")
def probe_session():
    """8 個候選詞 + 1 個 query（複錄同一個候選詞），每筆都是 3.5 秒、
    訊號只在中段 ~500ms，且正確標記了 VAD onset/offset attrs。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="d21_probe_test_"))
    rng = np.random.default_rng(42)
    words = [f"word_{i}" for i in range(8)]
    class_sigs = {w: (_class_signature(rng, N_ZONES), _class_signature(rng, N_ZONES),
                       _class_signature(rng, 40)) for w in words}
    target_word = words[0]

    path = tmp_dir / "session.h5"
    with SessionWriter(path, _meta()) as w:
        idx = 0
        for word in words + [f"{target_word}_query"]:
            label = target_word if word.endswith("_query") else word
            sig_A, sig_B, sig_mel = class_sigs[label]
            tof_A, tof_B, mel, m_start, m_end = _gen_long_trial_raw(rng, sig_A, sig_B, sig_mel)
            tof_t_us = np.arange(T_TOF_TOTAL, dtype=np.int64) * TOF_FRAME_US
            mel_t_us = np.arange(T_MEL_TOTAL, dtype=np.int64) * MEL_FRAME_US
            speech_start_us = int(tof_t_us[SPEECH_START_FRAME])
            speech_end_us = int(tof_t_us[SPEECH_END_FRAME])
            w.write_trial(
                idx, label=label,
                tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
                tof_valid_A=np.ones((T_TOF_TOTAL, N_ZONES), dtype=bool),
                tof_valid_B=np.ones((T_TOF_TOTAL, N_ZONES), dtype=bool),
                mic_rms=rng.uniform(0, 32767, size=T_TOF_TOTAL).astype(np.float32),
                mic_peak=rng.integers(0, 32767, size=T_TOF_TOTAL).astype(np.int16),
                mic_t_us=tof_t_us.copy(),
                mel=mel, mel_t_us=mel_t_us,
                wear_id=1, mode="quiz", valid_zone_ratio=1.0, drop_count=0,
                quality="ok",
                lip_onset_us_A=speech_start_us, lip_onset_us_B=speech_start_us + 5_000,
                voice_onset_us=speech_start_us + 20_000,
                vad_start_us=speech_start_us, vad_end_us=speech_end_us,
            )
            idx += 1

    session = load_session(path)
    yield session, target_word
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _enrollment_and_query(session):
    """`probe_session` 固定寫了 9 筆：`trial_000`..`trial_007` 是 8 個候選詞
    （enrollment，各 1 筆），`trial_008` 是額外複錄的 query（跟第一個候選詞
    同一個 label，用來測「探針該正確認出它」）。用 `key` 分辨兩者，不能用
    `label`——query 的 label 故意跟它模仿的候選詞相同。"""
    all_trials = {t.key: t for _, t in usable_trials([session])}
    query_trial = all_trials["trial_008"]
    pairs = {all_trials[f"trial_{i:03d}"].label: all_trials[f"trial_{i:03d}"] for i in range(8)}
    return pairs, query_trial


def test_untrimmed_probe_scores_collapse_near_uniform(probe_session):
    """驗收條件：未裁切版本要證明距離確實會擠成一團（86% 靜音稀釋）。"""
    session, target_word = probe_session
    pairs, query_trial = _enrollment_and_query(session)

    templates_by_class = {label: [build_probe_vector(session, t, trim=False)[0]] for label, t in pairs.items()}
    query_vec, trim = build_probe_vector(session, query_trial, trim=False)
    assert trim is None

    tracks = probe_three_track(query_vec, templates_by_class, "euclidean")
    tof_raw = tracks["tof"].d_raw
    # `normalize_distances()`（`fused` 軌用的）永遠把輸出重新縮放到差不多的
    # 範圍，不管輸入本來擠不擠——拿它的 spread 來判斷「有沒有擠成一團」是
    # 錯的量尺。真正該看的是**未正規化的原始距離**本身的離散係數
    # （std/mean）：稀釋到 86% 都是靜音對靜音時，8 個候選詞的原始距離應該
    # 彼此非常接近（離散係數很小），這才是 story 講的「擠成一團」。
    cv = float(tof_raw.std() / tof_raw.mean())
    assert cv < 0.15, (
        f"未裁切時 ToF 原始距離的離散係數應該很小（8 個候選詞的距離彼此接近，"
        f"被 86% 靜音稀釋），實際 cv={cv:.4f}，distances={tof_raw}"
    )


def test_trimmed_probe_correctly_identifies_target_word(probe_session):
    """驗收條件：裁切後應該能正確排出相似度，target word 排第一。"""
    session, target_word = probe_session
    pairs, query_trial = _enrollment_and_query(session)

    templates_by_class = {label: [build_probe_vector(session, t, trim=True)[0]] for label, t in pairs.items()}
    query_vec, trim = build_probe_vector(session, query_trial, trim=True)
    assert trim is not None and trim.trimmed is True

    for dist_method in ("euclidean", "cosine"):
        tracks = probe_three_track(query_vec, templates_by_class, dist_method)
        assert tracks["fused"].rank_of(target_word) == 1, (
            f"[{dist_method}] 裁切後正確答案應該排第一，"
            f"實際排名 {tracks['fused'].rank_of(target_word)}"
        )
