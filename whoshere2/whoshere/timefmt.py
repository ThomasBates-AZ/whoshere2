"""Time formatting shared by the web pages and Google Chat replies."""
from __future__ import annotations

import time
from datetime import datetime, timedelta


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
