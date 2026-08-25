"""`host/clock/ping_sync.py`（B05）的單元測試。

序列埠用假的 callback 注入，所以整組測試不碰硬體、不碰 pty，跑得很快。
真的接 mock device 的端到端測試在 `test_ping_sync_mock_device.py`。
"""
import pytest

from host.capture.protocol import ProtocolParser
from host.clock.align import ClockAligner
from host.clock.ping_sync import (
    MIN_FAST_RESPONSES,
    PING_BURST_COUNT,
    PING_RTT_BUDGET_US,
    PingBurst,
    PingSample,
    PingSyncer,
    SessionClockSync,
    burst_from_samples,
)


class FakeDevice:
    """假裝置：收到 PING 就排一行 `$H` 再一行 `$STATUS`（順序與 A09 韌體
    及 `mock_device.py` 一致）。時鐘完全由測試控制，沒有 sleep。"""

    def __init__(self, *, rtt_us_seq=None, device_t0_us=1_000_000,
                 device_rate=1.0, reply=True, extra_lines=None):
        self.now_s = 100.0                 # 單調時鐘
        self.wall_s = 1_700_000_000.0      # 牆上時鐘
        self.device_t0_us = device_t0_us
        self.device_rate = device_rate     # 裝置時鐘相對主機的速率
        self.rtt_us_seq = list(rtt_us_seq or [2000.0])
        self.reply = reply
        self.extra_lines = list(extra_lines or [])
        self.queue = []
        self.pings = 0
        self._i = 0

    # -- 時鐘 -----------------------------------------------------------
    def monotonic(self):
        return self.now_s

    def wall(self):
        return self.wall_s

    def sleep(self, seconds):
        self._advance(seconds)

    def _advance(self, seconds):
        self.now_s += seconds
        self.wall_s += seconds

    def _device_us(self):
        elapsed_us = (self.now_s - 100.0) * 1e6
        return int(self.device_t0_us + elapsed_us * self.device_rate)

    # -- 傳輸 -----------------------------------------------------------
    def _next_rtt_us(self):
        rtt = self.rtt_us_seq[self._i % len(self.rtt_us_seq)]
        self._i += 1
        return rtt

    def send_ping(self):
        self.pings += 1
        self.queue.extend(self.extra_lines)
        if not self.reply:
            return
        rtt_us = self._next_rtt_us()
        # 回覆在 rtt 之後才「到達」：讀的時候才推進時鐘。
        self.queue.append(("advance", rtt_us / 1e6))
        self.queue.append(("line", None))          # $H，t_us 讀取當下才算
        self.queue.append(("line", "$STATUS,res=4,proto=2,fw=abc"))

    def read_line(self, timeout_s):
        if not self.queue:
            self._advance(timeout_s)
            return None
        item = self.queue.pop(0)
        if isinstance(item, tuple) and item[0] == "advance":
            if item[1] > timeout_s:
                self._advance(timeout_s)
                self.queue.insert(0, ("advance", item[1] - timeout_s))
                return None
            self._advance(item[1])
            return self.read_line(timeout_s - item[1])
        if isinstance(item, tuple) and item[0] == "line":
            if item[1] is None:
                return f"$H,{self._device_us()},0,0,0,142300,42"
            return item[1]
        return item


def make_syncer(dev, **kwargs):
    kwargs.setdefault("count", PING_BURST_COUNT)
    kwargs.setdefault("gap_s", 0.0)
    return PingSyncer(
        send_ping=dev.send_ping,
        read_line=dev.read_line,
        monotonic=dev.monotonic,
        host_clock=dev.wall,
        sleep=dev.sleep,
        **kwargs,
    )


# ------------------------------------------------------------ 基本 burst


def test_burst_sends_20_pings_and_collects_samples():
    dev = FakeDevice(rtt_us_seq=[2000.0])
    burst = make_syncer(dev).burst()
    assert dev.pings == PING_BURST_COUNT
    assert burst.n_ok == PING_BURST_COUNT
    assert burst.attempts == PING_BURST_COUNT
    assert burst.timeouts == 0
    assert burst.confirmed is True


