#!/usr/bin/env python3
"""A15 -- 韌體效能回歸測試：讀一段時間的即時 $ 資料行，算出
`reports/A15_perf.md` 表格要的數字，寫成一份 markdown 小報告。

歸屬：`tools/**` 一般由 T 軌擁有（見 `tools/OWNER.md`），這支是這次調度員
特別授權 A 軌（A15）新增的例外（2026-08-26 SendMessage 紀錄，比照
`compare_mel.py` 的 B 軌例外模式）。

解析邏輯直接重用 `host/capture/protocol.py`（`ProtocolParser`）與
`host/capture/dropwatch.py`（`DropTracker`），刻意不自己刻一份 -- 兩邊各
自維護一份解析器，遲早會在某個邊界案例上分岔，而分岔了也不會有任何警告。

用法：
    python3 tools/fw_regression.py --port /dev/ttyUSB0 --duration 300 \\
        --config "4x4+mel" --out reports/A15/

驗收門檻（`ssi-backlog/stories/A-firmware/A15.md`）：
    - 任一串流（tof_A / tof_B / mic / mel）掉幀率 > 1% -> 結束碼 1
    - heap 在整段測試期間下降 >= 2 KB -> 結束碼 1（需要至少收到 2 筆心跳）
    - 否則結束碼 0

已解決（原「已知限制」，`HANDOFF.md` dry-run 稽核時重新確認，2026-08-26）：
這裡曾經記錄「A15 把 $H 從 6 欄擴成 7 欄，但 `host/capture/protocol.py`
的 `_parse_heartbeat()` 仍然硬性要求 `len(parts) == 7`，導致每一行 $H
都被判成畸形行、`heap`/`bw` 只能標 N/A」——**這個限制目前已經不成立**。
`_parse_heartbeat()` 現在是 `len(parts) < 7` 起接受、且會讀 `parts[7]`
當 `bw_bytes_since_last`（實測：餵一行合成的 8 欄 `$H` 給
`parse_line()`，`heap`/`temp_c`/`bw_bytes_since_last` 全部正確解析）。
下面的 `heap_str`/`malformed_h_lines` 分支保留，是防禦性的——如果
$H 真的解析失敗（例如未來格式又變了），這裡仍然會誠實報告 N/A 而不是
猜一個假數字，但**目前正常運作下不會觸發**，不代表本腳本仍卡在那個
已修好的舊 bug 上。真正的頻寬使用率算法（序列埠層「實際收到的總
bytes / 經過秒數」，不依賴 $H 欄位）從頭到尾都沒受這個問題影響。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from host.capture.dropwatch import DropTracker, tof_stream  # noqa: E402
from host.capture.protocol import ProtocolParser  # noqa: E402

DROP_RATE_FAIL_THRESHOLD = 0.01     # A15.md: 掉幀率 > 1% -> 非零結束碼
HEAP_DROP_FAIL_BYTES = 2 * 1024     # A15.md: heap 下降 >= 2 KB -> 非零結束碼
LINK_BAUD = 460800                  # CONTRACTS.md §1.4
LINK_BYTES_PER_S = LINK_BAUD / 10.0  # 8N1: 每個 byte 佔 10 bit


def _read_lines(port: str, baud: int, duration_s: float):
    """Yield raw lines (bytes) from the serial port for `duration_s` seconds.

    Kept separate from main() so a test could substitute a fake iterable
    without touching pyserial.
    """
    import serial  # 只有真的要開序列埠才 import，讓 --help 不需要硬體環境也能跑

    with serial.Serial(port, baudrate=baud, timeout=1) as ser:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            raw = ser.readline()
            if raw:
                yield raw


def _fmt_rate(x: float | None) -> str:
    return "N/A" if x is None else f"{x * 100:.2f}%"


def run(port: str, baud: int, duration_s: float, verbose: bool = True) -> dict:
    """Feed `duration_s` seconds of serial output through the shared parsers
    and return a stats dict. Separated from main() so it's independently
    testable / reusable without argparse or sys.exit."""
    parser = ProtocolParser()
    tracker = DropTracker()

    tof_seq_stream = {"A": tof_stream("A"), "B": tof_stream("B")}
    tof_t_us: dict[str, list[int]] = {"A": [], "B": []}
    mic_frames = 0
    mel_frames = 0
    heartbeats: list[dict] = []
    status_events: list[dict] = []
    malformed_h_lines = 0
    total_bytes = 0
    lines_seen = 0

    t0 = time.monotonic()
    for raw in _read_lines(port, baud, duration_s):
        lines_seen += 1
        total_bytes += len(raw)

        event = parser.feed(raw)

        # $H 目前因為已知限制（見本檔案頂端文件）解析不出來 -- 額外偵測一下，
        # 好在報告裡明確標出來，而不是讓它悄悄消失在 malformed 計數裡。
        if event is None:
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if text.startswith("$H"):
                malformed_h_lines += 1
            continue

        et = event["type"]
        if et == "status":
            status_events.append(event)
            tracker.on_status()
        elif et == "tof":
            sensor = event["sensor"]
            tracker.observe(tof_seq_stream[sensor], event["seq"])
            tof_t_us[sensor].append(event["t_us"])
        elif et == "mic":
            mic_frames += 1
            tracker.observe("mic", event["seq"])
        elif et == "mel":
            mel_frames += 1
            tracker.observe("mel", event["seq"])
        elif et == "heartbeat":
            heartbeats.append(event)

        if verbose and lines_seen % 500 == 0:
            elapsed = time.monotonic() - t0
            print(f"  ...{elapsed:.0f}s / {duration_s:.0f}s, {lines_seen} lines", file=sys.stderr)

    elapsed = time.monotonic() - t0
    snap = tracker.snapshot()

    def interval_stddev_ms(t_us_list: list[int]) -> float | None:
        if len(t_us_list) < 3:
            return None
        deltas = [(b - a) / 1000.0 for a, b in zip(t_us_list, t_us_list[1:])]
        return statistics.pstdev(deltas)

    tof_rate_hz = {s: (len(t_us_list) / elapsed if elapsed > 0 else 0.0) for s, t_us_list in tof_t_us.items()}
    tof_interval_sigma_ms = {s: interval_stddev_ms(t_us_list) for s, t_us_list in tof_t_us.items()}

    fw_sha = status_events[-1].get("fw") if status_events else None
    mel_on = status_events[-1].get("mel") if status_events else None
    mel_hop = status_events[-1].get("mel_hop") if status_events else None

    bandwidth_actual_pct = (total_bytes / elapsed) / LINK_BYTES_PER_S * 100.0 if elapsed > 0 else None

    heap_first = heartbeats[0].get("heap") if heartbeats else None
    heap_last = heartbeats[-1].get("heap") if heartbeats else None
    heap_delta = (heap_last - heap_first) if (heap_first is not None and heap_last is not None) else None

    worst_drop_rate = 0.0
    for stream, s in snap.items():
        r = s["drop_rate_total"]
        if r is not None and r > worst_drop_rate:
            worst_drop_rate = r

    return {
        "elapsed_s": elapsed,
        "lines_seen": lines_seen,
        "total_bytes": total_bytes,
        "bandwidth_actual_pct": bandwidth_actual_pct,
        "fw_sha": fw_sha,
        "mel_on": mel_on,
        "mel_hop": mel_hop,
        "tof_rate_hz": tof_rate_hz,
        "tof_interval_sigma_ms": tof_interval_sigma_ms,
        "mic_frames": mic_frames,
        "mel_frames": mel_frames,
        "drop_snapshot": snap,
        "worst_drop_rate": worst_drop_rate,
        "heap_first": heap_first,
        "heap_last": heap_last,
        "heap_delta": heap_delta,
        "heartbeat_count": len(heartbeats),
        "malformed_h_lines": malformed_h_lines,
        "parser_stats": parser.stats.as_dict(),
    }


def write_report(stats: dict, config_label: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = stats["fw_sha"] or "unknown-sha"
    date_tag = time.strftime("%Y%m%d-%H%M%S")
    safe_config = config_label.replace(" ", "_").replace("/", "-")
    out_path = out_dir / f"{safe_config}_{sha}_{date_tag}.md"

    heap_str = "N/A（心跳筆數不足，至少需要 2 筆才能算差值）" if stats["heap_delta"] is None else f"{stats['heap_delta']} bytes"
    bw_str = "N/A" if stats["bandwidth_actual_pct"] is None else f"{stats['bandwidth_actual_pct']:.1f}%"

    lines = [
        f"# A15 回歸測試結果 -- {config_label}",
        "",
        f"- git sha（韌體自報 `$STATUS` 的 `fw=`）: `{sha}`",
        f"- 測試時長: {stats['elapsed_s']:.1f} s（收到 {stats['lines_seen']} 行, {stats['total_bytes']} bytes）",
        f"- Mel 開關（來自 `$STATUS`）: {stats['mel_on']}, mel_hop={stats['mel_hop']}",
        "",
        "| 組態 | ToF Hz | 幀間 σ (ms) | 掉幀率 | Mel Hz | 頻寬 | heap Δ |",
        "|---|---|---|---|---|---|---|",
    ]
    a_hz = stats["tof_rate_hz"].get("A", 0.0)
    b_hz = stats["tof_rate_hz"].get("B", 0.0)
    a_sigma = stats["tof_interval_sigma_ms"].get("A")
    b_sigma = stats["tof_interval_sigma_ms"].get("B")
    tof_a_drop = _fmt_rate(stats["drop_snapshot"].get(tof_stream("A"), {}).get("drop_rate_total"))
    tof_b_drop = _fmt_rate(stats["drop_snapshot"].get(tof_stream("B"), {}).get("drop_rate_total"))
    mel_hz = stats["mel_frames"] / stats["elapsed_s"] if stats["elapsed_s"] > 0 else 0.0
    a_sigma_str = "N/A" if a_sigma is None else f"{a_sigma:.2f}"
    b_sigma_str = "N/A" if b_sigma is None else f"{b_sigma:.2f}"

    lines.append(
        f"| {config_label} | A={a_hz:.2f} B={b_hz:.2f} "
        f"| A={a_sigma_str} B={b_sigma_str} "
        f"| A={tof_a_drop} B={tof_b_drop} "
        f"| {mel_hz:.2f} | {bw_str} | {heap_str} |"
    )

    lines += [
        "",
        "## 每串流掉幀明細（DropTracker，靠 seq 缺口算，不吃 $H）",
        "",
        "| 串流 | 收到 | 判定遺失 | 掉幀率 | resync 次數 |",
        "|---|---|---|---|---|",
    ]
    for stream, s in stats["drop_snapshot"].items():
        lines.append(
            f"| {stream} | {s['received']} | {s['missing']} | {_fmt_rate(s['drop_rate_total'])} | {s['resyncs']} |"
        )

    if stats["malformed_h_lines"]:
        lines += [
            "",
            f"⚠️ 收到 {stats['malformed_h_lines']} 行 `$H` 但全部解析失敗"
            "（本檔案開頭文件字串記錄的舊已知限制已經解決，這裡若還是觸發，"
            "代表格式又有新變化，需要重新檢查 `_parse_heartbeat()`；"
            "heap/心跳次數因此是 0/N/A，不代表裝置沒送）。",
        ]

    lines += [
        "",
        f"parser 狀態: {stats['parser_stats']}",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="例如 /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=LINK_BAUD)
    ap.add_argument("--duration", type=float, default=300.0, help="秒（A15.md 驗收條件用 300）")
    ap.add_argument("--config", default="unlabeled", help="這次跑的組態標籤，例如 '4x4+mel'")
    ap.add_argument("--out", required=True, help="輸出目錄，例如 reports/A15/")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    print(f"讀取 {args.port} @ {args.baud} baud，{args.duration:.0f} 秒...", file=sys.stderr)
    try:
        stats = run(args.port, args.baud, args.duration, verbose=not args.quiet)
    except Exception as exc:  # noqa: BLE001 -- CLI 工具，頂層就是要把任何失敗講清楚再退出
        print(f"讀取失敗：{exc}", file=sys.stderr)
        return 2

    out_path = write_report(stats, args.config, Path(args.out))
    print(f"報告已寫入 {out_path}", file=sys.stderr)

    ok = True
    if stats["worst_drop_rate"] > DROP_RATE_FAIL_THRESHOLD:
        print(
            f"FAIL: 掉幀率 {stats['worst_drop_rate']*100:.2f}% > "
            f"{DROP_RATE_FAIL_THRESHOLD*100:.0f}%（A15.md 驗收條件）",
            file=sys.stderr,
        )
        ok = False
    if stats["heap_delta"] is not None and -stats["heap_delta"] >= HEAP_DROP_FAIL_BYTES:
        print(
            f"FAIL: heap 下降 {-stats['heap_delta']} bytes >= {HEAP_DROP_FAIL_BYTES} bytes（A15.md 驗收條件）",
            file=sys.stderr,
        )
        ok = False
    elif stats["heap_delta"] is None:
        print("heap Δ 無法判定（收到的心跳筆數不足 2 筆），這條驗收條件本輪無法自動判定。", file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
