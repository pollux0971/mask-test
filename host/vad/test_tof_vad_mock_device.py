"""B16 端到端：在真的 `mock_device.py` 上跑 ToF VAD 與唇動先行量測。

其中 `test_playback_control_no_lip_motion` 是 story 明訂的對照組，而且用了
一個現成的巧合：`mock_device.MicModel` **不看 `--scenario`**（我在 `B15`
回報過這件事），所以 `--scenario idle` 會產生「有聲音、沒有唇動」的串流
——正是「播放錄音給裝置聽」的樣子。這個對照組要通過，ToF 端必須完全不
觸發。（若日後 T 軌把 MicModel 接上 scenario，這個測試要改用別的方式
產生對照組——註解在此，免得那時候有人不知道為什麼會壞。）
"""
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from host.capture.protocol import ProtocolParser
from host.vad.audio_vad import detect_from_events as detect_voice_from_events
from host.vad.onset import measure_lip_lead
from host.vad.tof_vad import detect_from_events as detect_lip_from_events
from host.vad.tof_vad import detect_lip_activity, estimate_energy_floor, zone_energy

REPO = Path(__file__).resolve().parents[2]
MOCK = REPO / "ssi-backlog" / "tools" / "mock_device.py"

# `mock_device.MicModel.sample()`：rms = 300 + 800*bump + N(0, 30)。
MOCK_NOISE_MU = 300.0
MOCK_NOISE_SIGMA = 30.0
MOCK_PERIOD_S = 2.0


def run_mock(seconds, extra_args=()):
    proc = subprocess.Popen(
        [sys.executable, "-u", str(MOCK), "--fps", "30", "--dim", "4",
         "--seed", "21", *extra_args],
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

    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY)
    parser = ProtocolParser()
    events, buf = [], b""
    deadline = time.perf_counter() + seconds
    try:
        while time.perf_counter() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            buf += os.read(fd, 65536)
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                event = parser.feed(raw)
                if event is not None:
                    events.append(event)
    finally:
        os.close(fd)
        proc.terminate()
        proc.wait(timeout=5)
    return events, parser


def tof_matrix(events, sensor="A"):
    frames = [e for e in events if e["type"] == "tof" and e["sensor"] == sensor]
    rows = [[np.nan if v is None else float(v) for v in e["distance"]]
            + [np.nan if v is None else float(v) for v in e["signal"]]
            for e in frames]
    return np.asarray(rows, dtype=np.float64), [e["t_us"] for e in frames]


def record_baseline(seconds=2.5, sensor="A"):
    """模擬 `B10`：在 `--scenario idle`（唇部不動）期間錄 baseline。"""
    events, _ = run_mock(seconds, extra_args=("--scenario", "idle"))
    tof, _ = tof_matrix(events, sensor)
    assert tof.shape[0] > 30, "baseline 幀數不足"
    return np.nanmean(tof, axis=0), np.nanstd(tof, axis=0)


def test_detects_lip_motion_from_the_round_scenario():
    mu, sigma = record_baseline()
    events, parser = run_mock(5.0, extra_args=("--scenario", "round"))
    assert parser.stats.malformed == 0

    result = detect_lip_from_events(events, mu, sigma, sensor="A")
    assert result.applicable, result.reason
    assert result.detected, result.to_dict()

    # 每 2 秒一個合成「發話」，5 秒視窗抓得到 2–4 個
    assert 2 <= len(result.segments) <= 4, [s.to_dict() for s in result.segments]
    if len(result.segments) >= 2:
        gap = result.segments[1].start_us - result.segments[0].start_us
        assert abs(gap - MOCK_PERIOD_S * 1e6) < 250_000


def test_playback_control_no_lip_motion(caplog):
    """**story 明訂的對照組**：有聲音、沒有唇動 → ToF 端不可觸發。

    `--scenario idle` 的 ToF 完全靜止，但 `MicModel` 不看 scenario 所以
    麥克風照樣「說話」——正好是播放錄音給裝置聽的情境。
    """
    mu, sigma = record_baseline()
    events, _ = run_mock(5.0, extra_args=("--scenario", "idle"))

    audio = detect_voice_from_events(events, MOCK_NOISE_MU, MOCK_NOISE_SIGMA)
    assert audio.detected, "對照組前提不成立：麥克風這邊應該有聲音"

    lips = detect_lip_from_events(events, mu, sigma, sensor="A")
    assert lips.applicable, lips.reason
    assert lips.detected is False, (
        f"沒有唇動卻觸發了 ToF VAD，「唇動比較早」的結論會全部是假的："
        f"{lips.to_dict()}"
    )

    lead = measure_lip_lead(lips, audio)
    assert lead.lip_onset_us is None
    assert lead.lead_us is None
    assert lead.comparable is False


