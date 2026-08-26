#!/usr/bin/env python3
"""B08 -- 從一個目錄底下所有 `.h5` session 檔完整重建 manifest.csv。

manifest 是衍生資料（CONTRACTS.md §2.2），這支就是「壞了就重建」的那個
重建：不需要小心維護增量一致性，直接掃全部 `.h5` 重新算一次。

用法：
    python3 -m host.storage.rebuild_manifest data/sessions --out data/manifest.csv
    python3 -m host.storage.rebuild_manifest data/sessions --out data/manifest.csv --include-rejected
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from host.storage.manifest import rebuild_manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions_dir", help="遞迴搜尋 .h5 檔的根目錄")
    ap.add_argument("--out", required=True, help="輸出的 manifest.csv 路徑")
    ap.add_argument(
        "--include-rejected", action="store_true",
        help="連 quality=rejected 的 trial 也列入（預設排除；資料仍在 HDF5，"
             "manifest 只是索引表）。D12 想知道「這次戴的時候錄壞了幾次」時用。",
    )
    args = ap.parse_args()

    df = rebuild_manifest(args.sessions_dir, args.out, include_rejected=args.include_rejected)
    print(f"重建完成：{len(df)} 筆 trial，寫入 {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
