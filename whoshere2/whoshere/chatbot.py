"""What Who's Here says in Google Chat. Pure functions: event in, reply text out.

Commands (IDs are what you enter on the Chat API configuration page):

    1  /whoshere [name]   who's on campus; with a name, everyone whose name matches
    2  /allclear          is it safe to arm the alarm?
    3  /history [name]    today's arrivals and departures

In a direct message, plain text works too: "all clear?", "history Thomas", "Thomas", "help".
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime

from . import presence
from .config import Settings
from .db import Database
from .timefmt import make_filters

CMD_WHOSHERE, CMD_ALLCLEAR, CMD_HISTORY = 1, 2, 3
COMMANDS = {
    CMD_WHOSHERE: ("/whoshere", "Who's on campus. Add a name to look someone up: /whoshere Thomas"),
    CMD_ALLCLEAR: ("/allclear", "Is it safe to arm the alarm?"),
    CMD_HISTORY: ("/history", "Today's arrivals and departures. Add a name for one person: /history Thomas"),
}
_BY_NAME = {name.lstrip("/"): cid for cid, (name, _) in COMMANDS.items()}
MAX_LINES = 40


@dataclass
class ChatRequest:
    """A message to Who's Here, reduced to what matters."""
    kind: str                 # "message", "added", "removed", "other"
    space: str | None = None  # "spaces/AAAA..."
    space_type: str = ""      # "DIRECT_MESSAGE", "SPACE", "GROUP_CHAT"
    thread: str | None = None
    user_name: str | None = None   # "users/123..."
    user_email: str = ""
    user_display: str = ""
    command_id: int | None = None
    text: str = ""            # what the user typed after the command (or the whole DM)
    message_name: str | None = None


def parse_event(event: dict) -> ChatRequest:
    """Accepts both Chat event formats: classic (top-level "type") and Workspace add-on ("chat")."""
    if "chat" in event and isinstance(event["chat"], dict):
        c = event["chat"]
        user = c.get("user") or {}
        payload, kind = None, "other"
        for key, k in (("messagePayload", "message"), ("appCommandPayload", "message"),
                       ("addedToSpacePayload", "added"), ("removedFromSpacePayload", "removed")):
            if key in c:
                payload, kind = c[key], k
                break
        payload = payload or {}
        msg = payload.get("message") or {}
        space = payload.get("space") or msg.get("space") or {}
        if kind == "added" and msg:
            kind = "message"  # added by @mentioning in a message: answer the message
        command_id = ((payload.get("appCommandMetadata") or {}).get("appCommandId"))
    else:
        etype = event.get("type", "")
        user = event.get("user") or {}
        msg = event.get("message") or {}
        space = event.get("space") or msg.get("space") or {}
        kind = {"MESSAGE": "message", "APP_COMMAND": "message", "REMOVED_FROM_SPACE": "removed"}.get(etype, "other")
        if etype == "ADDED_TO_SPACE":
            kind = "message" if msg else "added"
        command_id = (event.get("appCommandMetadata") or {}).get("appCommandId")

    if command_id is None:
        command_id = (msg.get("slashCommand") or {}).get("commandId")
    if command_id is None:
        for a in msg.get("annotations") or []:
            if a.get("type") == "SLASH_COMMAND":
                command_id = (a.get("slashCommand") or {}).get("commandId")
                break
    text = msg.get("argumentText")
    if text is None:
        text = msg.get("text") or ""
    text = text.strip()

    cid = int(command_id) if str(command_id or "").isdigit() else None
    # The command's own name can be left in the text (e.g. "/whoshere Thomas"); take it off.
    m = re.match(r"^/(\w[\w-]*)\s*(.*)$", text, re.S)
    if m:
        if cid is None:
            cid = _BY_NAME.get(m.group(1).lower())
        if cid is not None:
            text = m.group(2).strip()

    return ChatRequest(
        kind=kind,
        space=space.get("name"),
        space_type=space.get("spaceType") or ("DIRECT_MESSAGE" if space.get("type") == "DM" else ""),
        thread=(msg.get("thread") or {}).get("name"),
        user_name=user.get("name"),
        user_email=(user.get("email") or "").lower(),
        user_display=user.get("displayName") or "",
        command_id=cid,
        text=text,
        message_name=msg.get("name"),
    )


