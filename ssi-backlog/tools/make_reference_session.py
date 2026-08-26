#!/usr/bin/env python3
"""D22 附帶任務 — 產生一份「多詞、多次戴、雙感測器」的合成參考 session，
讓 `analysis/run_all.py` 的五張驗證卡（`C0`/`A`/`B`/`C`/`E`）**都真的算得出
數字**，而不是因為資料只有單一詞、單一 `wear_id` 而被跳過。

## 為什麼需要這支工具

`ca`（驗證介面）、`ed`（降級 session 調查）、`8f`（裁切驗證）手上能跑的
資料集都是單一詞，導致：
* `extras.d19_dual_matrix` 永遠是 `{}`（沒有第二個類別可以分類）
* 分組驗證三態（`D18`）永遠落在 `ungrouped_single_group`（只有一個
  `wear_id`）
* 沒有真的 p 值/準確率可以看渲染結果長什麼樣

這支工具是唯一的解法：**合成一份符合五個實驗前提的資料**，而不是等
`E05` 的真板子資料（那要等硬體排練完成）。

## 用法

    python3 ssi-backlog/tools/make_reference_session.py [--out-dir DIR]

預設輸出到 `./reference_session/`（**不要**指到 `sessions/`——那是使用者
錄真實資料的地方）。輸出 6 個檔案：

* `main_wear0.h5` / `main_wear1.h5` / `main_wear2.h5`——3 次「戴上」，
  每次戴上完整詞彙集（`config/vocab.json` 的 8 個詞 + `_reject`），每詞
  每次戴上 2 筆（`D18`/`D19` 需要的「每類 >= 6 筆」由跨 3 次戴上累積）。
* `crosstalk_dual_wear99.h5` / `crosstalk_soloA_wear99.h5` /
  `crosstalk_soloB_wear99.h5`——`C0` 串擾要的三組對照錄製（同一次戴上，
  兩顆都開 vs 只開 A vs 只開 B），純靜止、不含詞彙訊號，避免跟「有沒有
  在講話」混在一起算成串擾。

跑完驗證（`analysis/run_all.py` 目前在改動中，只跑不改）：

    python3 -m analysis.run_all \\
        --session reference_session/main_wear0.h5 \\
        --session reference_session/main_wear1.h5 \\
        --session reference_session/main_wear2.h5 \\
        --session reference_session/crosstalk_dual_wear99.h5 \\
        --session reference_session/crosstalk_soloA_wear99.h5 \\
        --session reference_session/crosstalk_soloB_wear99.h5 \\
        --out reference_session/report --ablation-permutations 1000

`main_wear0.h5` **必須排第一個**——`run_all.run_snr()` 只用
`sessions[0]` 的 `baseline_mu_*` 算 SNR 的 sigma 地板，這裡刻意把它做成
全 zone 齊平（見 `_flat_baseline_mu()`），讓地板落在
`exp_a_snr.SIGMA_FLOOR`（量化雜訊理論下限），不是隨便一個猜的數字。

## 合成訊號設計，避開 `D22.md`「合成資料的已知陷阱」列的五個坑

1. **維度詛咒**：每個詞的訊號都攤在 4-5 個 zone／3-5 個 mel band，不是
   單一通道（`exp_c_silhouette.py` 的模組說明直接記錄過：單一 zone/band
   會把 Silhouette 打到 0.05-0.08）。
2. **天花板效應**：`wu`/`yi`/`hao`/`buyao` 這組跟 `ting` 共用完全相同的
   `tof_l` 訊號、`yi`/`buyao` 共用完全相同的 `tof_r` 訊號（見
   `WORD_SPEC` 裡的 `twin_*` 註解）——單一感測器分不開這幾對詞，只有
   合併雙感測器才分得開，這樣 `D19` 的 `dual_matrix_vs_single` 增益跟
   `C` 的 complementarity 檢查才有東西可以量，不會一開始就 100%。
3. **共同常數 + cosine**：跨次戴的變異（`wear_bias_*`）是**逐 zone 不等量
   的偏移**（每個 zone 各自抽一個小偏移量），不是對全部維度加同一個
   常數——見 `exp_d12_wear_cv.py` 模組說明對這個陷阱的明文警告。
4. **單一幾何**：`WORD_SPEC` 的 8 個詞、`WEAR_BIAS` 的 3 次戴上都是各自
   獨立設計的 zone/band 組合，不是同一組參數複製貼上再加雜訊。
5. **生成幾何與抽樣分開**：`WORD_SPEC`/`WEAR_BIAS`/`CROSSTALK_SPEC` 都是
   模組層級的常數字面值，跟下面「抽幾筆、加多少雜訊」的隨機數完全分開
   ——改 `TRIALS_PER_WORD_PER_WEAR` 不會讓任何詞的幾何跟著變。

## 一定要老實講的事

`si`（擦音，viseme F）的 ToF 訊號刻意設成貼著雜訊底（`tof_peak=WEAK`，
即完全不給主動訊號）——這是詞彙集設計本身的預期（CONTRACTS.md §6：
「四」ToF 弱、音訊強），不是沒調好。**如果 `E`（viseme 敏感度）那張卡
在真實資料上判不出來，不代表這支工具的問題**——真正的擦音頻譜紋理
（噪聲樣的寬頻能量）不是這裡「在幾個 mel band 加一個能量突起」能模擬的，
這裡只能驗證 pipeline 跑不跑得動、格式對不對，驗不了「聲學上像不像真的
擦音」。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[2]  # ssi-backlog/tools/ -> ssi-backlog -> repo root
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
from host.storage.session_writer import SessionWriter, TOF_VALID_DIM, MEL_BANDS  # noqa: E402

SCHEMA_VERSION = 1
VOCAB_PATH = ROOT_DIR / "config" / "vocab.json"

# ------------------------------------------------------------------ 取樣率／時間軸

TOF_HZ = 30.0
MEL_HZ = 62.5
MIC_HZ = 31.25

PRE_ROLL_S = 0.3     # trial 開頭的純靜止
ACTIVE_S = 1.2        # 唇動／發聲的完整視窗（含攻擊/持續/衰減）
POST_ROLL_S = 0.5     # trial 結尾的純靜止
TRIAL_S = PRE_ROLL_S + ACTIVE_S + POST_ROLL_S  # 2.0s

ATTACK_S = 0.15   # 快攻——真實嘴唇/聲門起音快
DECAY_S = 0.5     # 慢放——比攻擊長，讓包絡不對稱（D19 時間反轉檢定才有東西可測）
SUSTAIN_S = ACTIVE_S - ATTACK_S - DECAY_S

LIP_LEAD_S = 0.12   # 這個專案的命題：唇動要比出聲早（`measure_lip_lead()`）

N_ZONES = TOF_VALID_DIM  # 16

# ------------------------------------------------------------------ 感測器雜訊/底噪模型

FLAT_DISTANCE_MU_MM = 80.0
FLAT_SIGNAL_MU = 50.0
DISTANCE_SIGMA_MM = 0.35     # 略高於 SIGMA_FLOOR(≈0.289mm)，量化雜訊等級
SIGNAL_SIGMA = 1.0

MEL_BASE_LEVEL = 40.0
MEL_BASE_SLOPE = -0.3        # 高頻 band 能量略低，接近真實 log-mel 頻譜的形狀
MEL_NOISE_SIGMA = 1.2

NOISE_FLOOR_MU = 5.0         # 真板子安靜時骨傳導麥克風 RMS 的實測量級
NOISE_FLOOR_SIGMA = 1.0
MIC_PEAK_SCALE = 15.0        # peak ≈ rms * 這個比例

# ToF VAD 能量門檻（B16 docstring）：N=16 zone 時 |z| 的半常態統計量，
# 這裡直接算開放公式，不是複製一個實測數字——baseline 是這裡自己生成的，
# 理論值本來就該準。
ENERGY_MU = math.sqrt(2.0 / math.pi)
ENERGY_SIGMA = math.sqrt((1.0 - 2.0 / math.pi) / N_ZONES)

RNG_SEED = 20260826  # 固定種子：同一份幾何每次重跑都一樣，方便對照報告數字

# ------------------------------------------------------------------ 每個詞的訊號設計
#
# tof_peak_a/b：該感測器「主動」zone 在包絡頂點的距離變化量（mm，變近＝
# 負值方向，這裡存正數，套用時取負號）。signal_peak 同理，單位是
# signal_per_spad/100。mel_peak：mel_bands 在包絡頂點的 log-mel 增量
# （CMN 後的量級，不必再除以 sigma——mel 沒有 z-score 這一步，見
# `analysis/features/feature_assembly.py` 的模組文件）。
#
# 強度分級數字取自 D14 的 STRENGTH_THRESHOLDS／CONTRACTS §6 的預期表，
# 實際數字是「跑過 analysis/run_all.py 校過的」，不是抄書上的理論值。

WEAK, MEDIUM, MEDIUM_STRONG, STRONG = 0.0, 8.0, 12.0, 18.0
MEL_MEDIUM, MEL_MEDIUM_STRONG, MEL_STRONG = 8.0, 11.0, 20.0

WORD_SPEC = {
    # CONTRACTS §6：A 雙唇——兩顆 ToF 都強，Mel 中強。
    "ba":    dict(a_zones=[0, 1, 2],    tof_peak_a=STRONG,
                  b_zones=[0, 1, 2],    tof_peak_b=STRONG,
                  mel_bands=[2, 3, 4, 5], mel_peak=MEL_MEDIUM_STRONG),
    # B 圓唇——ToF 強（SNR 對照組：round）。跟 yi 的 zone 完全不重疊，
    # 兩者的差在 exp_a_snr 才不會被互相抵消。
    "wu":    dict(a_zones=[3, 4, 5, 6, 7], tof_peak_a=STRONG,
                  b_zones=[3, 4, 5, 6, 7], tof_peak_b=STRONG,
                  mel_bands=[8, 9, 10],  mel_peak=MEL_MEDIUM),
    # C 展唇——ToF 中/中強（SNR 對照組：spread）。
    "yi":    dict(a_zones=[8, 9, 10, 11], tof_peak_a=MEDIUM,
                  b_zones=[9, 10, 11, 12], tof_peak_b=MEDIUM_STRONG,
                  mel_bands=[12, 13, 14], mel_peak=MEL_MEDIUM),
    # D 開口——三個模態都中強。
    "a":     dict(a_zones=[1, 2, 3, 12],  tof_peak_a=MEDIUM_STRONG,
                  b_zones=[13, 14, 15, 0], tof_peak_b=MEDIUM_STRONG,
                  mel_bands=[16, 17, 18, 19], mel_peak=MEL_MEDIUM_STRONG),
    # F 擦音——ToF 刻意貼底噪（見模組文件的「一定要老實講的事」），Mel 強。
    # `audio_shape`：短突發（攻擊 20ms／持續 60ms／衰減 30ms，共 ~110ms），
    # 刻意跟其他詞的 1.2s 長包絡不同——見 `envelope()` 的說明，D14 的敏感度
    # 現在會先對 Mel 做 `_locally_standardize()`（除以這筆樣本自己的
    # std），振幅不影響比值、形狀才影響。這個數字不是理論推出來的，是
    # 掃過 5 組 (attack,sustain,decay,peak) 實測 `fricative_check()` 篩出
    # 來的：110ms 這組在保持 VAD 偵測得到的前提下（太短會連
    # `detect_voice_activity()` 都偵測不到）給出最高的 peak/std 對比，
    # 190ms 那組（原本的設計）量出來反而落在雜訊地板附近判不出來。
    "si":    dict(a_zones=[], tof_peak_a=WEAK,
                  b_zones=[], tof_peak_b=WEAK,
                  mel_bands=[25, 26, 27, 28, 29], mel_peak=MEL_STRONG,
                  audio_shape=(0.02, 0.06, 0.03)),
    # G 應用（無預期）——`tof_l` 訊號故意跟 `ting` 完全相同（twin_l），
    # 只有 `tof_r` 不同，讓「只用單一感測器」在這一對詞上會混淆，逼合併
    # 雙感測器才分得開（避免天花板效應，見模組文件第 2 點）。
    "hao":   dict(a_zones=[5, 6, 13],   tof_peak_a=MEDIUM_STRONG,      # twin_l with ting
                  b_zones=[1, 2, 3],    tof_peak_b=MEDIUM_STRONG,
                  mel_bands=[6, 7, 20], mel_peak=MEL_MEDIUM),
    "ting":  dict(a_zones=[5, 6, 13],   tof_peak_a=MEDIUM_STRONG,      # twin_l with hao
                  b_zones=[10, 11, 12], tof_peak_b=MEDIUM_STRONG,
                  mel_bands=[21, 22, 23], mel_peak=MEL_MEDIUM_STRONG),
    # `buyao` 的 `tof_r` 故意跟 `yi` 完全相同（twin_r），道理同上。
    "buyao": dict(a_zones=[13, 14, 15], tof_peak_a=STRONG,
                  b_zones=[9, 10, 11, 12], tof_peak_b=MEDIUM_STRONG,   # twin_r with yi
                  mel_bands=[30, 31, 32], mel_peak=MEL_MEDIUM),
}
REJECT_SPEC = dict(a_zones=[], tof_peak_a=WEAK, b_zones=[], tof_peak_b=WEAK,
                    mel_bands=[], mel_peak=0.0)

TRIALS_PER_WORD_PER_WEAR = 2
WEAR_IDS = (0, 1, 2)

# 額外加的 `silent` 模式 trial（每次戴上 1 筆，用 `hao` 的唇動幾何但沒有
# 聲音）——寄放派任第 6 點要求「silent 模式的 trial 也要有幾筆」。
SILENT_LABEL = "hao"

# ------------------------------------------------------------------ 跨次戴變異（B/D12 CV）
#
# 刻意做成「逐 zone 不等量的偏移」，不是整體平移常數——見模組文件第 3 點，
# 這是 `exp_d12_wear_cv.py` 模組文件明講的陷阱（整體平移對 cosine 距離
# 是隱形的）。數值等級（±1.5mm 上下）遠小於詞彙訊號的 6-18mm，
# 確保 `cv_between`（門檻 30%）壓得住。

WEAR_BIAS_MM = {
    0: np.array([0.0, 0.3, -0.2, 0.5, -0.4, 0.1, 0.6, -0.3,
                 0.2, -0.5, 0.4, 0.0, -0.1, 0.3, -0.6, 0.2], dtype=np.float64),
    1: np.array([1.2, -0.8, 1.5, -0.6, 0.9, -1.1, 0.7, -0.4,
                 1.0, -0.9, 0.5, -1.3, 0.8, -0.5, 1.1, -0.7], dtype=np.float64),
    2: np.array([-1.4, 0.6, -0.9, 1.3, -0.5, 0.8, -1.2, 0.4,
                 -0.7, 1.1, -0.6, 0.9, -1.0, 0.5, -0.8, 1.4], dtype=np.float64),
}

# ------------------------------------------------------------------ C0 串擾設計
#
# 感測器 A：solo vs dual 的差控制在 < 2mm（PASS）。
# 感測器 B：其中一個 zone 刻意 >= 2mm（FAIL）——見驗收條件「有 PASS 也有
# FAIL，不要全綠」。這是 A/B 兩顆感測器裡唯一刻意設計成失敗的一張卡，
# 沒有牽動分類 pipeline，風險最小。
CROSSTALK_DELTA_A_MM = {z: 0.0 for z in range(N_ZONES)}
CROSSTALK_DELTA_A_MM.update({2: 0.8, 7: -1.1})   # 都在 2mm 門檻內

CROSSTALK_DELTA_B_MM = {z: 0.0 for z in range(N_ZONES)}
CROSSTALK_DELTA_B_MM.update({4: 1.0, 9: -3.2})   # zone 9 超過 2mm 門檻，故意 FAIL

N_CROSSTALK_TRIALS = 4

# 專用的串擾測試 wear_id，刻意跟 3 個主要詞彙 wear_id（0/1/2）不同——
# `crosstalk_pairs()` 只用 `wear_id` 配對，如果沿用 `wear_id=0`，會跟
# `main_wear0.h5`（同樣是 wear_id=0、sensors_enabled="AB"）一起被分進
# 同一組「兩顆都開」候選，`run_crosstalk()` 只取第一組配對，可能配到
# 「跟有講話內容的 main_wear0 比」而不是「跟純靜止的 crosstalk_dual 比」
# ——這是這支工具第一版實測抓到的真實 bug，不是假設性的。
CROSSTALK_WEAR_ID = 99


def load_words():
    data = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    words = [w["id"] for w in data["words"]]
    reject = data["reject"]["id"]
    return words, reject


def envelope(t_s, onset_s, attack_s=ATTACK_S, sustain_s=SUSTAIN_S, decay_s=DECAY_S):
    """0..1 的不對稱包絡：快攻、平頂、慢放（見模組文件）。

    `attack_s`/`sustain_s`/`decay_s` 可覆寫——`si`（擦音）用短很多的形狀
    （見 `WORD_SPEC["si"]["audio_shape"]`）：D14 的敏感度是「這個模態在
    這筆樣本自己的時間軸上除以自己的標準差」（`_locally_standardize()`），
    不是除以一個固定常數，所以**振幅不影響比值，形狀才影響**——一個占滿
    整個偵測窗的平頂訊號，扣掉平均、除以自己的標準差之後量值反而不大；
    真正的擦音在時域上本來就是短促的突發，讓合成訊號也做成短突發
    （相對偵測窗大部分時間貼底噪），比值才會被拉開。這是這支工具實測
    (跑 `analysis/run_all.py` 校過)才發現、不是先驗猜的。
    """
    if t_s < onset_s:
        return 0.0
    if t_s < onset_s + attack_s:
        return (t_s - onset_s) / attack_s
    if t_s < onset_s + attack_s + sustain_s:
        return 1.0
    end = onset_s + attack_s + sustain_s + decay_s
    if t_s < end:
        return 1.0 - (t_s - onset_s - attack_s - sustain_s) / decay_s
    return 0.0


def _flat_baseline_mu(rng, wear_bias_mm=None):
    """幾乎齊平的 baseline，讓 `exp_a_snr.zone_snr()` 的 sigma 地板落在
    `SIGMA_FLOOR`（量化雜訊理論下限），不是被「baseline 本身的
    zone-to-zone 差異」意外撐大——見模組文件的用法說明，只有
    `sessions[0]`（也就是 `main_wear0.h5`）的這個值真的會被用到。

    `wear_bias_mm`（16 維，可省略）**一定要**跟 `synth_tof()` 加在 trial
    資料上的同一份戴法偏移相加——真實情況下 baseline 是「這次戴上」開始
    時才量的，本來就會反映這次戴的物理偏移。這裡如果不加，trial 讀值與
    baseline_mu 之間會多出一個跟戴法偏移等量的系統性落差，被下游 z-score
    誤讀成「唇動訊號」——`si`/`_reject` 這種本來不該有 ToF 訊號的詞會被
    這個假訊號污染，是這支工具第一版實測抓到的真實 bug，不是假設性的。
    """
    bias = wear_bias_mm if wear_bias_mm is not None else np.zeros(N_ZONES)
    distance = FLAT_DISTANCE_MU_MM + bias + rng.normal(0.0, 0.05, size=N_ZONES)
    signal = FLAT_SIGNAL_MU + rng.normal(0.0, 0.1, size=N_ZONES)
    return np.concatenate([distance, signal]).astype(np.float32)


def _baseline_sigma_vec():
    return np.concatenate([
        np.full(N_ZONES, DISTANCE_SIGMA_MM, dtype=np.float32),
        np.full(N_ZONES, SIGNAL_SIGMA, dtype=np.float32),
    ])


def _tof_time_axis():
    n = round(TRIAL_S * TOF_HZ)
    return (np.arange(n) / TOF_HZ * 1e6).astype(np.int64)


def _mel_time_axis():
    n = round(TRIAL_S * MEL_HZ)
    return (np.arange(n) / MEL_HZ * 1e6).astype(np.int64)


def _mic_time_axis():
    n = round(TRIAL_S * MIC_HZ)
    return (np.arange(n) / MIC_HZ * 1e6).astype(np.int64)


def synth_tof(t_us, spec_key, wear_bias_mm, rng, sensor):
    """回傳 `(distance, signal)`，各 (T, 16)。`sensor` 是 "A" 或 "B"，
    決定用 `a_zones`/`tof_peak_a` 還是 `b_zones`/`tof_peak_b`。"""
    zones_key = "a_zones" if sensor == "A" else "b_zones"
    peak_key = "tof_peak_a" if sensor == "A" else "tof_peak_b"
    spec = WORD_SPEC.get(spec_key, REJECT_SPEC)
    zones, peak = spec[zones_key], spec[peak_key]

    t_s = t_us / 1e6
    env = np.array([envelope(t, PRE_ROLL_S) for t in t_s])

    distance = (FLAT_DISTANCE_MU_MM + wear_bias_mm[np.newaxis, :]
                + rng.normal(0.0, DISTANCE_SIGMA_MM, size=(len(t_us), N_ZONES)))
    signal = (FLAT_SIGNAL_MU + rng.normal(0.0, SIGNAL_SIGMA, size=(len(t_us), N_ZONES)))

    if peak > 0.0 and zones:
        # 嘴唇/下巴靠近感測器 -> 距離讀值變小；同時 signal_per_spad 上升。
        distance[:, zones] -= peak * env[:, np.newaxis]
        signal[:, zones] += (peak * 1.3) * env[:, np.newaxis]

    return distance.astype(np.float32), signal.astype(np.float32)


def synth_mel(t_us, spec_key, rng, *, silent=False):
    spec = WORD_SPEC.get(spec_key, REJECT_SPEC)
    bands, peak = spec["mel_bands"], spec["mel_peak"]
    shape = spec.get("audio_shape", (ATTACK_S, SUSTAIN_S, DECAY_S))

    band_idx = np.arange(MEL_BANDS)
    base = MEL_BASE_LEVEL + MEL_BASE_SLOPE * band_idx
    mel = (base[np.newaxis, :]
           + rng.normal(0.0, MEL_NOISE_SIGMA, size=(len(t_us), MEL_BANDS)))

    if not silent and peak > 0.0 and bands:
        t_s = t_us / 1e6
        env = np.array([envelope(t, PRE_ROLL_S + LIP_LEAD_S, *shape) for t in t_s])
        mel[:, bands] += peak * env[:, np.newaxis]

    return mel.astype(np.float32)


def synth_mic(t_us, spec_key, rng, *, silent=False):
    spec = WORD_SPEC.get(spec_key, REJECT_SPEC)
    has_voice = (not silent) and spec["mel_peak"] > 0.0
    shape = spec.get("audio_shape", (ATTACK_S, SUSTAIN_S, DECAY_S))

    t_s = t_us / 1e6
    rms = (NOISE_FLOOR_MU
           + rng.normal(0.0, NOISE_FLOOR_SIGMA * 0.3, size=len(t_us)))
    if has_voice:
        env = np.array([envelope(t, PRE_ROLL_S + LIP_LEAD_S, *shape) for t in t_s])
        rms = rms + 30.0 * env
    rms = np.clip(rms, 0.1, None)
    peak = (rms * MIC_PEAK_SCALE + rng.normal(0.0, 5.0, size=len(t_us))).clip(0, 32767)
    return rms.astype(np.float32), peak.astype(np.int16)


def _onset_attrs(spec_key, *, silent):
    """回傳 VAD 時間戳 attrs（`None` 的整個不寫，跟 schema 對「沒偵測到」
    的規定一致，見 `session_writer.write_trial()` 的說明）。"""
    spec = WORD_SPEC.get(spec_key, REJECT_SPEC)
    has_lip = bool(spec["a_zones"] or spec["b_zones"])
    has_voice = (not silent) and spec["mel_peak"] > 0.0

    audio_duration_s = sum(spec.get("audio_shape", (ATTACK_S, SUSTAIN_S, DECAY_S)))
    lip_onset_us = int(PRE_ROLL_S * 1e6) if has_lip else None
    voice_onset_us = int((PRE_ROLL_S + LIP_LEAD_S) * 1e6) if has_voice else None
    attrs = {}
    if has_lip:
        attrs["lip_onset_us"] = lip_onset_us
        attrs["lip_onset_us_A"] = lip_onset_us
        attrs["lip_onset_us_B"] = lip_onset_us
    if has_voice:
        attrs["voice_onset_us"] = voice_onset_us
        attrs["vad_start_us"] = voice_onset_us
        attrs["vad_end_us"] = int((PRE_ROLL_S + LIP_LEAD_S + audio_duration_s) * 1e6)
    if has_lip and has_voice:
        attrs["comparable"] = True
    attrs["vad_confidence"] = 0.85 if (has_lip or has_voice) else 0.3
    return attrs


def _base_meta(rng, *, wear_id, sensors_enabled, wear_bias_mm=None):
    baseline_mu_a = _flat_baseline_mu(rng, wear_bias_mm)
    baseline_mu_b = _flat_baseline_mu(rng, wear_bias_mm)
    sigma_vec = _baseline_sigma_vec()
    return {
        "schema_version": SCHEMA_VERSION,
        "subject": "reference_synth",
        "session_date": "2026-08-26",
        "wear_id": wear_id,
        "mode": "reference",
        "distance_mm": 150.0,
        "angle_deg": 0.0,
        "ambient": "lab",
        "notes": "D22 附帶任務：make_reference_session.py 產生的合成參考資料",
        "fw_sha": "0000000",
        "proto_version": 2,
        "tof_dim": 8,
        "clock_slope": 1.0,
        "clock_offset": 0.0,
        "clock_residual_p95": 0.0,
        "clock_drift_us": 0.0,
        "clock_drift_ppm": 0.0,
        "clock_sync_span_us": 0,
        "clock_sync_confirmed": True,
        "session_start_device_us": 0,
        "session_start_host_us": 0,
        "session_start_rtt_min_us": 0,
        "baseline_mu_A": baseline_mu_a,
        "baseline_sigma_A": sigma_vec,
        "baseline_mu_B": baseline_mu_b,
        "baseline_sigma_B": sigma_vec,
        "noise_floor_mu": NOISE_FLOOR_MU,
        "noise_floor_sigma": NOISE_FLOOR_SIGMA,
        "energy_mu": ENERGY_MU,
        "energy_sigma": ENERGY_SIGMA,
        "sensors_enabled": sensors_enabled,
        "sensors_seen": sensors_enabled,
        "source": "mock",
    }


def build_main_session(path, wear_id, seed):
    """一次「戴上」的完整詞彙集：每詞 `TRIALS_PER_WORD_PER_WEAR` 筆
    ＋ 1 筆 `silent` 模式（`SILENT_LABEL`）。`is_synthetic` 由呼叫端
    （`analysis/run_all.py` 預設不加 `--real`）標示，這裡的 `source` 是
    另一件事（§4.2 的擷取來源，不是「這批資料是不是假的」）。
    """
    rng = np.random.default_rng(seed)
    words, reject = load_words()
    labels = words + [reject]
    wear_bias = WEAR_BIAS_MM[wear_id]

    tof_t_us = _tof_time_axis()
    mel_t_us = _mel_time_axis()
    mic_t_us = _mic_time_axis()

    meta = _base_meta(rng, wear_id=wear_id, sensors_enabled="AB", wear_bias_mm=wear_bias)
    with SessionWriter(path, meta) as w:
        idx = 0
        for label in labels:
            for _ in range(TRIALS_PER_WORD_PER_WEAR):
                _write_main_trial(w, idx, label, wear_id, wear_bias,
                                   tof_t_us, mel_t_us, mic_t_us, rng, silent=False)
                idx += 1
        # 額外的 silent 模式 trial：`voice_onset_us` 必然缺席（`SPEAKING_MODES`
        # 明訂 silent 完全不用音訊 VAD）——唇動幾何照舊，只是不出聲。
        _write_main_trial(w, idx, SILENT_LABEL, wear_id, wear_bias,
                           tof_t_us, mel_t_us, mic_t_us, rng, silent=True)
        idx += 1
    return idx


def _write_main_trial(w, idx, label, wear_id, wear_bias, tof_t_us, mel_t_us, mic_t_us,
                       rng, *, silent):
    dist_a, sig_a = synth_tof(tof_t_us, label, wear_bias, rng, "A")
    dist_b, sig_b = synth_tof(tof_t_us, label, wear_bias, rng, "B")
    tof_a = np.concatenate([dist_a, sig_a], axis=1)
    tof_b = np.concatenate([dist_b, sig_b], axis=1)
    tof_valid_a = np.ones((len(tof_t_us), N_ZONES), dtype=bool)
    tof_valid_b = np.ones((len(tof_t_us), N_ZONES), dtype=bool)

    mel = synth_mel(mel_t_us, label, rng, silent=silent)
    mic_rms, mic_peak = synth_mic(mic_t_us, label, rng, silent=silent)

    onset_attrs = _onset_attrs(label, silent=silent)
    speaking_mode = "silent" if silent else "normal"

    w.write_trial(
        idx, label=label,
        tof_A=tof_a, tof_B=tof_b, tof_t_us=tof_t_us,
        tof_valid_A=tof_valid_a, tof_valid_B=tof_valid_b,
        mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
        mel=mel, mel_t_us=mel_t_us,
        wear_id=wear_id, mode="reference",
        valid_zone_ratio=1.0, drop_count=0, quality="ok",
        speaking_mode=speaking_mode,
        sensors_seen="AB",
        **onset_attrs,
    )


def build_crosstalk_sessions(out_dir, seed):
    """三個「靜止」錄製：兩顆都開（dual）／只開 A（solo A）／只開 B
    （solo B），同一個 wear_id、同一份底噪，只在指定 zone 上加受控的
    solo-vs-dual 差——見模組文件的 C0 設計段落。"""
    rng = np.random.default_rng(seed)
    tof_t_us = _tof_time_axis()
    mel_t_us = _mel_time_axis()
    mic_t_us = _mic_time_axis()
    wear_bias = np.zeros(N_ZONES, dtype=np.float64)

    def _quiet_trial(w, idx, *, delta_mm, valid_a, valid_b, sensors_seen):
        dist_a = (FLAT_DISTANCE_MU_MM + wear_bias
                  + rng.normal(0.0, DISTANCE_SIGMA_MM, size=(len(tof_t_us), N_ZONES)))
        dist_b = dist_a.copy()
        for z, d in delta_mm.get("A", {}).items():
            dist_a[:, z] += d
        for z, d in delta_mm.get("B", {}).items():
            dist_b[:, z] += d
        sig_a = FLAT_SIGNAL_MU + rng.normal(0.0, SIGNAL_SIGMA, size=(len(tof_t_us), N_ZONES))
        sig_b = FLAT_SIGNAL_MU + rng.normal(0.0, SIGNAL_SIGMA, size=(len(tof_t_us), N_ZONES))
        tof_a = np.concatenate([dist_a, sig_a], axis=1).astype(np.float32)
        tof_b = np.concatenate([dist_b, sig_b], axis=1).astype(np.float32)
        tof_valid_a = np.full((len(tof_t_us), N_ZONES), valid_a, dtype=bool)
        tof_valid_b = np.full((len(tof_t_us), N_ZONES), valid_b, dtype=bool)
        mic_rms, mic_peak = synth_mic(mic_t_us, "_reject", rng)
        # `_reject` 的純底噪 mel——**一定要給**，不能省略：`exp_d12_wear_cv
        # .run_wear_cv()` 把所有 session 的 trial 一起攤平算 CV，省略 mel
        # 會讓 `mel_total_energy` 在這個 wear_id 上讀到 0，跟其他 wear 的
        # 真實底噪能量差一大截，被 CV 誤讀成「跨次戴變異大」——這是這支
        # 工具第一版實測抓到的真實 bug（`cv_between` 被拉到 57.7%），不是
        # 假設性的。
        mel = synth_mel(mel_t_us, "_reject", rng)

        w.write_trial(
            idx, label="_reject",
            tof_A=tof_a, tof_B=tof_b, tof_t_us=tof_t_us,
            tof_valid_A=tof_valid_a, tof_valid_B=tof_valid_b,
            mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
            mel=mel, mel_t_us=mel_t_us,
            wear_id=CROSSTALK_WEAR_ID, mode="crosstalk",
            valid_zone_ratio=1.0, drop_count=0, quality="ok",
            sensors_seen=sensors_seen,
        )

    # dual：AB 都開，沒有刻意的偏移——它是 solo 兩邊共同的對照基準。
    dual_meta = _base_meta(rng, wear_id=CROSSTALK_WEAR_ID, sensors_enabled="AB")
    with SessionWriter(out_dir / "crosstalk_dual_wear99.h5", dual_meta) as w:
        for i in range(N_CROSSTALK_TRIALS):
            _quiet_trial(w, i, delta_mm={}, valid_a=True, valid_b=True, sensors_seen="AB")

    # solo A：只有 A 的方向有意義（B 標成 invalid，不會被 `run_crosstalk()`
    # 讀到）。刻意加的偏移全部 < 2mm 門檻 -> PASS。
    solo_a_meta = _base_meta(rng, wear_id=CROSSTALK_WEAR_ID, sensors_enabled="A")
    with SessionWriter(out_dir / "crosstalk_soloA_wear99.h5", solo_a_meta) as w:
        for i in range(N_CROSSTALK_TRIALS):
            _quiet_trial(w, i, delta_mm={"A": CROSSTALK_DELTA_A_MM},
                         valid_a=True, valid_b=False, sensors_seen="A")

    # solo B：zone 9 故意 >= 2mm -> FAIL（驗收條件要的「不要全綠」）。
    solo_b_meta = _base_meta(rng, wear_id=CROSSTALK_WEAR_ID, sensors_enabled="B")
    with SessionWriter(out_dir / "crosstalk_soloB_wear99.h5", solo_b_meta) as w:
        for i in range(N_CROSSTALK_TRIALS):
            _quiet_trial(w, i, delta_mm={"B": CROSSTALK_DELTA_B_MM},
                         valid_a=False, valid_b=True, sensors_seen="B")


def build(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for i, wear_id in enumerate(WEAR_IDS):
        path = out_dir / f"main_wear{wear_id}.h5"
        build_main_session(path, wear_id, seed=RNG_SEED + wear_id)
        written.append(path)
    build_crosstalk_sessions(out_dir, seed=RNG_SEED + 100)
    written += [
        out_dir / "crosstalk_dual_wear99.h5",
        out_dir / "crosstalk_soloA_wear99.h5",
        out_dir / "crosstalk_soloB_wear99.h5",
    ]
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", default="reference_session", metavar="DIR",
                        help="輸出目錄（預設 ./reference_session/；不要指到 sessions/）")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    written = build(out_dir)
    print(f"寫入 {len(written)} 個檔案到 {out_dir.resolve()}：")
    for p in written:
        print(f"  {p.name}")
    print()
    print("驗證指令（main_wear0.h5 必須排第一個，見模組文件）：")
    print(f"  python3 -m analysis.run_all \\")
    for p in written:
        print(f"    --session {p} \\")
    print(f"    --out {out_dir}/report --ablation-permutations 1000")


if __name__ == "__main__":
    main()
