"""Apple push notifications for the Who's Here? Apple Watch app (optional).

The watch app registers its push token with POST /api/push/register. After every poll
the poller calls WatchPush.check(), which compares the board's verdict with the last one
and, when it changes in a way worth announcing, sends a notification to every registered
watch through Apple's push service (APNs). Nothing has to be opened to the internet:
the server only makes outgoing HTTPS connections to Apple.

Setup needs an APNs auth key (.p8) from the Apple Developer site, uploaded on the
Apple Watch admin page, plus APNS_KEY_ID, APNS_TEAM_ID and APNS_BUNDLE_ID.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import jwt

from . import presence
from .config import Settings
from .db import Database

log = logging.getLogger("whoshere.apns")

PRODUCTION = "https://api.push.apple.com"
SANDBOX = "https://api.sandbox.push.apple.com"
JWT_LIFETIME = 45 * 60          # Apple wants a new provider token every 20-60 minutes
DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
EVENTS = ("clear", "stale")     # what WATCH_PUSH can contain


class PushSetupError(Exception):
    """Something about the APNs settings or key is wrong; the message says what."""


def check_key(text: str) -> str:
    """Validate an uploaded .p8 key. Returns it cleaned up, or raises PushSetupError."""
    text = text.strip().replace("\r\n", "\n")
    if "BEGIN PRIVATE KEY" not in text:
        raise PushSetupError("That isn't an APNs key. Upload the .p8 file you downloaded from "
                             "Certificates, Identifiers & Profiles → Keys on the Apple Developer site.")
    try:
        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        key = load_pem_private_key(text.encode(), password=None)
    except (ValueError, TypeError) as e:
        raise PushSetupError(f"The key can't be read: {e}") from e
    if not isinstance(key, EllipticCurvePrivateKey):
        raise PushSetupError("That key isn't an APNs (elliptic-curve) key.")
    return text + "\n"


@dataclass
class SendResult:
    sent: int = 0
    failed: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"Sent to {self.sent} watch{'es' if self.sent != 1 else ''}"]
        if self.failed:
            parts.append(f"{self.failed} failed ({'; '.join(dict.fromkeys(self.errors))})")
        if self.removed:
            parts.append(f"{self.removed} no longer registered with Apple and removed")
        return ", ".join(parts) + "."


class WatchPush:
    def __init__(self, settings: Settings, db: Database, transport=None):
        self.settings = settings
        self.db = db
        self._transport = transport  # tests pass an httpx.MockTransport
        self._client = None
        self._jwt: str | None = None
        self._jwt_at = 0.0
        self._lock = threading.Lock()
        self.last_sent_at: float | None = None
        self.last_summary: str | None = None
        self.last_error: str | None = None

    # ----- configuration

    @property
    def key_present(self) -> bool:
        return self.settings.apns_key_file.exists()

    def missing(self) -> list[str]:
        s = self.settings
        out = [name for name, value in (("APNS_KEY_ID", s.apns_key_id), ("APNS_TEAM_ID", s.apns_team_id),
                                        ("APNS_BUNDLE_ID", s.apns_bundle_id)) if not value]
        if not self.key_present:
            out.append("the .p8 key (upload it below)")
        return out

    @property
    def enabled(self) -> bool:
        return not self.missing()

    @property
    def server(self) -> str:
        return SANDBOX if self.settings.apns_sandbox else PRODUCTION

    def watch_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM push_tokens")["n"]

    # ----- registration

    def register(self, token: str, name: str = "") -> None:
        now = time.time()
        self.db.run(
            """INSERT INTO push_tokens (token, name, created_at, last_seen) VALUES (?, ?, ?, ?)
               ON CONFLICT(token) DO UPDATE SET last_seen = excluded.last_seen,
                   name = CASE WHEN excluded.name != '' THEN excluded.name ELSE push_tokens.name END,
                   last_error = NULL""",
            (token, name[:80], now, now))

    def unregister(self, token: str) -> None:
        self.db.run("DELETE FROM push_tokens WHERE token = ?", (token,))

    # ----- sending

    def _provider_token(self) -> str:
        now = time.time()
        if self._jwt and now - self._jwt_at < JWT_LIFETIME:
            return self._jwt
        try:
            key = self.settings.apns_key_file.read_text()
            self._jwt = jwt.encode({"iss": self.settings.apns_team_id, "iat": int(now)}, key,
                                   algorithm="ES256", headers={"kid": self.settings.apns_key_id})
        except (OSError, ValueError, TypeError) as e:
            raise PushSetupError(f"The APNs key can't be used: {e}") from e
        self._jwt_at = now
        return self._jwt

    def _http(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(http2=True, timeout=15, transport=self._transport)
        return self._client

    def _post(self, token: str, body: bytes, collapse_id: str):
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": self.settings.apns_bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": str(int(time.time()) + 3600),  # pointless after an hour
            "apns-collapse-id": collapse_id,
        }
        return self._http().post(f"{self.server}/3/device/{token}", content=body, headers=headers)

    def send(self, title: str, body: str, verdict: str) -> SendResult:
        """Blocking. Sends one alert to every registered watch."""
        result = SendResult()
        if not self.enabled:
            raise PushSetupError("Apple Watch notifications aren't set up: missing " + ", ".join(self.missing()))
        payload = json.dumps({
            "aps": {"alert": {"title": title, "body": body}, "sound": "default", "thread-id": "whoshere",
                    "content-available": 1},  # wakes the app briefly so the complication updates too
            "verdict": verdict,
        }).encode()
        tokens = [r["token"] for r in self.db.all("SELECT token FROM push_tokens")]
        with self._lock:
            for token in tokens:
                try:
                    resp = self._post(token, payload, "whoshere-verdict")
                    if resp.status_code == 403 and _reason(resp) == "ExpiredProviderToken":
                        self._jwt = None
                        resp = self._post(token, payload, "whoshere-verdict")
                except PushSetupError:
                    raise
                except Exception as e:  # network trouble; try the next watch anyway
                    result.failed += 1
                    result.errors.append(f"can't reach Apple: {e}")
                    continue
                if resp.status_code == 200:
                    result.sent += 1
                    self.db.run("UPDATE push_tokens SET last_push = ?, last_error = NULL WHERE token = ?",
                                (time.time(), token))
                    continue
                reason = _reason(resp) or f"HTTP {resp.status_code}"
                if resp.status_code == 410 or reason in DEAD_TOKEN_REASONS:
                    if reason == "BadDeviceToken" and not self.settings.apns_sandbox:
                        reason += " (a build installed from Xcode needs APNS_SANDBOX=true)"
                    elif reason == "BadDeviceToken":
                        reason += " (a TestFlight or App Store build needs APNS_SANDBOX=false)"
                    if reason.startswith("BadDeviceToken"):
                        # Usually the sandbox setting, not a dead watch: keep it so fixing the setting works.
                        self.db.run("UPDATE push_tokens SET last_error = ? WHERE token = ?", (reason, token))
                        result.failed += 1
                        result.errors.append(reason)
                    else:
                        self.unregister(token)
                        result.removed += 1
                    continue
                result.failed += 1
                result.errors.append(reason)
                self.db.run("UPDATE push_tokens SET last_error = ? WHERE token = ?", (reason, token))
        self.last_sent_at = time.time()
        self.last_summary = result.summary()
        self.last_error = "; ".join(dict.fromkeys(result.errors)) or None
        log.info("Watch push %r: %s", title, result.summary())
        return result

    # ----- deciding when to notify

    def _meta(self, key: str) -> str | None:
        row = self.db.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.run("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def check(self, now: float | None = None) -> tuple[str, str, str] | None:
        """Blocking; call after each poll. Returns (title, body, verdict) if it notified."""
        now = now or time.time()
        verdict = presence.board(self.db, self.settings.stale_after, now)["verdict"]
        previous = self._meta("push_verdict")
        last_known = self._meta("push_known")  # last verdict that wasn't "stale"
        if verdict != previous:
            self._set_meta("push_verdict", verdict)
        if verdict != "stale" and verdict != last_known:
            self._set_meta("push_known", verdict)
        if previous is None or verdict == previous:
            return None  # first run just remembers where things stand

        message = None
        if verdict == "clear" and last_known == "occupied" and "clear" in self.settings.watch_push:
            message = ("All clear to arm", self._last_departure_text(), "clear")
        elif verdict == "stale" and "stale" in self.settings.watch_push:
            minutes = max(1, self.settings.stale_after // 60)
            message = ("Can't confirm", f"UniFi hasn't answered for {minutes} min. "
                                        "Don't arm until the board says all clear.", "stale")
        if message is None or not self.enabled or not self.watch_count():
            return None
        try:
            self.send(*message)
        except PushSetupError as e:
            self.last_error = str(e)
            log.warning("Watch push not sent: %s", e)
            return None
        return message

    def _last_departure_text(self) -> str:
        row = self.db.one("""SELECT p.name, e.at FROM events e JOIN people p ON p.id = e.person_id
                             WHERE e.kind = 'depart' ORDER BY e.at DESC LIMIT 1""")
        if not row:
            return "Everyone has left."
        at = datetime.fromtimestamp(row["at"], self.settings.timezone)
        clock = f"{at:%I}".lstrip("0") + f":{at:%M %p}"
        return f"{row['name']} was the last to leave, at {clock}."


def _reason(resp) -> str | None:
    try:
        return resp.json().get("reason")
    except ValueError:
        return None
