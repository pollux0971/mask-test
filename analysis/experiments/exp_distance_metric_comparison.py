"""距離度量比較：cosine（現行預設）vs euclidean（使用者新指定，2026-08-26
由 `ad` 轉述："我希望只要歐式距離不需要訓練"——「不需要訓練」本來就成立
（現行是最近鄰比對，沒有模型訓練），使用者要的是換掉距離函式本身）。

**全部合成資料，不是真實準確率或真實延遲。** 真實結論待 `E05` 真實錄音
後，把 `_make_synthetic_session()` 換成真實 session 路徑重跑本檔案即可
（跟 D05/D22 同一個「先建流程，換資料來源」的原則）。

## 走的是真實 production 路徑，不是抄近路

跟 `exp_d05_dtw_vs_cosine.py`（純合成特徵空間，不經 HDF5）不同，這裡
刻意用 `host.storage.session_writer.SessionWriter` 寫出真的 HDF5 session、
`analysis.reporting.session_loader.load_session()` 讀回、
`analysis.similarity.build_templates_from_session.build_templates()` 建樣板
——跟 `E05` 實際錄製 → 建樣板 → 上線的路徑完全一致，包含 D01 z-score／
D02 CMN／`Aligner` 對齊，不是繞過這些直接餵合成特徵向量。

## 合成資料的陷阱迴避（見 `stories/D-analysis/D22.md`「前人踩過四次」）

1. 維度詛咒：class 訊號分散在 ToF 16 個 zone、Mel 40 個 band 的大部分
   維度上（`_class_signature()` 產生跨全部維度的單位向量），不是塞在
   一兩個通道。
2. 天花板效應：訊號振幅刻意跟雜訊同量級（見 `TOF_SIGNAL_AMP_MM`／
   `MEL_SIGNAL_AMP` 跟對應 noise std 的比較），不是大到「拿掉什麼都
   還是 100%」。
3. 共同常數 + cosine 塌陷：baseline 本身雖然共用，但 class 訊號用
   單位向量疊加、每個 class 方向都不同，不會讓所有樣本指向同一方向。
4. 單一幾何抽樣運氣：對 `N_GEOMETRIES` 組獨立隨機種子各跑一次全流程，
   結果取平均，不是只看一次抽樣。
"""
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.features.feature_assembly import FEATURE_DIM  # noqa: E402
from analysis.features.tof_features import TOF_DIM  # noqa: E402
from analysis.reporting.session_loader import load_session, usable_trials  # noqa: E402
from analysis.similarity.build_templates_from_session import (  # noqa: E402
    build_template_vector,
    build_templates,
)
from analysis.similarity.euclidean_baseline import modality_euclidean_dist  # noqa: E402
from analysis.similarity.recognition_service import RecognitionService  # noqa: E402
from host.storage.session_writer import SessionWriter  # noqa: E402

WORDS = ["round_word", "spread_word", "click_word", "hum_word", "tap_word"]
N_TRAIN_PER_CLASS = 8
N_TEST_PER_CLASS = 4
N_REJECT_TRAIN = 10
N_REJECT_TEST = 5
N_GEOMETRIES = 3  # D22 陷阱 4：至少 3 組獨立幾何取平均

N_ZONES = 16
T_TOF = 60   # ~2s @ 30Hz，跟 test_session_writer.py 的慣例一致
T_MEL = 67   # F = M + 7，同一個「mel 有自己時間軸」的慣例

TOF_BASELINE_MM = 300.0
TOF_NOISE_STD_MM = 9.0          # 未加訊號時的雜訊標準差
TOF_SIGNAL_AMP_MM = 18.0        # 陷阱 2：跟雜訊同量級，不是壓倒性大
TOF_SIG_STRENGTH_BASELINE = 500.0
TOF_SIG_STRENGTH_NOISE = 20.0   # 訊號強度通道不帶 class 資訊，只當背景

MEL_BASELINE = -3.0             # log10 mel power（`reference_mel.py`：LOG_FLOOR=1e-10）
MEL_NOISE_STD = 0.25
MEL_SIGNAL_AMP = 0.9            # 陷阱 2：同上，跟雜訊同量級

SLICES = {"tof": slice(0, 2 * TOF_DIM), "mel": slice(2 * TOF_DIM, FEATURE_DIM)}
assert 2 * TOF_DIM + (FEATURE_DIM - 2 * TOF_DIM) == FEATURE_DIM  # 純粹自我檢查一致性


def _class_signature(rng, n_dims):
    """跨全部維度的單位向量（陷阱 1：不要把訊號塞在一兩個通道）。"""
    v = rng.normal(size=n_dims)
    return v / np.linalg.norm(v)


