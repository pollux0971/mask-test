#!/usr/bin/env python3
"""
Local bridge for the VL53L7CX x2 + bone-conduction mic live monitor panel.

Reads the '$TOF,...' / '$MIC,...' / '$STATUS,...' / '$REC,...' lines the
firmware prints over the console UART, re-broadcasts them as Server-Sent
Events to any browser tab with panel.html open, and serves the panel itself
over plain HTTP.

Two host-triggered actions:
  - POST /switch?res=4|8   edits TOF_RESOLUTION_MODE in main/vl53l7cx_test.c,
    rebuilds, reflashes, and resumes streaming -- the ULD driver's grid size
    isn't runtime-switchable, so a resolution change is a reflash by design.
  - POST /record?seconds=N sends "REC:N\\n" to the board over the same
    serial link (no reflash needed -- main/uart_cmd.c parses it at runtime),
    the mic task pauses its live $MIC stream, captures N seconds, and dumps
    it base64-encoded between BEGIN_WAV_B64/END_WAV_B64 markers; this file
    decodes that, writes a .wav under <repo root>/voice/, and serves it back
    at /voice/<filename> for the panel's playback button.

No third-party dependencies: stdlib http.server + threading for SSE,
pyserial for the UART link (already used elsewhere in this project).

Usage:
    python3 bridge_server.py [--port /dev/ttyUSB0] [--http-port 8765]
"""

import argparse
import base64
import http.server
import json
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import socketserver
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import numpy as np

MONITOR_DIR = Path(__file__).resolve().parent
PROJECT_DIR = MONITOR_DIR.parent
ROOT_DIR = PROJECT_DIR.parent
MAIN_SRC = PROJECT_DIR / "main" / "vl53l7cx_test.c"
IDF_EXPORT = Path.home() / "esp" / "esp-idf" / "export.sh"
PANEL_HTML = MONITOR_DIR / "panel.html"
PANEL_DIR = MONITOR_DIR / "panel"
VOICE_DIR = ROOT_DIR / "voice"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

# B14：WAV -> log-Mel 備援路線。`host/` 跟這支腳本不在同一個套件樹下，
# 用之前得先把 repo 根目錄塞進 sys.path。
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Imported lazily: mel_pipeline pulls in librosa, which is a heavy optional
# dependency. The bridge's core job -- serving the panel and relaying $-lines
# over SSE -- must still start on a machine that only has pyserial, and the
# E08 fallback demo depends on that. A missing librosa degrades the MFCC
# feature to an SSE error event instead of killing the whole server.
_MEL_IMPORT_ERROR = None
wav_to_log_mel_timed = None
write_mel_to_trial = None


def _load_mel_backend():
    """Return True once the B14 mel backend is importable; cache the failure."""
    global wav_to_log_mel_timed, write_mel_to_trial, _MEL_IMPORT_ERROR
    if wav_to_log_mel_timed is not None:
        return True
    if _MEL_IMPORT_ERROR is not None:
        return False
    try:
        from host.features.mel_pipeline import wav_to_log_mel_timed as _w
        from host.storage.mel_writer import write_mel_to_trial as _t
    except ImportError as exc:
        _MEL_IMPORT_ERROR = exc
        print(f"[bridge] mel backend unavailable ({exc}); "
              f"MFCC disabled, everything else still works")
        return False
    wav_to_log_mel_timed, write_mel_to_trial = _w, _t
    return True

# B09（session/trial 狀態機）還沒做，這裡先用「每次錄音自動 +1」模擬 trial_idx，
# 讓 B07/B09 落地前就能對著一個真實 h5 檔驗證「特徵寫回 HDF5」這條路徑通不通。
# --h5-session 沒給的話就只算 mel、存 .npy，不寫 HDF5。
mfcc_target = {"h5_path": None, "next_trial_idx": 0}

RESOLUTION_RE = re.compile(r"#define\s+TOF_RESOLUTION_MODE\s+\d+")
WAV_HEADER_RE = re.compile(r"rate=(\d+) bits=(\d+) channels=(\d+) bytes=(\d+)")


class Broadcaster:
    """Fan-out of parsed events to every connected SSE client."""

    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._clients.discard(q)

    def publish(self, event):
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # slow client, drop the frame rather than block the reader thread


broadcaster = Broadcaster()

