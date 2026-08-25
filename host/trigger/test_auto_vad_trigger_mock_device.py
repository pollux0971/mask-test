"""B13 端到端測試：真的跑 T04 的 `mock_device.py`（唯讀），用 B01 的
`host/capture/protocol.py`（唯讀，只 import）解析真實協定行，餵進一個
**真正的** `TrialStateMachine`（B11/B12，只 import）+ `AutoVadTrigger`，
驗證整條鏈路真的會自動觸發、真的把 trial 寫進 HDF5——不是靠假的狀態機
替身空轉。

`mock_device.py` 的 `MicModel` 不管 `--scenario` 是什麼，`rms` 一律是
`300 + 800*bump(t % 2.0, dur=0.4) + 雜訊`（見 T04 原始碼）：每 2 秒一次
0.4 秒寬的半正弦「說話」脈衝，中間穿插安靜的底噪——這剛好是這支測試
需要的「有真實起訖的語音」，不需要另外偽造。
"""
import re
import subprocess
import sys
import time
from pathlib import Path
import tempfile

import h5py
import pytest
import serial

from host.align.aligner import Aligner
from host.capture.protocol import parse_line
from host.storage.session_writer import SessionWriter
from host.storage.test_session_writer import _sample_meta
from host.trial.state_machine import TrialStateMachine
from host.trigger.auto_vad_trigger import AutoVadTrigger, TriggerConfig

MOCK_DEVICE = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools" / "mock_device.py"

# mock_device.py 的 MicModel：baseline rms=300、高斯雜訊 std=30（源碼常數，
# 不是猜的）——這裡拿它們當「已知的底噪統計」直接餵給 AutoVadTrigger，
# 相當於已經做過 B10 的 baseline 錄製。
MOCK_NOISE_MU = 300.0
MOCK_NOISE_SIGMA = 30.0


def _full_sample_meta(**overrides):
    """跟 `host/trial/test_state_machine.py` 的 `_full_sample_meta()` 是
    同樣的理由：`session_writer.py` 的 `REQUIRED_META_KEYS` 有時鐘欄位，
    `_sample_meta()` 已經補過，這裡再 `setdefault` 一次純粹是防禦，不影響
    任何既有值。"""
    meta = _sample_meta(**overrides)
    meta.setdefault("clock_drift_us", 12.0)
    meta.setdefault("clock_drift_ppm", 23.4)
    meta.setdefault("clock_sync_span_us", 5_000_000)
    meta.setdefault("clock_sync_confirmed", True)
    meta.setdefault("session_start_device_us", 0)
    meta.setdefault("session_start_host_us", 0.0)
    meta.setdefault("session_start_rtt_min_us", 800.0)
    return meta


def test_auto_vad_trigger_writes_a_real_trial_via_mock_device(tmp_path):
    session_dir = Path(tempfile.mkdtemp(dir=tmp_path))
    h5_path = session_dir / "session.h5"
    manifest_path = session_dir / "manifest.csv"

    writer = SessionWriter(h5_path, _full_sample_meta(wear_id=1, mode="quiz"))
    writer.__enter__()
    aligner = Aligner()
    sm = TrialStateMachine(
        ["五"], aligner, writer, h5_path, manifest_path,
        wear_id=1, mode="quiz", seed=1, manifest_root=session_dir,
    )
    trigger = AutoVadTrigger(sm, MOCK_NOISE_MU, MOCK_NOISE_SIGMA, TriggerConfig())

    proc = subprocess.Popen(
        [sys.executable, str(MOCK_DEVICE), "--seed", "1", "--fps", "30", "--dim", "4"],
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
        end_at = time.monotonic() + 5.0
        while time.monotonic() < end_at:
            raw = ser.readline()
            if not raw:
                continue
            event = parse_line(raw)
            if event is None:
                continue
            sm.push_event(event)
            if event.get("type") == "mic":
                trigger.push_mic(event["t_us"], event["rms"])
    finally:
        if ser is not None:
            ser.close()
        writer.__exit__(None, None, None)
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()

    with h5py.File(h5_path, "r") as f:
        trial_groups = sorted(k for k in f.keys() if k.startswith("trial_"))
        assert trial_groups, "5 秒內 mock_device 送出至少兩次語音脈衝，應該至少寫入一個 trial"
        for name in trial_groups:
            assert f[name].attrs["label"] == "五"
            assert f[name]["tof_A"].shape[0] > 0  # 真的有 ToF 資料被收進去
