"""Stable-mode pins (SPEC.md §2.5). Clock-free: ``now`` is an input.

The pinned model is "the winner of a sub-process": the most frequent
reachable per-call winner over the pin window, ties broken by the higher
catalog benchmark score, then the lower blended price, then the model id
(never by list or event order). The monitor backend computes the dashboard's
``pin_candidate`` with the same rule over the uploaded events, so "Today
that is X" on the dashboard is the model this module pins.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

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


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def catalog_quality(scores: Any, ranges: Any) -> Optional[float]:
    """The catalog-level benchmark score of a model (API.md §3.4
    ``GET /v1/monitor/models`` ``quality``): the mean over its benchmarks of
    ``(score - lo) / (hi - lo)`` clipped to 0..1, with ``lo``/``hi`` from the
    bundle's ``normalization_ranges.json``; 4 dp; None without any ranged
    benchmark. Raw catalog scores (no plausibility filter), like the server."""
    if not isinstance(scores, dict) or not isinstance(ranges, dict):
        return None
    parts = []
    for name, value in scores.items():
        rng = ranges.get(name)
        if not isinstance(rng, dict) or not _number(value):
            continue
        lo, hi = rng.get("lo"), rng.get("hi")
        if not _number(lo) or not _number(hi) or hi <= lo:
            continue
        parts.append(min(1.0, max(0.0, (value - lo) / (hi - lo))))
    return round(sum(parts) / len(parts), 4) if parts else None


def blended_price_per_m(pricing: Any) -> Optional[float]:
    """USD per 1M tokens: the mean of the input and output per-1k prices
    x 1000, 6 dp; None when unpriced (API.md §3.4 ``blended_price_usd_per_m``)."""
    if not isinstance(pricing, dict):
        return None
    i, o = pricing.get("input_per_1k"), pricing.get("output_per_1k")
    if not _number(i) or not _number(o):
        return None
    return round((i + o) / 2 * 1000, 6)


def model_facts(bundle) -> dict:
    """``{model id: {"quality", "blended_price_usd_per_m"}}`` for every model
    of a catalog bundle -- the tie-break inputs of :func:`winner_counts`,
    defined exactly as the server's ``GET /v1/monitor/models`` rows."""
    ranges = (getattr(bundle, "normalization_ranges", None) or {}).get("benchmarks") or {}
    models = (getattr(bundle, "models", None) or {}).get("models") or []
    out: dict = {}
    for data in models:
        if not isinstance(data, dict) or not isinstance(data.get("model_id"), str):
            continue
        out[data["model_id"]] = {
            "quality": catalog_quality(data.get("benchmark_scores"), ranges),
            "blended_price_usd_per_m": blended_price_per_m(data.get("pricing")),
        }
    return out


def _facts(models: Optional[dict], model_id: str) -> tuple:
    row = (models or {}).get(model_id) or {}
    quality = row.get("quality")
    price = row.get("blended_price_usd_per_m")
    return (quality if _number(quality) else None, price if _number(price) else None)


def rank_key(model_id: str, count: int, models: Optional[dict] = None) -> tuple:
    """Sort key of a winner (SPEC.md §2.5): count desc, then benchmark score
    desc, then blended price asc (unknown score / price after known ones),
    then model id asc."""
    quality, price = _facts(models, model_id)
    return (-count, quality is None, -(quality or 0.0), price is None, price or 0.0, model_id)


def tie_break(counts: dict, models: Optional[dict] = None) -> Optional[str]:
    """What decided between the first two entries of an ordered ``counts``
    when they have the same count: ``"benchmark score"``, ``"price"`` or
    ``"model id"``; None when the first entry has strictly more wins."""
    items = list(counts.items())
    if len(items) < 2 or items[0][1] != items[1][1]:
        return None
    (a, _), (b, _) = items[0], items[1]
    qa, pa = _facts(models, a)
    qb, pb = _facts(models, b)
    if qa != qb:
        return "benchmark score"
    if pa != pb:
        return "price"
    return "model id"


def winner_counts(events, process: str, now: str,
                  models: Optional[dict] = None) -> tuple[str, dict]:
    """``(window, counts)``: reachable winners of ``process`` in the last
    24 h, or the last 7 d when the 24 h window holds fewer than 20 of them.
    ``counts`` is ordered by :func:`rank_key` (count desc, then the catalog
    benchmark score desc, blended price asc, model id asc; ``models`` =
    :func:`model_facts`, None = no catalog, ids only)."""
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
    ordered = dict(sorted(counts.items(), key=lambda kv: rank_key(kv[0], kv[1], models)))
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


def evaluate_pin(events, pin: Optional[dict], now: str, process: str,
                 models: Optional[dict] = None) -> dict:
    """Decide the pin for ``process`` at ``now``.

    Returns ``{"action", "pin", "reason"}`` with action ``keep`` (the
    current pin is still valid), ``pin`` (a new pin built from history) or
    ``none`` (no usable history — the caller routes this call in-path and,
    if the winner is reachable, pins it via :func:`pin_from_first_call`).
    ``models`` (:func:`model_facts`) breaks ties between equally frequent
    winners; without it ties fall back to the model id.
    """
    if pin_valid(pin, now):
        return {"action": "keep", "pin": pin, "reason": "pin valid"}
    window, counts = winner_counts(events, process, now, models)
    if not counts:
        return {"action": "none", "pin": None, "reason": "no reachable winners in history"}
    model = next(iter(counts))
    reason = f"most frequent winner over {window}"
    decided = tie_break(counts, models)
    if decided is not None:
        reason += f" (tie broken by {decided})"
    return {"action": "pin", "pin": make_pin(model, now, window, counts), "reason": reason}


def set_pin(pins: Optional[dict], process: str, pin: Optional[dict]) -> dict:
    """A new pins document with ``process`` pinned (or cleared when None)."""
    doc = {"schema": PINS_SCHEMA, "processes": dict((pins or {}).get("processes") or {})}
    if pin is None:
        doc["processes"].pop(process, None)
    else:
        doc["processes"][process] = pin
    doc["processes"] = dict(sorted(doc["processes"].items()))
    return doc
