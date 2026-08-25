"""B06 — 多模態時間對齊器（CONTRACTS.md 第 1 章；輸入是 `host/capture/protocol.py`
的解析結果）。

`$T`(A) / `$T`(B) / `$M` / `$F` 是四條各自獨立的串流：各自的 `seq` 只代表
「這條線送出的第幾行」，**彼此之間沒有任何關係**——`A14` 之後 `$F` 是
62.5 Hz、`$M` 是 31.25 Hz，物理上不可能共用同一個 seq。對齊**只能靠
`t_us`**，這正是 T01 凍結 `t_us` 取樣點（ToF 在 `get_ranging_data()`
之前、Mic 是音框第一個 sample）的原始目的：讓時間戳可以直接比較。

**設計決定：以 ToF 為時基。** ToF 最慢也最不可內插——距離跳變是真實的
物理事件，內插會製造不存在的中間值。`frames()` 預設 `rate_hz=30`（ToF
常見幀率）。Mel／Mic 比較密，理論上可以線性內插，但**預設仍是最近鄰**——
`interp='linear'` 是選項，不是預設行為。

**缺資料一律用 mask 標記，不要填 0。** 這與 T02 `tof_valid_*` 布林陣列、
D01 `tof_features` 的「無效 zone 填 0 只在 z-score 之後才安全」是同一個
原則：`AlignedFrame` 的 `*_present` 欄位就是這個 mask，`None`/`False`
代表「這個時間點附近沒有可信的樣本」，呼叫端自己決定怎麼處理，不是我們
幫忙假裝有資料。
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import List, Optional, Tuple

DEFAULT_BUFFER_SECONDS = 10.0
DEFAULT_RATE_HZ = 30.0
# 兩個「可信樣本」之間容許的最大間隔。超過這個間隔就不再內插/沿用——
# 錄音 dump 期間 ToF 掉幀動輒是秒級的空窗（§1.4：dump 吃 92% 頻寬），
# 100ms 的預設值遠小於那種空窗，能把它跟一般的取樣抖動分開。
DEFAULT_MAX_GAP_US = 100_000


@dataclass(frozen=True)
class TofSample:
    """比照 T02 schema：`values` 是 32 維（[0:16] 距離 mm、[16:32]
    signal/100），`valid` 是 16 維、對應同一個 zone 的兩個通道。
    無效 zone 在 `values` 對應位置一律是 `None`，不是 0 或 -1。"""
    values: List[Optional[float]]
    valid: List[bool]


@dataclass(frozen=True)
class MicSample:
    rms: float
    peak: float


@dataclass(frozen=True)
class AlignedFrame:
    t_us: int
    tof_A: Optional[TofSample]
    tof_A_present: bool
    tof_B: Optional[TofSample]
    tof_B_present: bool
    mic_rms: Optional[float]
    mic_peak: Optional[float]
    mic_present: bool
    mel: Optional[List[float]]
    mel_present: bool


class _RingBuffer:
    """依 `t_us`排序、只保留最近 `buffer_seconds` 的樣本；容忍亂序到達
    （UART 不保證嚴格遞增），但幾乎所有真實資料本來就是遞增的，所以
    「加在尾端」用 O(1) append，只有真的亂序時才退回 O(n) 的 `insort`。
    """

    def __init__(self, buffer_seconds: float = DEFAULT_BUFFER_SECONDS):
        self._window_us = buffer_seconds * 1e6
        self._t: List[int] = []
        self._payload: List[object] = []

    def push(self, t_us: int, payload) -> None:
        if not self._t or t_us >= self._t[-1]:
            self._t.append(t_us)
            self._payload.append(payload)
        else:
            idx = bisect_left(self._t, t_us)
            self._t.insert(idx, t_us)
            self._payload.insert(idx, payload)
        self._evict(t_us)

    def _evict(self, latest_t_us: int) -> None:
        cutoff = latest_t_us - self._window_us
        n_drop = bisect_left(self._t, cutoff)
        if n_drop:
            del self._t[:n_drop]
            del self._payload[:n_drop]

    def __len__(self) -> int:
        return len(self._t)

    def nearest(self, t_us: int, max_gap_us: float):
        if not self._t:
            return None
        idx = bisect_left(self._t, t_us)
        candidates = [i for i in (idx - 1, idx) if 0 <= i < len(self._t)]
        best = min(candidates, key=lambda i: abs(self._t[i] - t_us))
        if abs(self._t[best] - t_us) > max_gap_us:
            return None
        return self._payload[best]

    def bracket(self, t_us: int) -> Tuple[Optional[Tuple], Optional[Tuple]]:
        """回傳 `(left, right)`，每邊是 `(payload, t_us)` 或 `None`。
        `left` 嚴格早於 `t_us`；`right` 是第一個 `>= t_us` 的樣本
        （剛好相等時，`right` 就是那個精確命中）。"""
        idx = bisect_left(self._t, t_us)
        left = (self._payload[idx - 1], self._t[idx - 1]) if idx > 0 else None
        right = (self._payload[idx], self._t[idx]) if idx < len(self._t) else None
        return left, right


def _resolve(buf: _RingBuffer, t_us: int, interp: str, max_gap_us: float, merge_fn, single_fn):
    """共用的「這個時間點的值是什麼」解析邏輯，modality 特定的部分只透過
    `merge_fn(left_payload, right_payload, frac) -> value`（線性內插用）與
    `single_fn(payload) -> value`（原封不動沿用一個樣本）注入。"""
    if interp == "nearest":
        payload = buf.nearest(t_us, max_gap_us)
        return (single_fn(payload), True) if payload is not None else (None, False)

    left, right = buf.bracket(t_us)
    if right is not None and right[1] == t_us:
        return single_fn(right[0]), True  # 精確命中，不需要內插

    left_ok = left is not None and (t_us - left[1]) <= max_gap_us
    right_ok = right is not None and (right[1] - t_us) <= max_gap_us

    if left_ok and right_ok:
        (lp, lt), (rp, rt) = left, right
        frac = (t_us - lt) / (rt - lt)
        return merge_fn(lp, rp, frac), True
    if left_ok:
        return single_fn(left[0]), True
    if right_ok:
        return single_fn(right[0]), True
    return None, False


def _merge_tof(lp: TofSample, rp: TofSample, frac: float) -> TofSample:
    n_zones = len(lp.valid)
    valid = [lp.valid[i] and rp.valid[i] for i in range(n_zones)]
    values: List[Optional[float]] = [None] * (2 * n_zones)
    for i in range(n_zones):
        if valid[i]:
            values[i] = lp.values[i] + frac * (rp.values[i] - lp.values[i])
            values[i + n_zones] = lp.values[i + n_zones] + frac * (rp.values[i + n_zones] - lp.values[i + n_zones])
    return TofSample(values=values, valid=valid)


def _merge_mic(lp: MicSample, rp: MicSample, frac: float) -> MicSample:
    return MicSample(rms=lp.rms + frac * (rp.rms - lp.rms), peak=lp.peak + frac * (rp.peak - lp.peak))


def _merge_mel(lp: List[float], rp: List[float], frac: float) -> List[float]:
    return [a + frac * (b - a) for a, b in zip(lp, rp)]


class Aligner:
    """收集四條串流的樣本，`frames()` 依指定頻率吐出對齊好的幀。

    純函式風格：`push_*` 只是把資料放進環形緩衝，真正的對齊計算全部在
    `frames()` 裡，且不會修改任何狀態，可以對同一份緩衝反覆呼叫。
    """

    def __init__(self, buffer_seconds: float = DEFAULT_BUFFER_SECONDS):
        self._tof = {"A": _RingBuffer(buffer_seconds), "B": _RingBuffer(buffer_seconds)}
        self._mic = _RingBuffer(buffer_seconds)
        self._mel = _RingBuffer(buffer_seconds)

    def push_tof(self, sensor: str, t_us: int, distance, signal, valid) -> None:
        if sensor not in ("A", "B"):
            raise ValueError(f"sensor 必須是 'A' 或 'B'，收到 {sensor!r}")
        n = len(valid)
        if len(distance) != n or len(signal) != n:
            raise ValueError(f"distance/signal/valid 長度必須一致，收到 {len(distance)}/{len(signal)}/{n}")
        values = list(distance) + list(signal)
        self._tof[sensor].push(int(t_us), TofSample(values=values, valid=list(valid)))

    def push_mic(self, t_us: int, rms: float, peak: float) -> None:
        self._mic.push(int(t_us), MicSample(rms=float(rms), peak=float(peak)))

    def push_mel(self, t_us: int, log_mel) -> None:
        self._mel.push(int(t_us), list(log_mel))

    def push_event(self, event: dict) -> None:
        """方便直接餵 `protocol.parse_line()` / `ProtocolParser.feed()` 的
        輸出。跟時間對齊無關的事件型別（heartbeat/status/record）忽略。"""
        etype = event.get("type")
        if etype == "tof":
            self.push_tof(event["sensor"], event["t_us"], event["distance"], event["signal"], event["valid"])
        elif etype == "mic":
            self.push_mic(event["t_us"], event["rms"], event["peak"])
        elif etype == "mel":
            self.push_mel(event["t_us"], event["log_mel"])

    def frames(self, t_start_us: int, t_end_us: int, rate_hz: float = DEFAULT_RATE_HZ,
               interp: str = "nearest", max_gap_us: float = DEFAULT_MAX_GAP_US):
        """`yield AlignedFrame`，時間點是 `t_start_us` 起、每 `1/rate_hz`
        秒一幀，直到 `t_end_us`（含）。純函式：不消耗緩衝，可重複呼叫。"""
        if rate_hz <= 0:
            raise ValueError(f"rate_hz 必須 > 0，收到 {rate_hz}")
        if interp not in ("nearest", "linear"):
            raise ValueError(f"interp 必須是 'nearest' 或 'linear'，收到 {interp!r}")
        if t_end_us < t_start_us:
            return

        period_us = 1e6 / rate_hz
        n_frames = int((t_end_us - t_start_us) // period_us) + 1
        for i in range(n_frames):
            t_us = int(round(t_start_us + i * period_us))

            tof_A, tof_A_present = _resolve(self._tof["A"], t_us, interp, max_gap_us, _merge_tof, lambda p: p)
            tof_B, tof_B_present = _resolve(self._tof["B"], t_us, interp, max_gap_us, _merge_tof, lambda p: p)
            mic, mic_present = _resolve(self._mic, t_us, interp, max_gap_us, _merge_mic, lambda p: p)
            mel, mel_present = _resolve(self._mel, t_us, interp, max_gap_us, _merge_mel, lambda p: p)

            yield AlignedFrame(
                t_us=t_us,
                tof_A=tof_A, tof_A_present=tof_A_present,
                tof_B=tof_B, tof_B_present=tof_B_present,
                mic_rms=(mic.rms if mic is not None else None),
                mic_peak=(mic.peak if mic is not None else None),
                mic_present=mic_present,
                mel=mel, mel_present=mel_present,
            )
