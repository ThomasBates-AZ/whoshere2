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


# ---------------------------------------------------------------- Wi-Fi networks

def _set(url, what, body):
    import json, urllib.request
    req = urllib.request.Request(f"{url}/_mock/{what}", data=json.dumps(body).encode(), method="POST")
    urllib.request.urlopen(req).read()


def test_integration_client_adds_wifi_names(mock):
    url, set_online = mock
    set_online({PHONE: AP1, "28:de:cf:88:f7:a2": AP2, "3c:22:fb:10:20:30": "0c:ea:14:00:00:01"})
    _set(url, "ssids", {"28:de:cf:88:f7:a2": "Ironwood Office"})
    try:
        s = IntegrationClient(url, "test-key", "default", False).snapshot()
        by = {c.mac: c for c in s.clients}
        assert s.ssid_ok and by[PHONE].ssid == "Ironwood Staff"
        assert by["28:de:cf:88:f7:a2"].ssid == "Ironwood Office"
        assert by["3c:22:fb:10:20:30"].ssid is None  # wired
    finally:
        _set(url, "ssids", {})


def test_wifi_names_refused_keeps_working(mock):
    url, set_online = mock
    set_online({PHONE: AP1})
    _set(url, "legacy_key", {"ok": False})
    try:
        c = IntegrationClient(url, "test-key", "default", False)
        s = c.snapshot()
        assert [x.mac for x in s.clients] == [PHONE] and s.clients[0].ssid is None
        assert not s.ssid_ok and "Wi-Fi network names" in s.ssid_note
        # doesn't hammer the console: next poll skips the lookup until the retry time
        _set(url, "legacy_key", {"ok": True})
        assert not c.snapshot().ssid_ok
        c._ssid_retry_at = 0
        assert c.snapshot().clients[0].ssid == "Ironwood Staff"
    finally:
        _set(url, "legacy_key", {"ok": True})


def test_wifi_lookup_off_when_not_wanted(mock):
    url, set_online = mock
    set_online({PHONE: AP1})
    s = IntegrationClient(url, "test-key", "default", False, want_ssid=False).snapshot()
    assert s.ssid_ok and s.clients[0].ssid is None


def test_session_client_reads_essid(mock):
    url, set_online = mock
    set_online({PHONE: AP2})
    s = SessionClient(url, "whoshere", "secret", "default", False).snapshot()
    assert s.clients[0].ssid == "Ironwood Staff"


def test_presence_records_wifi(db):
    add_person(db, "Alex", PHONE)
    t0 = 9_000_000.0
    presence.apply_snapshot(db, snap({}, t0), GRACE)
    s = snap({PHONE: AP1}, t0 + 60)
    s.clients = [ClientInfo(mac=PHONE, name="p", uplink_mac=AP1, ssid="Ironwood Office")]
    presence.apply_snapshot(db, s, GRACE)
    b = presence.board(db, 300, now=t0 + 61)
    assert b["here"][0]["wifi"] == "Ironwood Office"
    assert db.one("SELECT ssid FROM events WHERE kind = 'arrive'")["ssid"] == "Ironwood Office"
    assert db.one("SELECT last_ssid FROM devices WHERE mac = ?", (PHONE,))["last_ssid"] == "Ironwood Office"
    # a poll without names (lookup refused) keeps the device's last known network
    s2 = snap({PHONE: AP1}, t0 + 120)
    s2.ssid_ok = False
    presence.apply_snapshot(db, s2, GRACE)
    assert db.one("SELECT last_ssid FROM devices WHERE mac = ?", (PHONE,))["last_ssid"] == "Ironwood Office"