def test_best_sample_is_minimum_rtt_not_average():
    """§1.3：主機端統計時用最小值而非平均值。"""
    rtts = [8000.0] * 19 + [1200.0]
    dev = FakeDevice(rtt_us_seq=rtts)
    burst = make_syncer(dev).burst()
    assert burst.best.rtt_us == pytest.approx(1200.0, rel=1e-6)
    assert burst.rtt_min_us == pytest.approx(1200.0, rel=1e-6)
    # 中位數會被那 19 次拖上去——正是不能用平均/中位數的原因
    assert burst.rtt_median_us > 7000.0


def test_host_us_compensates_half_the_rtt():
    dev = FakeDevice(rtt_us_seq=[4000.0])
    burst = make_syncer(dev, count=1).burst()
    s = burst.samples[0]
    assert s.host_us == pytest.approx(s.t0_host_us + s.rtt_us / 2.0)


def test_acceptance_15_of_20_within_10ms():
    """驗收條件：20 次 PING 中至少 15 次在 10 ms 內回應。"""
    dev = FakeDevice(rtt_us_seq=[1000.0] * 15 + [50_000.0] * 5)
    burst = make_syncer(dev).burst()
    assert burst.fast_responses == 15
    assert burst.meets_acceptance is True

    dev2 = FakeDevice(rtt_us_seq=[1000.0] * 14 + [50_000.0] * 6)
    burst2 = make_syncer(dev2).burst()
    assert burst2.fast_responses == 14
    assert burst2.meets_acceptance is False


def test_within_budget_boundary_is_inclusive():
    s = PingSample(1, 2.0, PING_RTT_BUDGET_US, 0.0, True)
    assert s.within_budget is True
    assert PingSample(1, 2.0, PING_RTT_BUDGET_US + 1, 0.0, True).within_budget is False


# ------------------------------------------------- 週期心跳 vs PING 回覆


def test_stray_periodic_heartbeat_does_not_poison_the_minimum():
    """1 Hz 的週期心跳與 PING 回覆是同一種行。若把飄進視窗的心跳當成回覆，
    量到的 RTT 會遠小於真值——而我們刻意取最小值，一個假樣本就毀了整組。"""
    dev = FakeDevice(rtt_us_seq=[3000.0])
    # 每次 PING 送出後 0.2 ms，先飄進一行週期心跳（比真正的回覆早很多）
    dev.extra_lines = [("advance", 0.0002), ("line", None)]
    burst = make_syncer(dev).burst()

    assert burst.confirmed is True
    assert burst.n_ok == PING_BURST_COUNT
    assert burst.stray_heartbeats == PING_BURST_COUNT      # 每次都認出一個
    # 最小值應該是真正的回覆（0.2 ms 的假心跳 + 3 ms 的真回覆 = 3.2 ms），
    # 不是那個 0.2 ms 的假樣本。差一個數量級，抓錯了一眼就看得出來。
    assert burst.best.rtt_us == pytest.approx(3200.0, rel=1e-3)
    assert burst.best.confirmed is True


def test_unconfirmed_sample_is_reported_but_flagged():
    """韌體若沒在 `$H` 之後重發 `$STATUS`，仍要拿得到樣本，但要標出來
    ——回一個空結果會讓呼叫端以為裝置沒回應。"""
    dev = FakeDevice(rtt_us_seq=[2000.0])
    dev.send_ping = lambda: (
        dev.__dict__.__setitem__("pings", dev.pings + 1),
        dev.queue.extend([("advance", 0.002), ("line", None)]),
    )
    burst = make_syncer(dev).burst()
    assert burst.n_ok == PING_BURST_COUNT
    assert burst.confirmed is False
    assert all(s.confirmed is False for s in burst.samples)
    assert burst.best is not None            # 仍挑得出最小值


