"""Command line entry point.

    python -m whoshere                      run the web app + poller (what the container does)
    python -m whoshere poll-once            poll UniFi once and print what it saw (setup check)
    python -m whoshere import-users FILE    import a v1 users.txt
    python -m whoshere export FILE [--history]          export people/devices for a move
    python -m whoshere import FILE [--replace]          import an export (or a v1 users.txt)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import Settings
from .db import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whoshere")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the web app (default)")
    sub.add_parser("poll-once", help="poll UniFi once and print a summary")
    imp = sub.add_parser("import-users", help="import a Who's Here v1 users.txt")
    imp.add_argument("file")
    exp = sub.add_parser("export", help="export people, devices and location names to a JSON file")
    exp.add_argument("file", help="file to write, or - for the screen")
    exp.add_argument("--history", action="store_true", help="include arrival/departure history")
    imp2 = sub.add_parser("import", help="import an export file (or a v1 users.txt)")
    imp2.add_argument("file")
    imp2.add_argument("--replace", action="store_true",
                      help="delete all people, assignments, location names and history first")
    args = parser.parse_args(argv)

    logging.basicConfig(level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    if args.cmd == "poll-once":
        from .unifi import UniFiError, make_client
        try:
            snap = make_client(settings).snapshot()
        except UniFiError as e:
            print(f"FAILED: {e}", file=sys.stderr)
            return 1
        print(f"OK: {len(snap.clients)} connected clients, {len(snap.devices)} network devices")
        for d in sorted(snap.devices, key=lambda d: d.name.lower()):
            print(f"  device  {d.mac}  {d.kind or '-':7} {d.name}")
        return 0

    if args.cmd == "import-users":
        from .importer import import_v1
        with open(args.file, encoding="utf-8", errors="replace") as fh:
            added, assigned, problems = import_v1(Database(settings.db_path), fh.read())
        print(f"Imported {added} new people, {assigned} devices")
        for p in problems:
            print(f"  ! {p}")
        return 0

    if args.cmd == "export":
        import json
        from .backup import export_data
        data = export_data(Database(settings.db_path), include_history=args.history, tz=settings.timezone)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if args.file == "-":
            print(text)
        else:
            with open(args.file, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"Exported {len(data['people'])} people to {args.file}")
        return 0

    if args.cmd == "import":
        from .backup import ImportError_, import_text
        with open(args.file, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        try:
            result = import_text(Database(settings.db_path), text, replace=args.replace)
        except ImportError_ as e:
            print(f"FAILED: {e}", file=sys.stderr)
            return 1
        print(result.summary())
        for p in result.problems:
            print(f"  ! {p}")
        return 0

    import uvicorn
    from .web import create_app
    app = create_app(settings)
    uvicorn.run(app, host=os.environ.get("HOST") or "0.0.0.0", port=int(os.environ.get("PORT") or "8080"),
                proxy_headers=True, log_level="warning",
                # Only trust X-Forwarded-For from these addresses. The self-register page
                # identifies a phone by its IP, so a spoofed header must not be believed.
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS") or "127.0.0.1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
