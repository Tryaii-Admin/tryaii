"""Provider class, reachability, policy and best-for-less winner
(SPEC.md §2.2–§2.4, §2.8–§2.9).

Pure functions over the daemon's route wire shape. ``scores`` may be
``ModelScore`` objects or the wire dicts the daemon returns
(``{"modelId", "qualityScore", ...}``); both are read through
:func:`score_id` / :func:`score_quality`.

``allowed`` arguments are the data-save policy's allowed catalog ids (any
collection) or None for "no policy" (unconstrained).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from tryaii.diagnose.modelfit import SWAP_QUALITY_EPSILON, _blended_price
from tryaii.diagnose.resolve import resolve_model_id

PROVIDER_OPENAI = "openai"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_OTHER = "other"
PROVIDERS = (PROVIDER_OPENAI, PROVIDER_OPENROUTER, PROVIDER_OTHER)

MODE_OFF = "off"
MODE_STABLE = "stable"
MODE_DYNAMIC = "dynamic"
MODE_MANAGED = "managed"
MODES = (MODE_OFF, MODE_STABLE, MODE_DYNAMIC, MODE_MANAGED)

#: ``route_status`` when a managed model could not be sent (SPEC.md §2.9).
STATUS_UNREACHABLE_MANAGED = "unreachable_managed"


def provider_class(base_url: Optional[str]) -> str:
    """Classify a client's base URL: ``openai`` (api.openai.com or unset),
    ``openrouter`` (openrouter.ai) or ``other`` (any other host, including
    Azure OpenAI and local OpenAI-compatible servers)."""
    if not base_url or not isinstance(base_url, str):
        return PROVIDER_OPENAI
    text = base_url.strip()
    if not text:
        return PROVIDER_OPENAI
    if "//" not in text:
        text = "https://" + text
    host = (urlparse(text).hostname or "").lower()
    if not host:
        return PROVIDER_OTHER
    if host == "openai.com" or host.endswith(".openai.com"):
        return PROVIDER_OPENAI
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return PROVIDER_OPENROUTER
    return PROVIDER_OTHER


def resolve_anchor(model: Optional[str], registry) -> Optional[str]:
    """The catalog id of the developer's model, or None (diagnose §2.1)."""
    model_id, _method = resolve_model_id(model, registry)
    return model_id


def reachable(provider: str, model_id: Optional[str], anchor_id: Optional[str] = None) -> bool:
    """Can the wrapped client send a request for ``model_id``?

    ``openrouter`` reaches the whole catalog; ``openai`` only ``openai/*``
    ids; ``other`` only the anchor itself (never a swap).
    """
    if not model_id:
        return False
    if provider == PROVIDER_OPENROUTER:
        return True
    if provider == PROVIDER_OPENAI:
        return model_id.startswith("openai/")
    return anchor_id is not None and model_id == anchor_id


def allowed_models(provider: str, model_ids,
                   anchor_id: Optional[str] = None) -> Optional[list[str]]:
    """The reachable ids: None for ``openrouter`` (unfiltered), the
    ``openai/*`` ids for ``openai``, and ``[anchor]`` (or ``[]``) for
    ``other``. Order follows ``model_ids``."""
    if provider == PROVIDER_OPENROUTER:
        return None
    return [m for m in model_ids if reachable(provider, m, anchor_id)]


def policy_allowed(policy, origins, model_ids) -> Optional[list[str]]:
    """The catalog ids a data-save ``policy`` allows (SPEC.md §2.8).

    None when the policy is missing or inactive (unconstrained). An active
    policy that cannot be evaluated (no ``origins.json`` in the bundle, an
    unknown provider name) fails CLOSED: ``[]`` -- tryaii then picks
    nothing and the developer's model is sent. Pure: never loads the
    embedding model and never touches the daemon.
    """
    if policy is None or not getattr(policy, "active", False):
        return None
    from tryaii import policy as pol

    try:
        return list(pol.evaluate(policy, origins, model_ids).allowed)
    except pol.PolicyError:
        return []


def filter_ranking(scores, allowed) -> list:
    """``scores`` restricted to ``allowed`` ids, order kept (None = all)."""
    ranked = list(scores or [])
    if allowed is None:
        return ranked
    only = set(allowed)
    return [s for s in ranked if score_id(s) in only]


def route_allowed(provider: str, model_ids, anchor_id: Optional[str] = None,
                  allowed=None) -> Optional[list[str]]:
    """The ``allowedModels`` of the request whose ranking picks the model
    actually SENT: reachable ∩ policy-allowed, in ``model_ids`` order; None
    when both are unconstrained (an ``openrouter`` client, no policy)."""
    reach = allowed_models(provider, model_ids, anchor_id)
    if allowed is None:
        return reach
    only = set(allowed)
    base = list(model_ids) if reach is None else reach
    return [m for m in base if m in only]


def shadow_allowed(model_ids, allowed=None) -> Optional[list[str]]:
    """The ``allowedModels`` of the SHADOW request (the recommendation):
    the policy's ids in ``model_ids`` order, None without a policy."""
    if allowed is None:
        return None
    only = set(allowed)
    return [m for m in model_ids if m in only]