def allowed(req: ChatRequest, allow: tuple[str, ...]) -> bool:
    """GOOGLE_CHAT_ALLOWED: emails and/or @domains. Empty = anyone who can reach the app."""
    if not allow:
        return True
    email = req.user_email
    if not email:
        return False
    return any(email == a or (a.startswith("@") and email.endswith(a)) for a in allow)


def reply_for(req: ChatRequest, db: Database, settings: Settings, now: float | None = None) -> str | None:
    if req.kind == "removed":
        return None
    if req.kind == "added":
        return ("Thanks for adding me! " + help_text(settings))
    if req.kind != "message":
        return None
    if not allowed(req, settings.chat_allowed):
        return "Sorry, you're not on the list of people who can use Who's Here in Chat. Ask your administrator."

    cid, text = req.command_id, req.text
    if cid is None:  # plain message in a DM or @mention: guess what they mean
        lowered = text.lower().strip(" ?!.")
        if lowered in ("", "who", "whos here", "who's here", "who’s here", "whoshere", "who is here", "status"):
            cid, text = CMD_WHOSHERE, ""
        elif lowered in ("help", "commands", "?"):
            return help_text(settings)
        elif re.match(r"^(all ?clear|safe to arm|can i arm|arm|alarm|clear)\b", lowered):
            cid, text = CMD_ALLCLEAR, ""
        elif lowered.startswith("history"):
            cid, text = CMD_HISTORY, text[len("history"):].strip()
        else:
            cid = CMD_WHOSHERE
    if text.lower() in ("help", "?"):
        return help_text(settings)

    f = make_filters(settings.timezone)
    if cid == CMD_ALLCLEAR:
        return all_clear_text(db, settings, f, now)
    if cid == CMD_HISTORY:
        return history_text(db, settings, f, text, now)
    if cid == CMD_WHOSHERE:
        return lookup_text(db, settings, f, text, now) if text else on_campus_text(db, settings, f, now)
    return help_text(settings)


# --------------------------------------------------------------------------- replies

def _where(p, show_wifi: bool) -> str:
    where = p["location"] or ""
    if show_wifi and p.get("wifi"):
        where = f"{where} · {p['wifi']}" if where else p["wifi"]
    return where


def _here_line(p, f, show_wifi: bool) -> str:
    where = _where(p, show_wifi)
    bits = [f"*{p['name']}*"]
    if where:
        bits.append(f"— {where}")
    if p["leaving"]:
        bits.append(f"(Here? last seen {f['ago'](p['last_seen'])})")
    elif p["since"]:
        bits.append(f"(since {f['when'](p['since'])})")
    elif p["state"] == "unknown":
        bits.append("(checking…)")
    return "• " + " ".join(bits)


def _verdict_line(b, f) -> str:
    if b["verdict"] == "clear":
        return "🟢 *All clear to arm.* No tracked staff are on campus."
    if b["verdict"] == "occupied":
        n = len(b["here"])
        return f"🔴 *Not clear — {n} {'person' if n == 1 else 'people'} on campus.*"
    age = f"Last good update from UniFi was {f['ago'](b['last_success'])}." if b["last_success"] \
        else "No data from UniFi yet."
    return f"🟠 *Can't confirm — check manually.* {age}"


def _footer(settings: Settings) -> str:
    return f"\n<{settings.board_url}|Open the board>" if settings.board_url else ""


def on_campus_text(db, settings, f, now=None) -> str:
    b = presence.board(db, settings.stale_after, now=now)
    lines = [_verdict_line(b, f)]
    for p in b["here"][:MAX_LINES]:
        lines.append(_here_line(p, f, settings.show_wifi_network))
    if len(b["here"]) > MAX_LINES:
        lines.append(f"…and {len(b['here']) - MAX_LINES} more.")
    if b["verdict"] == "stale" and b["here"]:
        lines.insert(1, "Last known on campus:")
    if b["untracked"]:
        lines.append("_Not tracked (no device):_ " + ", ".join(p["name"] for p in b["untracked"]))
    return "\n".join(lines) + _footer(settings)


