"""B15 端到端：在真的 `mock_device.py` 的 `$M` 串流上跑音訊 VAD。

`test_audio_vad.py` 用合成軌跡驗演算法；這裡驗的是「接上一個真的會吐
`$M` 的裝置時，從 `ProtocolParser` 到 `VadResult` 這條路走得通」，包括
pty、真實時間、以及 `$T`/`$F` 夾雜在同一條 UART 上。
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
from host.storage.baseline import compute_noise_floor
from host.vad.audio_vad import detect_from_events, detect_voice_activity

REPO = Path(__file__).resolve().parents[2]
MOCK = REPO / "ssi-backlog" / "tools" / "mock_device.py"

# `mock_device.MicModel.sample()`：rms = 300 + 800*bump(t % 2.0) + N(0, 30)。
# 底噪就是那個常數項與雜訊項；每 2 秒有一個 0.4 秒的「詞」，峰值約 26σ。
MOCK_NOISE_MU = 300.0
MOCK_NOISE_SIGMA = 30.0
MOCK_WORD_PERIOD_S = 2.0
MOCK_WORD_DURATION_S = 0.4


def collect_events(seconds, extra_args=()):
    proc = subprocess.Popen(
        [sys.executable, "-u", str(MOCK), "--fps", "10", "--dim", "4",
         "--seed", "11", *extra_args],
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


def test_detects_the_mock_utterances():
    events, parser = collect_events(5.0)
    assert parser.stats.malformed == 0

    result = detect_from_events(events, MOCK_NOISE_MU, MOCK_NOISE_SIGMA)
    assert result.applicable and result.detected

    # 每 2 秒一個詞，5 秒視窗至少抓得到 2 個（頭尾可能被視窗切掉）
    assert 2 <= len(result.segments) <= 4, [s.to_dict() for s in result.segments]

    for seg in result.segments[1:-1] or result.segments:
        # 詞長 0.4 秒；$M 幀距 32 ms，兩端各容一幀的量化誤差
        assert 300_000 <= seg.duration_us <= 500_000, seg.to_dict()

    if len(result.segments) >= 2:
        gap = result.segments[1].start_us - result.segments[0].start_us
        assert abs(gap - MOCK_WORD_PERIOD_S * 1e6) < 150_000

    assert result.confidence == pytest.approx(1.0)      # 26σ，遠超進入閾值


def test_noise_floor_from_b10_matches_the_mock_model():
    """驗收條件 3 的端到端版：閾值真的是從 `B10` 的 `compute_noise_floor()`
    算出來的，不是寫死的。"""
    events, _ = collect_events(4.0)
    rms = np.array([e["rms"] for e in events if e["type"] == "mic"], dtype=float)
    assert rms.size > 50

    # 詞只佔 20% 的工作週期，但 mean/std 仍會被拉高——所以這裡取安靜段
    # （低於中位數的那一半）來估底噪，模擬 B10 在受試者靜止時錄 baseline。
    quiet = rms[rms <= np.median(rms)]
    mu, sigma = compute_noise_floor(quiet)
    assert abs(mu - MOCK_NOISE_MU) < 3 * MOCK_NOISE_SIGMA
    assert 0 < sigma < 3 * MOCK_NOISE_SIGMA

    result = detect_from_events(events, mu, sigma)
    assert result.applicable and result.detected
    assert result.enter_threshold == pytest.approx(mu + 3 * sigma)


def test_mel_and_tof_lines_do_not_disturb_the_audio_vad():
    """同一條 UART 上還有 `$T` 與 `$F`。VAD 只吃 `$M`，其餘要被忽略。"""
    events, _ = collect_events(4.0, extra_args=("--mel", "1"))
    kinds = {e["type"] for e in events}
    assert {"tof", "mic", "mel"} <= kinds

    result = detect_from_events(events, MOCK_NOISE_MU, MOCK_NOISE_SIGMA)
    assert result.detected
    n_mic = sum(1 for e in events if e["type"] == "mic")
    assert result.n_frames == n_mic          # 沒有把 $F/$T 混進來


def test_v1_stream_is_refused_not_silently_wrong():
    """v1 的 `$M` 沒有 `t_us`。用幀索引冒充時間會給出看似合理、實則全錯的
    邊界——那比直接說「做不了」危險得多。"""
    proc = subprocess.Popen(
        [sys.executable, "-u", str(MOCK), "--proto", "v1", "--fps", "10", "--seed", "11"],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    path = None
    for _ in range(50):
        line = proc.stdout.readline()
        match = re.search(r"(/dev/pts/\d+)", line or "")
        if match:
            path = match.group(1)
            break
    if path is None:
        proc.kill()
        pytest.skip("mock_device 沒有印出 pty 路徑")

    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY)
    parser = ProtocolParser(allow_v1=True)
    events, buf = [], b""
    deadline = time.perf_counter() + 2.0
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

    assert any(e["type"] == "mic" for e in events)
    result = detect_from_events(events, MOCK_NOISE_MU, MOCK_NOISE_SIGMA)
    assert result.applicable is False
    assert "v1" in result.reason


def test_dropped_mic_frames_do_not_split_a_word():
    """`--drop-rate` 讓 `$M` 掉幀。掛延遲用時間算，掉幀只會讓判斷變保守。"""
    events, _ = collect_events(6.0, extra_args=("--drop-rate", "0.2"))
    mic = [e for e in events if e["type"] == "mic"]
    seqs = [e["seq"] for e in mic]
    assert max(seqs) + 1 > len(seqs), "這次沒掉到幀，測試前提不成立"

    result = detect_voice_activity(
        [e["rms"] for e in mic], [e["t_us"] for e in mic],
        MOCK_NOISE_MU, MOCK_NOISE_SIGMA,
    )
    assert result.detected
    for seg in result.segments:
        assert seg.duration_us <= 700_000, "掉幀把兩個詞黏成一段了"
