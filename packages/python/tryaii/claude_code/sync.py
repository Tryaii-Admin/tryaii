"""`tryaii claude-code sync`: live advice for calls that came by live telemetry
(shared/monitor/SPEC.md §10.9).

Live telemetry (SPEC.md §10.8) sends no project and no task profile: Claude
Code's OTLP events carry neither. ``sync`` reads the local session logs of the
calls made since live telemetry was turned on (at most ``--days``, default 7)
and sends one **annotation** per call to
``POST /v1/monitor/claude-code/annotations``, so the server can complete the
live event it already holds:

- ``event_id`` = ``importer.request_event_id(requestId)``: the id the server
  gave the live event (a call without ``requestId`` cannot be matched and is
  skipped); ``ts`` the log time (the server matches within minutes of it);
- ``project``: the base name of the session's working directory, or null for
  anything path-like; ``process``: ``main`` or the real subagent type (live
  events of a custom subagent arrive as ``custom``);
- with a current ``analyze`` consent only: ``classification`` (category,
  subcategory, confidence), ``winner``, ``est_cost_winner_usd`` and
  ``taxonomy_version`` -- never text.

Calls younger than ``SETTLE_S`` are left for the next run (their live event
may not be stored yet). With a current analysis, the request also carries
one **process advice** document per process (structured fields of
``analysis.json`` ``processes[]``; the advice sentence stays local).

The payload is scanned for forbidden field names before it leaves the
machine (``upload.find_forbidden``). State lives in
``<data_dir>/claude-code/sync.json``: the cursor, the applied event ids, the
unmatched ids (retried for ``UNMATCHED_TTL_S``, then dropped) and the
``generated_at`` of the advice last sent. A lock file and a minimum interval
of ``MIN_INTERVAL_S`` keep concurrent session ends from piling up.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from tryaii.auth.store import write_atomic
from tryaii.claude_code import analysis as analysis_mod
from tryaii.claude_code.importer import STATE_DIR, request_event_id
from tryaii.claude_code.reader import UNKNOWN_PROJECT
from tryaii.monitor.events import format_ts, parse_ts

ROUTE = "/v1/monitor/claude-code/annotations"
SCHEMA = "tryaii.claude-code.annotations/1"
STATE_FILE = "sync.json"
STATE_SCHEMA = "tryaii.claude-code.sync/1"
LOCK_FILE = "sync.lock"
DEFAULT_DAYS = 7
MAX_CALLS = 500
MAX_ADVICE = 50
#: A call is annotated once it is this old (its live event is stored by then).
SETTLE_S = 120
#: An unmatched annotation is retried for this long, then dropped.
UNMATCHED_TTL_S = 24 * 3600
MIN_INTERVAL_S = 300
#: A lock older than this is a crashed run's (analyze can take minutes).
LOCK_STALE_S = 1800
#: analysis.json younger than this is reused; older is re-run (with consent).
ANALYSIS_MAX_AGE_S = 24 * 3600
_PATH_CHARS = ("/", "\\", ":")
#: The server's limits on an advice document (longer entries would fail the request).
ADVICE_MAX_LIST = 20
CLASS_MAX = 64


class SyncError(Exception):
    """A failed request; the message is one short line (never a key)."""


# --- payload ----------------------------------------------------------------------

def project_value(project) -> Optional[str]:
    """The project label to send: a plain base name, else None."""
    if not isinstance(project, str):
        return None
    value = project.strip()
    if not value or value == UNKNOWN_PROJECT or any(ch in value for ch in _PATH_CHARS):
        return None
    if not value.isprintable() or len(value) > 256:
        return None
    return value


def annotation(call, pricer, result: Optional[dict], taxonomy_version: Optional[str]) -> dict:
    """The annotation of one call (``result``: its stored analysis result or
    None: then project and process only)."""
    doc = {"event_id": request_event_id(call.request_id), "ts": format_ts(call.ts),
           "project": project_value(call.project), "process": call.process,
           "classification": None, "winner": None, "est_cost_winner_usd": None,
           "taxonomy_version": None}
    if isinstance(result, dict) and result.get("category"):
        doc["classification"] = {"category": result.get("category"),
                                 "subcategory": result.get("subcategory"),
                                 "confidence": result.get("confidence")}
        winner = result.get("recommended") if isinstance(result.get("recommended"), str) else None
        if winner is not None:
            doc["winner"] = winner
            now, alt = pricer.cost(call), pricer.cost(call, winner)
            if now is not None and alt is not None:
                doc["est_cost_winner_usd"] = round(alt["total"], 6)
        doc["taxonomy_version"] = taxonomy_version
    return doc


def advice_docs(doc: Optional[dict]) -> list:
    """One process advice document per ``processes[]`` row of an analysis:
    structured fields only (the advice sentence is not sent)."""
    if not doc:
        return []
    window = doc.get("window") or {}
    out = []
    for row in doc.get("processes") or []:
        if not isinstance(row, dict) or not isinstance(row.get("process"), str):
            continue
        rec = row.get("recommendation")
        out.append({
            "process": row["process"],
            "thread": row.get("thread"),
            "window_days": window.get("days"),
            "generated_at": doc.get("created_at"),
            "tasks": row.get("tasks"),
            "calls": row.get("calls"),
            "est_cost_usd": row.get("est_cost_usd"),
            "classes": [{"class": c["class"], "share": c.get("share")}
                        for c in row.get("classes") or [] if isinstance(c, dict)
                        and isinstance(c.get("class"), str)
                        and len(c["class"]) <= CLASS_MAX][:ADVICE_MAX_LIST],
            "good_enough_shares": [{"model": g["model"], "share": g.get("share")}
                                   for g in row.get("good_enough_shares") or []
                                   if isinstance(g, dict) and isinstance(g.get("model"), str)
                                   ][:ADVICE_MAX_LIST],
            "recommendation": ({"model": rec.get("model"), "alias": rec.get("alias"),
                                "good_enough_share": rec.get("good_enough_share"),
                                "saving_usd": rec.get("saving_usd"),
                                "saving_usd_per_month": rec.get("saving_usd_per_month")}
                               if isinstance(rec, dict) else None),
            "confidence": row.get("confidence"),
        })
    return out


def batches(calls: list, advice: list) -> list:
    """``[(calls, advice), ...]``: at most ``MAX_CALLS`` calls and
    ``MAX_ADVICE`` advice documents per request."""
    out = []
    i = j = 0
    while i < len(calls) or j < len(advice):
        out.append((calls[i:i + MAX_CALLS], advice[j:j + MAX_ADVICE]))
        i += MAX_CALLS
        j += MAX_ADVICE
    return out


def body(calls: list, advice: list, version: str) -> dict:
    from tryaii.monitor.privacy import NOTICE_VERSION

    return {"schema": SCHEMA, "sdk": {"lang": "python", "version": version},
            "notice_version": NOTICE_VERSION, "calls": calls, "advice": advice}


def check_payload(payload: dict) -> None:
    """Refuse a payload with a forbidden field name (the uploader's scan) or a
    path-like project: nothing of the kind may leave the machine."""
    from tryaii.monitor.upload import find_forbidden

    bad = find_forbidden(payload) or _forbidden_any_case(payload)
    if bad:
        raise SyncError(f"refused: the payload has a forbidden field ({bad})")
    for call in payload.get("calls") or []:
        if call.get("project") is not None and project_value(call["project"]) is None:
            raise SyncError("refused: a project value looks like a path")


def _forbidden_any_case(obj) -> Optional[str]:
    """The server's scan ignores case: so does this one."""
    from tryaii.monitor.upload import FORBIDDEN_FIELDS

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_FIELDS:
                return key
            found = _forbidden_any_case(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _forbidden_any_case(value)
            if found:
                return found
    return None


# --- state ------------------------------------------------------------------------

def state_path(data_dir) -> Path:
    return Path(data_dir) / STATE_DIR / STATE_FILE


def lock_path(data_dir) -> Path:
    return Path(data_dir) / STATE_DIR / LOCK_FILE


def read_state(data_dir) -> dict:
    try:
        doc = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
        doc = {}
    return {"schema": STATE_SCHEMA,
            "cursor": doc.get("cursor"),
            "last_run_at": doc.get("last_run_at"),
            "last_ok_at": doc.get("last_ok_at"),
            "last_error": doc.get("last_error"),
            #: event id -> {"ts", "classified"}
            "applied": dict(doc.get("applied") or {}),
            #: event id -> {"ts", "first_unmatched_at"}
            "unmatched": dict(doc.get("unmatched") or {}),
            #: event id -> ts (unmatched for longer than UNMATCHED_TTL_S)
            "dropped": dict(doc.get("dropped") or {}),
            "advice_generated_at": doc.get("advice_generated_at")}


def write_state(data_dir, state: dict) -> Path:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, (json.dumps(state, separators=(",", ":")) + "\n").encode("utf-8"))
    return path


def prune(state: dict, since: datetime) -> None:
    """Forget ids of calls before the read window (they are never read again)."""
    cut = format_ts(since)
    for name in ("applied", "unmatched"):
        state[name] = {k: v for k, v in state[name].items()
                       if isinstance(v, dict) and str(v.get("ts") or "") >= cut}
    state["dropped"] = {k: v for k, v in state["dropped"].items() if str(v) >= cut}


def seconds_since(stamp: Optional[str], now: datetime) -> Optional[float]:
    if not stamp:
        return None
    try:
        return (now - parse_ts(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


def too_soon(state: dict, now: datetime) -> Optional[float]:
    """Seconds until the next run may start, or None when it may start now."""
    age = seconds_since(state.get("last_run_at"), now)
    if age is None or age < 0 or age >= MIN_INTERVAL_S:
        return None
    return MIN_INTERVAL_S - age


class Lock:
    """An exclusive lock file; a lock older than ``LOCK_STALE_S`` is taken over."""

    def __init__(self, data_dir, clock: Callable[[], float] = time.time):
        self.path = lock_path(data_dir)
        self._clock = clock
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    age = self._clock() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age < LOCK_STALE_S:
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            self.held = True
            return True
        return False

    def release(self) -> None:
        if self.held:
            self.held = False
            try:
                self.path.unlink()
            except OSError:
                pass


# --- analysis ---------------------------------------------------------------------

def fresh_analysis(doc: Optional[dict], consent: Optional[dict], now: datetime) -> bool:
    """True when ``doc`` may be reused: younger than ``ANALYSIS_MAX_AGE_S``
    and made under the current consent."""
    if not doc or not consent:
        return False
    if (doc.get("consent") or {}).get("version") != analysis_mod.CONSENT_VERSION:
        return False
    age = seconds_since(doc.get("created_at"), now)
    return age is not None and 0 <= age < ANALYSIS_MAX_AGE_S


def choose_analysis(data_dir, now: datetime,
                    reanalyze: Optional[Callable[[dict, Optional[dict]], Optional[dict]]]
                    ) -> tuple[Optional[dict], str]:
    """``(analysis doc or None, how)``. Without a current consent nothing of an
    analysis is used (``how`` = ``no_consent``)."""
    consent = analysis_mod.read_consent(data_dir)
    if consent is None:
        return None, "no_consent"
    doc = analysis_mod.read_analysis(data_dir)
    if fresh_analysis(doc, consent, now):
        return doc, "reused"
    if reanalyze is not None:
        try:
            new = reanalyze(consent, doc)
        except Exception:  # noqa: BLE001 -- sync goes on with what it has
            new = None
        if new:
            return new, "analyzed"
    if doc and (doc.get("consent") or {}).get("version") == analysis_mod.CONSENT_VERSION:
        return doc, "stale"
    return None, "none"


# --- one run ----------------------------------------------------------------------

def plan(calls: list, state: dict, results: dict, pricer, taxonomy: Optional[str],
         now: datetime) -> list:
    """The annotations to send: settled calls with a request id that are not
    applied yet (or applied without the task profile they now have) and not
    dropped."""
    settled = now - timedelta(seconds=SETTLE_S)
    out = []
    seen = set()
    for call in calls:
        if not call.request_id or call.ts > settled:
            continue
        doc = annotation(call, pricer, results.get(call.key), taxonomy)
        eid = doc["event_id"]
        if eid in seen or eid in state["dropped"]:
            continue
        seen.add(eid)
        applied = state["applied"].get(eid)
        if applied and (applied.get("classified") or doc["classification"] is None):
            continue
        out.append(doc)
    return out


def send(payload: dict, *, key: str, base_url: str, opener=None) -> dict:
    from tryaii.monitor import api

    check_payload(payload)
    try:
        resp = api.request("POST", base_url.rstrip("/") + ROUTE, payload, bearer=key,
                           opener=opener)
    except api.ApiNetworkError as exc:
        raise SyncError(f"network error ({exc})") from None
    if resp.status == 200 and isinstance(resp.body, dict):
        return resp.body
    if resp.status in (401, 403):
        raise SyncError("the app key was rejected (invalid_key)")
    if resp.status == 404:
        raise SyncError("the server does not offer annotations yet (404)")
    raise SyncError(f"HTTP {resp.status}" + (f" ({resp.error})" if resp.error else ""))


def apply_response(state: dict, sent: list, response: dict, now: datetime) -> tuple[int, int]:
    """Record one answered request; returns ``(matched, unmatched)``."""
    unmatched = {u for u in response.get("unmatched") or [] if isinstance(u, str)}
    unmatched &= {d["event_id"] for d in sent}
    stamp = format_ts(now)
    for doc in sent:
        eid = doc["event_id"]
        if eid in unmatched:
            first = (state["unmatched"].get(eid) or {}).get("first_unmatched_at") or stamp
            if (seconds_since(first, now) or 0) >= UNMATCHED_TTL_S:
                state["unmatched"].pop(eid, None)
                state["dropped"][eid] = doc["ts"]
            else:
                state["unmatched"][eid] = {"ts": doc["ts"], "first_unmatched_at": first}
        else:
            state["unmatched"].pop(eid, None)
            state["applied"][eid] = {"ts": doc["ts"],
                                     "classified": doc["classification"] is not None}
    return len(sent) - len(unmatched), len(unmatched)


def run(data_dir, calls: list, *, pricer, now: datetime, since: datetime, key: str,
        base_url: str, version: str, analysis_doc: Optional[dict], state: dict,
        opener=None) -> dict:
    """Send the annotations and advice; updates ``state`` (the caller writes
    it). Returns counts; raises SyncError on the first failed request (what was
    answered before it is kept)."""
    prune(state, since)
    results = (analysis_doc or {}).get("calls") if analysis_doc else None
    results = results if isinstance(results, dict) else {}
    taxonomy = (analysis_doc or {}).get("taxonomy_version")
    todo = plan(calls, state, results, pricer, taxonomy, now)
    advice = advice_docs(analysis_doc)
    if advice and state.get("advice_generated_at") == (analysis_doc or {}).get("created_at"):
        advice = []
    counts = {"annotations": len(todo), "matched": 0, "unmatched": 0, "advice": len(advice),
              "advice_stored": 0, "requests": 0}
    for part_calls, part_advice in batches(todo, advice):
        response = send(body(part_calls, part_advice, version), key=key, base_url=base_url,
                        opener=opener)
        counts["requests"] += 1
        matched, unmatched = apply_response(state, part_calls, response, now)
        counts["matched"] += matched
        counts["unmatched"] += unmatched
        stored = response.get("advice_stored")
        counts["advice_stored"] += stored if isinstance(stored, int) else 0
    if advice:
        state["advice_generated_at"] = (analysis_doc or {}).get("created_at")
    state["cursor"] = format_ts(now - timedelta(seconds=SETTLE_S))
    return counts


# --- background -------------------------------------------------------------------

#: Windows creation flags of the detached run.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def spawn_background(args: list, popen=subprocess.Popen) -> None:
    """Start ``tryaii claude-code sync <args>`` detached from this process (no
    console, own process group / session, no inherited stdio) and return at
    once."""
    cmd = [sys.executable, "-c", "from tryaii.cli.main import cli; cli()", "claude-code",
           "sync", *args]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    popen(cmd, **kwargs)
