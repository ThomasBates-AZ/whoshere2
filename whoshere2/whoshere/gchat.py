"""Google Chat connection, Pub/Sub style.

Google Chat publishes every message for the app to a Pub/Sub topic. This module pulls
them from a subscription (an outbound HTTPS request from your server, so nothing on
your network is exposed), works out the reply with chatbot.py, and posts it with the
Chat API as the app. Both calls use one service-account key.

Google endpoints used:
  POST https://oauth2.googleapis.com/token                       (service account -> access token)
  POST https://pubsub.googleapis.com/v1/<subscription>:pull       (fetch events)
  POST https://pubsub.googleapis.com/v1/<subscription>:acknowledge
  POST https://chat.googleapis.com/v1/<space>/messages            (reply, scope chat.bot)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

from . import chatbot
from .config import Settings
from .db import Database

log = logging.getLogger("whoshere.gchat")

SCOPES = "https://www.googleapis.com/auth/chat.bot https://www.googleapis.com/auth/pubsub"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PUBSUB_URL = "https://pubsub.googleapis.com"
CHAT_URL = "https://chat.googleapis.com"
PULL_TIMEOUT = 70  # Pub/Sub holds a pull open for a while when there's nothing to deliver


class ChatSetupError(Exception):
    """Configuration problem; the message is shown on the admin page."""


def load_key(path: Path) -> dict:
    try:
        key = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ChatSetupError(f"No service account key at {path}. Upload it on the Google Chat admin page.")
    except ValueError:
        raise ChatSetupError("The service account key file isn't valid JSON.")
    return check_key(key)


def check_key(key: dict) -> dict:
    if not isinstance(key, dict) or key.get("type") != "service_account":
        raise ChatSetupError("That isn't a service account key (expected \"type\": \"service_account\").")
    for field in ("client_email", "private_key"):
        if not key.get(field):
            raise ChatSetupError(f"The service account key is missing {field}.")
    return key


def _http(method: str, url: str, body: dict | bytes | None = None, headers: dict | None = None,
          timeout: float = 30):
    """Returns (status, parsed JSON or text). Raises ConnectionError if Google can't be reached."""
    if isinstance(body, dict):
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    else:
        data = body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectionError(str(getattr(e, "reason", e))) from e
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, raw.decode(errors="replace")


def _err(status: int, payload) -> str:
    if isinstance(payload, dict):
        e = payload.get("error")
        if isinstance(e, dict):
            return f"HTTP {status}: {e.get('message') or e.get('status')}"
        if isinstance(e, str):
            return f"HTTP {status}: {e} {payload.get('error_description', '')}".strip()
    return f"HTTP {status}"


