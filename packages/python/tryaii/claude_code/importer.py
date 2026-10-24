"""Claude Code calls -> monitor events of the app ``claude-code`` (SPEC.md §10).

Each deduplicated call becomes one ``tryaii.monitor.event/1`` line in the
local monitor store, ``<data_dir>/monitor/claude-code/``; the monitor's own
uploader (SPEC.md §3.4) sends them with the app key in ``TRYAII_API_KEY``.

- ``event_id`` = uuid5(``NAMESPACE_URL``, ``"tryaii:claude-code:" + requestId``),
  the rule the server applies to a call it receives by live telemetry
  (SPEC.md §10.8), so the same call imported here and received live is one
  event; a call without ``requestId`` falls back to uuid5(``EVENT_NAMESPACE``,
  the call's message id). Either way the same call always gets the same id, so
  a re-run (or a resumed session) never adds a duplicate locally, and the
  server's ``ON CONFLICT DO NOTHING`` on ``event_id`` keeps a re-upload
  harmless;
- ``usage``: ``prompt_tokens`` = Anthropic ``input_tokens`` unchanged (it
  excludes cached and written tokens, SPEC.md §3.1b rule 2),
  ``completion_tokens`` = ``output_tokens``, ``total_tokens`` = every token of
  the call (input + cache write + cache read + output), and the cache columns
  via ``normalize_cache_usage("anthropic", ...)``;
- no prompt text is ever part of an event: ``prompt_hash`` is null and
  ``vector`` false. Without an analysis ``classification`` is null and
  ``route_status`` ``skipped``; a call that ``analyze`` classified on this
  machine (SPEC.md §10.7) carries its task profile in ``classification``
  (category, subcategory, confidence), the Anthropic recommendation in
  ``winner`` (``winner_reachable`` true, ``est_cost_winner_usd`` its price),
  ``route_status`` ``ok`` and the catalog's ``taxonomy_version``;
- ``call_site.file``: the project label (``reader.project_value``), null for
  ``unknown`` or an id-like folder name (the dashboard shows "Other");
- ``cache_cold`` (API.md v1.6): true when a main-thread call was a cold cache
  restart (``sessions``), false for any other main-thread call, null for a
  subagent call. The session itself is never part of an event.

The local state of the command is the app directory and ``connect.json``
beside it (``<data_dir>/claude-code/``); ``disconnect`` removes both and never
touches Claude Code's own files.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Optional

from tryaii.auth.store import write_atomic
from tryaii.claude_code.pricing import Pricer
from tryaii.claude_code.reader import Call, project_value
from tryaii.monitor import store
from tryaii.monitor.events import build_event, format_ts, normalize_cache_usage

APP = "claude-code"
PROVIDER = "anthropic"
MODE = "off"
ROUTE_STATUS = "skipped"
#: ``route_status`` of a call ``analyze`` classified (SPEC.md §10.7).
ROUTE_STATUS_ANALYZED = "ok"
#: Namespace of the fallback event ids, for a call without ``requestId``
#: (never change: ids would change).
EVENT_NAMESPACE = uuid.UUID("5b0f6a55-3c1e-4b8e-9d6c-2a7f1c0de001")
#: The shared rule (SPEC.md §10.8; the server derives the same id for a call it
#: receives by live telemetry): uuid5(NAMESPACE_URL, prefix + request id).
REQUEST_ID_NAMESPACE = uuid.NAMESPACE_URL
REQUEST_ID_PREFIX = "tryaii:claude-code:"
STATE_DIR = "claude-code"
STATE_FILE = "connect.json"
STATE_SCHEMA = "tryaii.claude-code.connect/1"
#: The server keeps events for 90 days and skips older ones (API.md §4).
MAX_DAYS = 90


def request_event_id(request_id: str) -> str:
    """The event id of the call with this API request id (SPEC.md §10.8)."""
    return str(uuid.uuid5(REQUEST_ID_NAMESPACE, REQUEST_ID_PREFIX + request_id))


def legacy_event_id(call: Call) -> str:
    """uuid5(``EVENT_NAMESPACE``, the call key): the id before the request-id
    rule, and still the id of a call whose lines carry no ``requestId``."""
    return str(uuid.uuid5(EVENT_NAMESPACE, call.key))


def event_id(call: Call) -> str:
    if call.request_id:
        return request_event_id(call.request_id)
    return legacy_event_id(call)


def _raw_usage(call: Call) -> dict:
    """The merged usage in Anthropic's shape, for ``normalize_cache_usage``."""
    raw = {"cache_read_input_tokens": call.usage.get("cache_read_input_tokens"),
           "cache_creation_input_tokens": call.usage.get("cache_creation_input_tokens")}
    if call.has_breakdown:
        raw["cache_creation"] = {
            "ephemeral_5m_input_tokens": call.usage.get("cache_write_5m_tokens"),
            "ephemeral_1h_input_tokens": call.usage.get("cache_write_1h_tokens"),
        }
    return raw


