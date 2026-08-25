"""B06 端到端測試：真的跑 T04 的 `mock_device.py`（唯讀，不改），用 B01 的
`host/capture/protocol.py`（唯讀，只 import）解析真實協定行，餵進
`Aligner`，驗證對齊結果。

`mock_device.py` 目前不送 `$F`（mel 明文在 T04 docstring 標示「out of
scope」），所以這支只覆蓋 ToF A/B + Mic；Mel 的對齊邏輯由
`test_aligner.py` 的純函式單元測試覆蓋（相同的 `_resolve()` 路徑，
modality 只是換一組 merge/single 函式，不需要重複端到端驗證）。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import serial

from host.align.aligner import Aligner
from host.capture.protocol import parse_line

MOCK_DEVICE = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools" / "mock_device.py"


def _collect_via_mock_device(extra_args, duration_s):
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_DEVICE), "--seed", "1", *extra_args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ser = None
    try:
        port_path = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                continue
            m = re.search(r"pty ready: (\S+)", line)
            if m:
                port_path = m.group(1)
                break
        assert port_path, "mock_device 逾時沒印出 pty 路徑"

        ser = serial.Serial(port_path, timeout=0.5)
        aligner = Aligner()
        end_at = time.monotonic() + duration_s
        n_events = 0
        while time.monotonic() < end_at:
            raw = ser.readline()
            if not raw:
                continue
            event = parse_line(raw)
            if event is not None:
                aligner.push_event(event)
                n_events += 1
        return aligner, n_events
    finally:
        if ser is not None:
            ser.close()
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_aligner_end_to_end_with_mock_device_normal_scenario():
    aligner, n_events = _collect_via_mock_device(
        ["--fps", "30", "--mic-fps", "31.25", "--dim", "4", "--scenario", "round"],
        duration_s=4.0,
    )
    assert n_events > 50

    frames = list(aligner.frames(500_000, 3_000_000, rate_hz=30))
    assert len(frames) > 50

    present_ratio_tof = sum(f.tof_A_present for f in frames) / len(frames)
    present_ratio_mic = sum(f.mic_present for f in frames) / len(frames)
    # 正常情境下（沒有 drop fault）绝大多数幀都該有資料；留一點餘裕給邊界效應。
    assert present_ratio_tof > 0.9
    assert present_ratio_mic > 0.9

    gaps = [b.t_us - a.t_us for a, b in zip(frames, frames[1:])]
    expected_period_us = 1e6 / 30
    assert all(abs(g - expected_period_us) < 1000 for g in gaps)


def test_aligner_end_to_end_tolerates_drop_rate_without_crashing():
    """§1.4 的縮影：真實資料流會掉幀，對齊器不能因此掛掉，缺資料的幀
    只是 present=False，其餘幀照常。"""
    # 0.3 的獨立丟包率下，單一樣本被丟通常還有鄰居在 max_gap_us（100ms）內
    # 補上——這其實是期望中的行為（單幀掉幀不該被當成缺資料）。要穩定製造
    # 「連續好幾幀都掉」的空窗（現實中對應錄音 dump），丟包率要拉高很多。
    aligner, n_events = _collect_via_mock_device(
        ["--fps", "30", "--dim", "4", "--drop-rate", "0.9"],
        duration_s=4.0,
    )
    assert n_events > 5

    frames = list(aligner.frames(500_000, 3_000_000, rate_hz=30))  # 不應該 raise

    assert frames
    assert any(not f.tof_A_present for f in frames)  # 高丟包率下應該真的產生缺資料幀
