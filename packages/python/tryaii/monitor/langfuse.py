"""Langfuse export of monitor events (SPEC.md "Langfuse export", Jira TS-246).

A second reader of the local event store, next to the uploader, with its own
cursor (``langfuse.json``). It turns each event into one OTLP span, posts
the spans as OTLP/HTTP JSON to ``<host>/api/public/otel/v1/traces`` and then
TryAii's recommendation as three Langfuse scores to
``<host>/api/public/scores``, with Basic auth from the customer's own
Langfuse keys. The scores are sent from the SDK only, so TryAii never holds
a Langfuse secret key.

Off unless ``TRYAII_LANGFUSE`` is ``on`` (``1``, ``true``) and both
``TRYAII_API_KEY`` (an app key) and ``LANGFUSE_PUBLIC_KEY`` /
``LANGFUSE_SECRET_KEY`` are set. Metadata only: no prompt or response text
(the store holds none) and never the prompt hash or the vectors. It runs on
its own daemon thread, never in the LLM call path; every failure is
swallowed and retried with backoff. Span and score ids are derived from the
event id, so a retried batch never duplicates anything in Langfuse.

The pure part (``settings``, ``status_line``, ``build_span``,
``build_traces_body``, ``build_scores``) is frozen by the ``langfuse``
fixture suite and byte-identical in the Node SDK (``langfuse.ts``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from tryaii.monitor import api, store
from tryaii.monitor.callsite import is_absolute_file
from tryaii.monitor.config import ENV_LANGFUSE, LANGFUSE_ON_VALUES, langfuse_enabled
from tryaii.monitor.events import EVENT_SCHEMA, format_ts, parse_ts, round_cost

logger = logging.getLogger("tryaii.monitor")

ENV_SWITCH = ENV_LANGFUSE  # the switch lives in config.py (one helper)
SWITCH_ON = LANGFUSE_ON_VALUES
ENV_BASE_URL = "LANGFUSE_BASE_URL"
ENV_HOST = "LANGFUSE_HOST"
ENV_PUBLIC_KEY = "LANGFUSE_PUBLIC_KEY"
ENV_SECRET_KEY = "LANGFUSE_SECRET_KEY"
DEFAULT_HOST = "https://cloud.langfuse.com"
TRACES_PATH = "/api/public/otel/v1/traces"
SCORES_PATH = "/api/public/scores"

CURSOR_SCHEMA = "tryaii.monitor.langfuse-cursor/1"
CURSOR_FILE = "langfuse.json"
LOCK_FILE = "langfuse.lock"
DASHBOARD_URL = "https://tryaii.com/dashboard/monitor"
SERVICE_NAME = "tryaii-monitor"
SCOPE_NAME = "tryaii.monitor"

SCORE_RECOMMENDED = "tryaii.recommended_model"
SCORE_SAVINGS = "tryaii.est_savings_usd"
SCORE_ROUTE_STATUS = "tryaii.route_status"

MAX_EVENTS = 100
BATCH_INTERVAL_S = 1.0
POLL_S = 2.0
SCORE_CONCURRENCY = 8
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
DRAIN_TIMEOUT_S = 2.0
LOCK_STALE_S = 60.0
TIMEOUT_S = 10.0

REASON_SWITCH = "TRYAII_LANGFUSE is not on"
REASON_NO_APP_KEY = "TRYAII_API_KEY is not set"
REASON_BAD_APP_KEY = "TRYAII_API_KEY is not an app key"
REASON_NO_LANGFUSE_KEYS = "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are needed"

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_URI_SAFE = "!*'()"  # with quote()'s own "_.-~": encodeURIComponent's set
_warned = threading.Event()


# --- settings ---------------------------------------------------------------

def _value(env, name: str) -> str:
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ""


def switch_on(env=None) -> bool:
    """``TRYAII_LANGFUSE`` is ``on``, ``1`` or ``true`` (trimmed, any case):
    :func:`tryaii.monitor.config.langfuse_enabled`."""
    return langfuse_enabled(env)


def host(env=None) -> str:
    """``LANGFUSE_BASE_URL``, else ``LANGFUSE_HOST``, else Langfuse Cloud
    (EU); trailing ``/`` dropped, empty values skipped."""
    env = os.environ if env is None else env
    for name in (ENV_BASE_URL, ENV_HOST):
        value = _value(env, name).rstrip("/")
        if value:
            return value
    return DEFAULT_HOST


def settings(env=None) -> dict:
    """``{"enabled", "host", "reason"}`` -- ``reason`` says why the export
    is off (None when it is on). Never contains a key."""
    env = os.environ if env is None else env
    target = host(env)
    reason = None
    if not switch_on(env):
        reason = REASON_SWITCH
    else:
        key = _value(env, api.ENV_API_KEY)
        if not key:
            reason = REASON_NO_APP_KEY
        elif not api.is_app_key(key):
            reason = REASON_BAD_APP_KEY
        elif not _value(env, ENV_PUBLIC_KEY) or not _value(env, ENV_SECRET_KEY):
            reason = REASON_NO_LANGFUSE_KEYS
    return {"enabled": reason is None, "host": target, "reason": reason}


def status_line(env=None) -> Optional[str]:
    """The ``tryaii monitor status`` line: ``langfuse: on (<host>)``,
    ``langfuse: off (<reason>)`` while the switch is on but a key is
    missing, ``langfuse: off`` for any other switch value, None when
    ``TRYAII_LANGFUSE`` is unset or empty."""
    env = os.environ if env is None else env
    if not _value(env, ENV_SWITCH):
        return None
    s = settings(env)
    if s["enabled"]:
        return f"langfuse: on ({s['host']})"
    if s["reason"] == REASON_SWITCH:
        return "langfuse: off"
    return f"langfuse: off ({s['reason']})"


# --- the mapping (pure) -----------------------------------------------------

def derived_ids(event_id: str) -> tuple:
    """``(trace_id, span_id)``: the first 32 and the next 16 hex characters
    of sha256(event_id)."""
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return digest[:32], digest[32:48]


ORIGIN_TRYAII = "tryaii"


def valid_otel(otel) -> Optional[dict]:
    """The event's ``otel`` when it names a valid span (lowercase hex, not
    all zeros), else None; ``origin`` kept only when it is ``"tryaii"``."""
    if not isinstance(otel, dict):
        return None
    trace_id, span_id = otel.get("trace_id"), otel.get("span_id")
    if not (isinstance(trace_id, str) and _HEX32.match(trace_id) and trace_id.strip("0")):
        return None
    if not (isinstance(span_id, str) and _HEX16.match(span_id) and span_id.strip("0")):
        return None
    out = {"trace_id": trace_id, "span_id": span_id}
    if otel.get("origin") == ORIGIN_TRYAII:
        out["origin"] = ORIGIN_TRYAII
    return out


def span_ids(event: dict) -> dict:
    """``{"trace_id", "span_id", "parent_span_id"}`` of the event's span.
    With ``otel.origin == "tryaii"`` the wrapper made the TryAii span the
    active parent at call time: the span takes exactly those ids, no parent."""
    trace_id, span_id = derived_ids(str(event.get("event_id")))
    otel = valid_otel(event.get("otel"))
    if otel is None:
        return {"trace_id": trace_id, "span_id": span_id, "parent_span_id": None}
    if otel.get("origin") == ORIGIN_TRYAII:
        return {"trace_id": otel["trace_id"], "span_id": otel["span_id"], "parent_span_id": None}
    return {"trace_id": otel["trace_id"], "span_id": span_id, "parent_span_id": otel["span_id"]}


def tryaii_url(app_id, process) -> str:
    """The sub-process page on the TryAii dashboard; the monitor overview
    when the app id is unknown (no remote config fetched yet)."""
    if not isinstance(app_id, str) or not app_id:
        return DASHBOARD_URL
    try:
        app = urllib.parse.quote(app_id, safe=_URI_SAFE)
        if not isinstance(process, str) or not process:
            return f"{DASHBOARD_URL}/{app}"
        return f"{DASHBOARD_URL}/{app}/features/{urllib.parse.quote(process, safe=_URI_SAFE)}"
    except (UnicodeError, ValueError):
        return DASHBOARD_URL


def _json(obj) -> str:
    """Minified JSON with Node's number format (SPEC.md §1.2)."""
    return store.dumps_line(obj).rstrip("\n")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value) -> str:
    """A metadata value as a string: strings as is, the rest as JSON."""
    return value if isinstance(value, str) else _json(value)


