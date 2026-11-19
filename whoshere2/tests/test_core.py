import time

import pytest

from whoshere import presence
from whoshere.db import Database
from whoshere.importer import import_v1, parse_v1
from whoshere.macutil import is_randomized, normalize_mac
from whoshere.unifi import (ClientInfo, IntegrationClient, NetDevice, SessionClient, Snapshot, UniFiError,
                            find_client_by_ip)

from mock_unifi import serve

AP1, AP2 = "b7:c5:21:08:82:32", "b6:98:4e:f7:a1:ca"
PHONE = "90:83:3e:7d:d3:6b"
GRACE = 600


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def add_person(db, name, *macs):
    pid = db.run("INSERT INTO people (name, created_at) VALUES (?, ?)", (name, time.time()))
    for m in macs:
        db.run("INSERT INTO devices (mac, person_id) VALUES (?, ?)", (m, pid))
    return pid


def snap(online: dict, t: float):
    return Snapshot(
        clients=[ClientInfo(mac=m, name="phone", uplink_mac=ap) for m, ap in online.items()],
        devices=[NetDevice(AP1, "Sound Booth", "ap"), NetDevice(AP2, "Bullpen", "ap")],
        taken_at=t)


def events(db):
    return [(r["kind"], r["at"]) for r in db.all("SELECT kind, at FROM events ORDER BY id")]


# ---------------------------------------------------------------- MACs

def test_normalize_mac():
    assert normalize_mac("90-83-3E-7D-D3-6B") == PHONE
    assert normalize_mac("9083.3e7d.d36b") == PHONE
    assert normalize_mac("90833e7dd36b") == PHONE
    assert normalize_mac("nope") is None
    assert normalize_mac("") is None


def test_randomized_detection():
    assert is_randomized("da:11:22:33:44:55")  # 'a' -> locally administered
    assert is_randomized("02:00:00:00:00:00")
    assert not is_randomized(PHONE)


# ---------------------------------------------------------------- presence rules

def test_arrival_then_departure_after_grace(db):
    pid = add_person(db, "Steve", PHONE)
    t0 = 1_000_000.0
    presence.apply_snapshot(db, snap({}, t0), GRACE)
    assert db.one("SELECT state FROM presence")["state"] == "away"

    presence.apply_snapshot(db, snap({PHONE: AP1}, t0 + 60), GRACE)
    b = presence.board(db, stale_after=300, now=t0 + 61)
    assert b["verdict"] == "occupied"
    assert b["here"][0]["location"] == "Sound Booth"
    assert events(db) == [("arrive", t0 + 60)]

    # Phone sleeps for 5 minutes: still here, flagged as maybe leaving, no event
    presence.apply_snapshot(db, snap({}, t0 + 120), GRACE)
    presence.apply_snapshot(db, snap({}, t0 + 420), GRACE)
    b = presence.board(db, stale_after=300, now=t0 + 421)
    assert b["verdict"] == "occupied" and b["here"][0]["leaving"]
    presence.apply_snapshot(db, snap({PHONE: AP2}, t0 + 480), GRACE)  # back, roamed to Bullpen
    b = presence.board(db, stale_after=300, now=t0 + 481)
    assert b["here"][0]["location"] == "Bullpen" and not b["here"][0]["leaving"]
    assert len(events(db)) == 1

    # Really leaves: departure is recorded at last-seen time once grace passes
    presence.apply_snapshot(db, snap({}, t0 + 540), GRACE)
    presence.apply_snapshot(db, snap({}, t0 + 540 + GRACE - 1), GRACE)
    assert len(events(db)) == 1
    presence.apply_snapshot(db, snap({}, t0 + 540 + GRACE), GRACE)
    assert events(db)[-1] == ("depart", t0 + 480)
    assert presence.board(db, 300, now=t0 + 540 + GRACE + 1)["verdict"] == "clear"


def test_outage_does_not_count_as_absence(db):
    add_person(db, "Steve", PHONE)
    t0 = 2_000_000.0
    presence.apply_snapshot(db, snap({PHONE: AP1}, t0), GRACE)
    # UniFi unreachable for an hour; the next good poll happens to miss the dozing phone
    presence.apply_snapshot(db, snap({}, t0 + 3600), GRACE)
    assert db.one("SELECT state FROM presence")["state"] == "here"
    assert events(db) == []