# Guards the serial port + build/flash cycle so only one is ever in flight,
# and lets the reader thread know to stand down while a flash is running.
serial_lock = threading.Lock()
flashing = threading.Event()
current_resolution = {"dim": 8}

# The live serial.Serial instance, so the /record HTTP handler (a different
# thread than the reader) can write the "REC:<n>\n" command to the board.
# Guarded separately from serial_lock, which is about the open/close and
# flash-vs-read handoff, not individual writes.
serial_write_lock = threading.Lock()
current_serial_holder = {"ser": None}


def parse_line(line):
    line = line.strip()
    if not line.startswith("$"):
        return None
    try:
        if line.startswith("$TOF,"):
            _, sensor, dim, *values = line.split(",")
            return {
                "type": "tof",
                "sensor": sensor,
                "dim": int(dim),
                "values": [int(v) for v in values],
            }
        if line.startswith("$MIC,"):
            _, rms, peak = line.split(",")
            return {"type": "mic", "rms": float(rms), "peak": int(peak)}
        if line.startswith("$STATUS,"):
            m = re.search(r"res=(\d+)", line)
            if m:
                dim = int(m.group(1))
                current_resolution["dim"] = dim
                return {"type": "status", "dim": dim}
        if line.startswith("$REC,start,"):
            seconds = int(line.split(",")[2])
            return {"type": "record", "state": "recording", "seconds": seconds}
    except (ValueError, IndexError):
        return None
    return None


def save_wav(rate, bits, channels, pcm_bytes):
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm_bytes)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block_align, bits)
    header += b"data" + struct.pack("<I", len(pcm_bytes))

    filename = f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
    path = VOICE_DIR / filename
    path.write_bytes(header + pcm_bytes)
    return filename, len(header) + len(pcm_bytes)


def _process_mfcc(filename):
    """B14 備援路線：WAV 存好之後算 log-Mel，永遠存一份 `.npy`，
    有指定 `--h5-session` 的話再多寫一份進該 session 的下一個 trial。

    在自己的執行緒跑，不擋 serial_reader 讀下一行（延遲拆解裡 MFCC 只要
    ~0.1s，但 h5py I/O 加上 GIL 底下沒必要冒風險卡到即時的 $TOF 解析）。
    """
    wav_path = VOICE_DIR / filename
    if not _load_mel_backend():
        broadcaster.publish({"type": "mfcc", "state": "error", "file": filename,
                             "message": f"mel backend unavailable: {_MEL_IMPORT_ERROR}"})
        return
    broadcaster.publish({"type": "mfcc", "state": "computing", "file": filename})
    try:
        log_mel, elapsed = wav_to_log_mel_timed(wav_path)
    except Exception as exc:
        broadcaster.publish({"type": "mfcc", "state": "error", "file": filename, "message": str(exc)})
        return

    npy_path = wav_path.with_suffix(".mel.npy")
    np.save(npy_path, log_mel)

    event = {
        "type": "mfcc", "state": "done", "file": filename,
        "npy_file": npy_path.name, "n_frames": int(log_mel.shape[0]),
        "elapsed_ms": round(elapsed * 1000, 1),
    }

    h5_path = mfcc_target["h5_path"]
    if h5_path is not None:
        trial_idx = mfcc_target["next_trial_idx"]
        mfcc_target["next_trial_idx"] += 1
        try:
            write_mel_to_trial(h5_path, trial_idx, log_mel)
            event["h5_trial_idx"] = trial_idx
        except Exception as exc:
            broadcaster.publish({
                "type": "mfcc", "state": "error", "file": filename,
                "message": f"HDF5 寫入失敗 (trial_idx={trial_idx}): {exc}",
            })
            return

    broadcaster.publish(event)


