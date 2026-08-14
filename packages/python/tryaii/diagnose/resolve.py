"""Model-id resolution against the routing registry (SPEC.md §2.1).

Deterministic, no fuzzy matching: exact id, then OpenRouter slug, then the
cachelint normalization rule — an unresolved model degrades honestly
downstream instead of guessing.
"""

from __future__ import annotations

import re
from typing import Optional


def _norm(name: str) -> str:
    """The cachelint norm_model rule: trim, lowercase, [ ._/]+ runs -> '-'."""
    return re.sub(r"[ ._/]+", "-", (name or "").strip().lower())


def resolve_model_id(model: Optional[str], registry) -> tuple[Optional[str], str]:
    """Resolve a site's model string to a registry id.

    Returns (model_id or None, method) with method one of
    'exact' | 'slug' | 'normalized' | 'none'.
    """
    if not model:
        return None, "none"
    if registry.get_model(model) is not None:
        return model, "exact"

    norm = _norm(model)

    # OpenRouter slugs ("anthropic/claude-sonnet-4.5" -> registry id).
    from tryaii.integrations.openrouter import MODEL_ID_TO_OPENROUTER
    for model_id, slug in MODEL_ID_TO_OPENROUTER.items():
        if _norm(slug) == norm and registry.get_model(model_id) is not None:
            return model_id, "slug"

    # Normalized comparison against every registry id; ambiguity resolves to
    # nothing (SPEC.md §2.1 — never guess between two models).
    candidates = [mid for mid in registry.model_ids if _norm(mid) == norm]
    if len(candidates) == 1:
        return candidates[0], "normalized"
    if candidates:
        return None, "none"

    # Last try: the text after the final '/' (provider-prefixed ids).
    if "/" in model:
        tail = _norm(model.rsplit("/", 1)[-1])
        candidates = [mid for mid in registry.model_ids if _norm(mid) == tail]
        if len(candidates) == 1:
            return candidates[0], "normalized"

    return None, "none"
