"""Background loop that polls UniFi on a timer."""
from __future__ import annotations

import asyncio
import logging
import time

from . import presence
from .config import Settings
from .db import Database
from .unifi import UniFiError, make_client

log = logging.getLogger("whoshere.poller")

PRUNE_EVERY = 3600  # seconds


class Poller:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._client = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_prune = 0.0

    def poll_once(self) -> None:
        """Blocking; run in a worker thread."""
        try:
            if self._client is None:
                self._client = make_client(self.settings)
            snap = self._client.snapshot()
        except UniFiError as e:
            log.warning("Poll failed: %s", e)
            presence.record_failure(self.db, str(e))
            return
        except Exception as e:  # keep the loop alive no matter what
            log.exception("Unexpected error polling UniFi")
            presence.record_failure(self.db, f"Unexpected error: {e}")
            self._client = None
            return

        presence.apply_snapshot(self.db, snap, grace=self.settings.departure_grace)
        log.info("Poll OK: %d clients, %d network devices", len(snap.clients), len(snap.devices))

        if time.time() - self._last_prune > PRUNE_EVERY:
            presence.prune(self.db, self.settings.history_days, self.settings.device_retention_days)
            self._last_prune = time.time()

    async def poll_now(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self.poll_once)

    def request_poll(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            await self.poll_now()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.poll_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
