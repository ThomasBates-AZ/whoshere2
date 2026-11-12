"""Import the people list from Who's Here? v1 (users.txt)."""
from __future__ import annotations

import re
import time

from .db import Database
from .macutil import normalize_mac


def parse_v1(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """v1 format: "Full Name  aa:bb:cc:dd:ee:ff" (name, two or more spaces or a tab, MAC).

    Returns (entries, problems). Lines that can't be read are reported, not guessed at.
    """
    entries, problems = [], []
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s{2,}|\t", line, maxsplit=1)
        if len(parts) != 2:
            # Be forgiving: last whitespace-separated token as the MAC
            name, _, mac_part = line.rpartition(" ")
            parts = [name, mac_part]
        name, mac = parts[0].strip(), normalize_mac(parts[1])
        if not name or not mac:
            problems.append(f"Line {n}: couldn't read {line!r}")
            continue
        entries.append((name, mac))
    return entries, problems


def import_v1(db: Database, text: str) -> tuple[int, int, list[str]]:
    """Returns (people added, devices assigned, problems). Safe to run twice."""
    entries, problems = parse_v1(text)
    added = assigned = 0
    now = time.time()
    with db.connect() as conn:
        for name, mac in entries:
            row = conn.execute("SELECT id FROM people WHERE lower(name) = lower(?)", (name,)).fetchone()
            if row:
                person_id = row["id"]
            else:
                person_id = conn.execute("INSERT INTO people (name, created_at) VALUES (?, ?)",
                                         (name, now)).lastrowid
                added += 1
            owner = conn.execute("SELECT person_id FROM devices WHERE mac = ?", (mac,)).fetchone()
            if owner and owner["person_id"] not in (None, person_id):
                problems.append(f"{mac} ({name}) already belongs to someone else; left as is")
                continue
            conn.execute(
                """INSERT INTO devices (mac, person_id) VALUES (?, ?)
                   ON CONFLICT(mac) DO UPDATE SET person_id = excluded.person_id, ignored = 0""",
                (mac, person_id))
            assigned += 1
    return added, assigned, problems