def test_stale_data_never_says_all_clear(db):
    add_person(db, "Steve", PHONE)
    t0 = 3_000_000.0
    presence.apply_snapshot(db, snap({}, t0), GRACE)
    assert presence.board(db, stale_after=180, now=t0 + 60)["verdict"] == "clear"
    assert presence.board(db, stale_after=180, now=t0 + 181)["verdict"] == "stale"
    presence.record_failure(db, "boom", now=t0 + 200)
    b = presence.board(db, stale_after=180, now=t0 + 201)
    assert b["verdict"] == "stale" and b["last_error"] == "boom"


def test_unknown_ap_still_counts_as_here(db):
    add_person(db, "Steve", PHONE)
    presence.apply_snapshot(db, snap({PHONE: "11:22:33:44:55:66"}, 4_000_000.0), GRACE)
    b = presence.board(db, 300, now=4_000_001.0)
    assert b["verdict"] == "occupied" and b["here"][0]["location"] == "Unknown location"


def test_untracked_people_reported_but_not_blocking(db):
    add_person(db, "No Phone")
    presence.apply_snapshot(db, snap({}, 5_000_000.0), GRACE)
    b = presence.board(db, 300, now=5_000_001.0)
    assert b["verdict"] == "clear" and [p["name"] for p in b["untracked"]] == ["No Phone"]


def test_alias_overrides_unifi_name(db):
    add_person(db, "Steve", PHONE)
    presence.apply_snapshot(db, snap({PHONE: AP1}, 6_000_000.0), GRACE)
    db.run("UPDATE locations SET alias = 'Booth' WHERE mac = ?", (AP1,))
    assert presence.board(db, 300, now=6_000_001.0)["here"][0]["location"] == "Booth"


def test_catalog_records_all_clients(db):
    presence.apply_snapshot(db, snap({PHONE: AP1, "aa:aa:aa:aa:aa:aa": AP2}, 7_000_000.0), GRACE)
    presence.apply_snapshot(db, snap({PHONE: AP1}, 7_000_060.0), GRACE)
    rows = {r["mac"]: r["connected"] for r in db.all("SELECT mac, connected FROM devices")}
    assert rows == {PHONE: 1, "aa:aa:aa:aa:aa:aa": 0}


def test_prune_keeps_assigned_devices(db):
    add_person(db, "Steve", PHONE)
    old = time.time() - 90 * 86400
    presence.apply_snapshot(db, snap({PHONE: AP1, "aa:aa:aa:aa:aa:aa": AP2}, old), GRACE)
    presence.apply_snapshot(db, snap({}, old + 60), GRACE)
    presence.prune(db, history_days=30, device_retention_days=30)
    assert [r["mac"] for r in db.all("SELECT mac FROM devices")] == [PHONE]
    assert db.all("SELECT * FROM events") == []


# ---------------------------------------------------------------- v1 import

def test_import_v1(db):
    text = "Steve Marcus  90:83:3e:7d:d3:6b\nDavid Wells\t28-DE-CF-88-F7-A2\n\nbad line\nJenny Nitzel  09:51:dc:e1:33:cf\n"
    entries, problems = parse_v1(text)
    assert len(entries) == 3 and len(problems) == 1
    added, assigned, _ = import_v1(db, text)
    assert (added, assigned) == (3, 3)
    added, assigned, _ = import_v1(db, text)  # idempotent
    assert added == 0
    assert db.one("SELECT COUNT(*) AS n FROM people")["n"] == 3


# ---------------------------------------------------------------- UniFi clients against the mock

@pytest.fixture(scope="module")
def mock():
    import urllib.request, json
    srv = serve(0)
    port = srv.server_address[1]
    def set_online(d):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/_mock/online", data=json.dumps(d).encode(), method="POST")
        urllib.request.urlopen(req).read()
    yield f"http://127.0.0.1:{port}", set_online
    srv.shutdown()


def test_integration_client(mock):
    url, set_online = mock
    set_online({PHONE: AP1, "3c:22:fb:10:20:30": "0c:ea:14:00:00:01"})
    s = IntegrationClient(url, "test-key", "default", False).snapshot()
    by = {c.mac: c for c in s.clients}
    assert set(by) == {PHONE, "3c:22:fb:10:20:30"}  # VPN client without MAC skipped
    assert by[PHONE].uplink_mac == AP1 and by[PHONE].name == "Alexs-iPhone"
    assert by["3c:22:fb:10:20:30"].wired
    assert {d.name for d in s.devices} >= {"Sound Booth", "Office Switch"}


def test_integration_bad_key(mock):
    url, _ = mock
    with pytest.raises(UniFiError, match="API key"):
        IntegrationClient(url, "wrong", "default", False).snapshot()


