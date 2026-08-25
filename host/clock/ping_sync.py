"""B05 — 用 `PING` 主動取校時樣本（CONTRACTS.md §1.2 / §1.3）。

被動等 1 Hz 的 `$H` 心跳來校時，取樣點受當下 UART 排隊狀況擺布；主動
`PING` 可以挑在流量低谷連發一串，從裡面挑最乾淨的那一次。

## 為什麼「取最小值」有效——三件事綁在一起

§1.3 凍結了統計方法：

> **PING 回應延遲**：含最多 2 ms 排隊延遲，**主機端統計時用最小值而非
> 平均值**。

這條規定能成立，是因為韌體側（`A09`）配合了兩件事：

1. **PING 路徑上完全不印 log。** 一行 `ESP_LOGI` 約 60 bytes，@460800 baud
   約 1.3 ms——對 5 ms 的對齊預算是實質成本，不是雜訊。
2. **`$H` 的 `t_us` 在 `uart_out_lock()` 之後才取樣。**

第 2 點是關鍵：時間戳是「輪到我寫 UART 的那一刻」量的，所以**排隊延遲
只出現在往返時間裡，不會混進時間戳**。因此 RTT 最小的那一次，就是排隊
延遲最接近 0 的那一次，它的 `(t_us, host_time)` 配對也最接近真實的時鐘
關係。若韌體改成在取得 lock 之前就取樣，排隊延遲會被寫進時間戳，「取
最小值」就失去意義——這三件事是綁在一起的，動其中一個要回頭看另外兩個。

## 怎麼分辨 PING 回覆與週期心跳

`$H` 沒有 request id，PING 的回覆與 1 Hz 的週期心跳**是同一種行、長得
一模一樣**。若把恰好在等待視窗裡飄進來的週期心跳當成回覆，量到的 RTT
會遠小於真值，而我們又刻意取最小值——**一個假樣本就足以污染整組校時**。

分辨方法來自 §1.1：裝置每次收到 `PING` 都要重發一次 `$STATUS`，而且
（`A09` 與 `mock_device.py` 一致）順序是 `$H` 先、`$STATUS` 後。所以

    PING 回覆 = 後面緊跟著一行 $STATUS 的那個 $H

週期心跳後面不會跟 `$STATUS`。本模組據此標記 `confirmed`；整個 burst 都
拿不到確認樣本時會退回未確認樣本並標 `confirmed=False`，讓呼叫端知道
這組數字的可信度較低，而不是安靜地回一個空結果。

## 與 B04 的關係

`host/clock/align.py`（`B04`）的模型是「每個 bucket 只留延遲最小的樣本，
再回歸」。B05 產出的樣本就是餵給它的：`feed_into()` 預設只送每個 burst
裡 RTT 最小的那一個，理由同 §1.3。B04 沒有（也不需要）逐樣本權重參數
——它的最小延遲濾波本身就是加權：乾淨的點勝出，髒的點被丟掉。

本模組不做 IO，序列埠的讀寫由呼叫端以 callback 注入，所以測得動。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from host.capture.protocol import ProtocolParser
from host.clock.align import ClockSample

# story B05：session 開始與結束時各連發 20 次。
PING_BURST_COUNT = 20

# 單次 PING 等回覆的上限。§1.3 的預算是「含最多 2 ms 排隊延遲」，100 ms
# 已經比它大兩個數量級——這個值是用來偵測「裝置沒回應」，不是用來卡效能。
PING_TIMEOUT_S = 0.1

# 兩次 PING 之間稍微讓開，避免把 20 次擠成一個突發把 UART 塞住，
# 反而量到自己造成的排隊延遲。
PING_GAP_S = 0.005

# 驗收條件：20 次至少 15 次在 10 ms 內回應。
PING_RTT_BUDGET_US = 10_000.0
MIN_FAST_RESPONSES = 15

# 明顯不可能的 RTT：主機時鐘被 NTP 往回撥、或 callback 給了亂七八糟的值。
# 這種樣本直接丟掉並計數，不能讓它成為「最小值」。
MIN_PLAUSIBLE_RTT_US = 0.0

SESSION_START = "session_start"
SESSION_END = "session_end"


@dataclass(frozen=True)
class PingSample:
    """一次 PING 往返的結果。

    `host_us` 用 `t0 + rtt/2`，假設去程與回程延遲對稱。這對 UART 不完全
    成立（送出 `PING\\n` 5 bytes vs 收回 `$H` 約 60 bytes，@460800 baud
    差約 1.2 ms），但誤差在 1 ms 量級，遠小於 5 ms 的對齊目標。**這是刻意
    接受的已知偏差，不是疏漏**——要再壓下去得靠雙向時間戳協定，那不在
    本專案範圍。
    """

    device_us: int          # `$H` 回報的 t_us
    host_us: float          # 主機時鐘（µs），已補半個 RTT
    rtt_us: float
    t0_host_us: float       # 送出 PING 當下的主機時鐘
    confirmed: bool         # 後面是否緊跟著 $STATUS（見模組 docstring）

    @property
    def within_budget(self) -> bool:
        return self.rtt_us <= PING_RTT_BUDGET_US

    def to_clock_sample(self) -> ClockSample:
        """轉成 B04 的樣本型別。"""
        return ClockSample(device_us=int(self.device_us), host_us=int(round(self.host_us)))


@dataclass(frozen=True)
class PingBurst:
    """一組（預設 20 次）PING 的結果。"""

    label: str
    samples: tuple[PingSample, ...]
    attempts: int
    timeouts: int = 0
    discarded: int = 0          # RTT 不合理（時鐘倒退等）而丟掉的
    stray_heartbeats: int = 0   # 等待期間飄進來、被判定為週期心跳的 $H

    @property
    def n_ok(self) -> int:
        return len(self.samples)

    @property
    def confirmed(self) -> bool:
        """整組裡至少有一個樣本是被 `$STATUS` 確認過的 PING 回覆。"""
        return any(s.confirmed for s in self.samples)

    @property
    def best(self) -> Optional[PingSample]:
        """RTT 最小的樣本 = 排隊延遲最接近 0 = 最乾淨的校時點（§1.3）。

        優先在「被 `$STATUS` 確認過」的樣本裡挑；一個都沒有時才退回全部
        樣本裡挑，此時 `confirmed` 是 `False`，呼叫端看得到。
        """
        pool = [s for s in self.samples if s.confirmed] or list(self.samples)
        if not pool:
            return None
        return min(pool, key=lambda s: s.rtt_us)

    @property
    def fast_responses(self) -> int:
        """RTT ≤ 10 ms 的次數（驗收條件用）。"""
        return sum(1 for s in self.samples if s.within_budget)

    @property
    def meets_acceptance(self) -> bool:
        """驗收條件：20 次 PING 中至少 15 次在 10 ms 內回應。"""
        return self.fast_responses >= MIN_FAST_RESPONSES

    @property
    def rtt_min_us(self) -> Optional[float]:
        return min((s.rtt_us for s in self.samples), default=None)

    @property
    def rtt_median_us(self) -> Optional[float]:
        if not self.samples:
            return None
        ordered = sorted(s.rtt_us for s in self.samples)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def to_clock_samples(self, best_only: bool = True) -> list[ClockSample]:
        """轉成 B04 吃的 `ClockSample`。

        `best_only=True`（預設）只給 RTT 最小的那一個，照 §1.3。
        `best_only=False` 給全部，讓 B04 自己的最小延遲濾波去挑——注意兩者
        挑的準則不同（RTT 最小 vs `host_us - device_us` 最小），結果可能不是
        同一個樣本，所以預設用契約明文規定的那個。
        """
        if best_only:
            best = self.best
            return [best.to_clock_sample()] if best else []
        return [s.to_clock_sample() for s in self.samples]

    def to_meta(self) -> dict:
        """這組 burst 的摘要，給 session metadata 用。"""
        best = self.best
        return {
            f"{self.label}_device_us": int(best.device_us) if best else None,
            f"{self.label}_host_us": float(best.host_us) if best else None,
            f"{self.label}_rtt_min_us": float(best.rtt_us) if best else None,
            f"{self.label}_rtt_median_us": self.rtt_median_us,
            f"{self.label}_n_ok": self.n_ok,
            f"{self.label}_n_attempts": self.attempts,
            f"{self.label}_fast_responses": self.fast_responses,
            f"{self.label}_confirmed": self.confirmed,
        }


class PingSyncer:
    """連發 PING、挑出最乾淨的校時點。

    IO 用 callback 注入，本類別自己不碰序列埠——這樣它測得動，也不必知道
    呼叫端是 `pyserial`、pty 還是別的東西。

    * `send_ping()` —— 送一行 `PING`（含換行）到裝置。
    * `read_line(timeout_s)` —— 讀下一行，逾時回 `None`。回 `str`/`bytes`
      都可以。

    等待 PING 回覆期間照樣會收到 `$T` / `$M` 等資料行。**那些行不會被丟掉**
    ——它們照常餵給 `parser`，並透過 `on_event` 交還給呼叫端，所以在 session
    首尾做校時不會在資料流上打洞。
    """

    def __init__(
        self,
        send_ping: Callable[[], None],
        read_line: Callable[[float], object],
        *,
        parser: Optional[ProtocolParser] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        count: int = PING_BURST_COUNT,
        timeout_s: float = PING_TIMEOUT_S,
        gap_s: float = PING_GAP_S,
        # RTT 用單調時鐘量（不會被 NTP 往回撥）；時間戳用牆上時鐘（跨
        # session 要能放到同一條時間軸）。兩個時鐘各司其職，不混用。
        monotonic: Callable[[], float] = time.perf_counter,
        host_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.send_ping = send_ping
        self.read_line = read_line
        self.parser = parser if parser is not None else ProtocolParser()
        self.on_event = on_event
        self.count = count
        self.timeout_s = timeout_s
        self.gap_s = gap_s
        self.monotonic = monotonic
        self.host_clock = host_clock
        self.sleep = sleep

    # ------------------------------------------------------------------

    def burst(self, label: str = SESSION_START) -> PingBurst:
        """連發 `count` 次 PING，回傳整組結果。**不拋例外**——裝置沒回應
        只會讓 `n_ok` 變小，不會中斷呼叫端。"""
        samples: list[PingSample] = []
        timeouts = discarded = strays = 0

        for i in range(self.count):
            if i and self.gap_s:
                self.sleep(self.gap_s)
            result = self._one_ping()
            strays += result.strays
            if result.sample is None:
                if result.discarded:
                    discarded += 1
                else:
                    timeouts += 1
            else:
                samples.append(result.sample)

        return PingBurst(
            label=label,
            samples=tuple(samples),
            attempts=self.count,
            timeouts=timeouts,
            discarded=discarded,
            stray_heartbeats=strays,
        )

    def sync_session(self, start: PingBurst, end: PingBurst) -> "SessionClockSync":
        return SessionClockSync(start=start, end=end)

    def feed_into(self, aligner, burst: PingBurst, best_only: bool = True) -> int:
        """把 burst 的校時點餵給 B04 的 `ClockAligner`，回傳餵進去幾個。

        B04 沒有逐樣本權重參數，也不需要——它每個 bucket 只留延遲最小的
        樣本，本身就是加權：PING 樣本的排隊延遲接近 0，自然會勝出同一個
        bucket 裡的被動 `$H` 樣本。這就是 story 講的「加權後餵給 B04」。
        """
        fed = 0
        for sample in burst.to_clock_samples(best_only=best_only):
            aligner.add_sample(sample.device_us, sample.host_us)
            fed += 1
        return fed

    # ------------------------------------------------------------ 內部

    @dataclass
    class _PingResult:
        sample: Optional[PingSample] = None
        strays: int = 0
        discarded: bool = False

    def _one_ping(self) -> "_PingResult":
        """送一次 PING 並等回覆。

        回覆的判定見模組 docstring：`$H` 之後緊跟著 `$STATUS` 才算確認。
        視窗內若先飄進週期心跳，會被後來的 `$H` 取代（並計入 `strays`）；
        視窗結束時只剩未確認的 `$H`，仍然回報但標 `confirmed=False`。
        """
        result = PingSyncer._PingResult()

        t0_mono = self.monotonic()
        t0_host_us = self.host_clock() * 1e6
        try:
            self.send_ping()
        except Exception:
            # 序列埠斷線之類的：當成這次逾時，讓上層看到 n_ok 變小，
            # 而不是讓整組校時炸掉。
            return result

        deadline = t0_mono + self.timeout_s
        candidate: Optional[PingSample] = None

        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            line = self.read_line(remaining)
            if line is None:
                break

            event = self.parser.feed(line)
            if event is None:
                continue

            if event["type"] == "heartbeat":
                rtt_us = (self.monotonic() - t0_mono) * 1e6
                if rtt_us <= MIN_PLAUSIBLE_RTT_US:
                    # 時鐘倒退或 callback 給了怪值。丟掉——這種樣本若留著
                    # 一定會變成「最小值」，直接毀掉整組校時。
                    result.discarded = True
                    continue
                if candidate is not None:
                    # 前一個 $H 沒等到 $STATUS 就又來一個 $H → 前一個是
                    # 週期心跳，不是這次 PING 的回覆。
                    result.strays += 1
                candidate = PingSample(
                    device_us=event["t_us"],
                    host_us=t0_host_us + rtt_us / 2.0,
                    rtt_us=rtt_us,
                    t0_host_us=t0_host_us,
                    confirmed=False,
                )
                continue

            if event["type"] == "status" and candidate is not None:
                # 確認：這個 $H 是 PING 的回覆。
                result.sample = PingSample(**{**candidate.__dict__, "confirmed": True})
                return result

            # `$T` / `$M` / `$F` 等資料行照常交還給呼叫端，不能因為在校時
            # 就把它們丟掉。
            if self.on_event is not None:
                self.on_event(event)

        if candidate is not None:
            result.sample = candidate      # 沒等到 $STATUS，仍回報但未確認
        return result


@dataclass(frozen=True)
class SessionClockSync:
    """session 首尾各一組校時點，據此算出這段期間的實際時鐘漂移。

    兩個端點的差可以直接算漂移，不必靠回歸——這是 B04 模型之外的一個
    **獨立檢查**：若 `drift_ppm` 與 B04 擬合出來的 `slope` 對不上，代表
    其中一邊有問題（例如 session 中途發生 clock jump），值得人看一眼。
    """

    start: PingBurst
    end: Optional[PingBurst] = None
    _: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def device_span_us(self) -> Optional[int]:
        a, b = self._endpoints()
        return None if a is None else b.device_us - a.device_us

    @property
    def host_span_us(self) -> Optional[float]:
        a, b = self._endpoints()
        return None if a is None else b.host_us - a.host_us

    @property
    def drift_us(self) -> Optional[float]:
        """整段 session 累積的漂移：主機經過的時間 − 裝置經過的時間。
        正值代表裝置的時鐘走得比主機慢。"""
        a, b = self._endpoints()
        if a is None:
            return None
        return (b.host_us - a.host_us) - (b.device_us - a.device_us)

    @property
    def drift_ppm(self) -> Optional[float]:
        span = self.device_span_us
        drift = self.drift_us
        if not span or drift is None:
            return None
        return drift / span * 1e6

    def _endpoints(self):
        if self.end is None:
            return None, None
        a, b = self.start.best, self.end.best
        if a is None or b is None:
            return None, None
        return a, b

    def to_meta(self) -> dict:
        """session metadata 用的欄位。

        ⚠️ T02 §2 的 `/meta` 目前只凍結了 `clock_slope` / `clock_offset` /
        `clock_residual_p95`，**沒有漂移相關欄位**。下面這些鍵名是本 story
        的提案，尚未進契約——見完成回報，等調度員裁決後再寫進 HDF5。
        """
        meta = {
            "clock_drift_us": self.drift_us,
            "clock_drift_ppm": self.drift_ppm,
            "clock_sync_span_us": self.device_span_us,
            "clock_sync_confirmed": self.start.confirmed and bool(
                self.end is not None and self.end.confirmed
            ),
        }
        meta.update(self.start.to_meta())
        if self.end is not None:
            meta.update(self.end.to_meta())
        return meta


def burst_from_samples(label: str, samples: Sequence[PingSample], attempts: int) -> PingBurst:
    """測試與離線重播用：直接從既有樣本組一個 burst。"""
    return PingBurst(label=label, samples=tuple(samples), attempts=attempts)
