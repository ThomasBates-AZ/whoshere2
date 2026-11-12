"""Web UI and JSON API."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
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
from .config import Settings
from .db import Database
from .importer import import_v1
from .macutil import is_randomized, normalize_mac
from .poller import Poller

log = logging.getLogger("whoshere.web")
HERE = Path(__file__).parent


# --------------------------------------------------------------------------- time formatting

def make_filters(tz):
    def local(ts):
        return datetime.fromtimestamp(ts, tz)

    def when(ts):
        if not ts:
            return "—"
        dt, now = local(ts), datetime.now(tz)
        clock = dt.strftime("%-I:%M %p")
        if dt.date() == now.date():
            return clock
        if dt.date() == (now - timedelta(days=1)).date():
            return f"Yesterday {clock}"
        if (now.date() - dt.date()).days < 7:
            return f"{dt.strftime('%a')} {clock}"
        if dt.year == now.year:
            return f"{dt.strftime('%b %-d')}, {clock}"
        return f"{dt.strftime('%b %-d, %Y')}, {clock}"

    def ago(ts):
        if not ts:
            return "never"
        s = max(0, int(time.time() - ts))
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s // 60} min ago"
        if s < 86400:
            return f"{s // 3600} hr ago"
        days = s // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"

    def clock(ts):
        return local(ts).strftime("%-I:%M %p") if ts else "—"

    def day(ts):
        return local(ts).strftime("%A, %B %-d, %Y")

    return {"when": when, "ago": ago, "clock": clock, "day": day}


# --------------------------------------------------------------------------- app

def create_app(settings: Settings, db: Database | None = None, start_poller: bool = True) -> Starlette:
    db = db or Database(settings.db_path)
    poller = Poller(settings, db)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(make_filters(settings.timezone))
    templates.env.globals.update(title=settings.title, version=__version__,
                                 is_randomized=is_randomized)

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
        return render(request, "board.html", b=state, refresh=30)

    async def api_status(request: Request):
        if not can_view(request):
            return JSONResponse({"error": "login required"}, status_code=401)
        state = presence.board(db, settings.stale_after)
        return JSONResponse({
            "all_clear": state["verdict"] == "clear",
            "verdict": state["verdict"],
            "on_site": [{"name": p["name"], "location": p["location"], "since": p["since"],
                         "last_seen": p["last_seen"], "leaving": p["leaving"]} for p in state["here"]],
            "people": [{k: p[k] for k in ("name", "state", "location", "since", "last_seen")}
                       for p in state["people"]],
            "last_success": state["last_success"],
            "last_error": state["last_error"],
        })

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
            grouped[-1][1].append({**dict(e), "location": names.get(e["uplink"]) if e["uplink"] else None})
        people = db.all("SELECT id, name FROM people ORDER BY name COLLATE NOCASE")
        return render(request, "history.html", grouped=grouped, people=people,
                      days=days, person=person, truncated=len(events) >= 2000)

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
        return render(request, "status.html", st=st, counts=counts, s=settings,
                      failing=bool(st["last_error"]) and (st["last_attempt"] or 0) > (st["last_success"] or 0))

    async def admin_root(request: Request):
        return back("/admin/people")

    # ----- lifecycle

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(poller.run()) if start_poller else None
        if not settings.admin_password:
            log.warning("ADMIN_PASSWORD is not set; admin pages are disabled")
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    routes = [
        Route("/", board_page),
        Route("/history", history_page),
        Route("/api/status", api_status),
        Route("/healthz", healthz),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout),
        Route("/admin", admin_root),
        Route("/admin/people", people_page, methods=["GET", "POST"]),
        Route("/admin/people/{pid:int}", person_page, methods=["GET", "POST"]),
        Route("/admin/devices", devices_page, methods=["GET", "POST"]),
        Route("/admin/locations", locations_page, methods=["GET", "POST"]),
        Route("/admin/status", status_page, methods=["GET", "POST"]),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    middleware = [Middleware(SessionMiddleware, secret_key=settings.secret_key,
                             session_cookie="whoshere_session", max_age=60 * 60 * 24 * 30,
                             same_site="lax")]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.db = db
    app.state.poller = poller
    return app
