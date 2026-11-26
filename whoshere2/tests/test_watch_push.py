"""Apple Watch push: key checks, when notifications go out, what Apple is sent, and the API."""
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from whoshere import presence
from whoshere.apns import PRODUCTION, SANDBOX, PushSetupError, WatchPush, check_key
from whoshere.config import Settings
from whoshere.db import Database

from test_core import AP1, GRACE, PHONE, add_person, snap

TOKEN = "a1" * 32
OTHER = "b2" * 32


def pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def make_settings(monkeypatch, tmp_path, key=None, **env):
    base = {"DATA_DIR": str(tmp_path), "TZ": "America/Phoenix", "APNS_KEY_ID": "ABC123DEFG",
            "APNS_TEAM_ID": "TEAM123456", "APNS_BUNDLE_ID": "org.example.whoshere.watchkitapp"}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    settings = Settings.from_env()
    if key is not None:
        settings.apns_key_file.write_text(pem(key))
    return settings


class FakeApple:
    """Stands in for api.push.apple.com. reply(token) -> (status, reason or None)."""

    def __init__(self, reply=None):
        self.requests = []
        self.reply = reply or (lambda token: (200, None))

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        status, reason = self.reply(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(status, json={"reason": reason} if reason else None)


def push_for(settings, apple):
    db = Database(settings.db_path)
    return db, WatchPush(settings, db, transport=httpx.MockTransport(apple))


def occupied_then_clear(db, push, t0):
    """Alex arrives and leaves; returns what check() said after each poll."""
    add_person(db, "Alex Rivera", PHONE)
    said = []
    for t, online in [(t0, {PHONE: AP1}), (t0 + 60, {}), (t0 + 60 + GRACE, {})]:
        presence.apply_snapshot(db, snap(online, t), GRACE)
        said.append(push.check(now=t + 1))
    return said


# ---------------------------------------------------------------- key upload

def test_check_key_accepts_p8_and_rejects_others(ec_key):
    assert "BEGIN PRIVATE KEY" in check_key(pem(ec_key))
    with pytest.raises(PushSetupError):
        check_key("not a key")
    with pytest.raises(PushSetupError):
        check_key(pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)))


def test_missing_settings_are_listed(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path, APNS_KEY_ID="")
    push = WatchPush(s, Database(s.db_path))
    assert not push.enabled
    assert "APNS_KEY_ID" in push.missing() and any(".p8" in m for m in push.missing())


def test_watch_push_setting(monkeypatch, tmp_path):
    assert make_settings(monkeypatch, tmp_path).watch_push == ("clear",)
    assert make_settings(monkeypatch, tmp_path, WATCH_PUSH="clear, stale").watch_push == ("clear", "stale")
    assert make_settings(monkeypatch, tmp_path, WATCH_PUSH="none").watch_push == ()
    with pytest.raises(SystemExit):
        make_settings(monkeypatch, tmp_path, WATCH_PUSH="sometimes")


# ---------------------------------------------------------------- when it notifies