def serial_reader(port, baud):
    """Runs for the lifetime of the process; pauses itself while `flashing`
    is set so the flash subprocess can have the port exclusively."""
    import serial

    # State for an in-progress BEGIN_WAV_B64 ... END_WAV_B64 capture. A
    # recording pauses the mic's normal $MIC lines but $TOF lines keep
    # interleaving on the same UART, so this only activates between the
    # markers and otherwise falls through to the regular line parser.
    wav_meta = None
    wav_chunks = []

    while True:
        if flashing.is_set():
            time.sleep(0.2)
            continue
        try:
            with serial_lock:
                ser = serial.Serial(port, baud, timeout=1)
            current_serial_holder["ser"] = ser
            print(f"[bridge] serial open: {port} @ {baud}")
            broadcaster.publish({"type": "link", "state": "up"})
            try:
                while not flashing.is_set():
                    raw = ser.readline()
                    if not raw:
                        continue
                    try:
                        text = raw.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    stripped = text.strip()

                    if stripped.startswith("BEGIN_WAV_B64"):
                        m = WAV_HEADER_RE.search(stripped)
                        wav_meta = {
                            "rate": int(m.group(1)), "bits": int(m.group(2)),
                            "channels": int(m.group(3)), "declared_bytes": int(m.group(4)),
                        } if m else {"rate": 16000, "bits": 16, "channels": 1, "declared_bytes": 0}
                        wav_chunks = []
                        broadcaster.publish({"type": "record", "state": "receiving"})
                        continue

                    if stripped == "END_WAV_B64" and wav_meta is not None:
                        try:
                            pcm = base64.b64decode("".join(wav_chunks))
                            filename, size = save_wav(
                                wav_meta["rate"], wav_meta["bits"], wav_meta["channels"], pcm)
                            broadcaster.publish({
                                "type": "record", "state": "done",
                                "file": filename, "url": f"/voice/{filename}",
                                "bytes": size, "duration_sec": round(len(pcm) / (wav_meta["rate"] * 2), 2),
                            })
                            print(f"[bridge] saved recording: voice/{filename} ({size} bytes)")
                            threading.Thread(target=_process_mfcc, args=(filename,), daemon=True).start()
                        except Exception as exc:
                            broadcaster.publish({"type": "record", "state": "error", "message": str(exc)})
                        wav_meta = None
                        wav_chunks = []
                        continue

                    if wav_meta is not None and not stripped.startswith("$"):
                        # A recording only pauses the mic's own $MIC lines;
                        # $TOF keeps interleaving on the same UART for a
                        # live panel during the capture+transfer window, so
                        # this can't assume every line seen in here is
                        # base64 -- only base64 lines are (base64 never
                        # starts with '$'). Anything '$'-prefixed falls
                        # through to the normal parser below instead.
                        wav_chunks.append(stripped)
                        continue

                    event = parse_line(text)
                    if event:
                        broadcaster.publish(event)
            finally:
                current_serial_holder["ser"] = None
                ser.close()
                broadcaster.publish({"type": "link", "state": "down"})
        except Exception as exc:
            broadcaster.publish({"type": "link", "state": "down", "message": str(exc)})
            time.sleep(1.0)


