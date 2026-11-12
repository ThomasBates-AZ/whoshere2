"""MAC address helpers."""
from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[\s:\-.]")
_HEX12 = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(value: str | None) -> str | None:
    """Return a MAC as lowercase aa:bb:cc:dd:ee:ff, or None if it isn't one.

    Accepts colons, dashes, dots (Cisco style) or no separators at all.
    """
    if not value:
        return None
    compact = _SEPARATORS.sub("", value.strip().lower())
    if not _HEX12.match(compact):
        return None
    return ":".join(compact[i:i + 2] for i in range(0, 12, 2))


def is_randomized(mac: str) -> bool:
    """True if the MAC is "locally administered" - what phones use for private Wi-Fi addresses.

    A tracked device with a private address will look like a new device on every
    network (and sometimes every few days), so the UI warns about these.
    """
    try:
        return bool(int(mac[1], 16) & 0x2)
    except (ValueError, IndexError):
        return False
