"""B05 端到端：把 `PingSyncer` 接上真的 `mock_device.py`（T04）。

`test_ping_sync.py` 用假 callback 驗邏輯；這裡驗的是「接上一個真的會回話
的裝置時，整條走得通」——包括 pty、真實時間、`$T`/`$M` 在等待視窗裡持續
灌進來、以及 `$H` 之後那行 `$STATUS`。

沒有硬體也跑得動（mock 開的是 pty），所以放進一般測試。
"""
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from host.capture.protocol import ProtocolParser
from host.clock.align import ClockAligner
from host.clock.ping_sync import SESSION_END, SESSION_START, PingSyncer, SessionClockSync

REPO = Path(__file__).resolve().parents[2]
MOCK = REPO / "ssi-backlog" / "tools" / "mock_device.py"


class PtyLink:
    """把 mock 的 pty 包成 `PingSyncer` 要的兩個 callback。"""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        self._buf = b""

    def send_ping(self):
        os.write(self.fd, b"PING\n")

    def read_line(self, timeout_s):
        deadline = time.perf_counter() + timeout_s
        while True:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                return line
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                return None
            self._buf += chunk

    def close(self):
        os.close(self.fd)


@pytest.fixture
def mock_link():
    proc = subprocess.Popen(
        [sys.executable, "-u", str(MOCK), "--fps", "30", "--dim", "4",
         "--mic-fps", "20", "--seed", "7"],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    path = None
    for _ in range(50):
        line = proc.stdout.readline()
        if not line:
            break
        match = re.search(r"(/dev/pts/\d+)", line)
        if match:
            path = match.group(1)
            break
    if path is None:
        proc.kill()
        pytest.skip("mock_device 沒有印出 pty 路徑")

    link = PtyLink(path)
    try:
        yield link
    finally:
        link.close()
        proc.terminate()
        proc.wait(timeout=5)


def test_burst_against_mock_device(mock_link):
    """20 次 PING 全部拿到確認過的回覆，而且資料行沒有被吃掉。"""
    parser = ProtocolParser()
    seen = []
    syncer = PingSyncer(
        send_ping=mock_link.send_ping,
        read_line=mock_link.read_line,
        parser=parser,
        on_event=seen.append,
    )
    burst = syncer.burst(SESSION_START)

    assert burst.attempts == 20
    assert burst.n_ok == 20, f"timeouts={burst.timeouts} discarded={burst.discarded}"
    assert burst.confirmed is True
    assert burst.best is not None and burst.best.confirmed is True
    assert burst.rtt_min_us > 0
    assert burst.rtt_min_us <= burst.rtt_median_us

    # **這裡不驗「20 次至少 15 次在 10 ms 內」。** 那是硬體驗收條件，這個
    # mock 驗不了：它的主迴圈每輪睡最多 20 ms（`mock_device.py:453`），而
    # 指令只在每輪開頭 poll 一次（`:393`），所以 PING 的回應延遲是 0–20 ms
    # 的均勻分布——量到的是 mock 的排程粒度，不是韌體的反應速度。實測
    # fast(≤10ms) 穩定落在 7/20 左右，與 fps/dim 無關，正是排程粒度的特徵。
    #
    # 但這恰好示範了 §1.3「取最小值而非平均值」為什麼有效：即使中位數被
    # 排程粒度拖到 11–15 ms，最小值仍然落在 0.2–5.6 ms。所以這裡驗的是
    # **最小值遠優於中位數**，那才是這個模組真正依賴的性質。
    assert burst.rtt_min_us < burst.rtt_median_us
    # 校時期間照樣收到 ToF/mic 資料行，沒有在資料流上打洞
    assert any(e["type"] in ("tof", "mic") for e in seen)
    assert parser.stats.malformed == 0


def test_session_start_and_end_produce_a_drift_estimate(mock_link):
    """驗收條件：session 首尾各存一組校時點、漂移量寫進 metadata。"""
    syncer = PingSyncer(
        send_ping=mock_link.send_ping,
        read_line=mock_link.read_line,
        count=8,
    )
    start = syncer.burst(SESSION_START)
    time.sleep(1.0)
    end = syncer.burst(SESSION_END)

    sync = SessionClockSync(start=start, end=end)
    assert sync.device_span_us > 500_000          # 至少跨了那 1 秒
    meta = sync.to_meta()
    assert meta["session_start_device_us"] is not None
    assert meta["session_end_device_us"] is not None
    assert meta["clock_sync_confirmed"] is True
    # mock 的裝置時鐘就是主機時鐘，漂移應該很小；這裡只確認算得出數字且
    # 在合理範圍，不斷言精確值（真實硬體才有真的晶振誤差）。
    assert meta["clock_drift_us"] is not None
    assert abs(sync.drift_ppm) < 50_000


def test_ping_samples_feed_the_b04_aligner(mock_link):
    """B04 的最小延遲濾波吃得下 B05 的樣本，並且擬合得出來。"""
    syncer = PingSyncer(
        send_ping=mock_link.send_ping,
        read_line=mock_link.read_line,
        count=6,
    )
    aligner = ClockAligner()
    for label in (SESSION_START, SESSION_END):
        burst = syncer.burst(label)
        assert syncer.feed_into(aligner, burst) == 1
        time.sleep(1.1)          # 跨到下一個 bucket，回歸才有兩個點

    assert aligner.n_buckets >= 2
    fit = aligner.fit()
    assert fit.n_samples_used >= 2
    assert fit.slope > 0


def test_heartbeat_from_mock_carries_bw_field(mock_link):
    """順帶確認 mock 的 `$H` 第 8 段（A15 `bw_bytes_since_last`）解得出來。"""
    parser = ProtocolParser()
    deadline = time.perf_counter() + 3.0
    heartbeat = None
    while time.perf_counter() < deadline and heartbeat is None:
        line = mock_link.read_line(0.5)
        if line is None:
            continue
        event = parser.feed(line)
        if event is not None and event["type"] == "heartbeat":
            heartbeat = event

    assert heartbeat is not None, "3 秒內沒收到 $H"
    assert heartbeat["bw_bytes_since_last"] is not None
    assert heartbeat["bw_bytes_since_last"] > 0
    assert heartbeat["heap"] > 0
