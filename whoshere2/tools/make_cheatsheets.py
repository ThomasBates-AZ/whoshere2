"""Render the printable cheat sheets to PDF (needs Playwright + Chromium; a dev tool, not used by the app).

    python tools/make_cheatsheets.py                   # generic PDFs shipped with the app (blank address)
    python tools/make_cheatsheets.py --out ~/Desktop \\
        --board-url http://whoshere.example.org:8085 \\
        --wifi "Staff, Office" --self-register        # tailored PDFs to hand out
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NAMES = {"board": "whoshere-quick-guide.pdf", "chat": "whoshere-google-chat-guide.pdf"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "whoshere" / "static" / "help"))
    ap.add_argument("--board-url", default="", help="address printed on the sheets; omit for a blank line")
    ap.add_argument("--wifi", default="", help="staff Wi-Fi names, comma-separated")
    ap.add_argument("--self-register", action="store_true", help="mention the Add my phone page")
    ap.add_argument("--chrome", default=os.environ.get("CHROME", ""), help="path to Chrome/Chromium")
    args = ap.parse_args()
    generic = not args.board_url

    os.environ.update({
        "DATA_DIR": tempfile.mkdtemp(), "UNIFI_URL": "http://127.0.0.1:9", "UNIFI_API_KEY": "x",
        "BOARD_URL": args.board_url, "WIFI_NETWORKS": args.wifi,
        "SELF_REGISTER": "true" if args.self_register else "false",
        "GOOGLE_CHAT_SUBSCRIPTION": "projects/x/subscriptions/x",
    })
    import uvicorn
    from playwright.async_api import async_playwright
    from whoshere.config import Settings
    from whoshere.web import create_app

    app = create_app(Settings.from_env(), start_poller=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    async def render():
        async with async_playwright() as p:
            browser = await p.chromium.launch(**({"executable_path": args.chrome} if args.chrome else {}))
            page = await browser.new_page()
            for sheet, name in NAMES.items():
                await page.goto(f"{base}/help/sheet/{sheet}" + ("?generic=1" if generic else ""))
                await page.pdf(path=str(out / name), format="Letter", print_background=True,
                               prefer_css_page_size=True)
                print("wrote", out / name)
            await browser.close()

    asyncio.run(render())
    server.should_exit = True


if __name__ == "__main__":
    main()