def event_usage(call: Call) -> dict:
    cache = normalize_cache_usage(PROVIDER, _raw_usage(call))
    prompt = call.usage.get("input_tokens")
    completion = call.usage.get("output_tokens")
    total = (call.tokens("input_tokens") + call.cache_write_tokens
             + call.tokens("cache_read_input_tokens") + call.tokens("output_tokens"))
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total,
            **cache}


def build_call_event(call: Call, pricer: Pricer, result: Optional[dict] = None,
                     taxonomy_version: Optional[str] = None) -> dict:
    """The event of one call; ``result`` is the call's stored analysis result
    (``analysis.call_results``), or None."""
    cost = pricer.cost(call)
    price = pricer.price(call.model)
    est = round(cost["total"], 6) if cost is not None else None
    classification, winner, winner_cost, status, taxonomy = None, None, None, ROUTE_STATUS, None
    if isinstance(result, dict) and result.get("category"):
        classification = {"category": result.get("category"),
                          "subcategory": result.get("subcategory"),
                          "confidence": result.get("confidence")}
        winner = result.get("recommended") if isinstance(result.get("recommended"), str) else None
        if winner is not None:
            alt = pricer.cost(call, winner)
            winner_cost = round(alt["total"], 6) if alt is not None and est is not None else None
        status = ROUTE_STATUS_ANALYZED
        taxonomy = taxonomy_version
    return build_event(
        ts=format_ts(call.ts), app=APP, process=call.process,
        call_site={"file": project_value(call.project), "line": None, "function": None},
        provider=PROVIDER, model_requested=call.model,
        model_resolved=price.catalog_id if price else None, model_used=call.model,
        mode=MODE, winner=winner, winner_reachable=winner is not None, route_status=status,
        classification=classification, usage=event_usage(call), est_cost_usd=est,
        est_cost_winner_usd=winner_cost, latency_ms=None, stream=False, error=None,
        prompt_hash=None, vector=False, event_id=event_id(call), taxonomy_version=taxonomy,
        est_cost_anchor_usd=est, cache_ttl_policy=None, cache_cold=call.cache_cold)


# --- local state ----------------------------------------------------------------

def app_directory(data_dir) -> Path:
    return store.app_dir(data_dir, APP)


def state_path(data_dir) -> Path:
    return Path(data_dir) / STATE_DIR / STATE_FILE


def existing_event_ids(data_dir) -> set:
    """Every ``event_id`` already in the app's events files."""
    ids = set()
    for path in store.event_files(app_directory(data_dir)):
        for event in store.read_lines(path):
            if isinstance(event.get("event_id"), str):
                ids.add(event["event_id"])
    return ids


def uploaded_events(data_dir) -> dict:
    """``event_id -> {"ts", "classified"}`` of the app's events the uploader
    has already sent (the byte prefixes of its cursor, SPEC.md §3.4):
    ``classified`` is True when the event went up with a task profile."""
    directory = app_directory(data_dir)
    cursor = store.read_upload_cursor(directory)
    out: dict = {}
    for name, size in ((cursor or {}).get("files") or {}).items():
        if size <= 0:
            continue
        try:
            with open(directory / name, "rb") as fh:
                data = fh.read(size)
        except OSError:
            continue
        for raw in data.split(b"\n"):
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if (isinstance(event, dict) and isinstance(event.get("event_id"), str)
                    and isinstance(event.get("ts"), str)):
                out[event["event_id"]] = {"ts": event["ts"],
                                          "classified": event.get("classification") is not None}
    return out


def read_state(data_dir) -> Optional[dict]:
    try:
        doc = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == STATE_SCHEMA else None


def write_state(data_dir, *, now: str, imported: int) -> Path:
    previous = read_state(data_dir) or {}
    doc = {"schema": STATE_SCHEMA, "app": APP,
           "connected_at": previous.get("connected_at") or now,
           "last_import_at": now,
           "imported_events": int(previous.get("imported_events") or 0) + imported}
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, (json.dumps(doc, indent=2) + "\n").encode("utf-8"))
    return path


def store_events(data_dir, events: list) -> int:
    directory = app_directory(data_dir)
    for event in events:
        store.append_event(directory, event)
    return len(events)


def disconnect(data_dir) -> list:
    """Remove the app directory and the state file; returns what was removed.
    Claude Code's own logs are never touched."""
    removed = []
    directory = app_directory(data_dir)
    if directory.is_dir():
        shutil.rmtree(directory)
        removed.append(directory)
    state_dir = state_path(data_dir).parent
    if state_dir.is_dir():
        shutil.rmtree(state_dir)
        removed.append(state_dir)
    return removed
