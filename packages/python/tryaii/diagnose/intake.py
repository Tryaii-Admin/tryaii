"""Lenient inventory intake (SPEC.md §2.0).

Accepts loosely-shaped agent-written inventories and normalizes them without
ever guessing: a malformed site is skipped with a reason; a malformed
optional field is simply absent. Nothing here raises except a top-level
shape that cannot be an inventory at all.
"""

from __future__ import annotations

import re
from typing import Any, Optional

DEFAULT_OUTPUT_TOKENS = 500

_INT_RE = re.compile(r"-?\d+")
_NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def _coerce_int(value: Any, minimum: int) -> Optional[int]:
    """int >= minimum from an int, integral float, or integer string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    elif isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        n = int(value.strip())
    else:
        return None
    return n if n >= minimum else None


def _coerce_positive_number(value: Any) -> Optional[float]:
    """Finite number > 0 from a number or numeric string (ints stay integral
    through the SPEC §1.3 normalizer at emission time)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    elif isinstance(value, str) and _NUM_RE.fullmatch(value.strip()):
        n = float(value.strip())
    else:
        return None
    return n if n > 0 and n == n and n not in (float("inf"), float("-inf")) else None


def _clean_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _clean_prompt(value: Any) -> Any:
    """A prompt is a string or a cachelint prompt object; wrong types are
    treated as absent (never guessed)."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return value
    return None


def _clean_classification(value: Any) -> Optional[dict]:
    """Validate the `_classification` injection seam; invalid → absent."""
    if not isinstance(value, dict):
        return None
    sims = value.get("benchmark_similarities")
    if not isinstance(sims, dict) or not sims:
        return None
    clean_sims: dict[str, float] = {}
    for name, score in sims.items():
        if not isinstance(name, str) or isinstance(score, bool) \
                or not isinstance(score, (int, float)):
            return None
        clean_sims[name] = float(score)
    out: dict[str, Any] = {"benchmark_similarities": clean_sims}
    for key in ("broad_category", "subcategory"):
        s = _clean_str(value.get(key))
        if s is not None:
            out[key] = s
    for key in ("confidence", "difficulty"):
        v = value.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = float(v)
    return out


def normalize_inventory(data: Any) -> dict:
    """Normalize an inventory (SPEC.md §2.0).

    Returns {"defaults": {"calls_per_day", "output_tokens"},
             "sites": [...], "skipped": [{"index", "reason"}]}.
    Raises ValueError only when the top-level shape is not an inventory.
    """
    if isinstance(data, list):
        raw_sites, raw_defaults = data, {}
    elif isinstance(data, dict) and isinstance(data.get("sites"), list):
        raw_sites = data["sites"]
        raw_defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    else:
        raise ValueError(
            "inventory must be an object with a 'sites' array, or an array of sites")

    defaults = {
        "calls_per_day": _coerce_positive_number(raw_defaults.get("calls_per_day")),
        "output_tokens": (
            _coerce_int(raw_defaults.get("output_tokens"), 1) or DEFAULT_OUTPUT_TOKENS),
    }

    sites: list[dict] = []
    skipped: list[dict] = []
    seen_ids: dict[str, int] = {}

    for index, raw in enumerate(raw_sites):
        if not isinstance(raw, dict):
            skipped.append({"index": index, "reason": "site is not an object"})
            continue
        file = _clean_str(raw.get("file"))
        if file is None:
            skipped.append({"index": index, "reason": "missing file"})
            continue
        line = _coerce_int(raw.get("line"), 1)
        if line is None:
            skipped.append({"index": index, "reason": "missing or invalid line"})
            continue

        site_id = _clean_str(raw.get("id")) or f"{file}:{line}"
        count = seen_ids.get(site_id, 0) + 1
        seen_ids[site_id] = count
        if count > 1:
            site_id = f"{site_id}#{count}"

        sites.append({
            "site_id": site_id,
            "index": index,
            "file": file,
            "line": line,
            "prompt": _clean_prompt(raw.get("prompt")),
            "provider": _clean_str(raw.get("provider")),
            "model": _clean_str(raw.get("model")),
            "calls_per_day": _coerce_positive_number(raw.get("calls_per_day")),
            "output_tokens": _coerce_int(raw.get("output_tokens"), 1),
            "notes": _clean_str(raw.get("notes")),
            "classification": _clean_classification(raw.get("_classification")),
        })

    return {"defaults": defaults, "sites": sites, "skipped": skipped}