def _gen_trial_raw(rng, tof_sig_A, tof_sig_B, mel_sig, amp_scale):
    """一筆 trial 的原始 ToF/mel 資料。訊號用時間包絡調變（開頭/結尾接近
    baseline、中段最強），振幅有逐筆抖動——同一類別不同筆之間不會完全
    可分，避免陷阱 2（天花板效應）。`amp_scale=0` 用來產生 `_reject`
    （靜止／無訊號）的資料：只有 baseline + 雜訊，沒有 class 包絡。
    """
    u = np.linspace(0, 1, T_TOF)
    envelope = np.sin(np.pi * u) ** 2
    jitter = max(0.3, 1.0 + rng.normal(0, 0.15))

    def _tof_channel(sig_dist):
        dist = TOF_BASELINE_MM + np.outer(envelope, sig_dist) * TOF_SIGNAL_AMP_MM * amp_scale * jitter
        dist += rng.normal(0, TOF_NOISE_STD_MM, size=(T_TOF, N_ZONES))
        strength = TOF_SIG_STRENGTH_BASELINE + rng.normal(0, TOF_SIG_STRENGTH_NOISE, size=(T_TOF, N_ZONES))
        return np.concatenate([dist, strength], axis=1).astype(np.float32)

    tof_A = _tof_channel(tof_sig_A)
    tof_B = _tof_channel(tof_sig_B)

    u_m = np.linspace(0, 1, T_MEL)
    envelope_m = np.sin(np.pi * u_m) ** 2
    mel = MEL_BASELINE + np.outer(envelope_m, mel_sig) * MEL_SIGNAL_AMP * amp_scale * jitter
    mel += rng.normal(0, MEL_NOISE_STD, size=(T_MEL, len(mel_sig)))
    return tof_A, tof_B, mel.astype(np.float32)


def _meta():
    baseline_mu = np.concatenate([
        np.full(N_ZONES, TOF_BASELINE_MM, dtype=np.float32),
        np.full(N_ZONES, TOF_SIG_STRENGTH_BASELINE, dtype=np.float32),
    ])
    baseline_sigma = np.concatenate([
        np.full(N_ZONES, TOF_NOISE_STD_MM, dtype=np.float32),
        np.full(N_ZONES, TOF_SIG_STRENGTH_NOISE, dtype=np.float32),
    ])
    return {
        "schema_version": 1, "subject": "synthetic", "session_date": "2026-08-26",
        "wear_id": 1, "mode": "quiz", "distance_mm": 30.0, "angle_deg": 0.0,
        "ambient": "quiet room", "notes": "distance-metric-comparison synthetic fixture",
        "fw_sha": "0000000", "proto_version": 2, "tof_dim": N_ZONES,
        "clock_slope": 1.0, "clock_offset": 0.0, "clock_residual_p95": 0.0,
        "clock_drift_ppm": 0.0, "clock_drift_us": 0.0, "clock_sync_span_us": 30_000_000,
        "clock_sync_confirmed": True, "session_start_device_us": 0,
        "session_start_host_us": 1_756_000_000_000_000, "session_start_rtt_min_us": 800,
        "baseline_mu_A": baseline_mu, "baseline_sigma_A": baseline_sigma,
        "baseline_mu_B": baseline_mu, "baseline_sigma_B": baseline_sigma,
        "noise_floor_mu": 0.0, "noise_floor_sigma": 1.0,
    }


def _write_session(path, rng, class_sigs, n_per_class):
    """`class_sigs`: {label: (tof_sig_A, tof_sig_B, mel_sig)}，同一組跨
    train/test 沿用（同一個「使用者」/「配戴位置」的訊號特徵不該在
    train/test 之間換掉）。`"_reject"` 用 amp_scale=0（沒有 class 包絡）。
    """
    with SessionWriter(path, _meta()) as w:
        idx = 0
        for label, n in n_per_class.items():
            amp_scale = 0.0 if label == "_reject" else 1.0
            sig_A, sig_B, sig_mel = class_sigs[label]
            for _ in range(n):
                tof_A, tof_B, mel = _gen_trial_raw(rng, sig_A, sig_B, sig_mel, amp_scale)
                w.write_trial(
                    idx, label=label,
                    tof_A=tof_A, tof_B=tof_B,
                    tof_t_us=np.arange(T_TOF, dtype=np.int64) * 33_333,
                    tof_valid_A=np.ones((T_TOF, N_ZONES), dtype=bool),
                    tof_valid_B=np.ones((T_TOF, N_ZONES), dtype=bool),
                    mic_rms=rng.uniform(0, 32767, size=T_TOF).astype(np.float32),
                    mic_peak=rng.integers(0, 32767, size=T_TOF).astype(np.int16),
                    mic_t_us=np.arange(T_TOF, dtype=np.int64) * 16_000,
                    mel=mel, mel_t_us=np.arange(T_MEL, dtype=np.int64) * 8_000,
                    wear_id=1, mode="quiz", valid_zone_ratio=1.0, drop_count=0,
                    quality="ok",
                )
                idx += 1


