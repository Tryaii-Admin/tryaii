"""Background upload of monitor events (shared/monitor/API.md §3.2, SPEC.md §3.4).

The :class:`Uploader` runs on its own daemon thread, started by the
transport only when ``TRYAII_API_KEY`` is set and no opt-out is set
(``TRYAII_MONITOR_UPLOAD=0``, ``TRYAII_TELEMETRY=0``, ``DO_NOT_TRACK=1``;
SPEC.md §9). It reads the UNREAD bytes of every ``events-*.jsonl`` of
the app directories this process records to, batches whole lines (at most
500 events and a 1 MiB body, one file per batch), and POSTs them to
``/v1/monitor/events`` -- at most one batch every 5 s, with exponential
backoff (5 s -> 5 min) on 429/5xx/network. ``upload.json`` (the cursor)
says how many bytes of each file were uploaded. Local files are never
deleted or rewritten.

Privacy (SPEC.md §9): a pass pauses an app directory while the local
setting (``tryaii monitor upload off``) or the account switch (remote.json
``"upload": false``, or a 403 ``upload_disabled``) says off; every sent line
carries the keyed ``prompt_hash`` (HMAC with the per-installation secret),
never the plain one; the first upload of a machine prints the Data Notice
once to a terminal.

Hard rules: prompt text never leaves the machine (a line carrying a field
named ``prompt``, ``messages``, ``response``, ``content``, ``text`` or
``raw`` at any depth is never sent); every failure is swallowed (fail-open:
the app's calls are never affected). The start-up key check
(``GET /v1/apps/keys/validate``) runs here, never on the hot path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from tryaii.monitor import api, privacy, store
from tryaii.monitor.callsite import is_absolute_file
from tryaii.monitor.events import EVENT_SCHEMA, format_ts
from tryaii.monitor.langfuse import settings as langfuse_settings

logger = logging.getLogger("tryaii.monitor")

UPLOAD_SCHEMA = "tryaii.monitor.upload/1"
ENV_UPLOAD = privacy.ENV_UPLOAD
MAX_EVENTS = 500
MAX_BYTES = 1024 * 1024
INTERVAL_S = 5.0
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
DRAIN_TIMEOUT_S = 2.0
LOCK_STALE_S = 60.0
FORBIDDEN_FIELDS = ("prompt", "messages", "response", "content", "text", "raw")

_rejected_logged = threading.Event()


def upload_enabled(env=None) -> bool:
    """False when an environment opt-out is set (SPEC.md §9)."""
    return privacy.upload_decision(env)[0]


def log_key_rejected() -> None:
    """The one line a process logs when its app key is rejected."""
    if not _rejected_logged.is_set():
        _rejected_logged.set()
        logger.warning("tryaii monitor: TRYAII_API_KEY was rejected (invalid_key); "
                       "upload and remote config are off for this process")


def find_forbidden(obj) -> Optional[str]:
    """The first forbidden field name anywhere in ``obj`` (dict keys at any
    depth, lists included), else None."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in FORBIDDEN_FIELDS:
                return key
            found = find_forbidden(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_forbidden(value)
            if found:
                return found
    return None


def strip_machine_path(obj: dict, raw: bytes) -> bytes:
    """An absolute ``call_site.file`` never leaves the machine (TS-226): the
    line is re-serialised with the base name only. Any other line is sent
    verbatim (``raw``)."""
    return outgoing_line(obj, raw, None)


def outgoing_line(obj: dict, raw: bytes, hash_key: Optional[bytes]) -> bytes:
    """The line as uploaded: an absolute ``call_site.file`` reduced to its
    base name (TS-226) and, with ``hash_key``, ``prompt_hash`` replaced by
    its keyed form (SPEC.md §9). Re-serialised only when something changed;
    otherwise ``raw`` verbatim."""
    changed = False
    site = obj.get("call_site")
    if isinstance(site, dict) and is_absolute_file(site.get("file")):
        site["file"] = re.split(r"[\\/]", str(site["file"]))[-1]
        changed = True
    if hash_key is not None and isinstance(obj.get("prompt_hash"), str):
        obj["prompt_hash"] = privacy.keyed_hash(hash_key, obj["prompt_hash"])
        changed = True
    if not changed:
        return raw
    return store.dumps_line(obj).rstrip("\n").encode("utf-8")


def _valid_event_id(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def batch_integrations(env=None) -> Optional[list]:
    """The batch's ``integrations`` (API.md v1.7): ``["langfuse"]`` while the
    Langfuse export is really on (``langfuse.settings``: the switch, an app
    key in ``TRYAII_API_KEY`` and both Langfuse keys), else None (the key is
    then left out). ``env`` defaults to ``os.environ``."""
    return ["langfuse"] if langfuse_settings(env)["enabled"] else None


#: ``sdk.source`` of a Claude Code import batch (API.md v1.6, §3.2.3), the only
#: accepted value; every other upload leaves the key out.
SOURCE_CLAUDE_CODE_IMPORT = "claude-code-import"


def envelope_head(batch_id: str, version: str, source: Optional[str] = None,
                  integrations: Optional[list] = None) -> bytes:
    """The request body up to the opening ``[`` of ``events`` (API.md §3.2
    key order: schema, batch_id, sdk {lang, version[, source]}, notice_version,
    then ``integrations`` (v1.7) only when given and non-empty). ``source``
    (v1.6) is set only for the Claude Code importer's directory."""
    from tryaii.monitor import NOTICE_VERSION

    sdk = {"lang": "python", "version": version}
    if source is not None:
        sdk["source"] = source
    doc = {"schema": UPLOAD_SCHEMA, "batch_id": batch_id,
           "sdk": sdk,
           "notice_version": NOTICE_VERSION}
    if integrations:
        doc["integrations"] = list(integrations)
    head = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    return (head[:-1] + ',"events":[').encode("utf-8")


def build_body(batch_id: str, version: str, lines: list,
               source: Optional[str] = None,
               integrations: Optional[list] = None) -> bytes:
    """The exact request body: the envelope with the outgoing event lines
    (:func:`outgoing_line`) spliced in."""
    return (envelope_head(batch_id, version, source, integrations)
            + b",".join(lines) + b"]}")


class Batch:
    """Whole lines of ONE events file: ``lines`` to send (raw bytes without
    the newline) and the byte range ``[start, end)`` they cover, including
    skipped lines."""

    __slots__ = ("file", "start", "end", "lines", "skipped", "forbidden")

    def __init__(self, file: str, start: int):
        self.file = file
        self.start = start
        self.end = start
        self.lines: list = []
        self.skipped = 0
        self.forbidden: Optional[str] = None


def next_batch(directory, cursor: dict, *, max_events: int = MAX_EVENTS,
               max_bytes: int = MAX_BYTES, head_bytes: int = 0,
               hash_key: Optional[bytes] = None) -> Optional[Batch]:
    """The next batch of unread lines (oldest file first), or None when
    every file is fully uploaded. A batch may hold no lines (only skipped
    ones): the caller then just advances the cursor.

    Lines are skipped (never sent) when they are not JSON objects of schema
    ``tryaii.monitor.event/1``, lack a uuid ``event_id`` (slice-1 lines),
    or carry a forbidden field. A trailing line without ``\\n`` is left for
    later. A file shorter than its cursor (rewritten) restarts at 0.
    """
    files = cursor.setdefault("files", {})
    budget = max_bytes - head_bytes - 2  # the closing "]}"
    for path in store.event_files(directory):
        name = path.name
        offset = files.get(name, 0)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < offset:
            offset = 0
        if size == offset:
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(size - offset)
        except OSError:
            continue
        batch = Batch(name, offset)
        used = 0
        pos = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl < 0:
                break  # partial last line: wait for the writer
            raw = data[pos:nl].rstrip(b"\r")
            line_end = nl + 1
            keep = False
            try:
                obj = json.loads(raw.decode("utf-8")) if raw.strip() else None
            except (UnicodeDecodeError, ValueError):
                obj = None
            if isinstance(obj, dict) and obj.get("schema") == EVENT_SCHEMA \
                    and _valid_event_id(obj.get("event_id")):
                bad = find_forbidden(obj)
                if bad:
                    batch.forbidden = batch.forbidden or bad
                else:
                    keep = True
                    raw = outgoing_line(obj, raw, hash_key)
            if keep:
                cost = len(raw) + (1 if batch.lines else 0)
                if batch.lines and (len(batch.lines) >= max_events or used + cost > budget):
                    break
                if not batch.lines and cost > budget:
                    batch.skipped += 1  # one line larger than a whole batch
                else:
                    batch.lines.append(raw)
                    used += cost
            else:
                batch.skipped += 1
            pos = line_end
            batch.end = offset + line_end
            if len(batch.lines) >= max_events:
                break
        if batch.end > batch.start:
            return batch
    return None


def _utcnow_ts() -> str:
    return format_ts(datetime.now(timezone.utc))


class _DirLock:
    """``upload.lock`` (O_EXCL): one uploading process per app directory at
    a time; a lock older than 60 s is taken over (crashed holder)."""

    def __init__(self, directory):
        self.path = Path(directory) / store.UPLOAD_LOCK_FILE

    def acquire(self) -> bool:
        for _attempt in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age < LOCK_STALE_S:
                    return False
                try:
                    os.unlink(str(self.path))
                except OSError:
                    return False
                continue
            except OSError:
                return False
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return True
        return False

    def release(self) -> None:
        try:
            os.unlink(str(self.path))
        except OSError:
            pass


class Uploader:
    """Uploads the registered app directories' events in the background.

    Test seam: ``opener`` (urlopen signature), ``clock`` (monotonic
    seconds) and ``now_ts`` (the ``last_ok`` timestamp) are injectable, and
    :meth:`run_once` performs one pass synchronously without a thread.
    """

    def __init__(self, key: str, base_url: Optional[str] = None, *, opener=None,
                 clock: Callable[[], float] = time.monotonic,
                 now_ts: Callable[[], str] = _utcnow_ts, version: Optional[str] = None,
                 validate: bool = True):
        self.key = key
        self.base_url = (base_url or api.api_url()).rstrip("/")
        self._opener = opener
        self._clock = clock
        self._now_ts = now_ts
        if version is None:
            from tryaii import __version__ as version
        self._version = version
        self._dirs: list = []
        self._sources: dict = {}
        self._lock = threading.Lock()
        self._pass_lock = threading.Lock()
        self.disabled = not api.is_app_key(key)
        self.validated: Optional[dict] = None if validate else {}
        self.backoff = 0.0
        self.next_at = 0.0
        self.sent_events = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        if self.disabled:
            log_key_rejected()

    # -- registration ----------------------------------------------------

    def register(self, directory, source: Optional[str] = None) -> None:
        """Upload ``directory``; ``source`` (API.md v1.6) labels its batches'
        ``sdk.source`` (only ``SOURCE_CLAUDE_CODE_IMPORT``, by the Claude Code
        importer; wrapped-call directories leave it out)."""
        directory = Path(directory)
        with self._lock:
            if directory not in self._dirs:
                self._dirs.append(directory)
            if source is not None:
                self._sources[directory] = source
        self._wake.set()

    @property
    def dirs(self) -> list:
        with self._lock:
            return list(self._dirs)

    # -- one pass --------------------------------------------------------

    def _fail(self, now: float) -> None:
        self.backoff = BACKOFF_MIN_S if self.backoff <= 0 else min(self.backoff * 2,
                                                                    BACKOFF_MAX_S)
        self.next_at = now + self.backoff

    def _validate(self, now: float) -> bool:
        """The cached start-up key check. False = not (yet) usable."""
        if self.validated is not None:
            return True
        try:
            resp = api.request("GET", f"{self.base_url}/v1/apps/keys/validate",
                               bearer=self.key, opener=self._opener)
        except api.ApiNetworkError:
            self._fail(now)
            return False
        if resp.status == 200 and isinstance(resp.body, dict):
            self.validated = resp.body
            return True
        if resp.status == 401:
            self.disabled = True
            self._mark_rejected()
            log_key_rejected()
            return False
        self._fail(now)
        return False

    def _mark_rejected(self) -> None:
        """``upload: error invalid_key`` in every registered app directory,
        so ``tryaii monitor status`` shows a key rejected at start-up too
        (the warning line is silent in apps that configure no logging)."""
        from tryaii.monitor import store

        for directory in self.dirs:
            try:
                cursor = store.read_upload_cursor(directory) or store.empty_upload_cursor()
                cursor["last_error"] = "invalid_key"
                store.write_upload_cursor(directory, cursor)
            except OSError:
                logger.debug("monitor: could not record the rejected key", exc_info=True)

    def run_once(self) -> Optional[str]:
        """One pass: validate (once), then send at most ONE batch. Returns
        the outcome (``sent``, ``skipped``, ``idle``, ``wait``, ``disabled``,
        ``backoff``, ``rejected``) or None on an internal error. Never
        raises."""
        with self._pass_lock:
            try:
                return self._run_once()
            except Exception:  # noqa: BLE001 - fail-open
                logger.debug("monitor upload pass failed", exc_info=True)
                return None

    def _run_once(self) -> str:
        if self.disabled:
            return "disabled"
        now = self._clock()
        if now < self.next_at:
            return "wait"
        if not self._validate(now):
            return "disabled" if self.disabled else "backoff"
        for directory in self.dirs:
            outcome = self._upload_dir(directory, now)
            if outcome != "idle":
                return outcome
        return "idle"

    def _paused(self, directory: Path, cursor: dict) -> bool:
        """True (and ``last_error`` set) while the local setting or the
        account switch in remote.json says off."""
        enabled, _reason = privacy.upload_decision({}, directory.parent)
        remote = store.read_remote(directory)
        reason = None
        if not enabled:
            reason = "upload_off"
        elif isinstance(remote, dict) and remote.get("upload") is False:
            reason = "upload_disabled"
        if reason is None:
            return False
        if cursor.get("last_error") != reason:
            cursor["last_error"] = reason
            store.write_upload_cursor(directory, cursor)
        return True

    def _first_upload_notice(self, root: Path) -> None:
        """The Data Notice, once per machine and version, to a terminal only."""
        try:
            if privacy.notice_shown(root) or not privacy.can_show_notice(sys.stderr):
                return
            sys.stderr.write(privacy.NOTICE_TEXT)
            sys.stderr.flush()
            privacy.mark_notice_shown(root, self._now_ts())
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: notice failed", exc_info=True)

    def _upload_dir(self, directory: Path, now: float) -> str:
        lock = _DirLock(directory)
        if not directory.is_dir() or not lock.acquire():
            return "idle"
        try:
            cursor = store.read_upload_cursor(directory) or store.empty_upload_cursor()
            if self._paused(directory, cursor):
                return "idle"
            batch_id = str(uuid.uuid4())
            with self._lock:
                source = self._sources.get(directory)
            head = envelope_head(batch_id, self._version, source,
                                 batch_integrations())
            batch = next_batch(directory, cursor, head_bytes=len(head),
                               hash_key=privacy.hash_key(directory.parent))
            if batch is None:
                return "idle"
            if batch.forbidden:
                cursor["last_error"] = f"forbidden_field: {batch.forbidden}"
            if not batch.lines:
                cursor["files"][batch.file] = batch.end
                store.write_upload_cursor(directory, cursor)
                return "skipped"
            body = head + b",".join(batch.lines) + b"]}"
            self._first_upload_notice(directory.parent)
            try:
                resp = api.request("POST", f"{self.base_url}/v1/monitor/events",
                                   bearer=self.key, opener=self._opener, raw_body=body)
            except api.ApiNetworkError:
                cursor["last_error"] = "network"
                store.write_upload_cursor(directory, cursor)
                self._fail(now)
                return "backoff"
            return self._handle(directory, cursor, batch, resp, now)
        finally:
            lock.release()

    def _handle(self, directory: Path, cursor: dict, batch: Batch,
                resp: api.ApiResponse, now: float) -> str:
        status = resp.status
        if 200 <= status < 300:
            cursor["files"][batch.file] = batch.end
            cursor["last_ok"] = self._now_ts()
            if not batch.forbidden:
                cursor["last_error"] = None
            store.write_upload_cursor(directory, cursor)
            self.sent_events += len(batch.lines)
            self.backoff = 0.0
            self.next_at = now + INTERVAL_S
            return "sent"
        if status in (400, 413):
            # Shape problem: retrying cannot help -- skip the batch.
            cursor["files"][batch.file] = batch.end
            error = resp.error or ("bad_batch" if status == 400 else "too_large")
            cursor["last_error"] = f"{error}: {resp.detail}" if resp.detail else error
            store.write_upload_cursor(directory, cursor)
            self.next_at = now + INTERVAL_S
            return "skipped"
        if status == 403 and resp.error == "upload_disabled":
            # The account switch is off: retry rarely; remote.json says when it is on.
            cursor["last_error"] = "upload_disabled"
            store.write_upload_cursor(directory, cursor)
            self.next_at = now + BACKOFF_MAX_S
            return "backoff"
        if status == 401:
            cursor["last_error"] = "invalid_key"
            store.write_upload_cursor(directory, cursor)
            self.disabled = True
            log_key_rejected()
            return "rejected"
        if status == 429:
            cursor["last_error"] = "rate_limited"
            store.write_upload_cursor(directory, cursor)
            self._fail(now)
            retry = resp.body.get("retry_after") if isinstance(resp.body, dict) else None
            if isinstance(retry, (int, float)) and not isinstance(retry, bool) and retry > 0:
                self.next_at = max(self.next_at, now + min(float(retry), BACKOFF_MAX_S))
            return "backoff"
        cursor["last_error"] = f"http {status}"
        store.write_upload_cursor(directory, cursor)
        self._fail(now)
        return "backoff"

    # -- thread ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None or self.disabled:
            return
        self._thread = threading.Thread(target=self._loop, name="tryaii-monitor-upload",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set() and not self.disabled:
            self._idle.clear()
            outcome = self.run_once()
            self._idle.set()
            if outcome in ("disabled", "rejected"):
                return
            if outcome in ("sent", "skipped") and self._clock() >= self.next_at:
                continue
            delay = max(self.next_at - self._clock(), 0.0) or INTERVAL_S
            self._wake.wait(min(delay, INTERVAL_S) if outcome != "backoff" else delay)
            self._wake.clear()

    def drain(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """At exit: one last pass within ``timeout`` seconds (best effort)."""
        if self._thread is None or self.disabled:
            return
        worker = threading.Thread(target=self.run_once, name="tryaii-monitor-upload-drain",
                                  daemon=True)
        worker.start()
        worker.join(timeout)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
