"""量測「訓練/分析路徑」跟「線上推論路徑」兩套跨模態對齊邏輯的落差。

## 背景

`esp-mask-test-59` 做 Demo 乾跑時發現同一筆 trial 的 `tof_A` 是 39 幀、
`mel` 是 50 幀（CONTRACTS.md 早警告過兩者幀率不同：ToF 30 Hz、Mel 62.5 Hz），
而全庫**有兩套完全不同的邏輯**在處理這個幀數落差：

- **`analysis/run_all.py` 的 `build_feature_seqs()`**（`D01`→`D02`→`D03`，
  `D06`/`D08`/`D09`/`D22` 等歷史分析報告的準確率、拒識門檻全部是拿它產出的
  特徵算的）：完全**不使用** `host/align/aligner.py`，直接對每個模態原生
  取樣率的陣列各自算 `tof_features()`/`mel_features()`，再用
  `n = min(len(tof_a_z), len(tof_b_z), len(mel_cmn), len(trial.tof_t_us))`
  **按索引截斷**——第 i 個 ToF 幀配第 i 個 Mel 幀，兩者取樣率不同，
  同一個索引對應到的**真實時間不一樣**，而且落差隨索引線性增長。
- **`host/features/live_pipeline.py`**（`7c [4bedc9]` 正在拿它寫建樣板腳本，
  也是唯一可能的線上推論路徑——資料是串流進來的，沒有「先湊齊全部再算」
  這個選項）：先把原始樣本餵進 `host/align/aligner.py` 的 `Aligner`，
  用**真實 `t_us`** 在固定輸出頻率上做最近鄰/線性內插，兩個模態的每一幀
  才真的對應同一個時間點。

`analysis/features/feature_assembly.py`（`D03`）的模組文件字串明講：
「傳進來的 `tof_a_z`/`tof_b_z`/`mel_cmn`/`t_us` **必須已經由 B06
（多模態時間對齊器）對到同一組共用幀**」——`build_feature_seqs()`
完全沒有呼叫 B06，違反了自己下游模組寫明的前提，是讀程式碼直接
確認的事實，不是推測。

## 這支腳本量什麼

用合成資料（目前沒有真實資料，見 `HANDOFF.md`）建構已知「真實時間軸」的
單一 trial，分別餵給兩條路徑，比較兩者算出的 (T=24, 104) 特徵向量差多少，
並且用一個簡單的分類情境（兩個可分辨的合成詞）檢查這個落差**會不會讓
辨識結果翻轉**，不只是看數值差多少。

**只做量測，不改 `analysis/run_all.py`／`host/features/live_pipeline.py`
──兩者都在改動邊界之外，這輪只回報。**
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from analysis.reporting.session_loader import SessionData, Trial  # noqa: E402
from analysis.run_all import build_feature_seqs  # noqa: E402  只 import，不修改
from analysis.similarity.cosine_baseline import cosine_dist, modality_cosine_dist  # noqa: E402
from host.align.aligner import Aligner  # noqa: E402
from host.features.live_pipeline import assemble_query_from_aligned_frames  # noqa: E402  只 import，不修改

FEATURE_SLICES = {"tof": slice(0, 64), "mel": slice(64, 104)}

N_TOF_ZONES = 16
N_MEL_BANDS = 40
TOF_DIST_BASELINE_MM = 200.0
TOF_DIST_AMPLITUDE_MM = 50.0
TOF_SIGNAL_BASELINE = 100.0  # z-score 後恆為 0，避免蓋掉真正想量的落差
MEL_BASELINE = -5.0
MEL_AMPLITUDE = 2.0

# 兩個可互相分辨的合成「詞」：不同頻率＋不同初始相位。
WORD_PATTERNS = {
    "word_round": {"freq_hz": 1.2, "phase0": 0.0},
    "word_spread": {"freq_hz": 2.6, "phase0": 1.4},
}

# z-score 用的「假 baseline」：距離通道 mu=0/sigma=1（等於直接看原始 mm
# 差異），signal 通道 mu=100/sigma=1（讓 signal 通道 z-score 後恆為 0）——
# 這樣兩條路徑的差異只可能來自「對齊方式」，不會被 baseline 選擇混進來。
BASELINE_MU = np.concatenate([np.zeros(N_TOF_ZONES), np.full(N_TOF_ZONES, TOF_SIGNAL_BASELINE)])
BASELINE_SIGMA = np.ones(2 * N_TOF_ZONES)


def _zone_weights(seed: int, n: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.uniform(0.3, 1.0, size=n)


_TOF_A_WEIGHTS = _zone_weights(1, N_TOF_ZONES)
_TOF_B_WEIGHTS = _zone_weights(2, N_TOF_ZONES)
_MEL_WEIGHTS = _zone_weights(3, N_MEL_BANDS)


def _phase(t_s: np.ndarray, freq_hz: float, phase0: float) -> np.ndarray:
    return np.sin(2 * np.pi * freq_hz * t_s + phase0)


def synth_record(word: str, duration_s: float, tof_rate_hz: float, mel_rate_hz: float,
                  seed: int = 0, tof_drop_rate: float = 0.0, noise_std: float = 0.02):
    """造一筆「真實時間軸已知」的合成 trial：回傳原生取樣率的 `tof_A`/`tof_B`/
    `mel` 陣列與各自的 `t_us`——刻意跟真機一樣，ToF 與 Mel 是`兩條獨立、
    取樣率不同的串流`，不是同一組索引可以互相對應的東西（CONTRACTS.md §1.1.1）。

    `tof_drop_rate`：模擬 REC dump 期間 ToF 掉幀（CONTRACTS.md §1.4）——
    掉幀不會讓 `seq` 補位，只會讓那個時間點的樣本真的不存在。
    """
    rng = np.random.default_rng(seed)
    pattern = WORD_PATTERNS[word]

    t_tof = np.arange(0.0, duration_s, 1.0 / tof_rate_hz)
    if tof_drop_rate > 0:
        keep = rng.random(len(t_tof)) >= tof_drop_rate
        keep[0] = True  # 保留起點，避免整段空白
        t_tof = t_tof[keep]
    t_mel = np.arange(0.0, duration_s, 1.0 / mel_rate_hz)

    phase_tof = _phase(t_tof, pattern["freq_hz"], pattern["phase0"])
    phase_mel = _phase(t_mel, pattern["freq_hz"], pattern["phase0"])

    def _tof_frame(phase_row, weights, tof_amp_scale):
        dist = TOF_DIST_BASELINE_MM + tof_amp_scale * TOF_DIST_AMPLITUDE_MM * np.outer(phase_row, weights)
        dist += rng.normal(0, noise_std * TOF_DIST_AMPLITUDE_MM, dist.shape)
        signal = np.full((len(phase_row), N_TOF_ZONES), TOF_SIGNAL_BASELINE)
        return np.concatenate([dist, signal], axis=1)

    tof_A = _tof_frame(phase_tof, _TOF_A_WEIGHTS, 1.0)
    tof_B = _tof_frame(phase_tof, _TOF_B_WEIGHTS, 0.85)
    mel = MEL_BASELINE + MEL_AMPLITUDE * np.outer(phase_mel, _MEL_WEIGHTS)
    mel += rng.normal(0, noise_std * MEL_AMPLITUDE, mel.shape)

    return dict(
        tof_A=tof_A.astype(np.float64), tof_B=tof_B.astype(np.float64),
        tof_valid_A=np.ones((len(t_tof), N_TOF_ZONES), dtype=bool),
        tof_valid_B=np.ones((len(t_tof), N_TOF_ZONES), dtype=bool),
        tof_t_us=(t_tof * 1e6).astype(np.int64),
        mel=mel.astype(np.float64),
        mel_t_us=(t_mel * 1e6).astype(np.int64),
    )


def run_offline_path(record: dict) -> np.ndarray:
    """真正呼叫 `analysis.run_all.build_feature_seqs()`——不重寫一份邏輯，
    量的是實際在跑的那條路徑。"""
    trial = Trial(
        key="trial_001", label="query", wear_id=0, mode="record", speaking_mode="normal",
        quality="ok",
        tof_a=record["tof_A"], tof_b=record["tof_B"],
        tof_valid_a=record["tof_valid_A"], tof_valid_b=record["tof_valid_B"],
        tof_t_us=record["tof_t_us"], mic_rms=np.zeros(1), mic_t_us=np.zeros(1, dtype=np.int64),
        mel=record["mel"],
    )
    meta = {
        "baseline_mu_A": BASELINE_MU, "baseline_sigma_A": BASELINE_SIGMA,
        "baseline_mu_B": BASELINE_MU, "baseline_sigma_B": BASELINE_SIGMA,
    }
    session = SessionData(path=Path("synthetic.h5"), meta=meta, trials=[trial])
    feature_seqs, labels, skipped, _ = build_feature_seqs([trial], {id(trial): session})
    if skipped:
        raise RuntimeError(f"offline 路徑把這筆合成資料跳過了：{skipped}")
    return feature_seqs[0]


def run_online_path(record: dict, tof_rate_hz: float) -> np.ndarray:
    """真正呼叫 `host.align.aligner.Aligner` + `live_pipeline
    .assemble_query_from_aligned_frames()`——線上推論唯一可能的路徑
    （資料是串流進來的，沒有「先湊齊全部再算」這個選項）。"""
    aligner = Aligner()
    n_tof = record["tof_A"].shape[0]
    for i in range(n_tof):
        t_us = int(record["tof_t_us"][i])
        aligner.push_tof("A", t_us, record["tof_A"][i, :N_TOF_ZONES], record["tof_A"][i, N_TOF_ZONES:],
                          record["tof_valid_A"][i])
        aligner.push_tof("B", t_us, record["tof_B"][i, :N_TOF_ZONES], record["tof_B"][i, N_TOF_ZONES:],
                          record["tof_valid_B"][i])
    for i in range(record["mel"].shape[0]):
        aligner.push_mel(int(record["mel_t_us"][i]), record["mel"][i])

    t_start = int(record["tof_t_us"][0])
    t_end = int(record["tof_t_us"][-1])
    frames = list(aligner.frames(t_start, t_end, rate_hz=tof_rate_hz))
    seq = assemble_query_from_aligned_frames(
        frames, BASELINE_MU, BASELINE_SIGMA, BASELINE_MU, BASELINE_SIGMA,
    )
    return seq.data


def compare(offline: np.ndarray, online: np.ndarray) -> dict:
    """`RecognitionService`（`analysis/similarity/fusion.py`
    的 `compute_tri_result()`）**分開算 ToF 跟 Mel 的距離**，融合前互不干擾
    ——所以只看合併後的 104 維向量會**低估**問題：ToF 通道（64/104 維）
    只要 index 對 index 就自己內部一致（沒被跨模態對齊落差污染），
    在合併向量的餘弦相似度裡權重又大，容易把 Mel 通道的落差稀釋到看不見。
    這裡刻意把合併／ToF-only／Mel-only 三個都印出來。"""
    diff = offline - online
    scale = np.abs(online).mean() or 1.0
    return {
        "max_abs_diff": float(np.abs(diff).max()),
        "rms_diff": float(np.sqrt((diff ** 2).mean())),
        "rel_rms_diff_pct": float(np.sqrt((diff ** 2).mean()) / scale * 100),
        "cosine_dist_combined": float(cosine_dist(offline, online)),
        "cosine_dist_tof_only": float(modality_cosine_dist(offline, online, FEATURE_SLICES, "tof")),
        "cosine_dist_mel_only": float(modality_cosine_dist(offline, online, FEATURE_SLICES, "mel")),
    }


def scan_rate_and_duration_scenarios():
    """對照 59 的觀察與協調者指名的兩種惡化情境：ToF 掉幀、8×8@10Hz。"""
    scenarios = [
        dict(name="4x4@30Hz / Mel@62.5Hz, 0.5s, 無掉幀", duration_s=0.5, tof_rate_hz=30.0, mel_rate_hz=62.5, drop=0.0),
        dict(name="4x4@30Hz / Mel@62.5Hz, 1.3s, 無掉幀", duration_s=1.3, tof_rate_hz=30.0, mel_rate_hz=62.5, drop=0.0),
        dict(name="4x4@30Hz / Mel@62.5Hz, 3.0s, 無掉幀", duration_s=3.0, tof_rate_hz=30.0, mel_rate_hz=62.5, drop=0.0),
        dict(name="4x4@30Hz / Mel@62.5Hz, 1.3s, 20% ToF 掉幀（模擬 REC dump）",
             duration_s=1.3, tof_rate_hz=30.0, mel_rate_hz=62.5, drop=0.2),
        dict(name="8x8@10Hz / Mel@62.5Hz, 1.3s, 無掉幀（CONTRACTS §1.4 頻寬表組態）",
             duration_s=1.3, tof_rate_hz=10.0, mel_rate_hz=62.5, drop=0.0),
    ]
    results = []
    for sc in scenarios:
        record = synth_record("word_round", sc["duration_s"], sc["tof_rate_hz"], sc["mel_rate_hz"],
                               seed=42, tof_drop_rate=sc["drop"])
        offline = run_offline_path(record)
        online = run_online_path(record, sc["tof_rate_hz"])
        metrics = compare(offline, online)
        metrics["name"] = metrics_name = sc["name"]
        metrics["n_tof_native"] = record["tof_A"].shape[0]
        metrics["n_mel_native"] = record["mel"].shape[0]
        results.append(metrics)
    return results


def classification_flip_test(duration_s: float, tof_rate_hz: float = 30.0, mel_rate_hz: float = 62.5):
    """兩個可分辨的合成詞，樣板一律用線上（`Aligner`）路徑建（對應 `7c
    [4bedc9]` 正在寫的建樣板腳本）；查詢分別用線上／離線兩條路徑處理
    **同一筆錄音**，看 cosine 最近鄰會不會選錯類別。**分開測合併向量、
    ToF-only、Mel-only 三種**——`compute_tri_result()` 實際上是分開判定
    再融合，只測合併向量會錯過「Mel 這一軌單獨壞掉」這種情況。"""
    templates = {}
    for word in WORD_PATTERNS:
        enroll_record = synth_record(word, duration_s, tof_rate_hz, mel_rate_hz, seed=100)
        templates[word] = run_online_path(enroll_record, tof_rate_hz)

    query_word = "word_round"
    query_record = synth_record(query_word, duration_s, tof_rate_hz, mel_rate_hz, seed=777)
    query_online = run_online_path(query_record, tof_rate_hz)
    query_offline = run_offline_path(query_record)

    def _nearest(query, dist_fn):
        dists = {w: float(dist_fn(query, t)) for w, t in templates.items()}
        best = min(dists, key=dists.get)
        return best, dists

    dist_fns = {
        "combined": cosine_dist,
        "tof_only": lambda a, b: modality_cosine_dist(a, b, FEATURE_SLICES, "tof"),
        "mel_only": lambda a, b: modality_cosine_dist(a, b, FEATURE_SLICES, "mel"),
    }

    result = {"duration_s": duration_s, "true_label": query_word}
    for name, fn in dist_fns.items():
        online_pred, online_dists = _nearest(query_online, fn)
        offline_pred, offline_dists = _nearest(query_offline, fn)
        result[name] = {
            "online_prediction": online_pred, "online_correct": online_pred == query_word,
            "online_dists": online_dists,
            "offline_prediction": offline_pred, "offline_correct": offline_pred == query_word,
            "offline_dists": offline_dists,
        }
    return result


def main():
    print("=== 幀數落差掃描（59 觀察的機制 + 協調者指名的兩種惡化情境）===")
    for r in scan_rate_and_duration_scenarios():
        print(f"- {r['name']}")
        print(f"    原生幀數：ToF {r['n_tof_native']}　Mel {r['n_mel_native']}")
        print(f"    合併向量 cosine(offline,online)：{r['cosine_dist_combined']:.4f}　"
              f"ToF-only：{r['cosine_dist_tof_only']:.4f}　"
              f"Mel-only：{r['cosine_dist_mel_only']:.4f}")
        print(f"    RMS 差：{r['rms_diff']:.3f}（相對 online 幅度 {r['rel_rms_diff_pct']:.1f}%）　"
              f"max|diff|：{r['max_abs_diff']:.3f}")

    print()
    print("=== 分類翻轉測試（樣板一律線上建，查詢分別用線上/離線路徑，分軌測）===")
    for duration_s in (0.5, 1.3, 3.0, 6.0):
        r = classification_flip_test(duration_s)
        print(f"- duration={duration_s}s，真實詞={r['true_label']}")
        for track in ("combined", "tof_only", "mel_only"):
            t = r[track]
            flag = "" if t["offline_correct"] else "  🔴 翻轉！"
            print(f"    [{track}] online→{t['online_prediction']}"
                  f"（{'對' if t['online_correct'] else '錯'}）　"
                  f"offline→{t['offline_prediction']}（{'對' if t['offline_correct'] else '錯'}）{flag}")
            print(f"        online 距離：{t['online_dists']}")
            print(f"        offline 距離：{t['offline_dists']}")


if __name__ == "__main__":
    main()
