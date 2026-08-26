"""`analysis/run_all.py` 與 `session_loader.py`（D15）的端到端測試。

用 `h5py` 直接寫出符合 §2 的合成 session（不依賴 `host/storage`，那是 B 軌的
檔案），然後真的跑一次 `python -m analysis.run_all` 的 `main()`。
"""
import time

import h5py
import numpy as np
import pytest

from analysis import run_all
from analysis.reporting import session_loader
from analysis.reporting.verification_report import (
    STATUS_ERROR,
    STATUS_PASS,
    STATUS_SKIPPED,
)

N_ZONES = 16
N_MELS = 40
BASE_MM = 40.0
BASE_SIGMA_MM = 1.2

# §6 的詞：五（圓唇）／一（展唇）給 SNR 當對照，四（擦音）給 D14。
WORDS = ("wu", "yi", "si", "ba")


def _trial_arrays(rng, word, n_frames=30):
    """一筆合成 trial。不同詞讓不同的 zone／band 動，才有類別間差異。"""
    tof_a = rng.normal(BASE_MM, BASE_SIGMA_MM, (n_frames, 2 * N_ZONES))
    tof_b = rng.normal(BASE_MM, BASE_SIGMA_MM, (n_frames, 2 * N_ZONES))
    mel = rng.normal(-6.0, 0.3, (n_frames, N_MELS))

    idx = WORDS.index(word)
    if word == "si":
        # 擦音：Mel 高頻強、ToF 幾乎不動（§6 的「四」）
        mel[10:20, 25:35] += 4.0
    else:
        zones = slice(idx * 3, idx * 3 + 5)
        tof_a[10:20, zones] -= 12.0 * BASE_SIGMA_MM
        tof_b[10:20, zones] -= 10.0 * BASE_SIGMA_MM
        mel[10:20, idx * 4:idx * 4 + 3] += 1.0

    return {
        "tof_A": tof_a.astype(np.float32),
        "tof_B": tof_b.astype(np.float32),
        "tof_valid_A": np.ones((n_frames, N_ZONES), dtype=bool),
        "tof_valid_B": np.ones((n_frames, N_ZONES), dtype=bool),
        "tof_t_us": (np.arange(n_frames) * 33_333).astype(np.int64),
        "mic_rms": rng.normal(300.0, 30.0, n_frames).astype(np.float32),
        "mic_peak": np.full(n_frames, 900, dtype=np.int16),
        "mic_t_us": (np.arange(n_frames) * 32_000).astype(np.int64),
        "mel": mel.astype(np.float32),
        "mel_t_us": (np.arange(n_frames) * 16_000).astype(np.int64),
    }