def all_clear_text(db, settings, f, now=None) -> str:
    b = presence.board(db, settings.stale_after, now=now)
    lines = [_verdict_line(b, f)]
    if b["verdict"] == "occupied":
        for p in b["here"][:MAX_LINES]:
            lines.append(_here_line(p, f, settings.show_wifi_network))
        leaving = [p for p in b["here"] if p["leaving"]]
        if leaving and len(leaving) == len(b["here"]):
            lines.append(f"_Everyone left looks like they may be leaving. They'll drop off after "
                         f"{settings.departure_grace // 60} minutes unseen; check again shortly._")
    elif b["verdict"] == "stale":
        if b["last_error"]:
            lines.append(f"_UniFi error: {b['last_error']}_")
        lines.append("Walk the building or check the board before arming.")
    if b["untracked"]:
        lines.append(f"_Heads up: {len(b['untracked'])} staff have no device assigned and aren't tracked: "
                     + ", ".join(p["name"] for p in b["untracked"]) + "._")
    return "\n".join(lines) + _footer(settings)


def _matches(db, name: str):
    q = name.strip().lower()
    rows = db.all("SELECT id, name FROM people WHERE active = 1 ORDER BY name COLLATE NOCASE")
    return [r for r in rows if q in r["name"].lower()]


def lookup_text(db, settings, f, name: str, now=None) -> str:
    matches = _matches(db, name)
    if not matches:
        return f"Nobody on the board matches “{name}”."
    b = presence.board(db, settings.stale_after, now=now)
    by_id = {p["id"]: p for p in b["people"]}
    lines = []
    if b["verdict"] == "stale":
        lines.append("🟠 _UniFi data is out of date, so this may be wrong._")
    for r in matches[:MAX_LINES]:
        p = by_id.get(r["id"])
        if p is None:
            continue
        if p["state"] in ("here", "unknown"):
            line = _here_line(p, f, settings.show_wifi_network).replace("• ", "🟢 ", 1)
        elif p["state"] == "away":
            line = f"⚪ *{p['name']}* — not here" + (f", last seen {f['when'](p['last_seen'])}"
                                                    if p["last_seen"] else ", never seen")
        else:
            line = f"⚪ *{p['name']}* — not tracked (no device assigned)"
        lines.append(line)
    if len(matches) > MAX_LINES:
        lines.append(f"…and {len(matches) - MAX_LINES} more. Try a longer name.")
    return "\n".join(lines) + _footer(settings)


def history_text(db, settings, f, name: str, now=None) -> str:
    now = now or time.time()
    start = datetime.fromtimestamp(now, settings.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    sql = ("SELECT e.kind, e.at, e.uplink, e.ssid, p.name FROM events e JOIN people p ON p.id = e.person_id "
           "WHERE e.at >= ?")
    params: list = [start.timestamp()]
    who = ""
    if name:
        matches = _matches(db, name)
        if not matches:
            return f"Nobody on the board matches “{name}”."
        sql += f" AND e.person_id IN ({','.join('?' * len(matches))})"
        params += [m["id"] for m in matches]
        who = " for " + ", ".join(m["name"] for m in matches[:5]) + ("…" if len(matches) > 5 else "")
    rows = db.all(sql + " ORDER BY e.at", tuple(params))
    if not rows:
        return f"No arrivals or departures today{who}."
    names = presence.location_names(db)
    lines = [f"*Today{who}:*"]
    shown = rows[-MAX_LINES:]
    if len(rows) > len(shown):
        lines.append(f"_(last {len(shown)} of {len(rows)})_")
    for e in shown:
        where = names.get(e["uplink"], "") if e["uplink"] else ""
        if settings.show_wifi_network and e["ssid"]:
            where = f"{where} · {e['ssid']}" if where else e["ssid"]
        verb = "arrived" if e["kind"] == "arrive" else "left"
        lines.append(f"{f['clock'](e['at'])}  *{e['name']}* {verb}" + (f" ({where})" if where else ""))
    return "\n".join(lines) + _footer(settings)


def help_text(settings: Settings) -> str:
    lines = ["*Who's Here* tells you who's on campus, from the staff phones on the Wi-Fi.", ""]
    for _, (name, desc) in sorted(COMMANDS.items()):
        lines.append(f"• *{name}* — {desc}")
    lines.append("")
    lines.append("In a direct message you can also just type: “all clear?”, a name, or “history”.")
    return "\n".join(lines) + _footer(settings)
