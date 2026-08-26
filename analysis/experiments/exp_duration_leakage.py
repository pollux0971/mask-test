"""量測「按住多久」（hold-to-record 的錄音總長度）會不會洩漏進
`D03` 的固定長度 `(T=24, 104)` 特徵向量。

## 由來

使用者剛把 `HOLD_MAX_DURATION_S` 從 `5.0` 降到 `4.0`（`esp-mask-test-4f`
正在改 `host/trial/state_machine.py`，這裡不碰）。這個改動本身只是 UX
決定，但浮出一個更根本的問題：`assemble_feature_seq()`/
`resample_fixed_length()`（`analysis/features/feature_assembly.py`）
不管原始錄了幾秒，一律**按索引**線性內插成 `T=24` 幀，而且**沒有 VAD
裁切**——`host/features/live_pipeline.py` 與 `analysis/run_all.py` 的
模組文件都明講「整段不裁切」（`live_pipeline.py` 第 22-25 行：
「`mel_features()` 的 `vad_start`/`vad_end` 兩個參數留空……因為沒有 VAD
起訖點的即時 producer」）。

也就是說一筆錄音「按住總長」直接決定了「講話那段動作」在 24 幀裡占多大
比例：按得剛剛好，動作占滿整個 24 幀；按得比較久，動作被稀釋成一小段，
其餘幀都是「講完話還按著」的靜止畫面。**如果這個稀釋比例本身變成一個
比詞義更穩定的特徵，分類器可能學到的是「這個人按多久」而不是「這個詞
長怎樣」**——LOOCV 上會表現得很好（同一個人同一次錄音，按鍵習慣一致），
Demo 當天（緊張、節奏跟練習不同）就會崩掉。

## 方法

真正呼叫 `host/align/aligner.py` 的 `Aligner` +
`host/features/live_pipeline.py` 的 `assemble_query_from_aligned_frames()`
（不重寫一份，量的是實際在跑的程式碼——跟 `exp_alignment_mismatch.py`
同一個原則），造「固定時長的說話動作 + 之後保持不動直到放開按鍵」的
合成資料，在不同「按住總長」下跑一次，比較：

- **同一個詞、不同按住時長**的 cosine 距離
- **不同詞、相同按住時長**的 cosine 距離（當作「訊號」的量級參考）

**如果前者 ≥ 後者，長度就是主導特徵。**

⚠️ **避開 `D22.md` 提過的天花板效應**：訊號振幅刻意跟既有的 D09/
`exp_alignment_mismatch.py` 合成實驗同量級（z-score 後 O(1)~O(3)），
不刻意調大或調小到讓長度效應被訊號強度蓋過去或誇大。

**只做量測，不改 `feature_assembly.py`/`host/trial/state_machine.py`。**
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from analysis.similarity.cosine_baseline import cosine_dist, modality_cosine_dist  # noqa: E402
from host.align.aligner import Aligner  # noqa: E402
from host.features.live_pipeline import assemble_query_from_aligned_frames  # noqa: E402  只 import，不修改

N_TOF_ZONES = 16
N_MEL_BANDS = 40
TOF_RATE_HZ = 30.0
MEL_RATE_HZ = 62.5
FEATURE_SLICES = {"tof": slice(0, 64), "mel": slice(64, 104)}

# 假設一個詞「實際講話的動作」大約需要這麼多秒——跟按住多久無關，
# 是嘴型/發音本身的物理時長，這是整個實驗裡唯一不隨情境改變的量。
SPEECH_DURATION_S = 0.45

# z-score 後直接看原始值：baseline mu=0/sigma=1，理由跟
# `exp_alignment_mismatch.py` 一樣——讓合成訊號的振幅本身就是 z-score
# 後的尺度，不用另外假裝一個 baseline。
BASELINE_MU = np.zeros(2 * N_TOF_ZONES)
BASELINE_SIGMA = np.ones(2 * N_TOF_ZONES)

# 兩個「詞」：哪些 zone/band 在動，代表哪個詞——用「哪裡動」區分詞，
# 比用「動多快」更貼近真實語意（不同唇形動不同的 zone，不是動得比較快）。
WORD_PATTERNS = {
    "word_A": {"tof_zones": range(0, 5), "mel_bands": range(0, 5), "sign": 1.0},
    "word_B": {"tof_zones": range(8, 13), "mel_bands": range(20, 25), "sign": -1.0},
}

AMPLITUDE = 3.0   # z-score 後的振幅量級，跟 D09 的 magnitude=10、noise=0.15 同一種尺度感（相對噪音的訊噪比類似）
NOISE_STD = 0.15 * AMPLITUDE


def _bump(t, center, width):
    """平滑鐘形曲線，代表一次發音動作：從 0 升起、回到 0。"""
    return np.exp(-0.5 * ((t - center) / width) ** 2)


def synth_hold(word, hold_duration_s, *, tof_rate_hz=TOF_RATE_HZ, mel_rate_hz=MEL_RATE_HZ,
               speech_duration_s=SPEECH_DURATION_S, seed=0,
               amplitude=AMPLITUDE, noise_std=NOISE_STD):
    """模擬 hold-to-record：按下就開始錄，講話動作固定發生在按下後
    `speech_duration_s` 秒內（bump），之後一路持平（保持按著、已經講完）
    到放開按鍵。`hold_duration_s`（按了多久）是唯一隨情境變化的量。"""
    rng = np.random.default_rng(seed)
    pattern = WORD_PATTERNS[word]

    t_tof = np.arange(0.0, hold_duration_s, 1.0 / tof_rate_hz)
    t_mel = np.arange(0.0, hold_duration_s, 1.0 / mel_rate_hz)

    bump_center = speech_duration_s / 2
    bump_width = speech_duration_s / 4
    bump_tof = _bump(t_tof, bump_center, bump_width) * amplitude * pattern["sign"]
    bump_mel = _bump(t_mel, bump_center, bump_width) * amplitude * pattern["sign"]

    tof_a = np.zeros((len(t_tof), 2 * N_TOF_ZONES))
    tof_b = np.zeros((len(t_tof), 2 * N_TOF_ZONES))
    mel = np.zeros((len(t_mel), N_MEL_BANDS))
    for z in pattern["tof_zones"]:
        tof_a[:, z] += bump_tof
        tof_b[:, z] += bump_tof * 0.9  # 兩顆感測器角度不同，量到的幅度略有差異，較真實
    for b in pattern["mel_bands"]:
        mel[:, b] += bump_mel

    tof_a += rng.normal(0, noise_std, tof_a.shape)
    tof_b += rng.normal(0, noise_std, tof_b.shape)
    mel += rng.normal(0, noise_std, mel.shape)

    return dict(
        tof_a=tof_a, tof_b=tof_b,
        valid_a=np.ones((len(t_tof), N_TOF_ZONES), dtype=bool),
        valid_b=np.ones((len(t_tof), N_TOF_ZONES), dtype=bool),
        tof_t_us=(t_tof * 1e6).astype(np.int64),
        mel=mel, mel_t_us=(t_mel * 1e6).astype(np.int64),
    )


def build_query(record, tof_rate_hz=TOF_RATE_HZ):
    """真正呼叫 `Aligner` + `assemble_query_from_aligned_frames()`。"""
    aligner = Aligner()
    n_tof = record["tof_a"].shape[0]
    for i in range(n_tof):
        t_us = int(record["tof_t_us"][i])
        aligner.push_tof("A", t_us, list(record["tof_a"][i, :N_TOF_ZONES]),
                          list(record["tof_a"][i, N_TOF_ZONES:]), list(record["valid_a"][i]))
        aligner.push_tof("B", t_us, list(record["tof_b"][i, :N_TOF_ZONES]),
                          list(record["tof_b"][i, N_TOF_ZONES:]), list(record["valid_b"][i]))
    for i in range(record["mel"].shape[0]):
        aligner.push_mel(int(record["mel_t_us"][i]), record["mel"][i])

    frames = list(aligner.frames(int(record["tof_t_us"][0]), int(record["tof_t_us"][-1]),
                                  rate_hz=tof_rate_hz))
    seq = assemble_query_from_aligned_frames(frames, BASELINE_MU, BASELINE_SIGMA,
                                              BASELINE_MU, BASELINE_SIGMA)
    return seq.data


DURATIONS_S = (0.5, 1.0, 2.0, 4.0, 5.0)


def scan_duration_vs_word_distance():
    """對每個按住時長各建一次 word_A/word_B 的向量，量：
    - 同一個詞、不同時長的距離（該死的方向，理想值應該很小）
    - 不同詞、同一時長的距離（訊號的量級參考）
    """
    queries = {
        (word, d): build_query(synth_hold(word, d, seed=1 if word == "word_A" else 2))
        for word in WORD_PATTERNS for d in DURATIONS_S
    }

    same_word_diff_duration = []
    for word in WORD_PATTERNS:
        for i, d1 in enumerate(DURATIONS_S):
            for d2 in DURATIONS_S[i + 1:]:
                dist = cosine_dist(queries[(word, d1)], queries[(word, d2)])
                same_word_diff_duration.append({"word": word, "d1": d1, "d2": d2, "dist": float(dist)})

    diff_word_same_duration = []
    for d in DURATIONS_S:
        dist = cosine_dist(queries[("word_A", d)], queries[("word_B", d)])
        diff_word_same_duration.append({"duration": d, "dist": float(dist)})

    cross = []
    for d1 in DURATIONS_S:
        for d2 in DURATIONS_S:
            dist = cosine_dist(queries[("word_A", d1)], queries[("word_B", d2)])
            cross.append({"d1": d1, "d2": d2, "dist": float(dist)})

    return same_word_diff_duration, diff_word_same_duration, cross, queries


def classification_test(reference_duration=2.0):
    """樣板在一個「典型」按住時長下建，查詢用不同的按住時長，看 cosine
    最近鄰會不會選錯詞——這是「長度會不會實際造成誤判」的直接測試，
    比單純看距離數字更貼近後果。"""
    templates = {
        word: build_query(synth_hold(word, reference_duration, seed=100 + i))
        for i, word in enumerate(WORD_PATTERNS)
    }
    results = []
    for true_word in WORD_PATTERNS:
        for d in DURATIONS_S:
            query = build_query(synth_hold(true_word, d, seed=777))
            dists = {w: float(cosine_dist(query, t)) for w, t in templates.items()}
            pred = min(dists, key=dists.get)
            results.append({
                "true_word": true_word, "duration": d, "pred": pred,
                "correct": pred == true_word, "dists": dists,
            })
    return results


def main():
    same_word, diff_word, cross, _ = scan_duration_vs_word_distance()

    print("=== 同一個詞、不同按住時長的 cosine 距離 ===")
    for r in same_word:
        print(f"  [{r['word']}] {r['d1']}s vs {r['d2']}s: {r['dist']:.4f}")
    max_same_word_dist = max(r["dist"] for r in same_word)

    print("\n=== 不同詞、相同按住時長的 cosine 距離（訊號量級參考）===")
    for r in diff_word:
        print(f"  duration={r['duration']}s: {r['dist']:.4f}")
    min_diff_word_dist = min(r["dist"] for r in diff_word)

    print(f"\n同一詞不同長度最大距離：{max_same_word_dist:.4f}")
    print(f"不同詞相同長度最小距離：{min_diff_word_dist:.4f}")
    if max_same_word_dist >= min_diff_word_dist:
        print("🔴 長度造成的距離 >= 詞義造成的距離——長度是主導特徵")
    else:
        print(f"✅ 長度造成的距離明顯小於詞義造成的距離（比例 {max_same_word_dist / min_diff_word_dist:.1%}）")

    print("\n=== 交叉情境：不同詞 + 不同長度（最貼近真實 Demo）===")
    for r in cross:
        flag = "  ⚠️ 比同詞同長度還近" if r["d1"] != r["d2"] else ""
        print(f"  word_A@{r['d1']}s vs word_B@{r['d2']}s: {r['dist']:.4f}{flag}")

    print("\n=== 分類測試：樣板固定在 2.0s 建，查詢用不同按住時長 ===")
    for r in classification_test():
        flag = "" if r["correct"] else "  🔴 翻轉！"
        print(f"  真實詞={r['true_word']} 按住={r['duration']}s → 判定={r['pred']}"
              f"（{'對' if r['correct'] else '錯'}）{flag}  距離={r['dists']}")

    print("\n=== 4s vs 5s 這個改動本身有沒有額外影響 ===")
    d4 = build_query(synth_hold("word_A", 4.0, seed=1))
    d5 = build_query(synth_hold("word_A", 5.0, seed=1))
    print(f"  word_A @4s vs @5s cosine 距離：{cosine_dist(d4, d5):.4f}")
    print(f"  ToF-only：{modality_cosine_dist(d4, d5, FEATURE_SLICES, 'tof'):.4f}　"
          f"Mel-only：{modality_cosine_dist(d4, d5, FEATURE_SLICES, 'mel'):.4f}")


if __name__ == "__main__":
    main()
