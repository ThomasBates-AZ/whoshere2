"""Command line entry point.

    python -m whoshere                      run the web app + poller (what the container does)
    python -m whoshere poll-once            poll UniFi once and print what it saw (setup check)
    python -m whoshere import-users FILE    import a v1 users.txt
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
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

    import uvicorn
    from .web import create_app
    app = create_app(settings)
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips="*", log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
