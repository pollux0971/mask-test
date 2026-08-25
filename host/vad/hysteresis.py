"""兩個 VAD 共用的雙閾值遲滯偵測核心。

`B15`（音訊）與 `B16`（ToF）**必須用同一個偵測器**。這不是為了少寫程式
——`B16` 要量的是 `lip_onset_us - voice_onset_us`，如果兩邊的狀態機、
平滑、邊界修正有任何一點不一樣，那個差值裡就混進了**演算法差異**，而它
會系統性地偏向某一邊。`B16` 的 story 明講了這個陷阱：

> 如果 ToF-VAD 的閾值比音訊 VAD 寬鬆，會系統性地產生「唇動比較早」的
> 假結果。

閾值寬鬆只是最明顯的一種；平滑視窗、掛延遲、邊界貼合方式不同，效果一樣。
所以兩邊的差異只剩**輸入訊號**與**呼叫端明確傳入的 σ 倍數**，其餘完全共用。

演算法本身與各個常數的由來（含實測數據）見 `audio_vad.py` 的模組說明。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 判斷閾值之前先做幾幀的置中移動平均。**這不是美化，是讓 3σ/1.5σ 這組
# 閾值真的能用。** 純底噪高於 1.5σ 的機率是每幀 6.7%，200 ms 掛延遲
# （31.25 Hz 下 6.25 幀）全部低於的機率只有 64.9%——也就是有三分之一的
# 機率被隨機尖峰重置，段落一路延長（實測邊界誤差衝到 1168 ms）。平滑
# 3 幀讓雜訊 sigma 降為 1/√3，等效門檻 2.60σ，機率升到 97.1%。
#
# 用**置中**視窗（不是因果視窗）才不會引入群延遲把邊界整個往後推。
DEFAULT_SMOOTH_FRAMES = 3

# 離開閾值要持續這麼久才算真的結束（B15 story 明訂）。
DEFAULT_HANGOVER_MS = 200.0

# 比這個短的段落當成雜訊尖峰丟掉。約等於「至少要有兩幀」。
DEFAULT_MIN_SEGMENT_MS = 50.0

# 起點回退的上限。回退是為了接住上升沿的腳，但低訊噪比時底噪本身就常常
# 高於離開閾值，不設上限會一路退穿整段靜止（實測退了將近一秒）。
DEFAULT_MAX_ONSET_BACKOFF_MS = 96.0

# sigma 的下限守衛，同 §3.2 對 ToF z-score 的做法。感測器壞掉或整段完全
# 沒有變異時 sigma 會是 0，除下去會變成 inf/NaN，讓整個判斷靜默壞掉。
SIGMA_FLOOR = 1e-3


@dataclass(frozen=True)
class Segment:
    """一段偵測到的語音。時間單位是 `t_us`（裝置時鐘 µs），與 `$M` 一致。"""

    start_us: int
    end_us: int
    peak_z: float             # 段內最大 rms 高於底噪幾個 sigma
    mean_z: float
    n_frames: int
    n_frames_above_enter: int

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    @property
    def duration_ms(self) -> float:
        return self.duration_us / 1000.0

    def to_dict(self) -> dict:
        return {
            "start_us": self.start_us, "end_us": self.end_us,
            "duration_us": self.duration_us,
            "peak_z": self.peak_z, "mean_z": self.mean_z,
            "n_frames": self.n_frames,
            "n_frames_above_enter": self.n_frames_above_enter,
        }


def _smooth(values, n_frames):
    """置中移動平均。`n_frames <= 1` 直接原樣回傳。

    邊緣用 `edge` 模式補值，不用補零——補零會在第一／最後幾幀製造一個
    往下的假斜坡，剛好落在我們最在意的邊界上。
    """
    if n_frames is None or n_frames <= 1:
        return values
    half = int(n_frames) // 2
    padded = np.pad(values, half, mode="edge")
    kernel = np.ones(2 * half + 1) / (2 * half + 1)
    return np.convolve(padded, kernel, mode="valid")


def _scan_hysteresis(decision, values, times, z, enter_thr, exit_thr,
                     hangover_us, min_segment_us, max_backoff_us, half_window):
    """雙閾值遲滯掃描。回傳 `(segments, discarded_short_count)`。

    狀態機只有兩個狀態，但「離開」是延遲確認的：先記下第一次低於離開閾值
    的時間，若在掛延遲結束前又回到閾值以上，就把它取消（這就是遲滯）。
    """
    segments = []
    discarded = 0
    in_voice = False
    start_idx = 0
    pending_exit_idx = None      # 第一次低於離開閾值的索引

    def close(end_idx):
        nonlocal discarded
        # 兩段式：平滑後的訊號負責**判斷**（狀態機才不會被底噪尖峰打斷），
        # 原始訊號負責**定位**（回報的邊界才不會被平滑的 ±半視窗糊掉）。
        lo, hi = _refine(values, enter_thr, exit_thr, start_idx, end_idx, half_window)
        seg = _make_segment(values, times, z, lo, hi, enter_thr)
        if seg.duration_us < min_segment_us:
            discarded += 1
        else:
            segments.append(seg)

    for i, value in enumerate(decision):
        if not in_voice:
            if value > enter_thr:
                in_voice = True
                # 起點退到同一個上升沿的腳：往回走到最後一個仍在離開閾值
                # 以上的幀。子音起始爬得快但峰值不高，只取越過進入閾值的
                # 那一幀會把起音切掉。上限見 MAX_ONSET_BACKOFF_MS。
                start_idx = i
                while (start_idx > 0
                       and decision[start_idx - 1] > exit_thr
                       and times[i] - times[start_idx - 1] <= max_backoff_us):
                    start_idx -= 1
                pending_exit_idx = None
            continue

        if value >= exit_thr:
            pending_exit_idx = None       # 回到閾值以上 → 取消待確認的結束
            continue

        if pending_exit_idx is None:
            pending_exit_idx = i
            continue

        # 掛延遲用時間算，不用幀數算：$M 會掉幀，用幀數在掉幀時會提早結束。
        if times[i] - times[pending_exit_idx] >= hangover_us:
            # 終點取「最後一幀仍在離開閾值以上」，與起點對稱（起點是上升沿
            # 上第一幀在離開閾值以上）。取第一幀低於閾值的話，段落會固定
            # 多含一個幀距（31.25 Hz 下 32 ms），對 <100 ms 的邊界預算是白
            # 白吃掉三分之一。
            close(pending_exit_idx - 1)
            in_voice = False
            pending_exit_idx = None

    if in_voice:
        # 資料在還沒確認結束時就沒了。結束點取第一次低於離開閾值處（若有），
        # 否則取最後一幀——不外推，時間戳只能來自真的收到的幀。
        close(pending_exit_idx - 1 if pending_exit_idx is not None else len(decision) - 1)

    return segments, discarded


def _refine(values, enter_thr, exit_thr, start_idx, end_idx, half_window):
    """把平滑訊號定出的段落，貼回原始訊號上真正的活動範圍。

    定義：**段落 = 主體（高於進入閾值）+ 與主體相連、高於離開閾值的裙邊。**

    「與主體相連」是關鍵。詞尾之後的底噪偶爾會連續兩三幀超過離開閾值
    （1.5σ 每幀有 6.7% 機率），若只看「掛延遲內最後一幀高於離開閾值」，
    段落就會被那撮雜訊往後拖——實測有一筆因此多了 112 ms。只要中間隔著
    一幀低於離開閾值，那撮雜訊就不屬於這個詞。

    找不到主體（整段都只在裙邊高度）時原樣回傳，不強行猜。
    """
    body = [j for j in range(start_idx, end_idx + 1) if values[j] > enter_thr]
    if not body:
        return start_idx, end_idx

    lo, hi = body[0], body[-1]
    # 往回補起音的腳，上限是狀態機給的起點再往前半個平滑視窗。
    lo_limit = max(0, start_idx - half_window)
    while lo > lo_limit and values[lo - 1] > exit_thr:
        lo -= 1
    # 往後補收音的尾，上限是狀態機給的終點（掛延遲已經確認過了，不外推）。
    hi_limit = min(len(values) - 1, end_idx)
    while hi < hi_limit and values[hi + 1] > exit_thr:
        hi += 1
    return lo, max(hi, lo)


def _make_segment(values, times, z, start_idx, end_idx, enter_thr) -> Segment:
    end_idx = max(end_idx, start_idx)
    sl = slice(start_idx, end_idx + 1)
    window = values[sl]
    return Segment(
        start_us=int(times[start_idx]),
        end_us=int(times[end_idx]),
        peak_z=float(np.max(z[sl])),
        mean_z=float(np.mean(z[sl])),
        n_frames=int(window.size),
        n_frames_above_enter=int(np.count_nonzero(window > enter_thr)),
    )


def thresholds(mu, sigma, enter_sigma, exit_sigma):
    """`(enter, exit)` 兩個絕對閾值。`sigma` 有下限守衛。"""
    sigma = max(float(sigma), SIGMA_FLOOR)
    mu = float(mu)
    return mu + float(enter_sigma) * sigma, mu + float(exit_sigma) * sigma


def detect_segments(
    values, times, mu, sigma, *, enter_sigma, exit_sigma,
    hangover_ms=DEFAULT_HANGOVER_MS,
    min_segment_ms=DEFAULT_MIN_SEGMENT_MS,
    smooth_frames=DEFAULT_SMOOTH_FRAMES,
    max_onset_backoff_ms=DEFAULT_MAX_ONSET_BACKOFF_MS,
):
    """在一串 `(values, times)` 上做雙閾值遲滯偵測。

    回傳 `(segments, discarded_short_count, enter_thr, exit_thr)`。
    `values`/`times` 必須已經是 numpy 陣列且依 `times` 排好序——排序與
    型別轉換由呼叫端負責，這裡只管演算法。
    """
    enter_thr, exit_thr = thresholds(mu, sigma, enter_sigma, exit_sigma)
    guarded_sigma = max(float(sigma), SIGMA_FLOOR)
    z = (values - float(mu)) / guarded_sigma

    half_window = max(0, int(smooth_frames)) // 2
    decision = _smooth(values, smooth_frames)

    segments, discarded = _scan_hysteresis(
        decision, values, times, z, enter_thr, exit_thr,
        hangover_us=int(hangover_ms * 1000),
        min_segment_us=int(min_segment_ms * 1000),
        max_backoff_us=int(max_onset_backoff_ms * 1000),
        half_window=half_window,
    )
    return segments, discarded, enter_thr, exit_thr