def test_all_clear_is_announced_once(monkeypatch, tmp_path, ec_key):
    apple = FakeApple()
    s = make_settings(monkeypatch, tmp_path, ec_key)
    db, push = push_for(s, apple)
    push.register(TOKEN, "Thomas's Watch")
    t0 = 1_800_000_000.0
    said = occupied_then_clear(db, push, t0)
    assert said[0] is None and said[1] is None  # first poll just remembers; still occupied
    title, body, verdict = said[2]
    assert (title, verdict) == ("All clear to arm", "clear")
    assert body.startswith("Alex Rivera was the last to leave, at ") and body[-4:] in (" AM.", " PM.")
    assert " 0" not in body  # 9:42 PM, not 09:42 PM

    assert len(apple.requests) == 1
    req = apple.requests[0]
    assert str(req.url) == f"{SANDBOX}/3/device/{TOKEN}"
    assert req.headers["apns-topic"] == "org.example.whoshere.watchkitapp"
    assert req.headers["apns-push-type"] == "alert"
    provider = req.headers["authorization"].split(" ", 1)[1]
    assert jwt.get_unverified_header(provider)["kid"] == "ABC123DEFG"
    claims = jwt.decode(provider, ec_key.public_key(), algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456"
    payload = json.loads(req.content)
    assert payload["aps"]["alert"]["title"] == "All clear to arm" and payload["verdict"] == "clear"

    # Nothing more while it stays clear
    presence.apply_snapshot(db, snap({}, t0 + 2000), GRACE)
    assert push.check(now=t0 + 2001) is None
    assert len(apple.requests) == 1


def test_production_server_when_not_sandbox(monkeypatch, tmp_path, ec_key):
    apple = FakeApple()
    s = make_settings(monkeypatch, tmp_path, ec_key, APNS_SANDBOX="false")
    db, push = push_for(s, apple)
    push.register(TOKEN)
    occupied_then_clear(db, push, 1_800_000_000.0)
    assert str(apple.requests[0].url).startswith(PRODUCTION + "/3/device/")


def test_nothing_sent_without_watches_or_when_turned_off(monkeypatch, tmp_path, ec_key):
    apple = FakeApple()
    s = make_settings(monkeypatch, tmp_path, ec_key)
    db, push = push_for(s, apple)
    occupied_then_clear(db, push, 1_800_000_000.0)  # no watch registered
    assert apple.requests == []

    s2 = make_settings(monkeypatch, tmp_path / "b", ec_key, WATCH_PUSH="none")
    db2, push2 = push_for(s2, apple)
    push2.register(TOKEN)
    occupied_then_clear(db2, push2, 1_800_000_000.0)
    assert apple.requests == []


def test_stale_is_announced_only_when_asked(monkeypatch, tmp_path, ec_key):
    apple = FakeApple()
    s = make_settings(monkeypatch, tmp_path, ec_key, WATCH_PUSH="clear,stale")
    db, push = push_for(s, apple)
    push.register(TOKEN)
    add_person(db, "Alex Rivera", PHONE)
    t0 = 1_800_000_000.0
    presence.apply_snapshot(db, snap({PHONE: AP1}, t0), GRACE)
    assert push.check(now=t0 + 1) is None
    presence.record_failure(db, "timed out", now=t0 + 60)
    title, _, verdict = push.check(now=t0 + s.stale_after + 5)
    assert (title, verdict) == ("Can't confirm", "stale")
    # UniFi comes back and everyone has gone: that's still news
    presence.apply_snapshot(db, snap({}, t0 + 400), GRACE)
    presence.apply_snapshot(db, snap({}, t0 + 400 + GRACE), GRACE)
    assert push.check(now=t0 + 401 + GRACE)[2] == "clear"


def test_coming_back_clear_after_clear_is_not_news(monkeypatch, tmp_path, ec_key):
    apple = FakeApple()
    s = make_settings(monkeypatch, tmp_path, ec_key)
    db, push = push_for(s, apple)
    push.register(TOKEN)
    t0 = 1_800_000_000.0
    presence.apply_snapshot(db, snap({}, t0), GRACE)
    push.check(now=t0 + 1)                      # clear
    push.check(now=t0 + s.stale_after + 10)     # stale (no polls)
    presence.apply_snapshot(db, snap({}, t0 + 1000), GRACE)
    assert push.check(now=t0 + 1001) is None    # clear again, nobody came or went
    assert apple.requests == []


# ---------------------------------------------------------------- Apple's answers

def test_dead_tokens_are_removed_and_bad_tokens_kept(monkeypatch, tmp_path, ec_key):
    apple = FakeApple(lambda token: (410, "Unregistered") if token == TOKEN else (400, "BadDeviceToken"))
    s = make_settings(monkeypatch, tmp_path, ec_key)
    db, push = push_for(s, apple)
    push.register(TOKEN)
    push.register(OTHER)
    result = push.send("Test", "body", "test")
    assert (result.sent, result.removed, result.failed) == (0, 1, 1)
    rows = db.all("SELECT token, last_error FROM push_tokens")
    assert [r["token"] for r in rows] == [OTHER]
    assert "APNS_SANDBOX=false" in rows[0]["last_error"]  # hint at the usual cause


def test_expired_provider_token_is_renewed(monkeypatch, tmp_path, ec_key):
    calls = []

    def reply(token):
        calls.append(token)
        return (403, "ExpiredProviderToken") if len(calls) == 1 else (200, None)

    s = make_settings(monkeypatch, tmp_path, ec_key)
    db, push = push_for(s, FakeApple(reply))
    push.register(TOKEN)
    assert push.send("Test", "body", "test").sent == 1
    assert len(calls) == 2


def test_send_without_setup_explains(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)  # no key uploaded
    db, push = push_for(s, FakeApple())
    with pytest.raises(PushSetupError, match=".p8"):
        push.send("Test", "body", "test")


# ---------------------------------------------------------------- API

def client_for(settings):
    from starlette.testclient import TestClient
    from whoshere.web import create_app
    return TestClient(create_app(settings, start_poller=False))


def test_register_and_unregister(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    with client_for(s) as c:
        r = c.post("/api/push/register", json={"token": TOKEN.upper(), "name": "Thomas's Watch"})
        assert r.status_code == 200 and r.json() == {"ok": True, "push_enabled": False}
        db = c.app.state.db
        assert db.one("SELECT name FROM push_tokens WHERE token = ?", (TOKEN,))["name"] == "Thomas's Watch"
        c.post("/api/push/register", json={"token": TOKEN})  # again, without a name: keeps it
        assert db.one("SELECT name FROM push_tokens")["name"] == "Thomas's Watch"
        assert c.post("/api/push/register", json={"token": "nope"}).status_code == 400
        assert c.post("/api/push/register", content=b"{").status_code == 400
        assert c.post("/api/push/delete", json={"token": TOKEN}).status_code == 404
        assert c.post("/api/push/unregister", json={"token": TOKEN}).json()["ok"]
        assert db.one("SELECT COUNT(*) AS n FROM push_tokens")["n"] == 0


def test_api_token(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path, API_TOKEN="s3cret-token")
    with client_for(s) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = c.get("/api/status", headers={"Authorization": "Bearer s3cret-token"})
        assert r.status_code == 200 and r.json()["verdict"] in ("clear", "occupied", "stale")
        assert "generated_at" in r.json() and r.json()["stale_after"] == s.stale_after
        assert c.post("/api/push/register", json={"token": TOKEN}).status_code == 401
        assert c.post("/api/push/register", json={"token": TOKEN},
                      headers={"Authorization": "Bearer s3cret-token"}).status_code == 200


def test_no_token_means_open_like_the_board(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    with client_for(s) as c:
        assert c.get("/api/status").status_code == 200
        # a bearer token when none is configured is refused rather than ignored
        assert c.get("/api/status", headers={"Authorization": "Bearer x"}).status_code == 401


def test_admin_watch_page(monkeypatch, tmp_path, ec_key):
    import re
    s = make_settings(monkeypatch, tmp_path, ADMIN_PASSWORD="admin")
    with client_for(s) as c:
        assert c.get("/admin/watch", follow_redirects=False).status_code in (302, 303, 307)
        page = c.get("/login").text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"csrf": csrf, "password": "admin", "next": "/admin/watch"})
        page = c.get("/admin/watch").text
        assert "Apple Watch" in page and "Still needed" in page
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        c.post("/admin/watch", data={"csrf": csrf, "action": "upload"},
               files={"key": ("AuthKey_ABC123DEFG.p8", pem(ec_key).encode())})
        assert s.apns_key_file.exists()
        c.post("/api/push/register", json={"token": TOKEN, "name": "Thomas's Watch"})
        page = c.get("/admin/watch").text
        assert "Thomas&#39;s Watch" in page and "Send a test notification" in page
        r = c.post("/admin/watch", data={"csrf": csrf, "action": "upload"},
                   files={"key": ("bad.p8", b"hello")})
        assert "isn&#39;t an APNs key" in r.text  # shown on the page the redirect lands on
