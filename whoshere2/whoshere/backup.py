"""Export and import people, their devices and location names, for moving to a new server.

The export is a JSON file:

    {
      "format": "whoshere-export", "format_version": 1, "app_version": "2.3.0",
      "exported_at": "2026-11-19T14:10:00-07:00",
      "people":    [{"id": 1, "name": "Alex Rivera", "active": true,
                     "devices": [{"mac": "90:83:3e:7d:d3:6b", "name": "Alexs-iPhone"}]}],
      "locations": [{"mac": "b7:c5:21:08:82:32", "name": "Sound Booth", "alias": "Sanctuary"}],
      "hidden_devices": ["3c:22:fb:10:20:30"],
      "history":   [{"person_id": 1, "kind": "arrive", "at": 1794520674.4,
                     "location_mac": "b7:c5:21:08:82:32", "device_mac": "90:83:3e:7d:d3:6b"}]
    }

"id"/"person_id" only link history to people inside the file; they are not database ids.
"history" is present only when the export included it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import __version__
from .db import Database
from .importer import import_v1
from .macutil import normalize_mac

FORMAT = "whoshere-export"
FORMAT_VERSION = 1


def export_data(db: Database, include_history: bool = False, tz=None) -> dict:
    people = db.all("SELECT id, name, active FROM people ORDER BY name COLLATE NOCASE")
    devices = db.all("SELECT mac, person_id, client_name FROM devices "
                     "WHERE person_id IS NOT NULL ORDER BY mac")
    by_person: dict[int, list] = {}
    for d in devices:
        by_person.setdefault(d["person_id"], []).append({"mac": d["mac"], "name": d["client_name"] or ""})

    data = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": __version__,
        "exported_at": datetime.now(tz).isoformat(timespec="seconds"),
        "people": [{"id": p["id"], "name": p["name"], "active": bool(p["active"]),
                    "devices": by_person.get(p["id"], [])} for p in people],
        "locations": [{"mac": r["mac"], "name": r["name"] or "", "alias": r["alias"]}
                      for r in db.all("SELECT mac, name, alias FROM locations ORDER BY mac")],
        "hidden_devices": [r["mac"] for r in db.all(
            "SELECT mac FROM devices WHERE ignored = 1 AND person_id IS NULL ORDER BY mac")],
    }
    if include_history:
        data["history"] = [
            {"person_id": e["person_id"], "kind": e["kind"], "at": e["at"],
             "location_mac": e["uplink"], "device_mac": e["device_mac"]}
            for e in db.all("SELECT person_id, kind, at, uplink, device_mac FROM events ORDER BY at")]
    return data


@dataclass
class ImportResult:
    people_added: int = 0
    people_updated: int = 0
    devices_assigned: int = 0
    locations_named: int = 0
    events_added: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        def n(count, one, many):
            return f"{count} {one if count == 1 else many}"
        parts = [n(self.people_added, "person", "people") + " added"]
        if self.people_updated:
            parts.append(f"{self.people_updated} already here")
        parts.append(n(self.devices_assigned, "device", "devices") + " assigned")
        if self.locations_named:
            parts.append(n(self.locations_named, "location name", "location names"))
        if self.events_added:
            parts.append(n(self.events_added, "history entry", "history entries"))
        return "Imported: " + ", ".join(parts) + "."


class ImportError_(ValueError):
    """The file isn't something we can import. Message is shown to the admin."""


def import_text(db: Database, text: str, replace: bool = False) -> ImportResult:
    """Import a Who's Here export (JSON) or a v1 users.txt, detected from the content.

    merge (default): people are matched by name; existing people, devices and history are kept.
    replace: everything people-related (people, device assignments, location names, history)
             is deleted first, so this server ends up matching the file.
    """
    stripped = text.lstrip("﻿ \t\r\n")
    if not stripped:
        raise ImportError_("The file is empty.")
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError as e:
            raise ImportError_(f"That isn't valid JSON ({e}).") from e
        return import_export(db, data, replace=replace)

    if replace:
        _wipe(db)
    added, assigned, problems = import_v1(db, stripped)
    return ImportResult(people_added=added, devices_assigned=assigned, problems=problems)


