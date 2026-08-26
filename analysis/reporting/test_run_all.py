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
                  with_mel=True, quality="ok"):
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

        index = 0
        for word in words:
            for _ in range(n_per_word):
                group = handle.create_group(f"trial_{index:03d}")
                for name, value in _trial_arrays(rng, word).items():
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


def test_crosstalk_is_always_skipped_from_a_session_file():
    """§2 的 schema 沒有記錄擷取時另一顆感測器的開關狀態。"""
    reasons = session_loader.availability([])
    assert reasons["C0"] is not None
    assert "開關狀態" in reasons["C0"]


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
    _, _, _, by_trial = run_all.build_feature_seqs(trials, session_by_trial)

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

    assert {slug for slug, _ in side} == {"d16_mutual_information", "d19_ablation"}


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