def test_both_sensors_agree_on_the_same_motion():
    """A/B 兩顆看同一張嘴。起點應該相近——差太多代表其中一顆沒對準。"""
    mu, sigma = record_baseline()
    events, _ = run_mock(5.0, extra_args=("--scenario", "round"))

    a = detect_lip_from_events(events, mu, sigma, sensor="A")
    b = detect_lip_from_events(events, mu, sigma, sensor="B")
    assert a.detected and b.detected
    # 兩顆的取樣時刻本來就錯開約半幀，容一幀多一點
    assert abs(a.primary.start_us - b.primary.start_us) < 100_000


def test_idle_energy_is_far_below_the_threshold():
    """靜止期間的能量必須遠低於進入閾值。

    ⚠️ 這裡**不能**拿理論值（半常態的 0.798）來比。理論值假設 z ~ N(0,1)，
    而 mock 的靜止距離是 17.0 mm ± 0.15 mm、輸出時四捨五入成整數 mm——
    雜訊遠小於一個量化級距，所以幾乎每一幀都是整數 17，實際變異接近 0。
    這是 mock 的保真度問題（見完成回報），不是本模組的問題：`sigma_floor`
    的量化下限讓它安全地退化成「能量接近 0」，而不是 z 爆到上萬。
    """
    mu, sigma = record_baseline()
    events, _ = run_mock(3.0, extra_args=("--scenario", "idle"))
    tof, _ = tof_matrix(events)
    energy, _, n_used = zone_energy(tof, mu, sigma)
    est_mu, est_sigma = estimate_energy_floor(energy)

    assert n_used == 16
    assert np.all(np.isfinite(energy))
    assert energy.max() < 3.0, "靜止時的能量不該接近動作的量級"
    assert est_mu < 1.0


def test_dropped_tof_frames_do_not_split_a_motion():
    mu, sigma = record_baseline()
    events, _ = run_mock(6.0, extra_args=("--scenario", "round", "--drop-rate", "0.2"))
    frames = [e for e in events if e["type"] == "tof" and e["sensor"] == "A"]
    seqs = [e["seq"] for e in frames]
    assert max(seqs) + 1 > len(seqs), "這次沒掉到幀，測試前提不成立"

    result = detect_lip_from_events(events, mu, sigma, sensor="A")
    assert result.detected
    for seg in result.segments:
        assert seg.duration_us <= 900_000, "掉幀把兩段動作黏在一起了"


def test_invalid_zones_are_tolerated():
    mu, sigma = record_baseline()
    events, _ = run_mock(4.0, extra_args=("--scenario", "round",
                                          "--invalid-zone-rate", "0.25"))
    result = detect_lip_from_events(events, mu, sigma, sensor="A")
    assert result.applicable and result.detected
    assert result.n_zones_used == 16          # zone 本身沒壞，只是個別幀無效


def test_lip_lead_pipeline_runs_end_to_end():
    """整條走一遍：同一段串流同時跑兩個 VAD，合成一筆先行量測。

    ⚠️ mock 的唇動與「發聲」共用同一個 `bump()` 包絡，**兩者在合成資料裡
    是同時發生的**，所以這裡量到的先行量沒有物理意義——這條測試驗的是
    **管線接得起來**，不是那個數字。真實的先行量必須上機量，見完成回報。
    """
    mu, sigma = record_baseline()
    events, _ = run_mock(6.0, extra_args=("--scenario", "round"))

    lips = detect_lip_from_events(events, mu, sigma, sensor="A")
    audio = detect_voice_from_events(events, MOCK_NOISE_MU, MOCK_NOISE_SIGMA)
    assert lips.detected and audio.detected

    lead = measure_lip_lead(lips, audio)
    assert lead.comparable is True, lead.reason
    assert lead.lead_us is not None
    attrs = lead.to_trial_attrs()
    assert set(attrs) == {"vad_start_us", "vad_end_us", "lip_onset_us", "voice_onset_us"}
    assert all(v is not None for v in attrs.values())
