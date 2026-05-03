"""
Dynamic model scoring engine.

Combines benchmark performance, cost, and speed into a single score
weighted by user priorities. This is the heart of the routing logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tryaii_dre.registry.models import ModelInfo
from tryaii_dre.scoring.benchmarks import BenchmarkNormalizer
from tryaii_dre.scoring.priorities import DEFAULT_PRIORITIES, Priorities


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
        # TODO: parity with Node -- the TS engine in
        # packages/node/src/scoring/engine.ts uses top-5 (not top-3) and
        # imputes missing benchmark scores with the registry-wide median
        # instead of silently skipping them. Without those changes, Python
        # routing keeps the sparse-data-inflation bug documented in the
        # repo-root CHANGELOG.md (Scoring v2 entry). Mirror when this
        # package becomes a hot path again.
        # Use top 3 most relevant benchmarks for scoring
        top_benchmarks = sorted(
            benchmark_similarities.items(), key=lambda x: x[1], reverse=True
        )[:3]
        top_benchmark_dict = dict(top_benchmarks)

        scores: list[ModelScore] = []

        for model in models:
            score = self._score_single_model(model, top_benchmark_dict, priorities)
            if score is not None:
                scores.append(score)

        # Sort by final score descending
        scores.sort(key=lambda s: s.final_score, reverse=True)

        # Normalize to 0.1-0.95 range (best model ~ 0.95)
        if scores:
            max_raw = scores[0].final_score
            min_raw = scores[-1].final_score if len(scores) > 1 else 0

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
        priorities: Priorities,
    ) -> Optional[ModelScore]:
        """Score a single model against the benchmark similarities."""

        # --- Quality score ---
        weighted_quality_sum = 0.0
        total_similarity_weight = 0.0
        model_top_benchmarks: list[tuple[str, float]] = []

        for benchmark_name, user_similarity in top_benchmarks.items():
            model_bench_score = model.benchmark_scores.get(benchmark_name)
            if model_bench_score is None:
                continue

            normalized = self._normalizer.normalize(benchmark_name, model_bench_score)
            weighted_quality_sum += user_similarity * normalized
            total_similarity_weight += user_similarity
            model_top_benchmarks.append((benchmark_name, normalized))

        if total_similarity_weight == 0:
            return None

        quality_score = weighted_quality_sum / total_similarity_weight

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
        q_weight = priorities.quality_weight
        c_weight = priorities.cost_weight
        s_weight = priorities.speed_weight

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
        reasoning = f"Quality: {quality_score:.2f} on [{top_bench_str}]"
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
