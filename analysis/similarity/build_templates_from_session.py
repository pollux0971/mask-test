"""建樣板：把已經錄好的 session（一或多個 HDF5 檔）變成
`RecognitionService`/`GET /templates`/`POST /recognize` 可以直接載入的
`templates/<subject>_<wear_id>.npz`。

`D08.md`（Enrollment 樣板管理）範圍裡「樣板存/載」的存檔機制
（`analysis/similarity/enrollment.py` 的 `save_templates`/`load_templates`/
`template_path`）已經存在且測試過，但「把真的錄音變成樣板」這個串接
動作——不管是命令列還是面板——從沒有人寫過。`D08` 完整範圍還包含 LOOCV、
逐樣板貢獻度分析、壞樣板標記與重錄介面（配合 `C14`），那些卡在
`C20`/`D09`/`E06` 依賴鏈，**不在這支腳本範圍內**——這支腳本只做「錄音 →
樣板」這一段，讓 `/recognize` 有東西可以吃。

🔴 **每一筆樣板都用跟 `POST /recognize` 完全同一條路組出來**：
`host/align/aligner.Aligner` + `host/features/live_pipeline.
assemble_query_from_aligned_frames`。`$T` 跟 `$F` 幀率不同
（`CONTRACTS.md` 早就警告過），如果建樣板跟線上推論用不同對齊邏輯，
準確率會系統性偏差，而且不會有任何錯誤訊息——這正是
`esp-mask-test-59` 讀程式碼比對兩條路徑後發現的，這支腳本刻意避開它。

⚠️ **這裡原本還寫著「不是 `analysis/run_all.py` 的 `build_feature_seqs()`，
那邊直接把 tof_a/tof_b/mel 截斷到最短長度」——這句話現在過時了**：
`run_all.py` 自己後來也改成走 `Aligner`+`assemble_query_from_aligned_frames()`
這條真正的時間對齊路（見該檔案的 `build_feature_seqs()` 模組說明、
`reports/ALIGNMENT_MISMATCH.md`），不再是索引截斷。兩支腳本現在用的是
同一條對齊邏輯，只是各自獨立呼叫，不是同一份程式碼——這句提醒留著是
為了記錄「兩邊必須用同一種對齊方式」這個要求本身，不是說兩支腳本現在
還在用不同的方法。

用 `analysis/reporting/session_loader.py` 讀 HDF5（不直接開 `h5py`——那樣
布林值會變成 `numpy.bool_`，`session_loader` 已經處理過這個轉換）。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

from analysis.reporting.session_loader import load_session, usable_trials
from analysis.similarity.enrollment import save_templates
from host.align.aligner import Aligner
from host.features.live_pipeline import InsufficientFramesError, assemble_query_from_aligned_frames

# 樣板數低於這個值時，LOOCV/準確率這類「留一筆出來測」的統計不可靠——
# n=1 時分母是 0，算出來的數字是 nan，不是「還沒錄夠」的 0%，也不是「還
# 不錯」；n=2 時只有兩種可能結果（0% 或 100%），噪聲極大。這裡不跑 LOOCV
# （D08 的範圍，這支腳本不做），只在建完樣板時把這個限制講清楚。
MIN_TEMPLATES_WARN = 3
RECOMMENDED_TEMPLATES = 30  # reports/HANDOFF.md §3.1：D22 實測 n>=30 時誤拒率 <=1.1%


def _tof_row_to_lists(tof, valid_row, n_zones):
    """session_loader.Trial 的無效值是 NaN（跟 HDF5 schema 一致）；
    Aligner/AlignedFrame 的慣例是 None -- 這裡做那個轉換，僅此而已。"""
    distance = [None if np.isnan(v) else float(v) for v in tof[:n_zones]]
    signal = [None if np.isnan(v) else float(v) for v in tof[n_zones:]]
    return distance, signal


def _aligner_for_trial(trial):
    aligner = Aligner()
    n_zones = trial.n_zones
    for i in range(len(trial.tof_t_us)):
        d_a, s_a = _tof_row_to_lists(trial.tof_a[i], trial.tof_valid_a[i], n_zones)
        aligner.push_tof("A", int(trial.tof_t_us[i]), d_a, s_a, [bool(v) for v in trial.tof_valid_a[i]])
        d_b, s_b = _tof_row_to_lists(trial.tof_b[i], trial.tof_valid_b[i], n_zones)
        aligner.push_tof("B", int(trial.tof_t_us[i]), d_b, s_b, [bool(v) for v in trial.tof_valid_b[i]])
    for i in range(len(trial.mel_t_us)):
        aligner.push_mel(int(trial.mel_t_us[i]), [float(v) for v in trial.mel[i]])
    return aligner


# 低於這個覆蓋率才值得讓使用者看到——**不是拒絕的門檻**。真機上 ToF-A
# 會間歇性斷線（接頭接觸不良，2026-08-26 現場確認，沒有重置線可以重打，
# 4 小時 E05 錄製過程中隨時可能再發生）；`union_min` 融合的整個設計前提
# 就是一顆感測器可能瞎掉，這裡不擅自決定「少一顆就丟掉這筆樣板」，只負責
# 讓這件事被看見（見 host/features/live_pipeline.py 的 SensorCoverage）。
LOW_COVERAGE_WARN_THRESHOLD = 0.9


def build_template_vector(session, trial):
    """One trial -> one (T,104) template vector + its SensorCoverage, via
    the exact same Aligner + live_pipeline path POST /recognize uses
    (bridge_server.py's _handle_recognize / _frames_from_live_session /
    _frames_from_stored_trial)."""
    if trial.mel is None or trial.mel_t_us is None:
        raise ValueError(f"{trial.key}: 沒有 mel/mel_t_us（選填欄位，這筆錄音當下 Mel 未開啟），無法組樣板")

    mu_A, sigma_A = session.baseline("A")
    mu_B, sigma_B = session.baseline("B")
    if mu_A is None or mu_B is None:
        raise ValueError(f"{trial.key}: session 缺 baseline mu/sigma，無法計算 ToF 特徵")

    aligner = _aligner_for_trial(trial)
    t_start = int(min(trial.tof_t_us[0], trial.mel_t_us[0]))
    t_end = int(max(trial.tof_t_us[-1], trial.mel_t_us[-1]))
    frames = list(aligner.frames(t_start, t_end))

    query = assemble_query_from_aligned_frames(frames, mu_A, sigma_A, mu_B, sigma_B)
    return query.data, query.coverage  # .data: fixed T=24 -- matches RecognitionService's default dist_method="cosine"


def build_templates(sessions, require_quality=("ok", "low")):
    """回傳 (templates_by_class, provenance, skipped, coverage_warnings)。

    provenance: {label: [{"session": path, "trial": trial_key, "coverage": {...}}, ...]} ——
    使用者會錄好幾次 session，這是唯一能回答「這批樣板哪些來自哪次錄製、
    每筆當下感測器覆蓋率如何」的地方，分不出來的話發現某次錄壞了也沒辦法
    只重錄那一批。

    skipped: [(trial_key, reason), ...] —— 逐筆容錯（跟 run_all.py 的
    build_feature_seqs() 同一種紀律：一筆組不出來就跳過並記錄原因，不是
    整批放棄），但**不重用**那支函式本身，理由見模組 docstring。這只在
    InsufficientFramesError（整段完全沒有交集）時發生——單純覆蓋率偏低
    不會讓一筆被跳過，見下面 coverage_warnings。

    coverage_warnings: [str, ...] —— 人話警告，某筆 trial 某個感測器的覆蓋
    率偏低（低於 LOW_COVERAGE_WARN_THRESHOLD）。**那筆樣板依然被納入**——
    低覆蓋率不是拒絕的理由，只是讓使用者知道，這批樣板裡有哪幾筆可能因為
    感測器斷線而失真。
    """
    templates_by_class = {}
    provenance = {}
    skipped = []
    coverage_warnings = []
    for session, trial in usable_trials(sessions, require_quality=require_quality):
        try:
            vec, coverage = build_template_vector(session, trial)
        except (ValueError, InsufficientFramesError) as exc:
            skipped.append((trial.key, str(exc)))
            continue
        templates_by_class.setdefault(trial.label, []).append(vec)
        provenance.setdefault(trial.label, []).append({
            "session": str(session.path), "trial": trial.key,
            "coverage": {
                "tof_A": round(coverage.fraction("tof_A"), 3),
                "tof_B": round(coverage.fraction("tof_B"), 3),
                "mel": round(coverage.fraction("mel"), 3),
                "usable_fraction": round(coverage.usable_fraction(), 3),
            },
        })
        for key, zh in (("tof_A", "感測器 A"), ("tof_B", "感測器 B"), ("mel", "Mel")):
            frac = coverage.fraction(key)
            if frac < LOW_COVERAGE_WARN_THRESHOLD:
                coverage_warnings.append(
                    f"{trial.key}（{trial.label}）：{zh} 只在這筆錄音 {frac * 100:.0f}% 的時間內有資料"
                    f"（其餘時間可能斷線）——這筆樣板還是被納入了，但距離量測可能因此偏移，"
                    f"準確率若異常可以先檢查這筆"
                )
    return templates_by_class, provenance, skipped, coverage_warnings




def build_and_save_templates(sessions, out_path, subject, wear_id, require_quality=("ok", "low")):
    """sessions: already-loaded `SessionData` list (caller loads them --
    see the module docstring's note on bridge_server.py needing to handle
    a locked-file read separately from "zero usable trials").

    Builds `templates_by_class` via `build_templates()`, saves the `.npz` +
    `.provenance.json` sidecar, returns a summary dict. Raises `ValueError`
    if zero templates could be built at all (empty after quality filtering,
    or every trial failed to assemble) -- that is the one case with nothing
    useful to save; a *partial* build (some trials skipped, others fine)
    is NOT an error, it is reported in the returned "skipped"/"warnings"
    lists instead (same "逐筆容錯，記錄不是放棄" discipline as
    `analysis/run_all.py`'s `build_feature_seqs()`, without reusing that
    function itself -- see the module docstring for why).
    """
    templates_by_class, provenance, skipped, coverage_warnings = build_templates(sessions, require_quality)
    if not templates_by_class:
        detail = ("；" + "；".join(f"{key}（{reason}）" for key, reason in skipped)) if skipped else ""
        raise ValueError(f"0 筆可用 trial（quality 篩選：{require_quality}）——沒有東西可以建樣板{detail}")

    out_path = Path(out_path)
    save_templates(templates_by_class, out_path, subject=subject, wear_id=wear_id)

    provenance_path = out_path.with_suffix(".provenance.json")
    provenance_path.write_text(json.dumps({
        "subject": subject, "wear_id": wear_id,
        "source_sessions": [str(s.path) for s in sessions],
        "require_quality": list(require_quality),
        "trials_by_class": provenance,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    warnings = []
    if "_reject" not in templates_by_class:
        warnings.append("沒有任何 _reject（靜止／其他）樣板——RecognitionService 需要它校準拒識門檻"
                         "（D22 雙邊 ROC），缺少的話這批樣板無法拿去建構 RecognitionService。")
    for label, vecs in sorted(templates_by_class.items()):
        n = len(vecs)
        if n < MIN_TEMPLATES_WARN:
            warnings.append(
                f"{label}: 只有 {n} 筆——這種「留一筆出來測」的統計在 n={n} 時不可靠："
                f"n=1 沒有東西可以留一筆出來測，算出來的準確率是 nan（不是 0%，也不是「還不錯」）；"
                f"n=2 只有兩種結果（0% 或 100%），噪聲極大。至少 {MIN_TEMPLATES_WARN} 筆才有意義，"
                f"正式 enrollment 建議每個詞約 {RECOMMENDED_TEMPLATES} 筆（reports/HANDOFF.md §3.1）")
    warnings.extend(coverage_warnings)

    return {
        "out_path": str(out_path),
        "provenance_path": str(provenance_path),
        "counts": {label: len(vecs) for label, vecs in templates_by_class.items()},
        "skipped": [{"trial": key, "reason": reason} for key, reason in skipped],
        "warnings": warnings,
        "has_reject": "_reject" in templates_by_class,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", action="append", required=True, dest="sessions",
                         help="要用的 session HDF5 檔路徑，可重複給多個（同一次戴上分好幾次錄）")
    parser.add_argument("--out", required=True, help="輸出的 .npz 路徑（例如 templates/alice_1.npz）")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--wear-id", type=int, required=True)
    parser.add_argument("--require-quality", default="ok,low",
                         help="逗號分隔，預設 ok,low（排除 rejected，跟 enrollment.py 的 "
                              "EXCLUDED_QUALITY / session_loader.usable_trials 預設一致）")
    args = parser.parse_args(argv)

    require_quality = tuple(q.strip() for q in args.require_quality.split(","))
    sessions = [load_session(Path(p)) for p in args.sessions]

    try:
        summary = build_and_save_templates(sessions, args.out, args.subject, args.wear_id, require_quality)
    except ValueError as exc:
        print(f"[build_templates] {exc}", file=sys.stderr)
        return 1

    if summary["skipped"]:
        print(f"[build_templates] 跳過 {len(summary['skipped'])} 筆（逐筆記錄原因，不是整批放棄）：")
        for item in summary["skipped"]:
            print(f"  - {item['trial']}: {item['reason']}")
    print("[build_templates] 各類別樣板數：")
    for label, n in sorted(summary["counts"].items()):
        print(f"  {label}: {n}")
    for warning in summary["warnings"]:
        print(f"  ⚠ {warning}")
    print(f"[build_templates] 存好：{summary['out_path']}")
    print(f"[build_templates] 樣板來源記錄：{summary['provenance_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