def usage_details(usage) -> Optional[dict]:
    """Langfuse's usage keys (as its own OpenAI integration sends them):
    ``input`` = prompt - cached, ``output`` = completion - reasoning,
    ``total``, ``input_cached_tokens``, ``output_reasoning_tokens``; only the
    numbers the event has. None when there are none."""
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    total = usage.get("total_tokens")
    cached, reasoning = usage.get("cached_tokens"), usage.get("reasoning_tokens")
    out: dict = {}
    if _is_int(prompt):
        out["input"] = max(prompt - cached, 0) if _is_int(cached) else prompt
    if _is_int(completion):
        out["output"] = max(completion - reasoning, 0) if _is_int(reasoning) else completion
    if _is_int(total):
        out["total"] = total
    if _is_int(cached):
        out["input_cached_tokens"] = cached
    if _is_int(reasoning):
        out["output_reasoning_tokens"] = reasoning
    return out or None


def _call_site(site) -> Optional[str]:
    if not isinstance(site, dict) or not isinstance(site.get("file"), str) or not site["file"]:
        return None
    file = site["file"]
    if is_absolute_file(file):
        file = re.split(r"[\\/]", file)[-1]  # TS-226: never a machine path
    line = site.get("line")
    return f"{file}:{line}" if _is_int(line) else file


