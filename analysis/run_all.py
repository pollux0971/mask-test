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


def build_feature_seqs(trials, session_by_trial):
    """把 trial 走一遍 `D01` → `D02` → `D03`，回傳 `(feature_seqs, labels)`。

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

    ⚠️ **改這裡會讓 `D06`/`D09`/`D22` 的數字跟著變，這是預期的**——
    舊數字是拿一條違反自己前提的管線算出來的，不是「不小心變了」。

    任何一筆組裝失敗（幀數不足、缺 mel/`mel_t_us`、baseline 缺漏）就
    **跳過那一筆並記錄**，不是整批放棄——一次 session 幾十筆，為了一筆
    壞掉的丟掉全部太貴。回傳 `(feature_seqs, labels, skipped, by_trial)`
    ——`skipped` 是被跳過的清單（會寫進報告），`by_trial` 是
    `id(trial) -> 特徵`，給 `D12` 的距離比用。
    """
    from host.align.aligner import Aligner
    from host.features.live_pipeline import (
        InsufficientFramesError,
        assemble_query_from_aligned_frames,
    )

    feature_seqs, labels, skipped, by_trial = [], [], [], {}
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
            seq = assemble_query_from_aligned_frames(frames, mu_a, sigma_a, mu_b, sigma_b)
        except InsufficientFramesError as exc:
            skipped.append((trial.key, f"三個模態同時有資料的幀不足，無法對齊：{exc}"))
            continue
        except Exception as exc:                     # noqa: BLE001 — 逐筆容錯
            skipped.append((trial.key, f"{type(exc).__name__}: {exc}"))
            continue
        feature_seqs.append(seq.data)
        labels.append(trial.label)
        by_trial[id(trial)] = seq.data
    return feature_seqs, labels, skipped, by_trial


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

    n_components = 12 if fast else mod.DEFAULT_PCA_COMPONENTS
    report = mod.silhouette_report({"all": (feature_seqs, labels)},
                                   n_pca_components=n_components,
                                   is_synthetic=is_synthetic)
    table = report["modes"]["all"]["table"]
    complementarity = report["modes"]["all"]["complementarity"]
    score = table["all"]["score"]
    tof_score = table["tof_combined"]["score"]
    passed = mod.verdict_for_score(score) != "fail"

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
        report_md=mod.format_report(report),
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


# ------------------------------------------------------------------ 主流程


def run_experiments(sessions, *, fast=False, is_synthetic=True,
                    ablation_permutations=200):
    """跑所有跑得動的實驗。回傳 `(outcomes, extras, notes)`。

    `extras` 裝的是**不在通過矩陣裡、但參與跨實驗一致性檢查**的結果
    （`D16` 的資訊增益、`D19` 的消融）。它們跟五個主實驗分開的理由見
    `run_mutual_information()`。
    """
    available = session_loader.availability(sessions)
    pairs = session_loader.usable_trials(sessions)
    trials = [trial for _, trial in pairs]
    session_by_trial = {id(trial): session for session, trial in pairs}

    outcomes, notes = [], []
    feature_seqs, labels, feature_by_trial = [], [], {}
    if available["C"] is None or available["E"] is None or available["B"] is None:
        feature_seqs, labels, skipped, feature_by_trial = build_feature_seqs(
            trials, session_by_trial)
        if skipped:
            notes.append(f"{len(skipped)} 筆 trial 未能組裝成特徵："
                         + "、".join(f"{k}（{why}）" for k, why in skipped[:5])
                         + ("…" if len(skipped) > 5 else ""))

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

    # 側邊實驗需要跟 `C`/`E` 同一批特徵；特徵不足就跳過，並在備註講明
    # ——**不是靜靜地讓一致性檢查永遠只有一票**。
    if len(feature_seqs) >= 2 and len(set(labels)) >= 2:
        for name, runner in (
            ("D16 雙矩陣資訊增益", lambda: run_mutual_information(feature_seqs, labels)),
            ("D19 消融", (lambda: run_ablation(feature_seqs, labels,
                                               n_permutations=ablation_permutations))
                          if ablation_permutations > 0 else None),
        ):
            if runner is None:
                notes.append("D19 消融未執行（--ablation-permutations 0）；"
                             "「第二顆 ToF 有沒有用」的三方投票少一票")
                continue
            try:
                value, report = runner()
            except Exception as exc:                 # noqa: BLE001 — 側邊實驗容錯
                notes.append(f"{name} 執行失敗（{type(exc).__name__}: {exc}）；"
                             "跨實驗一致性檢查會少一個來源")
                continue
            key = "d16_gain" if name.startswith("D16") else "d19_dual_matrix"
            extras[key] = value
            side_reports.append(report)
    else:
        notes.append("特徵序列不足，`D16`/`D19` 未執行；"
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
