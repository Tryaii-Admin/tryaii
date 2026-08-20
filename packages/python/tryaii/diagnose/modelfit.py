"""model_fit check (SPEC.md §2.2).

Ranks the FULL catalog for the site's classification and reports where the
currently-used model lands. Only rank and the absolute raw dimension scores
are emitted — never the per-set renormalized final_score, which is
meaningless as a cross-set delta.
"""

from __future__ import annotations

from typing import Optional

from tryaii.scoring.engine import ModelScore, ScoringEngine
from tryaii.scoring.priorities import Priorities

TOP_EMITTED = 5
RANK_OK_MAX = 3
RANK_CONSIDER_MAX = 10
SWAP_QUALITY_EPSILON = 0.05  # shared with cost_exposure's swap rule
PRICE_BAND = 0.2  # recommended_same_price: ±20% of the current model's price


def _dims(score: ModelScore) -> dict:
    return {
        "model_id": score.model_id,
        "quality_score": score.quality_score,
        "cost_score": score.cost_score,
        "speed_score": score.speed_score,
    }


def _blended_price(model) -> Optional[float]:
    """The scoring engine's cost basis: mean of input/output per-1k prices."""
    if model is None or model.pricing is None:
        return None
    return (model.pricing.input_per_1k + model.pricing.output_per_1k) / 2


def _same_price_recommendation(
    scores: list[ModelScore],
    resolved_model_id: Optional[str],
    registry,
) -> Optional[dict]:
    """SPEC §2.2.1: the best-RANKED model within ±20% of the current model's
    blended price. The current model is itself a candidate — being the best
    at your price is a positive result. None when there is no band to
    search (current unresolved/unranked/unpriced) — never guessed."""
    if resolved_model_id is None:
        return None
    current_price = _blended_price(registry.get_model(resolved_model_id))
    if current_price is None:
        return None
    low = current_price * (1 - PRICE_BAND)
    high = current_price * (1 + PRICE_BAND)
    for rank0, score in enumerate(scores):
        price = _blended_price(registry.get_model(score.model_id))
        if price is None or price < low or price > high:
            continue
        return {
            "model_id": score.model_id,
            "rank": rank0 + 1,
            "quality_score": score.quality_score,
            "cost_score": score.cost_score,
            "speed_score": score.speed_score,
            "is_current": score.model_id == resolved_model_id,
        }
    return None


def run_model_fit(
    classification: dict,
    resolved_model_id: Optional[str],
    declared_model: Optional[str],
    priorities: Priorities,
    registry,
) -> tuple[dict, dict]:
    """Returns (payload, internal). `internal` carries the full ranking for
    the cost check's swap rule; it is never emitted."""
    engine = ScoringEngine()
    models = registry.all_models
    scores = engine.score_models(
        models, classification["benchmark_similarities"], priorities,
        top_k=len(models))

    rank_of = {s.model_id: i + 1 for i, s in enumerate(scores)}
    ranked_count = len(scores)
    best = scores[0]

    current_score: Optional[ModelScore] = None
    current_rank: Optional[int] = None
    if resolved_model_id is not None and resolved_model_id in rank_of:
        current_rank = rank_of[resolved_model_id]
        current_score = scores[current_rank - 1]

    if current_score is not None:
        if current_rank <= RANK_OK_MAX:
            status, reason = "ok", None
            summary = (f"{resolved_model_id} is a strong fit "
                       f"(rank {current_rank} of {ranked_count})")
        elif current_rank <= RANK_CONSIDER_MAX:
            status, reason = "finding", None
            summary = (f"consider {best.model_id} (current {resolved_model_id} "
                       f"ranks {current_rank} of {ranked_count})")
        else:
            status, reason = "finding", None
            summary = (f"{resolved_model_id} is a poor fit for this prompt "
                       f"(rank {current_rank} of {ranked_count}) — best: {best.model_id}")
    else:
        status = "insufficient_data"
        if resolved_model_id is not None:
            reason = f"model '{resolved_model_id}' has no benchmark signal for this prompt"
        elif declared_model:
            reason = f"unknown model '{declared_model}'"
        else:
            reason = "no model declared"
        summary = f"recommended: {best.model_id} (no current model to compare)"

    def _round4(x: float) -> float:
        return round(x, 4)

    payload = {
        "status": status,
        "reason": reason,
        "summary": summary,
        "classification": {
            "broad_category": classification.get("broad_category"),
            "subcategory": classification.get("subcategory"),
            "confidence": (_round4(classification["confidence"])
                           if "confidence" in classification else None),
        },
        "current_rank": current_rank,
        "catalog_size": ranked_count,
        "current": _dims(current_score) if current_score is not None else None,
        "recommended": {
            **_dims(best),
            "top_benchmarks": [[name, _round4(v)] for name, v in best.top_benchmarks],
            "reasoning": best.reasoning,
        },
        "recommended_same_price": _same_price_recommendation(
            scores, resolved_model_id if current_score is not None else None,
            registry),
        "top": [
            {"rank": i + 1, **_dims(s), "reasoning": s.reasoning}
            for i, s in enumerate(scores[:TOP_EMITTED])
        ],
    }
    internal = {"scores": scores, "current_score": current_score,
                "current_rank": current_rank}
    return payload, internal
