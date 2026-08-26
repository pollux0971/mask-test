"""D21 — 快速閉集合相似度探針，CLI 入口。

規格見 `ssi-backlog/stories/D-analysis/D21.md`；純函式邏輯在
`analysis/similarity/closed_set_probe.py`，這裡只負責讀 session、組報告、
畫熱力圖（story 範圍明訂「CLI + matplotlib，不做前端 UI」）。

## 兩種用法

1. **`--session ... --query-session ... --query-trial ...`**：吃真的
   HDF5 session（`T04` mock device 或真裝置錄的都可以，`session_loader`
   不分兩者）。每個 label 的所有 trial 當該候選詞的樣板（story 建議錄
   2–3 次），另一個 session／trial 當探針。
2. **`--demo`**：不需要任何錄音，用合成資料跑一次完整流程——**這是
   驗收條件「跑一次不裁切版本、保留輸出，證明距離確實會擠成一團」的
   具體交付物**：`ad`/使用者的板子接頭還沒修好、還沒能錄真實資料，
   這個模式讓「陷阱被檢查過」這件事現在就能被驗證，不必等硬體。

兩種用法都會**跑兩次**（`trim=True` 與 `trim=False`）並把兩份輸出都
存到 `--out-dir`（預設 `analysis/experiments/output/d21/`）——未裁切的
那份不是失敗的執行，是驗收條件要求保留的對照證據本身。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.reporting.session_loader import load_session, usable_trials  # noqa: E402
from analysis.similarity.closed_set_probe import (  # noqa: E402
    DEFAULT_N_CANDIDATES,
    SEPARABLE_RATIO_THRESHOLD,
    build_probe_vector,
    format_probe_report,
    pairwise_distance_matrix,
    probe_three_track,
    separability_ratio,
)
from analysis.similarity.euclidean_baseline import euclidean_dist  # noqa: E402
from host.storage.session_writer import SessionWriter  # noqa: E402

OUT_DIR = REPO_ROOT / "analysis" / "experiments" / "output" / "d21"


def _enrollment_by_label(sessions):
    by_label = {}
    for session, trial in usable_trials(sessions):
        by_label.setdefault(trial.label, []).append((session, trial))
    return by_label


def plot_pairwise_heatmap(matrix, labels, out_path, title):
    """可分性預檢熱力圖：對角線應該最暗（距離 0），非對角線越亮越好
    （story 原文）。用 `analysis.reporting.plot_style` 的既有色階，跟
    `exp_c_silhouette`/`d14_viseme_sensitivity` 的圖表風格一致——只讀
    那個常數，不改 `analysis/reporting/` 任何東西。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        from analysis.reporting.plot_style import SEQUENTIAL_CMAP
        cmap = SEQUENTIAL_CMAP
    except ImportError:
        cmap = "viridis"

    fig, ax = plt.subplots(figsize=(1 + 0.6 * len(labels), 1 + 0.6 * len(labels)))
    im = ax.imshow(matrix, cmap=cmap)
    ax.set_xticks(range(len(labels)))
    ax.set_yticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_yticklabels(labels)
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="distance")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def run_probe(enrollment_by_label, query_session, query_trial, out_dir, tag, n_candidates=DEFAULT_N_CANDIDATES):
    """跑一次完整流程（`trim=True` 與 `trim=False` 各一次），輸出：
    - `<tag>_trimmed.json` / `<tag>_untrimmed.json`：完整數字（距離矩陣、
      可分性比值、三軌排名），機器可讀，也是「留給未來的人的證據」。
    - `<tag>_trimmed_matrix.png` / `<tag>_untrimmed_matrix.png`：可分性
      預檢熱力圖。
    - stdout：人類可讀的報告（`format_probe_report()`）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = sorted(enrollment_by_label.keys())[:n_candidates]

    for trim in (True, False):
        suffix = "trimmed" if trim else "untrimmed"
        templates_by_class = {}
        for label in labels:
            vecs = []
            for session, trial in enrollment_by_label[label]:
                vec, _sw = build_probe_vector(session, trial, trim=trim)
                vecs.append(vec)
            templates_by_class[label] = vecs

        full_vectors = [templates_by_class[label][0] for label in labels]
        matrix = pairwise_distance_matrix(full_vectors, euclidean_dist)
        ratios = separability_ratio(templates_by_class, euclidean_dist)

        query_vec, speech_window = build_probe_vector(query_session, query_trial, trim=trim)
        tracks_by_dist = {
            dist_method: probe_three_track(query_vec, templates_by_class, dist_method)
            for dist_method in ("euclidean", "cosine")
        }

        true_label = query_trial.label if query_trial.label in labels else None
        report_text = format_probe_report(tracks_by_dist, true_label, len(labels))
        print(f"\n########## {tag} / {suffix} ##########")
        print(report_text)
        print(f"可分性比值 (> {SEPARABLE_RATIO_THRESHOLD} 才算真的可分):",
              {k: (round(v, 2) if v is not None else None) for k, v in ratios.items()})

        payload = {
            "tag": tag, "trim": trim, "n_candidates": len(labels), "labels": labels,
            "true_label": true_label,
            "pairwise_distance_matrix": matrix.tolist(),
            "separability_ratio": ratios,
            "speech_window": speech_window.to_dict() if speech_window is not None else None,
            "rankings": {
                dist_method: {
                    track_name: {
                        "ranked": tracks[track_name].ranked,
                        "rank_of_true_label": tracks[track_name].rank_of(true_label) if true_label else None,
                    }
                    for track_name in ("tof", "mel", "fused")
                }
                for dist_method, tracks in tracks_by_dist.items()
            },
        }
        (out_dir / f"{tag}_{suffix}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        plot_pairwise_heatmap(matrix, labels, out_dir / f"{tag}_{suffix}_matrix.png",
                               f"D21 enrollment pairwise distance ({suffix})")


# --------------------------------------------------------------------- demo


def _demo_synthetic_session(tmp_path, n_candidates=8):
    """合成一個 session：`n_candidates` 個候選詞（各錄 2 筆，供可分性比值
    用）+ 1 個複錄同一個目標詞的探針，3.5 秒錄音、訊號只在中段 ~500ms
    ——跟 `analysis/similarity/test_closed_set_probe.py` 的 fixture 同一
    套產生邏輯（避免兩份各自維護一份合成資料生成器，這裡直接重用那個
    測試模組，不重寫）。"""
    from analysis.similarity import test_closed_set_probe as fixture

    rng = np.random.default_rng(7)
    words = [f"word_{i}" for i in range(n_candidates)]
    class_sigs = {w: (fixture._class_signature(rng, 16), fixture._class_signature(rng, 16),
                       fixture._class_signature(rng, 40)) for w in words}
    target_word = words[0]

    path = tmp_path / "d21_demo_session.h5"
    with SessionWriter(path, fixture._meta()) as w:
        idx = 0
        for word in words * 2 + [f"{target_word}_query"]:  # 每詞錄 2 筆 + 1 筆探針
            label = target_word if word.endswith("_query") else word
            sig_A, sig_B, sig_mel = class_sigs[label]
            tof_A, tof_B, mel, _, _ = fixture._gen_long_trial_raw(rng, sig_A, sig_B, sig_mel)
            tof_t_us = np.arange(fixture.T_TOF_TOTAL, dtype=np.int64) * fixture.TOF_FRAME_US
            mel_t_us = np.arange(fixture.T_MEL_TOTAL, dtype=np.int64) * fixture.MEL_FRAME_US
            s_us = int(tof_t_us[fixture.SPEECH_START_FRAME])
            e_us = int(tof_t_us[fixture.SPEECH_END_FRAME])
            w.write_trial(
                idx, label=label, tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
                tof_valid_A=np.ones((fixture.T_TOF_TOTAL, 16), dtype=bool),
                tof_valid_B=np.ones((fixture.T_TOF_TOTAL, 16), dtype=bool),
                mic_rms=rng.uniform(0, 32767, size=fixture.T_TOF_TOTAL).astype(np.float32),
                mic_peak=rng.integers(0, 32767, size=fixture.T_TOF_TOTAL).astype(np.int16),
                mic_t_us=tof_t_us.copy(), mel=mel, mel_t_us=mel_t_us,
                wear_id=1, mode="quiz", valid_zone_ratio=1.0, drop_count=0, quality="ok",
                lip_onset_us_A=s_us, lip_onset_us_B=s_us + 5_000, voice_onset_us=s_us + 20_000,
                vad_start_us=s_us, vad_end_us=e_us,
            )
            idx += 1
    return path, target_word


def run_demo(out_dir, n_candidates=8):
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="d21_demo_"))
    try:
        path, target_word = _demo_synthetic_session(tmp_dir, n_candidates)
        session = load_session(path)
        by_label = _enrollment_by_label([session])
        # 最後寫入、key 最大的那筆是探針（跟其中一個候選詞同 label，複錄），
        # 其餘同 label 的筆數留給 enrollment 當「同一個詞錄兩次」。
        query_trial = max((t for _, t in usable_trials([session]) if t.label == target_word),
                           key=lambda tr: tr.key)
        by_label[target_word] = [(s, t) for s, t in by_label[target_word] if t.key != query_trial.key]

        run_probe(by_label, session, query_trial, out_dir, tag="demo_synthetic", n_candidates=n_candidates)
        print(f"\n⚠️ 以上全部是合成資料（`--demo`），不是真實錄音——"
              f"真實結論待板子接頭修好、真的錄完 N 個選項後用 `--session` 模式重跑。")
        print(f"輸出已存到: {out_dir}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", nargs="*", default=[], help="enrollment session(s), HDF5 路徑")
    parser.add_argument("--query-session", help="探針 session 路徑（可以跟 --session 相同檔案）")
    parser.add_argument("--query-trial", help="探針 trial 的 key（例如 trial_008）")
    parser.add_argument("--n-candidates", type=int, default=DEFAULT_N_CANDIDATES)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--demo", action="store_true", help="不需要真實錄音，跑合成資料示範")
    args = parser.parse_args(argv)

    if args.demo:
        run_demo(args.out_dir, args.n_candidates)
        return

    if not args.session or not args.query_session or not args.query_trial:
        parser.error("非 --demo 模式需要 --session、--query-session、--query-trial 三者都給")

    sessions = [load_session(p) for p in args.session]
    by_label = _enrollment_by_label(sessions)
    query_session = load_session(args.query_session)
    query_trial = next(t for _, t in usable_trials([query_session]) if t.key == args.query_trial)
    run_probe(by_label, query_session, query_trial, args.out_dir, tag="probe", n_candidates=args.n_candidates)


if __name__ == "__main__":
    main()