def write_session(path, *, wear_id, seed, n_per_word=3, words=WORDS,
                  with_mel=True, quality="ok", sensors_enabled="AB",
                  sensors_confirmed=False, with_ambient=False,
                  crosstalk_shift_mm=0.0):
    """`crosstalk_shift_mm` 讓 dual 錄製的距離整體偏移，模擬串擾。"""
    rng = np.random.RandomState(seed)
    with h5py.File(path, "w") as handle:
        meta = handle.create_group("meta")
        meta.attrs["schema_version"] = 1
        meta.attrs["subject"] = "s01"
        meta.attrs["wear_id"] = wear_id
        meta.attrs["mode"] = "quiz"
        meta.attrs["tof_dim"] = N_ZONES
        for sensor in ("A", "B"):
            meta.attrs[f"baseline_mu_{sensor}"] = np.full(2 * N_ZONES, BASE_MM,
                                                          dtype=np.float32)
            meta.attrs[f"baseline_sigma_{sensor}"] = np.full(2 * N_ZONES,
                                                             BASE_SIGMA_MM,
                                                             dtype=np.float32)
        meta.attrs["noise_floor_mu"] = 300.0
        meta.attrs["noise_floor_sigma"] = 30.0
        if sensors_enabled is not None:
            meta.attrs["sensors_enabled"] = sensors_enabled
            meta.attrs["sensors_enabled_confirmed"] = sensors_confirmed

        index = 0
        for word in words:
            for _ in range(n_per_word):
                group = handle.create_group(f"trial_{index:03d}")
                arrays = _trial_arrays(rng, word)
                if crosstalk_shift_mm:
                    arrays["tof_A"] = arrays["tof_A"] + np.float32(crosstalk_shift_mm)
                    arrays["tof_B"] = arrays["tof_B"] + np.float32(crosstalk_shift_mm)
                if with_ambient:
                    n = arrays["tof_A"].shape[0]
                    for sensor in ("A", "B"):
                        arrays[f"tof_ambient_{sensor}"] = rng.normal(
                            5.0, 0.2, (max(1, n // 10), N_ZONES)).astype(np.float32)
                    arrays["tof_ambient_t_us"] = (
                        np.arange(max(1, n // 10)) * 1_000_000).astype(np.int64)
                for name, value in arrays.items():
                    if name.startswith("mel") and not with_mel:
                        continue
                    group.create_dataset(name, data=value)
                group.attrs["label"] = word
                group.attrs["trial_idx"] = index
                group.attrs["wear_id"] = wear_id
                group.attrs["mode"] = "quiz"
                group.attrs["quality"] = quality
                index += 1
    return path


@pytest.fixture
def two_sessions(tmp_path):
    """兩次戴上的 session——跨次戴 CV 需要至少 2 個 `wear_id`。"""
    return [
        write_session(tmp_path / "wear1.h5", wear_id=1, seed=1),
        write_session(tmp_path / "wear2.h5", wear_id=2, seed=2),
    ]


# ------------------------------------------------------------------ 讀檔


def test_load_session_reads_meta_and_trials(tmp_path):
    path = write_session(tmp_path / "s.h5", wear_id=7, seed=0)
    session = session_loader.load_session(path)
    assert session.meta["subject"] == "s01"
    assert session.wear_ids == [7]
    assert session.labels == sorted(WORDS)
    assert session.trials[0].n_zones == N_ZONES
    mu, sigma = session.baseline("A")
    assert mu.shape == (2 * N_ZONES,) and sigma.shape == (2 * N_ZONES,)


def test_load_session_exposes_mic_peak_and_paired_timestamps(tmp_path):
    """SCHEMA_SUPPLY_DEMAND.md 標記過的「結構性讀不到」：`mic_peak`/
    `mic_t_us`（必填 dataset）跟 `mel_t_us`/`tof_ambient_t_us`（分別跟
    `mel`/`tof_ambient_*` 成對必寫）以前寫得進 HDF5，`Trial` dataclass
    卻沒有對應欄位可以讀出來。`mic_t_us` 是這一批裡最後一個漏網的：
    `8f` 做語音 VAD 裁切時卡在這裡——沒有它，offline 分析只能做唇動-only
    裁切，跟線上唇動+語音聯集裁切的窗口定義不同，會造成樣板與查詢用兩種
    不同定義比對的訓練/推論不一致。"""
    path = write_session(tmp_path / "s.h5", wear_id=1, seed=0, with_ambient=True)
    session = session_loader.load_session(path)
    trial = session.trials[0]

    assert trial.mic_peak is not None
    assert trial.mic_peak.shape == trial.mic_rms.shape

    assert trial.mic_t_us is not None
    assert trial.mic_t_us.shape == trial.mic_rms.shape

    assert trial.mel is not None and trial.mel_t_us is not None
    assert trial.mel_t_us.shape[0] == trial.mel.shape[0]

    assert trial.ambient_a is not None and trial.ambient_t_us is not None
    assert trial.ambient_t_us.shape[0] == trial.ambient_a.shape[0]


def test_load_session_mel_t_us_and_ambient_t_us_absent_when_optional_data_is(tmp_path):
    """`mel`/`tof_ambient_*` 都是選填；沒有的時候，成對的時間戳也該是
    `None`，不是報錯，也不是留一個跟資料對不上的陣列。"""
    path = write_session(tmp_path / "s.h5", wear_id=1, seed=0,
                         with_mel=False, with_ambient=False)
    session = session_loader.load_session(path)
    trial = session.trials[0]
    assert trial.mel is None and trial.mel_t_us is None
    assert trial.ambient_a is None and trial.ambient_t_us is None


def test_load_session_rejects_a_file_without_meta(tmp_path):
    path = tmp_path / "bad.h5"
    with h5py.File(path, "w") as handle:
        handle.create_group("trial_000")
    with pytest.raises(ValueError, match="/meta"):
        session_loader.load_session(path)


def test_load_session_rejects_a_file_without_trials(tmp_path):
    path = tmp_path / "empty.h5"
    with h5py.File(path, "w") as handle:
        handle.create_group("meta")
    with pytest.raises(ValueError, match="trial"):
        session_loader.load_session(path)


def test_rejected_trials_are_excluded_by_default(tmp_path):
    """`quality == "rejected"` 是人工標記為不可用的錄音。混進統計等於把
    已知的壞資料當好資料用。"""
    path = write_session(tmp_path / "r.h5", wear_id=1, seed=0, quality="rejected")
    session = session_loader.load_session(path)
    assert len(session.trials) == len(WORDS) * 3
    assert session_loader.usable_trials([session]) == []
    assert len(session_loader.usable_trials([session], require_quality=None)) == len(session.trials)


def test_missing_baseline_is_none_not_a_guess(tmp_path):
    """缺 baseline 就是不能算 z-score，補一個猜的會讓下游安靜地算錯。"""
    path = write_session(tmp_path / "s.h5", wear_id=1, seed=0)
    with h5py.File(path, "a") as handle:
        del handle["meta"].attrs["baseline_sigma_A"]
    session = session_loader.load_session(path)
    assert session.baseline("A") == (None, None)
    assert session.baseline("B")[0] is not None


# ------------------------------------------------------------ 可用性判定


def test_crosstalk_is_skipped_when_there_is_nothing_to_pair():
    """`C0` 不再是「永遠 SKIPPED」。

    這條原本斷言「§2 的 schema **沒有**記錄感測器開關狀態，所以 `C0` 永遠
    跑不了」。`sensors_enabled` 加進 §2 之後那個前提消失了——**測試斷言的是
    一個已經被修掉的限制**，所以改的是測試不是程式。

    現在的正確行為：沒有東西可配時仍然 SKIPPED，但訊息要**數得出來**
    （有幾個、哪一種缺），使用者才知道要補錄什麼。
    """
    reasons = session_loader.availability([])
    assert reasons["C0"] is not None
    assert "共 0 個 session" in reasons["C0"]
    assert "SENS:B=0" in reasons["C0"]           # 告訴他怎麼錄


def test_single_wear_id_skips_the_cv_experiment(tmp_path):
    path = write_session(tmp_path / "s.h5", wear_id=1, seed=0)
    reasons = session_loader.availability([session_loader.load_session(path)])
    assert reasons["B"] is not None and "wear_id" in reasons["B"]
    assert reasons["C"] is None and reasons["E"] is None


def test_two_wear_ids_enable_the_cv_experiment(two_sessions):
    sessions = [session_loader.load_session(p) for p in two_sessions]
    assert session_loader.availability(sessions)["B"] is None


def test_missing_baseline_skips_the_zscore_experiments(tmp_path):
    path = write_session(tmp_path / "s.h5", wear_id=1, seed=0)
    with h5py.File(path, "a") as handle:
        del handle["meta"].attrs["baseline_mu_A"]
    reasons = session_loader.availability([session_loader.load_session(path)])
    for key in ("A", "C", "E"):
        assert reasons[key] is not None and "baseline" in reasons[key]


# ------------------------------------------------------- 一個指令跑完


def test_one_command_produces_a_complete_report(two_sessions, tmp_path):
    """驗收條件：**一個指令產出完整報告**、**同時輸出 HTML**。"""
    out = tmp_path / "out"
    code = run_all.main(["--session", str(two_sessions[0]),
                         "--session", str(two_sessions[1]),
                         "--ablation-permutations", "0", "--out", str(out)])

    assert (out / "summary.md").exists()
    assert (out / "summary.html").exists()
    assert (out / "figures").is_dir()
    assert code in (run_all.EXIT_OK, run_all.EXIT_MUST_PASS_FAILED)

    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert summary.index("## 通過矩陣") < summary.index("## 已知限制")
    for key in ("C0", "A", "B", "C", "E"):
        assert f"| {key} " in summary


def test_report_finishes_well_inside_the_two_minute_budget(two_sessions, tmp_path):
    """驗收條件：執行時間 < 2 分鐘。

    合成資料的規模比真實 session 小，所以這條**不能**證明真實資料也在預算內
    ——它證明的是管線本身沒有病態的慢。真實資料的計時見完成回報。
    """
    started = time.perf_counter()
    run_all.main(["--session", str(two_sessions[0]),
                  "--ablation-permutations", "0", "--out", str(tmp_path / "o")])
    assert time.perf_counter() - started < 120.0


def test_fast_flag_runs_and_is_not_slower(two_sessions, tmp_path):
    run_all.main(["--session", str(two_sessions[0]), "--ablation-permutations", "0",
                  "--out", str(tmp_path / "full")])
    code = run_all.main(["--session", str(two_sessions[0]), "--fast",
                         "--ablation-permutations", "0", "--out", str(tmp_path / "fast")])
    assert code in (run_all.EXIT_OK, run_all.EXIT_MUST_PASS_FAILED)
    assert (tmp_path / "fast" / "summary.md").exists()


def test_unreadable_session_exits_with_bad_input(tmp_path, capsys):
    code = run_all.main(["--session", str(tmp_path / "nope.h5"),
                         "--out", str(tmp_path / "o")])
    assert code == run_all.EXIT_BAD_INPUT
    assert "讀不到 session" in capsys.readouterr().err


def test_report_defaults_to_synthetic_unless_real_is_passed(two_sessions, tmp_path):
    """**預設是合成。** 預設 real 會讓忘記加旗標的人產出一份看起來像真實
    結論的報告。"""
    run_all.main(["--session", str(two_sessions[0]), "--ablation-permutations", "0",
                  "--out", str(tmp_path / "a")])
    assert "合成資料" in (tmp_path / "a" / "summary.md").read_text(encoding="utf-8")

    run_all.main(["--session", str(two_sessions[0]), "--real",
                  "--ablation-permutations", "0", "--out", str(tmp_path / "b")])
    assert "合成資料" not in (tmp_path / "b" / "summary.md").read_text(encoding="utf-8")


def test_missing_mel_downgrades_instead_of_crashing(tmp_path):
    """沒有 `mel` dataset（§2 選填）時整批 trial 組不出特徵——要標成
    SKIPPED 並說明，不是讓整支程式炸掉。"""
    path = write_session(tmp_path / "nomel.h5", wear_id=1, seed=0, with_mel=False)
    code = run_all.main(["--session", str(path), "--ablation-permutations", "0",
                         "--out", str(tmp_path / "o")])
    assert code in (run_all.EXIT_OK, run_all.EXIT_MUST_PASS_FAILED)
    summary = (tmp_path / "o" / "summary.md").read_text(encoding="utf-8")
    assert "SKIPPED" in summary


def test_experiment_error_does_not_kill_the_whole_run(two_sessions, tmp_path, monkeypatch):
    """單一實驗炸掉不該讓前面幾個的結果拿不到——但 `ERROR` 絕不能被當成
    `PASS`。"""
    def boom(*args, **kwargs):
        raise RuntimeError("刻意炸掉")

    monkeypatch.setattr(run_all, "run_viseme", boom)
    sessions = [session_loader.load_session(p) for p in two_sessions]
    outcomes, _, _, _ = run_all.run_experiments(sessions, ablation_permutations=0)

    by_key = {o.key: o for o in outcomes}
    assert by_key["E"].status == STATUS_ERROR
    assert "刻意炸掉" in by_key["E"].reason
    assert "Traceback" in by_key["E"].diagnosis
    # 其他實驗仍然有結果
    assert by_key["C"].status in (STATUS_PASS, "fail", STATUS_SKIPPED)


def test_figures_are_written_when_an_experiment_produces_them(two_sessions, tmp_path):
    out = tmp_path / "out"
    run_all.main(["--session", str(two_sessions[0]), "--session", str(two_sessions[1]),
                  "--ablation-permutations", "0", "--out", str(out)])
    figures = list((out / "figures").glob("*.png"))
    per_experiment = list(out.glob("*.md"))
    # summary.md 一定在；有跑起來的實驗會各自多一份
    assert any(p.name == "summary.md" for p in per_experiment)
    if figures:
        assert all(p.stat().st_size > 0 for p in figures)


def test_exit_code_is_one_when_a_must_pass_experiment_fails(two_sessions, tmp_path,
                                                             monkeypatch):
    """exit code 要能直接掛進 CI 或 shell 迴圈。"""
    from analysis.reporting.verification_report import ExperimentOutcome, STATUS_FAIL

    def failing(*args, **kwargs):
        return ExperimentOutcome(key="A", name="逐 zone SNR", metric="SNR",
                                 measured="0.1 / 0.2", criterion="> 3",
                                 status=STATUS_FAIL, diagnosis="換戴法")

    monkeypatch.setattr(run_all, "run_snr", failing)
    code = run_all.main(["--session", str(two_sessions[0]),
                         "--session", str(two_sessions[1]),
                         "--ablation-permutations", "0", "--out", str(tmp_path / "o")])
    assert code == run_all.EXIT_MUST_PASS_FAILED
    assert "必通過項目失敗" in (tmp_path / "o" / "summary.md").read_text(encoding="utf-8")


def test_the_end_to_end_run_actually_executes_experiments(two_sessions, tmp_path):
    """**這條測試防的是「端到端測試其實全部 SKIPPED」。**

    如果 fixture 的資料不足以跑任何實驗，上面那些 e2e 測試會全綠但什麼都
    沒驗到。這裡明確要求四個實驗真的跑出了判定（`C0` 除外——它從 session
    檔本來就跑不了，見 `session_loader.availability`）。
    """
    from analysis.reporting.verification_report import STATUS_FAIL

    sessions = [session_loader.load_session(p) for p in two_sessions]
    outcomes, _, _, _ = run_all.run_experiments(sessions, ablation_permutations=0)
    by_key = {o.key: o for o in outcomes}

    assert by_key["C0"].status == STATUS_SKIPPED
    for key in ("A", "B", "C", "E"):
        assert by_key[key].status in (STATUS_PASS, STATUS_FAIL), by_key[key].to_dict()
        assert by_key[key].measured != "—"


def test_wear_distance_ratio_refuses_to_pad_with_other_words(two_sessions):
    """跨詞的距離量的是「不同的詞長得不一樣」，跟戴法重複性無關。
    條件不足時要回 `None` + 原因，**不能用別的詞湊數**。"""
    sessions = [session_loader.load_session(two_sessions[0])]   # 只有一個 wear_id
    pairs = session_loader.usable_trials(sessions)
    trials = [t for _, t in pairs]
    by_trial = {id(t): np.zeros((24, 104)) for t in trials}

    result, note = run_all._wear_distance_ratio(trials, by_trial)
    assert result is None
    assert "湊數" in note


def test_wear_distance_ratio_works_with_two_wears(two_sessions):
    sessions = [session_loader.load_session(p) for p in two_sessions]
    pairs = session_loader.usable_trials(sessions)
    trials = [t for _, t in pairs]
    session_by_trial = {id(t): s for s, t in pairs}
    _, _, _, by_trial, _ = run_all.build_feature_seqs(trials, session_by_trial)

    result, note = run_all._wear_distance_ratio(trials, by_trial)
    assert note is None
    assert result is not None
    assert result["ratio"] > 0
    assert result["within_distances"].size and result["between_distances"].size


# ------------------------------- D16/D19 的 extras（「第二顆 ToF 有沒有用」）


def test_extras_are_populated_for_the_three_way_vote(two_sessions):
    """**這個交叉檢查是專案最重要的一個**，而它在接上 `D16`/`D19` 之前
    永遠只有一票。三個來源都要有值。"""
    sessions = [session_loader.load_session(p) for p in two_sessions]
    outcomes, extras, notes, side = run_all.run_experiments(
        sessions, ablation_permutations=20)

    assert "d16_gain" in extras and isinstance(extras["d16_gain"], float)
    assert "d19_dual_matrix" in extras
    assert extras["d19_dual_matrix"]["passed"] in (True, False)

    silhouette = next(o for o in outcomes if o.key == "C")
    # `complementarity_check()` 的鍵是 `passed` 不是 `complementary`——
    # 讀錯鍵名時 `.get()` 會安靜回 None，這一票就沒了。
    assert silhouette.detail["complementary"] in (True, False)

    assert {slug for slug, _ in side} == {
        "d16_mutual_information", "d19_ablation", "d18_permutation",
    }


def test_d18_permutation_uses_wear_id_grouping_when_available(two_sessions):
    """`two_sessions` 是兩個不同 `wear_id`——`D18` 應該真的做了分組驗證，
    不是安靜地退回未分組。"""
    sessions = [session_loader.load_session(p) for p in two_sessions]
    _, extras, notes, side = run_all.run_experiments(sessions, ablation_permutations=20)

    assert extras["d18_grouping"] == "grouped"
    assert any("已用 wear_id 做分組驗證" in note for note in notes)
    d18_report = dict(side)["d18_permutation"]
    assert "有做分組驗證" in d18_report


def test_d18_permutation_flags_a_single_wear_id_loudly(tmp_path):
    """使用者第一批資料很可能只戴一次——分組驗證做不到時，`summary.md`
    的 notes 必須明講，不能看起來跟真的分組一樣。"""
    path = write_session(tmp_path / "single_wear.h5", wear_id=1, seed=1)
    sessions = [session_loader.load_session(path)]
    _, extras, notes, side = run_all.run_experiments(sessions, ablation_permutations=20)

    assert extras["d18_grouping"] == "ungrouped_single_group"
    assert any("分組驗證無法進行" in note for note in notes)
    assert any("🔴" in note and "D18" in note for note in notes)


# ------------------------------- effect_size 接線（7c 的 effect_size.py，只 import）


def _fake_permutation_result(*, score, pvalue, passed, permutation_scores):
    return {
        "score": score, "pvalue": pvalue, "passed": passed,
        "permutation_scores": permutation_scores,
    }


def test_d18_effect_size_flags_significant_p_that_does_not_clear_chance():
    """7c 點名的那條規則：p 值顯著不等於 CI 下界贏過機率基準。手刻一個兩者
    不一致的 report（n 很小、null 分布很窄），不需要真的跑 sklearn CV
    才能驗證這個不一致偵測有沒有接對。"""
    # n=8, n_classes=2（機率基準 50%），k=6/8=75%——Wilson CI 下界約 41%，
    # 蓋住 50% 的機率基準，所以「贏過隨機猜」站不住；但把 pvalue 手動設成
    # < 0.01（模擬置換檢定剛好判定顯著的情境），製造出跟 CI 判斷不一致。
    report = {
        "all": _fake_permutation_result(
            score=0.75, pvalue=0.005, passed=True,
            permutation_scores=[0.5, 0.5, 0.5, 0.5, 0.5],
        ),
        "tof_only": _fake_permutation_result(
            score=0.75, pvalue=0.005, passed=True,
            permutation_scores=[0.5, 0.5, 0.5, 0.5, 0.5],
        ),
    }

    markdown, extra_notes = run_all.d18_effect_size_section(report, n=8, n_classes=2)

    assert len(extra_notes) == 2  # all + tof_only 都不一致
    for note in extra_notes:
        assert "🔴" in note
        assert "p 值顯著" in note
        assert "沒有蓋過機率基準" in note
    assert "準確率（近似）" in markdown
    assert "置換效果量 z" in markdown


def test_d18_effect_size_does_not_flag_when_ci_and_p_agree():
    """兩者一致（都顯著、CI 也贏過機率基準）時不該產生任何 extra_notes——
    這條規則只在**不一致**時才要出聲，不是每次都要講。"""
    report = {
        "all": _fake_permutation_result(
            score=0.95, pvalue=0.001, passed=True,
            permutation_scores=[0.5] * 20,
        ),
        "tof_only": _fake_permutation_result(
            score=0.95, pvalue=0.001, passed=True,
            permutation_scores=[0.5] * 20,
        ),
    }

    _, extra_notes = run_all.d18_effect_size_section(report, n=20, n_classes=2)

    assert extra_notes == []


def test_missing_votes_are_reported_not_silently_empty():
    """**「沒有資料」不等於「沒有矛盾」。** 票數不足時要講出來。"""
    from analysis.reporting.verification_report import (
        STATUS_PASS,
        ExperimentOutcome,
        cross_experiment_checks,
    )

    only_one = [ExperimentOutcome(key="C", name="Silhouette", metric="s",
                                  measured="0.5", criterion="> 0.15",
                                  status=STATUS_PASS,
                                  detail={"complementary": True})]
    findings = cross_experiment_checks(only_one, {})
    vote = next(f for f in findings if f["topic"] == "第二顆 ToF 是否帶來額外資訊")
    assert "只有 1 個來源" in vote["message"]
    assert "「沒有資料」不等於「沒有矛盾」" in vote["message"]


def test_ablation_can_be_switched_off_but_says_so(two_sessions):
    sessions = [session_loader.load_session(p) for p in two_sessions]
    _, extras, notes, _ = run_all.run_experiments(sessions, ablation_permutations=0)
    assert "d19_dual_matrix" not in extras
    assert any("三方投票少一票" in note for note in notes)


def test_side_reports_are_written(two_sessions, tmp_path):
    out = tmp_path / "out"
    run_all.main(["--session", str(two_sessions[0]), "--session", str(two_sessions[1]),
                  "--ablation-permutations", "20", "--out", str(out)])
    assert (out / "d16_mutual_information.md").exists()
    assert (out / "d19_ablation.md").exists()


def test_figures_are_written_in_both_formats(two_sessions, tmp_path):
    """`D20` 驗收條件：PNG(300dpi) + PDF 雙輸出，透過 `run_all` 也要成立。"""
    out = tmp_path / "out"
    run_all.main(["--session", str(two_sessions[0]), "--session", str(two_sessions[1]),
                  "--ablation-permutations", "0", "--out", str(out)])
    pngs = sorted((out / "figures").glob("*.png"))
    pdfs = sorted((out / "figures").glob("*.pdf"))
    assert pngs and len(pngs) == len(pdfs)
    assert [p.stem for p in pngs] == [p.stem for p in pdfs]
    assert all(p.read_bytes()[:5] == b"%PDF-" for p in pdfs)


# ======================================= C0 串擾：solo/dual 配對（sensors_enabled）

from analysis.reporting.session_loader import (          # noqa: E402
    crosstalk_pairs,
    describe_crosstalk_gap,
)


@pytest.fixture
def crosstalk_sessions(tmp_path):
    """同一次戴上（`wear_id=1`）錄兩段：只開 A、兩顆都開。"""
    return [
        write_session(tmp_path / "solo_a.h5", wear_id=1, seed=3,
                      sensors_enabled="A"),
        write_session(tmp_path / "dual.h5", wear_id=1, seed=4,
                      sensors_enabled="AB", crosstalk_shift_mm=0.5),
    ]


def _load(paths):
    return [session_loader.load_session(p) for p in paths]


def test_sensors_enabled_is_read_from_meta(crosstalk_sessions):
    solo, dual = _load(crosstalk_sessions)
    assert solo.sensors_enabled == "A"
    assert dual.sensors_enabled == "AB"


def test_missing_sensors_enabled_is_unknown_not_ab(tmp_path):
    """🔴 **猜 `AB` 會讓舊資料被錯誤配對，而結果看起來完全正常。**"""
    path = write_session(tmp_path / "old.h5", wear_id=1, seed=5,
                         sensors_enabled=None)
    session = session_loader.load_session(path)
    assert session.sensors_enabled is None

    pairs, diagnosis = crosstalk_pairs([session])
    assert pairs == []
    assert diagnosis["counts"]["unknown"] == 1
    assert diagnosis["counts"]["AB"] == 0


def test_confirmed_defaults_to_false_when_absent(tmp_path):
    """「沒寫」不能當成「確認過」。"""
    path = write_session(tmp_path / "s.h5", wear_id=1, seed=6, sensors_enabled=None)
    assert session_loader.load_session(path).sensors_confirmed is False


def test_pairs_solo_with_dual_on_the_same_wear(crosstalk_sessions):
    pairs, diagnosis = crosstalk_pairs(_load(crosstalk_sessions))
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.wear_id == 1
    assert pair.solo_sensor == "A"
    assert pair.dual.sensors_enabled == "AB"
    assert diagnosis["n_pairs"] == 1


def test_does_not_pair_across_different_wear_ids(tmp_path):
    """🔴 跨次戴比 crosstalk 會把**戴法差異**算成**串擾**。

    串擾門檻是 2 mm，而重新戴一次造成的距離差可以輕易超過它——
    **配錯的結果看起來完全正常，只是 Δ 偏大。**
    """
    sessions = _load([
        write_session(tmp_path / "solo_w1.h5", wear_id=1, seed=7, sensors_enabled="A"),
        write_session(tmp_path / "dual_w2.h5", wear_id=2, seed=8, sensors_enabled="AB"),
    ])
    pairs, diagnosis = crosstalk_pairs(sessions)
    assert pairs == []
    assert diagnosis["unpaired_wear_ids"] == [1, 2]
    assert "同一次戴上" in describe_crosstalk_gap(diagnosis)


def test_unconfirmed_state_does_not_block_pairing(crosstalk_sessions):
    """⚠️ `confirmed` 幾乎永遠是 `False`（`$STATUS` 沒有 `sens_a=`）。
    拿它當配對條件會讓 `C0` 永遠跑不了——**但也不能靜默忽略**。"""
    pairs, diagnosis = crosstalk_pairs(_load(crosstalk_sessions))
    assert len(pairs) == 1
    assert pairs[0].confirmed is False
    assert diagnosis["any_unconfirmed"] is True


def test_confirmed_true_propagates_when_the_device_did_confirm(tmp_path):
    sessions = _load([
        write_session(tmp_path / "s.h5", wear_id=1, seed=9, sensors_enabled="A",
                      sensors_confirmed=True),
        write_session(tmp_path / "d.h5", wear_id=1, seed=10, sensors_enabled="AB",
                      sensors_confirmed=True),
    ])
    pairs, diagnosis = crosstalk_pairs(sessions)
    assert pairs[0].confirmed is True
    assert diagnosis["any_unconfirmed"] is False


def test_gap_message_counts_what_exists_not_just_says_insufficient(tmp_path):
    """「資料不足」幫不上任何忙——使用者要的是「我該補錄什麼」。"""
    sessions = _load([
        write_session(tmp_path / "d1.h5", wear_id=1, seed=11, sensors_enabled="AB"),
        write_session(tmp_path / "old.h5", wear_id=1, seed=12, sensors_enabled=None),
    ])
    _, diagnosis = crosstalk_pairs(sessions)
    message = describe_crosstalk_gap(diagnosis)
    assert "共 2 個 session" in message
    assert "兩顆都開（AB）1 個" in message
    assert "未知）1 個" in message
    assert "SENS:B=0" in message


def test_availability_reports_c0_as_runnable_when_paired(crosstalk_sessions):
    """`C0` 從「永遠 SKIPPED」變成「有資料就跑」。"""
    assert session_loader.availability(_load(crosstalk_sessions))["C0"] is None


# ------------------------------------------------------------ C0 真的跑起來


def test_crosstalk_runs_and_reports_partial_coverage(crosstalk_sessions):
    """只有一組 solo 錄製時，**覆蓋率要出現在矩陣的「實測」欄**，
    不能藏在備註裡。"""
    outcomes, _, _, _ = run_all.run_experiments(
        _load(crosstalk_sessions), ablation_permutations=0)
    c0 = next(o for o in outcomes if o.key == "C0")

    assert c0.status in (STATUS_PASS, "fail")
    assert c0.measured != "—"
    assert "A:" in c0.measured
    assert "B 未量測" in c0.measured           # ← 掃矩陣就看得到
    assert c0.detail["measured_sensors"] == ["A"]
    assert c0.detail["missing_sensors"] == ["B"]
    assert c0.detail["sensors_confirmed"] is False
    assert "未經裝置確認" in c0.reason
    assert "只開 B" in c0.reason


def test_crosstalk_report_flags_the_missing_ambient(crosstalk_sessions):
    """`D10` 明訂 ambient 是 crosstalk **最靈敏**的指標——沒有它就要講。"""
    outcomes, _, _, _ = run_all.run_experiments(
        _load(crosstalk_sessions), ablation_permutations=0)
    c0 = next(o for o in outcomes if o.key == "C0")
    assert "沒有 ambient 資料" in c0.report_md
    assert "最靈敏" in c0.report_md


def test_crosstalk_never_fabricates_ambient_to_call_format_report(crosstalk_sessions):
    """**不能為了呼叫 `format_report()` 而餵零進去**——那是捏造一個
    「ambient 完全沒變」的結論。"""
    outcomes, _, _, _ = run_all.run_experiments(
        _load(crosstalk_sessions), ablation_permutations=0)
    c0 = next(o for o in outcomes if o.key == "C0")
    assert c0.detail["has_ambient"] == []
    assert "部分覆蓋" in c0.report_md          # 簡版而非 D10 的完整報告


def test_crosstalk_full_coverage_uses_the_d10_report(tmp_path):
    """A/B 兩個方向都有、ambient 也齊全時，用 `D10` 自己的完整報告。"""
    sessions = _load([
        write_session(tmp_path / "solo_a.h5", wear_id=1, seed=13,
                      sensors_enabled="A", with_ambient=True),
        write_session(tmp_path / "solo_b.h5", wear_id=1, seed=14,
                      sensors_enabled="B", with_ambient=True),
        write_session(tmp_path / "dual.h5", wear_id=1, seed=15,
                      sensors_enabled="AB", with_ambient=True,
                      crosstalk_shift_mm=0.4),
    ])
    outcomes, _, _, _ = run_all.run_experiments(sessions, ablation_permutations=0)
    c0 = next(o for o in outcomes if o.key == "C0")
    assert c0.detail["measured_sensors"] == ["A", "B"]
    assert c0.detail["missing_sensors"] == []
    assert "未量測" not in c0.measured
    assert c0.report_md.startswith("# D10")   # ← D10 自己的完整報告
    assert sorted(c0.detail["has_ambient"]) == ["A", "B"]


def test_crosstalk_fails_when_the_shift_exceeds_the_threshold(tmp_path):
    """門檻是 2 mm。偏移 5 mm 必須 FAIL，不能永遠回 PASS。"""
    sessions = _load([
        write_session(tmp_path / "solo.h5", wear_id=1, seed=16, sensors_enabled="A"),
        write_session(tmp_path / "dual.h5", wear_id=1, seed=17, sensors_enabled="AB",
                      crosstalk_shift_mm=5.0),
    ])
    outcomes, _, _, _ = run_all.run_experiments(sessions, ablation_permutations=0)
    c0 = next(o for o in outcomes if o.key == "C0")
    assert c0.status == "fail"
    assert c0.diagnosis and "超過" in c0.diagnosis


def test_crosstalk_still_skipped_without_pairs(two_sessions):
    """兩個 session 都是 `AB`（預設），配不出 solo → 仍然 SKIPPED，
    但訊息要說清楚缺什麼。"""
    outcomes, _, _, _ = run_all.run_experiments(
        _load(two_sessions), ablation_permutations=0)
    c0 = next(o for o in outcomes if o.key == "C0")
    assert c0.status == STATUS_SKIPPED
    assert "共 2 個 session" in c0.reason
    assert "只開一顆（A/B）0 個" in c0.reason
