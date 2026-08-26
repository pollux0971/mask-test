import asyncio, json, urllib.request, base64
import websockets
CHROME_PORT = 9450
BRIDGE_HTTP_PORT = 8930
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
        await call("Page.enable")
        await call("Emulation.setDeviceMetricsOverride", {"width":900,"height":900,"deviceScaleFactor":1,"mobile":False})
        await call("Page.navigate", {"url": f"http://127.0.0.1:{BRIDGE_HTTP_PORT}/panel/#/record"})
        await asyncio.sleep(1.5)
        shot = await call("Page.captureScreenshot", {"format": "png"})
        with open(f"{SCRATCH}/c25_record_form.png", "wb") as f:
            f.write(base64.b64decode(shot["result"]["data"]))
asyncio.run(main())
