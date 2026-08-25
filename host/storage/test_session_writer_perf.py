"""B07 效能與硬中斷測試，跟 `test_session_writer.py` 分開放——這兩支比較慢
（前者跑 100 個 trial，後者要開子行程送 SIGKILL），純邏輯測試不需要付這個成本。
"""
import os
import signal
import subprocess
import sys
import textwrap
import time

import h5py
import numpy as np

from host.storage.session_writer import SessionWriter
from host.storage.test_session_writer import _sample_meta, _sample_trial_kwargs


def test_hundred_trials_under_3_seconds_and_50mb(tmp_path):
    """驗收條件：100 個 trial 的 session 寫檔 < 3 秒，檔案 < 50 MB。"""
    path = tmp_path / "session.h5"
    rng = np.random.default_rng(0)

    t0 = time.perf_counter()
    with SessionWriter(path, _sample_meta()) as w:
        for i in range(100):
            w.write_trial(i, **_sample_trial_kwargs(T=60, M=80, rng=rng))
    elapsed = time.perf_counter() - t0

    size_mb = path.stat().st_size / (1024 * 1024)
    assert elapsed < 3.0, f"寫 100 個 trial 花了 {elapsed:.2f}s"
    assert size_mb < 50.0, f"檔案 {size_mb:.1f}MB 超過 50MB"

    with h5py.File(path, "r") as f:
        trial_groups = [k for k in f.keys() if k.startswith("trial_")]
        assert len(trial_groups) == 100


_KILL_TEST_WRITER_SCRIPT = textwrap.dedent("""
    import sys
    sys.path.insert(0, {root!r})
    import numpy as np
    from host.storage.session_writer import SessionWriter
    from host.storage.test_session_writer import _sample_meta, _sample_trial_kwargs

    path = {path!r}
    rng = np.random.default_rng(0)
    with SessionWriter(path, _sample_meta()) as w:
        for i in range(10):
            w.write_trial(i, **_sample_trial_kwargs(T=20, M=25, rng=rng))
            print(f"WROTE {{i}}", flush=True)
            import time; time.sleep(0.15)
""")


def test_kill_minus_9_mid_session_leaves_earlier_trials_readable(tmp_path):
    """驗收條件：寫到一半 kill -9，已寫入的 trial 仍可用 h5py 讀出。"""
    path = tmp_path / "session.h5"
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[2]  # host/storage/this_file.py -> host -> repo root

    script_path = tmp_path / "writer_script.py"
    script_path.write_text(_KILL_TEST_WRITER_SCRIPT.format(root=str(repo_root), path=str(path)))

    proc = subprocess.Popen(
        [sys.executable, str(script_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    n_confirmed = 0
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and n_confirmed < 4:
            line = proc.stdout.readline()
            if not line:
                continue
            if line.startswith("WROTE"):
                n_confirmed += 1
        assert n_confirmed >= 4, "子行程沒能在逾時內寫完至少 4 個 trial"
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5.0)

    assert proc.returncode != 0  # 真的被 SIGKILL 弄死了，不是正常結束

    os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")
    with h5py.File(path, "r") as f:
        assert "meta" in f
        assert f["meta"].attrs["schema_version"] == 1
        written_trials = sorted(k for k in f.keys() if k.startswith("trial_"))
        assert len(written_trials) >= n_confirmed - 1  # 給一點緩衝(flush跟print順序的時間差)
        for name in written_trials:
            assert f[name]["tof_A"].shape == (20, 32)
