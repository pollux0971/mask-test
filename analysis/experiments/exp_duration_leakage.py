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
from host.features.live_pipeline import (  # noqa: E402  只 import，不修改
    assemble_query_from_aligned_frames,
    compute_speech_window,
)
from host.vad.tof_vad import detect_lip_activity  # noqa: E402  只 import，不修改

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

AMPLITUDE = 10.0  # z-score 後的振幅量級，跟 D09 的 magnitude=10、noise=0.15 同一種尺度感（相對噪音的訊噪比類似）
NOISE_STD = 0.15 * AMPLITUDE
# 原本 AMPLITUDE=3.0 量距離用的 cosine 效應沒問題，但拿去餵
# `detect_lip_activity()` 的真實 3σ 進入閾值時，跨 16 個 zone 平均後的
# 能量峰值不夠高，五種時長裡沒有一個能真的偵測到（見驗證這支腳本時的
# 診斷）——改成 10.0 之後在所有測試時長都能穩定偵測到，且合成訊號本身
# 是否被偵測到，不影響第 2 段落「不套用裁切」的距離量測結果不變（該段落
# 不呼叫 `detect_lip_activity()`）。


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


def _reference_energy_floor():
    """模擬 `B10` 的 baseline 期間——一段保證沒有動作的純靜止合成訊號，
    用它算穩定的 `energy_mu`/`energy_sigma`。

    **這不是可省的一步**：驗證這支腳本時發現，讓 `detect_lip_activity()`
    自己從含動作的 trial 裡自動估（不傳 `energy_mu`/`energy_sigma`），
    在按住時長接近講話動作本身長度時（例如 0.5s 按住、0.45s 的動作幾乎
    占滿整段），trial 裡幾乎沒有真正安靜的幀可以估，自動估計器會把
    「這段幾乎全在動」誤判成「baseline 過期」而拒絕偵測——這正是
    `host/vad/tof_vad.py` 模組文件裡說的「拿得到乾淨的靜止資料時，請
    明確傳 baseline 期間算好的值，那比從含動作的 trial 自己估準得多」。
    跟 `analysis/run_all.py::_speech_window_for_trial()` 用
    `session.meta.get("energy_mu")` 是同一個原則，這裡沒有真的 session，
    用一段乾淨的合成靜止訊號代替。
    """
    quiet = synth_hold("word_A", 3.0, seed=999, amplitude=0.0)
    from host.vad.tof_vad import zone_energy, estimate_energy_floor
    energy, _, _ = zone_energy(quiet["tof_a"], BASELINE_MU, BASELINE_SIGMA)
    return estimate_energy_floor(energy)


_REFERENCE_ENERGY_MU, _REFERENCE_ENERGY_SIGMA = _reference_energy_floor()


def build_query(record, tof_rate_hz=TOF_RATE_HZ, use_trim=False):
    """真正呼叫 `Aligner` + `assemble_query_from_aligned_frames()`。

    `use_trim=True`：**修好之後的路徑**——用真正的 `detect_lip_activity()`
    （唇動，兩顆感測器都測，餵 `_reference_energy_floor()` 算好的穩定
    baseline 能量門檻）算出 `speech_window`，裁到「真的在講話」那段再
    重採樣（見 `host/features/live_pipeline.py` 的 `compute_speech_window()`）。
    這裡沒有語音 VAD（合成資料沒有麥克風訊號），只用唇動——跟
    `analysis/run_all.py::_speech_window_for_trial()` 的差別只在於「這支
    腳本沒有 mic_rms 可以餵」，裁切機制本身是同一份程式碼。
    `use_trim=False`（預設）：修好之前的行為，整段不裁切，用來對照。
    """
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

    speech_window = None
    if use_trim:
        lip_a = detect_lip_activity(record["tof_a"], record["tof_t_us"], BASELINE_MU, BASELINE_SIGMA,
                                     energy_mu=_REFERENCE_ENERGY_MU, energy_sigma=_REFERENCE_ENERGY_SIGMA)
        lip_b = detect_lip_activity(record["tof_b"], record["tof_t_us"], BASELINE_MU, BASELINE_SIGMA,
                                     energy_mu=_REFERENCE_ENERGY_MU, energy_sigma=_REFERENCE_ENERGY_SIGMA)
        segments = []
        if lip_a.detected:
            segments.append(("lip_A", lip_a.primary.start_us, lip_a.primary.end_us))
        if lip_b.detected:
            segments.append(("lip_B", lip_b.primary.start_us, lip_b.primary.end_us))
        speech_window = compute_speech_window(segments)

    seq = assemble_query_from_aligned_frames(frames, BASELINE_MU, BASELINE_SIGMA,
                                              BASELINE_MU, BASELINE_SIGMA,
                                              speech_window=speech_window)
    return seq.data, speech_window


