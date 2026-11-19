"""Spot when Docker's port forwarding hides visitors' real IP addresses.

On a bridge network (the Docker default), some hosts, Synology among them, hand the
app every connection from the bridge's gateway address (e.g. 172.17.0.1) instead of
the visitor's. "Add my phone" then can't tell which phone is asking. Host networking
(NETWORK_MODE=host) fixes it.
"""
from __future__ import annotations

import os
import socket
import struct

IN_CONTAINER = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def default_gateway(route_file: str = "/proc/net/route") -> str | None:
    """IPv4 default gateway of this container/host, or None."""
    try:
        with open(route_file) as fh:
            lines = fh.read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 0x2:
            return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    return None


def masked_by_docker(ip: str | None, gateway: str | None = None, in_container: bool | None = None) -> bool:
    """True if a visitor's address is really just Docker's gateway."""
    in_container = IN_CONTAINER if in_container is None else in_container
    if not ip or not in_container:
        return False
    gateway = default_gateway() if gateway is None else gateway
    return ip.removeprefix("::ffff:") == gateway