def test_best_prefers_confirmed_over_faster_unconfirmed():
    confirmed_slow = PingSample(1000, 10.0, 5000.0, 0.0, True)
    unconfirmed_fast = PingSample(1000, 10.0, 100.0, 0.0, False)
    burst = burst_from_samples("b", [unconfirmed_fast, confirmed_slow], attempts=2)
    assert burst.best is confirmed_slow      # 確認過的優先，即使比較慢
    assert burst.confirmed is True


# --------------------------------------------------------- 失敗與異常


def test_no_reply_counts_as_timeout_and_does_not_raise():
    dev = FakeDevice(reply=False)
    burst = make_syncer(dev, count=5, timeout_s=0.05).burst()
    assert burst.n_ok == 0
    assert burst.timeouts == 5
    assert burst.best is None
    assert burst.meets_acceptance is False
    assert burst.to_meta()["session_start_device_us"] is None   # to_meta 不炸


def test_write_failure_is_swallowed_as_timeout():
    """序列埠斷線不該讓整組校時炸掉，只該讓 n_ok 變小。"""
    def boom():
        raise OSError("port disappeared")

    dev = FakeDevice()
    syncer = make_syncer(dev, count=3)
    syncer.send_ping = boom
    burst = syncer.burst()
    assert burst.n_ok == 0 and burst.timeouts == 3


def test_data_lines_are_handed_back_not_dropped():
    """在 session 首尾校時不能在資料流上打洞。"""
    seen = []
    dev = FakeDevice(rtt_us_seq=[2000.0])
    dev.extra_lines = [("line", "$M,7,1234567,120,900")]
    burst = make_syncer(dev, count=3, on_event=seen.append).burst()
    assert burst.n_ok == 3
    assert [e["type"] for e in seen] == ["mic"] * 3
    assert seen[0]["rms"] == 120


def test_malformed_lines_during_burst_do_not_break_it():
    dev = FakeDevice(rtt_us_seq=[2000.0])
    dev.extra_lines = [("line", "$H,broken"), ("line", "garbage \x00\xff")]
    burst = make_syncer(dev, count=3).burst()
    assert burst.n_ok == 3 and burst.confirmed is True


def test_shared_parser_state_is_reused():
    """呼叫端可以把自己的 `ProtocolParser` 傳進來，統計不會被切成兩份。"""
    parser = ProtocolParser()
    dev = FakeDevice(rtt_us_seq=[2000.0])
    make_syncer(dev, count=2, parser=parser).burst()
    assert parser.stats.parsed > 0
    assert parser.status is not None          # burst 期間收到的 $STATUS


# ------------------------------------------------------- 餵給 B04 的模型


def test_feed_into_aligner_best_only_by_default():
    dev = FakeDevice(rtt_us_seq=[8000.0] * 19 + [1000.0])
    syncer = make_syncer(dev)
    burst = syncer.burst()
    aligner = ClockAligner()
    assert syncer.feed_into(aligner, burst) == 1
    assert aligner.n_buckets == 1
    fed = aligner.clean_samples()[0]
    assert fed.device_us == int(burst.best.device_us)


def test_feed_into_aligner_all_samples_opt_in():
    dev = FakeDevice(rtt_us_seq=[2000.0])
    syncer = make_syncer(dev)
    burst = syncer.burst()
    aligner = ClockAligner()
    assert syncer.feed_into(aligner, burst, best_only=False) == PING_BURST_COUNT


def test_to_clock_samples_is_empty_when_burst_failed():
    burst = PingBurst(label="x", samples=(), attempts=20, timeouts=20)
    assert burst.to_clock_samples() == []
    assert burst.to_clock_samples(best_only=False) == []


# ------------------------------------------------------- session 漂移


def make_burst(label, device_us, host_us, rtt_us=2000.0):
    return burst_from_samples(
        label, [PingSample(device_us, host_us, rtt_us, host_us - rtt_us / 2, True)], attempts=20
    )