def _unix_nano(ts) -> int:
    dt = parse_ts(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    whole = int(dt.replace(microsecond=0).timestamp())
    return whole * 1_000_000_000 + (dt.microsecond // 1000) * 1_000_000


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def build_span(event: dict, app_id: Optional[str] = None) -> dict:
    """One OTLP span (JSON encoding, hex ids) for one event."""
    ids = span_ids(event)
    own_trace = ids["parent_span_id"] is None
    # A generation only when nothing else records the call: with any otel the
    # integration's own generation carries model, usage and cost.
    generation = valid_otel(event.get("otel")) is None
    process = event.get("process") if isinstance(event.get("process"), str) else "default"
    name = f"tryaii {process}"
    url = tryaii_url(app_id, process)
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    classification = event.get("classification")
    if not isinstance(classification, dict):
        classification = {}
    error = event.get("error") if isinstance(event.get("error"), dict) else None

    attrs = [_attr("langfuse.observation.type", "generation" if generation else "span")]
    if own_trace:
        attrs.append(_attr("langfuse.trace.name", name))
        attrs.append(_attr("langfuse.trace.metadata.tryaii_url", url))
    if generation:
        if isinstance(event.get("model_used"), str) and event["model_used"]:
            attrs.append(_attr("langfuse.observation.model.name", event["model_used"]))
        details = usage_details(usage)
        if details is not None:
            attrs.append(_attr("langfuse.observation.usage_details", _json(details)))
        if _is_num(event.get("est_cost_usd")):
            attrs.append(_attr("langfuse.observation.cost_details",
                               _json({"total": event["est_cost_usd"]})))
    if error is not None:
        status = error.get("status")
        message = str(error.get("class") or "error")
        if _is_int(status):
            message = f"{message} (HTTP {status})"
        attrs.append(_attr("langfuse.observation.level", "ERROR"))
        attrs.append(_attr("langfuse.observation.status_message", message))
    metadata = [
        ("tryaii_url", url),
        ("event_id", event.get("event_id")),
        ("app", event.get("app")),
        ("process", event.get("process")),
        ("mode", event.get("mode")),
        ("provider", event.get("provider")),
        ("model_requested", event.get("model_requested")),
        ("model_resolved", event.get("model_resolved")),
        ("model_used", event.get("model_used")),
        ("winner", event.get("winner")),
        ("winner_reachable", event.get("winner_reachable")),
        ("route_status", event.get("route_status")),
        ("category", classification.get("category")),
        ("subcategory", classification.get("subcategory")),
        ("prompt_tokens", usage.get("prompt_tokens")),
        ("completion_tokens", usage.get("completion_tokens")),
        ("total_tokens", usage.get("total_tokens")),
        ("cached_tokens", usage.get("cached_tokens")),
        ("reasoning_tokens", usage.get("reasoning_tokens")),
        ("est_cost_usd", event.get("est_cost_usd")),
        ("est_cost_winner_usd", event.get("est_cost_winner_usd")),
        ("est_cost_anchor_usd", event.get("est_cost_anchor_usd")),
        ("latency_ms", event.get("latency_ms")),
        ("stream", event.get("stream")),
        ("call_site", _call_site(event.get("call_site"))),
        ("error_class", error.get("class") if error else None),
        ("error_status", error.get("status") if error else None),
    ]
    for key, value in metadata:
        if value is not None and not isinstance(value, (dict, list)):
            attrs.append(_attr(f"langfuse.observation.metadata.{key}", _text(value)))

    start = _unix_nano(event.get("ts"))
    latency = event.get("latency_ms")
    end = start + (max(int(latency), 0) * 1_000_000 if _is_num(latency) else 0)
    span: dict = {"traceId": ids["trace_id"], "spanId": ids["span_id"]}
    if not own_trace:
        span["parentSpanId"] = ids["parent_span_id"]
    span.update({
        "name": name,
        "kind": 3,  # SPAN_KIND_CLIENT
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attrs,
        "status": {"code": 2} if error is not None else {"code": 1},
    })
    return span


def build_traces_body(spans: list, version: str) -> dict:
    """The OTLP/HTTP JSON request for a batch of spans."""
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", SERVICE_NAME)]},
        "scopeSpans": [{"scope": {"name": SCOPE_NAME, "version": version}, "spans": spans}],
    }]}


