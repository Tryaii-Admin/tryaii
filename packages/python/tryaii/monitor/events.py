"""Event records (SPEC.md §1.2) — key order, rounding and cost estimates."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from tryaii.budget import estimate_generation_cost
from tryaii.cachelint._jsonutil import normalize_numbers
from tryaii.monitor.cache import cached_tokens_from_usage

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


#: The event's ``usage`` keys, in contract order (SPEC.md §3.1, §3.1b).
USAGE_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
USAGE_CACHE_KEYS = ("cached_tokens", "cache_write_tokens", "cache_writes")
USAGE_KEYS = USAGE_TOKEN_KEYS + USAGE_CACHE_KEYS
#: Cache lifetimes a write or a request policy can name (SPEC.md §3.1b).
CACHE_TTLS = ("5m", "1h", "30m", "24h", "explicit")
#: Provider API shapes ``normalize_cache_usage`` knows (SPEC.md §3.1b).
CACHE_PROVIDERS = ("openai", "openai_responses", "openrouter", "azure", "anthropic",
                   "bedrock", "gemini")


def _get(obj: Any, key: str) -> Any:
    """``obj[key]`` for a dict, ``getattr`` otherwise; None when absent."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _count(value: Any) -> Optional[int]:
    """A token count: a non-negative number truncated to int; booleans,
    negatives and non-numbers count as absent (SPEC.md §3.1b rule 5)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:  # NaN, inf, negative
        return None
    return int(value)


def _ttl(value: Any) -> Optional[str]:
    """A lifetime of ``CACHE_TTLS`` (lower-cased, trimmed), else None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if text in CACHE_TTLS else None


def normalize_cache_writes(writes: Any) -> Optional[list]:
    """``[{"tokens", "ttl"}, ...]`` from a breakdown list; entries with zero
    tokens, a bad count or an unknown ttl are dropped; None when ``writes``
    is not a list (no breakdown)."""
    if not isinstance(writes, (list, tuple)):
        return None
    out = []
    for entry in writes:
        tokens = _count(_get(entry, "tokens"))
        ttl = _ttl(_get(entry, "ttl"))
        if tokens and ttl:
            out.append({"tokens": tokens, "ttl": ttl})
    return out


def _cache_fields(read: Any, write: Any, writes: Any) -> dict:
    cached = _count(read)
    total = _count(write)
    breakdown = normalize_cache_writes(writes)
    if total is None and breakdown is not None:
        total = sum(w["tokens"] for w in breakdown)
    return {"cached_tokens": cached, "cache_write_tokens": total,
            "cache_writes": breakdown}


def _openai_cache(raw: Any, details_key: str, top_level_read: bool = False) -> dict:
    details = _get(raw, details_key)
    read = _get(details, "cached_tokens")
    if read is None and top_level_read:
        read = _get(raw, "cached_tokens")
    write = _get(details, "cache_write_tokens")
    if write is None:
        write = _get(raw, "cache_write_tokens")
    return _cache_fields(read, write, None)


def _anthropic_cache(raw: Any) -> dict:
    creation = _get(raw, "cache_creation")
    writes = None
    if creation is not None:
        writes = [
            {"tokens": _get(creation, "ephemeral_5m_input_tokens"), "ttl": "5m"},
            {"tokens": _get(creation, "ephemeral_1h_input_tokens"), "ttl": "1h"},
        ]
    return _cache_fields(_get(raw, "cache_read_input_tokens"),
                         _get(raw, "cache_creation_input_tokens"), writes)


def _bedrock_cache(raw: Any) -> dict:
    details = _get(raw, "cacheDetails")
    writes = None
    if isinstance(details, (list, tuple)):
        writes = [{"tokens": _get(d, "inputTokens"), "ttl": _get(d, "ttl")} for d in details]
    return _cache_fields(_get(raw, "cacheReadInputTokens"),
                         _get(raw, "cacheWriteInputTokens"), writes)


def _gemini_cache(raw: Any) -> dict:
    meta = _get(raw, "usageMetadata")
    if meta is None:
        meta = _get(raw, "usage_metadata")
    if meta is None:
        meta = raw
    read = _get(meta, "cachedContentTokenCount")
    if read is None:
        read = _get(meta, "cached_content_token_count")
    return _cache_fields(read, None, None)