def native_model_id(provider: str, model_id: str) -> str:
    """The id to put in the request for a catalog id: ``openai`` strips the
    namespace (``openai/gpt-5.5`` → ``gpt-5.5``); ``openrouter`` maps through
    ``MODEL_ID_TO_OPENROUTER`` (identity for slugs); ``other`` is unchanged."""
    if provider == PROVIDER_OPENAI:
        return model_id.split("/", 1)[1] if "/" in model_id else model_id
    if provider == PROVIDER_OPENROUTER:
        from tryaii.integrations.openrouter import MODEL_ID_TO_OPENROUTER

        return MODEL_ID_TO_OPENROUTER.get(model_id, model_id)
    return model_id


def score_id(score: Any) -> str:
    if isinstance(score, dict):
        return score.get("modelId") or score.get("model_id") or ""
    return getattr(score, "model_id", "")


def score_quality(score: Any) -> float:
    if isinstance(score, dict):
        value = score.get("qualityScore")
        if value is None:
            value = score.get("quality_score", 0.0)
        return float(value)
    return float(getattr(score, "quality_score", 0.0))


def pick_winner(scores, anchor_id: Optional[str], registry, allowed=None) -> dict:
    """best-for-less over a value-ranked ``scores`` list (SPEC.md §2.3).

    Returns ``{"winner": id|None, "rank": int|None, "method": m}`` where
    ``m`` is ``top`` (no anchor in the ranking → the top model), ``band``
    (the first model with quality ≥ anchor − 0.05 and blended price ≤ the
    anchor's), ``anchor`` (nothing cheaper-and-as-good, or the anchor is
    unpriced), or ``none`` (empty ranking). ``allowed`` (the policy's ids)
    first restricts the ranking, so a disallowed model -- the anchor
    included -- is never the winner; ``rank`` is 1-based in the restricted
    ranking.
    """
    ranked = filter_ranking(scores, allowed)
    if not ranked:
        return {"winner": None, "rank": None, "method": "none"}
    anchor = None
    anchor_rank = None
    if anchor_id:
        for i, s in enumerate(ranked):
            if score_id(s) == anchor_id:
                anchor, anchor_rank = s, i + 1
                break
    if anchor is None:
        return {"winner": score_id(ranked[0]), "rank": 1, "method": "top"}
    cap = _blended_price(registry.get_model(anchor_id))
    if cap is None:
        return {"winner": anchor_id, "rank": anchor_rank, "method": "anchor"}
    floor = score_quality(anchor) - SWAP_QUALITY_EPSILON
    for i, s in enumerate(ranked):
        if score_quality(s) < floor:
            continue
        price = _blended_price(registry.get_model(score_id(s)))
        if price is None or price > cap:
            continue
        if score_id(s) == anchor_id:
            return {"winner": anchor_id, "rank": anchor_rank, "method": "anchor"}
        return {"winner": score_id(s), "rank": i + 1, "method": "band"}
    return {"winner": anchor_id, "rank": anchor_rank, "method": "anchor"}


def routed_model(mode: str, provider: str, anchor_id: Optional[str],
                 winner: Optional[str], pin_model: Optional[str],
                 allowed=None) -> Optional[str]:
    """The catalog id the call should be sent to, or None = keep the
    developer's model as written (SPEC.md §2.4): ``dynamic`` → the winner;
    ``stable`` → the pinned model; ``off`` (and ``managed``, see
    :func:`managed_target`) → None. A target equal to the anchor,
    unreachable, or outside ``allowed`` yields None."""
    target = None
    if mode == MODE_DYNAMIC:
        target = winner
    elif mode == MODE_STABLE:
        target = pin_model
    return sendable(provider, anchor_id, target, allowed)


def sendable(provider: str, anchor_id: Optional[str], target: Optional[str],
             allowed=None) -> Optional[str]:
    """``target`` when it may replace the anchor, else None."""
    if not target or target == anchor_id:
        return None
    if not reachable(provider, target, anchor_id):
        return None
    if allowed is not None and target not in set(allowed):
        return None
    return target


def managed_target(provider: str, anchor_id: Optional[str], model: Optional[str],
                   registry, allowed=None) -> dict:
    """What a ``managed`` call sends (SPEC.md §2.9).

    ``model`` (the dashboard- or ``monitor set``-assigned id) is resolved
    like the anchor (diagnose §2.1). Returns ``{"model", "target",
    "route_status"}``: ``model`` = the resolved catalog id or None;
    ``target`` = the catalog id to send, or None = the anchor;
    ``route_status`` = ``"unreachable_managed"`` when the managed model is
    missing, unknown, unreachable from this client or outside ``allowed``
    (the anchor is sent instead), else None. A managed model equal to the
    anchor is sent as written (target None, no status).
    """
    resolved = None
    if isinstance(model, str) and model.strip():
        resolved = resolve_anchor(model.strip(), registry)
    if resolved is not None and resolved == anchor_id:
        return {"model": resolved, "target": None, "route_status": None}
    target = sendable(provider, anchor_id, resolved, allowed)
    if target is None:
        return {"model": resolved, "target": None,
                "route_status": STATUS_UNREACHABLE_MANAGED}
    return {"model": resolved, "target": target, "route_status": None}
