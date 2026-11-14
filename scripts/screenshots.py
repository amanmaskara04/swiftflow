"""Take console screenshots with headless Edge/Chrome over the DevTools protocol.

    python scripts/screenshots.py [--base http://localhost:3000] [--out docs/screenshots]

Signs in as acme-admin by calling the token endpoint from inside the page (same as the
console does), then captures each screen in light and dark, plus a phone-width pass.
Prints any browser console errors (CSP violations show up here).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
]

SIGN_IN = """
(async () => {
  const r = await fetch('/v1/auth/token', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({client_id:'%s', client_secret:'%s'})});
  const t = await r.json();
  sessionStorage.setItem('swiftflow.session', JSON.stringify({token:t.access_token, expiresAt:Date.now()+t.expires_in*1000,
    clientId:'%s', tenantId:t.tenant_id, tenantName:t.tenant_name, role:t.role, scopes:t.scope.split(' ')}));
  localStorage.setItem('swiftflow.theme', '%s');
  return r.status;
})()
"""


class Page:
    def __init__(self, ws):
        self.ws, self.n, self.errors = ws, 0, []

    async def send(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                return msg.get("result", {})
            if msg.get("method") == "Runtime.exceptionThrown":
                self.errors.append(
                    msg["params"]["exceptionDetails"].get("text", "")
                    + " "
                    + str(msg["params"]["exceptionDetails"].get("exception", {}).get("description", ""))[:300]
                )
            if msg.get("method") == "Log.entryAdded" and msg["params"]["entry"]["level"] == "error":
                self.errors.append(msg["params"]["entry"]["text"][:300])

    async def js(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        return r.get("result", {}).get("value")

    async def shot(self, path, width, height):
        await self.send("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=width < 600)
        await asyncio.sleep(0.4)
        data = (await self.send("Page.captureScreenshot", format="png"))["data"]
        Path(path).write_bytes(base64.b64decode(data))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:3000")
    ap.add_argument("--out", default="docs/screenshots")
    ap.add_argument("--only", default="")
    ap.add_argument("--js", default="", help="extra JS to run on the page before each capture")
    ap.add_argument("--wait", type=float, default=1.0, help="seconds to wait after --js")
    args = ap.parse_args()
    exe = next((b for b in BROWSERS if os.path.exists(b)), None)
    if not exe:
        sys.exit("no Edge/Chrome found")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    port = 9333
    proc = subprocess.Popen(
        [
            exe,
            "--headless=new",
            "--disable-gpu",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={tempfile.mkdtemp(prefix='sf-shots-')}",
            "--hide-scrollbars",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                break
            except Exception:
                time.sleep(0.2)
        ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        async with websockets.connect(ws_url, max_size=None) as ws:
            page = Page(ws)
            for d in ("Page", "Runtime", "Log"):
                await page.send(f"{d}.enable")
            shots = [
                ("signin", "/", None, 1440, 900, "light"),
                ("overview", "/", "acme-admin", 1440, 1000, "light"),
                ("orders", "/orders", "acme-admin", 1440, 900, "light"),
                ("order-drawer", "/orders/@first", "acme-admin", 1440, 900, "light"),
                ("order-cancelled", "/orders/@cancelled", "acme-admin", 1440, 900, "dark"),
                ("stock", "/stock", "acme-admin", 1440, 900, "light"),
                ("webhooks", "/webhooks", "acme-admin", 1440, 900, "light"),
                ("system-map", "/system", "acme-admin", 1440, 900, "dark"),
                ("chaos", "/chaos", "acme-admin", 1440, 900, "light"),
                ("overview-dark", "/", "acme-admin", 1440, 1000, "dark"),
                ("overview-phone", "/", "acme-admin", 390, 844, "light"),
                ("orders-phone", "/orders", "acme-admin", 390, 844, "dark"),
            ]
            for name, route, who, w, h, theme in shots:
                if args.only and name not in args.only.split(","):
                    continue
                await page.send("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=w < 600)
                await page.send("Page.navigate", url=args.base + "/")
                await asyncio.sleep(0.8)
                await page.js("sessionStorage.clear(); localStorage.setItem('swiftflow.theme','%s')" % theme)
                if who:
                    await page.js(SIGN_IN % (who, who + "-secret", who, theme))
                    if "@" in route:
                        status = "CANCELLED" if "cancelled" in route else "CONFIRMED"
                        oid = await page.js(
                            "(async()=>{const s=JSON.parse(sessionStorage.getItem('swiftflow.session'));const r=await fetch('/v1/orders?limit=1&status=%s',{headers:{Authorization:'Bearer '+s.token}});return (await r.json()).items[0]?.id||''})()"
                            % status
                        )
                        route = "/orders/" + (oid or "")
                await page.send("Page.navigate", url=args.base + route)
                await asyncio.sleep(2.5)
                if args.js:
                    await page.js(args.js)
                    await asyncio.sleep(args.wait)
                await page.shot(out / f"{name}.png", w, h)
                print("shot", name)
            if page.errors:
                print("BROWSER ERRORS:")
                [print("  ", e) for e in dict.fromkeys(page.errors)]
            else:
                print("no browser console errors")
    finally:
        proc.terminate()


asyncio.run(main())