def _run_one_geometry(seed, tmp_dir):
    rng = np.random.default_rng(seed)
    class_sigs = {
        label: (_class_signature(rng, N_ZONES), _class_signature(rng, N_ZONES), _class_signature(rng, 40))
        for label in WORDS + ["_reject"]
    }

    train_path = tmp_dir / f"train_{seed}.h5"
    test_path = tmp_dir / f"test_{seed}.h5"
    _write_session(train_path, rng, class_sigs, {**{w: N_TRAIN_PER_CLASS for w in WORDS}, "_reject": N_REJECT_TRAIN})
    _write_session(test_path, rng, class_sigs, {**{w: N_TEST_PER_CLASS for w in WORDS}, "_reject": N_REJECT_TEST})

    train_session = load_session(train_path)
    templates_by_class, provenance, skipped, coverage_warnings = build_templates([train_session])
    assert not skipped, f"合成資料不該有組不出來的 trial: {skipped}"
    reject_templates = templates_by_class.pop("_reject")

    # 量測：ToF-only vs Mel-only 的歐式距離尺度（同類樣板兩兩配對）
    tof_dists, mel_dists = [], []
    for label, vecs in templates_by_class.items():
        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                tof_dists.append(modality_euclidean_dist(vecs[i], vecs[j], SLICES, "tof"))
                mel_dists.append(modality_euclidean_dist(vecs[i], vecs[j], SLICES, "mel"))

    test_session = load_session(test_path)
    test_pairs = usable_trials([test_session])  # [(session, trial), ...]

    results = {}
    for dist_method in ("cosine", "euclidean"):
        service = RecognitionService(
            dict(templates_by_class), list(reject_templates), SLICES,
            subject="synthetic", wear_id=1, dist_method=dist_method,
        )
        info = service.list_templates()
        theta_tof, theta_mel = info["theta_reject_tof"], info["theta_reject_mel"]

        n_word_correct = n_word_total = 0
        n_reject_correctly_rejected = n_reject_total = 0
        n_word_falsely_rejected = 0
        latencies_ms = []
        for _, trial in test_pairs:
            vec, _coverage = build_template_vector(test_session, trial)
            tri, latency_ms = service.recognize(vec)
            latencies_ms.append(latency_ms["dist"])  # 只比距離計算+融合耗時，不含特徵萃取（這裡沒量）
            if trial.label == "_reject":
                n_reject_total += 1
                n_reject_correctly_rejected += int(tri.reject_fused(0.5))
            else:
                n_word_total += 1
                n_word_correct += int(tri.top1(0.5) == trial.label)
                n_word_falsely_rejected += int(tri.reject_fused(0.5))

        results[dist_method] = {
            "theta_reject_tof": theta_tof,
            "theta_reject_mel": theta_mel,
            "word_accuracy": n_word_correct / n_word_total,
            "false_reject_rate": n_word_falsely_rejected / n_word_total,
            "correct_reject_rate": n_reject_correctly_rejected / n_reject_total,
            "mean_latency_ms": float(np.mean(latencies_ms)),
        }

    return {
        "tof_dist_mean": float(np.mean(tof_dists)),
        "mel_dist_mean": float(np.mean(mel_dists)),
        "results": results,
    }


def main():
    tmp_dir = Path(tempfile.mkdtemp(prefix="exp_distance_metric_"))
    try:
        runs = [_run_one_geometry(seed, tmp_dir) for seed in range(N_GEOMETRIES)]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    scale_ratios = [r["tof_dist_mean"] / r["mel_dist_mean"] for r in runs]
    print(f"ToF-only vs Mel-only 歐式距離量級比（tof/mel，{N_GEOMETRIES} 組幾何各自的同類樣板配對平均值）：")
    for seed, ratio, r in zip(range(N_GEOMETRIES), scale_ratios, runs):
        print(f"  seed={seed}: tof_mean={r['tof_dist_mean']:.3f}  mel_mean={r['mel_dist_mean']:.3f}  ratio={ratio:.2f}x")
    print(f"  平均比例：{np.mean(scale_ratios):.2f}x（標準差 {np.std(scale_ratios):.2f}）")
    print()

    for method in ("cosine", "euclidean"):
        accs = [r["results"][method]["word_accuracy"] for r in runs]
        frrs = [r["results"][method]["false_reject_rate"] for r in runs]
        crrs = [r["results"][method]["correct_reject_rate"] for r in runs]
        thetas_tof = [r["results"][method]["theta_reject_tof"] for r in runs]
        thetas_mel = [r["results"][method]["theta_reject_mel"] for r in runs]
        print(f"[{method}]")
        print(f"  top1 準確率（真詞）：{np.mean(accs):.1%}（各幾何：{[f'{a:.1%}' for a in accs]}）")
        print(f"  誤拒率（真詞卻被拒識）：{np.mean(frrs):.1%}")
        print(f"  正確拒識率（_reject 確實被拒）：{np.mean(crrs):.1%}")
        print(f"  theta_reject_tof：{[f'{t:.3f}' for t in thetas_tof]}")
        print(f"  theta_reject_mel：{[f'{t:.3f}' for t in thetas_mel]}")
        lats = [r["results"][method]["mean_latency_ms"] for r in runs]
        print(f"  平均單次辨識延遲：{np.mean(lats):.3f} ms")
        print()


if __name__ == "__main__":
    main()