def import_export(db: Database, data: dict, replace: bool = False) -> ImportResult:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ImportError_("This isn't a Who's Here export file.")
    if int(data.get("format_version", 0)) > FORMAT_VERSION:
        raise ImportError_(f"This export came from a newer Who's Here ({data.get('app_version')}). "
                           f"Update this server first.")
    people = data.get("people")
    if not isinstance(people, list):
        raise ImportError_("The export has no people list.")

    if replace:
        _wipe(db)

    result = ImportResult()
    now = time.time()
    id_map: dict = {}  # id in the file -> id in this database
    with db.connect() as conn:
        for p in people:
            name = str(p.get("name", "")).strip()
            if not name:
                result.problems.append("Skipped a person with no name")
                continue
            row = conn.execute("SELECT id FROM people WHERE lower(name) = lower(?)", (name,)).fetchone()
            if row:
                pid = row["id"]
                result.people_updated += 1
            else:
                pid = conn.execute("INSERT INTO people (name, active, created_at) VALUES (?, ?, ?)",
                                   (name, 1 if p.get("active", True) else 0, now)).lastrowid
                result.people_added += 1
            if "id" in p:
                id_map[p["id"]] = pid

            for d in p.get("devices") or []:
                mac = normalize_mac(str(d.get("mac", "")))
                if not mac:
                    result.problems.append(f"{name}: skipped unreadable MAC {d.get('mac')!r}")
                    continue
                owner = conn.execute("SELECT d.person_id, p.name FROM devices d "
                                     "LEFT JOIN people p ON p.id = d.person_id WHERE d.mac = ?",
                                     (mac,)).fetchone()
                if owner and owner["person_id"] not in (None, pid):
                    result.problems.append(f"{mac} ({name}) already belongs to {owner['name']}; left as is")
                    continue
                conn.execute(
                    """INSERT INTO devices (mac, person_id, client_name) VALUES (?, ?, ?)
                       ON CONFLICT(mac) DO UPDATE SET person_id = excluded.person_id, ignored = 0,
                           client_name = COALESCE(devices.client_name, excluded.client_name)""",
                    (mac, pid, d.get("name") or None))
                result.devices_assigned += 1

        for loc in data.get("locations") or []:
            mac = normalize_mac(str(loc.get("mac", "")))
            alias = (loc.get("alias") or "").strip() or None
            if not mac or not alias:
                continue
            conn.execute(
                """INSERT INTO locations (mac, name, alias) VALUES (?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET alias = excluded.alias""",
                (mac, loc.get("name") or None, alias))
            result.locations_named += 1

        for raw in data.get("hidden_devices") or []:
            mac = normalize_mac(str(raw))
            if mac:
                conn.execute("""INSERT INTO devices (mac, ignored) VALUES (?, 1)
                                ON CONFLICT(mac) DO UPDATE SET ignored = 1 WHERE person_id IS NULL""",
                             (mac,))

        for e in data.get("history") or []:
            pid = id_map.get(e.get("person_id"))
            kind = e.get("kind")
            try:
                at = float(e.get("at"))
            except (TypeError, ValueError):
                continue
            if pid is None or kind not in ("arrive", "depart"):
                continue
            dup = conn.execute("SELECT 1 FROM events WHERE person_id = ? AND kind = ? AND abs(at - ?) < 1",
                               (pid, kind, at)).fetchone()
            if dup:
                continue
            conn.execute("INSERT INTO events (person_id, kind, at, uplink, device_mac) VALUES (?, ?, ?, ?, ?)",
                         (pid, kind, at, normalize_mac(e.get("location_mac") or ""),
                          normalize_mac(e.get("device_mac") or "")))
            result.events_added += 1
    return result


def _wipe(db: Database) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM presence")
        conn.execute("DELETE FROM people")  # device assignments clear via ON DELETE SET NULL
        conn.execute("UPDATE devices SET person_id = NULL, ignored = 0")
        conn.execute("UPDATE locations SET alias = NULL")