def normalize_cache_usage(provider: Any, raw_usage: Any) -> dict:
    """Map a provider's raw usage object to the event's cache columns
    (SPEC.md §3.1b): ``{"cached_tokens", "cache_write_tokens",
    "cache_writes"}``, every key present, None for what the provider does
    not report. ``provider`` is the API shape (``CACHE_PROVIDERS``); an
    unknown shape or no usage answers all None. Pure: TS-229 calls it from
    the wrappers, the ``cache_usage`` fixtures freeze it."""
    empty = {"cached_tokens": None, "cache_write_tokens": None, "cache_writes": None}
    if raw_usage is None or not isinstance(provider, str):
        return empty
    shape = provider.strip().lower()
    if shape in ("openai", "azure"):
        return _openai_cache(raw_usage, "prompt_tokens_details")
    if shape == "openrouter":
        return _openai_cache(raw_usage, "prompt_tokens_details", top_level_read=True)
    if shape == "openai_responses":
        return _openai_cache(raw_usage, "input_tokens_details")
    if shape == "anthropic":
        return _anthropic_cache(raw_usage)
    if shape == "bedrock":
        return _bedrock_cache(raw_usage)
    if shape == "gemini":
        return _gemini_cache(raw_usage)
    return empty


def normalize_usage(usage: Any) -> Optional[dict]:
    """The event's ``usage`` in contract key order from an event-shaped
    dict (or object): the three token counts plus the §3.1b cache fields,
    each None when absent or malformed; None when nothing is numeric."""
    if usage is None:
        return None
    out = {}
    for key in USAGE_TOKEN_KEYS:
        out[key] = _count(_get(usage, key))
    cache = _cache_fields(_get(usage, "cached_tokens"),
                          _get(usage, "cache_write_tokens"), _get(usage, "cache_writes"))
    out.update(cache)
    if all(v is None for v in out.values()):
        return None
    return out


def normalize_cache_ttl_policy(value: Any) -> Optional[str]:
    """``cache_ttl_policy``: one of ``CACHE_TTLS`` or None (SPEC.md §3.1b)."""
    return _ttl(value)


def usage_from_response(usage: Any) -> Optional[dict]:
    """Normalise a provider ``usage`` (object or dict) to the event shape.
    ``cached_tokens`` is read from the raw usage (API.md v1.4:
    ``prompt_tokens_details.cached_tokens`` or ``cache_read_input_tokens``)
    unless the input already carries it under its event name; the write
    columns pass through when the input carries them under their event
    names (TS-229 merges ``normalize_cache_usage`` into the raw usage before
    calling this) and are None otherwise."""
    out = normalize_usage(usage)
    if out is not None and out["cached_tokens"] is None:
        out["cached_tokens"] = cached_tokens_from_usage(usage)
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
                est_cost_anchor_usd: Optional[float] = None,
                cache_ttl_policy: Optional[str] = None,
                cache: Optional[dict] = None) -> dict:
    """One event line in contract key order (SPEC.md §3.1). ``event_id``
    defaults to a fresh uuid4; ``est_cost_anchor_usd`` is the usage priced
    at the anchor (equal to ``est_cost_usd`` when nothing was swapped);
    ``usage`` is re-emitted in contract order with every §3.1b cache key
    present and ``cache_ttl_policy`` must be a known lifetime or None;
    ``cache`` (API.md v1.4) is the last key, present only when the call ran
    the runtime cache lint."""
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
        "usage": normalize_usage(usage),
        "cache_ttl_policy": normalize_cache_ttl_policy(cache_ttl_policy),
        "est_cost_usd": round_cost(est_cost_usd),
        "est_cost_winner_usd": round_cost(est_cost_winner_usd),
        "est_cost_anchor_usd": round_cost(est_cost_anchor_usd),
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "stream": bool(stream),
        "error": error,
        "prompt_hash": prompt_hash,
        "vector": bool(vector),
    }
    if cache is not None:
        event["cache"] = cache
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
