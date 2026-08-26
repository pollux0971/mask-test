"""`make_reference_session.py` 的結構性測試。

**不重新驗證 `analysis/run_all.py` 五張卡的通過/失敗**——那要跑真的
`python -m analysis.run_all`（見模組文件的驗證指令），這裡的測試只鎖住
「產生器本身」的結構性前提：檔案數、label/wear_id/sensors_enabled 分布、
schema 合法性（`SessionWriter` 本身的驗證會擋掉大部分格式錯誤），以及
`si`（擦音）刻意設計的「ToF 貼底噪、有語音」性質——這是這支工具第一版
實測時抓到最多 bug 的地方（wear 偏移沒同步進 baseline_mu、crosstalk 檔
wear_id 撞名、mel 省略污染 CV），數字校準本身留給真的跑一次 `run_all`
去確認，不在這裡重新算一份。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import make_reference_session as m  # noqa: E402
from analysis.reporting import session_loader  # noqa: E402
from host.vad.tof_vad import detect_lip_activity  # noqa: E402
from host.vad.audio_vad import detect_voice_activity  # noqa: E402


@pytest.fixture(scope="module")
def ref_dir(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("reference_session")
    written = m.build(out_dir)
    return out_dir, written


def test_build_writes_six_files(ref_dir):
    out_dir, written = ref_dir
    assert len(written) == 6
    for p in written:
        assert p.exists(), f"{p} 沒有被寫出來"


def test_main_sessions_have_distinct_wear_ids_and_full_vocab(ref_dir):
    out_dir, _ = ref_dir
    words, reject = m.load_words()
    expected_labels = set(words) | {reject}

    seen_wear_ids = set()
    for wear_id in m.WEAR_IDS:
        session = session_loader.load_session(out_dir / f"main_wear{wear_id}.h5")
        assert session.sensors_enabled == "AB"
        assert session.meta["wear_id"] == wear_id
        seen_wear_ids.add(wear_id)

        labels_here = {t.label for t in session.trials}
        assert expected_labels <= labels_here, (
            f"wear {wear_id} 缺少詞：{expected_labels - labels_here}"
        )
        # 每次戴上每個詞至少 `TRIALS_PER_WORD_PER_WEAR` 筆。
        for label in expected_labels:
            count = sum(1 for t in session.trials if t.label == label)
            assert count >= m.TRIALS_PER_WORD_PER_WEAR, (
                f"wear {wear_id} 的 {label} 只有 {count} 筆"
            )
        # 至少一筆 silent 模式（派任第 6 點）。
        silent_trials = [t for t in session.trials if t.speaking_mode == "silent"]
        assert silent_trials, f"wear {wear_id} 沒有 silent 模式 trial"
        for t in silent_trials:
            assert "voice_onset_us" not in t.attrs, (
                "silent trial 不該有 voice_onset_us（B15：silent 模式完全不用音訊 VAD）"
            )

    assert seen_wear_ids == set(m.WEAR_IDS)


def test_pooled_trial_count_per_label_meets_minimum(ref_dir):
    """`D18`/`D19` 需要每類至少 6 筆——這裡驗跨 3 次戴上累積後的總數，
    不是單一檔案內的數字（見模組文件：這是刻意跨檔案累積的設計）。"""
    out_dir, _ = ref_dir
    words, reject = m.load_words()
    counts = {label: 0 for label in words + [reject]}
    for wear_id in m.WEAR_IDS:
        session = session_loader.load_session(out_dir / f"main_wear{wear_id}.h5")
        for t in session.trials:
            if t.label in counts:
                counts[t.label] += 1
    for label, count in counts.items():
        assert count >= 6, f"{label} 只有 {count} 筆（跨 3 次戴上累積），未達 D18/D19 的最低要求"


def test_crosstalk_files_share_wear_id_distinct_from_main(ref_dir):
    """鎖住第一版實測抓到的那個 bug：串擾檔的 `wear_id` 不能跟任何
    `main_wear*.h5` 撞在一起，否則 `crosstalk_pairs()` 會配錯對象
    （見 `CROSSTALK_WEAR_ID` 的說明）。"""
    out_dir, _ = ref_dir
    assert m.CROSSTALK_WEAR_ID not in m.WEAR_IDS

    dual = session_loader.load_session(out_dir / "crosstalk_dual_wear99.h5")
    solo_a = session_loader.load_session(out_dir / "crosstalk_soloA_wear99.h5")
    solo_b = session_loader.load_session(out_dir / "crosstalk_soloB_wear99.h5")

    assert dual.sensors_enabled == "AB"
    assert solo_a.sensors_enabled == "A"
    assert solo_b.sensors_enabled == "B"
    for session in (dual, solo_a, solo_b):
        assert session.meta["wear_id"] == m.CROSSTALK_WEAR_ID


def test_crosstalk_solo_b_exceeds_threshold_deliberately(ref_dir):
    """驗收條件要求「有 PASS 也有 FAIL」——鎖住這張刻意設計的 FAIL：
    感測器 B 至少有一個 zone 的 solo-vs-dual 距離差 >= 2mm 門檻。"""
    out_dir, _ = ref_dir
    dual = session_loader.load_session(out_dir / "crosstalk_dual_wear99.h5")
    solo_b = session_loader.load_session(out_dir / "crosstalk_soloB_wear99.h5")

    dual_dist, dual_valid = dual.stacked_tof("B")
    solo_dist, solo_valid = solo_b.stacked_tof("B")
    mean_dual = np.nanmean(np.where(dual_valid, dual_dist, np.nan), axis=0)
    mean_solo = np.nanmean(np.where(solo_valid, solo_dist, np.nan), axis=0)
    worst_delta = np.nanmax(np.abs(mean_dual - mean_solo))
    assert worst_delta >= 2.0, f"solo B 的最差 zone 差只有 {worst_delta:.2f}mm，設計上應該 >= 2mm 才會觸發 FAIL"

    solo_a = session_loader.load_session(out_dir / "crosstalk_soloA_wear99.h5")
    dual_dist_a, dual_valid_a = dual.stacked_tof("A")
    solo_dist_a, solo_valid_a = solo_a.stacked_tof("A")
    mean_dual_a = np.nanmean(np.where(dual_valid_a, dual_dist_a, np.nan), axis=0)
    mean_solo_a = np.nanmean(np.where(solo_valid_a, solo_dist_a, np.nan), axis=0)
    worst_delta_a = np.nanmax(np.abs(mean_dual_a - mean_solo_a))
    assert worst_delta_a < 2.0, f"solo A 的最差 zone 差 {worst_delta_a:.2f}mm，設計上應該 < 2mm 才會 PASS"


def test_si_has_no_lip_activity_but_has_voice_activity(ref_dir):
    """`si`（擦音，viseme F）的核心設計：ToF 貼底噪、Mel/mic 有真的突發
    ——見模組文件「一定要老實講的事」。"""
    out_dir, _ = ref_dir
    session = session_loader.load_session(out_dir / "main_wear0.h5")
    mu_a, sigma_a = session.baseline("A")
    mu_b, sigma_b = session.baseline("B")

    si_trials = [t for t in session.trials if t.label == "si"]
    assert si_trials

    for t in si_trials:
        lip = detect_lip_activity(t.tof_a, t.tof_t_us, mu_a, sigma_a,
                                   energy_mu=session.meta.get("energy_mu"),
                                   energy_sigma=session.meta.get("energy_sigma"))
        assert not lip.detected, "si 不該偵測到唇動（ToF 刻意貼底噪）"

        voice = detect_voice_activity(t.mic_rms, t.mic_t_us,
                                       session.meta.get("noise_floor_mu"),
                                       session.meta.get("noise_floor_sigma"),
                                       speaking_mode="normal")
        assert voice.detected, "si 應該偵測到語音（Mel/mic 的突發訊號）"


def test_strong_word_has_lip_leading_voice(ref_dir):
    """這個專案的命題：唇動要比出聲早——用 `ba`（雙唇，兩顆 ToF 都強）驗。"""
    out_dir, _ = ref_dir
    session = session_loader.load_session(out_dir / "main_wear0.h5")
    mu_a, sigma_a = session.baseline("A")

    ba_trials = [t for t in session.trials if t.label == "ba"]
    assert ba_trials

    t = ba_trials[0]
    lip = detect_lip_activity(t.tof_a, t.tof_t_us, mu_a, sigma_a,
                               energy_mu=session.meta.get("energy_mu"),
                               energy_sigma=session.meta.get("energy_sigma"))
    voice = detect_voice_activity(t.mic_rms, t.mic_t_us,
                                   session.meta.get("noise_floor_mu"),
                                   session.meta.get("noise_floor_sigma"),
                                   speaking_mode="normal")
    assert lip.detected and voice.detected
    assert lip.primary.start_us < voice.primary.start_us, "唇動應該比出聲早"


def test_reject_label_has_no_detected_activity(ref_dir):
    out_dir, _ = ref_dir
    session = session_loader.load_session(out_dir / "main_wear0.h5")
    mu_a, sigma_a = session.baseline("A")

    reject_trials = [t for t in session.trials if t.label == "_reject"]
    assert reject_trials
    for t in reject_trials:
        lip = detect_lip_activity(t.tof_a, t.tof_t_us, mu_a, sigma_a,
                                   energy_mu=session.meta.get("energy_mu"),
                                   energy_sigma=session.meta.get("energy_sigma"))
        voice = detect_voice_activity(t.mic_rms, t.mic_t_us,
                                       session.meta.get("noise_floor_mu"),
                                       session.meta.get("noise_floor_sigma"),
                                       speaking_mode="normal")
        assert not lip.detected and not voice.detected
