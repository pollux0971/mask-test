"""B15 — 音訊 VAD 端點偵測（`$M` 的 RMS 串流上找詞的起訖）。

DTW 比對的應該是語音本體。前後靜音長度不一時，「靜音對齊靜音」也會被算
進距離，把真正的差異稀釋掉——所以要先把兩端切乾淨。

## 為什麼是遲滯（hysteresis）而不是單一閾值

單一閾值在音量貼著閾值上下抖動時，會把一個詞切成好幾段。所以進入與離開
用**兩個不同的閾值**，而且離開還要**持續一段時間**才算數：

    進入：rms > mu + enter_sigma * sigma           （normal 3σ）
    離開：rms < mu + exit_sigma  * sigma 且持續 200 ms   （normal 1.5σ）

## 閾值一定要來自當次 session 的底噪

`mu` / `sigma` 是 `B10` 的 `compute_noise_floor()` 算出來、寫進 `/meta` 的
`noise_floor_mu` / `noise_floor_sigma`。**這裡不自己定義第二套靜音基準。**
不同環境（實驗室 vs 家裡）、不同戴法（骨傳導貼合鬆緊）的底噪差很多，寫死
的閾值換個場地就失效——而且會靜默地失效。

## 兩個容易寫錯、而且錯了不會報錯的地方

**1. 掛延遲（hangover）要用時間算，不能用幀數算。**
`$M` 會掉幀（§1.1／`$H` 的 `drop_M`）。用「連續 N 幀低於閾值」判斷離開，
在掉幀時會**提早**結束——掉掉的那幾幀被當成「持續安靜」。這裡一律用
`t_us` 的差值，掉幀只會讓判斷變保守，不會變錯。

**2. 起點要回退到上升沿的起腳，不是回退到越過進入閾值的那一幀。**
子音起始（爆音、擦音）的能量爬升很快但峰值不高，只取「越過 3σ 的那一
幀」會把起音切掉。這裡把起點退到**越過離開閾值**的那一幀——也就是同一
個上升沿的腳。這正是 §1.3.1 之所以規定「`$M` 的 RMS 必須涵蓋完整 512
窗、不可只算新的 256」的同一個顧慮：短促爆音若對 VAD 隱形，這裡再怎麼
回退也救不回來。

## 說話模式（`speaking_mode`）

`normal` 3σ / `whisper` 2σ / `silent` 不用音訊 VAD。

⚠️ **這個參數不是 `CONTRACTS.md` `/meta` 裡的 `mode` 欄位。** 那個 `mode`
是 session／面板模式（`quiz` 之類），是另一條軸。契約目前沒有定義「說話
模式」這個概念，所以這裡用 `speaking_mode` 這個不同的名字，避免兩者被誤
接在一起——見完成回報，等調度員裁決欄位名。

**不包含**：ToF VAD（`B16`）、Auto-VAD 觸發（`B13`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from host.vad.hysteresis import (
    DEFAULT_HANGOVER_MS,
    DEFAULT_MAX_ONSET_BACKOFF_MS,
    DEFAULT_MIN_SEGMENT_MS,
    DEFAULT_SMOOTH_FRAMES,
    SIGMA_FLOOR,
    Segment,
    detect_segments,
    thresholds,
)

# 舊名保留：B15 的下游已經在用 `VadSegment` 這個名字。
VadSegment = Segment

# 說話模式 → (進入 σ 倍數, 離開 σ 倍數)。
#
# `normal` 的 3σ / 1.5σ 與 `whisper` 的進入 2σ 都是 story 明訂的。
# **`whisper` 的離開維持 1.5σ，與 `normal` 相同**——story 沒有規定它，
# 而這是量出來的，不是猜的。
#
# 兩個閾值的職責不一樣：
#   * **進入**決定靈敏度——「多小聲的詞我們還要算數」。那是關於**說話者**
#     的，所以 whisper 要放寬。
#   * **離開**決定「安靜要多安靜才算結束」。那是關於**底噪**的，跟人講話
#     多大聲無關，所以**不該**跟著說話模式縮放。
#
# 我一開始按「離開 = 進入的一半」的比例訂成 1.0σ，實測邊界誤差飆到
# 240 ms：底噪有 15.9% 的幀會高於 1.0σ，200 ms 掛延遲被隨機尖峰反覆重置，
# 段落一路延長。同一批合成資料掃過去（40 筆，峰值 3.5–6σ，平滑 3 幀）：
#
#     whisper exit=1.00σ → 最大誤差 240.0 ms，平均 27.2 ms
#     whisper exit=1.25σ → 最大誤差 240.0 ms，平均 23.6 ms
#     whisper exit=1.50σ → 最大誤差  80.0 ms，平均 17.6 ms   ← 選這個
SPEAKING_MODES = {
    "normal": (3.0, 1.5),
    "whisper": (2.0, 1.5),
    "silent": None,          # 不用音訊 VAD
}
DEFAULT_SPEAKING_MODE = "normal"

# 離開閾值要持續這麼久才算真的結束（story 明訂）。
HANGOVER_MS = DEFAULT_HANGOVER_MS

# 比這個短的段落當成雜訊尖峰丟掉。$M 在 31.25 Hz 下幀距 32 ms，50 ms 約
# 等於「至少要有兩幀」——單一幀的尖刺不構成一個詞。
MIN_SEGMENT_MS = DEFAULT_MIN_SEGMENT_MS

# sigma 的下限守衛，同 §3.2 對 ToF z-score 的做法。麥克風壞掉或整段完全
# 沒有變異時 sigma 會是 0，除下去會變成 inf/NaN，讓整個判斷靜默壞掉。
SIGMA_FLOOR = 1e-3

# 判斷閾值之前先做幾幀的置中移動平均。**這不是美化，是讓 story 指定的
# 閾值真的能用。** 純底噪高於離開閾值的機率是：
#
#     normal  (1.5σ)  6.7% 每幀 → 200 ms 掛延遲(6.25 幀)全部低於只有 64.9%
#     whisper (1.0σ) 15.9% 每幀 → 全部低於只有 34.0%
#
# 也就是說掛延遲有 35%／66% 的機率被底噪的隨機尖峰重置，段落就一路延長
# 下去——實測邊界誤差衝到 1168 ms。平滑 3 幀讓雜訊的 sigma 降為 1/√3，
# 等效門檻變成 2.60σ／1.73σ：
#
#     normal  單幀 0.47% → 掛延遲全部低於 97.1%
#     whisper 單幀 4.16% → 掛延遲全部低於 76.7%
#
# 用**置中**視窗（不是因果視窗）才不會引入群延遲把邊界整個往後推。
# 3 幀 @31.25 Hz 是 96 ms 視窗、±32 ms 半寬，還在 100 ms 邊界預算內。
SMOOTH_FRAMES = DEFAULT_SMOOTH_FRAMES

# 起點回退的上限。回退是為了接住上升沿的腳，但在低訊噪比時底噪本身就常常
# 高於離開閾值，不設上限會一路退穿整段靜音（實測退了將近一秒）。語音的
# 起音爬升在 30–60 ms 量級，96 ms 綽綽有餘。
MAX_ONSET_BACKOFF_MS = DEFAULT_MAX_ONSET_BACKOFF_MS



@dataclass(frozen=True)
class VadResult:
    """一次偵測的完整結果。

    `applicable=False` 代表「這次沒有跑音訊 VAD」，而不是「跑了但沒找到」
    ——兩者對下游的意義完全不同（`silent` 模式本來就不該用音訊，而「跑了
    沒找到」是一次失敗的錄音）。`reason` 說明是哪一種。
    """

    applicable: bool
    segments: tuple[VadSegment, ...] = ()
    reason: Optional[str] = None
    enter_threshold: Optional[float] = None
    exit_threshold: Optional[float] = None
    speaking_mode: str = DEFAULT_SPEAKING_MODE
    noise_floor_mu: Optional[float] = None
    noise_floor_sigma: Optional[float] = None
    n_frames: int = 0
    discarded_short_segments: int = 0

    @property
    def primary(self) -> Optional[VadSegment]:
        """最長的那一段。一次 trial 錄一個詞，偶爾會夾雜咳嗽、椅子聲之類
        的短雜訊，取最長的比取第一個穩健。"""
        if not self.segments:
            return None
        return max(self.segments, key=lambda s: s.duration_us)

    @property
    def detected(self) -> bool:
        return self.primary is not None

    @property
    def confidence(self) -> float:
        """0.0–1.0。**這是一個定義出來的分數，不是機率。**

            confidence = clamp01((peak_z - enter_sigma) / enter_sigma)

        剛好擦過進入閾值 → 0.0；達到進入閾值兩倍（normal 是 6σ）→ 1.0。
        單調、跟著模式的閾值一起縮放、而且一句話講得完。真要自己定規則的
        下游可以直接讀 `primary.peak_z` / `mean_z` / `n_frames_above_enter`
        ——原始材料都在，不必反推這個分數。
        """
        seg = self.primary
        if seg is None:
            return 0.0
        enter_sigma = SPEAKING_MODES.get(self.speaking_mode)
        enter_sigma = enter_sigma[0] if enter_sigma else 1.0
        return float(min(1.0, max(0.0, (seg.peak_z - enter_sigma) / enter_sigma)))

    def to_dict(self) -> dict:
        seg = self.primary
        return {
            "applicable": self.applicable,
            "detected": self.detected,
            "reason": self.reason,
            "speaking_mode": self.speaking_mode,
            # `/trial_NNN` 的 attrs（CONTRACTS.md §2）。沒偵測到就是 None，
            # **不填 0**——0 是一個合法的 t_us，會被當成「詞從開機那一刻
            # 開始」。
            "vad_start_us": seg.start_us if seg else None,
            "vad_end_us": seg.end_us if seg else None,
            "vad_confidence": self.confidence if seg else None,
            "enter_threshold": self.enter_threshold,
            "exit_threshold": self.exit_threshold,
            "noise_floor_mu": self.noise_floor_mu,
            "noise_floor_sigma": self.noise_floor_sigma,
            "n_frames": self.n_frames,
            "n_segments": len(self.segments),
            "discarded_short_segments": self.discarded_short_segments,
            "segments": [s.to_dict() for s in self.segments],
        }


def thresholds_for(
    noise_floor_mu: float, noise_floor_sigma: float,
    speaking_mode: str = DEFAULT_SPEAKING_MODE,
) -> "tuple[float, float]":
    """回傳 `(enter, exit)` 兩個絕對閾值（與 `rms` 同單位）。

    驗收條件「閾值從 session 底噪自動計算，換環境無需手調」的具體實作就
    是這一行：閾值 = 底噪 μ + k·σ，兩個統計量都來自 `B10`。
    """
    factors = SPEAKING_MODES.get(speaking_mode)
    if factors is None:
        raise ValueError(f"{speaking_mode!r} 沒有音訊閾值（silent 模式不用音訊 VAD）")
    return thresholds(noise_floor_mu, noise_floor_sigma, *factors)


def detect_voice_activity(
    rms: Sequence[float],
    t_us: Sequence[int],
    noise_floor_mu: Optional[float],
    noise_floor_sigma: Optional[float],
    *,
    speaking_mode: str = DEFAULT_SPEAKING_MODE,
    hangover_ms: float = HANGOVER_MS,
    min_segment_ms: float = MIN_SEGMENT_MS,
    smooth_frames: int = SMOOTH_FRAMES,
    max_onset_backoff_ms: float = MAX_ONSET_BACKOFF_MS,
) -> VadResult:
    """在一串 `$M` 的 `(rms, t_us)` 上做雙閾值遲滯端點偵測。

    **不拋例外，除非參數本身就不合法**（長度不符、模式不認得）——資料不
    足、底噪缺漏都回一個 `applicable=False` 的結果並說明原因，讓呼叫端能
    把它當成一次品質不良的錄音處理，而不是整條 pipeline 崩掉。
    """
    if speaking_mode not in SPEAKING_MODES:
        raise ValueError(
            f"未知的 speaking_mode {speaking_mode!r}，可用：{sorted(SPEAKING_MODES)}"
        )
    if len(rms) != len(t_us):
        raise ValueError(f"rms 與 t_us 長度不符：{len(rms)} vs {len(t_us)}")

    if SPEAKING_MODES[speaking_mode] is None:
        return VadResult(
            applicable=False, speaking_mode=speaking_mode, n_frames=len(rms),
            reason="silent 模式不使用音訊 VAD（沒有出聲，唇動由 B16 的 ToF VAD 負責）",
        )
    if noise_floor_mu is None or noise_floor_sigma is None:
        return VadResult(
            applicable=False, speaking_mode=speaking_mode, n_frames=len(rms),
            reason="缺少 session 底噪統計（/meta 的 noise_floor_mu/sigma），"
                   "無法自動決定閾值；請先跑 B10 的 baseline 錄製",
        )
    if len(rms) < 2:
        return VadResult(
            applicable=False, speaking_mode=speaking_mode, n_frames=len(rms),
            noise_floor_mu=float(noise_floor_mu),
            noise_floor_sigma=float(noise_floor_sigma),
            reason=f"$M 幀數不足（{len(rms)}），至少要 2 幀才談得上端點",
        )

    values = np.asarray(rms, dtype=np.float64)
    times = np.asarray(t_us, dtype=np.int64)
    if np.any(np.diff(times) < 0):
        # 時間必須單調。亂序多半代表呼叫端把兩條串流混在一起了，這裡直接
        # 排好而不是拒絕——但保守起見不去猜 rms 該怎麼配對，用 argsort 一起帶。
        order = np.argsort(times, kind="stable")
        values, times = values[order], times[order]

    mu = float(noise_floor_mu)
    enter_sigma, exit_sigma = SPEAKING_MODES[speaking_mode]
    segments, discarded, enter_thr, exit_thr = detect_segments(
        values, times, mu, noise_floor_sigma,
        enter_sigma=enter_sigma, exit_sigma=exit_sigma,
        hangover_ms=hangover_ms, min_segment_ms=min_segment_ms,
        smooth_frames=smooth_frames, max_onset_backoff_ms=max_onset_backoff_ms,
    )

    return VadResult(
        applicable=True,
        segments=tuple(segments),
        speaking_mode=speaking_mode,
        enter_threshold=enter_thr,
        exit_threshold=exit_thr,
        noise_floor_mu=mu,
        noise_floor_sigma=float(noise_floor_sigma),
        n_frames=len(values),
        discarded_short_segments=discarded,
        reason=None if segments else "沒有任何幀越過進入閾值",
    )



def detect_from_events(
    events: Sequence[dict],
    noise_floor_mu: Optional[float],
    noise_floor_sigma: Optional[float],
    **kwargs,
) -> VadResult:
    """吃 `host/capture/protocol.py` 解出來的事件串，挑出 `$M` 來做偵測。

    協定 v1 的 `$M` 沒有 `t_us`（`has_timestamp=False`），端點偵測沒有時間
    軸就沒有意義，所以整批 v1 事件會回 `applicable=False` 而不是硬算——
    silently 用幀索引冒充時間，會產出看起來合理但完全錯誤的邊界。
    """
    mic = [e for e in events if e.get("type") == "mic"]
    if not mic:
        return VadResult(
            applicable=False, reason="事件串裡沒有任何 $M 幀",
            speaking_mode=kwargs.get("speaking_mode", DEFAULT_SPEAKING_MODE),
        )
    if any(e.get("t_us") is None for e in mic):
        return VadResult(
            applicable=False, n_frames=len(mic),
            speaking_mode=kwargs.get("speaking_mode", DEFAULT_SPEAKING_MODE),
            reason="協定 v1 的 $M 沒有 t_us，無法做端點偵測（請升級韌體到 proto=2）",
        )
    return detect_voice_activity(
        [e["rms"] for e in mic], [e["t_us"] for e in mic],
        noise_floor_mu, noise_floor_sigma, **kwargs,
    )
