"""B17 端到端測試：真的跑 T04 的 `mock_device.py`（唯讀），透過 B01 的
`protocol.py` + 一個真正的 `TrialStateMachine`（`B11`/`B13`，只 import）
把資料寫成一個真實的 HDF5 session，再用這裡的 `read_session_events()`／
`ReplayController` 重播它——驗證的是「真正由裝置資料產生的 session 檔」
能被完整重播，不是只測合成的 fixture。

跟 `B13` 的 mock_device 測試共用同一套「餵 mock_device 資料進真正的
TrialStateMachine + AutoVadTrigger」手法，因為那正是產生一個真實 session
檔最省事、最真實的辦法。
"""
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import serial

from host.align.aligner import Aligner
from host.capture.protocol import parse_line
from host.replay.session_replay import ReplayController, read_session_events
from host.storage.session_writer import SessionWriter
from host.storage.test_session_writer import _sample_meta
from host.trial.state_machine import TrialStateMachine
from host.trigger.auto_vad_trigger import AutoVadTrigger, TriggerConfig

MOCK_DEVICE = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools" / "mock_device.py"
MOCK_NOISE_MU = 300.0
MOCK_NOISE_SIGMA = 30.0


def _full_sample_meta(**overrides):
    meta = _sample_meta(**overrides)
    meta.setdefault("clock_drift_us", 12.0)
    meta.setdefault("clock_drift_ppm", 23.4)
    meta.setdefault("clock_sync_span_us", 5_000_000)
    meta.setdefault("clock_sync_confirmed", True)
    meta.setdefault("session_start_device_us", 0)
    meta.setdefault("session_start_host_us", 0.0)
    meta.setdefault("session_start_rtt_min_us", 800.0)
    return meta


def _record_real_session_via_mock_device(h5_path, manifest_path, manifest_root, duration_s=5.0):
    writer = SessionWriter(h5_path, _full_sample_meta(wear_id=1, mode="quiz"))
    writer.__enter__()
    aligner = Aligner()
    sm = TrialStateMachine(
        ["五"], aligner, writer, h5_path, manifest_path,
        wear_id=1, mode="quiz", seed=1, manifest_root=manifest_root,
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
        end_at = time.monotonic() + duration_s
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


def test_replay_a_session_actually_recorded_from_mock_device(tmp_path):
    session_dir = Path(tempfile.mkdtemp(dir=tmp_path))
    h5_path = session_dir / "session.h5"
    manifest_path = session_dir / "manifest.csv"

    _record_real_session_via_mock_device(h5_path, manifest_path, session_dir, duration_s=5.0)

    events = read_session_events(h5_path)
    assert events
    assert any(e.payload["type"] == "tof" for e in events)
    assert any(e.payload["type"] == "trial" for e in events)

    ctrl = ReplayController(events)
    replayed = []
    deadline = time.monotonic() + 15.0  # 上限：即使照原速重播，5 秒的錄音不該花超過這個
    while not ctrl.finished and time.monotonic() < deadline:
        replayed.extend(ctrl.poll())
        time.sleep(0.005)

    assert ctrl.finished, "回放沒有在合理時間內播完"
    assert replayed
    assert all(e["replay"] is True for e in replayed)
    # 原始事件跟重播出來的事件數量要一致——回放不能漏事件也不能重複。
    assert len(replayed) == len(events)
