"""量測：把 Mel 也做變異數正規化（`mel_features(cvn=True)`）之後，ToF/Mel
的距離尺度失衡、分類準確率、拒識行為分別怎麼變。

背景：`analysis/similarity/euclidean_baseline.py` 的量測發現 ToF-only／
Mel-only 歐式距離量級差 5.18 倍，結構性原因是 D01 的 ToF z-score 有除以
標準差，D02 的 Mel 預設（`cvn=False`）只做 CMN、沒有除以標準差——兩個
模態的正規化程度本來就不對等。`ad`（調度員）要求「先量，不要直接改
`analysis/features/` 的預設值」。

**這裡不改任何預設值**：`assemble_query_from_aligned_frames()` 本來就有
`cvn` 參數（預設 `False`，跟現行系統一致），這裡只是用 `cvn=True` 多跑
一次同一套流程比較，不動 `mel_features`/`assemble_query_from_aligned_frames`
的預設簽名，也不改 `build_templates_from_session.py`（那邊硬寫
`cvn=False`，維持不動；這裡自己重新組一次向量，不影響那支腳本的既有
行為）。

重用 `exp_distance_metric_comparison.py` 的合成資料產生器（同一組
class 訊號、同一套陷阱迴避），只是這次自己控制 `assemble_query_from_aligned_frames`
的 `cvn` 參數，而不是透過 `build_template_vector()`（那個函式沒有暴露
`cvn` 這個口子，改它會超出「先量」的範圍）。

**全部合成資料。**
"""
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.experiments.exp_distance_metric_comparison import (  # noqa: E402
    N_GEOMETRIES,
    SLICES,
    _class_signature,
    _meta,
    _write_session,
    N_REJECT_TEST,
    N_REJECT_TRAIN,
    N_TEST_PER_CLASS,
    N_TRAIN_PER_CLASS,
    WORDS,
)
from analysis.reporting.session_loader import usable_trials, load_session  # noqa: E402
from analysis.similarity.build_templates_from_session import _aligner_for_trial  # noqa: E402
from analysis.similarity.euclidean_baseline import modality_euclidean_dist  # noqa: E402
from analysis.similarity.recognition_service import RecognitionService  # noqa: E402
from host.features.live_pipeline import assemble_query_from_aligned_frames  # noqa: E402


def _build_vector(session, trial, cvn):
    """跟 `build_templates_from_session.build_template_vector()` 同一條路，
    唯一差別是可以指定 `cvn`（那支腳本硬寫 `cvn=False`，不重用是因為
    這裡只是量測用的實驗變體，不動生產路徑）。"""
    mu_A, sigma_A = session.baseline("A")
    mu_B, sigma_B = session.baseline("B")
    aligner = _aligner_for_trial(trial)
    t_start = int(min(trial.tof_t_us[0], trial.mel_t_us[0]))
    t_end = int(max(trial.tof_t_us[-1], trial.mel_t_us[-1]))
    frames = list(aligner.frames(t_start, t_end))
    query = assemble_query_from_aligned_frames(frames, mu_A, sigma_A, mu_B, sigma_B, cvn=cvn)
    return query.data


def _run_one_geometry(seed, tmp_dir, cvn):
    rng = np.random.default_rng(seed)
    class_sigs = {
        label: (_class_signature(rng, 16), _class_signature(rng, 16), _class_signature(rng, 40))
        for label in WORDS + ["_reject"]
    }

    train_path = tmp_dir / f"train_{seed}_{cvn}.h5"
    test_path = tmp_dir / f"test_{seed}_{cvn}.h5"
    _write_session(train_path, rng, class_sigs, {**{w: N_TRAIN_PER_CLASS for w in WORDS}, "_reject": N_REJECT_TRAIN})
    _write_session(test_path, rng, class_sigs, {**{w: N_TEST_PER_CLASS for w in WORDS}, "_reject": N_REJECT_TEST})

    train_session = load_session(train_path)
    train_pairs = usable_trials([train_session])
    templates_by_class = {}
    for _, trial in train_pairs:
        vec = _build_vector(train_session, trial, cvn)
        templates_by_class.setdefault(trial.label, []).append(vec)
    reject_templates = templates_by_class.pop("_reject")

    tof_dists, mel_dists = [], []
    for label, vecs in templates_by_class.items():
        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                tof_dists.append(modality_euclidean_dist(vecs[i], vecs[j], SLICES, "tof"))
                mel_dists.append(modality_euclidean_dist(vecs[i], vecs[j], SLICES, "mel"))

    test_session = load_session(test_path)
    test_pairs = usable_trials([test_session])

    results = {}
    for dist_method in ("cosine", "euclidean"):
        service = RecognitionService(
            dict(templates_by_class), list(reject_templates), SLICES,
            subject="synthetic", wear_id=1, dist_method=dist_method,
        )
        n_word_correct = n_word_total = 0
        n_reject_correctly_rejected = n_reject_total = 0
        n_word_falsely_rejected = 0
        for _, trial in test_pairs:
            vec = _build_vector(test_session, trial, cvn)
            tri, _latency_ms = service.recognize(vec)
            if trial.label == "_reject":
                n_reject_total += 1
                n_reject_correctly_rejected += int(tri.reject_fused(0.5))
            else:
                n_word_total += 1
                n_word_correct += int(tri.top1(0.5) == trial.label)
                n_word_falsely_rejected += int(tri.reject_fused(0.5))

        results[dist_method] = {
            "word_accuracy": n_word_correct / n_word_total,
            "false_reject_rate": n_word_falsely_rejected / n_word_total,
            "correct_reject_rate": n_reject_correctly_rejected / n_reject_total,
        }

    return {
        "tof_dist_mean": float(np.mean(tof_dists)),
        "mel_dist_mean": float(np.mean(mel_dists)),
        "results": results,
    }


def main():
    tmp_dir = Path(tempfile.mkdtemp(prefix="exp_cvn_"))
    try:
        for cvn in (False, True):
            runs = [_run_one_geometry(seed, tmp_dir, cvn) for seed in range(N_GEOMETRIES)]
            ratios = [r["tof_dist_mean"] / r["mel_dist_mean"] for r in runs]
            print(f"=== cvn={cvn} ===")
            print(f"  ToF/Mel 歐式距離量級比：平均 {np.mean(ratios):.2f}x（{[f'{r:.2f}' for r in ratios]}）")
            for method in ("cosine", "euclidean"):
                accs = [r["results"][method]["word_accuracy"] for r in runs]
                frrs = [r["results"][method]["false_reject_rate"] for r in runs]
                crrs = [r["results"][method]["correct_reject_rate"] for r in runs]
                print(f"  [{method}] top1={np.mean(accs):.1%}  誤拒={np.mean(frrs):.1%}  正確拒識={np.mean(crrs):.1%}")
            print()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
