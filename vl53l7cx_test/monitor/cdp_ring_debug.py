import asyncio, json, urllib.request
import websockets
CHROME_PORT = 9470
BRIDGE_HTTP_PORT = 8950
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
        await call("Page.navigate", {"url": f"http://127.0.0.1:{BRIDGE_HTTP_PORT}/"})
        await asyncio.sleep(2.5)
        print("drop level dot class:", await ev("document.querySelector('[data-status-drop]')?.parentElement?.textContent"))
        print("status-warning display:", await ev("getComputedStyle(document.querySelector('[data-status-warning]')).display"))
        print("status-warning text:", await ev("document.querySelector('[data-status-warning]')?.textContent"))
        print("drop pct:", await ev("document.querySelector('[data-status-drop]')?.textContent"))
        print("symmetry pct:", await ev("document.querySelector('[data-status-symmetry]')?.textContent"))
asyncio.run(main())
