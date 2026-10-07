"""Generic frontier-quality guards.

These tests pin the property the routing engine must keep as the catalog
evolves: when the user asks for quality above all, the genuinely best models
win -- including models that do not exist yet. No real model names appear in
any assertion; everything is expressed through synthetic dominance or
catalog-wide statistics, so the guards cannot be satisfied by overfitting to
today's leaderboard.

Mirrors packages/node/tests/scoring/frontierQuality.test.ts.
"""

from __future__ import annotations

from tryaii.registry.models import ModelInfo, ModelPricing, ModelRegistry
from tryaii.scoring.benchmarks import NORMALIZATION_RANGES, BenchmarkNormalizer
from tryaii.scoring.engine import ScoringEngine
from tryaii.scoring.priorities import Priorities

QUALITY_ONLY = Priorities(quality=5, cost=1, speed=1)

# Representative task signals (benchmark similarities a classifier would emit).
TASK_SIGNALS = {
    "coding": {
        "Chatbot Arena Elo (Code)": 0.62,
        "LiveCodeBench": 0.60,
        "SWE-bench-verified": 0.58,
        "HumanEval": 0.53,
    },
    "reasoning": {"GPQA": 0.62, "HLE": 0.60, "MMLU-Pro": 0.58, "MMLU": 0.53},
    "chat": {"Chatbot Arena Elo": 0.62, "IFBench": 0.58, "IFEval": 0.53},
    "math": {"AIME-2025": 0.62, "MATH": 0.60, "GSM8K": 0.55},
}


def _champion_all() -> ModelInfo:
    """A synthetic future model strictly at the top of every benchmark scale."""
    return ModelInfo(
        model_id="future/champion",
        provider="future",
        benchmark_scores={b: r.max_score for b, r in NORMALIZATION_RANGES.items()},
        pricing=ModelPricing(input_per_1k=0.002, output_per_1k=0.008),
        latency="fast",
    )


class TestFutureModelsWin:
    """If a strictly better model ships tomorrow, quality-first routing must
    pick it -- this is the anti-overfitting contract. Runs on the packaged
    starter catalog; the last test repeats the core check on the full one."""

    def test_dominant_new_model_ranks_first_on_the_full_catalog(self, full_bundle):
        from tryaii.scoring.benchmarks import ranges_from_bundle

        registry = ModelRegistry.from_bundle(full_bundle)
        engine = ScoringEngine(normalizer=BenchmarkNormalizer.from_bundle(full_bundle))
        champion = ModelInfo(
            model_id="future/champion",
            provider="future",
            benchmark_scores={
                b: r.max_score for b, r in ranges_from_bundle(full_bundle).items()
            },
            pricing=ModelPricing(input_per_1k=0.002, output_per_1k=0.008),
            latency="fast",
        )
        models = registry.all_models + [champion]
        for task, sims in TASK_SIGNALS.items():
            top = engine.score_models(
                models, sims, QUALITY_ONLY, top_k=1,
                benchmark_coverage=registry.benchmark_coverage(),
            )[0]
            assert top.model_id == "future/champion", (
                f"dominant model lost the {task} signal to {top.model_id}"
            )

    def test_dominant_new_model_ranks_first_on_every_task_signal(self):
        registry = ModelRegistry.default()
        engine = ScoringEngine()
        models = registry.all_models + [_champion_all()]
        for task, sims in TASK_SIGNALS.items():
            top = engine.score_models(models, sims, QUALITY_ONLY, top_k=1)[0]
            assert top.model_id == "future/champion", (
                f"dominant model lost the {task} signal to {top.model_id}"
            )

    def test_sparse_but_stellar_new_model_wins_its_covered_signals(self):
        """New frontier models arrive with few-but-modern benchmark rows
        (Arena, GPQA, coding harnesses). On prompts matching those benchmarks
        they must win outright; shrinkage imputation must not flatten them."""
        covered = [
            "Chatbot Arena Elo",
            "Chatbot Arena Elo (Code)",
            "GPQA",
            "HLE",
            "LiveCodeBench",
            "MMLU-Pro",
            "Tau2-bench",
            "IFBench",
        ]
        sparse_champion = ModelInfo(
            model_id="future/sparse-champion",
            provider="future",
            benchmark_scores={b: NORMALIZATION_RANGES[b].max_score for b in covered},
            pricing=ModelPricing(input_per_1k=0.002, output_per_1k=0.008),
            latency="fast",
        )
        registry = ModelRegistry.default()
        engine = ScoringEngine()
        models = registry.all_models + [sparse_champion]
        sims = {"Chatbot Arena Elo (Code)": 0.62, "LiveCodeBench": 0.60, "GPQA": 0.55}
        top = engine.score_models(models, sims, QUALITY_ONLY, top_k=1)[0]
        assert top.model_id == "future/sparse-champion"

    def test_dominant_new_model_beats_catalog_even_when_expensive_and_slow(self):
        """Quality-only means quality-only: cost/speed penalties must not
        keep a strictly better model off the top slot at (5, 1, 1)."""
        champion = _champion_all()
        expensive = ModelInfo(
            model_id=champion.model_id,
            provider=champion.provider,
            benchmark_scores=champion.benchmark_scores,
            pricing=ModelPricing(input_per_1k=0.05, output_per_1k=0.25),
            latency="very slow",
        )
        registry = ModelRegistry.default()
        engine = ScoringEngine()
        top = engine.score_models(
            registry.all_models + [expensive],
            TASK_SIGNALS["reasoning"],
            QUALITY_ONLY,
            top_k=1,
        )[0]
        assert top.model_id == "future/champion"


