"""Remote (dashboard) config long poll (shared/monitor/API.md §3.3, SPEC.md §3.3).

The :class:`RemotePoller` runs on its own daemon thread, started by the
transport only when ``TRYAII_API_KEY`` is set and ``TRYAII_MONITOR_REMOTE``
is not ``"0"``. It long-polls ``GET {TRYAII_API_URL}/v1/monitor/config?
etag=<etag>&wait=25``: a 200 writes the answer plus ``fetched_at`` to
``remote.json`` (atomically, in every registered app directory) and polls
again at once; a 304 polls again at once; any error waits 30 s; a 401 stops
polling for the process. The wrapper reads ``remote.json`` through the same
mtime cache as ``config.json`` and applies it on the next call (precedence
env > remote > ``wrap(mode=)`` > ``config.json`` > default). Fail-open: no
failure here ever reaches the app.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from tryaii.monitor import api, store
from tryaii.monitor.config import REMOTE_SCHEMA
from tryaii.monitor.events import format_ts
from tryaii.monitor.upload import log_key_rejected

logger = logging.getLogger("tryaii.monitor")

ENV_REMOTE = "TRYAII_MONITOR_REMOTE"
WAIT_S = 25
HTTP_TIMEOUT_S = 35.0
ERROR_WAIT_S = 30.0
MIN_INTERVAL_S = 1.0


def remote_enabled(env=None) -> bool:
    """False when ``TRYAII_MONITOR_REMOTE`` is ``"0"``."""
    env = os.environ if env is None else env
    return (env.get(ENV_REMOTE) or "1").strip() != "0"


def _utcnow_ts() -> str:
    return format_ts(datetime.now(timezone.utc))


def config_url(base_url: str, etag: Optional[str]) -> str:
    return (f"{base_url.rstrip('/')}/v1/monitor/config"
            f"?etag={quote(etag or '', safe='')}&wait={WAIT_S}")


class RemotePoller:
    """Keeps ``remote.json`` current. Test seam: ``opener``, ``now_ts`` and
    :meth:`poll_once` (one request, synchronously)."""

    def __init__(self, key: str, base_url: Optional[str] = None, *, opener=None,
                 now_ts: Callable[[], str] = _utcnow_ts):
        self.key = key
        self.base_url = (base_url or api.api_url()).rstrip("/")
        self._opener = opener
        self._now_ts = now_ts
        self._dirs: list = []
        self._lock = threading.Lock()
        self.etag: Optional[str] = None
        self.disabled = not api.is_app_key(key)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._registered = threading.Event()

    def register(self, directory) -> None:
        directory = Path(directory)
        with self._lock:
            if directory in self._dirs:
                return
            self._dirs.append(directory)
            if self.etag is None:
                cached = store.read_remote(directory)
                if cached and isinstance(cached.get("etag"), str):
                    self.etag = cached["etag"]
        self._registered.set()

    @property
    def dirs(self) -> list:
        with self._lock:
            return list(self._dirs)

    def poll_once(self) -> str:
        """One long poll. Returns ``changed``, ``unchanged``, ``error`` or
        ``disabled``. Never raises."""
        if self.disabled:
            return "disabled"
        try:
            resp = api.request("GET", config_url(self.base_url, self.etag),
                               bearer=self.key, opener=self._opener,
                               timeout=HTTP_TIMEOUT_S)
        except api.ApiNetworkError:
            return "error"
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor remote poll failed", exc_info=True)
            return "error"
        if resp.status == 304:
            return "unchanged"
        if resp.status == 401:
            self.disabled = True
            log_key_rejected()
            return "disabled"
        body = resp.body
        if resp.status != 200 or not isinstance(body, dict) \
                or body.get("schema") != REMOTE_SCHEMA:
            return "error"
        fetched = self._now_ts()
        try:
            for directory in self.dirs:
                store.write_remote(directory, body, fetched)
        except Exception:  # noqa: BLE001
            logger.debug("monitor remote.json write failed", exc_info=True)
            return "error"
        etag = body.get("etag")
        self.etag = etag if isinstance(etag, str) else None
        return "changed"

    def start(self) -> None:
        if self._thread is not None or self.disabled:
            return
        self._thread = threading.Thread(target=self._loop, name="tryaii-monitor-remote",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._registered.wait()
        while not self._stop.is_set():
            started = time.monotonic()
            outcome = self.poll_once()
            if outcome == "disabled":
                return
            if outcome == "error":
                self._stop.wait(ERROR_WAIT_S)
                continue
            # Guard against a server that answers instantly in a loop.
            spent = time.monotonic() - started
            if spent < MIN_INTERVAL_S:
                self._stop.wait(MIN_INTERVAL_S - spent)

    def stop(self) -> None:
        self._stop.set()
