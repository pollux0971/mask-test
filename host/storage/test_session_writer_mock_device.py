"""B07 端到端測試：真的跑 T04 的 `mock_device.py`（唯讀），用 B01 的
`host/capture/protocol.py`（唯讀，只 import）解析真實協定行，組成一個
trial 的原始陣列，交給 `SessionWriter` 寫成 HDF5，再讀回驗證。

這裡刻意不經過 `host/align/aligner.py`（B06）——schema 的 `tof_*`/`mic_*`
本來就是各模態自己原生取樣率的長度（`T` 跟 `M` 不同），`SessionWriter`
要寫的就是這種未經對齊、原始的每模態陣列；B06 的對齊幀是給
`analysis/`（D 軌）算特徵用的，不是 HDF5 儲存格式。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import h5py
import numpy as np
import serial

from host.capture.protocol import parse_line
from host.storage.session_writer import SessionWriter
from host.storage.test_session_writer import _sample_meta

MOCK_DEVICE = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools" / "mock_device.py"


def _collect_one_trial_from_mock_device(duration_s=2.0):
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_DEVICE), "--seed", "1", "--fps", "30",
         "--mic-fps", "31.25", "--dim", "4", "--scenario", "round"],
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
        tof = {"A": [], "B": []}
        mic = []
        end_at = time.monotonic() + duration_s
        while time.monotonic() < end_at:
            raw = ser.readline()
            if not raw:
                continue
            event = parse_line(raw)
            if event is None:
                continue
            if event["type"] == "tof":
                tof[event["sensor"]].append(event)
            elif event["type"] == "mic":
                mic.append(event)
        return tof, mic
    finally:
        if ser is not None:
            ser.close()
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def _events_to_tof_arrays(events):
    t_us = np.array([e["t_us"] for e in events], dtype=np.int64)
    values = np.array([e["distance"] + e["signal"] for e in events], dtype=object)
    values = np.where(values == None, np.nan, values).astype(np.float32)  # noqa: E711
    valid = np.array([e["valid"] for e in events], dtype=bool)
    return values, valid, t_us


def test_session_writer_end_to_end_with_mock_device():
    tof, mic = _collect_one_trial_from_mock_device(duration_s=2.0)
    assert len(tof["A"]) > 10
    assert len(tof["B"]) > 10
    assert len(mic) > 10

    tof_A, tof_valid_A, tof_t_us_A = _events_to_tof_arrays(tof["A"])
    tof_B, tof_valid_B, tof_t_us_B = _events_to_tof_arrays(tof["B"])
    # 兩顆感測器各自的 t_us 序列理論上該幾乎一樣長；schema 只有一份
    # tof_t_us，用感測器 A 的（跟 B06 的設計決定一致：以 ToF 為時基）。
    n = min(len(tof_t_us_A), len(tof_t_us_B))
    tof_A, tof_valid_A, tof_t_us = tof_A[:n], tof_valid_A[:n], tof_t_us_A[:n]
    tof_B, tof_valid_B = tof_B[:n], tof_valid_B[:n]

    mic_rms = np.array([e["rms"] for e in mic], dtype=np.float32)
    mic_peak = np.array([e["peak"] for e in mic], dtype=np.int16)
    mic_t_us = np.array([e["t_us"] for e in mic], dtype=np.int64)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/session.h5"
        with SessionWriter(path, _sample_meta()) as w:
            w.write_trial(
                0, label="五", tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
                tof_valid_A=tof_valid_A, tof_valid_B=tof_valid_B,
                mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
                wear_id=1, mode="quiz", valid_zone_ratio=float(tof_valid_A.mean()),
                drop_count=0, vad_start_us=0, vad_end_us=int(mic_t_us[-1]),
                lip_onset_us=0, voice_onset_us=0, quality="ok",
            )

        with h5py.File(path, "r") as f:
            trial = f["trial_000"]
            assert trial["tof_A"].shape == (n, 32)
            assert trial["tof_valid_A"].shape == (n, 16)
            assert trial["mic_rms"].shape == (len(mic),)
            # mock_device 的 round 情境在正常運作下不太會產生無效 zone，
            # 但只要有值，NaN 就該對應 valid=False，絕不會是 -1。
            invalid_positions = ~trial["tof_valid_A"][:]
            values = trial["tof_A"][:]
            if invalid_positions.any():
                zone_idx = np.argwhere(invalid_positions)
                for t, z in zone_idx:
                    assert np.isnan(values[t, z])
                    assert np.isnan(values[t, z + 16])
