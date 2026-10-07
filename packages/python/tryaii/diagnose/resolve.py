"""Model-id resolution against the routing registry (SPEC.md §2.1).

Deterministic, no fuzzy matching: exact id, then OpenRouter slug, then the
cachelint normalization rule, then (only when all of those find nothing) the
provider-native alias fallback — an unresolved model degrades honestly
downstream instead of guessing.
"""

from __future__ import annotations

import re
from typing import Optional


def _norm(name: str) -> str:
    """The cachelint norm_model rule: trim, lowercase, [ ._/]+ runs -> '-'."""
    return re.sub(r"[ ._/]+", "-", (name or "").strip().lower())


# A provider-native version suffix: Anthropic-style compact dates
# ("-20250929", Vertex "@20250929") or a floating "-latest" alias.
_VERSION_SUFFIX = re.compile(r"(?:[-@]20[0-9]{6}|-latest)$")


def resolve_model_id(model: Optional[str], registry) -> tuple[Optional[str], str]:
    """Resolve a site's model string to a registry id.

    Returns (model_id or None, method) with method one of
    'exact' | 'slug' | 'normalized' | 'alias' | 'none'.
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

    # Last try: compare the text after the final '/' on BOTH sides, so a bare
    # "gpt-4o" finds the provider-prefixed "openai/gpt-4o" and "myorg/gpt-4o"
    # does too. Ambiguity still resolves to nothing.
    tail = _norm(model.rsplit("/", 1)[-1])
    candidates = [mid for mid in registry.model_ids if _norm(mid.rsplit("/", 1)[-1]) == tail]
    if len(candidates) == 1:
        return candidates[0], "normalized"
    if candidates:
        return None, "none"

    # Alias fallback for provider-native API ids the OpenRouter-style catalog
    # doesn't carry ("claude-sonnet-4-5-20250929", "grok-4-latest"). Never
    # reached when an earlier step matched; only ever lands on a model that is
    # in THIS registry.
    raw_tail = model.rsplit("/", 1)[-1].strip()
    # (a) the SDK's provider-native id -> OpenRouter slug table.
    for native_id, slug in MODEL_ID_TO_OPENROUTER.items():
        if _norm(native_id) == tail and registry.get_model(slug) is not None:
            return slug, "alias"
    # (b) drop ONE trailing date / "-latest" suffix and retry the tail rule.
    stripped = _VERSION_SUFFIX.sub("", raw_tail.lower())
    if stripped and stripped != raw_tail.lower():
        base = _norm(stripped)
        candidates = [mid for mid in registry.model_ids
                      if _norm(mid.rsplit("/", 1)[-1]) == base]
        if len(candidates) == 1:
            return candidates[0], "alias"

    return None, "none"