def build_scores(event: dict) -> list:
    """The event's Langfuse scores (``POST /api/public/scores`` bodies), in
    the order recommended_model, est_savings_usd, route_status; each only
    when the event has its value. Score id = ``<event_id>-<name without
    "tryaii.">``."""
    ids = span_ids(event)
    event_id = str(event.get("event_id"))

    def score(name: str, value, data_type: str) -> dict:
        return {"id": f"{event_id}-{name[len('tryaii.'):]}", "traceId": ids["trace_id"],
                "observationId": ids["span_id"], "name": name, "value": value,
                "dataType": data_type}

    out = []
    winner = event.get("winner")
    if isinstance(winner, str) and winner:
        out.append(score(SCORE_RECOMMENDED, winner, "CATEGORICAL"))
    cost, at_winner = event.get("est_cost_usd"), event.get("est_cost_winner_usd")
    if _is_num(cost) and _is_num(at_winner):
        saving = round_cost(cost - at_winner)
        out.append(score(SCORE_SAVINGS, int(saving) if saving.is_integer() else saving, "NUMERIC"))
    status = event.get("route_status")
    if isinstance(status, str) and status:
        out.append(score(SCORE_ROUTE_STATUS, status, "CATEGORICAL"))
    return out


def exportable(obj) -> bool:
    """A store line the exporter sends: a ``tryaii.monitor.event/1`` object
    with a uuid ``event_id`` and a parseable ``ts``, without any forbidden
    field (the uploader's hard rule, SPEC.md §3.4)."""
    from tryaii.monitor.upload import _valid_event_id, find_forbidden

    if not isinstance(obj, dict) or obj.get("schema") != EVENT_SCHEMA:
        return False
    if not _valid_event_id(obj.get("event_id")) or find_forbidden(obj):
        return False
    try:
        parse_ts(obj.get("ts"))
    except ValueError:
        return False
    return True


# --- cursor -----------------------------------------------------------------

