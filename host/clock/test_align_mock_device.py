"""B04 端到端測試：真的跑 T04 的 `mock_device.py`（唯讀，不改），把它的輸出
餵給 `ClockAligner`，驗證擬合結果，以及 `--fault clock-jump` 下不崩潰且
標記異常。

跟 `test_align.py` 的純函式單元測試分開放，因為這支要開子行程、跑真實時間，
比較慢（約 15 秒），純函式邏輯不需要付這個成本就能測。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import serial

from host.clock.align import ClockAligner

MOCK_DEVICE = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools" / "mock_device.py"

# CONTRACTS.md §1.1：`$T,<A|B>,<seq>,<t_us>,...` / `$M,<seq>,<t_us>,...` / `$H,<t_us>,...`
_T_US_INDEX = {"$T": 3, "$M": 2, "$H": 1}


def _device_us_from_line(line: str):
    line = line.strip()
    for prefix, idx in _T_US_INDEX.items():
        if line.startswith(prefix + ","):
            parts = line.split(",")
            try:
                return int(parts[idx])
            except (IndexError, ValueError):
                return None
    return None


def _collect_via_mock_device(extra_args, duration_s):
    """開子行程跑 mock_device.py，像真正的 bridge_server 一樣打開它印出來的
    pty 路徑、逐行讀取，用「讀到這行的當下」當作 host_us，餵進 ClockAligner。"""
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
        aligner = ClockAligner()
        end_at = time.monotonic() + duration_s
        while time.monotonic() < end_at:
            raw = ser.readline()
            if not raw:
                continue
            host_us = time.monotonic() * 1e6
            device_us = _device_us_from_line(raw.decode("ascii", errors="replace"))
            if device_us is not None:
                aligner.add_sample(device_us, host_us)
        return aligner
    finally:
        if ser is not None:
            ser.close()
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_clock_alignment_end_to_end_with_mock_device():
    """驗收條件：對齊殘差 p95 < 5 ms；斜率落在 1 ± 200 ppm。"""
    aligner = _collect_via_mock_device(["--fps", "30", "--dim", "4"], duration_s=6.0)

    assert aligner.n_buckets >= 4
    alignment = aligner.fit()

    assert not alignment.anomaly, alignment.anomaly_reason
    slope_ppm_error = abs(alignment.slope - 1.0) * 1e6
    assert slope_ppm_error < 200.0
    assert alignment.residual_p95_us < 5000.0


def test_clock_alignment_handles_clock_jump_fault_without_crashing():
    """驗收條件：T04 的 `--fault clock-jump` 下模型不崩潰，且標記異常。"""
    aligner = _collect_via_mock_device(
        ["--fault", "clock-jump", "--clock-jump-interval", "2", "--clock-jump-max-ms", "300"],
        duration_s=8.0,
    )

    assert aligner.n_buckets >= 4
    alignment = aligner.fit()  # 不應該 raise

    assert alignment.anomaly, "clock-jump 造成的不連續應該被標記成異常，不能悄悄回一個看似正常的結果"