def do_switch_resolution(new_dim, port):
    if new_dim not in (4, 8):
        broadcaster.publish({"type": "flash", "state": "error", "message": "resolution must be 4 or 8"})
        return

    flashing.set()
    try:
        with serial_lock:
            time.sleep(0.3)  # let the reader thread release the port

            broadcaster.publish({"type": "flash", "state": "editing", "dim": new_dim})
            src = MAIN_SRC.read_text()
            new_src, n = RESOLUTION_RE.subn(f"#define TOF_RESOLUTION_MODE {new_dim}", src, count=1)
            if n != 1:
                broadcaster.publish({
                    "type": "flash", "state": "error",
                    "message": "could not find TOF_RESOLUTION_MODE define in vl53l7cx_test.c",
                })
                return
            MAIN_SRC.write_text(new_src)

            broadcaster.publish({"type": "flash", "state": "building", "dim": new_dim})
            cmd = (
                f"source {IDF_EXPORT} > /dev/null 2>&1 && "
                f"cd {PROJECT_DIR} && idf.py build"
            )
            result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                broadcaster.publish({
                    "type": "flash", "state": "error",
                    "message": "build failed: " + result.stdout[-1500:] + result.stderr[-1500:],
                })
                return

            broadcaster.publish({"type": "flash", "state": "flashing", "dim": new_dim})
            cmd = (
                f"source {IDF_EXPORT} > /dev/null 2>&1 && "
                f"cd {PROJECT_DIR} && idf.py -p {port} flash"
            )
            result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                broadcaster.publish({
                    "type": "flash", "state": "error",
                    "message": "flash failed: " + result.stdout[-1500:] + result.stderr[-1500:],
                })
                return

            current_resolution["dim"] = new_dim
            broadcaster.publish({"type": "flash", "state": "done", "dim": new_dim})
    finally:
        flashing.clear()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "vl53l7cx-monitor/1.0"

    def log_message(self, fmt, *args):
        pass  # keep stdout clean; the important stuff comes from the [bridge] prints

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/panel/"):
            self._serve_panel_asset()
        elif self.path == "/panel.html":
            # Legacy single-file panel, kept as the E08 fallback demo path.
            self._serve_panel()
        elif self.path.startswith("/events"):
            self._serve_events()
        elif self.path.startswith("/voice/"):
            self._serve_voice_file()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/switch"):
            self._handle_switch()
        elif self.path.startswith("/record"):
            self._handle_record()
        else:
            self.send_error(404)

    def _serve_voice_file(self):
        # basename only: no path traversal out of VOICE_DIR via ../
        name = Path(unquote(self.path[len("/voice/"):])).name
        path = VOICE_DIR / name
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # <script type="module"> enforces the JS MIME type strictly -- serving
    # .js as text/plain makes the browser refuse the module outright with
    # "Failed to load module script". BaseHTTPRequestHandler has no built-in
    # table, so spell out the types the panel actually ships.
    _PANEL_MIME = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".woff2": "font/woff2",
    }

    def _serve_panel_asset(self):
        rel = unquote(self.path[len("/panel/"):]) if self.path.startswith("/panel/") else ""
        rel = rel.split("?", 1)[0].split("#", 1)[0]
        if not rel or rel.endswith("/"):
            rel += "index.html"

        # Resolve-then-contain: PANEL_DIR has real subdirectories (js/modes/,
        # css/modes/), so the basename-only guard used by _serve_voice_file()
        # would flatten them. Resolve the joined path and require it to stay
        # under PANEL_DIR -- that rejects ../ and absolute paths alike.
        try:
            path = (PANEL_DIR / rel).resolve()
            path.relative_to(PANEL_DIR.resolve())
        except (ValueError, OSError):
            self.send_error(403)
            return
        if not path.is_file():
            self.send_error(404)
            return

        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         self._PANEL_MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_panel(self):
        body = PANEL_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = broadcaster.subscribe()
        try:
            # tell the freshly-connected client what we currently know
            initial = {"type": "status", "dim": current_resolution["dim"]}
            self.wfile.write(f"data: {json.dumps(initial)}\n\n".encode())
            self.wfile.flush()

            last_ping = time.time()
            while True:
                try:
                    event = q.get(timeout=1.0)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    if time.time() - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            broadcaster.unsubscribe(q)

    def _handle_switch(self):
        m = re.search(r"res=(\d+)", self.path)
        if not m:
            self.send_error(400, "missing res= query param")
            return
        new_dim = int(m.group(1))

        if flashing.is_set():
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"a flash is already in progress"}')
            return

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"accepted": True, "dim": new_dim}).encode())

        threading.Thread(target=do_switch_resolution, args=(new_dim, self.server.serial_port), daemon=True).start()

    def _handle_record(self):
        m = re.search(r"seconds=(\d+)", self.path)
        seconds = int(m.group(1)) if m else 5
        seconds = max(1, min(30, seconds))

        if flashing.is_set():
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"a flash is in progress"}')
            return

        ser = current_serial_holder["ser"]
        if ser is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"serial link is down"}')
            return

        with serial_write_lock:
            ser.write(f"REC:{seconds}\n".encode())

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"accepted": True, "seconds": seconds}).encode())


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyUSB0", help="ESP32 serial port")
    parser.add_argument("--baud", type=int, default=460800, help="must match CONFIG_ESP_CONSOLE_UART_BAUDRATE")
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument(
        "--h5-session", default=None,
        help="B14 備援路線：每次錄音完成後，除了存 .npy，也把 log-mel 寫進這個 "
             "session HDF5 檔（trial group 要已經存在，例如用 "
             "ssi-backlog/tools/schema_example.py 產生）。不給就只存 .npy。")
    args = parser.parse_args()

    if args.h5_session:
        mfcc_target["h5_path"] = Path(args.h5_session)

    reader = threading.Thread(target=serial_reader, args=(args.port, args.baud), daemon=True)
    reader.start()

    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    server.serial_port = args.port
    print(f"[bridge] panel: http://127.0.0.1:{args.http_port}/")
    print(f"[bridge] serial: {args.port} @ {args.baud}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
