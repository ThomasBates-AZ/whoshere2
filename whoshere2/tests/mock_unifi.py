"""A tiny fake UniFi console for development and tests.

    python tests/mock_unifi.py 8443

Serves both the Integration API (X-API-KEY: test-key) and the classic
cookie API (username "whoshere", password "secret"). Change who is online with:

    curl -X POST localhost:8443/_mock/online -d '{"aa:bb:...": "ap-mac", ...}'
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY = "test-key"
SITE_ID = "88f7af54-98f8-306a-a1c7-c9349722b1f6"

DEVICES = [
    {"id": "dev-1", "macAddress": "B7:C5:21:08:82:32", "name": "Sound Booth", "model": "U7 Pro", "features": ["accessPoint"]},
    {"id": "dev-2", "macAddress": "8E:F1:FE:F2:30:CC", "name": "Security Office", "model": "U6 LR", "features": ["accessPoint"]},
    {"id": "dev-3", "macAddress": "B6:98:4E:F7:A1:CA", "name": "Bullpen", "model": "U6 Pro", "features": ["accessPoint"]},
    {"id": "dev-4", "macAddress": "F3:9A:1C:2F:97:24", "name": "Gym", "model": "U6 Mesh", "features": ["accessPoint"]},
    {"id": "dev-5", "macAddress": "AA:A2:92:68:C4:EA", "name": "Conference Room", "model": "U7 Lite", "features": ["accessPoint"]},
    {"id": "dev-6", "macAddress": "0C:EA:14:00:00:01", "name": "Office Switch", "model": "USW 24 PoE", "features": ["switching"]},
]
NAMES = {
    "90:83:3e:7d:d3:6b": "Alexs-iPhone",
    "28:de:cf:88:f7:a2": "David's Galaxy S24",
    "08:51:dc:e1:33:cf": "Jenny-iPhone",
    "3c:22:fb:10:20:30": "Front Desk PC",
    "da:11:22:33:44:55": "iPhone",
    "f0:18:98:aa:bb:cc": "Toms-Pixel-8",
    "f4:0f:24:11:22:33": "Morgans-iPhone",
}
WIRED = {"3c:22:fb:10:20:30"}

STATE = {"online": {}}  # client mac -> AP mac
LOCK = threading.Lock()
SESSION = "sess-123"
CSRF = "csrf-abc"


def dev_id_for(ap_mac: str) -> str | None:
    for d in DEVICES:
        if d["macAddress"].lower() == ap_mac.lower():
            return d["id"]
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _page(self, items, qs):
        offset = int(qs.get("offset", ["0"])[0])
        limit = int(qs.get("limit", ["25"])[0])
        chunk = items[offset:offset + limit]
        return {"offset": offset, "limit": limit, "count": len(chunk), "totalCount": len(items), "data": chunk}

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/_mock/online":
            with LOCK:
                STATE["online"] = {k.lower(): v.lower() for k, v in self._body().items()}
            return self._send(200, {"ok": True})
        if path == "/api/auth/login":
            b = self._body()
            if b.get("username") == "whoshere" and b.get("password") == "secret":
                return self._send(200, {"username": "whoshere"},
                                  {"Set-Cookie": f"TOKEN={SESSION}; Path=/", "X-CSRF-Token": CSRF})
            return self._send(401, {"message": "Invalid username or password"})
        self._send(404, {"error": "not found"})

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path
        with LOCK:
            online = dict(STATE["online"])

        if path.startswith("/proxy/network/integration/v1/"):
            if self.headers.get("X-API-KEY") != API_KEY:
                return self._send(401, {"statusCode": 401, "statusName": "UNAUTHORIZED",
                                        "message": "Unauthorized"})
            rest = path[len("/proxy/network/integration/v1"):]
            if rest == "/sites":
                return self._send(200, self._page([{"id": SITE_ID, "internalReference": "default",
                                                    "name": "Default"}], qs))
            if rest == f"/sites/{SITE_ID}/devices":
                return self._send(200, self._page(DEVICES, qs))
            if rest == f"/sites/{SITE_ID}/clients":
                clients = [{
                    "type": "WIRED" if mac in WIRED else "WIRELESS",
                    "id": f"c-{mac}", "name": NAMES.get(mac, mac),
                    "connectedAt": "2026-11-12T15:00:00Z", "ipAddress": "10.0.0.5",
                    "macAddress": mac, "uplinkDeviceId": dev_id_for(ap),
                } for mac, ap in online.items()]
                # plus a VPN client without MAC, which must be ignored
                clients.append({"type": "VPN", "id": "vpn-1", "name": "Remote user"})
                return self._send(200, self._page(clients, qs))
            return self._send(404, {"message": "not found"})

        if path.startswith("/proxy/network/api/s/default/"):
            if f"TOKEN={SESSION}" not in (self.headers.get("Cookie") or "") or self.headers.get("X-CSRF-Token") != CSRF:
                return self._send(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}, "data": []})
            if path.endswith("/stat/device"):
                data = [{"mac": d["macAddress"].lower(), "name": d["name"],
                         "type": "uap" if "accessPoint" in d["features"] else "usw"} for d in DEVICES]
                return self._send(200, {"meta": {"rc": "ok"}, "data": data})
            if path.endswith("/stat/sta"):
                data = []
                for mac, ap in online.items():
                    row = {"mac": mac, "hostname": NAMES.get(mac, ""), "is_wired": mac in WIRED}
                    row["sw_mac" if mac in WIRED else "ap_mac"] = ap
                    data.append(row)
                return self._send(200, {"meta": {"rc": "ok"}, "data": data})
        self._send(404, {"error": "not found"})


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
    print(f"Mock UniFi on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
