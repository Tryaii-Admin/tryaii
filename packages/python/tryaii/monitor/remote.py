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

First answer (TS-296): the first poll asks with ``wait=0`` so the server
answers at once (200 when the cached etag is stale or missing, else 304),
and :attr:`RemotePoller.first_answer` is set when it returns, whatever the
outcome. The transport lets the first call of the process wait for it, at
most 0.5 s, once.
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


_app_name_warned: set = set()
_app_name_lock = threading.Lock()


def app_name_warning(local_app: str, remote) -> Optional[str]:
    """The TS-308 warning when the key's app (RemoteConfig ``app_name``,
    API.md v1.8) differs from the local app name (exact compare), else
    None (also without ``app_name``: a v1.7 server)."""
    if not isinstance(remote, dict):
        return None
    name = remote.get("app_name")
    if not isinstance(name, str) or not name or name == local_app:
        return None
    return (f'tryaii monitor: app="{local_app}" uploads to app "{name}" (the app of '
            f"TRYAII_API_KEY). To use a separate app, run: tryaii apps create {local_app} "
            f"and use its key.")


def warn_app_name(local_app: str, remote) -> None:
    """Log :func:`app_name_warning` once per local app per OS process."""
    text = app_name_warning(local_app, remote)
    if text is None:
        return
    with _app_name_lock:
        if local_app in _app_name_warned:
            return
        _app_name_warned.add(local_app)
    logger.warning(text)


def reset_app_name_warnings() -> None:
    """Forget which apps were warned about (tests)."""
    with _app_name_lock:
        _app_name_warned.clear()


def remote_enabled(env=None) -> bool:
    """False when ``TRYAII_MONITOR_REMOTE`` is ``"0"``."""
    env = os.environ if env is None else env
    return (env.get(ENV_REMOTE) or "1").strip() != "0"


def _utcnow_ts() -> str:
    return format_ts(datetime.now(timezone.utc))


def config_url(base_url: str, etag: Optional[str], wait: int = WAIT_S) -> str:
    return (f"{base_url.rstrip('/')}/v1/monitor/config"
            f"?etag={quote(etag or '', safe='')}&wait={wait}")


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
        #: Set once the first poll returned (any outcome), or when the
        #: poller cannot run: the first-call wait ends here (TS-296).
        self.first_answer = threading.Event()
        if self.disabled:
            self.first_answer.set()

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

    def poll_once(self, wait: int = WAIT_S) -> str:
        """One long poll (``wait`` seconds server-side). Returns ``changed``,
        ``unchanged``, ``error`` or ``disabled``. Never raises."""
        if self.disabled:
            return "disabled"
        try:
            resp = api.request("GET", config_url(self.base_url, self.etag, wait),
                               bearer=self.key, opener=self._opener,
                               timeout=HTTP_TIMEOUT_S)
        except api.ApiNetworkError:
            return "error"
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor remote poll failed", exc_info=True)
            return "error"
        if resp.status == 304:
            # The cached answer still holds: warn from it (TS-308), since a
            # long-lived mismatch is never answered with a 200 again.
            for directory in self.dirs:
                warn_app_name(directory.name, store.read_remote(directory))
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
        for directory in self.dirs:
            warn_app_name(directory.name, body)
        return "changed"

    def start(self) -> None:
        if self._thread is not None or self.disabled:
            return
        self._thread = threading.Thread(target=self._loop, name="tryaii-monitor-remote",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._registered.wait()
        first = True
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                # The first poll is answered at once (wait=0): the first
                # call of the process may be waiting for it.
                outcome = self.poll_once(0 if first else WAIT_S)
            finally:
                if first:
                    first = False
                    self.first_answer.set()
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
        self.first_answer.set()
