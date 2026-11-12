"""Turns UniFi snapshots into who's here, arrivals and departures.

Rules:
* Someone is HERE as soon as any of their devices is connected.
* They only LEAVE after none of their devices has been seen for the grace period
  (DEPARTURE_GRACE_MINUTES). Phones drop Wi-Fi when they sleep, and roam between
  access points; without a grace period the history fills with fake comings and goings.
  The departure is recorded at the time they were last seen, not when the grace ran out.
* The grace period only counts time we were actually watching: if UniFi was
  unreachable for an hour, nobody is marked as gone just because of the outage.
* While someone is in their grace period they still count as here for "All Clear".
"""
from __future__ import annotations

import time

from .db import Database
from .unifi import ClientInfo, Snapshot


def apply_snapshot(db: Database, snap: Snapshot, grace: int, now: float | None = None) -> None:
    now = snap.taken_at if now is None else now
    with db.connect() as conn:
        for d in snap.devices:
            conn.execute(
                """INSERT INTO locations (mac, name, kind, last_seen) VALUES (?, ?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET name = excluded.name, kind = excluded.kind,
                                                  last_seen = excluded.last_seen""",
                (d.mac, d.name, d.kind, now))

        conn.execute("UPDATE devices SET connected = 0 WHERE connected = 1")
        connected: dict[str, ClientInfo] = {}
        for c in snap.clients:
            connected[c.mac] = c
            conn.execute(
                """INSERT INTO devices (mac, client_name, first_seen, last_seen, last_uplink, connected)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(mac) DO UPDATE SET
                       client_name = COALESCE(NULLIF(excluded.client_name, ''), devices.client_name),
                       last_seen = excluded.last_seen,
                       last_uplink = COALESCE(excluded.last_uplink, devices.last_uplink),
                       connected = 1""",
                (c.mac, c.name, now, now, c.uplink_mac))

        people = conn.execute("SELECT id FROM people WHERE active = 1").fetchall()
        for (person_id,) in people:
            devices = conn.execute(
                "SELECT mac, last_seen FROM devices WHERE person_id = ?", (person_id,)).fetchall()
            if not devices:
                continue
            online = [connected[d["mac"]] for d in devices if d["mac"] in connected]
            pres = conn.execute("SELECT * FROM presence WHERE person_id = ?", (person_id,)).fetchone()

            if online:
                # Prefer a wireless device for location: a phone moves with the person, a desk PC doesn't.
                best = sorted(online, key=lambda c: (c.wired, c.uplink_mac is None))[0]
                if pres is None:
                    # First time we've looked at this person; we don't know when they arrived.
                    conn.execute(
                        "INSERT INTO presence (person_id, state, since, last_seen, uplink) "
                        "VALUES (?, 'here', NULL, ?, ?)", (person_id, now, best.uplink_mac))
                elif pres["state"] == "away":
                    conn.execute(
                        "UPDATE presence SET state = 'here', since = ?, last_seen = ?, uplink = ?, "
                        "missing_since = NULL WHERE person_id = ?",
                        (now, now, best.uplink_mac, person_id))
                    conn.execute(
                        "INSERT INTO events (person_id, kind, at, uplink, device_mac) "
                        "VALUES (?, 'arrive', ?, ?, ?)", (person_id, now, best.uplink_mac, best.mac))
                else:
                    conn.execute(
                        "UPDATE presence SET last_seen = ?, uplink = ?, missing_since = NULL "
                        "WHERE person_id = ?", (now, best.uplink_mac, person_id))
                continue

            last_seen = max((d["last_seen"] for d in devices if d["last_seen"]), default=None)
            if pres is None:
                conn.execute(
                    "INSERT INTO presence (person_id, state, since, last_seen) VALUES (?, 'away', ?, ?)",
                    (person_id, last_seen, last_seen))
            elif pres["state"] == "here":
                missing_since = pres["missing_since"] or now
                if now - missing_since >= grace:
                    left_at = pres["last_seen"] or missing_since
                    conn.execute(
                        "UPDATE presence SET state = 'away', since = ?, missing_since = NULL "
                        "WHERE person_id = ?", (left_at, person_id))
                    conn.execute(
                        "INSERT INTO events (person_id, kind, at, uplink) VALUES (?, 'depart', ?, ?)",
                        (person_id, left_at, pres["uplink"]))
                elif pres["missing_since"] is None:
                    conn.execute("UPDATE presence SET missing_since = ? WHERE person_id = ?",
                                 (now, person_id))

        conn.execute(
            "UPDATE poll_status SET last_attempt = ?, last_success = ?, last_error = NULL, "
            "client_count = ? WHERE id = 1", (now, now, len(snap.clients)))


def record_failure(db: Database, error: str, now: float | None = None) -> None:
    db.run("UPDATE poll_status SET last_attempt = ?, last_error = ? WHERE id = 1",
           (now or time.time(), error))


def prune(db: Database, history_days: int, device_retention_days: int, now: float | None = None) -> None:
    now = now or time.time()
    with db.connect() as conn:
        conn.execute("DELETE FROM events WHERE at < ?", (now - history_days * 86400,))
        conn.execute("DELETE FROM devices WHERE person_id IS NULL AND connected = 0 AND last_seen < ?",
                     (now - device_retention_days * 86400,))


def location_names(db: Database) -> dict[str, str]:
    return {r["mac"]: (r["alias"] or r["name"] or r["mac"])
            for r in db.all("SELECT mac, name, alias FROM locations")}


def board(db: Database, stale_after: int, now: float | None = None) -> dict:
    """Everything the board (and /api/status) shows."""
    now = now or time.time()
    status = db.one("SELECT * FROM poll_status WHERE id = 1")
    names = location_names(db)

    rows = db.all(
        """SELECT p.id, p.name, pr.state, pr.since, pr.last_seen, pr.uplink, pr.missing_since,
                  (SELECT COUNT(*) FROM devices d WHERE d.person_id = p.id) AS device_count
           FROM people p LEFT JOIN presence pr ON pr.person_id = p.id
           WHERE p.active = 1""")

    people = []
    for r in rows:
        if r["device_count"] == 0:
            state = "untracked"
        elif r["state"] is None:
            state = "unknown"
        else:
            state = r["state"]
        people.append({
            "id": r["id"],
            "name": r["name"],
            "state": state,
            "location": names.get(r["uplink"], "Unknown location") if state == "here" and r["uplink"] else None,
            "since": r["since"],
            "last_seen": r["last_seen"],
            "leaving": state == "here" and r["missing_since"] is not None,
        })
    order = {"here": 0, "unknown": 1, "away": 2, "untracked": 3}
    people.sort(key=lambda p: (order[p["state"]], p["name"].lower()))

    last_success = status["last_success"] if status else None
    fresh = bool(last_success) and now - last_success <= stale_after
    here = [p for p in people if p["state"] in ("here", "unknown")]
    if not fresh:
        verdict = "stale"
    elif here:
        verdict = "occupied"
    else:
        verdict = "clear"

    return {
        "verdict": verdict,
        "here": here,
        "people": people,
        "untracked": [p for p in people if p["state"] == "untracked"],
        "last_success": last_success,
        "last_attempt": status["last_attempt"] if status else None,
        "last_error": status["last_error"] if status else None,
        "generated_at": now,
    }
