"""Status aggregation for ``tryaii monitor status`` (SPEC.md §2.7)."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from tryaii.cachelint._jsonutil import normalize_numbers
from tryaii.monitor.config import resolve_mode
from tryaii.monitor.events import parse_ts, round_cost
from tryaii.monitor.pins import pin_valid

STATUS_SCHEMA = "tryaii.monitor.status/1"
WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def window_seconds(since: str) -> float:
    if since not in WINDOWS:
        raise ValueError("since must be 24h, 7d or 30d")
    return WINDOWS[since].total_seconds()


def display_path(path: Optional[str]) -> Optional[str]:
    return path.replace("\\", "/") if isinstance(path, str) else None


def _swapped(event: dict) -> bool:
    """A call whose model was replaced (``model_used != model_requested``)."""
    used, requested = event.get("model_used"), event.get("model_requested")
    return bool(used) and bool(requested) and used != requested


def source_info(remote: Optional[dict]) -> dict:
    """``{"kind", "etag", "fetched_at"}``: ``local`` without a usable
    remote.json, else ``remote`` with its etag and fetch time."""
    if isinstance(remote, dict):
        etag = remote.get("etag")
        fetched = remote.get("fetched_at")
        return {"kind": "remote",
                "etag": etag if isinstance(etag, str) else None,
                "fetched_at": fetched if isinstance(fetched, str) else None}
    return {"kind": "local", "etag": None, "fetched_at": None}


def upload_info(cursor: Optional[dict], uploaded_events: int = 0) -> dict:
    """``{"state", "events", "last_ok", "last_error"}`` from upload.json:
    ``off`` without a cursor; ``error`` while ``last_error`` is set;
    else ``ok``. ``events`` = the event lines the cursor covers."""
    if not isinstance(cursor, dict):
        return {"state": "off", "events": 0, "last_ok": None, "last_error": None}
    state = "error" if cursor.get("last_error") else "ok"
    return {"state": state, "events": int(uploaded_events),
            "last_ok": cursor.get("last_ok"), "last_error": cursor.get("last_error")}


def aggregate(app: str, events, pins: Optional[dict], config: Optional[dict],
              now: str, since: str = "24h", env: Optional[dict] = None,
              remote: Optional[dict] = None, upload_cursor: Optional[dict] = None,
              uploaded_events: int = 0) -> dict:
    """Per-process roll-up of ``events`` within ``since`` of ``now``.

    Processes = those with events in the window ∪ those named in
    ``config.processes`` ∪ ``remote.processes`` ∪ those with a pin, sorted
    by name. Costs are 6 dp; ``est_cost_winner_usd`` / ``savings_usd`` only
    count events that carry BOTH cost estimates (and are null when none do).
    """
    end = parse_ts(now)
    start = end - WINDOWS[since] if since in WINDOWS else None
    if start is None:
        raise ValueError("since must be 24h, 7d or 30d")
    pins = pins or {}
    config = config or {}
    by_process: dict[str, list] = {}
    for event in events:
        try:
            ts = parse_ts(event["ts"])
        except (KeyError, ValueError, TypeError):
            continue
        if ts < start or ts > end:
            continue
        name = event.get("process") or "default"
        by_process.setdefault(name, []).append(event)
    names = set(by_process)
    names.update((config.get("processes") or {}).keys())
    names.update((pins.get("processes") or {}).keys())
    if isinstance(remote, dict) and isinstance(remote.get("processes"), dict):
        names.update(remote["processes"].keys())

    processes = []
    total_calls = total_errors = 0
    total_cost = total_paired = total_winner = 0.0
    any_paired = False
    total_swapped = 0
    total_anchor = total_realised = 0.0
    any_anchored = False
    total_cache: dict = {"cached_tokens": None, "cache_write_tokens": None}
    for name in sorted(names):
        rows = by_process.get(name, [])
        mode = resolve_mode(name, env=env, remote=remote, config=config)
        pin = (pins.get("processes") or {}).get(name)
        pinned = None
        if pin_valid(pin, now):
            pinned = {"model": pin["model"], "expires_at": pin["expires_at"]}
        calls = len(rows)
        errors = sum(1 for e in rows if e.get("error"))
        tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cache: dict = {"cached_tokens": None, "cache_write_tokens": None}
        for e in rows:
            usage = e.get("usage") or {}
            for key in tokens:
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    tokens[key] += int(value)
            for key in cache:
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cache[key] = (cache[key] or 0) + int(value)
        cost = sum(e["est_cost_usd"] for e in rows if e.get("est_cost_usd") is not None)
        paired = [e for e in rows
                  if e.get("est_cost_usd") is not None and e.get("est_cost_winner_usd") is not None]
        cost_paired = sum(e["est_cost_usd"] for e in paired)
        cost_winner = sum(e["est_cost_winner_usd"] for e in paired)
        anchored = [e for e in rows if e.get("est_cost_anchor_usd") is not None]
        cost_anchor = sum(e["est_cost_anchor_usd"] for e in anchored)
        swapped = [e for e in rows if _swapped(e)]
        realised = sum(max(0.0, e["est_cost_anchor_usd"] - e["est_cost_usd"])
                       for e in swapped
                       if e.get("est_cost_anchor_usd") is not None
                       and e.get("est_cost_usd") is not None)
        counts: dict = {}
        for e in rows:
            if e.get("winner"):
                counts[e["winner"]] = counts.get(e["winner"], 0) + 1
        top = None
        if counts:
            model, n = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            top = {"model": model, "share": round(n / sum(counts.values()), 4)}
        files = sorted({display_path((e.get("call_site") or {}).get("file"))
                        for e in rows} - {None})
        processes.append({
            "process": name,
            "mode": mode["mode"],
            "mode_source": mode["source"],
            "managed_model": mode["model"],
            "pinned": pinned,
            "calls": calls,
            "errors": errors,
            "prompt_tokens": tokens["prompt_tokens"],
            "completion_tokens": tokens["completion_tokens"],
            "total_tokens": tokens["total_tokens"],
            "cached_tokens": cache["cached_tokens"],
            "cache_write_tokens": cache["cache_write_tokens"],
            "est_cost_usd": round_cost(cost),
            "est_cost_winner_usd": round_cost(cost_winner) if paired else None,
            "savings_usd": round_cost(cost_paired - cost_winner) if paired else None,
            "swapped_calls": len(swapped),
            "est_cost_anchor_usd": round_cost(cost_anchor) if anchored else None,
            "realised_savings_usd": round_cost(realised) if anchored else None,
            "top_winner": top,
            "files": files,
        })
        total_calls += calls
        total_errors += errors
        for key in total_cache:
            if cache[key] is not None:
                total_cache[key] = (total_cache[key] or 0) + cache[key]
        total_cost += cost
        total_paired += cost_paired
        total_winner += cost_winner
        any_paired = any_paired or bool(paired)
        total_swapped += len(swapped)
        total_anchor += cost_anchor
        total_realised += realised
        any_anchored = any_anchored or bool(anchored)
    doc = {
        "schema": STATUS_SCHEMA,
        "app": app,
        "now": now,
        "since": since,
        "source": source_info(remote),
        "upload": upload_info(upload_cursor, uploaded_events),
        "processes": processes,
        "totals": {
            "calls": total_calls,
            "errors": total_errors,
            "cached_tokens": total_cache["cached_tokens"],
            "cache_write_tokens": total_cache["cache_write_tokens"],
            "est_cost_usd": round_cost(total_cost),
            "est_cost_winner_usd": round_cost(total_winner) if any_paired else None,
            "savings_usd": round_cost(total_paired - total_winner) if any_paired else None,
            "swapped_calls": total_swapped,
            "est_cost_anchor_usd": round_cost(total_anchor) if any_anchored else None,
            "realised_savings_usd": round_cost(total_realised) if any_anchored else None,
        },
    }
    return normalize_numbers(doc)