def test_existing_database_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE devices (mac TEXT PRIMARY KEY, person_id INTEGER, client_name TEXT, first_seen REAL,
            last_seen REAL, last_uplink TEXT, connected INTEGER NOT NULL DEFAULT 0, ignored INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE presence (person_id INTEGER PRIMARY KEY, state TEXT NOT NULL, since REAL, last_seen REAL,
            uplink TEXT, missing_since REAL);
        CREATE TABLE events (id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL,
            uplink TEXT, device_mac TEXT);
        INSERT INTO devices (mac) VALUES ('90:83:3e:7d:d3:6b');
    """)
    conn.commit(); conn.close()
    db = Database(path)
    assert db.one("SELECT last_ssid FROM devices")["last_ssid"] is None
    Database(path)  # second start is a no-op


def test_watched_wifi_setting(monkeypatch, tmp_path):
    from whoshere.config import Settings
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WIFI_NETWORKS", " Ironwood Staff , Ironwood Office,Ironwood Staff ")
    monkeypatch.delenv("STAFF_WIFI_NAME", raising=False)
    s = Settings.from_env()
    assert s.wifi_networks == ("Ironwood Staff", "Ironwood Office")
    assert s.staff_wifi == "Ironwood Staff or Ironwood Office"
    assert s.is_watched_wifi("ironwood office") and not s.is_watched_wifi("Ironwood Guest")
    assert not s.is_watched_wifi(None)
    assert s.show_wifi_network
    monkeypatch.setenv("SHOW_WIFI_NETWORK", "false")
    monkeypatch.setenv("WIFI_NETWORKS", "")
    s = Settings.from_env()
    assert not s.show_wifi_network and s.is_watched_wifi("anything")


def test_two_networks_register_and_devices(mock, tmp_path, monkeypatch):
    """Runs the real app with WIFI_NETWORKS set: registration refuses the guest network,
    the Devices page shows only phones on the staff networks, the board shows the network."""
    import http.cookiejar, json, re, threading, urllib.parse, urllib.request
    import uvicorn
    from whoshere.config import Settings
    from whoshere.web import create_app

    url, set_online = mock
    staff2, guest = "28:de:cf:88:f7:a2", "f4:0f:24:11:22:33"
    for k, v in {"DATA_DIR": str(tmp_path), "UNIFI_URL": url, "UNIFI_API_KEY": "test-key",
                 "ADMIN_PASSWORD": "admin", "SELF_REGISTER": "true",
                 "WIFI_NETWORKS": "Ironwood Staff, Ironwood Office"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STAFF_WIFI_NAME", raising=False)
    monkeypatch.delenv("REGISTER_CODE", raising=False)
    settings = Settings.from_env()
    app = create_app(settings, start_poller=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    get = lambda path: opener.open(base + path).read().decode()

    try:
        set_online({PHONE: AP1, staff2: AP2, guest: AP2, "3c:22:fb:10:20:30": "0c:ea:14:00:00:01"})
        _set(url, "ssids", {staff2: "Ironwood Office", guest: "Ironwood Guest"})

        # Registration from the guest network is refused and names both staff networks
        _set(url, "ips", {guest: "127.0.0.1"})
        page = get("/register")
        assert "connected to <strong>Ironwood Guest</strong>" in page
        assert "<strong>Ironwood Staff</strong> or <strong>Ironwood Office</strong>" in page
        # ...but works from the second staff network
        _set(url, "ips", {staff2: "127.0.0.1"})
        assert "Add my phone</button>" in get("/register")

        # Devices page: staff networks only by default, everything with net=all
        app.state.poller.poll_once()
        login = get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', login).group(1)
        opener.open(base + "/login", data=urllib.parse.urlencode({"csrf": csrf, "password": "admin"}).encode())
        page = get("/admin/devices")
        assert PHONE in page and staff2 in page
        assert guest not in page and "3c:22:fb:10:20:30" not in page
        assert "Show all networks" in page and "Ironwood Office" in page
        page = get("/admin/devices?show=unassigned&net=all")
        assert guest in page and "3c:22:fb:10:20:30" in page

        # Board shows the network next to the location
        pid = add_person(app.state.db, "Sam Ortiz")
        app.state.db.run("UPDATE devices SET person_id = ? WHERE mac = ?", (pid, staff2))
        app.state.poller.poll_once()
        assert '<span class="wifi">Ironwood Office</span>' in get("/")
        status = json.loads(get("/api/status"))
        assert status["on_site"][0]["wifi"] == "Ironwood Office"
        assert "Ironwood Staff, Ironwood Office" in get("/admin/status")
    finally:
        server.should_exit = True
        _set(url, "ssids", {})
        _set(url, "ips", {})


def test_export_import_keeps_wifi_in_history(db):
    from whoshere.backup import export_data, import_text
    import json
    pid = add_person(db, "Alex", PHONE)
    db.run("INSERT INTO events (person_id, kind, at, uplink, ssid) VALUES (?, 'arrive', 1, ?, 'Ironwood Office')",
           (pid, AP1))
    data = export_data(db, include_history=True)
    assert data["history"][0]["wifi"] == "Ironwood Office"
    other = Database(db.path + ".2")
    import_text(other, json.dumps(data))
    assert other.one("SELECT ssid FROM events")["ssid"] == "Ironwood Office"


# ---------------------------------------------------------------- Docker address check

def test_default_gateway_and_masking(tmp_path):
    from whoshere.netcheck import default_gateway, masked_by_docker
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n")
    assert default_gateway(str(route)) == "172.17.0.1"
    assert default_gateway(str(tmp_path / "missing")) is None
    assert masked_by_docker("172.17.0.1", gateway="172.17.0.1", in_container=True)
    assert masked_by_docker("::ffff:172.17.0.1", gateway="172.17.0.1", in_container=True)
    assert not masked_by_docker("192.168.1.50", gateway="172.17.0.1", in_container=True)
    assert not masked_by_docker("172.17.0.1", gateway="172.17.0.1", in_container=False)


# ---------------------------------------------------------------- Google Chat

def _chat_settings(monkeypatch, tmp_path, **extra):
    from whoshere.config import Settings
    env = {"DATA_DIR": str(tmp_path), "GOOGLE_CHAT_SUBSCRIPTION": "projects/whoshere-test/subscriptions/whoshere-chat",
           "TZ": "America/Phoenix", "STALE_AFTER_SECONDS": "300", "POLL_INTERVAL_SECONDS": "60"}
    env.update(extra)
    for k in ("GOOGLE_CHAT_ALLOWED", "BOARD_URL", "WIFI_NETWORKS", "GOOGLE_CHAT_PRIVATE_REPLIES", "GOOGLE_CHAT_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def _campus(db, now):
    """Alex here in the Sound Booth, Sam left this morning, Jordan untracked, Thomas never seen."""
    alex = add_person(db, "Alex Rivera", PHONE)
    sam = add_person(db, "Sam Ortiz", "28:de:cf:88:f7:a2")
    add_person(db, "Jordan Lee")
    add_person(db, "Thomas Bates", "f4:0f:24:11:22:33")
    add_person(db, "Thomas Nguyen", "f4:0f:24:11:22:44")
    s = snap({PHONE: AP1}, now - 30)
    s.clients = [ClientInfo(mac=PHONE, name="p", uplink_mac=AP1, ssid="Ironwood Staff")]
    presence.apply_snapshot(db, s, GRACE)
    db.run("INSERT INTO events (person_id, kind, at, uplink) VALUES (?, 'arrive', ?, ?)", (sam, now - 3600, AP2))
    db.run("INSERT INTO events (person_id, kind, at, uplink) VALUES (?, 'depart', ?, ?)", (sam, now - 1800, AP2))
    return alex, sam


def test_parse_event_formats():
    from whoshere.chatbot import parse_event
    from mock_google import addon_command_event, dm_event, slash_event
    r = parse_event(slash_event(1, "/whoshere Thomas", " Thomas", space_type="SPACE"))
    assert (r.kind, r.command_id, r.text, r.space, r.space_type) == ("message", 1, "Thomas", "spaces/AAA", "SPACE")
    assert r.user_email == "thomas@ironwood.church" and r.thread == "spaces/AAA/threads/T1"
    # annotation only (no message.slashCommand)
    e = slash_event(2, "/allclear", "")
    del e["message"]["slashCommand"]
    assert parse_event(e).command_id == 2
    # command recognised by name when IDs are missing
    e = dm_event("/history Sam")
    r = parse_event(e)
    assert (r.command_id, r.text) == (3, "Sam")
    # Workspace add-on format
    r = parse_event(addon_command_event(1, "/whoshere Alex"))
    assert (r.command_id, r.text, r.space, r.space_type) == (1, "Alex", "spaces/BBB", "SPACE")
    # added / removed
    assert parse_event({"type": "ADDED_TO_SPACE", "space": {"name": "spaces/C"}}).kind == "added"
    assert parse_event({"type": "REMOVED_FROM_SPACE", "space": {"name": "spaces/C"}}).kind == "removed"


def test_chat_replies(db, monkeypatch, tmp_path):
    from whoshere.chatbot import parse_event, reply_for
    from mock_google import dm_event, slash_event
    s = _chat_settings(monkeypatch, tmp_path, BOARD_URL="http://whoshere.local:8085/")
    from datetime import datetime
    now = datetime.now(s.timezone).replace(hour=12, minute=0, second=0).timestamp()  # midday, so "today" is stable
    _campus(db, now)
    ask = lambda ev: reply_for(parse_event(ev), db, s, now=now)

    out = ask(slash_event(1, "/whoshere", ""))
    assert "Not clear — 1 person on campus" in out
    assert "*Alex Rivera* — Sound Booth · Ironwood Staff" in out
    assert "Not tracked" in out and "Jordan Lee" in out
    assert out.endswith("\nBoard: http://whoshere.local:8085")
    assert "|" not in out

    out = ask(slash_event(1, "/whoshere thomas", " thomas"))
    assert "*Thomas Bates*" in out and "*Thomas Nguyen*" in out and "Alex" not in out
    out = ask(slash_event(1, "/whoshere alex", " alex"))
    assert out.startswith("🟢 *Alex Rivera*")
    assert "Nobody on the board matches “Zed”" in ask(slash_event(1, "/whoshere Zed", " Zed"))

    out = ask(slash_event(2, "/allclear", ""))
    assert "Not clear" in out and "Alex Rivera" in out and "aren't tracked: Jordan Lee" in out

    out = ask(slash_event(3, "/history", ""))
    assert "*Sam Ortiz* arrived" in out and "*Sam Ortiz* left" in out
    assert "for Sam Ortiz" in ask(slash_event(3, "/history sam", " sam"))
    assert "No arrivals or departures today for Alex Rivera" in ask(slash_event(3, "/history alex", " alex"))

    # plain DM text
    assert "Not clear" in ask(dm_event("all clear?"))
    assert "*Alex Rivera*" in ask(dm_event("who's here"))
    assert "/allclear" in ask(dm_event("help"))
    assert "Thomas Bates" in ask(dm_event("Thomas"))


def test_chat_all_clear_and_stale(db, monkeypatch, tmp_path):
    from whoshere.chatbot import parse_event, reply_for
    from mock_google import slash_event
    s = _chat_settings(monkeypatch, tmp_path)
    now = time.time()
    add_person(db, "Alex Rivera", PHONE)
    presence.apply_snapshot(db, snap({}, now - 10), GRACE)
    out = reply_for(parse_event(slash_event(2, "/allclear", "")), db, s, now=now)
    assert out.startswith("🟢 *All clear to arm.*")
    out = reply_for(parse_event(slash_event(2, "/allclear", "")), db, s, now=now + 3600)
    assert "Can't confirm" in out and "Walk the building" in out


def test_chat_allowlist(db, monkeypatch, tmp_path):
    from whoshere.chatbot import parse_event, reply_for
    from mock_google import slash_event
    s = _chat_settings(monkeypatch, tmp_path, GOOGLE_CHAT_ALLOWED="@ironwood.church, guest@gmail.com")
    ok = reply_for(parse_event(slash_event(2, "/allclear", "")), db, s)
    assert "not on the list" not in ok
    out = reply_for(parse_event(slash_event(2, "/allclear", "", email="someone@else.org")), db, s)
    assert "not on the list" in out
    assert "not on the list" not in reply_for(parse_event(slash_event(2, "/allclear", "", email="Guest@Gmail.com")), db, s)


def test_chat_bridge_end_to_end(db, monkeypatch, tmp_path):
    import json
    from whoshere.gchat import ChatBridge, ChatSetupError
    from mock_google import SUBSCRIPTION, FakeGoogle, dm_event, slash_event
    g = FakeGoogle()
    try:
        s = _chat_settings(monkeypatch, tmp_path)
        bridge = ChatBridge(s, db, pubsub_url=g.url, chat_url=g.url, token_url=g.url + "/token")
        # no key yet
        with pytest.raises(ChatSetupError, match="No service account key"):
            bridge.pull_once()
        s.chat_key_file.write_text(json.dumps(g.key))
        bridge.reset()
        _campus(db, time.time())

        g.queue += [slash_event(1, "/whoshere", "", space_type="SPACE", space="spaces/TEAM",
                                msg="spaces/TEAM/messages/1"),
                    dm_event("all clear?"),
                    dm_event("all clear?")]  # same message delivered twice
        assert bridge.pull_once() == 3
        assert len(g.acked) == 3
        assert len(g.sent) == 2  # the duplicate isn't answered twice
        team, dm = g.sent
        assert team["space"] == "spaces/TEAM" and "Alex Rivera" in team["body"]["text"]
        assert team["body"]["privateMessageViewer"] == {"name": "users/111"}  # shared space: private
        assert team["body"]["thread"]["name"] == "spaces/TEAM/threads/T1"
        assert team["query"]["messageReplyOption"] == ["REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"]
        assert "privateMessageViewer" not in dm["body"]  # DM: normal reply
        assert bridge.replies_sent == 2 and bridge.last_pull_ok

        # If Chat won't take a private reply, answer in the space instead
        g.reject_private = True
        g.queue.append(slash_event(2, "/allclear", "", space_type="SPACE", space="spaces/TEAM",
                                   msg="spaces/TEAM/messages/5"))
        bridge.pull_once()
        assert "privateMessageViewer" not in g.sent[-1]["body"] and "Not clear" in g.sent[-1]["body"]["text"]

        # Permission problem on the subscription is explained
        g.pull_status = 403
        with pytest.raises(ChatSetupError, match="Pub/Sub Subscriber"):
            bridge.pull_once()
        g.pull_status = 200

        # Bad key is explained
        bad = dict(g.key, private_key=__import__("mock_google").make_key()[0]["private_key"])
        s.chat_key_file.write_text(json.dumps(bad))
        bridge.reset()
        with pytest.raises(ChatSetupError, match="rejected the service account key"):
            bridge.pull_once()
    finally:
        g.close()


def test_chat_subscription_setting(monkeypatch, tmp_path):
    from whoshere.config import Settings
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CHAT_SUBSCRIPTION", "whoshere-chat")
    monkeypatch.setenv("GOOGLE_CHAT_PROJECT", "ironwood-whoshere")
    assert Settings.from_env().chat_subscription == "projects/ironwood-whoshere/subscriptions/whoshere-chat"
    monkeypatch.delenv("GOOGLE_CHAT_PROJECT")
    with pytest.raises(SystemExit):
        Settings.from_env()
    monkeypatch.setenv("GOOGLE_CHAT_SUBSCRIPTION", "")
    assert Settings.from_env().chat_subscription == ""


def test_help_sheets_and_person_history(tmp_path, monkeypatch):
    """Help page, both cheat sheets, and clicking a name through to that person's history."""
    import re, threading, urllib.request
    import uvicorn
    from whoshere.config import Settings
    from whoshere.web import create_app
    for k, v in {"DATA_DIR": str(tmp_path), "UNIFI_URL": "http://127.0.0.1:9", "UNIFI_API_KEY": "k",
                 "ADMIN_PASSWORD": "admin", "SELF_REGISTER": "true",
                 "GOOGLE_CHAT_SUBSCRIPTION": "projects/p/subscriptions/s",
                 "BOARD_URL": "http://solomon.example.org:8085"}.items():
        monkeypatch.setenv(k, v)
    app = create_app(Settings.from_env(), start_poller=False)
    db = app.state.db
    t0 = time.time()
    sam = add_person(db, "Sam Ortiz", PHONE)
    add_person(db, "Alex Rivera", "28:de:cf:88:f7:a2")
    presence.apply_snapshot(db, snap({PHONE: AP1}, t0 - 5), GRACE)
    db.run("INSERT INTO events (person_id, kind, at, uplink) VALUES (?, 'arrive', ?, ?)", (sam, t0 - 3600, AP1))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    get = lambda p: urllib.request.urlopen(base + p).read().decode()
    try:
        page = get("/help")
        assert "Reading the board" in page and "Google Chat" in page and "/register" in page
        assert "http://solomon.example.org:8085" in page
        board_sheet = get("/help/sheet/board")
        assert "Quick guide" in board_sheet and "solomon.example.org:8085/register" in board_sheet
        chat_sheet = get("/help/sheet/chat")
        assert "/allclear" in chat_sheet and "Start a chat" in chat_sheet
        generic = get("/help/sheet/board?generic=1")
        assert "solomon" not in generic and 'class="blank"' in generic
        # name on the board links to that person's history
        board = get("/")
        link = re.search(r'href="(/history\?person=%d&amp;days=30)"' % sam, board).group(1).replace("&amp;", "&")
        hist = get(link)
        assert "<h1>Sam Ortiz</h1>" in hist and "Sound Booth" in hist and "Arrived" in hist
        assert "Alex Rivera</a>" not in hist
        assert 'href="/history?person=%d' % sam in get("/history")
    finally:
        server.should_exit = True