def test_drift_is_host_span_minus_device_span():
    """裝置慢 100 ms → 主機經過的時間比裝置多 100 ms → drift 為正。"""
    span_us = 600_000_000
    start = make_burst("session_start", 1_000_000, 5_000_000.0)
    end = make_burst("session_end", 1_000_000 + span_us, 5_000_000.0 + span_us + 100_000)
    sync = SessionClockSync(start=start, end=end)
    assert sync.device_span_us == span_us
    assert sync.host_span_us == pytest.approx(span_us + 100_000.0)
    assert sync.drift_us == pytest.approx(100_000.0)

    # 反過來：裝置走得比主機快 → drift 為負
    fast_end = make_burst("session_end", 1_000_000 + span_us, 5_000_000.0 + span_us - 100_000)
    assert SessionClockSync(start=start, end=fast_end).drift_us == pytest.approx(-100_000.0)


def test_drift_ppm_matches_a_known_slow_device_clock():
    """裝置 10 分鐘慢了 30 ms = 50 ppm（典型晶振誤差）。"""
    span_us = 600_000_000                     # 10 分鐘
    drift_us = 30_000                         # 30 ms
    start = make_burst("session_start", 1_000_000, 1_000_000.0)
    end = make_burst("session_end", 1_000_000 + span_us, 1_000_000.0 + span_us + drift_us)
    sync = SessionClockSync(start=start, end=end)
    assert sync.drift_us == pytest.approx(drift_us)
    assert sync.drift_ppm == pytest.approx(50.0, rel=1e-3)


def test_drift_is_none_without_an_end_burst():
    sync = SessionClockSync(start=make_burst("session_start", 1, 2.0))
    assert sync.drift_us is None
    assert sync.drift_ppm is None
    assert sync.to_meta()["clock_drift_us"] is None


def test_drift_is_none_when_a_burst_has_no_samples():
    empty = PingBurst(label="session_end", samples=(), attempts=20, timeouts=20)
    sync = SessionClockSync(start=make_burst("session_start", 1, 2.0), end=empty)
    assert sync.drift_us is None
    assert sync.to_meta()["clock_sync_confirmed"] is False


def test_session_meta_carries_both_endpoints():
    """驗收條件：session 首尾各存一組校時點、漂移量寫進 metadata。"""
    start = make_burst("session_start", 1_000_000, 1_000_000.0)
    end = make_burst("session_end", 61_000_000, 61_000_100.0)
    meta = SessionClockSync(start=start, end=end).to_meta()
    assert meta["session_start_device_us"] == 1_000_000
    assert meta["session_end_device_us"] == 61_000_000
    assert meta["clock_drift_us"] == pytest.approx(100.0)
    assert meta["clock_sync_span_us"] == 60_000_000
    assert meta["clock_sync_confirmed"] is True
    assert meta["session_start_rtt_min_us"] == pytest.approx(2000.0)


def test_end_to_end_burst_pair_produces_plausible_drift():
    """整條走一遍：兩組真的跑出來的 burst，中間裝置時鐘快 100 ppm。"""
    dev = FakeDevice(rtt_us_seq=[3000.0, 2000.0, 5000.0], device_rate=1.0001)
    syncer = make_syncer(dev, count=5)
    start = syncer.burst("session_start")
    for _ in range(200):                      # 讓時間走一段
        dev.sleep(0.05)
    end = syncer.burst("session_end")

    sync = SessionClockSync(start=start, end=end)
    assert sync.device_span_us > 0
    # 裝置走得比主機快 100 ppm → 主機經過的時間比較短 → drift 為負
    assert sync.drift_ppm == pytest.approx(-100.0, rel=0.05)
    assert start.meets_acceptance is False    # count=5，本來就到不了 15
    assert start.fast_responses == 5


def test_min_fast_responses_constant_matches_story():
    assert MIN_FAST_RESPONSES == 15
    assert PING_BURST_COUNT == 20
    assert PING_RTT_BUDGET_US == 10_000.0
