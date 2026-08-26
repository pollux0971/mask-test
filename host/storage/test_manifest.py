"""B08 -- manifest.py 的測試。

真實寫入 HDF5（用 `session_writer.SessionWriter`，不是憑空捏造欄位），
所以這裡驗的是「manifest 從真正的 schema 讀出來對不對」，不是「manifest
自己內部邏輯自洽」。跟 `test_session_writer.py` 共用同一組 fixture 產生器
（`_sample_meta`/`_sample_trial_kwargs`），刻意不重新發明一份假資料。
"""
import time

import h5py
import numpy as np
import pandas as pd

from host.storage.manifest import add_session, read_manifest, rebuild_manifest, write_manifest
from host.storage.manifest_summary import summarize
from host.storage.session_writer import SessionWriter
from host.storage.test_session_writer import _sample_meta, _sample_trial_kwargs


def _write_session(path, *, n_trials, wear_id=3, mode="quiz", label="五", quality="ok", rng=None):
    rng = rng or np.random.default_rng(0)
    with SessionWriter(path, _sample_meta(wear_id=wear_id, mode=mode)) as w:
        for i in range(n_trials):
            kwargs = _sample_trial_kwargs(T=40 + i, M=50 + i, rng=rng)
            kwargs.update(label=label, wear_id=wear_id, mode=mode, quality=quality)
            w.write_trial(i, **kwargs)


# ---------------------------------------------------------------------------
# 基本讀取


def test_add_session_reads_correct_rows(tmp_path):
    h5_path = tmp_path / "session_001.h5"
    _write_session(h5_path, n_trials=3, wear_id=7, mode="silent", label="八", quality="low")

    manifest_path = tmp_path / "manifest.csv"
    df = add_session(h5_path, manifest_path, root=tmp_path)

    assert len(df) == 3
    assert list(df["trial_idx"]) == [0, 1, 2]
    assert (df["session_path"] == "session_001.h5").all()
    assert (df["wear_id"] == 7).all()
    assert (df["mode"] == "silent").all()
    assert (df["label"] == "八").all()
    assert (df["quality"] == "low").all()
    assert (df["session_date"] == "2026-08-26").all()
    # n_frames 取的是 tof_A 的長度 (T = 40+i)，見 manifest.py 的說明
    assert list(df["n_frames"]) == [40, 41, 42]

    # 也確認真的寫進磁碟、讀回來一樣
    on_disk = read_manifest(manifest_path)
    pd.testing.assert_frame_equal(df.reset_index(drop=True), on_disk.reset_index(drop=True))


def test_add_session_overwrite_does_not_duplicate(tmp_path):
    """同一個 session 重錄過（例如 SENS 中斷重跑），manifest 不能留下舊列。"""
    h5_path = tmp_path / "session_001.h5"
    manifest_path = tmp_path / "manifest.csv"

    _write_session(h5_path, n_trials=5)
    add_session(h5_path, manifest_path, root=tmp_path)

    _write_session(h5_path, n_trials=2)  # 覆寫，這次只錄了 2 個 trial
    df = add_session(h5_path, manifest_path, root=tmp_path)

    assert len(df) == 2, "重錄後 manifest 應該只剩新的 2 筆，不能殘留舊的 5 筆"


def test_add_session_multiple_sessions_accumulate(tmp_path):
    manifest_path = tmp_path / "manifest.csv"
    for idx in range(3):
        h5_path = tmp_path / f"session_{idx:03d}.h5"
        _write_session(h5_path, n_trials=idx + 1, wear_id=idx)
        add_session(h5_path, manifest_path, root=tmp_path)

    df = read_manifest(manifest_path)
    assert len(df) == 1 + 2 + 3
    assert df["session_path"].nunique() == 3


# ---------------------------------------------------------------------------
# C14：manifest 預設排除 rejected


def test_add_session_excludes_rejected_by_default(tmp_path):
    h5_path = tmp_path / "session_001.h5"
    _write_session(h5_path, n_trials=3, label="八", quality="rejected")
    manifest_path = tmp_path / "manifest.csv"

    df = add_session(h5_path, manifest_path, root=tmp_path)
    assert len(df) == 0, "quality=rejected 的 trial 預設不該進 manifest"

    df_all = add_session(h5_path, manifest_path, root=tmp_path, include_rejected=True)
    assert len(df_all) == 3, "include_rejected=True 應該拿回全部"


