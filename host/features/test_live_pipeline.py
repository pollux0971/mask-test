"""host/features/live_pipeline.py 的獨立測試——不需要 bridge_server.py、
HTTP，或真裝置。

用 `ssi-backlog/tools/mock_device.py` 自己的合成資料模型（`Scenario`/
`MelModel`，就是餵給真的 `mock_device.py` 子行程那一份，這裡直接當函式庫
匯入，不需要真的開一個 pty 子行程）產生逼真的 ToF/Mel 樣本序列，推進
`Aligner`，餵給 `assemble_query_from_aligned_frames()`，再把組出來的向量
餵給一個小型 `RecognitionService`，證明從「裝置資料」到「辨識結果」這條
路整個通。

⚠️ 這是合成資料，不是真實量測——跟這個專案所有合成資料測試一樣，
這裡驗證的是「管線本身沒有斷掉、形狀對、拒識邏輯有反應」，不是「真的能
辨識人說的話」。真實資料要等 `E05`。
"""
import random
import sys
from pathlib import Path

import numpy as np
import pytest

from host.align.aligner import Aligner
from host.features.live_pipeline import (
    InsufficientFramesError,
    assemble_query_from_aligned_frames,
)

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from mock_device import MelModel, Scenario, zone_weights  # noqa: E402

DIM = 16  # 4x4, matches N_ZONES elsewhere
TOF_HZ = 30.0
MEL_HZ = 62.5
UTTERANCE_S = 2.0  # matches mock_device.PERIOD


def _synthesize_utterance(aligner, scenario_name, seed, t0_us=0):
    """Push one ~2s synthetic utterance's worth of $T(A)/$T(B)/$F samples
    into `aligner`, starting at `t0_us`. Returns the utterance's end time
    in microseconds (for chaining multiple utterances into one buffer
    without overlapping timestamps)."""
    rng = random.Random(seed)
    scenario_a = Scenario(scenario_name, DIM, rng, seed=seed)
    scenario_b = Scenario(scenario_name, DIM, rng, seed=seed + 1)
    mel_model = MelModel(rng, seed=seed)

    n_tof = int(UTTERANCE_S * TOF_HZ)
    for i in range(n_tof):
        t = i / TOF_HZ
        t_us = t0_us + int(round(t * 1e6))
        for sensor, scenario in (("A", scenario_a), ("B", scenario_b)):
            distances = [scenario.distance_mm(t, z, rng) for z in range(DIM)]
            signals = [scenario.signal(d, rng) for d in distances]
            aligner.push_tof(sensor, t_us, distances, signals, [True] * DIM)

    n_mel = int(UTTERANCE_S * MEL_HZ)
    for i in range(n_mel):
        t = i / MEL_HZ
        t_us = t0_us + int(round(t * 1e6))
        # MelModel.frame() returns the WIRE encoding (int16 = log_mel*100,
        # CONTRACTS.md #3.1) -- decode back to float log_mel before pushing,
        # same conversion protocol.py's real parser does. This is exactly
        # the bug C08 found live (a first draft used the wire scale
        # directly): decode once, here, not inside the pipeline.
        bands_wire = mel_model.frame(t, scenario_name)
        log_mel = [b / 100.0 for b in bands_wire]
        aligner.push_mel(t_us, log_mel)

    return t0_us + int(UTTERANCE_S * 1e6)


def _build_query(scenario_name, seed):
    aligner = Aligner()
    t_end_us = _synthesize_utterance(aligner, scenario_name, seed)
    frames = list(aligner.frames(0, t_end_us, rate_hz=TOF_HZ))
    mu = np.zeros(32)
    sigma = np.ones(32)  # flat baseline -- this test isn't about baseline calibration
    return assemble_query_from_aligned_frames(frames, mu, sigma, mu, sigma)


def test_assemble_query_shape():
    seq = _build_query("round", seed=1)
    assert seq.data.shape == (24, 104)  # DEFAULT_T_FIXED x CONTRACTS §3.3
    assert seq.data_raw.shape[1] == 104
    assert seq.slices == {"tof": slice(0, 64), "mel": slice(64, 104)}
    assert np.isfinite(seq.data).all()


def test_assemble_query_insufficient_frames_raises():
    aligner = Aligner()  # nothing pushed -- zero usable frames
    with pytest.raises(InsufficientFramesError):
        assemble_query_from_aligned_frames(list(aligner.frames(0, 100_000, rate_hz=30)),
                                            np.zeros(32), np.ones(32), np.zeros(32), np.ones(32))


def test_missing_modality_frames_are_dropped_not_faked():
    aligner = Aligner()
    # Only push ToF -- no mel at all -- frames() will report mel_present=False
    # for every frame, so every frame should be filtered out.
    for i in range(60):
        t_us = int(i / TOF_HZ * 1e6)
        aligner.push_tof("A", t_us, [17.0] * DIM, [140] * DIM, [True] * DIM)
        aligner.push_tof("B", t_us, [17.0] * DIM, [140] * DIM, [True] * DIM)
    frames = list(aligner.frames(0, 2_000_000, rate_hz=TOF_HZ))
    assert any(f.tof_A_present for f in frames)  # sanity: tof itself did land
    with pytest.raises(InsufficientFramesError):
        assemble_query_from_aligned_frames(frames, np.zeros(32), np.ones(32), np.zeros(32), np.ones(32))


def test_end_to_end_through_recognition_service():
    """The thing esp-mask-test-ad actually asked for: run mock-generated
    data all the way through enrollment -> recognize and confirm the pipe
    itself works. Not an accuracy claim -- see module/file docstrings."""
    from analysis.similarity.recognition_service import RecognitionService

    templates_by_class = {
        "round_word": [_build_query("round", seed=s).data for s in (10, 11, 12)],
        "spread_word": [_build_query("spread", seed=s).data for s in (20, 21, 22)],
    }
    reject_templates = [_build_query("idle", seed=s).data for s in (30, 31, 32, 33, 34)]
    slices = {"tof": slice(0, 64), "mel": slice(64, 104)}

    service = RecognitionService(
        templates_by_class, reject_templates, slices,
        subject="synthetic", wear_id=1,
    )

    query = _build_query("round", seed=99)
    tri_result, latency_ms = service.recognize(query.data)

    assert set(tri_result.classes) == {"round_word", "spread_word"}
    assert tri_result.d_tof.shape == (2,)
    assert tri_result.d_mel.shape == (2,)
    assert tri_result.d_tof_raw.shape == (2,)
    assert tri_result.d_mel_raw.shape == (2,)
    assert isinstance(tri_result.reject_tof, bool)
    assert isinstance(tri_result.reject_mel, bool)
    assert "dist" in latency_ms and "total" in latency_ms

    # The formula itself (already verified against real quiz.js behavior in
    # reports/REJECT_PATH.md) -- exercised here against a real
    # RecognitionService output, not a hand-built TriResult.
    for w in (0.0, 0.5, 1.0):
        fused = tri_result.reject_fused(w)
        assert isinstance(fused, (bool, np.bool_))
