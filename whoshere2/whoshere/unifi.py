"""Talks to the UniFi Network application and returns a normalized snapshot.

Two ways to connect:

* API key (recommended, UniFi Network 9+): the official Integration API at
  /proxy/network/integration/v1, authenticated with an X-API-KEY header.
* Username/password (fallback): the classic cookie-based API that v1 used.
  Works on UniFi OS consoles (/api/auth/login + /proxy/network/...) and on
  older self-hosted controllers (/api/login on :8443).

Both return the same Snapshot, so nothing else in the app cares which is used.
"""
from __future__ import annotations

import http.cookiejar
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace

from .config import Settings
from .macutil import normalize_mac

TIMEOUT = 20  # seconds per HTTP request
PAGE_SIZE = 200  # Integration API maximum


class UniFiError(Exception):
    """Something went wrong talking to UniFi. The message is shown to admins."""


@dataclass(frozen=True)
class ClientInfo:
    mac: str
    name: str            # UniFi's display name (alias if set, otherwise hostname)
    uplink_mac: str | None  # access point (or switch, for wired clients) it's attached to
    wired: bool = False
    ip: str | None = None   # current IPv4 address, used to recognize a phone on the self-register page
    ssid: str | None = None  # Wi-Fi network name; None for wired clients or when UniFi didn't say


@dataclass(frozen=True)
class NetDevice:
    mac: str
    name: str
    kind: str = ""  # "ap", "switch", "gateway"... informational only


@dataclass
class Snapshot:
    clients: list[ClientInfo] = field(default_factory=list)
    devices: list[NetDevice] = field(default_factory=list)
    taken_at: float = field(default_factory=time.time)
    # Whether Wi-Fi network names were available on this poll, and why not if they weren't.
    ssid_ok: bool = True
    ssid_note: str | None = None