def read_cursor(directory) -> Optional[dict]:
    path = Path(directory) / CURSOR_FILE
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("schema") != CURSOR_SCHEMA:
        return None
    files = obj.get("files") if isinstance(obj.get("files"), dict) else {}
    return {
        "schema": CURSOR_SCHEMA,
        "host": obj.get("host") if isinstance(obj.get("host"), str) else None,
        "files": {k: v for k, v in sorted(files.items()) if _is_int(v) and v >= 0},
        "last_ok": obj.get("last_ok") if isinstance(obj.get("last_ok"), str) else None,
        "last_error": obj.get("last_error") if isinstance(obj.get("last_error"), str) else None,
    }


def write_cursor(directory, cursor: dict) -> None:
    """Atomic, indent 2; key order schema, host, files (sorted), last_ok, last_error."""
    from tryaii.auth.store import write_atomic

    doc = {"schema": CURSOR_SCHEMA, "host": cursor.get("host"),
           "files": dict(sorted((cursor.get("files") or {}).items())),
           "last_ok": cursor.get("last_ok"), "last_error": cursor.get("last_error")}
    write_atomic(Path(directory) / CURSOR_FILE,
                 (store.js_numbers(json.dumps(doc, indent=2, ensure_ascii=False)) + "\n")
                 .encode("utf-8"))


def start_cursor(directory, target_host: str) -> dict:
    """A new cursor at the current end of every events file: the export
    covers the calls made from now on (no backfill)."""
    files = {}
    for path in store.event_files(directory):
        try:
            files[path.name] = path.stat().st_size
        except OSError:
            continue
    return {"schema": CURSOR_SCHEMA, "host": target_host, "files": files,
            "last_ok": None, "last_error": None}


# --- HTTP -------------------------------------------------------------------

class _NetworkError(Exception):
    pass


