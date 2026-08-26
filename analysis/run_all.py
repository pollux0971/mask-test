"""D15 — `python -m analysis.run_all --session <file.h5>`：一個指令跑完驗證。

調整骨架或戴法之後你會想知道「SNR 有沒有變好」。如果重跑要手動執行五個
腳本再自己拼結果，你就不會常常做——而快速迭代正是這個階段最需要的。

## 這支檔案只負責串接

* 資料層 → `analysis/reporting/session_loader.py`
* 報告層 → `analysis/reporting/verification_report.py`（純函式，測得動）
* 實驗   → `analysis/experiments/*`（**只 import 不修改**）

## 三個刻意的設計

**1. 資料不足的實驗是 `SKIPPED`，不是消失也不是 `FAIL`。**
一個 session 檔給不了全部實驗要的資料（串擾要兩種擷取組態、跨次戴 CV 要
多個 `wear_id`）。缺什麼會寫在那一列的原因裡。**一列從報告裡消失之後，
沒有人會發現它從來沒跑過。**

**2. 單一實驗炸掉不會讓整份報告死掉。** 每個實驗各自 try/except，狀態變成
`ERROR` 並把 traceback 摘要寫進報告。理由：跑一次要幾十秒到兩分鐘，為了
第五個實驗的 bug 而拿不到前四個的結果，只會讓人更不想跑。**但 `ERROR`
絕不會被當成 `PASS`**——必通過項目 error 一樣觸發紅色警示、一樣 exit 1。

**3. exit code 反映必通過項目。** 0 = 全部必通過項目都過；1 = 有必通過項目
失敗或錯誤；2 = 連 session 都讀不起來。這樣它可以直接掛進 CI 或 shell 迴圈。

執行時間目標 < 2 分鐘（story 驗收條件）。超過會在報告與 stderr 提示，並
建議加 `--fast`。
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

import numpy as np

from analysis.reporting import session_loader
from analysis.reporting.plot_style import apply_style
from analysis.reporting.verification_report import (
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    ExperimentOutcome,
    build_report,
    render_summary_html,
    render_summary_markdown,
)

TIME_BUDGET_S = 120.0

EXIT_OK = 0
EXIT_MUST_PASS_FAILED = 1
EXIT_BAD_INPUT = 2

# 輸出檔名用的 ASCII slug（見 `write_outputs`）。
EXPERIMENT_SLUGS = {
    "C0": "c0_crosstalk",
    "A": "a_zone_snr",
    "B": "b_wear_cv",
    "C": "c_silhouette",
    "E": "e_viseme_sensitivity",
}

# 實驗的顯示資訊。`metric`/`criterion` 只是報告用的文字，真正的門檻在各實驗
# 模組裡——**不要在這裡複製一份數字**，複製的那份遲早跟實際門檻對不上。
EXPERIMENT_META = {
    "C0": ("串擾", "Δ_dist", "< 2 mm"),
    "A": ("逐 zone SNR", "SNR_L / SNR_R", "> 3"),
    "B": ("跨次戴 CV", "CV_between", "< 30%"),
    "C": ("Silhouette", "Silhouette", "> 0.15"),
    "E": ("Viseme 敏感度", "擦音 Mel > ToF", "有模式"),
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m analysis.run_all",
        description="跑完全部驗證實驗並產出報告（D15）",
    )
    parser.add_argument("--session", action="append", required=True, metavar="FILE.h5",
                        help="session HDF5 檔；可以給多次（跨次戴 CV 需要多個 wear_id）")
    parser.add_argument("--out", default="reports/verification", metavar="DIR",
                        help="輸出目錄（預設 reports/verification）")
    parser.add_argument("--fast", action="store_true",
                        help="降低計算量（PCA 維度與置換次數），供快速迭代用；"
                             "數字會比完整跑略有出入，報告會標示")
    parser.add_argument("--time-budget", type=float, default=TIME_BUDGET_S, metavar="SEC",
                        help=f"執行時間目標，超過會提示（預設 {TIME_BUDGET_S:.0f} 秒）")
    parser.add_argument("--ablation-permutations", type=int, default=200, metavar="N",
                        help="`D19` 消融套件的置換次數（0 = 不跑 D19）。預設 200；"
                             "`D18`/`D19` 的標準是 1000，但那要約 70 秒，會吃掉"
                             "大半個時間預算。跑正式報告時請明確指定 1000")
    parser.add_argument("--real", action="store_true",
                        help="標示這批是真實資料。**預設是合成**——假資料是目前的"
                             "常態，預設 real 會讓忘記加旗標的人產出一份看起來像"
                             "真實結論的報告")
    return parser.parse_args(argv)


# ------------------------------------------------------------------ 特徵組裝


DEFAULT_TOF_RATE_HZ = 30.0


def _tof_row_to_optional(values_row, valid_row):
    """`Aligner`/`TofSample` 的慣例是無效 zone 填 `None`，不是 HDF5 裡的
    `NaN`（`host/align/aligner.py` 模組文件）——這裡把 `§2` 的 NaN 表示法
    轉成 `Aligner` 認得的表示法，authoritative 的是 `valid`，不是檢查
    `NaN`（跟真機的 `protocol.py` 對無效欄位一律回 `None` 是同一個規矩）。
    """
    return [float(v) if ok else None for v, ok in zip(values_row, valid_row)]


def _infer_tof_rate_hz(tof_t_us):
    """`Aligner.frames()` 需要一個輸出頻率；用這筆 trial 自己實測的 ToF
    幀間隔反推，而不是寫死 30——8×8 組態是 10 Hz（CONTRACTS.md §1.4），
    寫死 30 會讓那個組態的輸出格點跟實際取樣完全對不上。"""
    diffs = np.diff(np.asarray(tof_t_us, dtype=np.float64))
    median_us = float(np.median(diffs))
    if median_us <= 0:
        return DEFAULT_TOF_RATE_HZ
    return 1e6 / median_us


def _speech_window_for_trial(trial, session, mu_a, sigma_a, mu_b, sigma_b):
    """算這筆 trial 的裁切窗——唇動（A/B 兩顆）+ 語音，取聯集（完整理由見
    `host/features/live_pipeline.py` 的 `compute_speech_window()`）。

    `mic_t_us` 現在已經在 `Trial` 上（`session_loader.py` 的既有欄位，
    `18` 補上的），語音 VAD 因此跟線上路徑一樣可以算——不需要「offline
    只能唇動-only」的降級：那個不對稱本身會是新的「訓練/推論不一致」
    （樣板可能是別條路徑建的、查詢又是另一條），今天早上才修過同一種
    問題（見 `reports/ALIGNMENT_MISMATCH.md`）。
    """
    from host.features.live_pipeline import compute_speech_window
    from host.vad.audio_vad import DEFAULT_SPEAKING_MODE, SPEAKING_MODES, detect_voice_activity
    from host.vad.tof_vad import detect_lip_activity

    energy_mu = session.meta.get("energy_mu")
    energy_sigma = session.meta.get("energy_sigma")
    lip_a = detect_lip_activity(trial.tof_a, trial.tof_t_us, mu_a, sigma_a,
                                 energy_mu=energy_mu, energy_sigma=energy_sigma)
    lip_b = detect_lip_activity(trial.tof_b, trial.tof_t_us, mu_b, sigma_b,
                                 energy_mu=energy_mu, energy_sigma=energy_sigma)

    speaking_mode = trial.speaking_mode if trial.speaking_mode in SPEAKING_MODES else DEFAULT_SPEAKING_MODE
    voice = detect_voice_activity(
        trial.mic_rms, trial.mic_t_us,
        session.meta.get("noise_floor_mu"), session.meta.get("noise_floor_sigma"),
        speaking_mode=speaking_mode,
    )

    segments = []
    if lip_a.detected:
        segments.append(("lip_A", lip_a.primary.start_us, lip_a.primary.end_us))
    if lip_b.detected:
        segments.append(("lip_B", lip_b.primary.start_us, lip_b.primary.end_us))
    if voice.detected:
        segments.append(("voice", voice.primary.start_us, voice.primary.end_us))
    return compute_speech_window(segments)


def build_feature_seqs(trials, session_by_trial):
    """把 trial 走一遍 `D01` → `D02` → `D03`，回傳
    `(feature_seqs, labels, skipped, by_trial, trim_info)`。

    **跨模態對齊走 `host/align/aligner.py` 的 `Aligner` +
    `host/features/live_pipeline.py` 的 `assemble_query_from_aligned_frames()`
    ——跟線上推論唯一可能的路徑用同一份程式碼**，不是離線自己重寫一套。

    這裡原本是各模態各自算完 z-score/CMN 後用 `n = min(len(...))`
    按索引截斷——ToF 30 Hz、Mel 62.5 Hz，取樣率不同，同一個索引對應到的
    真實時間不一樣，而且落差隨索引線性增長。`feature_assembly
    .assemble_feature_seq()` 自己的模組文件明講輸入「必須已經由 B06 對到
    同一組共用幀」，舊寫法完全沒有做這件事，直接違反自己下游模組寫明的
    前提。量測見 `reports/ALIGNMENT_MISMATCH.md`——只看合併後的 104 維
    向量幾乎量不出差異（ToF 通道把 Mel 通道的落差稀釋掉了），拆開來看
    Mel-only 的 cosine 距離達 0.86–1.46（值域 0–2），且會讓分類結果翻轉。

    **這裡也裁切到「真的在講話」那一段**（`_speech_window_for_trial()`），
    修 hold-to-record 按鍵按多久會洩漏進固定 `T=24` 幀的問題（同一份報告
    「按住多久」章節）。`trim_info` 是每筆 trial 的診斷
    （`key`/`trimmed`/`source`/`reason`/`coverage`）——**裁切與否永遠被記錄**，不會
    有「一部分樣板裁過、一部分沒裁但沒人知道」的情況。

    ⚠️ **改這裡會讓 `D06`/`D09`/`D22` 的數字跟著變，這是預期的**——
    舊數字是拿一條違反自己前提、也沒有裁切的管線算出來的，不是「不小心
    變了」。

    任何一筆組裝失敗（幀數不足、缺 mel/`mel_t_us`、baseline 缺漏）就
    **跳過那一筆並記錄**，不是整批放棄——一次 session 幾十筆，為了一筆
    壞掉的丟掉全部太貴。`skipped` 是被跳過的清單（會寫進報告），`by_trial`
    是 `id(trial) -> 特徵`，給 `D12` 的距離比用。
    """
    from host.align.aligner import Aligner
    from host.features.live_pipeline import (
        InsufficientFramesError,
        assemble_query_from_aligned_frames,
    )

    feature_seqs, labels, skipped, by_trial, trim_info = [], [], [], {}, []
    for trial in trials:
        session = session_by_trial[id(trial)]
        mu_a, sigma_a = session.baseline("A")
        mu_b, sigma_b = session.baseline("B")
        if mu_a is None or mu_b is None:
            skipped.append((trial.key, "缺 baseline"))
            continue
        if trial.mel is None:
            skipped.append((trial.key, "沒有 mel dataset（§2 選填）"))
            continue
        if trial.mel_t_us is None:
            skipped.append((trial.key, "沒有 mel_t_us（§2 規定跟 mel 成對必寫，缺了就沒有真實時間可以對齊）"))
            continue
        n_tof = trial.tof_a.shape[0]
        n_mel = trial.mel.shape[0]
        if n_tof < 2 or n_mel < 2:
            skipped.append((trial.key, f"ToF/Mel 原生幀數不足以對齊（ToF={n_tof}, Mel={n_mel}）"))
            continue
        try:
            n_zones = trial.tof_valid_a.shape[1]
            aligner = Aligner()
            for i in range(n_tof):
                t_us = int(trial.tof_t_us[i])
                aligner.push_tof(
                    "A", t_us,
                    _tof_row_to_optional(trial.tof_a[i, :n_zones], trial.tof_valid_a[i]),
                    _tof_row_to_optional(trial.tof_a[i, n_zones:], trial.tof_valid_a[i]),
                    trial.tof_valid_a[i],
                )
                aligner.push_tof(
                    "B", t_us,
                    _tof_row_to_optional(trial.tof_b[i, :n_zones], trial.tof_valid_b[i]),
                    _tof_row_to_optional(trial.tof_b[i, n_zones:], trial.tof_valid_b[i]),
                    trial.tof_valid_b[i],
                )
            for i in range(n_mel):
                aligner.push_mel(int(trial.mel_t_us[i]), trial.mel[i])

            rate_hz = _infer_tof_rate_hz(trial.tof_t_us)
            frames = list(aligner.frames(
                int(trial.tof_t_us[0]), int(trial.tof_t_us[-1]), rate_hz=rate_hz,
            ))

            window_result = _speech_window_for_trial(trial, session, mu_a, sigma_a, mu_b, sigma_b)
            seq = assemble_query_from_aligned_frames(
                frames, mu_a, sigma_a, mu_b, sigma_b, speech_window=window_result,
            )
        except InsufficientFramesError as exc:
            skipped.append((trial.key, f"三個模態同時有資料的幀不足，無法對齊：{exc}"))
            continue
        except Exception as exc:                     # noqa: BLE001 — 逐筆容錯
            skipped.append((trial.key, f"{type(exc).__name__}: {exc}"))
            continue
        feature_seqs.append(seq.data)
        labels.append(trial.label)
        by_trial[id(trial)] = seq.data
        trim_info.append({
            "key": trial.key, "trimmed": window_result.trimmed,
            "source": window_result.source, "reason": window_result.reason,
            # `QueryAssembly.coverage`（`live_pipeline.py` 的 `SensorCoverage`）
            # 算好了，但沒有任何呼叫端把它接出來——`reports/DEGRADED_SESSION.md`
            # 指名的第三種失效形態（斷斷續續的感測器）**只有這個欄位看得到**：
            # 全程無資料看 `/meta` 的 `sensors_seen`，中途掉線看 per-trial
            # `sensors_seen`，斷斷續續兩者都看不出來，只有這裡的
            # `present_frames`/`usable_fraction` 才有。這裡只是把它帶出來，
            # **不訂門檻**——多低算不可用，故意留給看報告的人自己判斷
            # （見下方 `run_experiments()` 怎麼用它）。
            "coverage": {
                "tof_A": seq.coverage.fraction("tof_A"),
                "tof_B": seq.coverage.fraction("tof_B"),
                "mel": seq.coverage.fraction("mel"),
                "usable_fraction": seq.coverage.usable_fraction(),
            },
        })
    return feature_seqs, labels, skipped, by_trial, trim_info


# ------------------------------------------------------------------ 各實驗


def _skipped(key, reason):
    name, metric, criterion = EXPERIMENT_META[key]
    return ExperimentOutcome(key=key, name=name, metric=metric, measured="—",
                             criterion=criterion, status=STATUS_SKIPPED, reason=reason)


def _errored(key, exc):
    name, metric, criterion = EXPERIMENT_META[key]
    summary = f"{type(exc).__name__}: {exc}"
    return ExperimentOutcome(
        key=key, name=name, metric=metric, measured="—", criterion=criterion,
        status=STATUS_ERROR, reason=summary,
        diagnosis="完整 traceback：\n\n```\n" + traceback.format_exc().strip() + "\n```",
    )


def run_silhouette(feature_seqs, labels, *, fast, is_synthetic):
    from analysis.experiments import exp_c_silhouette as mod
    from analysis.reporting import effect_size

    n_components = 12 if fast else mod.DEFAULT_PCA_COMPONENTS
    report = mod.silhouette_report({"all": (feature_seqs, labels)},
                                   n_pca_components=n_components,
                                   is_synthetic=is_synthetic)
    table = report["modes"]["all"]["table"]
    complementarity = report["modes"]["all"]["complementarity"]
    score = table["all"]["score"]
    tof_score = table["tof_combined"]["score"]
    passed = mod.verdict_for_score(score) != "fail"

    # Silhouette 本身就是效果量（`effect_size.silhouette_interpretation()`
    # 只負責解釋尺度，不重新定義通過門檻——那個門檻仍然是
    # `verdict_for_score()`，這裡不重複也不覆蓋）。附上解讀文字，不是只有
    # 分數：口試委員問「這個數字代表什麼」時，這段話要能直接回答。
    effect_size_md = effect_size.format_effect_size_section(
        [("Silhouette（全模態）", effect_size.silhouette_interpretation(score)),
         ("Silhouette（ToF-only）", effect_size.silhouette_interpretation(tof_score))],
        title="效果量：這個分開有多明顯",
    )

    name, metric, criterion = EXPERIMENT_META["C"]
    return ExperimentOutcome(
        key="C", name=name, metric=metric,
        measured=f"{score:.3f}（ToF {tof_score:.3f}）",
        criterion=criterion,
        status=STATUS_PASS if passed else STATUS_FAIL,
        detail={
            "score": score, "tof_score": tof_score,
            "verdict": mod.verdict_for_score(score),
            # `complementarity_check()` 的鍵是 `passed`，不是 `complementary`。
            # 我一開始寫錯，`.get()` 安靜地回 `None`，**三方投票就少了一票而
            # 且沒有任何跡象**——那正是這個交叉檢查存在要防的事。現在
            # `_check_dual_matrix_agreement()` 會在票數不足時明講。
            "complementary": complementarity["passed"],
            # `tof_separable` 給跨實驗一致性檢查用：ToF 單獨是不是分得開。
            "tof_separable": mod.verdict_for_score(tof_score) != "fail",
        },
        diagnosis=None if passed else (
            "Silhouette 落在 fail 區間。**先懷疑維度詛咒，不要先懷疑資料或模型**"
            "（見 `exp_c_silhouette` 的模組說明）：分數普遍偏低通常代表訊號集中"
            "在少數 zone/band，攤平後被大量無關維度稀釋。可先試 `--fast`（較低的"
            " PCA 維度）看分數會不會回升。"
        ),
        report_md=mod.format_report(report) + "\n" + effect_size_md,
    )


def run_viseme(feature_seqs, labels, *, is_synthetic):
    from analysis.experiments import d14_viseme_sensitivity as mod

    samples = list(zip(labels, feature_seqs))
    report = mod.viseme_sensitivity_report(samples, is_synthetic=is_synthetic)
    check = report["fricative_check"]

    if check["pass"] is None:
        outcome_status, measured = STATUS_SKIPPED, "—"
    else:
        outcome_status = STATUS_PASS if check["pass"] else STATUS_FAIL
        measured = f"Mel {check['mel_mean']:.2f} vs ToF {check['tof_best_mean']:.2f}"

    name, metric, criterion = EXPERIMENT_META["E"]
    return ExperimentOutcome(
        key="E", name=name, metric=metric, measured=measured, criterion=criterion,
        status=outcome_status,
        reason=check.get("reason"),
        detail={
            "fricative_pass": check["pass"],
            "uniform_weak_tof": report["uniform_weak_tof"],
            "implausible_cells": report["implausible_cells"],
        },
        diagnosis=None if check["pass"] is not False else (
            "擦音（F，「四」）在 Mel 上沒有明顯強過 ToF。**這一格是「五／四」"
            "設計的核心**（§6）——它若不成立，多模態融合的論證就少了最直接的"
            "一半證據。先確認麥克風有收到聲音（`D12` 的 mic 特徵、`B15` 的底噪），"
            "再看是不是 Mel 特徵的問題。"
        ),
        report_md=mod.format_report(report),
        figures=(("viseme_sensitivity.png", lambda: mod.plot_viseme_sensitivity(report)),),
    )


def run_snr(sessions, trials, *, is_synthetic):
    """實驗 A：逐 zone SNR。

    需要「round」與「spread」兩種對照的唇形錄製。§6 的詞彙集裡「五」是圓唇
    （B）、「一」是展唇（C），所以用這兩個詞當對照——**這是本模組的對應，
    不是契約規定的**，見完成回報。
    """
    from analysis.experiments import exp_a_snr as mod

    round_word, spread_word = "wu", "yi"
    by_label = {}
    for trial in trials:
        by_label.setdefault(trial.label, []).append(trial)
    if round_word not in by_label or spread_word not in by_label:
        return _skipped("A", f"SNR 需要 `{round_word}`（圓唇）與 `{spread_word}`（展唇）"
                             f"兩種錄製當對照，目前的標籤是 {sorted(by_label)}")

    session = sessions[0]
    outcomes = {}
    for sensor, attr in (("A", "tof_a"), ("B", "tof_b")):
        mu, sigma = session.baseline(sensor)
        n_zones = len(mu) // 2
        rounds = [getattr(t, attr)[:, :n_zones] for t in by_label[round_word]]
        spreads = [getattr(t, attr)[:, :n_zones] for t in by_label[spread_word]]
        snr_zone = mod.zone_snr(mu[:n_zones], rounds, spreads)
        outcomes[sensor] = mod.overall_snr(snr_zone)

    verdict, action, detail = mod.three_way_verdict(outcomes["A"], outcomes["B"])
    passed = verdict == mod.VERDICT_PASS
    name, metric, criterion = EXPERIMENT_META["A"]
    return ExperimentOutcome(
        key="A", name=name, metric=metric,
        measured=f"{outcomes['A']:.2f} / {outcomes['B']:.2f}",
        criterion=criterion,
        status=STATUS_PASS if passed else STATUS_FAIL,
        detail={"verdict": verdict, **detail},
        diagnosis=None if passed else action,
    )


def run_wear_cv(trials, feature_by_trial, *, is_synthetic):
    """實驗 B：同次戴 vs 跨次戴的 CV。

    兩個層面各算一次（`D12` 兩者都要）：純量特徵的 CV，以及**同一個詞**在
    同次戴／跨次戴之間的兩兩距離比。後者需要特徵序列，拿不到就只回報 CV
    的部分——**不是整個實驗放棄**，CV 本身已經回答了驗收條件的門檻。
    """
    import pandas as pd

    from analysis.experiments import exp_d12_wear_cv as mod

    rows = []
    for trial in trials:
        # **傳完整的 (T, 2Z) 陣列**：`extract_scalar_features` 自己會切前半
        # 距離、後半 signal。先切成距離的話 signal 那半會變成 (T, 0)，
        # `np.where` 廣播就炸了（開發時真的踩到）。
        features = mod.extract_scalar_features(
            trial.tof_a, trial.tof_valid_a,
            trial.tof_b, trial.tof_valid_b,
            trial.mel if trial.mel is not None else np.zeros((1, 40)),
        )
        rows.append({"wear_id": trial.wear_id, **features})
    frame = pd.DataFrame(rows)

    verdicts = {}
    for column in frame.columns:
        if column == "wear_id":
            continue
        cv = mod.scalar_cv_within_between(frame, "wear_id", column)
        verdicts[column] = mod.wear_verdict(cv["cv_within"], cv["cv_between"])

    distance_result, distance_note = _wear_distance_ratio(trials, feature_by_trial)

    worst = max(verdicts.items(), key=lambda kv: kv[1]["cv_between"])
    passed = all(v["passed"] for v in verdicts.values())
    name, metric, criterion = EXPERIMENT_META["B"]
    diagnosis = None
    if not passed:
        diagnosis = "\n".join(f"* {item}" for item in mod.IMPROVEMENT_SUGGESTIONS)
    return ExperimentOutcome(
        key="B", name=name, metric=metric,
        measured=f"最差 {worst[0]} {worst[1]['cv_between']:.1%}",
        criterion=criterion,
        status=STATUS_PASS if passed else STATUS_FAIL,
        detail={"verdicts": verdicts, "worst_feature": worst[0],
                "distance_ratio": (distance_result or {}).get("ratio"),
                "distance_note": distance_note},
        diagnosis=diagnosis,
        report_md=(mod.format_report(verdicts, distance_result, is_synthetic)
                   if distance_result is not None else None),
    )


def _wear_distance_ratio(trials, feature_by_trial):
    """同一個詞在同次戴／跨次戴之間的兩兩距離比。

    需要「至少兩個 wear_id、且每個 wear_id 至少兩筆同一個詞」。條件不足時
    回 `(None, 原因)`——**不猜、不用別的詞湊數**：跨詞的距離量的是「不同的
    詞長得不一樣」，跟戴法重複性無關，混進來會讓比值完全失去意義。
    """
    from analysis.experiments import exp_d12_wear_cv as mod
    from analysis.similarity.cosine_baseline import cosine_dist

    by_label = {}
    for trial in trials:
        data = feature_by_trial.get(id(trial))
        if data is None:
            continue
        by_label.setdefault(trial.label, {}).setdefault(trial.wear_id, []).append(data)

    for label, by_wear in sorted(by_label.items()):
        usable = {wid: seqs for wid, seqs in by_wear.items() if len(seqs) >= 2}
        if len(usable) >= 2:
            try:
                return mod.distance_based_wear_ratio(usable, cosine_dist), None
            except ValueError as exc:
                return None, f"距離比算不出來（{exc}）"
    return None, ("距離比需要同一個詞在至少 2 個 wear_id、每個 wear_id 至少 2 筆；"
                  "目前沒有任何一個詞滿足。**不用別的詞湊數**——跨詞的距離量的是"
                  "「不同的詞長得不一樣」，跟戴法重複性無關。")


def run_crosstalk(pairs, diagnosis, *, is_synthetic):
    """實驗 `C0`：串擾。比較「只開一顆」與「兩顆都開」兩次錄製的差異。

    ## 覆蓋率會寫進矩陣的「實測」欄，不是藏在備註裡

    一組 solo 錄製只回答一個方向的問題：`solo="A"` 量的是**「B 開著會不會
    干擾 A」**。要兩個方向都答，需要 `A` 與 `B` 各一組 solo 錄製。

    只量到一邊時，矩陣那一列會寫成 `A: 1.30 mm（B 未量測）`——
    **覆蓋率必須出現在使用者會掃的那一欄**。一個看起來很正常的 `✓ PASS`
    配上藏在別處的「其實只驗了一半」，跟沒講一樣。
    """
    from analysis.experiments import exp_d10_crosstalk as mod

    verdicts, ambient_rates, notes = {}, {}, []
    for pair in pairs:
        sensor = pair.solo_sensor
        if sensor in verdicts:
            continue                        # 同一顆有多組配對，取第一組就好
        solo_dist, solo_valid = pair.solo.stacked_tof(sensor)
        dual_dist, dual_valid = pair.dual.stacked_tof(sensor)
        if solo_dist is None or dual_dist is None:
            notes.append(f"感測器 {sensor} 的配對缺 ToF 資料，跳過")
            continue
        delta = mod.zone_distance_delta(solo_dist, solo_valid, dual_dist, dual_valid)
        verdicts[sensor] = mod.crosstalk_verdict(delta)

        solo_amb = pair.solo.stacked_ambient(sensor)
        dual_amb = pair.dual.stacked_ambient(sensor)
        if solo_amb is not None and dual_amb is not None:
            _, rate = mod.zone_ambient_delta(solo_amb, dual_amb)
            ambient_rates[sensor] = rate

    if not verdicts:
        return _skipped("C0", "配對到了但兩邊都沒有可用的 ToF 資料："
                              + session_loader.describe_crosstalk_gap(diagnosis))

    missing = [s for s in ("A", "B") if s not in verdicts]
    worst = max(verdicts.items(), key=lambda kv: kv[1]["worst_delta_mm"])
    measured = "、".join(f"{s}: {v['worst_delta_mm']:.2f} mm"
                         for s, v in sorted(verdicts.items()))
    if missing:
        measured += f"（{'/'.join(missing)} 未量測）"

    passed = all(v["passed"] for v in verdicts.values())
    if not diagnosis["any_unconfirmed"]:
        confirmation_note = None
    else:
        confirmation_note = (
            "⚠️ 這組配對是基於 **未經裝置確認** 的感測器狀態："
            "`sensors_enabled` 來自主機端記錄的 `SENS:` 指令，"
            "而 `$STATUS` 沒有 `sens_a=`/`sens_b=` 可以回報實際狀態（§4.1.2）。"
            "**指令送出去不等於裝置照做了。**"
        )

    reasons = notes[:]
    if missing:
        reasons.append(
            f"只量到感測器 {'/'.join(sorted(verdicts))} 的方向。"
            f"要驗 {'/'.join(missing)} 那個方向，需要在同一次戴上多錄一段"
            f"「只開 {'/'.join(missing)}」的資料。"
        )
    if confirmation_note:
        reasons.append(confirmation_note)

    name, metric, criterion = EXPERIMENT_META["C0"]
    return ExperimentOutcome(
        key="C0", name=name, metric=metric, measured=measured, criterion=criterion,
        status=STATUS_PASS if passed else STATUS_FAIL,
        reason="；".join(reasons) if reasons else None,
        detail={
            "verdicts": {s: dict(v) for s, v in verdicts.items()},
            "measured_sensors": sorted(verdicts),
            "missing_sensors": missing,
            "sensors_confirmed": not diagnosis["any_unconfirmed"],
            "n_pairs": diagnosis["n_pairs"],
            "has_ambient": sorted(ambient_rates),
        },
        diagnosis=None if passed else (
            f"感測器 {worst[0]} 的最差 zone（#{worst[1]['worst_zone']}）"
            f"距離差 {worst[1]['worst_delta_mm']:.2f} mm，超過 "
            f"{worst[1]['threshold_mm']:.1f} mm 門檻。"
            + ("\n\n" + mod.FALLBACK_RECOMMENDATION
               if hasattr(mod, "FALLBACK_RECOMMENDATION") else "")
        ),
        report_md=_crosstalk_markdown(mod, verdicts, ambient_rates, missing,
                                      confirmation_note, is_synthetic),
    )


def _crosstalk_markdown(mod, verdicts, ambient_rates, missing, confirmation_note,
                        is_synthetic):
    """`C0` 的報告。

    `exp_d10_crosstalk.format_report()` 要求 A/B 兩顆的 verdict **與** ambient
    變化率**都在**。實務上 ambient（`$A` 幀）是新加的，多數資料還沒有——
    **不能為了呼叫它而餵零進去**，那是捏造一個「ambient 完全沒變」的結論。
    所以齊全時用它的完整報告，不齊全時產一份講清楚缺什麼的簡版。
    """
    if len(verdicts) == 2 and len(ambient_rates) == 2:
        return mod.format_report(verdicts["A"], verdicts["B"],
                                 ambient_rates["A"], ambient_rates["B"], is_synthetic)

    lines = ["# C0 — Crosstalk 分析（部分覆蓋）", ""]
    if is_synthetic:
        lines += ["> ⚠️ **本報告使用合成資料，數字不是真實結論。**", ""]
    lines += ["| 感測器 | 最差 zone | 距離差 | 門檻 | 判定 |", "|---|---|---|---|---|"]
    for sensor, verdict in sorted(verdicts.items()):
        lines.append(
            f"| {sensor} | #{verdict['worst_zone']} | "
            f"{verdict['worst_delta_mm']:.2f} mm | {verdict['threshold_mm']:.1f} mm | "
            f"{'✓ PASS' if verdict['passed'] else '✗ FAIL'} |")
    lines.append("")
    if missing:
        lines += [f"⚠️ **感測器 {'/'.join(missing)} 的方向沒有量到。** "
                  "一組 solo 錄製只回答一個方向：`solo=A` 量的是「B 會不會干擾 A」。"
                  "兩個方向都要驗的話，同一次戴上要各錄一段。", ""]
    if not ambient_rates:
        lines += ["⚠️ **沒有 ambient 資料**（`tof_ambient_*`，§2 選填）。"
                  "`D10` 明訂 `ambient_per_spad` 是 crosstalk **最靈敏**的指標——"
                  "只看距離差可能漏掉還沒大到 2 mm、但 ambient 已經明顯上升的干擾。"
                  "需要韌體開 `AMB:1`。", ""]
    elif len(ambient_rates) < 2:
        lines += [f"⚠️ 只有感測器 {'/'.join(sorted(ambient_rates))} 有 ambient 資料。", ""]
    if confirmation_note:
        lines += ["> " + confirmation_note, ""]
    return "\n".join(lines)


# ------------------------------------------ 側邊實驗（不進通過矩陣，餵一致性檢查）


def run_mutual_information(feature_seqs, labels):
    """`D16` 的雙矩陣資訊增益，供「第二顆 ToF 有沒有用」的三方投票。

    **不進通過矩陣**——story 的 範圍 只列了五個實驗。它存在的唯一理由是讓
    `_check_dual_matrix_agreement()` 有第二個意見可比；沒有它，那個交叉檢查
    永遠只有一票，也就永遠不會發現矛盾。
    """
    from analysis.experiments import d16_mutual_information as mod

    table = mod.mutual_information_table(feature_seqs, labels)
    gain = mod.dual_matrix_gain(table)
    return gain["gain"], ("d16_mutual_information", mod.format_report(table))


def run_ablation(feature_seqs, labels, *, n_permutations, cv=3):
    """`D19` 消融套件的 `dual_matrix_vs_single`，同樣供三方投票用。

    置換次數預設調低（見 `--ablation-permutations` 的說明）：完整的 1000 次
    要約 70 秒，會吃掉大半個時間預算，而這裡只需要它的**方向**（拿掉第二顆
    有沒有掉），不是它的 p 值精度。**正式報告請用 1000。**
    """
    from analysis.experiments import d19_ablation_suite as mod

    suite = mod.run_ablation_suite(feature_seqs, labels,
                                   n_permutations=n_permutations, cv=cv)
    return suite["dual_matrix_vs_single"], ("d19_ablation", mod.format_report(suite))


def run_d18_permutation(feature_seqs, labels, wear_ids, *, n_permutations, is_synthetic):
    """`D18` 置換檢定，**帶 `wear_id` 分組**。

    `esp-mask-test-7c` 已經在 `d18_permutation_test.py` 做好分組驗證
    （`groups=`），但 `run_all.py` 從來沒有呼叫過這支模組——這裡是第一次
    接上，不是「修一個接錯的參數」。`wear_ids` 直接傳給
    `permutation_report(groups=...)`，三種分組狀態（`grouped`／
    `ungrouped_no_groups_given`／`ungrouped_single_group`）完全由那支
    模組自己的 `_resolve_grouping()` 決定，這裡不做任何預先判斷或過濾
    ——**尤其不能因為「看起來只有一個 wear_id」就自己決定不傳，那樣
    `grouping` 永遠是 `ungrouped_no_groups_given`（沒要求過），會蓋掉
    `ungrouped_single_group`（要求了但做不到）這個更重要的警訊**。

    置換次數沿用 `--ablation-permutations`（跟 `D19` 共用同一個時間預算
    旋鈕，理由相同：完整 1000 次要約 70 秒）。

    **也接上 `analysis/reporting/effect_size.py`**（只 import，沒有改那支
    檔案）：附上準確率的 Wilson CI（跟機率基準比較用 CI 下界，不是點估計）
    與置換檢定的標準化效果量 z。回傳
    `(grouping, (slug, markdown), extra_notes)`——比 `D16`/`D19` 多一個
    `extra_notes`，因為「p 值顯著但 CI 下界沒蓋過機率基準」這個不一致
    必須直接出現在 `summary.md`，不能只藏在 side report 裡。
    """
    from analysis.experiments import d18_permutation_test as mod

    report = mod.permutation_report(
        feature_seqs, labels, n_permutations=n_permutations,
        is_synthetic=is_synthetic, groups=wear_ids,
    )
    grouping = report["all"]["grouping"]
    effect_size_md, extra_notes = d18_effect_size_section(report, n=len(labels),
                                                           n_classes=len(set(labels)))
    markdown = mod.format_report(report) + "\n" + effect_size_md
    return grouping, ("d18_permutation", markdown), extra_notes


def d18_effect_size_section(report, *, n, n_classes):
    """把 `d18_permutation_test.permutation_report()` 的結果轉成效果量
    小節（Wilson CI + 標準化效果量 z），拆成獨立函式**只是為了能直接單元
    測試「p 值顯著但 CI 下界沒蓋過機率基準」這個不一致偵測**——不需要真的
    跑一次 sklearn CV 才能驗證這條規則有沒有接對。

    回傳 `(markdown, extra_notes)`。
    """
    from analysis.reporting import effect_size

    entries = []
    extra_notes = []
    for label, key in (("全模態", "all"), ("ToF-only", "tof_only")):
        r = report[key]
        # `score` 是這輪 CV 的平均準確率，不是單一二項式試驗的 k/n——
        # 用 `round(score * n)` 反推一個近似的 k，供 `accuracy_with_ci()`
        # 算信賴區間用。這是近似（CV 折之間不獨立），跟 `cohens_d()` 那條
        # 限制說明是同一種精神：夠好用來判斷「CI 下界有沒有蓋過機率基準」，
        # 不是精確的二項式檢定。
        k = round(r["score"] * n)
        acc = effect_size.accuracy_with_ci(k, n, n_classes=n_classes)
        entries.append((f"{label} 準確率（近似）", acc))
        entries.append((f"{label} 置換效果量 z", effect_size.permutation_effect_size(
            r["score"], r["permutation_scores"])))

        # 🔴 這是 7c 點名要接的那條規則：p 值顯著（`passed`）不等於
        # CI 下界蓋過機率基準（`above_chance`）——兩者用不同的統計量，
        # 可能得出不同結論。兩者不一致時必須明講，不能只印比較好看的那個。
        if r["passed"] and acc["above_chance"] is False:
            extra_notes.append(
                f"🔴 D18（{label}）：p 值顯著（p={r['pvalue']:.4f} < 0.01），"
                f"但準確率信賴區間下界（{acc['ci_lower']:.1%}）沒有蓋過機率基準"
                f"（{acc['chance_level']:.1%}）——**不能同時宣稱「統計上顯著」跟"
                f"「贏過隨機猜」**，見 d18_permutation.md 的效果量小節。"
            )

    markdown = effect_size.format_effect_size_section(entries, title="效果量：這個顯著性有多大")
    return markdown, extra_notes


# ------------------------------------------------------------------ 主流程


def run_experiments(sessions, *, fast=False, is_synthetic=True,
                    ablation_permutations=200):
    """跑所有跑得動的實驗。回傳 `(outcomes, extras, notes)`。

    `extras` 裝的是**不在通過矩陣裡**的側邊結果：`D16` 的資訊增益、`D19`
    的消融（兩者參與跨實驗一致性檢查，理由見 `run_mutual_information()`）、
    `D18` 的置換檢定分組狀態（`"d18_grouping"`，不參與那個一致性檢查，
    純粹是「這一輪的準確率數字能不能信」的旗標——見 `run_d18_permutation()`）。
    """
    available = session_loader.availability(sessions)
    pairs = session_loader.usable_trials(sessions)
    trials = [trial for _, trial in pairs]
    session_by_trial = {id(trial): session for session, trial in pairs}

    outcomes, notes = [], []
    feature_seqs, labels, feature_by_trial = [], [], {}
    if available["C"] is None or available["E"] is None or available["B"] is None:
        feature_seqs, labels, skipped, feature_by_trial, trim_info = build_feature_seqs(
            trials, session_by_trial)
        if skipped:
            notes.append(f"{len(skipped)} 筆 trial 未能組裝成特徵："
                         + "、".join(f"{k}（{why}）" for k, why in skipped[:5])
                         + ("…" if len(skipped) > 5 else ""))
        if trim_info:
            n_trimmed = sum(1 for t in trim_info if t["trimmed"])
            note = (f"{n_trimmed}/{len(trim_info)} 筆 trial 有裁切到「真的在講話」那一段"
                    "（見 reports/ALIGNMENT_MISMATCH.md「按住多久」章節）")
            untrimmed = [t for t in trim_info if not t["trimmed"]]
            if untrimmed:
                note += "；退回整段的：" + "、".join(
                    f"{t['key']}（{t['reason']}）" for t in untrimmed[:5]
                ) + ("…" if len(untrimmed) > 5 else "")
            notes.append(note)

        # `SensorCoverage`（`live_pipeline.py`）算好了三個模態各自的資料
        # 覆蓋率，這裡只是把它帶出來讓人看得到，**不訂通過/不通過的門檻**
        # ——`reports/DEGRADED_SESSION.md` 指名「斷斷續續」這種失效形態
        # 只有這個數字看得出來（全程無資料看 `sensors_seen`、中途掉線看
        # per-trial `sensors_seen`，兩者都看不出「忽有忽無」）。列最低的
        # 幾筆，不代表「這幾筆不能用」，只是讓人知道去哪裡看。
        with_coverage = [t for t in trim_info if t.get("coverage")]
        if with_coverage:
            worst = sorted(with_coverage, key=lambda t: t["coverage"]["usable_fraction"])[:5]
            notes.append(
                "各 trial 的模態資料覆蓋率（tof_A/tof_B/mel 個別 present 比例、"
                "usable = 三者同時有資料的比例，1.0 = 全程都有；不代表通過/"
                "不通過，數字直接列出）：最低的幾筆 " + "；".join(
                    f"{t['key']}（tof_A={t['coverage']['tof_A']:.0%}, "
                    f"tof_B={t['coverage']['tof_B']:.0%}, "
                    f"mel={t['coverage']['mel']:.0%}, "
                    f"usable={t['coverage']['usable_fraction']:.0%}）"
                    for t in worst
                )
            )

    crosstalk, crosstalk_diagnosis = session_loader.crosstalk_pairs(sessions)
    runners = {
        "C0": lambda: run_crosstalk(crosstalk, crosstalk_diagnosis,
                                    is_synthetic=is_synthetic),
        "A": lambda: run_snr(sessions, trials, is_synthetic=is_synthetic),
        "B": lambda: run_wear_cv(trials, feature_by_trial, is_synthetic=is_synthetic),
        "C": lambda: run_silhouette(feature_seqs, labels, fast=fast,
                                    is_synthetic=is_synthetic),
        "E": lambda: run_viseme(feature_seqs, labels, is_synthetic=is_synthetic),
    }

    extras, side_reports = {}, []
    for key, runner in runners.items():
        reason = available.get(key)
        if reason is not None:
            outcomes.append(_skipped(key, reason))
            continue
        if key in ("C", "E") and len(feature_seqs) < 2:
            outcomes.append(_skipped(key, "可用的特徵序列不足 2 筆"))
            continue
        try:
            outcomes.append(runner())
        except Exception as exc:                     # noqa: BLE001 — 逐實驗容錯
            outcomes.append(_errored(key, exc))

    # `wear_id` 跟 `feature_seqs`/`labels` 對齊：`build_feature_seqs()`
    # 逐一走訪 `trials`，成功的才 append，順序不變——所以「有成功組出特徵
    # 的那些 trial」用同樣的過濾條件重建，順序就跟 `feature_seqs` 對得上，
    # 不需要再讓 `build_feature_seqs()` 多回傳一個列表。
    wear_ids_for_features = [t.wear_id for t in trials if id(t) in feature_by_trial]

    # 側邊實驗需要跟 `C`/`E` 同一批特徵；特徵不足就跳過，並在備註講明
    # ——**不是靜靜地讓一致性檢查永遠只有一票**。
    if len(feature_seqs) >= 2 and len(set(labels)) >= 2:
        for name, extras_key, skip_consequence, runner in (
            ("D16 雙矩陣資訊增益", "d16_gain", "「第二顆 ToF 有沒有用」的三方投票少一票",
             lambda: run_mutual_information(feature_seqs, labels)),
            ("D19 消融", "d19_dual_matrix", "「第二顆 ToF 有沒有用」的三方投票少一票",
             (lambda: run_ablation(feature_seqs, labels, n_permutations=ablation_permutations))
             if ablation_permutations > 0 else None),
            ("D18 置換檢定", "d18_grouping", "無法確認這批資料的分類顯著性是否可信",
             (lambda: run_d18_permutation(feature_seqs, labels, wear_ids_for_features,
                                           n_permutations=ablation_permutations,
                                           is_synthetic=is_synthetic))
             if ablation_permutations > 0 else None),
        ):
            if runner is None:
                notes.append(f"{name}未執行（--ablation-permutations 0）；{skip_consequence}")
                continue
            try:
                if extras_key == "d18_grouping":
                    # D18 多回傳一個 `extra_notes`——「p 值顯著但 CI 下界
                    # 沒蓋過機率基準」這個不一致必須直接進 `notes`，
                    # 不能只藏在 side report 裡，D16/D19 沒有這個需求。
                    value, report, extra_notes = runner()
                else:
                    value, report = runner()
                    extra_notes = []
            except Exception as exc:                 # noqa: BLE001 — 側邊實驗容錯
                notes.append(f"{name} 執行失敗（{type(exc).__name__}: {exc}）；{skip_consequence}")
                continue
            extras[extras_key] = value
            side_reports.append(report)
            notes.extend(extra_notes)
            if extras_key == "d18_grouping":
                # 🔴 這個狀態必須在 summary.md 上就看得到，不能只藏在
                # side report 裡——尤其 `ungrouped_single_group`（要求了
                # 分組但只戴過一次做不到）是使用者第一批資料最可能落入
                # 的狀態，而它「看起來」很容易被誤讀成「跟真的分組一樣」。
                if value == "grouped":
                    notes.append("D18 置換檢定：已用 wear_id 做分組驗證（StratifiedGroupKFold）。")
                elif value == "ungrouped_single_group":
                    notes.append(
                        "🔴 D18 置換檢定：要求了分組驗證，但這批資料只有 1 個 "
                        "wear_id（例如只戴過一次），分組驗證無法進行——這一輪"
                        "的準確率與 p 值可能被同一次戴上的組內洩漏灌水，"
                        "見 d18_permutation.md。"
                    )
                else:
                    notes.append(
                        "D18 置換檢定：未做分組驗證（沒有提供 wear_id）。"
                    )
    else:
        notes.append("特徵序列不足，`D16`/`D18`/`D19` 未執行；"
                     "「第二顆 ToF 有沒有用」的三方投票沒有任何來源")

    return outcomes, extras, notes, side_reports


def write_outputs(report, out_dir, notes=(), side_reports=()):
    """寫出 `summary.md` / `summary.html` / 各實驗的 md / `figures/`。

    圖一律走 `plot_style.save_figure()`：同一套樣式、PNG(300dpi) + PDF 雙輸出，
    並在存檔當下檢查「英文 only」與「灰階可辨」（`D20`）。**在存檔的當下檢查
    最容易修**——等到論文排版才發現一張圖印出來看不懂，要回頭重跑整個實驗。
    """
    from analysis.reporting.plot_style import save_figure

    out_dir = Path(out_dir)
    (out_dir / "figures").mkdir(parents=True, exist_ok=True)

    summary = render_summary_markdown(report)
    if notes:
        summary += "\n## 執行備註\n\n" + "\n".join(f"* {n}" for n in notes) + "\n"
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    (out_dir / "summary.html").write_text(render_summary_html(report), encoding="utf-8")

    written = [out_dir / "summary.md", out_dir / "summary.html"]
    for outcome in report["outcomes"]:
        if outcome.report_md:
            # 檔名用 ASCII slug：CJK 與空格在跨平台、URL 與 shell 補全上
            # 都會出事，而這些檔案會被 `C23` 用相對路徑引用。
            path = out_dir / f"{EXPERIMENT_SLUGS[outcome.key]}.md"
            path.write_text(outcome.report_md, encoding="utf-8")
            written.append(path)
        for filename, make_figure in outcome.figures:
            figure = make_figure()
            stem = out_dir / "figures" / Path(filename).stem
            written += save_figure(figure, stem)
            try:
                import matplotlib.pyplot as plt

                plt.close(figure)
            except Exception:                        # noqa: BLE001 — 關圖失敗無所謂
                pass

    for slug, markdown in side_reports:
        path = out_dir / f"{slug}.md"
        path.write_text(markdown, encoding="utf-8")
        written.append(path)
    return written


def main(argv=None):
    args = parse_args(argv)

    sessions = []
    for path in args.session:
        try:
            sessions.append(session_loader.load_session(path))
        except (OSError, ValueError) as exc:
            print(f"讀不到 session {path}：{exc}", file=sys.stderr)
            return EXIT_BAD_INPUT

    apply_style()          # D20：所有圖共用同一套樣式

    started = time.perf_counter()
    outcomes, extras, notes, side_reports = run_experiments(
        sessions, fast=args.fast, is_synthetic=not args.real,
        ablation_permutations=args.ablation_permutations)
    elapsed = time.perf_counter() - started

    if elapsed > args.time_budget:
        notes.append(
            f"執行時間 {elapsed:.1f} 秒超過 {args.time_budget:.0f} 秒的目標；"
            "可以加 `--fast` 降低 PCA 維度換取速度（數字會略有出入）。"
        )
        print(notes[-1], file=sys.stderr)

    report = build_report(outcomes, is_synthetic=not args.real, extras=extras,
                          session_paths=[s.path for s in sessions],
                          elapsed_s=elapsed)
    written = write_outputs(report, args.out, notes, side_reports)

    print(f"報告已寫入 {Path(args.out).resolve()}（{len(written)} 個檔案，"
          f"{elapsed:.1f} 秒）")
    for row in report["matrix"]:
        print(f"  {row['mark']:<12} {row['key']:<3} {row['name']}  {row['measured']}")

    if report["blocking"]:
        print("必通過項目失敗，見 summary.md 頂端的警示。", file=sys.stderr)
        return EXIT_MUST_PASS_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
