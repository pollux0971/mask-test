#!/usr/bin/env python3
"""B08 -- 印出 manifest.csv 目前每個 label / mode 各有幾筆 trial。

資料蒐集（E05）時最常問的問題就是「還差多少」，這支就是回答它的，
不用開 pandas 手動查。

用法：
    python3 -m host.storage.manifest_summary data/manifest.csv
"""
from __future__ import annotations

import argparse
import sys

from host.storage.manifest import read_manifest


def summarize(manifest_path) -> str:
    df = read_manifest(manifest_path)
    lines = [f"總計 {len(df)} 筆 trial，{df['session_path'].nunique()} 個 session"]

    lines.append("")
    lines.append("依 label：")
    if df.empty:
        lines.append("  (無資料)")
    else:
        for label, count in df["label"].value_counts().sort_index().items():
            lines.append(f"  {label}: {count}")

    lines.append("")
    lines.append("依 mode：")
    if df.empty:
        lines.append("  (無資料)")
    else:
        for mode, count in df["mode"].value_counts().sort_index().items():
            lines.append(f"  {mode}: {count}")

    lines.append("")
    lines.append("依 quality：")
    if df.empty:
        lines.append("  (無資料)")
    else:
        for quality, count in df["quality"].value_counts().sort_index().items():
            lines.append(f"  {quality}: {count}")

    lines.append("")
    lines.append("依 label x mode（quality='ok' 才算，這是能拿去訓練/分析的量）：")
    ok = df[df["quality"] == "ok"]
    if ok.empty:
        lines.append("  (無資料)")
    else:
        pivot = ok.pivot_table(index="label", columns="mode", values="trial_idx", aggfunc="count", fill_value=0)
        lines.append(pivot.to_string())

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", help="manifest.csv 路徑")
    args = ap.parse_args()
    print(summarize(args.manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
