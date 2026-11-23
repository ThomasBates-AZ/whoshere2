"""Fake Google OAuth token endpoint, Pub/Sub and Chat API, for tests.

Checks the service-account JWT signature against the test key, serves queued Chat
events from a "subscription", and records the replies the app posts.
"""
from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SUBSCRIPTION = "projects/whoshere-test/subscriptions/whoshere-chat"
TOKEN = "ya29.test-token"


def make_key() -> tuple[dict, object]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    key = {"type": "service_account", "project_id": "whoshere-test", "private_key_id": "abc123",
           "private_key": pem, "client_email": "whoshere-chat@whoshere-test.iam.gserviceaccount.com",
           "client_id": "1", "token_uri": "SET_BY_TEST"}
    return key, private.public_key()


class FakeGoogle:
    def __init__(self):
        self.key, self.public_key = make_key()
        self.queue: list[dict] = []        # events waiting to be pulled
        self.acked: list[str] = []
        self.sent: list[dict] = []         # {"space", "query", "body"}
        self.reject_private = False
        self.reject_cards = False
        self.pull_status = 200
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                url = urllib.parse.urlparse(self.path)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if url.path == "/token":
                    form = urllib.parse.parse_qs(raw.decode())
                    try:
                        claims = jwt.decode(form["assertion"][0], fake.public_key, algorithms=["RS256"],
                                            audience=fake.key["token_uri"])
                        assert claims["iss"] == fake.key["client_email"]
                        assert "chat.bot" in claims["scope"] and "pubsub" in claims["scope"]
                    except Exception as e:
                        return self._send(400, {"error": "invalid_grant", "error_description": str(e)})
                    return self._send(200, {"access_token": TOKEN, "expires_in": 3599, "token_type": "Bearer"})

                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"error": {"code": 401, "message": "Request had invalid authentication credentials."}})
                body = json.loads(raw or b"{}")
                if url.path == f"/v1/{SUBSCRIPTION}:pull":
                    if fake.pull_status != 200:
                        return self._send(fake.pull_status, {"error": {"code": fake.pull_status,
                                                                       "message": "User not authorized to perform this action."}})
                    with fake.lock:
                        batch, fake.queue = fake.queue[:body.get("maxMessages", 10)], fake.queue[body.get("maxMessages", 10):]
                    msgs = [{"ackId": f"ack-{i}-{id(e)}", "message": {
                        "data": base64.b64encode(json.dumps(e).encode()).decode(), "messageId": str(i)}}
                        for i, e in enumerate(batch)]
                    return self._send(200, {"receivedMessages": msgs} if msgs else {})
                if url.path == f"/v1/{SUBSCRIPTION}:acknowledge":
                    with fake.lock:
                        fake.acked += body.get("ackIds", [])
                    return self._send(200, {})
                if url.path.startswith("/v1/spaces/") and url.path.endswith("/messages"):
                    if fake.reject_cards and "cardsV2" in body:
                        return self._send(400, {"error": {"code": 400, "message": "Invalid card."}})
                    if fake.reject_private and "privateMessageViewer" in body:
                        return self._send(400, {"error": {"code": 400, "message": "Private messages aren't supported here."}})
                    with fake.lock:
                        fake.sent.append({"space": url.path[len("/v1/"):-len("/messages")],
                                          "query": urllib.parse.parse_qs(url.query), "body": body})
                    return self._send(200, {"name": url.path[4:] + "/m1", **body})
                self._send(404, {"error": {"code": 404, "message": "not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.key["token_uri"] = self.url + "/token"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


# ----- Chat events, as Google sends them

def slash_event(command_id: int, text: str, arg: str, *, space_type="DIRECT_MESSAGE", space="spaces/AAA",
                email="thomas@ironwood.church", msg="spaces/AAA/messages/1"):
    return {
        "type": "MESSAGE",
        "eventTime": "2026-11-20T17:00:00Z",
        "space": {"name": space, "spaceType": space_type},
        "user": {"name": "users/111", "displayName": "Thomas Bates", "email": email},
        "message": {
            "name": msg, "text": text, "argumentText": arg,
            "thread": {"name": f"{space}/threads/T1"},
            "slashCommand": {"commandId": str(command_id)},
            "annotations": [{"type": "SLASH_COMMAND", "startIndex": 0, "length": len(text) - len(arg),
                             "slashCommand": {"commandId": str(command_id), "commandName": text.split()[0],
                                              "type": "INVOKE"}}],
        },
    }


def dm_event(text: str, email="thomas@ironwood.church", msg="spaces/AAA/messages/2"):
    return {"type": "MESSAGE", "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "user": {"name": "users/111", "displayName": "Thomas Bates", "email": email},
            "message": {"name": msg, "text": text, "argumentText": text, "thread": {"name": "spaces/AAA/threads/T2"}}}


def addon_command_event(command_id: int, text: str):
    return {"chat": {
        "user": {"name": "users/111", "email": "thomas@ironwood.church"},
        "appCommandPayload": {
            "appCommandMetadata": {"appCommandId": command_id, "appCommandType": "SLASH_COMMAND"},
            "space": {"name": "spaces/BBB", "spaceType": "SPACE"},
            "message": {"name": "spaces/BBB/messages/9", "text": text,
                        "thread": {"name": "spaces/BBB/threads/T9"}},
        }}}
