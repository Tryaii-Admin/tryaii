"""Event records (SPEC.md §1.2) — key order, rounding and cost estimates."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from tryaii.budget import estimate_generation_cost
from tryaii.cachelint._jsonutil import normalize_numbers

EVENT_SCHEMA = "tryaii.monitor.event/1"
VECTOR_SCHEMA = "tryaii.monitor.vector/1"
ROUTE_STATUSES = ("ok", "unavailable", "timeout", "disabled", "queue_full", "skipped",
                  "unreachable_managed")
TAXONOMY_VERSION_KEY = "taxonomy_version"


def new_event_id() -> str:
    """A fresh uuid4 string (the event's idempotency key upstream)."""
    return str(uuid.uuid4())


def taxonomy_version(bundle) -> Optional[str]:
    """The benchmark-taxonomy version of a catalog bundle (SPEC.md §3.1):
    ``benchmarks.json``'s top-level ``taxonomy_version``, else the
    manifest's, as a non-empty string; None when neither carries one (true
    of every bundle released so far)."""
    if bundle is None:
        return None
    for doc in (getattr(bundle, "benchmarks", None), getattr(bundle, "manifest", None)):
        if isinstance(doc, dict):
            value = doc.get(TAXONOMY_VERSION_KEY)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
COST_DECIMALS = 6
TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def format_ts(dt: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.mmmZ`` — naive datetimes are taken as UTC."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return f"{dt.strftime(TS_FORMAT)}.{dt.microsecond // 1000:03d}Z"


def parse_ts(text: str) -> datetime:
    """Inverse of :func:`format_ts`; also accepts whole-second ``...SSZ``
    and a ``+00:00`` offset. Raises ValueError otherwise."""
    if not isinstance(text, str):
        raise ValueError("timestamp must be a string")
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1]
    elif value.endswith("+00:00"):
        value = value[:-6]
    for fmt in (TS_FORMAT + ".%f", TS_FORMAT):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"bad timestamp: {text!r}")


def round_cost(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, COST_DECIMALS)


def estimate_cost(registry, model_id: Optional[str], usage: Optional[dict]) -> Optional[float]:
    """USD for one call at catalog prices, 6 dp; None when the model is not
    in the catalog, unpriced, or the usage has no token counts."""
    if not model_id or not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, (int, float)) or not isinstance(completion, (int, float)):
        return None
    model = registry.get_model(model_id)
    if model is None:
        return None
    return round_cost(estimate_generation_cost(model, int(prompt), int(completion)))


def usage_from_response(usage: Any) -> Optional[dict]:
    """Normalise a provider ``usage`` (object or dict) to the event shape."""
    if usage is None:
        return None
    get = usage.get if isinstance(usage, dict) else lambda k, d=None: getattr(usage, k, d)
    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = get(key)
        out[key] = int(value) if isinstance(value, (int, float)) else None
    if all(v is None for v in out.values()):
        return None
    return out


def classification_from_wire(cls: Any) -> Optional[dict]:
    """``{"category", "subcategory", "confidence"}`` from a daemon
    classification (``broadCategory``...) or a ClassificationResult."""
    if cls is None:
        return None
    if isinstance(cls, dict):
        category = cls.get("broadCategory", cls.get("broad_category"))
        sub = cls.get("subcategory")
        conf = cls.get("confidence")
    else:
        category = getattr(cls, "broad_category", None)
        sub = getattr(cls, "subcategory", None)
        conf = getattr(cls, "confidence", None)
    return {
        "category": category or None,
        "subcategory": sub or None,
        "confidence": round(float(conf), 4) if isinstance(conf, (int, float)) else None,
    }


def build_event(*, ts: str, app: str, process: str, call_site: Optional[dict],
                provider: str, model_requested: Optional[str],
                model_resolved: Optional[str], model_used: Optional[str],
                mode: str, winner: Optional[str], winner_reachable: bool,
                route_status: str, classification: Optional[dict],
                usage: Optional[dict], est_cost_usd: Optional[float],
                est_cost_winner_usd: Optional[float], latency_ms: Optional[int],
                stream: bool, error: Optional[dict], prompt_hash: str,
                vector: bool, event_id: Optional[str] = None,
                taxonomy_version: Optional[str] = None,
                est_cost_anchor_usd: Optional[float] = None) -> dict:
    """One event line in contract key order (SPEC.md §3.1). ``event_id``
    defaults to a fresh uuid4; ``est_cost_anchor_usd`` is the usage priced
    at the anchor (equal to ``est_cost_usd`` when nothing was swapped)."""
    site = None
    if call_site:
        site = {
            "file": call_site.get("file"),
            "line": call_site.get("line"),
            "function": call_site.get("function"),
        }
    event = {
        "schema": EVENT_SCHEMA,
        "event_id": event_id or new_event_id(),
        "ts": ts,
        "app": app,
        "process": process,
        "call_site": site,
        "provider": provider,
        "model_requested": model_requested,
        "model_resolved": model_resolved,
        "model_used": model_used,
        "mode": mode,
        "winner": winner,
        "winner_reachable": bool(winner_reachable),
        "route_status": route_status,
        "classification": classification,
        "taxonomy_version": taxonomy_version,
        "usage": usage,
        "est_cost_usd": round_cost(est_cost_usd),
        "est_cost_winner_usd": round_cost(est_cost_winner_usd),
        "est_cost_anchor_usd": round_cost(est_cost_anchor_usd),
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "stream": bool(stream),
        "error": error,
        "prompt_hash": prompt_hash,
        "vector": bool(vector),
    }
    return normalize_numbers(event)


def build_vector(*, prompt_hash: str, ts: str, process: str, model: str,
                 vector) -> dict:
    """One vectors line: the prompt's embedding, 4 dp (SPEC.md §1.3)."""
    values = [round(float(x), 4) for x in vector]
    return normalize_numbers({
        "schema": VECTOR_SCHEMA,
        "prompt_hash": prompt_hash,
        "ts": ts,
        "process": process,
        "model": model,
        "dim": len(values),
        "v": values,
    })
