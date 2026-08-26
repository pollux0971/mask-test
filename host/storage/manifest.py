"""B08 -- manifest.csv 產生與維護（CONTRACTS.md §2.2，FROZEN 2026-08-26）。

manifest 是**衍生資料**：所有真相都在各個 session 的 HDF5 檔案裡
（`host/storage/session_writer.py`，B07），manifest.csv 只是一張跨 session
的索引表，讓分析端不用逐一開檔就能 `df.query(...)` 篩 trial。

正因為是衍生資料，這個模組刻意只有兩條寫入路徑，而且**兩條路徑必須產生
逐位元組相同的輸出**（見 `test_manifest.py` 的
`test_incremental_matches_rebuild`）：

* `add_session()` -- 一個 session 寫完後，增量把它的 trial 併入既有 manifest。
* `rebuild_manifest()` -- 從一個目錄底下所有的 `.h5` 完整重建，救回遺失或
  懷疑跟 HDF5 不同步的 manifest。

兩者共用同一個「從一份 HDF5 讀出 trial 列」的函式（`_session_rows()`），
也共用同一個排序與欄位型別（`_finalize()`），差別只在「要不要保留其他
session 的既有列」。這個共用結構本身就是那份一致性保證的來源，不是額外
測試碰運氣測出來的巧合。
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

import h5py
import pandas as pd

# CONTRACTS.md §2.2，順序即輸出欄位順序。
MANIFEST_COLUMNS = [
    "session_path", "trial_idx", "label", "wear_id", "mode", "quality",
    "n_frames", "valid_zone_ratio", "drop_count", "session_date",
]

# pandas 讀 CSV 天生沒有型別資訊，讀回來後一律轉成這些型別，
# 確保 `rebuild` 產出的 DataFrame 跟從既有 manifest.csv 讀回來的 DataFrame
# 型別一致（尤其是整數欄位不會因為讀 CSV 時混進 NaN 而被 pandas 升成 float64）。
_DTYPES = {
    "session_path": "string",
    "trial_idx": "int64",
    "label": "string",
    "wear_id": "int64",
    "mode": "string",
    "quality": "string",
    "n_frames": "int64",
    "valid_zone_ratio": "float64",
    "drop_count": "int64",
    "session_date": "string",
}


def _session_path_str(h5_path, root: Path | None) -> str:
    """manifest 裡的 `session_path`：若給了 `root`，存相對路徑（可攜、換機器
    也能用）；沒給就存呼叫端傳進來的字串原樣（呼叫端自己決定慣例）。

    `add_session()` 與 `rebuild_manifest()` 必須用**同一個** `root`
    呼叫，兩條路徑產生的 `session_path` 才會是同一個字串——這正是
    `test_incremental_matches_rebuild` 在釘的東西。
    """
    p = Path(h5_path)
    if root is not None:
        p = p.resolve().relative_to(Path(root).resolve())
    return str(p)


def _session_rows(h5_path, root: Path | None) -> list[dict]:
    """讀一個 session 的 HDF5，回傳每個 trial 一列的 dict list。

    `n_frames` 這個欄位 CONTRACTS.md §2.2 只給了名字，沒定義是哪個模態的
    幀數（ToF 是 `T`、麥克風是 `M`，兩者本來就不同，見 `session_writer.py`
    的文件字串）。這裡取 `tof_A` 的長度（`T`）：ToF 是驅動 trial 切分與
    `valid_zone_ratio`/`quality` 判斷的主模態，`n_frames` 跟著它定義最一致。
    這是本 story 的判斷，不是 CONTRACTS 明文——已經在完成回報裡提醒調度員
    考慮把它寫進 CONTRACTS.md，避免以後有人假設成 `M`。
    """
    session_path = _session_path_str(h5_path, root)
    rows: list[dict] = []
    with h5py.File(h5_path, "r") as f:
        session_date = f["meta"].attrs["session_date"]
        trial_names = sorted(k for k in f.keys() if k.startswith("trial_"))
        for name in trial_names:
            grp = f[name]
            rows.append({
                "session_path": session_path,
                "trial_idx": int(grp.attrs["trial_idx"]),
                "label": str(grp.attrs["label"]),
                "wear_id": int(grp.attrs["wear_id"]),
                "mode": str(grp.attrs["mode"]),
                "quality": str(grp.attrs["quality"]),
                "n_frames": int(grp["tof_A"].shape[0]),
                "valid_zone_ratio": float(grp.attrs["valid_zone_ratio"]),
                "drop_count": int(grp.attrs["drop_count"]),
                "session_date": str(session_date),
            })
    return rows


def _filter_quality(rows: list[dict], include_rejected: bool) -> list[dict]:
    """C14：manifest 預設把 `quality="rejected"` 的 trial 排除在外（保留在
    HDF5，只是不進這張索引表），因為 manifest 的用途就是「能拿去分析的
    trial 清單」，而 `rejected` 明確定義為「棄用」。

    `low` 刻意不排除（`D08` 的判斷，`esp-mask-test-59`）：quality 標籤反映
    擷取當下的訊號完整度，不等於這筆樣本對辨識訓練沒有幫助，過濾掉會讓
    `D12` 的 CV 實驗少看到一整類真實資料。

    `include_rejected=True` 拿回完整資料，給 `D12` 需要知道「這次戴的時候
    錄壞了幾次」用。`add_session()`／`rebuild_manifest()` 都經過這裡，
    是兩者輸出一致這個保證延伸到「過濾」這一層的地方。
    """
    if include_rejected:
        return rows
    return [r for r in rows if r["quality"] != "rejected"]


def _finalize(rows: list[dict]) -> pd.DataFrame:
    """把一堆 row dict 變成排序、型別固定的 DataFrame -- `add_session()` 跟
    `rebuild_manifest()` 的最後一步都經過這裡，是兩者輸出一致的關鍵。"""
    df = pd.DataFrame(rows, columns=MANIFEST_COLUMNS)
    df = df.astype(_DTYPES)
    df = df.sort_values(["session_path", "trial_idx"], kind="stable").reset_index(drop=True)
    return df


def read_manifest(manifest_path) -> pd.DataFrame:
    """讀既有 manifest；檔案不存在就回一份空的（型別仍然正確，方便直接
    `pd.concat` 而不用另外判斷 None）。"""
    path = Path(manifest_path)
    if not path.exists():
        return pd.DataFrame(columns=MANIFEST_COLUMNS).astype(_DTYPES)
    df = pd.read_csv(path)
    return df.astype(_DTYPES)


def write_manifest(df: pd.DataFrame, manifest_path) -> None:
    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def add_session(h5_path, manifest_path, root: Path | None = None, include_rejected: bool = False) -> pd.DataFrame:
    """一個 session 寫完後呼叫：把它的 trial 併入既有 manifest 並存檔。

    對同一個 `session_path` 重複呼叫是安全的（例如那個 session 被重錄過，
    或 `C14` 把某個 trial 事後標成 `rejected` 而重新呼叫）：先把舊有同名
    session 的列全部丟掉，再放進新算出來的列，不會產生重複或殘留舊資料的
    列。回傳更新後的完整 DataFrame，方便呼叫端立刻用。

    `include_rejected` 見 `_filter_quality()`——只影響**這次新讀進來**的列，
    `existing`（其他 session 先前寫入的列）維持它們自己當初寫入時的樣子，
    不會被這次呼叫的參數回頭改變。
    """
    existing = read_manifest(manifest_path)
    session_path = _session_path_str(h5_path, root)
    existing = existing[existing["session_path"] != session_path]

    new_rows = _filter_quality(_session_rows(h5_path, root), include_rejected)
    merged = pd.concat([existing, pd.DataFrame(new_rows, columns=MANIFEST_COLUMNS)], ignore_index=True)
    merged = _finalize(merged.to_dict("records"))
    write_manifest(merged, manifest_path)
    return merged


def rebuild_manifest(sessions_dir, manifest_path, root: Path | None = None, include_rejected: bool = False) -> pd.DataFrame:
    """從 `sessions_dir` 底下所有 `.h5` 完整重建 manifest（遞迴搜尋）。

    manifest 是衍生資料的設計决定就是為了這個函式存在：manifest.csv 壞了、
    掉了、或懷疑跟磁碟上的 HDF5 不同步，直接重建，不需要小心維護一致性。

    單一 `.h5` 檔案損毀（`h5py` 打不開、缺必要的 attrs）不會讓整次重建失敗
    --這是掃磁碟這種系統邊界輸入該有的容錯，錯誤與被跳過的檔案會印出來，
    不會靜默吞掉。

    `include_rejected` 見 `_filter_quality()`——跟 `add_session()` 用同一個
    參數名、同一個預設值、同一個過濾函式，這是
    `test_incremental_matches_rebuild` 在釘的一致性保證的一部分。
    """
    sessions_dir = Path(sessions_dir)
    h5_paths = sorted(glob.glob(os.path.join(str(sessions_dir), "**", "*.h5"), recursive=True))

    all_rows: list[dict] = []
    for h5_path in h5_paths:
        try:
            rows = _session_rows(h5_path, root if root is not None else sessions_dir)
        except (OSError, KeyError) as exc:
            print(f"警告：略過無法讀取的 session 檔案 {h5_path}: {exc}")
            continue
        all_rows.extend(_filter_quality(rows, include_rejected))

    df = _finalize(all_rows)
    write_manifest(df, manifest_path)
    return df