def test_integration_unknown_site(mock):
    url, _ = mock
    with pytest.raises(UniFiError, match="Sites this key can see"):
        IntegrationClient(url, "test-key", "campus2", False).snapshot()


def test_session_client(mock):
    url, set_online = mock
    set_online({PHONE: AP2})
    c = SessionClient(url, "whoshere", "secret", "default", False)
    s = c.snapshot()
    assert [(x.mac, x.uplink_mac, x.name) for x in s.clients] == [(PHONE, AP2, "Alexs-iPhone")]
    c.csrf = "stale"  # simulate expired session -> re-login transparently
    assert len(c.snapshot().clients) == 1


def test_session_bad_password(mock):
    url, _ = mock
    with pytest.raises(UniFiError, match="login failed"):
        SessionClient(url, "whoshere", "nope", "default", False).snapshot()


def test_unreachable():
    with pytest.raises(UniFiError, match="Can't reach"):
        IntegrationClient("http://127.0.0.1:9", "k", "default", False).snapshot()


# ---------------------------------------------------------------- self-registration

def test_client_ips_and_lookup(mock):
    url, set_online = mock
    set_online({PHONE: AP1, "f0:18:98:aa:bb:cc": AP2})
    for client in (IntegrationClient(url, "test-key", "default", False),
                   SessionClient(url, "whoshere", "secret", "default", False)):
        s = client.snapshot()
        assert find_client_by_ip(s, "10.0.0.107").mac == PHONE
        assert find_client_by_ip(s, "::ffff:10.0.0.204").mac == "f0:18:98:aa:bb:cc"
        assert find_client_by_ip(s, "10.0.0.99") is None
        assert find_client_by_ip(s, None) is None


