"""B10 端到端測試：真的跑 T04 的 `mock_device.py`（唯讀），用 B01 的
`host/capture/protocol.py`（唯讀，只 import）解析真實協定行，收集一段
「靜止」資料組成 baseline 陣列，交給 `capture_baseline_trial()` 驗證
正常路徑能正確算出 μ/σ 並寫進 HDF5。

只跑幾秒（不是真的 30 秒）—— story 的 30 秒是產品需求（前端倒數
UI 的事，C11 範圍），這支只驗證「資料量足夠時，管線本身算得對、
寫得對」，不需要真的等 30 秒才能確認邏輯正確。
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
from host.storage.baseline import capture_baseline_trial
from host.storage.test_session_writer import _sample_meta

MOCK_DEVICE = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools" / "mock_device.py"


def _collect_from_mock_device(duration_s):
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_DEVICE), "--seed", "1", "--fps", "30",
         "--mic-fps", "31.25", "--dim", "4", "--scenario", "idle"],
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


def test_capture_baseline_trial_end_to_end_with_mock_device(tmp_path):
    tof, mic = _collect_from_mock_device(duration_s=3.0)
    assert len(tof["A"]) > 30
    assert len(tof["B"]) > 30
    assert len(mic) > 30

    tof_A, valid_A, t_us_A = _events_to_tof_arrays(tof["A"])
    tof_B, valid_B, t_us_B = _events_to_tof_arrays(tof["B"])
    n = min(len(t_us_A), len(t_us_B))
    tof_A, valid_A, tof_t_us = tof_A[:n], valid_A[:n], t_us_A[:n]
    tof_B, valid_B = tof_B[:n], valid_B[:n]

    mic_rms = np.array([e["rms"] for e in mic], dtype=np.float32)
    mic_peak = np.array([e["peak"] for e in mic], dtype=np.int16)
    mic_t_us = np.array([e["t_us"] for e in mic], dtype=np.int64)

    meta_base = _sample_meta()
    for k in ("baseline_mu_A", "baseline_sigma_A", "baseline_mu_B",
              "baseline_sigma_B", "noise_floor_mu", "noise_floor_sigma"):
        del meta_base[k]

    path = tmp_path / "session.h5"
    outcome = capture_baseline_trial(
        path, meta_base,
        tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
        tof_valid_A=valid_A, tof_valid_B=valid_B,
        mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
        wear_id=1, mode="quiz",
    )

    # mock_device 的 idle 情境本來就模擬「沒有動作」，正常情況下應該 ok；
    # 如果環境雜訊剛好讓某個 zone 抖過 2mm 門檻也不算測試錯誤，但那種情況
    # 就直接說明白，不要讓斷言看起來像邏輯本身壞掉。
    assert outcome.ok, f"預期 idle 情境下 baseline 穩定，但回報: {outcome.reason}, quality={outcome.quality}"

    with h5py.File(path, "r") as f:
        assert f["meta"].attrs["baseline_mu_A"].shape == (32,)
        assert f["trial_000"].attrs["label"] == "_baseline"
