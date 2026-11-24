"""What Who's Here says in Google Chat. Pure functions: event in, reply out.

Each reply is a card (one row per person, easy to scan) plus a plain-text version used
for notifications and as a fallback if Chat won't take the card.

Commands (IDs are what you enter on the Chat API configuration page):

    1  /whoshere [name]   who's on campus; with a name, everyone whose name matches
    2  /allclear          is it safe to arm the alarm?
    3  /history [name]    today's arrivals and departures

In a direct message, plain text works too: "all clear?", "history Taylor", "Taylor", "help".
"""
from __future__ import annotations

import html
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
    CMD_WHOSHERE: ("/whoshere", "Who's on campus. Add a name to look someone up: /whoshere Taylor"),
    CMD_ALLCLEAR: ("/allclear", "Is it safe to arm the alarm?"),
    CMD_HISTORY: ("/history", "Today's arrivals and departures. Add a name for one person: /history Taylor"),
}
_BY_NAME = {name.lstrip("/"): cid for cid, (name, _) in COMMANDS.items()}
MAX_LINES = 40
GREEN, RED, AMBER, YELLOW, GREY = "#1f7a3d", "#b42318", "#b45309", "#a16207", "#6b6b70"


@dataclass
class Reply:
    text: str               # plain text: notification preview and fallback
    card: dict | None = None  # a cardsV2 entry, when the answer is a list


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
    # The command's own name can be left in the text (e.g. "/whoshere Taylor"); take it off.
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


def reply_for(req: ChatRequest, db: Database, settings: Settings, now: float | None = None) -> Reply | None:
    if req.kind == "removed":
        return None
    if req.kind == "added":
        return Reply("Thanks for adding me! " + help_text(settings))
    if req.kind != "message":
        return None
    if not allowed(req, settings.chat_allowed):
        return Reply("Sorry, you're not on the list of people who can use Who's Here in Chat. Ask your administrator.")

    cid, text = req.command_id, req.text
    if cid is None:  # plain message in a DM or @mention: guess what they mean
        lowered = text.lower().strip(" ?!.")
        if lowered in ("", "who", "whos here", "who's here", "who’s here", "whoshere", "who is here", "status"):
            cid, text = CMD_WHOSHERE, ""
        elif lowered in ("help", "commands", "?"):
            return Reply(help_text(settings))
        elif re.match(r"^(all ?clear|safe to arm|can i arm|arm|alarm|clear)\b", lowered):
            cid, text = CMD_ALLCLEAR, ""
        elif lowered.startswith("history"):
            cid, text = CMD_HISTORY, text[len("history"):].strip()
        else:
            cid = CMD_WHOSHERE
    if text.lower() in ("help", "?"):
        return Reply(help_text(settings))

    f = make_filters(settings.timezone)
    if cid == CMD_ALLCLEAR:
        return all_clear(db, settings, f, now)
    if cid == CMD_HISTORY:
        return history(db, settings, f, text, now)
    if cid == CMD_WHOSHERE:
        return lookup(db, settings, f, text, now) if text else on_campus(db, settings, f, now)
    return Reply(help_text(settings))


# --------------------------------------------------------------------------- building blocks

def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _where(p, show_wifi: bool) -> str:
    where = p["location"] or ""
    if show_wifi and p.get("wifi"):
        where = f"{where} · {p['wifi']}" if where else p["wifi"]
    return where


def _when_here(p, f) -> str:
    if p["leaving"]:
        return f"Here? last seen {f['ago'](p['last_seen'])}"
    if p["since"]:
        return f"since {f['when'](p['since'])}"
    if p["state"] == "unknown":
        return "checking…"
    return ""


def _verdict(b, f) -> tuple[str, str, str]:
    """(emoji, headline, color) for the banner."""
    if b["verdict"] == "clear":
        return "🟢", "All clear to arm", GREEN
    if b["verdict"] == "occupied":
        n = len(b["here"])
        return "🔴", f"Not clear — {n} {'person' if n == 1 else 'people'} on campus", RED
    return "🟠", "Can't confirm — check manually", AMBER


def _stale_note(b, f) -> str:
    return (f"Last good update from UniFi was {f['ago'](b['last_success'])}." if b["last_success"]
            else "No data from UniFi yet.")


def _card(title_html: str, sections: list[dict], settings: Settings, f, now=None, card_id="whoshere") -> dict:
    """A Chat card: Who's Here header, a coloured headline, then the sections and a board button."""
    sections = [s for s in sections if s.get("widgets")]
    head = {"widgets": [{"textParagraph": {"text": title_html}}]}
    out = [head] + sections
    if settings.board_url:
        out.append({"widgets": [{"buttonList": {"buttons": [
            {"text": "Open the board", "onClick": {"openLink": {"url": settings.board_url}}}]}}]})
    stamp = f["clock"](now or time.time())
    header = {"title": "Who's Here", "subtitle": f"As of {stamp}"}
    if settings.chat_avatar_url:  # must be a public https image; leave unset for no image
        header.update(imageUrl=settings.chat_avatar_url, imageType="CIRCLE", imageAltText="Who's Here")
    return {"cardId": card_id, "card": {"header": header, "sections": out}}