def test_register_page_flow(mock, tmp_path, monkeypatch):
    """Runs the real app and walks the self-register page as a phone at 127.0.0.1."""
    import http.cookiejar, json, re, threading, urllib.parse, urllib.request
    import uvicorn
    from whoshere.config import Settings
    from whoshere.web import create_app

    url, set_online = mock
    def set_ips(d):
        req = urllib.request.Request(url + "/_mock/ips", data=json.dumps(d).encode(), method="POST")
        urllib.request.urlopen(req).read()

    for k, v in {"DATA_DIR": str(tmp_path), "UNIFI_URL": url, "UNIFI_API_KEY": "test-key",
                 "ADMIN_PASSWORD": "admin", "SELF_REGISTER": "true", "REGISTER_CODE": "ironwood",
                 "STAFF_WIFI_NAME": "Ironwood Staff"}.items():
        monkeypatch.setenv(k, v)
    settings = Settings.from_env()
    app = create_app(settings, start_poller=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get():
        return opener.open(base + "/register").read().decode()

    def post(**fields):
        page = get()
        fields["csrf"] = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        return opener.open(base + "/register", data=urllib.parse.urlencode(fields).encode()).read().decode()

    try:
        private = "da:11:22:33:44:55"
        set_online({private: AP1})
        set_ips({private: "127.0.0.1"})
        assert "private Wi-Fi address" in get() and "Ironwood Staff" in get()

        set_online({PHONE: AP1})
        set_ips({})
        assert "wasn't found" in get()

        set_ips({PHONE: "127.0.0.1"})
        assert "Alexs-iPhone" in get() and "Registration code" in get()
        assert "That registration code" in post(name="Alex Rivera", code="wrong")
        assert db_people(settings) == []

        done = post(name="  Alex   Rivera ", code="ironwood")
        assert "registered to <strong>Alex Rivera</strong>" in done
        assert db_people(settings) == [("Alex Rivera", PHONE)]
        assert "already registered to <strong>Alex Rivera</strong>" in get()

        # A second phone with the same name (any case) joins the existing person
        set_online({"f0:18:98:aa:bb:cc": AP2})
        set_ips({"f0:18:98:aa:bb:cc": "127.0.0.1"})
        post(name="alex rivera", code="ironwood")
        assert sorted(db_people(settings)) == [("Alex Rivera", PHONE), ("Alex Rivera", "f0:18:98:aa:bb:cc")]
    finally:
        set_ips({})
        server.should_exit = True
        t.join(5)


def db_people(settings):
    return [(r["name"], r["mac"]) for r in Database(settings.db_path).all(
        "SELECT p.name, d.mac FROM people p JOIN devices d ON d.person_id = p.id ORDER BY d.mac")]


def test_changelog_matches_version():
    from whoshere import __version__
    from whoshere.changelog import CHANGELOG
    assert CHANGELOG[0]["version"] == __version__, "add a CHANGELOG entry for the new version"
    assert all(c["changes"] for c in CHANGELOG)


# ---------------------------------------------------------------- export / import

def _seed_site(db):
    t0 = 8_000_000.0
    a = add_person(db, "Alex Rivera", PHONE)
    add_person(db, "Sam Ortiz", "28:de:cf:88:f7:a2")
    add_person(db, "No Phone")
    presence.apply_snapshot(db, snap({PHONE: AP1, "3c:22:fb:10:20:30": AP2}, t0), GRACE)
    db.run("INSERT INTO events (person_id, kind, at, uplink, device_mac) VALUES (?, 'arrive', ?, ?, ?)",
           (a, t0 - 3600, AP1, PHONE))
    db.run("INSERT INTO events (person_id, kind, at, uplink) VALUES (?, 'depart', ?, ?)", (a, t0 - 600, AP2))
    db.run("UPDATE locations SET alias = 'Sanctuary' WHERE mac = ?", (AP1,))
    db.run("UPDATE devices SET ignored = 1 WHERE mac = '3c:22:fb:10:20:30'")
    db.run("UPDATE people SET active = 0 WHERE name = 'No Phone'")
    return a


def test_export_import_roundtrip(tmp_path):
    import json
    from whoshere.backup import export_data, import_text
    old = Database(tmp_path / "old.db")
    _seed_site(old)
    data = export_data(old, include_history=True)
    assert data["format"] == "whoshere-export" and len(data["people"]) == 3
    assert "history" in data and len(data["history"]) >= 1

    new = Database(tmp_path / "new.db")
    r = import_text(new, json.dumps(data))
    assert (r.people_added, r.devices_assigned, r.locations_named) == (3, 2, 1)
    assert r.events_added == len(data["history"])
    people = {p["name"]: p for p in new.all("SELECT * FROM people")}
    assert people["No Phone"]["active"] == 0
    owner = new.one("SELECT p.name FROM devices d JOIN people p ON p.id = d.person_id WHERE d.mac = ?", (PHONE,))
    assert owner["name"] == "Alex Rivera"
    assert new.one("SELECT alias FROM locations WHERE mac = ?", (AP1,))["alias"] == "Sanctuary"
    assert new.one("SELECT ignored FROM devices WHERE mac = '3c:22:fb:10:20:30'")["ignored"] == 1

    # Importing again changes nothing
    r2 = import_text(new, json.dumps(data))
    assert r2.people_added == 0 and r2.events_added == 0
    assert new.one("SELECT COUNT(*) AS n FROM people")["n"] == 3


def test_export_without_history(db):
    from whoshere.backup import export_data
    _seed_site(db)
    assert "history" not in export_data(db)


def test_import_merge_keeps_existing_and_reports_conflicts(db):
    import json
    from whoshere.backup import import_text
    add_person(db, "Already Here", "aa:aa:aa:aa:aa:01")
    add_person(db, "Owner", PHONE)
    data = {"format": "whoshere-export", "format_version": 1, "people": [
        {"id": 1, "name": "alex rivera", "devices": [{"mac": PHONE}]},
        {"id": 2, "name": "Owner", "devices": [{"mac": "90-83-3E-7D-D3-6B"}]},
    ]}
    r = import_text(db, json.dumps(data))
    assert r.people_added == 1 and r.people_updated == 1
    assert any("already belongs to Owner" in p for p in r.problems)
    assert db.one("SELECT COUNT(*) AS n FROM people")["n"] == 3


def test_import_replace_wipes_first(db):
    import json
    from whoshere.backup import import_text
    _seed_site(db)
    data = {"format": "whoshere-export", "format_version": 1,
            "people": [{"id": 9, "name": "Only One", "devices": [{"mac": "f4:0f:24:11:22:33"}]}]}
    import_text(db, json.dumps(data), replace=True)
    assert [r["name"] for r in db.all("SELECT name FROM people")] == ["Only One"]
    assert db.all("SELECT * FROM events") == []
    assert db.one("SELECT alias FROM locations WHERE mac = ?", (AP1,))["alias"] is None
    assert db.one("SELECT person_id FROM devices WHERE mac = ?", (PHONE,))["person_id"] is None


def test_import_detects_v1_and_rejects_junk(db):
    from whoshere.backup import ImportError_, import_text
    r = import_text(db, "﻿Alex Rivera  90:83:3e:7d:d3:6b\n")
    assert r.people_added == 1
    for bad in ["", "{not json", '{"format": "other"}',
                '{"format": "whoshere-export", "format_version": 99, "people": []}']:
        with pytest.raises(ImportError_):
            import_text(db, bad)
