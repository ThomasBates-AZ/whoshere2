"""Web UI and JSON API."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import __version__, presence
from .changelog import CHANGELOG
from .config import Settings
from .db import Database
from .backup import ImportError_, export_data, import_text
from .importer import import_v1
from .netcheck import masked_by_docker
from .gchat import ChatBridge, ChatSetupError, check_key
from . import chatbot
from .timefmt import make_filters
from .macutil import is_randomized, normalize_mac
from .poller import Poller
from .unifi import UniFiError, find_client_by_ip, make_client

log = logging.getLogger("whoshere.web")
HERE = Path(__file__).parent


# --------------------------------------------------------------------------- time formatting

# --------------------------------------------------------------------------- app

def create_app(settings: Settings, db: Database | None = None, start_poller: bool = True) -> Starlette:
    db = db or Database(settings.db_path)
    poller = Poller(settings, db)
    chat = ChatBridge(settings, db)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(make_filters(settings.timezone))
    templates.env.globals.update(title=settings.title, version=__version__, changelog=CHANGELOG,
                                 is_randomized=is_randomized, self_register=settings.self_register,
                                 staff_wifi=settings.staff_wifi, show_wifi=settings.show_wifi_network,
                                 wifi_networks=settings.wifi_networks)

    # ----- helpers

    def role(request: Request) -> str | None:
        return request.session.get("role")

    def csrf_token(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        return request.session["csrf"]

    def flash(request: Request, message: str, kind: str = "ok") -> None:
        request.session.setdefault("flash", []).append([kind, message])

    def render(request: Request, name: str, **ctx) -> Response:
        ctx.update(role=role(request), csrf=csrf_token(request),
                   flashes=request.session.pop("flash", []),
                   admin_enabled=bool(settings.admin_password), path=request.url.path)
        return templates.TemplateResponse(request, name, ctx)

    def to_login(request: Request) -> RedirectResponse:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    def can_view(request: Request) -> bool:
        return not settings.view_password or role(request) in ("viewer", "admin")

    def is_admin(request: Request) -> bool:
        return bool(settings.admin_password) and role(request) == "admin"

    async def checked_form(request: Request):
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
            return None
        return form

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # ----- public / viewer pages

    async def board_page(request: Request):
        if not can_view(request):
            return to_login(request)
        state = presence.board(db, settings.stale_after)
        return render(request, "board.html", b=state, refresh=30, live=True)

    async def api_status(request: Request):
        if not can_view(request):
            return JSONResponse({"error": "login required"}, status_code=401)
        state = presence.board(db, settings.stale_after)
        return JSONResponse({
            "all_clear": state["verdict"] == "clear",
            "verdict": state["verdict"],
            "on_site": [{"name": p["name"], "location": p["location"], "wifi": p["wifi"], "since": p["since"],
                         "last_seen": p["last_seen"], "leaving": p["leaving"]} for p in state["here"]],
            "people": [{k: p[k] for k in ("name", "state", "location", "wifi", "since", "last_seen")}
                       for p in state["people"]],
            "last_success": state["last_success"],
            "last_error": state["last_error"],
        })

    async def manifest(request: Request):
        """Lets phones add the board to the home screen as an app."""
        return JSONResponse({
            "name": settings.title, "short_name": "Who's Here", "start_url": "/", "scope": "/",
            "display": "standalone", "background_color": "#f6f5f3", "theme_color": "#6b0f1a",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
            ],
        }, media_type="application/manifest+json")

    async def healthz(request: Request):
        return PlainTextResponse("ok")

    async def history_page(request: Request):
        if not can_view(request):
            return to_login(request)
        try:
            days = int(request.query_params.get("days", 7))
        except ValueError:
            days = 7
        days = days if days in (1, 7, 30, 90, 365) else 7
        person = request.query_params.get("person", "")
        sql = ("SELECT e.*, p.name FROM events e JOIN people p ON p.id = e.person_id "
               "WHERE e.at >= ?")
        params: list = [time.time() - days * 86400]
        if person.isdigit():
            sql += " AND e.person_id = ?"
            params.append(int(person))
        sql += " ORDER BY e.at DESC LIMIT 2000"
        events = db.all(sql, tuple(params))
        names = presence.location_names(db)
        day_filter = templates.env.filters["day"]
        grouped: list[tuple[str, list]] = []
        for e in events:
            label = day_filter(e["at"])
            if not grouped or grouped[-1][0] != label:
                grouped.append((label, []))
            grouped[-1][1].append({**dict(e), "location": names.get(e["uplink"]) if e["uplink"] else None,
                                       "wifi": e["ssid"]})
        people = db.all("SELECT id, name FROM people ORDER BY name COLLATE NOCASE")
        selected = None
        if person.isdigit():
            board_people = presence.board(db, settings.stale_after)["people"]
            selected = next((p for p in board_people if p["id"] == int(person)), None)
            if selected is None:  # hidden from the board: still show their name
                row = db.one("SELECT id, name FROM people WHERE id = ?", (int(person),))
                if row:
                    selected = {"id": row["id"], "name": row["name"], "state": "hidden"}
        return render(request, "history.html", live=True, grouped=grouped, people=people, selected=selected,
                      days=days, person=person, truncated=len(events) >= 2000)

    # ----- self-registration: staff add their own phone

    def lookup_visitor(request: Request):
        """Blocking; run in a thread. Asks UniFi which client has the visitor's IP.

        Returns (client or None, error message or None). The MAC always comes from
        UniFi, never from the browser, so nobody can register a device they aren't holding.
        """
        ip = request.client.host if request.client else None
        try:
            snap = make_client(settings).snapshot()
        except UniFiError as e:
            log.warning("Self-register lookup failed: %s", e)
            return None, "unifi"
        return find_client_by_ip(snap, ip), None

    async def register_page(request: Request):
        if not settings.self_register:
            return render(request, "register.html", step="disabled")
        client, error = await asyncio.to_thread(lookup_visitor, request)
        ip = request.client.host if request.client else ""
        if error:
            return render(request, "register.html", step="unifi_error", ip=ip)
        if client is None and masked_by_docker(ip):
            log.warning("Add my phone sees every visitor as %s (Docker's gateway), not the phone's own "
                        "address. Set NETWORK_MODE=host so phones can be recognized.", ip)
            return render(request, "register.html", step="not_found", ip=ip, masked=True)
        if client is None:
            return render(request, "register.html", step="not_found", ip=ip)
        if not client.wired and client.ssid and not settings.is_watched_wifi(client.ssid):
            return render(request, "register.html", step="wrong_wifi", client=client, wifi_here=client.ssid)
        if is_randomized(client.mac):
            return render(request, "register.html", step="private", client=client, wifi_here=client.ssid)

        owner = db.one("SELECT p.id, p.name FROM devices d JOIN people p ON p.id = d.person_id "
                       "WHERE d.mac = ?", (client.mac,))
        if owner:
            return render(request, "register.html", step="already", client=client, owner=owner)

        if request.method == "POST":
            form = await checked_form(request)
            if form is None:
                flash(request, "Your session expired; please try again.", "error")
                return back("/register")
            name = " ".join(str(form.get("name", "")).split())[:80]
            if settings.register_code:
                await asyncio.sleep(0.5)  # slow down guessing
                if not hmac.compare_digest(str(form.get("code", "")).strip(), settings.register_code):
                    flash(request, "That registration code isn't right.", "error")
                    return back("/register")
            if not name:
                flash(request, "Please enter your name.", "error")
                return back("/register")
            with db.connect() as conn:
                row = conn.execute("SELECT id, name FROM people WHERE lower(name) = lower(?) "
                                   "ORDER BY active DESC, id LIMIT 1", (name,)).fetchone()
                if row:
                    pid, name = row["id"], row["name"]
                    conn.execute("UPDATE people SET active = 1 WHERE id = ?", (pid,))
                else:
                    pid = conn.execute("INSERT INTO people (name, created_at) VALUES (?, ?)",
                                       (name, time.time())).lastrowid
                conn.execute(
                    """INSERT INTO devices (mac, person_id, client_name, first_seen, last_seen, connected)
                       VALUES (?, ?, ?, ?, ?, 1)
                       ON CONFLICT(mac) DO UPDATE SET person_id = excluded.person_id, ignored = 0""",
                    (client.mac, pid, client.name, time.time(), time.time()))
            log.info("Self-registered %s (%s) for %s from %s", client.mac, client.name, name,
                     request.client.host if request.client else "?")
            poller.request_poll()
            return render(request, "register.html", step="done", client=client, person_name=name)

        return render(request, "register.html", step="form", client=client,
                      need_code=bool(settings.register_code))

    # ----- login

    async def login(request: Request):
        nxt = request.query_params.get("next", "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if request.method == "POST":
            form = await checked_form(request)
            if form is None:
                flash(request, "Your session expired; please try again.", "error")
                return back(f"/login?next={nxt}")
            pw = str(form.get("password", ""))
            await asyncio.sleep(0.5)  # slow down guessing
            if settings.admin_password and hmac.compare_digest(pw, settings.admin_password):
                request.session["role"] = "admin"
                return back(nxt)
            if settings.view_password and hmac.compare_digest(pw, settings.view_password):
                request.session["role"] = "viewer"
                return back(nxt if not nxt.startswith("/admin") else "/")
            flash(request, "That password didn't work.", "error")
            return back(f"/login?next={nxt}")
        return render(request, "login.html", next=nxt)

    async def logout(request: Request):
        request.session.clear()
        return back("/")

    # ----- admin: people

    def admin_guard(request: Request):
        if not settings.admin_password:
            return render(request, "admin_disabled.html")
        if not is_admin(request):
            return to_login(request)
        return None

    async def people_page(request: Request):
        if (r := admin_guard(request)):
            return r
        if request.method == "POST":
            form = await checked_form(request)
            if form is None:
                return back("/admin/people")
            action = form.get("action")
            if action == "add":
                name = str(form.get("name", "")).strip()
                if name:
                    pid = db.run("INSERT INTO people (name, created_at) VALUES (?, ?)", (name, time.time()))
                    flash(request, f"Added {name}. Now pick their phone from the devices list.")
                    return back(f"/admin/devices?for={pid}")
            elif action == "import":
                added, assigned, problems = import_v1(db, str(form.get("text", "")))
                flash(request, f"Imported {added} new people and {assigned} devices.")
                for p in problems[:20]:
                    flash(request, p, "error")
                poller.request_poll()
            return back("/admin/people")

        people = db.all(
            """SELECT p.*, pr.state, pr.last_seen,
                      (SELECT COUNT(*) FROM devices d WHERE d.person_id = p.id) AS device_count
               FROM people p LEFT JOIN presence pr ON pr.person_id = p.id
               ORDER BY p.active DESC, p.name COLLATE NOCASE""")
        return render(request, "people.html", people=people)

    async def person_page(request: Request):
        if (r := admin_guard(request)):
            return r
        pid = request.path_params["pid"]
        person = db.one("SELECT * FROM people WHERE id = ?", (pid,))
        if not person:
            return back("/admin/people")

        if request.method == "POST":
            form = await checked_form(request)
            if form is None:
                return back(f"/admin/people/{pid}")
            action = form.get("action")
            if action == "rename":
                name = str(form.get("name", "")).strip()
                if name:
                    db.run("UPDATE people SET name = ? WHERE id = ?", (name, pid))
                    flash(request, "Name saved.")
            elif action == "toggle":
                db.run("UPDATE people SET active = 1 - active WHERE id = ?", (pid,))
                flash(request, "Hidden from the board." if person["active"] else "Back on the board.")
            elif action == "delete":
                db.run("DELETE FROM people WHERE id = ?", (pid,))
                flash(request, f"Deleted {person['name']} and their history.")
                return back("/admin/people")
            elif action == "add_mac":
                mac = normalize_mac(str(form.get("mac", "")))
                if not mac:
                    flash(request, "That doesn't look like a MAC address (aa:bb:cc:dd:ee:ff).", "error")
                else:
                    assign_device(request, mac, int(pid))
            elif action == "unassign":
                unassign_device(str(form.get("mac", "")))
                flash(request, "Device removed from this person.")
            return back(f"/admin/people/{pid}")

        devices = db.all("SELECT * FROM devices WHERE person_id = ? ORDER BY last_seen DESC", (pid,))
        events = db.all("SELECT * FROM events WHERE person_id = ? ORDER BY at DESC LIMIT 25", (pid,))
        return render(request, "person.html", person=person, devices=devices, events=events,
                      names=presence.location_names(db))

    def assign_device(request: Request, mac: str, pid: int | None) -> None:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO devices (mac, person_id) VALUES (?, ?)
                   ON CONFLICT(mac) DO UPDATE SET person_id = excluded.person_id, ignored = 0""",
                (mac, pid))
        if is_randomized(mac):
            flash(request, f"{mac} is a private (randomized) Wi-Fi address. It may change; ask them to "
                           "turn off Private Wi-Fi Address for this network.", "warn")
        else:
            flash(request, "Device assigned.")
        poller.request_poll()

    def unassign_device(mac: str) -> None:
        with db.connect() as conn:
            row = conn.execute("SELECT person_id FROM devices WHERE mac = ?", (mac,)).fetchone()
            conn.execute("UPDATE devices SET person_id = NULL WHERE mac = ?", (mac,))
            if row and row["person_id"]:
                left = conn.execute("SELECT COUNT(*) FROM devices WHERE person_id = ?",
                                    (row["person_id"],)).fetchone()[0]
                if left == 0:  # no devices left: forget presence so it starts fresh later
                    conn.execute("DELETE FROM presence WHERE person_id = ?", (row["person_id"],))

    # ----- admin: devices (auto-discovered from UniFi)

    async def devices_page(request: Request):
        if (r := admin_guard(request)):
            return r
        qp = request.query_params
        if request.method == "POST":
            form = await checked_form(request)
            ret = str(form.get("return", "/admin/devices")) if form else "/admin/devices"
            if not ret.startswith("/admin/devices"):
                ret = "/admin/devices"
            if form is None:
                return back(ret)
            mac = normalize_mac(str(form.get("mac", "")))
            action = form.get("action")
            if mac and action == "assign":
                pid = str(form.get("person_id", ""))
                if pid.isdigit():
                    assign_device(request, mac, int(pid))
                else:
                    unassign_device(mac)
                    flash(request, "Device unassigned.")
            elif mac and action in ("ignore", "unignore"):
                db.run("UPDATE devices SET ignored = ? WHERE mac = ?", (1 if action == "ignore" else 0, mac))
            return back(ret)

        show = qp.get("show", "unassigned")
        q = qp.get("q", "").strip()
        for_pid = qp.get("for", "")
        people = db.all("SELECT id, name FROM people WHERE active = 1 ORDER BY name COLLATE NOCASE")
        for_person = next((p for p in people if str(p["id"]) == for_pid), None)

        where, params = [], []
        if show == "unassigned":
            where.append("d.person_id IS NULL AND d.ignored = 0")
        elif show == "assigned":
            where.append("d.person_id IS NOT NULL")
        elif show == "ignored":
            where.append("d.ignored = 1")
        net = qp.get("net", "staff")
        net_filtered = bool(settings.wifi_networks) and show == "unassigned" and net != "all" and not q
        if net_filtered:
            # Phones last seen on a watched network. If UniFi isn't giving network names,
            # devices with no known network stay visible rather than vanishing.
            marks = ",".join("?" * len(settings.wifi_networks))
            if poller.ssid_ok is False:
                where.append(f"(d.last_ssid IS NULL OR lower(d.last_ssid) IN ({marks}))")
            else:
                where.append(f"lower(d.last_ssid) IN ({marks})")
            params += [n.lower() for n in settings.wifi_networks]
        if q:
            where.append("(lower(d.client_name) LIKE ? OR d.mac LIKE ? OR lower(p.name) LIKE ?)")
            like = f"%{q.lower()}%"
            params += [like, f"%{(normalize_mac(q) or q.lower())}%", like]
        sql = ("SELECT d.*, p.name AS person_name FROM devices d LEFT JOIN people p ON p.id = d.person_id "
               + ("WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY d.connected DESC, d.last_seen DESC LIMIT 500")
        devices = [dict(d) for d in db.all(sql, tuple(params))]

        # Suggest an owner when the device name contains part of a person's name ("Jens-iPhone").
        tokens = [(p, t) for p in people for t in p["name"].lower().replace("'", "").split() if len(t) >= 3]
        for d in devices:
            d["suggest"] = None
            if d["person_id"] is None and d["client_name"]:
                cname = d["client_name"].lower().replace("'", "").replace("’", "")
                hits = {p["id"]: p for p, t in tokens if t in cname}
                if len(hits) == 1:
                    d["suggest"] = next(iter(hits.values()))
        if for_person:
            devices.sort(key=lambda d: (not (d["suggest"] and d["suggest"]["id"] == for_person["id"]),
                                        not d["connected"]))

        return render(request, "devices.html", devices=devices, people=people, show=show, q=q,
                      net_filtered=net_filtered, net=net,
                      for_person=for_person, names=presence.location_names(db),
                      return_url=str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""))

    # ----- admin: locations

    async def locations_page(request: Request):
        if (r := admin_guard(request)):
            return r
        if request.method == "POST":
            form = await checked_form(request)
            if form is not None:
                mac = normalize_mac(str(form.get("mac", "")))
                alias = str(form.get("alias", "")).strip() or None
                if mac:
                    db.run("UPDATE locations SET alias = ? WHERE mac = ?", (alias, mac))
                    flash(request, "Location name saved.")
            return back("/admin/locations")
        rows = db.all(
            """SELECT l.*, (SELECT COUNT(*) FROM devices d WHERE d.connected = 1 AND d.last_uplink = l.mac)
                      AS clients
               FROM locations l ORDER BY (l.kind = 'ap') DESC, COALESCE(l.alias, l.name) COLLATE NOCASE""")
        return render(request, "locations.html", locations=rows)

    # ----- admin: status

    async def status_page(request: Request):
        if (r := admin_guard(request)):
            return r
        if request.method == "POST":
            form = await checked_form(request)
            if form is not None:
                await poller.poll_now()
                st = db.one("SELECT * FROM poll_status WHERE id = 1")
                if st["last_error"] and (st["last_attempt"] or 0) > (st["last_success"] or 0):
                    flash(request, f"Poll failed: {st['last_error']}", "error")
                else:
                    flash(request, f"Poll OK: UniFi reported {st['client_count']} connected clients.")
            return back("/admin/status")
        st = db.one("SELECT * FROM poll_status WHERE id = 1")
        counts = db.one(
            """SELECT (SELECT COUNT(*) FROM people WHERE active = 1) AS people,
                      (SELECT COUNT(*) FROM devices WHERE person_id IS NOT NULL) AS tracked,
                      (SELECT COUNT(*) FROM devices) AS known,
                      (SELECT COUNT(*) FROM locations) AS locations,
                      (SELECT COUNT(*) FROM events) AS events""")
        return render(request, "status.html", visitor_ip=(request.client.host if request.client else ""), visitor_masked=masked_by_docker(request.client.host if request.client else ""), ssid_ok=poller.ssid_ok, ssid_note=poller.ssid_note, st=st, counts=counts, s=settings,
                      failing=bool(st["last_error"]) and (st["last_attempt"] or 0) > (st["last_success"] or 0))

    async def admin_root(request: Request):
        return back("/admin/people")

    # ----- admin: backup (export / import)

    async def backup_page(request: Request):
        if (r := admin_guard(request)):
            return r
        if request.method == "POST":
            form = await checked_form(request)
            if form is None:
                flash(request, "Your session expired; please try again.", "error")
                return back("/admin/backup")
            text = ""
            upload = form.get("file")
            if upload is not None and hasattr(upload, "read"):
                raw = await upload.read()
                text = raw.decode("utf-8-sig", errors="replace")
            if not text.strip():
                text = str(form.get("text", ""))
            if not text.strip():
                flash(request, "Choose an export file or paste a list first.", "error")
                return back("/admin/backup")
            replace = form.get("mode") == "replace"
            if replace and str(form.get("confirm", "")).strip().upper() != "REPLACE":
                flash(request, 'To replace everything, type REPLACE in the box. Nothing was changed.', "error")
                return back("/admin/backup")
            try:
                result = import_text(db, text, replace=replace)
            except ImportError_ as e:
                flash(request, f"Nothing imported: {e}", "error")
                return back("/admin/backup")
            flash(request, result.summary())
            for p in result.problems[:20]:
                flash(request, p, "warn")
            if len(result.problems) > 20:
                flash(request, f"...and {len(result.problems) - 20} more problems.", "warn")
            poller.request_poll()
            return back("/admin/people")
        counts = db.one(
            """SELECT (SELECT COUNT(*) FROM people) AS people,
                      (SELECT COUNT(*) FROM devices WHERE person_id IS NOT NULL) AS devices,
                      (SELECT COUNT(*) FROM locations WHERE alias IS NOT NULL) AS aliases,
                      (SELECT COUNT(*) FROM events) AS events""")
        return render(request, "backup.html", counts=counts)

    async def export_download(request: Request):
        if (r := admin_guard(request)):
            return r
        include_history = request.query_params.get("history") == "1"
        data = export_data(db, include_history=include_history, tz=settings.timezone)
        stamp = datetime.now(settings.timezone).strftime("%Y-%m-%d")
        body = json.dumps(data, indent=2, ensure_ascii=False)
        return Response(body, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="whoshere-export-{stamp}.json"',
            "Cache-Control": "no-store"})

    # ----- revision history

    # ----- admin: Google Chat

    async def chat_page(request: Request):
        if (r := admin_guard(request)):
            return r
        if request.method == "POST":
            form = await checked_form(request)
            if form is None:
                flash(request, "Your session expired; please try again.", "error")
                return back("/admin/chat")
            action = form.get("action")
            if action == "upload":
                upload = form.get("key")
                raw = (await upload.read()) if upload is not None and hasattr(upload, "read") else b""
                if not raw.strip():
                    raw = str(form.get("key_text", "")).encode()
                try:
                    key = check_key(json.loads(raw.decode("utf-8-sig")))
                except ValueError:
                    flash(request, "That file isn't valid JSON. Upload the .json key you downloaded from Google Cloud.", "error")
                    return back("/admin/chat")
                except ChatSetupError as e:
                    flash(request, str(e), "error")
                    return back("/admin/chat")
                path = settings.chat_key_file
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(key))
                try:
                    tmp.chmod(0o600)
                except OSError:
                    pass
                tmp.replace(path)
                chat.reset()
                flash(request, f"Key saved for {key['client_email']}.")
            elif action == "remove":
                try:
                    settings.chat_key_file.unlink()
                except FileNotFoundError:
                    pass
                chat.reset()
                flash(request, "Service account key removed.")
            elif action == "test":
                try:
                    email = await asyncio.to_thread(chat.check)
                    flash(request, f"Google accepted the key for {email}.")
                except ChatSetupError as e:
                    flash(request, str(e), "error")
                except ConnectionError as e:
                    flash(request, f"Can't reach Google: {e}", "error")
            return back("/admin/chat")

        key_email, key_error = None, None
        if settings.chat_key_file.exists():
            try:
                key_email = json.loads(settings.chat_key_file.read_text()).get("client_email")
            except (OSError, ValueError):
                key_error = "The saved key can't be read. Upload it again."
        return render(request, "chat.html", chat=chat, key_email=key_email, key_error=key_error,
                      commands=sorted(chatbot.COMMANDS.items()), s=settings)

    # ----- help and cheat sheets

    def help_context(request: Request) -> dict:
        generic = request.query_params.get("generic") == "1"
        address = "" if generic else (settings.board_url or str(request.base_url).rstrip("/"))
        return dict(
            board_address=address,
            generic=generic,
            chat_on=bool(settings.chat_subscription) or generic,
            register_on=settings.self_register or generic,
            grace_min=settings.departure_grace // 60,
            stale_min=max(1, settings.stale_after // 60),
            staff_wifi_names=list(settings.wifi_networks),
            commands=sorted(chatbot.COMMANDS.items()),
        )

    async def help_page(request: Request):
        if not can_view(request):
            return to_login(request)
        return render(request, "help.html", **help_context(request))

    async def cheat_sheet(request: Request):
        if not can_view(request):
            return to_login(request)
        sheet = request.path_params["sheet"]
        if sheet not in ("board", "chat"):
            return back("/help")
        return templates.TemplateResponse(request, "sheet.html", {
            **help_context(request), "sheet": sheet, "title": settings.title, "version": __version__})

    async def revisions_page(request: Request):
        if not can_view(request):
            return to_login(request)
        return render(request, "revisions.html")

    # ----- lifecycle

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(poller.run()) if start_poller else None
        chat_task = asyncio.create_task(chat.run()) if start_poller and chat.enabled else None
        if not settings.admin_password:
            log.warning("ADMIN_PASSWORD is not set; admin pages are disabled")
        yield
        for t in (task, chat_task):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    routes = [
        Route("/", board_page),
        Route("/history", history_page),
        Route("/api/status", api_status),
        Route("/healthz", healthz),
        Route("/manifest.webmanifest", manifest),
        Route("/register", register_page, methods=["GET", "POST"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout),
        Route("/admin", admin_root),
        Route("/admin/people", people_page, methods=["GET", "POST"]),
        Route("/admin/people/{pid:int}", person_page, methods=["GET", "POST"]),
        Route("/admin/devices", devices_page, methods=["GET", "POST"]),
        Route("/admin/locations", locations_page, methods=["GET", "POST"]),
        Route("/admin/status", status_page, methods=["GET", "POST"]),
        Route("/admin/backup", backup_page, methods=["GET", "POST"]),
        Route("/admin/export", export_download),
        Route("/revisions", revisions_page),
        Route("/help", help_page),
        Route("/help/sheet/{sheet}", cheat_sheet),
        Route("/admin/chat", chat_page, methods=["GET", "POST"]),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    middleware = [Middleware(SessionMiddleware, secret_key=settings.secret_key,
                             session_cookie="whoshere_session", max_age=60 * 60 * 24 * 30,
                             same_site="lax")]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.db = db
    app.state.poller = poller
    app.state.chat = chat
    return app