def _person_row(p, f, show_wifi: bool) -> dict:
    """One person, one row: location above, name in bold, time below."""
    name = f"<b>{_e(p['name'])}</b>"
    if p["leaving"]:
        name += f' <font color="{YELLOW}">(Here?)</font>'
    row = {"startIcon": {"knownIcon": "PERSON"}, "text": name, "wrapText": True}
    where = _where(p, show_wifi)
    if where:
        row["topLabel"] = where
    when = _when_here(p, f)
    if when:
        row["bottomLabel"] = when
    return {"decoratedText": row}


def _untracked_section(b) -> dict:
    if not b["untracked"]:
        return {}
    names = ", ".join(_e(p["name"]) for p in b["untracked"])
    return {"header": "Not tracked (no phone set up)", "collapsible": False,
            "widgets": [{"textParagraph": {"text": f'<font color="{GREY}">{names}</font>'}}]}


def _footer(settings: Settings) -> str:
    return f"\n\nBoard: {settings.board_url}" if settings.board_url else ""


# --------------------------------------------------------------------------- replies

def _here_text_lines(b, f, show_wifi) -> list[str]:
    lines = []
    for p in b["here"][:MAX_LINES]:
        where = _where(p, show_wifi)
        when = _when_here(p, f)
        lines.append(f"• *{p['name']}*" + (f" — {where}" if where else "") + (f" ({when})" if when else ""))
    if len(b["here"]) > MAX_LINES:
        lines.append(f"…and {len(b['here']) - MAX_LINES} more.")
    return lines


def on_campus(db, settings, f, now=None) -> Reply:
    b = presence.board(db, settings.stale_after, now=now)
    emoji, headline, color = _verdict(b, f)
    rows = [_person_row(p, f, settings.show_wifi_network) for p in b["here"][:MAX_LINES]]
    if len(b["here"]) > MAX_LINES:
        rows.append({"textParagraph": {"text": f"…and {len(b['here']) - MAX_LINES} more."}})

    title = f'<font color="{color}"><b>{emoji} {_e(headline)}</b></font>'
    sub = ""
    if b["verdict"] == "clear":
        sub = "No tracked staff are on campus."
    elif b["verdict"] == "stale":
        sub = _stale_note(b, f)
    if sub:
        title += f"<br>{_e(sub)}"
    on_header = "Last known on campus" if b["verdict"] == "stale" else "On campus"
    card = _card(title, [{"header": on_header, "widgets": rows}, _untracked_section(b)], settings, f, now)

    lines = [f"{emoji} *{headline}*" + (f" {sub}" if sub else "")]
    if b["here"]:
        lines.append("")
        if b["verdict"] == "stale":
            lines.append("Last known on campus:")
        lines += _here_text_lines(b, f, settings.show_wifi_network)
    if b["untracked"]:
        lines += ["", "_Not tracked (no device):_ " + ", ".join(p["name"] for p in b["untracked"])]
    return Reply("\n".join(lines) + _footer(settings), card)


def all_clear(db, settings, f, now=None) -> Reply:
    b = presence.board(db, settings.stale_after, now=now)
    emoji, headline, color = _verdict(b, f)
    notes: list[str] = []
    if b["verdict"] == "clear":
        notes.append("No tracked staff are on campus. Safe to arm.")
    elif b["verdict"] == "occupied":
        leaving = [p for p in b["here"] if p["leaving"]]
        if leaving and len(leaving) == len(b["here"]):
            notes.append(f"Everyone left looks like they may be leaving. They'll drop off after "
                         f"{settings.departure_grace // 60} minutes unseen; check again shortly.")
    else:
        notes.append(_stale_note(b, f))
        if b["last_error"]:
            notes.append(f"UniFi error: {b['last_error']}")
        notes.append("Walk the building or check the board before arming.")
    if b["untracked"]:
        notes.append(f"Heads up: {len(b['untracked'])} staff have no device assigned and aren't tracked.")

    title = f'<font color="{color}"><b>{emoji} {_e(headline)}</b></font>'
    if notes:
        title += "<br>" + "<br>".join(_e(n) for n in notes)
    rows = [_person_row(p, f, settings.show_wifi_network) for p in b["here"][:MAX_LINES]] \
        if b["verdict"] == "occupied" else []
    card = _card(title, [{"header": "Still on campus", "widgets": rows}, _untracked_section(b)],
                 settings, f, now, card_id="allclear")

    lines = [f"{emoji} *{headline}*"]
    if b["verdict"] == "occupied":
        lines.append("")
        lines += _here_text_lines(b, f, settings.show_wifi_network)
    if notes:
        lines += [""] + [f"_{n}_" for n in notes]
    if b["untracked"]:
        lines.append("_Not tracked:_ " + ", ".join(p["name"] for p in b["untracked"]))
    return Reply("\n".join(lines) + _footer(settings), card)