def test_add_session_keeps_low_quality_by_default(tmp_path):
    """`low` 不是 `rejected`：D08 刻意不預篩，不能被這個過濾誤傷。"""
    h5_path = tmp_path / "session_001.h5"
    _write_session(h5_path, n_trials=2, label="八", quality="low")
    manifest_path = tmp_path / "manifest.csv"

    df = add_session(h5_path, manifest_path, root=tmp_path)
    assert len(df) == 2
    assert (df["quality"] == "low").all()


def test_rebuild_excludes_rejected_by_default_and_matches_incremental(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    manifest_path = tmp_path / "manifest.csv"

    rng = np.random.default_rng(7)
    qualities = ["ok", "low", "rejected", "ok"]
    for idx, quality in enumerate(qualities):
        h5_path = sessions_dir / f"session_{idx:03d}.h5"
        _write_session(h5_path, n_trials=2, wear_id=idx, quality=quality, rng=rng)
        add_session(h5_path, manifest_path, root=sessions_dir)

    incremental_df = read_manifest(manifest_path)
    assert "rejected" not in set(incremental_df["quality"])
    assert len(incremental_df) == 3 * 2  # 4 個 session，1 個被排除

    rebuilt_path = tmp_path / "manifest_rebuilt.csv"
    rebuilt_df = rebuild_manifest(sessions_dir, rebuilt_path)
    pd.testing.assert_frame_equal(
        incremental_df.reset_index(drop=True), rebuilt_df.reset_index(drop=True)
    )
    assert manifest_path.read_text() == rebuilt_path.read_text()

    # include_rejected=True 兩條路徑也要繼續互相一致，不是只有預設路徑
    rebuilt_all_path = tmp_path / "manifest_rebuilt_all.csv"
    rebuilt_all_df = rebuild_manifest(sessions_dir, rebuilt_all_path, include_rejected=True)
    assert len(rebuilt_all_df) == 4 * 2
    assert "rejected" in set(rebuilt_all_df["quality"])


# ---------------------------------------------------------------------------
# 驗收條件：rebuild 與增量必須完全一致


def test_incremental_matches_rebuild(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    manifest_path = tmp_path / "manifest.csv"

    rng = np.random.default_rng(42)
    n_sessions = 4
    for idx in range(n_sessions):
        h5_path = sessions_dir / f"session_{idx:03d}.h5"
        _write_session(
            h5_path, n_trials=(idx % 3) + 1, wear_id=idx, mode=("quiz", "silent")[idx % 2], rng=rng,
        )
        add_session(h5_path, manifest_path, root=sessions_dir)

    incremental_df = read_manifest(manifest_path)

    rebuilt_path = tmp_path / "manifest_rebuilt.csv"
    rebuilt_df = rebuild_manifest(sessions_dir, rebuilt_path)

    pd.testing.assert_frame_equal(
        incremental_df.reset_index(drop=True), rebuilt_df.reset_index(drop=True)
    )
    assert (sessions_dir / "session_000.h5").exists()
    # 兩個 CSV 檔案本身也要逐位元組相同，不是只有記憶體內的 DataFrame 相等
    assert manifest_path.read_text() == rebuilt_path.read_text()


def test_rebuild_skips_corrupt_file_without_crashing(tmp_path, capsys):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    _write_session(sessions_dir / "good.h5", n_trials=2)
    (sessions_dir / "corrupt.h5").write_bytes(b"not a real hdf5 file")

    manifest_path = tmp_path / "manifest.csv"
    df = rebuild_manifest(sessions_dir, manifest_path)

    assert len(df) == 2, "壞掉的檔案應該被跳過，不影響好檔案的重建結果"
    assert "corrupt.h5" in capsys.readouterr().out


def test_rebuild_recurses_into_subdirectories(tmp_path):
    sessions_dir = tmp_path / "sessions"
    (sessions_dir / "2026-08-26").mkdir(parents=True)
    _write_session(sessions_dir / "2026-08-26" / "session_001.h5", n_trials=2)

    df = rebuild_manifest(sessions_dir, tmp_path / "manifest.csv")
    assert len(df) == 2
    assert df["session_path"].iloc[0] == str(
        (__import__("pathlib").Path("2026-08-26") / "session_001.h5")
    )


# ---------------------------------------------------------------------------
# manifest_summary.py


def test_summary_counts_by_label_mode_quality(tmp_path):
    manifest_path = tmp_path / "manifest.csv"
    for i, (label, mode, quality) in enumerate([
        ("五", "quiz", "ok"), ("五", "quiz", "ok"), ("五", "silent", "low"),
        ("四", "quiz", "ok"), ("四", "quiz", "rejected"),
    ]):
        h5_path = tmp_path / f"s{i}.h5"
        _write_session(h5_path, n_trials=1, label=label, mode=mode, quality=quality)
        # C14: manifest 預設排除 rejected -- 這個測試要看到全部（包含
        # rejected）才能驗證 summarize() 的「依 quality」欄位，所以顯式要
        # 「全部給我」。預設行為本身由下面 test_add_session_excludes_rejected_by_default 驗。
        add_session(h5_path, manifest_path, root=tmp_path, include_rejected=True)

    text = summarize(manifest_path)
    assert "總計 5 筆 trial" in text
    assert "五: 3" in text
    assert "四: 2" in text
    assert "quiz: 4" in text
    assert "silent: 1" in text
    assert "ok: 3" in text
    assert "low: 1" in text
    assert "rejected: 1" in text


def test_summary_on_missing_manifest_does_not_crash(tmp_path):
    text = summarize(tmp_path / "does_not_exist.csv")
    assert "總計 0 筆 trial" in text


# ---------------------------------------------------------------------------
# 驗收條件：10 個 session、1000 個 trial 讀取 < 200 ms


def test_manifest_read_performance(tmp_path):
    """驗收條件原文是「10 個 session、1000 個 trial 的 manifest 讀取 <
    200ms」。真的去寫 1000 個 trial 的 HDF5（哪怕每個都很小）在 CI 上會慢到
    不成比例，且這條驗收條件量的是 `pd.read_csv` 本身的速度，不是 HDF5
    寫入速度（那是 B07 `test_session_writer_perf.py` 的驗收範圍）。所以這裡
    直接組出等價的 1000 列 manifest 內容寫進 CSV，量測讀取。"""
    rows = []
    for session_idx in range(10):
        for trial_idx in range(100):
            rows.append({
                "session_path": f"session_{session_idx:03d}.h5",
                "trial_idx": trial_idx,
                "label": "五",
                "wear_id": session_idx,
                "mode": "quiz",
                "quality": "ok",
                "n_frames": 60,
                "valid_zone_ratio": 0.9,
                "drop_count": 0,
                "session_date": "2026-08-26",
            })
    df = pd.DataFrame(rows)
    manifest_path = tmp_path / "manifest.csv"
    write_manifest(df, manifest_path)
    assert len(df) == 1000

    t0 = time.perf_counter()
    read_manifest(manifest_path)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 200, f"讀取花了 {elapsed_ms:.1f} ms，超過 200 ms 驗收門檻"


# ---------------------------------------------------------------------------
# 邊界：空 manifest


def test_read_manifest_missing_file_returns_empty_typed_frame(tmp_path):
    df = read_manifest(tmp_path / "nope.csv")
    assert len(df) == 0
    assert list(df.columns) == [
        "session_path", "trial_idx", "label", "wear_id", "mode", "quality",
        "n_frames", "valid_zone_ratio", "drop_count", "session_date",
    ]


def test_multi_target_h5_dataset_open_close_does_not_leak_handles(tmp_path):
    """`_session_rows` 用 `with h5py.File(...)` -- 確認讀完之後檔案真的關了
    （Windows 上沒關的話後面同一個路徑會鎖住；這裡至少確認可以在讀完後立刻
    重新打開來寫，等於一個很便宜的迴歸測試）。"""
    h5_path = tmp_path / "session.h5"
    _write_session(h5_path, n_trials=1)
    add_session(h5_path, tmp_path / "manifest.csv", root=tmp_path)
    with h5py.File(h5_path, "a"):
        pass
