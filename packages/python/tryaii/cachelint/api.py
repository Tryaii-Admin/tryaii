"""Top-level API: normalize input(s), run per-item + sequence analysis."""

from __future__ import annotations

from typing import Any

from tryaii.cachelint._jsonutil import normalize_numbers
from tryaii.cachelint.analyzer import analyze_item
from tryaii.cachelint.providers import META
from tryaii.cachelint.sequence import analyze_sequences


def _normalize(data: Any) -> list[dict]:
    """Accept: a single item, a list of items, or {"inputs": [...]}. Prompt may be a bare string."""
    if isinstance(data, dict) and "inputs" in data:
        data = data["inputs"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Input must be an object, a list of objects, or {\"inputs\": [...]}")
    items = []
    for i, raw in enumerate(data):
        if isinstance(raw, str):
            raise ValueError(
                f"input #{i} is a bare string — wrap it: "
                '{"prompt": "...", "llm": {"provider": "...", "name": "..."}}')
        if not isinstance(raw, dict) or "prompt" not in raw:
            raise ValueError(f"input #{i}: expected an object with a 'prompt' field")
        items.append(raw)
    return items


def analyze(data: Any) -> dict:
    """Analyze one or more {prompt, llm:{provider,name}, sent_at?} inputs.

    Returns {"items": [...], "sequences": [...], "meta": {...}} — sequences
    only when the list has 2+ entries. All numbers are normalized per
    SPEC.md §1.3 (integral floats emitted as integers).
    """
    raw_items = _normalize(data)
    analyzed = [analyze_item(item, i) for i, item in enumerate(raw_items)]
    return normalize_numbers({
        "items": [a.data for a in analyzed],
        "sequences": analyze_sequences(analyzed) if len(analyzed) > 1 else [],
        "meta": dict(META),
    })


def analyze_full(data: Any) -> dict:
    """Like analyze(), but each item also carries the canonical text and section
    spans — the shape a renderer that marks findings inline consumes (it needs
    character offsets)."""
    raw_items = _normalize(data)
    analyzed = [analyze_item(item, i) for i, item in enumerate(raw_items)]
    return normalize_numbers({
        "items": [
            {
                "data": a.data,
                "canonical": a.canonical,
                "sections": [{"name": s.name, "start": s.start, "end": s.end}
                             for s in a.sections],
                "sent_at": a.sent_at,
            }
            for a in analyzed
        ],
        "sequences": analyze_sequences(analyzed) if len(analyzed) > 1 else [],
        "meta": dict(META),
    })
