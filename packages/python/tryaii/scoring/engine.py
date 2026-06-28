"""
Dynamic model scoring engine.

Combines benchmark performance, cost, and speed into a single score
weighted by user priorities. This is the heart of the routing logic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from tryaii.registry.models import ModelInfo
from tryaii.scoring.benchmarks import BenchmarkNormalizer
from tryaii.scoring.priorities import DEFAULT_PRIORITIES, Priorities


@dataclass
class ModelScore:
    """Detailed score breakdown for a single model."""

    model_id: str
    final_score: float  # 0-1 combined score
    quality_score: float  # 0-1 benchmark quality
    cost_score: float  # 0-1 (higher = cheaper)
    speed_score: float  # 0-1 (higher = faster)
    quality_contribution: float
    cost_contribution: float
    speed_contribution: float
    top_benchmarks: list[tuple[str, float]]  # Most relevant benchmarks for this model
    reasoning: str  # Human-readable explanation


# Speed tier -> numeric score
SPEED_SCORES: dict[str, float] = {
    "very fast": 1.0,
    "fast": 0.8,
    "medium": 0.6,
    "slow": 0.3,
    "very slow": 0.1,
}


TOP_BENCHMARKS_FOR_SCORING = 5

# Neutral quality used only as a last-resort fallback when a prompt matches no
# benchmark at all (every similarity clamps to 0), so it stays routable on
# cost/speed instead of being dropped. See score_models' neutral_fallback retry.
NEUTRAL_QUALITY_SCORE = 0.5


def _compute_benchmark_medians(
    models: list[ModelInfo],
    benchmark_names: list[str],
) -> dict[str, float]:
    """Compute registry-wide raw-score medians for missing benchmark data."""
    medians: dict[str, float] = {}
    for name in benchmark_names:
        values = [
            score
            for model in models
            if (score := model.benchmark_scores.get(name)) is not None
            and math.isfinite(score)
        ]
        if not values:
            continue
        values.sort()
        mid = len(values) // 2
        if len(values) % 2 == 1:
            medians[name] = values[mid]
        else:
            medians[name] = (values[mid - 1] + values[mid]) / 2
    return medians


class ScoringEngine:
    """
    Scores models against a classified prompt.

    Takes benchmark similarity scores (from the classifier) and user priorities,
    then ranks all available models using a three-factor weighted algorithm:

        final = (quality * qW + cost * cW + speed * sW) / (qW + cW + sW)

    Where weights are derived from user priorities (1-5 scale).
    """

    def __init__(self, normalizer: Optional[BenchmarkNormalizer] = None):
        self._normalizer = normalizer or BenchmarkNormalizer()

    def score_models(
        self,
        models: list[ModelInfo],
        benchmark_similarities: dict[str, float],
        priorities: Priorities = DEFAULT_PRIORITIES,
        top_k: int = 5,
    ) -> list[ModelScore]:
        """
        Score and rank models based on benchmark similarities and priorities.

        Args:
            models: Available models to score.
            benchmark_similarities: Cosine similarity of user prompt to each benchmark
                                    centroid. Keys are benchmark names, values are 0-1.
            priorities: User priority weights.
            top_k: Return top K models.

        Returns:
            Sorted list of ModelScore objects (highest score first).
        """
        top_benchmarks = sorted(
            benchmark_similarities.items(), key=lambda x: x[1], reverse=True
        )[:TOP_BENCHMARKS_FOR_SCORING]
        top_benchmark_dict = dict(top_benchmarks)
        benchmark_medians = _compute_benchmark_medians(
            models,
            [name for name, _ in top_benchmarks],
        )

        scores: list[ModelScore] = []

        for model in models:
            score = self._score_single_model(
                model,
                top_benchmark_dict,
                benchmark_medians,
                priorities,
            )
            if score is not None:
                scores.append(score)

        # Fallback: if NO model scored, the prompt matched no benchmark at all
        # (its embedding is orthogonal/negative to every centroid, so all
        # similarities clamped to 0). Rather than return nothing -- which makes a
        # single route() raise and a budget run report the whole dataset
        # infeasible -- re-score every model on a neutral quality baseline so the
        # prompt stays routable on cost/speed. The per-model skip above still
        # applies in the normal case where only *some* models lack signal.
        if not scores:
            for model in models:
                score = self._score_single_model(
                    model,
                    top_benchmark_dict,
                    benchmark_medians,
                    priorities,
                    neutral_fallback=True,
                )
                if score is not None:
                    scores.append(score)

        # Sort by final score descending. Deterministic secondary keys break
        # ties in favour of real (non-imputed) benchmark coverage, then by
        # ascending modelId in Unicode code point order (not locale-aware).
        scores.sort(
            key=lambda s: (-s.final_score, -len(s.top_benchmarks), s.model_id)
        )

        # Normalize to 0.1-0.95 range (best model ~ 0.95). With a single
        # surviving model there is nothing to rescale against, so surface its
        # own unnormalized weighted score (clamped to [0,1]) instead of forcing
        # it against a hardcoded 0 floor.
        if len(scores) == 1:
            scores[0].final_score = round(max(0.0, min(1.0, scores[0].final_score)), 4)
        elif scores:
            max_raw = scores[0].final_score
            min_raw = scores[-1].final_score

            for s in scores:
                if max_raw == min_raw:
                    s.final_score = 0.5
                else:
                    normalized = (s.final_score - min_raw) / (max_raw - min_raw)
                    s.final_score = round(0.1 + 0.85 * normalized, 4)

        return scores[:top_k]

    def _score_single_model(
        self,
        model: ModelInfo,
        top_benchmarks: dict[str, float],
        benchmark_medians: dict[str, float],
        priorities: Priorities,
        neutral_fallback: bool = False,
    ) -> Optional[ModelScore]:
        """Score a single model against the benchmark similarities.

        When neutral_fallback is True, a model with no usable similarity signal
        is scored on a neutral quality baseline instead of being dropped -- used
        only for the all-models-signal-less case (see score_models).
        """

        # --- Quality score ---
        weighted_quality_sum = 0.0
        total_similarity_weight = 0.0
        imputed_count = 0
        model_top_benchmarks: list[tuple[str, float]] = []

        for benchmark_name, user_similarity in top_benchmarks.items():
            model_bench_score = model.benchmark_scores.get(benchmark_name)
            imputed = False
            # Treat a non-finite raw score (NaN/inf) as missing so it does not
            # poison the weighted quality sum; fall back to the median instead.
            if model_bench_score is None or not math.isfinite(model_bench_score):
                model_bench_score = benchmark_medians.get(benchmark_name)
                if model_bench_score is None:
                    continue
                imputed = True
                imputed_count += 1

            normalized = self._normalizer.normalize(benchmark_name, model_bench_score)
            weighted_quality_sum += user_similarity * normalized
            total_similarity_weight += user_similarity
            if not imputed:
                model_top_benchmarks.append((benchmark_name, normalized))

        # No usable similarity signal: this model shares none of the prompt's
        # relevant benchmarks (even after median imputation). Normally drop it so
        # models with real signal win. But when EVERY model is signal-less,
        # score_models retries with neutral_fallback=True so the prompt stays
        # routable on cost/speed alone -- flagged in the reasoning below so the
        # "no signal" case is observable, not mistaken for a real quality call.
        no_signal = total_similarity_weight == 0
        if no_signal and not neutral_fallback:
            return None

        quality_score = (
            NEUTRAL_QUALITY_SCORE
            if no_signal
            else weighted_quality_sum / total_similarity_weight
        )

        # --- Cost score ---
        cost_score = 0.0
        if model.pricing:
            avg_cost = (model.pricing.input_per_1k + model.pricing.output_per_1k) / 2
            # Normalize against $0.10/1k tokens baseline
            cost_score = max(0.0, 1.0 - (avg_cost / 0.1))

        # --- Speed score ---
        speed_score = 0.0
        if model.latency:
            speed_score = SPEED_SCORES.get(model.latency, 0.3)

        # --- Combine with priority weights ---
        # In the no-signal fallback, cost/speed must still break ties even if the
        # user suppressed them (priority 1 -> weight 0): the fallback's whole
        # point is to keep the prompt "routable on cost/speed". A small floor
        # restores that without touching normal routing (no_signal is False there).
        q_weight = priorities.quality_weight
        c_weight = max(priorities.cost_weight, 0.1) if no_signal else priorities.cost_weight
        s_weight = max(priorities.speed_weight, 0.1) if no_signal else priorities.speed_weight

        q_contrib = quality_score * q_weight
        c_contrib = cost_score * c_weight
        s_contrib = speed_score * s_weight

        total_weight = q_weight + c_weight + s_weight
        final = (q_contrib + c_contrib + s_contrib) / total_weight
        final = max(0.0, min(1.0, final))

        # Generate reasoning
        top_bench_str = ", ".join(
            f"{b} ({s:.0%})" for b, s in model_top_benchmarks[:2]
        )
        if no_signal:
            reasoning = "No benchmark signal -- routed on cost/speed"
        else:
            reasoning = f"Quality: {quality_score:.2f} on [{top_bench_str}]"
            if imputed_count > 0:
                reasoning += f" | imputed: {imputed_count}/{len(top_benchmarks)}"
        if cost_score > 0:
            reasoning += f" | Cost efficiency: {cost_score:.2f}"
        if speed_score > 0:
            reasoning += f" | Speed: {speed_score:.2f} ({model.latency})"

        return ModelScore(
            model_id=model.model_id,
            final_score=final,
            quality_score=round(quality_score, 4),
            cost_score=round(cost_score, 4),
            speed_score=round(speed_score, 4),
            quality_contribution=round(q_contrib, 4),
            cost_contribution=round(c_contrib, 4),
            speed_contribution=round(s_contrib, 4),
            top_benchmarks=model_top_benchmarks,
            reasoning=reasoning,
        )
