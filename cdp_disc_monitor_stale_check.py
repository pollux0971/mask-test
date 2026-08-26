import asyncio, json, urllib.request, base64, subprocess
import websockets
CHROME_PORT = 9460
BRIDGE_HTTP_PORT = 8940
SCRATCH = "/tmp/claude-1000/-home-pollux-Desktop-esp-mask-test/9ee0f85d-9df1-410a-ade9-ddd3ecc56f75/scratchpad"
async def main():
    req = urllib.request.Request(f"http://127.0.0.1:{CHROME_PORT}/json/new?about:blank", method="PUT")
    r = urllib.request.urlopen(req, timeout=5)
    target = json.loads(r.read())
    ws_url = target["webSocketDebuggerUrl"]
    _id=[0]
    async with websockets.connect(ws_url, max_size=20_000_000) as ws:
        async def call(method, params=None):
            _id[0]+=1; i=_id[0]
            await ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                raw = await ws.recv(); msg = json.loads(raw)
                if msg.get("id") == i: return msg
        async def ev(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            v = r.get("result", {}).get("result", {})
            return v.get("value") if "value" in v else v
        await call("Page.enable")
        await call("Page.navigate", {"url": f"http://127.0.0.1:{BRIDGE_HTTP_PORT}/panel/#/monitor"})
        await asyncio.sleep(1.5)
        print("sidebar Hz before:", await ev("document.querySelector('[data-status-fps]')?.textContent"))
        print("panel Hz A before:", await ev("document.querySelector('[data-rate=\"A\"]')?.textContent"))
        pids = subprocess.run(["pgrep", "-f", f"http-port {BRIDGE_HTTP_PORT}"], capture_output=True, text=True).stdout.split()
        for p in pids:
            subprocess.run(["kill", "-9", p])
        checkpoints = []
        elapsed = 0
        for step in [3, 5, 10]:
            await asyncio.sleep(step)
            elapsed += step
            sidebar_hz = await ev("document.querySelector('[data-status-fps]')?.textContent")
            panel_hz_a = await ev("document.querySelector('[data-rate=\"A\"]')?.textContent")
            checkpoints.append({"elapsed_s": elapsed, "sidebar_hz": sidebar_hz, "panel_hz_a": panel_hz_a})
        print(json.dumps(checkpoints, indent=2, ensure_ascii=False))
        shot = await call("Page.captureScreenshot", {"format": "png"})
        with open(f"{SCRATCH}/c25_disc_monitor_18s_later.png", "wb") as f:
            f.write(base64.b64decode(shot["result"]["data"]))
asyncio.run(main())
