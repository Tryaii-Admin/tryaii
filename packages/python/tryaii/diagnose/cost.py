"""cost_exposure check (SPEC.md §2.4).

Pure arithmetic over the budget-module primitives; every number the check
cannot ground in the inventory or the other checks is itemized as
insufficient — never blended or guessed. Cache savings are an upper bound
built on conservative read-discount factors (shared/diagnose/costmodel.json).
"""

from __future__ import annotations

from typing import Any, Optional

from tryaii.budget import estimate_generation_cost, estimate_tokens

DAYS_PER_MONTH = 30
SWAP_QUALITY_EPSILON = 0.05
_CACHEABLE_VERDICTS = ("CACHEABLE", "CACHEABLE_WITH_ACTION", "CACHEABLE_PREFIX")


def _round4(x: float) -> float:
    return round(x, 4)


def _shell(reason: str, output_tokens: int,
           input_tokens: Optional[int] = None,
           token_method: Optional[str] = None) -> dict:
    return {
        "status": "insufficient_data",
        "reason": reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "token_method": token_method,
        "cost_per_call_usd": None,
        "monthly": None,
        "swap": None,
    }


def run_cost(
    *,
    canonical_text: Optional[str],
    resolved_model_id: Optional[str],
    declared_model: Optional[str],
    registry,
    output_tokens: int,
    calls_per_day: Optional[float],
    cache_ctx: Optional[dict],
    fit_internal: Optional[dict],
    read_discount_factors: dict,
) -> dict:
    """cache_ctx: {verdict_code, stable_tokens, total_tokens, provider_key,
    upstream} extracted from the raw cachelint item (None when the cache
    check did not run). fit_internal: the model_fit internals (None when it
    did not run)."""

    # --- input tokens -----------------------------------------------------
    if cache_ctx is not None:
        input_tokens, token_method = cache_ctx["total_tokens"], "tokenizer"
    elif canonical_text is not None:
        input_tokens, token_method = estimate_tokens(canonical_text), "chars/4"
    else:
        return _shell("no prompt", output_tokens)

    # --- per-call cost ----------------------------------------------------
    if resolved_model_id is None:
        reason = (f"unknown model '{declared_model}'" if declared_model
                  else "no model declared")
        return _shell(reason, output_tokens, input_tokens, token_method)
    model = registry.get_model(resolved_model_id)
    cost_per_call = estimate_generation_cost(model, input_tokens, output_tokens)
    if cost_per_call is None:
        return _shell(f"no pricing for '{resolved_model_id}'",
                      output_tokens, input_tokens, token_method)
    cost_per_call = _round4(cost_per_call)

    # --- monthly ----------------------------------------------------------
    monthly: dict[str, Any]
    if calls_per_day is not None:
        monthly = {
            "status": "ok",
            "calls_per_day": calls_per_day,
            "cost_usd": _round4(cost_per_call * calls_per_day * DAYS_PER_MONTH),
        }
        savings = _cache_savings(cache_ctx, model, calls_per_day,
                                 read_discount_factors)
        if savings is not None:
            monthly["cache_savings_usd"] = savings
    else:
        monthly = {"status": "insufficient_data", "reason": "no traffic estimate"}

    # --- swap -------------------------------------------------------------
    swap = _swap(fit_internal, registry, input_tokens, output_tokens,
                 cost_per_call, calls_per_day)

    return {
        "status": "finding" if swap is not None else "ok",
        "reason": None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "token_method": token_method,
        "cost_per_call_usd": cost_per_call,
        "monthly": monthly,
        "swap": swap,
    }


def _cache_savings(cache_ctx: Optional[dict], model, calls_per_day: float,
                   factors: dict) -> Optional[float]:
    """Upper-bound monthly savings from prompt caching (SPEC.md §2.4)."""
    if cache_ctx is None or cache_ctx["verdict_code"] not in _CACHEABLE_VERDICTS:
        return None
    key = cache_ctx["provider_key"]
    factor = factors.get(key)
    if factor is None and key == "openrouter":
        factor = factors.get(cache_ctx.get("upstream") or "")
    if factor is None or not model or not model.pricing:
        return None
    return _round4(cache_ctx["stable_tokens"] / 1000
                   * model.pricing.input_per_1k
                   * factor * calls_per_day * DAYS_PER_MONTH)


def _swap(fit_internal: Optional[dict], registry, input_tokens: int,
          output_tokens: int, current_cost: float,
          calls_per_day: Optional[float]) -> Optional[dict]:
    """Cheapest ranked model within the quality tolerance (SPEC.md §2.4)."""
    if fit_internal is None or fit_internal.get("current_score") is None:
        return None
    floor = fit_internal["current_score"].quality_score - SWAP_QUALITY_EPSILON
    best: Optional[tuple[float, int, str]] = None  # (cost, rank, model_id)
    for rank0, score in enumerate(fit_internal["scores"]):
        if score.quality_score < floor:
            continue
        candidate = registry.get_model(score.model_id)
        cost = estimate_generation_cost(candidate, input_tokens, output_tokens)
        if cost is None:
            continue
        cost = _round4(cost)
        if best is None or cost < best[0]:  # ties break on rank (first wins)
            best = (cost, rank0 + 1, score.model_id)
    if best is None or best[0] >= current_cost:
        return None
    swap: dict[str, Any] = {"model_id": best[2], "cost_per_call_usd": best[0]}
    if calls_per_day is not None:
        swap["monthly_savings_usd"] = _round4(
            (current_cost - best[0]) * calls_per_day * DAYS_PER_MONTH)
    return swap