class _Http:
    def __init__(self, base_url: str, verify_ssl: bool):
        if not base_url:
            raise UniFiError("UNIFI_URL is not set")
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        self.base_url = base_url.rstrip("/")
        ctx = ssl.create_default_context()
        if not verify_ssl:
            # UniFi consoles ship with a self-signed certificate.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, headers: dict | None = None):
        """Returns (status, response headers, parsed JSON or None). Never raises on HTTP status."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status, resp.headers, _parse(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, e.headers, _parse(e.read())
        except urllib.error.URLError as e:
            raise UniFiError(f"Can't reach {self.base_url}: {e.reason}") from e
        except (TimeoutError, OSError) as e:
            raise UniFiError(f"Can't reach {self.base_url}: {e}") from e


def _parse(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _describe(status: int, payload) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error"):
            if payload.get(key):
                return f"HTTP {status}: {payload[key]}"
        meta = payload.get("meta")
        if isinstance(meta, dict) and meta.get("msg"):
            return f"HTTP {status}: {meta['msg']}"
    return f"HTTP {status}"


# --------------------------------------------------------------------------- API key


class IntegrationClient(_Http):
    PREFIX = "/proxy/network/integration/v1"

    SSID_RETRY = 3600  # seconds to wait before retrying network names after the console refused

    def __init__(self, base_url: str, api_key: str, site: str, verify_ssl: bool, want_ssid: bool = True):
        super().__init__(base_url, verify_ssl)
        self.api_key = api_key
        self.site = site
        self.want_ssid = want_ssid
        self._site_id: str | None = None
        self._site_ref: str | None = None  # short site name ("default") used by the classic API
        self._ssid_note: str | None = None
        self._ssid_retry_at = 0.0

    def _get(self, path: str, params: dict | None = None):
        status, _, payload = self.request("GET", self.PREFIX + path, params=params,
                                          headers={"X-API-KEY": self.api_key})
        if status == 401 or status == 403:
            raise UniFiError(f"UniFi rejected the API key ({_describe(status, payload)}). "
                             "Check UNIFI_API_KEY and that it hasn't expired.")
        if status == 404:
            raise UniFiError(f"{self.PREFIX}{path} not found (HTTP 404). Is UNIFI_URL the UniFi "
                             "console's address, and is UniFi Network 9.0 or newer?")
        if status >= 400 or payload is None:
            raise UniFiError(f"UniFi error on {path}: {_describe(status, payload)}")
        return payload

    def _paged(self, path: str) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while True:
            page = self._get(path, {"offset": offset, "limit": PAGE_SIZE})
            data = page.get("data") or []
            items.extend(data)
            total = page.get("totalCount")
            offset += len(data)
            if not data or (total is not None and offset >= total) or len(data) < PAGE_SIZE:
                return items

    def site_id(self) -> str:
        if self._site_id:
            return self._site_id
        sites = self._paged("/sites")
        wanted = self.site.lower()
        for s in sites:
            candidates = {str(s.get(k, "")).lower() for k in ("id", "internalReference", "name")}
            if wanted in candidates:
                self._site_id = s["id"]
                self._site_ref = s.get("internalReference") or self.site
                return self._site_id
        if len(sites) == 1 and wanted == "default":
            self._site_id = sites[0]["id"]
            self._site_ref = sites[0].get("internalReference") or "default"
            return self._site_id
        names = ", ".join(f"{s.get('name')} ({s.get('internalReference')})" for s in sites) or "none"
        raise UniFiError(f"Site {self.site!r} not found. Sites this key can see: {names}")

    def snapshot(self) -> Snapshot:
        sid = self.site_id()
        raw_devices = self._paged(f"/sites/{sid}/devices")
        raw_clients = self._paged(f"/sites/{sid}/clients")

        devices: list[NetDevice] = []
        by_id: dict[str, str] = {}
        for d in raw_devices:
            mac = normalize_mac(d.get("macAddress"))
            if not mac:
                continue
            by_id[str(d.get("id"))] = mac
            features = d.get("features") or []
            kind = "ap" if "accessPoint" in features else ("switch" if "switching" in features else "")
            devices.append(NetDevice(mac=mac, name=d.get("name") or d.get("model") or mac, kind=kind))

        clients: list[ClientInfo] = []
        for c in raw_clients:
            mac = normalize_mac(c.get("macAddress"))
            if not mac:
                continue  # VPN / teleport clients have no MAC
            clients.append(ClientInfo(
                mac=mac,
                name=c.get("name") or "",
                uplink_mac=by_id.get(str(c.get("uplinkDeviceId"))),
                wired=str(c.get("type", "")).upper() == "WIRED",
                ip=c.get("ipAddress") or None,
            ))

        snap = Snapshot(clients=clients, devices=devices)
        if self.want_ssid:
            ssids = self._ssids()
            if ssids is None:
                snap.ssid_ok, snap.ssid_note = False, self._ssid_note
            else:
                snap.clients = [replace(c, ssid=None if c.wired else ssids.get(c.mac)) for c in clients]
        return snap

    def _ssids(self) -> dict[str, str] | None:
        """Wi-Fi network name per client MAC.

        The Integration API's client list has no network name, so this asks the classic
        client list (stat/sta, field "essid") with the same API key. If the console refuses,
        everything else keeps working; we just try again later.
        """
        if time.time() < self._ssid_retry_at:
            return None
        path = f"/proxy/network/api/s/{self._site_ref or self.site}/stat/sta"
        try:
            status, _, payload = self.request("GET", path, headers={"X-API-KEY": self.api_key})
        except UniFiError as e:
            status, payload = 0, str(e)
        if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list):
            self._ssid_note = None
            return {mac: c["essid"] for c in payload["data"]
                    if (mac := normalize_mac(c.get("mac"))) and c.get("essid") and not c.get("is_wired")}
        reason = _describe(status, payload) if status else str(payload)
        self._ssid_note = (f"UniFi didn't provide Wi-Fi network names with this API key ({reason}). "
                           "Everything else works; network names will be blank.")
        self._ssid_retry_at = time.time() + self.SSID_RETRY
        return None


# --------------------------------------------------------------------------- username/password


class SessionClient(_Http):
    def __init__(self, base_url: str, username: str, password: str, site: str, verify_ssl: bool):
        super().__init__(base_url, verify_ssl)
        self.username = username
        self.password = password
        self.site = site
        self.csrf: str | None = None
        self.prefix: str | None = None  # "/proxy/network" on UniFi OS, "" on old controllers

    def login(self) -> None:
        self.cookies.clear()
        creds = {"username": self.username, "password": self.password, "remember": True}
        status, headers, payload = self.request("POST", "/api/auth/login", body=creds)
        if status == 200:
            self.prefix = "/proxy/network"
            self.csrf = headers.get("X-CSRF-Token") or headers.get("x-csrf-token")
            return
        if status in (401, 403, 429):
            raise UniFiError(f"UniFi login failed ({_describe(status, payload)}). "
                             "Check UNIFI_USERNAME / UNIFI_PASSWORD.")
        # Older self-hosted controller (Network Application on :8443)
        status, headers, payload = self.request("POST", "/api/login", body=creds)
        if status == 200:
            self.prefix = ""
            self.csrf = None
            return
        raise UniFiError(f"UniFi login failed ({_describe(status, payload)}). "
                         "Check UNIFI_URL, UNIFI_USERNAME and UNIFI_PASSWORD.")

    def _get(self, path: str) -> list[dict]:
        for attempt in (1, 2):
            if self.prefix is None:
                self.login()
            headers = {"X-CSRF-Token": self.csrf} if self.csrf else {}
            status, resp_headers, payload = self.request(
                "GET", f"{self.prefix}/api/s/{self.site}{path}", headers=headers)
            new_csrf = resp_headers.get("X-Updated-CSRF-Token") if resp_headers else None
            if new_csrf:
                self.csrf = new_csrf
            if status == 401 and attempt == 1:
                self.prefix = None  # session expired; log in again
                continue
            if status >= 400 or not isinstance(payload, dict):
                raise UniFiError(f"UniFi error on {path}: {_describe(status, payload)}")
            return payload.get("data") or []
        raise UniFiError("UniFi session could not be re-established")

    def snapshot(self) -> Snapshot:
        raw_devices = self._get("/stat/device")
        raw_clients = self._get("/stat/sta")
        devices = []
        for d in raw_devices:
            mac = normalize_mac(d.get("mac"))
            if mac:
                kind = {"uap": "ap", "usw": "switch"}.get(d.get("type", ""), d.get("type", ""))
                devices.append(NetDevice(mac=mac, name=d.get("name") or d.get("model") or mac, kind=kind))
        clients = []
        for c in raw_clients:
            mac = normalize_mac(c.get("mac"))
            if not mac:
                continue
            wired = bool(c.get("is_wired"))
            clients.append(ClientInfo(
                mac=mac,
                name=c.get("name") or c.get("hostname") or "",
                uplink_mac=normalize_mac(c.get("sw_mac") if wired else c.get("ap_mac")),
                wired=wired,
                ip=c.get("ip") or None,
                ssid=None if wired else (c.get("essid") or None),
            ))
        return Snapshot(clients=clients, devices=devices)


def find_client_by_ip(snap: Snapshot, ip: str | None) -> ClientInfo | None:
    """The connected client that currently has this IP address, if UniFi knows of exactly one."""
    if not ip:
        return None
    ip = ip.removeprefix("::ffff:")  # IPv4-mapped IPv6, as some sockets report it
    matches = [c for c in snap.clients if c.ip == ip]
    return matches[0] if len(matches) == 1 else None


def make_client(settings: Settings):
    if settings.auth_mode == "api_key":
        return IntegrationClient(settings.unifi_url, settings.unifi_api_key,
                                 settings.unifi_site, settings.unifi_verify_ssl,
                                 want_ssid=settings.show_wifi_network or bool(settings.wifi_networks))
    if settings.auth_mode == "password":
        return SessionClient(settings.unifi_url, settings.unifi_username, settings.unifi_password,
                             settings.unifi_site, settings.unifi_verify_ssl)
    raise UniFiError("No UniFi credentials: set UNIFI_API_KEY (recommended) "
                     "or UNIFI_USERNAME and UNIFI_PASSWORD")
