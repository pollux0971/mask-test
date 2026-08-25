"""B18 端到端測試：真的跑 T04 的 `mock_device.py`（唯讀），用這裡的
`sens_command()`/`mel_command()` 組出來的字串送進去，驗證裝置真的照做
（`$T,A` 停止、`$STATUS` 的 `mel=` 翻轉），而不是只驗證字串格式本身。

**`AMB` 沒有端到端測試**：`mock_device.py` 目前完全沒實作 `AMB:<0|1>`
（沒有 handler，`$STATUS` 也不吐 `amb=`），`A16` 的 mock 支援還沒做。
`amb_command()` 本身的字串格式只在 `test_commands.py` 裡驗證，見完成回報。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import serial

from host.capture.protocol import parse_line
from host.control.commands import mel_command, sens_command

MOCK_DEVICE = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools" / "mock_device.py"


class _MockDeviceSession:
    def __init__(self, extra_args):
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK_DEVICE), "--seed", "1", *extra_args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.ser = None

    def __enter__(self):
        deadline = time.monotonic() + 5.0
        port_path = None
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                continue
            m = re.search(r"pty ready: (\S+)", line)
            if m:
                port_path = m.group(1)
                break
        assert port_path, "mock_device 逾時沒印出 pty 路徑"
        self.ser = serial.Serial(port_path, timeout=0.5)
        return self

    def __exit__(self, *exc):
        if self.ser is not None:
            self.ser.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def send(self, command: str) -> None:
        self.ser.write((command + "\n").encode("ascii"))

    def read_events(self, duration_s: float):
        events = []
        end_at = time.monotonic() + duration_s
        while time.monotonic() < end_at:
            raw = self.ser.readline()
            if not raw:
                continue
            event = parse_line(raw)
            if event is not None:
                events.append(event)
        return events


def test_sens_command_actually_stops_the_sensor():
    with _MockDeviceSession(["--fps", "30", "--dim", "4"]) as session:
        before = session.read_events(0.5)
        assert any(e["type"] == "tof" and e["sensor"] == "A" for e in before)

        session.send(sens_command("A", False))

        after = session.read_events(0.8)
        assert not any(e["type"] == "tof" and e["sensor"] == "A" for e in after)
        # B 感測器不該被 A 的開關影響到
        assert any(e["type"] == "tof" and e["sensor"] == "B" for e in after)
        # 關閉後裝置照 §1.1「輸出組態改變要重發 $STATUS」
        assert any(e["type"] == "status" for e in after)


def test_mel_command_toggles_status_self_description():
    with _MockDeviceSession(["--fps", "30", "--dim", "4"]) as session:
        session.read_events(0.3)  # 清掉開機時的第一批事件

        session.send(mel_command(True))
        after_on = session.read_events(0.5)
        status_events = [e for e in after_on if e["type"] == "status"]
        assert status_events and status_events[-1]["mel"] is True

        session.send(mel_command(False))
        after_off = session.read_events(0.5)
        status_events = [e for e in after_off if e["type"] == "status"]
        assert status_events and status_events[-1]["mel"] is False