class TestCatalogQualityCalibration:
    """Catalog-wide statistical guards against the version-mixing failure mode
    (a frontier model poisoned by scores from a harder benchmark variant)."""

    @staticmethod
    def _levels() -> dict[str, float]:
        registry = ModelRegistry.default()
        n = BenchmarkNormalizer()
        levels: dict[str, float] = {}
        for m in registry.all_models:
            vals = sorted(n.normalize(b, v) for b, v in m.benchmark_scores.items())
            mid = len(vals) // 2
            levels[m.model_id] = (
                vals[mid] if len(vals) % 2 == 1 else (vals[mid - 1] + vals[mid]) / 2
            )
        return levels

    def test_arena_top_models_have_healthy_benchmark_levels(self):
        """The Arena Elo leaderboard is the cross-generation ground truth for
        'who is at the frontier'. If the catalog's other benchmark scores said
        those models were mediocre (median normalized level in the bottom
        30%), the data would be poisoned the way version-mixed MATH/GSM8K rows
        once poisoned the newest models."""
        registry = ModelRegistry.default()
        levels = self._levels()
        by_arena = sorted(
            (m.benchmark_scores.get("Chatbot Arena Elo"), m.model_id)
            for m in registry.all_models
            if m.benchmark_scores.get("Chatbot Arena Elo") is not None
        )
        top10 = [mid for _, mid in by_arena[-10:]]
        top10_levels = sorted(levels[mid] for mid in top10)
        top10_median = top10_levels[len(top10_levels) // 2]

        all_levels = sorted(levels.values())
        p70 = all_levels[int(0.7 * len(all_levels))]
        assert top10_median >= p70, (
            f"Arena top-10 models score like mid-catalog models "
            f"(median level {top10_median:.3f} < p70 {p70:.3f}) -- "
            f"benchmark data looks version-poisoned"
        )

    def test_no_model_mixes_top_arena_with_bottom_accuracy_scores(self):
        """A model in the Arena top decile must not be *mostly* at the floor.

        The version-mixing signature (a harder-variant score filed under a
        common benchmark name, e.g. the MATH=5.2 rows that once buried
        gpt-5.5) is a frontier model whose scores collapse across the board.

        A single floored benchmark is no longer evidence of that: the ranges are
        now the catalog's own p25..max headroom window, so by construction a
        quarter of all real scores normalise at or near 0 and even an Arena
        leader is below p25 on *some* benchmark. What stays diagnostic is the
        share: more than a third of a top-decile model's scores at the floor
        means the rows are not measuring the same thing.
        """
        registry = ModelRegistry.default()
        n = BenchmarkNormalizer()
        # Sort on the score ONLY: two models can share an Arena Elo exactly, and
        # a bare tuple sort then falls through to comparing ModelInfo, which is
        # not orderable (TypeError).
        by_arena = sorted(
            (
                (m.benchmark_scores.get("Chatbot Arena Elo"), m)
                for m in registry.all_models
                if m.benchmark_scores.get("Chatbot Arena Elo") is not None
            ),
            key=lambda pair: pair[0],
        )
        decile = max(1, len(by_arena) // 10)
        suspicious: list[tuple[str, float]] = []
        for _, m in by_arena[-decile:]:
            norms = [n.normalize(b, v) for b, v in m.benchmark_scores.items()]
            if not norms:
                continue
            floored = sum(1 for x in norms if x < 0.05) / len(norms)
            if floored > 1 / 3:
                suspicious.append((m.model_id, round(floored, 3)))
        assert suspicious == [], (
            f"top-decile Arena models are mostly at the normalization floor "
            f"(version-mixing signature): {suspicious[:5]}"
        )