class GoogleAuth:
    def __init__(self, key: dict, token_url: str = TOKEN_URL):
        self.key = key
        self.token_url = key.get("token_uri") or token_url
        self._token: str | None = None
        self._expires = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires - 120:
            return self._token
        import jwt  # PyJWT, with the cryptography backend for RS256

        now = int(time.time())
        assertion = jwt.encode(
            {"iss": self.key["client_email"], "scope": SCOPES, "aud": self.token_url,
             "iat": now, "exp": now + 3600},
            self.key["private_key"], algorithm="RS256",
            headers={"kid": self.key.get("private_key_id", "")} if self.key.get("private_key_id") else None)
        body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": assertion}).encode()
        status, payload = _http("POST", self.token_url, body,
                                {"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
            raise ChatSetupError(f"Google rejected the service account key ({_err(status, payload)}).")
        self._token = payload["access_token"]
        self._expires = time.time() + int(payload.get("expires_in", 3600))
        return self._token


class ChatBridge:
    def __init__(self, settings: Settings, db: Database, *, pubsub_url: str = PUBSUB_URL,
                 chat_url: str = CHAT_URL, token_url: str = TOKEN_URL):
        self.settings = settings
        self.db = db
        self.pubsub_url = pubsub_url.rstrip("/")
        self.chat_url = chat_url.rstrip("/")
        self.token_url = token_url
        self._auth: GoogleAuth | None = None
        self._seen: deque[str] = deque(maxlen=500)  # Pub/Sub can deliver twice
        # Shown on the admin page
        self.last_pull_ok: float | None = None
        self.last_event_at: float | None = None
        self.last_error: str | None = None        # connection/setup problem, cleared once pulling works
        self.last_reply_error: str | None = None  # most recent reply that couldn't be sent
        self.replies_sent = 0

    @property
    def enabled(self) -> bool:
        return bool(self.settings.chat_subscription)

    def reset(self) -> None:
        """Forget the cached key/token (after a new key is uploaded)."""
        self._auth = None
        self.last_error = None

    def auth(self) -> GoogleAuth:
        if self._auth is None:
            self._auth = GoogleAuth(load_key(self.settings.chat_key_file), self.token_url)
        return self._auth

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.auth().token()}"}

    # ----- one round: pull, answer, acknowledge (blocking; runs in a thread)

    def pull_once(self, max_messages: int = 10) -> int:
        sub = self.settings.chat_subscription
        status, payload = _http("POST", f"{self.pubsub_url}/v1/{sub}:pull",
                                {"maxMessages": max_messages}, self._headers(), timeout=PULL_TIMEOUT)
        if status == 403:
            raise ChatSetupError(f"Not allowed to read {sub} ({_err(status, payload)}). Give the service "
                                 "account the Pub/Sub Subscriber role on the subscription.")
        if status == 404:
            raise ChatSetupError(f"Subscription {sub} not found. Check GOOGLE_CHAT_SUBSCRIPTION.")
        if status != 200:
            raise ChatSetupError(f"Pub/Sub pull failed ({_err(status, payload)}).")
        self.last_pull_ok = time.time()
        received = payload.get("receivedMessages") or [] if isinstance(payload, dict) else []
        ack_ids = []
        for item in received:
            ack_ids.append(item.get("ackId"))
            msg = item.get("message") or {}
            try:
                event = json.loads(base64.b64decode(msg.get("data") or b"") or b"{}")
            except ValueError:
                log.warning("Ignoring a Pub/Sub message that isn't JSON")
                continue
            try:
                self.handle(event)
            except Exception as e:  # one bad event mustn't block the others
                log.warning("Couldn't answer a Chat message: %s", e)
                self.last_reply_error = f"{time.strftime('%Y-%m-%d %H:%M')}: {e}"
        if ack_ids:
            _http("POST", f"{self.pubsub_url}/v1/{sub}:acknowledge",
                  {"ackIds": [a for a in ack_ids if a]}, self._headers())
        return len(received)

    def handle(self, event: dict) -> str | None:
        req = chatbot.parse_event(event)
        if req.message_name:
            if req.message_name in self._seen:
                return None
            self._seen.append(req.message_name)
        self.last_event_at = time.time()
        reply = chatbot.reply_for(req, self.db, self.settings)
        if not reply or not req.space:
            return None
        log.info("Chat: %s asked %r (command %s)", req.user_email or req.user_name, req.text, req.command_id)
        private = (self.settings.chat_private_replies and req.user_name
                   and req.space_type not in ("DIRECT_MESSAGE", ""))
        self.send(req.space, reply, thread=req.thread, private_to=req.user_name if private else None)
        return reply.text

    def send(self, space: str, reply, thread: str | None = None, private_to: str | None = None) -> None:
        """Post a reply. Tries, in order: card, then plain text; privately, then to the space.

        The first one Chat accepts wins, so a reply always gets through if anything can.
        """
        if isinstance(reply, str):
            reply = chatbot.Reply(reply)
        url = f"{self.chat_url}/v1/{space}/messages"
        if thread:
            url += "?" + urllib.parse.urlencode({"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"})

        attempts = []
        for viewer in ([private_to, None] if private_to else [None]):
            if reply.card:
                attempts.append(("card", viewer))
            attempts.append(("text", viewer))

        last = None
        for kind, viewer in attempts:
            if kind == "card":
                body: dict = {"cardsV2": [reply.card], "fallbackText": reply.text[:400]}
            else:
                body = {"text": reply.text}
            if thread:
                body["thread"] = {"name": thread}
            if viewer:
                body["privateMessageViewer"] = {"name": viewer}
            status, payload = _http("POST", url, body, self._headers())
            if status == 200:
                self.replies_sent += 1
                return
            last = (status, payload)
            log.info("Chat refused a %s%s reply (%s); trying the next way", kind,
                     " private" if viewer else "", _err(status, payload))
        raise ChatSetupError(f"Chat API refused the reply ({_err(*last)}).")

    def check(self) -> str:
        """For the admin page's test button: can we get a token with this key?"""
        self.reset()
        key = self.auth().key
        self.auth().token()
        return key["client_email"]

    # ----- background loop

    async def run(self) -> None:
        if not self.enabled:
            return
        log.info("Google Chat: listening on %s", self.settings.chat_subscription)
        delay = 5
        while True:
            try:
                await asyncio.to_thread(self.pull_once)
                if self.last_error:
                    log.info("Google Chat: connected again")
                self.last_error = None
                delay = 5
            except (ChatSetupError, ConnectionError) as e:
                msg = str(e) if isinstance(e, ChatSetupError) else f"Can't reach Google: {e}"
                if msg != self.last_error:
                    log.warning("Google Chat: %s", msg)
                self.last_error = msg
                if isinstance(e, ChatSetupError):
                    self._auth = None  # re-read the key next time, in case it was replaced
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
            except Exception as e:
                log.exception("Google Chat loop error")
                self.last_error = f"Unexpected error: {e}"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
