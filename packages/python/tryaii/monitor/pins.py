"""Stable-mode pins (SPEC.md §2.5). Clock-free: ``now`` is an input."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from tryaii.monitor.events import format_ts, parse_ts

PINS_SCHEMA = "tryaii.monitor.pins/1"
PIN_TTL = timedelta(hours=24)
WINDOW_SHORT = ("24h", timedelta(hours=24))
WINDOW_LONG = ("7d", timedelta(days=7))
MIN_EVENTS_SHORT = 20


def empty_pins() -> dict:
    return {"schema": PINS_SCHEMA, "processes": {}}


def pin_valid(pin: Optional[dict], now: str) -> bool:
    """True while ``now`` is before the pin's ``expires_at``."""
    if not isinstance(pin, dict) or not pin.get("model"):
        return False
    try:
        return parse_ts(now) < parse_ts(pin["expires_at"])
    except (KeyError, ValueError, TypeError):
        return False


def _in_window(event: dict, process: str, start, end) -> bool:
    if event.get("process") != process:
        return False
    try:
        ts = parse_ts(event["ts"])
    except (KeyError, ValueError, TypeError):
        return False
    return start <= ts <= end


def winner_counts(events, process: str, now: str) -> tuple[str, dict]:
    """``(window, counts)``: reachable winners of ``process`` in the last
    24 h, or the last 7 d when the 24 h window holds fewer than 20 of them.
    ``counts`` is ordered by count desc, then model id asc."""
    end = parse_ts(now)
    chosen = None
    for name, span in (WINDOW_SHORT, WINDOW_LONG):
        start = end - span
        counts: dict = {}
        for event in events:
            if not _in_window(event, process, start, end):
                continue
            if not event.get("winner") or not event.get("winner_reachable"):
                continue
            counts[event["winner"]] = counts.get(event["winner"], 0) + 1
        chosen = (name, counts)
        if sum(counts.values()) >= MIN_EVENTS_SHORT:
            break
    name, counts = chosen
    ordered = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return name, ordered


def make_pin(model: str, now: str, window: str, counts: dict) -> dict:
    start = parse_ts(now)
    return {
        "model": model,
        "pinned_at": format_ts(start),
        "expires_at": format_ts(start + PIN_TTL),
        "basis": {"window": window, "events": sum(counts.values()), "counts": counts},
    }


def pin_from_first_call(winner: str, now: str) -> dict:
    """The pin for a process with no history: this call's winner."""
    return make_pin(winner, now, "first_call", {winner: 1})


def evaluate_pin(events, pin: Optional[dict], now: str, process: str) -> dict:
    """Decide the pin for ``process`` at ``now``.

    Returns ``{"action", "pin", "reason"}`` with action ``keep`` (the
    current pin is still valid), ``pin`` (a new pin built from history) or
    ``none`` (no usable history — the caller routes this call in-path and,
    if the winner is reachable, pins it via :func:`pin_from_first_call`).
    """
    if pin_valid(pin, now):
        return {"action": "keep", "pin": pin, "reason": "pin valid"}
    window, counts = winner_counts(events, process, now)
    if not counts:
        return {"action": "none", "pin": None, "reason": "no reachable winners in history"}
    model = next(iter(counts))
    return {"action": "pin", "pin": make_pin(model, now, window, counts),
            "reason": f"most frequent winner over {window}"}


def set_pin(pins: Optional[dict], process: str, pin: Optional[dict]) -> dict:
    """A new pins document with ``process`` pinned (or cleared when None)."""
    doc = {"schema": PINS_SCHEMA, "processes": dict((pins or {}).get("processes") or {})}
    if pin is None:
        doc["processes"].pop(process, None)
    else:
        doc["processes"][process] = pin
    doc["processes"] = dict(sorted(doc["processes"].items()))
    return doc