def _matches(db, name: str):
    q = name.strip().lower()
    rows = db.all("SELECT id, name FROM people WHERE active = 1 ORDER BY name COLLATE NOCASE")
    return [r for r in rows if q in r["name"].lower()]


def lookup(db, settings, f, name: str, now=None) -> Reply:
    matches = _matches(db, name)
    if not matches:
        return Reply(f"Nobody on the board matches “{name}”.")
    b = presence.board(db, settings.stale_after, now=now)
    by_id = {p["id"]: p for p in b["people"]}
    rows, lines = [], []
    if b["verdict"] == "stale":
        lines += ["🟠 _UniFi data is out of date, so this may be wrong._", ""]
    for r in matches[:MAX_LINES]:
        p = by_id.get(r["id"])
        if p is None:
            continue
        if p["state"] in ("here", "unknown"):
            status, color = ("Here?", YELLOW) if p["leaving"] else ("Here", GREEN)
            where, when = _where(p, settings.show_wifi_network), _when_here(p, f)
            if p["leaving"]:
                when = when.replace("Here? ", "")
            emoji = "🟢"
        elif p["state"] == "away":
            status, color, where, emoji = "Not here", GREY, "", "⚪"
            when = f"last seen {f['when'](p['last_seen'])}" if p["last_seen"] else "never seen"
        else:
            status, color, where, emoji = "Not tracked", GREY, "", "⚪"
            when = "no phone set up"
        row = {"text": f'<b>{_e(p["name"])}</b>  <font color="{color}">{status}</font>', "wrapText": True,
               "startIcon": {"knownIcon": "PERSON"}}
        if where:
            row["topLabel"] = where
        if when:
            row["bottomLabel"] = when
        rows.append({"decoratedText": row})
        lines.append(f"{emoji} *{p['name']}* — {status.lower()}" + (f", {where}" if where else "")
                     + (f" ({when})" if when else ""))
    if len(matches) > MAX_LINES:
        rows.append({"textParagraph": {"text": f"…and {len(matches) - MAX_LINES} more. Try a longer name."}})
        lines.append(f"…and {len(matches) - MAX_LINES} more. Try a longer name.")
    title = f"<b>Matching “{_e(name)}”</b>"
    if b["verdict"] == "stale":
        title += f'<br><font color="{AMBER}">UniFi data is out of date, so this may be wrong.</font>'
    card = _card(title, [{"widgets": rows}], settings, f, now, card_id="lookup")
    return Reply("\n".join(lines) + _footer(settings), card)


def history(db, settings, f, name: str, now=None) -> Reply:
    now = now or time.time()
    start = datetime.fromtimestamp(now, settings.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    sql = ("SELECT e.kind, e.at, e.uplink, e.ssid, p.name FROM events e JOIN people p ON p.id = e.person_id "
           "WHERE e.at >= ?")
    params: list = [start.timestamp()]
    who = ""
    if name:
        matches = _matches(db, name)
        if not matches:
            return Reply(f"Nobody on the board matches “{name}”.")
        sql += f" AND e.person_id IN ({','.join('?' * len(matches))})"
        params += [m["id"] for m in matches]
        who = " for " + ", ".join(m["name"] for m in matches[:5]) + ("…" if len(matches) > 5 else "")
    rows = db.all(sql + " ORDER BY e.at", tuple(params))
    if not rows:
        return Reply(f"No arrivals or departures today{who}.")
    names = presence.location_names(db)
    shown = rows[-MAX_LINES:]
    lines = [f"*Today{who}:*"]
    if len(rows) > len(shown):
        lines.append(f"_(last {len(shown)} of {len(rows)})_")
    widgets = []
    for e in shown:
        where = names.get(e["uplink"], "") if e["uplink"] else ""
        if settings.show_wifi_network and e["ssid"]:
            where = f"{where} · {e['ssid']}" if where else e["ssid"]
        arrived = e["kind"] == "arrive"
        verb = "arrived" if arrived else "left"
        lines.append(f"{f['clock'](e['at'])}  *{e['name']}* {verb}" + (f" ({where})" if where else ""))
        row = {"topLabel": f["clock"](e["at"]), "wrapText": True,
               "text": f'<b>{_e(e["name"])}</b>  <font color="{GREEN if arrived else GREY}">{verb}</font>'}
        if where:
            row["bottomLabel"] = where
        widgets.append({"decoratedText": row})
    title = f"<b>Today{_e(who)}</b>"
    if len(rows) > len(shown):
        title += f"<br>Last {len(shown)} of {len(rows)}"
    card = _card(title, [{"widgets": widgets}], settings, f, now, card_id="history")
    return Reply("\n".join(lines) + _footer(settings), card)


def help_text(settings: Settings) -> str:
    lines = ["*Who's Here* tells you who's on campus, from the staff phones on the Wi-Fi.", ""]
    for _, (name, desc) in sorted(COMMANDS.items()):
        lines.append(f"• *{name}* — {desc}")
    lines.append("")
    lines.append("In a direct message you can also just type: “all clear?”, a name, or “history”.")
    return "\n".join(lines) + _footer(settings)