def _post(url: str, body: bytes, auth: str, opener=None) -> int:
    """POST JSON with Basic auth; the HTTP status (redirects never followed,
    so the credentials never reach another host). Raises _NetworkError."""
    from tryaii.monitor.api import _OPENER, _version

    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": f"tryaii/{_version()}", "Authorization": f"Basic {auth}"})
    open_fn = opener or _OPENER.open
    try:
        with open_fn(req, timeout=TIMEOUT_S) as response:
            status = getattr(response, "status", None) or response.getcode()
            response.read()
            return int(status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception as exc:  # noqa: BLE001 - one collapsed transport failure
        raise _NetworkError(type(exc).__name__) from exc


# --- exporter ---------------------------------------------------------------

def _utcnow_ts() -> str:
    return format_ts(datetime.now(timezone.utc))


def warn_off(reason: str) -> None:
    if not _warned.is_set():
        _warned.set()
        logger.warning("tryaii monitor: the Langfuse export is off (%s)", reason)


class _Lock:
    def __init__(self, directory):
        self.path = Path(directory) / LOCK_FILE

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime < LOCK_STALE_S:
                        return False
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


class Exporter:
    """Exports the registered app directories' events to Langfuse.

    Test seam: ``opener`` (urlopen signature), ``clock`` and ``now_ts`` are
    injectable; :meth:`run_once` performs one pass without the thread.
    """

    def __init__(self, target_host: str, public_key: str, secret_key: str, *,
                 opener=None, clock: Callable[[], float] = time.monotonic,
                 now_ts: Callable[[], str] = _utcnow_ts, version: Optional[str] = None):
        self.host = target_host.rstrip("/")
        self._auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
        self._opener = opener
        self._clock = clock
        self._now_ts = now_ts
        if version is None:
            from tryaii import __version__ as version
        self._version = version
        self._dirs: list = []
        self._lock = threading.Lock()
        self._pass_lock = threading.Lock()
        self.disabled = False
        self.backoff = 0.0
        self.next_at = 0.0
        self.sent_spans = 0
        self.sent_scores = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    @classmethod
    def from_env(cls, env=None) -> Optional[Exporter]:
        """An exporter when the settings say on; None (with one warning
        when the switch is on but a key is missing) otherwise."""
        env = os.environ if env is None else env
        s = settings(env)
        if not s["enabled"]:
            if s["reason"] != REASON_SWITCH:
                warn_off(s["reason"])
            return None
        return cls(s["host"], _value(env, ENV_PUBLIC_KEY), _value(env, ENV_SECRET_KEY))

    # -- registration --------------------------------------------------------

    def register(self, directory) -> None:
        """Track ``directory``; a missing cursor (or one for another host)
        starts at the current end of the store. Never raises."""
        directory = Path(directory)
        with self._lock:
            if directory in self._dirs:
                return
            self._dirs.append(directory)
        try:
            cursor = read_cursor(directory)
            if cursor is None or cursor.get("host") != self.host:
                directory.mkdir(parents=True, exist_ok=True)
                write_cursor(directory, start_cursor(directory, self.host))
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: langfuse cursor could not be created", exc_info=True)
        self._wake.set()

    @property
    def dirs(self) -> list:
        with self._lock:
            return list(self._dirs)

    # -- one pass ------------------------------------------------------------

    def _fail(self, now: float) -> None:
        self.backoff = BACKOFF_MIN_S if self.backoff <= 0 else min(self.backoff * 2, BACKOFF_MAX_S)
        self.next_at = now + self.backoff

    def run_once(self) -> Optional[str]:
        """One pass: at most one batch per app directory. Returns ``sent``,
        ``idle``, ``wait``, ``backoff``, ``disabled`` or None (internal
        error). Never raises."""
        with self._pass_lock:
            try:
                return self._run_once()
            except Exception:  # noqa: BLE001 - fail-open
                logger.debug("monitor: langfuse pass failed", exc_info=True)
                return None

    def _run_once(self) -> str:
        if self.disabled:
            return "disabled"
        now = self._clock()
        if now < self.next_at:
            return "wait"
        result = "idle"
        for directory in self.dirs:
            outcome = self._export_dir(directory, now)
            if outcome in ("backoff", "disabled"):
                return outcome
            if outcome == "sent":
                result = "sent"
        if result == "sent":
            self.next_at = now + BATCH_INTERVAL_S
        return result

    def _batch(self, directory: Path, cursor: dict):
        """``(file name, end offset, events)`` of the unread whole lines of
        the oldest file that has any (at most MAX_EVENTS events), or None."""
        files = cursor.setdefault("files", {})
        for path in store.event_files(directory):
            offset = files.get(path.name, 0)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < offset:
                offset = 0  # rewritten file
            if size == offset:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    data = fh.read(size - offset)
            except OSError:
                continue
            events, pos, end = [], 0, offset
            while pos < len(data) and len(events) < MAX_EVENTS:
                nl = data.find(b"\n", pos)
                if nl < 0:
                    break  # a partial last line waits for the writer
                raw = data[pos:nl].rstrip(b"\r")
                pos = nl + 1
                end = offset + pos
                try:
                    obj = json.loads(raw.decode("utf-8")) if raw.strip() else None
                except (UnicodeDecodeError, ValueError):
                    obj = None
                if exportable(obj):
                    events.append(obj)
            if end > offset:
                return path.name, end, events
        return None

    def _send(self, path: str, body: bytes) -> int:
        return _post(self.host + path, body, self._auth, self._opener)

    def _export_dir(self, directory: Path, now: float) -> str:
        lock = _Lock(directory)
        if not directory.is_dir() or not lock.acquire():
            return "idle"
        try:
            cursor = read_cursor(directory)
            if cursor is None or cursor.get("host") != self.host:
                cursor = start_cursor(directory, self.host)
                write_cursor(directory, cursor)
                return "idle"
            batch = self._batch(directory, cursor)
            if batch is None:
                return "idle"
            name, end, events = batch
            if events:
                outcome = self._post_batch(directory, cursor, events, now)
                if outcome is not None:
                    return outcome
            cursor["files"][name] = end
            if events:
                cursor["last_ok"] = self._now_ts()
                cursor["last_error"] = cursor.pop("_note", None)
            write_cursor(directory, cursor)
            self.backoff = 0.0
            return "sent" if events else "idle"
        finally:
            lock.release()

    def _post_batch(self, directory: Path, cursor: dict, events: list,
                    now: float) -> Optional[str]:
        """Spans first, then the scores. None = the batch is done (advance
        the cursor); else the outcome of a failure that must be retried."""
        app_id = None
        remote = store.read_remote(directory)
        if isinstance(remote, dict) and isinstance(remote.get("app_id"), str):
            app_id = remote["app_id"]
        spans = [build_span(e, app_id) for e in events]
        body = _json(build_traces_body(spans, self._version)).encode("utf-8")
        failure = self._outcome(directory, cursor, self._try(TRACES_PATH, body), now, "traces")
        if failure == "skip":
            return None
        if failure is not None:
            return failure
        self.sent_spans += len(spans)
        bodies = [_json(score).encode("utf-8") for event in events for score in build_scores(event)]
        statuses = self._post_all(SCORES_PATH, bodies)
        for status in statuses:
            failure = self._outcome(directory, cursor, status, now, "scores")
            if failure == "skip":
                cursor["_note"] = f"scores: http {status}"
                continue
            if failure is not None:
                return failure  # retried: span and score ids are stable
            self.sent_scores += 1
        return None

    def _post_all(self, path: str, bodies: list) -> list:
        """POST every body, SCORE_CONCURRENCY at a time (plain threads: they
        also start inside the exit hook); the statuses in body order."""
        statuses: list = [None] * len(bodies)

        def send(i: int) -> None:
            statuses[i] = self._try(path, bodies[i])

        for start in range(0, len(bodies), SCORE_CONCURRENCY):
            threads = [threading.Thread(target=send, args=(i,), daemon=True)
                       for i in range(start, min(start + SCORE_CONCURRENCY, len(bodies)))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        return statuses

    def _try(self, path: str, body: bytes) -> Optional[int]:
        try:
            return self._send(path, body)
        except _NetworkError:
            return None

    def _outcome(self, directory: Path, cursor: dict, status: Optional[int], now: float,
                 what: str) -> Optional[str]:
        """None for 2xx; ``skip`` for a 4xx retrying cannot fix; else the
        pass outcome (cursor kept, ``last_error`` set)."""
        if status is not None and 200 <= status < 300:
            return None
        if status in (401, 403):
            cursor["last_error"] = (f"{what}: http {status} "
                                    "(check LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)")
            write_cursor(directory, cursor)
            self.disabled = True
            logger.warning("tryaii monitor: Langfuse rejected the keys (HTTP %s); "
                           "the Langfuse export is off for this process", status)
            return "disabled"
        if status is not None and 400 <= status < 500 and status not in (408, 429):
            if what == "traces":
                cursor["_note"] = f"traces: http {status}"
            return "skip"
        cursor["last_error"] = f"{what}: " + ("network" if status is None else f"http {status}")
        write_cursor(directory, cursor)
        self._fail(now)
        return "backoff"

    # -- thread --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None or self.disabled:
            return
        self._thread = threading.Thread(target=self._loop, name="tryaii-monitor-langfuse",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set() and not self.disabled:
            outcome = self.run_once()
            if outcome == "disabled":
                return
            if outcome == "sent":
                delay = max(self.next_at - self._clock(), 0.0)
            elif outcome in ("backoff", "wait"):
                delay = max(self.next_at - self._clock(), 0.0) or POLL_S
            else:
                delay = POLL_S
            if delay > 0:
                self._wake.wait(delay)
            self._wake.clear()

    def drain(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """At exit: passes until nothing is left, within ``timeout`` seconds
        (best effort; a batch cut short is resent by the next process)."""
        if self._thread is None or self.disabled:
            return

        def passes() -> None:
            while True:
                self.next_at = 0.0
                if self.run_once() != "sent":
                    return

        worker = threading.Thread(target=passes, name="tryaii-monitor-langfuse-drain", daemon=True)
        worker.start()
        worker.join(timeout)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
