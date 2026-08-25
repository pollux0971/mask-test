import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

from host.storage.mel_writer import write_mel_to_trial

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "ssi-backlog" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from schema_example import build as build_schema_example  # noqa: E402


@pytest.fixture
def schema_h5(tmp_path):
    """驗收條件：特徵正確寫回 HDF5 —— 用 T02 的 schema_example.py 產生的空檔
    當作寫入目標，證明 B14 的寫入不需要修改 schema。"""
    h5_path = tmp_path / "session_example.h5"
    build_schema_example(str(h5_path))
    return h5_path


def test_write_mel_to_trial_creates_correct_dataset(schema_h5):
    mel = np.random.default_rng(0).normal(size=(37, 40)).astype(np.float32)

    write_mel_to_trial(schema_h5, trial_idx=1, mel=mel)

    with h5py.File(schema_h5, "r") as f:
        written = f["trial_001"]["mel"][:]
        assert written.shape == (37, 40)
        assert written.dtype == np.float32
        np.testing.assert_allclose(written, mel)


def test_write_mel_to_trial_overwrites_existing_placeholder(schema_h5):
    """trial_000 在 schema_example 裡已經有一個 (0, 40) 的 mel 佔位 dataset，
    寫入真實資料要能覆蓋掉它，而不是 shape 衝突報錯。"""
    mel = np.ones((5, 40), dtype=np.float32)

    write_mel_to_trial(schema_h5, trial_idx=0, mel=mel)

    with h5py.File(schema_h5, "r") as f:
        assert f["trial_000"]["mel"].shape == (5, 40)


def test_write_mel_to_trial_rejects_wrong_band_count(schema_h5):
    mel = np.ones((5, 39), dtype=np.float32)
    with pytest.raises(ValueError):
        write_mel_to_trial(schema_h5, trial_idx=0, mel=mel)


def test_write_mel_to_trial_missing_trial_raises_keyerror(schema_h5):
    mel = np.ones((5, 40), dtype=np.float32)
    with pytest.raises(KeyError):
        write_mel_to_trial(schema_h5, trial_idx=99, mel=mel)