DURATIONS_S = (0.5, 1.0, 2.0, 4.0, 5.0)


def scan_duration_vs_word_distance(use_trim=False):
    """對每個按住時長各建一次 word_A/word_B 的向量，量：
    - 同一個詞、不同時長的距離（該死的方向，理想值應該很小）
    - 不同詞、同一時長的距離（訊號的量級參考）

    `use_trim=True`：套用 `compute_speech_window()` 裁切（修好之後的路徑）。
    """
    queries = {
        (word, d): build_query(synth_hold(word, d, seed=1 if word == "word_A" else 2),
                                use_trim=use_trim)[0]
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


def classification_test(reference_duration=2.0, use_trim=False):
    """樣板在一個「典型」按住時長下建，查詢用不同的按住時長，看 cosine
    最近鄰會不會選錯詞——這是「長度會不會實際造成誤判」的直接測試，
    比單純看距離數字更貼近後果。"""
    templates = {
        word: build_query(synth_hold(word, reference_duration, seed=100 + i), use_trim=use_trim)[0]
        for i, word in enumerate(WORD_PATTERNS)
    }
    results = []
    for true_word in WORD_PATTERNS:
        for d in DURATIONS_S:
            query, _ = build_query(synth_hold(true_word, d, seed=777), use_trim=use_trim)
            dists = {w: float(cosine_dist(query, t)) for w, t in templates.items()}
            pred = min(dists, key=dists.get)
            results.append({
                "true_word": true_word, "duration": d, "pred": pred,
                "correct": pred == true_word, "dists": dists,
            })
    return results


def _report_pass(use_trim, label):
    same_word, diff_word, cross, _ = scan_duration_vs_word_distance(use_trim=use_trim)

    print(f"\n########## {label} ##########")
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
    for r in classification_test(use_trim=use_trim):
        flag = "" if r["correct"] else "  🔴 翻轉！"
        print(f"  真實詞={r['true_word']} 按住={r['duration']}s → 判定={r['pred']}"
              f"（{'對' if r['correct'] else '錯'}）{flag}  距離={r['dists']}")

    return max_same_word_dist, min_diff_word_dist


def main():
    old_max, old_min = _report_pass(use_trim=False, label="修之前（整段不裁切，舊行為）")
    new_max, new_min = _report_pass(use_trim=True, label="修之後（裁到「真的在講話」那段）")

    print("\n########## 新舊對照 ##########")
    print(f"同一詞不同長度最大距離：修前 {old_max:.4f} → 修後 {new_max:.4f}")
    print(f"不同詞相同長度最小距離：修前 {old_min:.4f} → 修後 {new_min:.4f}")
    old_ratio = old_max / old_min if old_min else float("inf")
    new_ratio = new_max / new_min if new_min else float("inf")
    print(f"「長度效應／詞義效應」比例：修前 {old_ratio:.1%} → 修後 {new_ratio:.1%}")
    if new_max < old_max:
        print(f"✅ 洩漏確實變小了（同一詞不同長度的最大距離下降 {(1 - new_max / old_max):.1%}）")
    else:
        print("🔴 洩漏沒有變小，甚至變大——裁切沒有解決問題，這個結論比「修好了」更有價值，照實回報")

    print("\n=== 4s vs 5s 這個改動本身有沒有額外影響（修之後的路徑）===")
    d4, w4 = build_query(synth_hold("word_A", 4.0, seed=1), use_trim=True)
    d5, w5 = build_query(synth_hold("word_A", 5.0, seed=1), use_trim=True)
    print(f"  word_A @4s vs @5s cosine 距離：{cosine_dist(d4, d5):.4f}")
    print(f"  ToF-only：{modality_cosine_dist(d4, d5, FEATURE_SLICES, 'tof'):.4f}　"
          f"Mel-only：{modality_cosine_dist(d4, d5, FEATURE_SLICES, 'mel'):.4f}")
    print(f"  裁切窗口 @4s：{w4.to_dict()}")
    print(f"  裁切窗口 @5s：{w5.to_dict()}")


if __name__ == "__main__":
    main()
