"""Tests for the remote-catalog scoring additions.

Covers the three mechanisms added for the remote 22-benchmark dataset:
- BENCHMARK_WEIGHTS: per-benchmark trust weights in the quality aggregation
- RANDOM_CHANCE_FLOORS / is_implausible_benchmark_score: load-time filtering
- shrinkage imputation: missing benchmarks blend the model's own level with
  the registry median instead of taking the raw median

Mirrors packages/node/tests/scoring/weightsAndFloors.test.ts.
"""

from __future__ import annotations

from tryaii.catalog.bundle import starter_bundle
from tryaii.registry.models import ModelInfo, ModelPricing
from tryaii.scoring.benchmarks import (
    BENCHMARK_WEIGHTS,
    DEFAULT_BENCHMARK_WEIGHT,
    BenchmarkNormalizer,
    is_implausible_benchmark_score,
)
from tryaii.scoring.engine import IMPUTATION_SHRINKAGE_K, ScoringEngine
from tryaii.scoring.priorities import Priorities

QUALITY_ONLY = Priorities(quality=5, cost=1, speed=1)


def _model(model_id: str, benchmarks: dict[str, float]) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        provider="test",
        benchmark_scores=benchmarks,
        pricing=ModelPricing(input_per_1k=0.001, output_per_1k=0.002),
        latency="fast",
    )


class TestNormalizerWeights:
    def test_known_benchmark_returns_table_weight(self):
        n = BenchmarkNormalizer()
        assert n.get_weight("LiveCodeBench") == BENCHMARK_WEIGHTS["LiveCodeBench"]

    def test_full_bundle_normalizer_carries_the_full_weights(self, full_bundle):
        n = BenchmarkNormalizer.from_bundle(full_bundle)
        weights = full_bundle.benchmark_weights()
        assert len(weights) > len(starter_bundle().benchmark_weights())
        for name, w in weights.items():
            assert n.get_weight(name) == w
        assert n.get_weight("MyCustomBench") == DEFAULT_BENCHMARK_WEIGHT

    def test_unknown_benchmark_defaults_to_neutral(self):
        n = BenchmarkNormalizer()
        assert n.get_weight("MyCustomBench") == DEFAULT_BENCHMARK_WEIGHT

    def test_register_weight_overrides(self):
        n = BenchmarkNormalizer()
        n.register_weight("GPQA", 3.0)
        assert n.get_weight("GPQA") == 3.0

    def test_weights_do_not_leak_across_instances(self):
        a = BenchmarkNormalizer()
        a.register_weight("GPQA", 3.0)
        b = BenchmarkNormalizer()
        assert b.get_weight("GPQA") == BENCHMARK_WEIGHTS["GPQA"]

    def test_weight_tilts_ranking_toward_trusted_benchmark(self):
        """Equal similarity, equal-quality models: the one strong on the
        higher-weight benchmark (HLE 1.4 vs AIME-2024 0.9) wins."""
        engine = ScoringEngine()
        n = BenchmarkNormalizer()
        assert n.get_weight("HLE") > n.get_weight("AIME-2024")
        hle, aime = n.get_range("HLE"), n.get_range("AIME-2024")
        # hle_strong: top of the HLE range, bottom of AIME-2024.
        hle_strong = _model("hle-strong", {"HLE": hle.max_score, "AIME-2024": aime.min_score})
        # aime_strong: the exact mirror image.
        aime_strong = _model("aime-strong", {"HLE": hle.min_score, "AIME-2024": aime.max_score})
        sims = {"HLE": 0.6, "AIME-2024": 0.6}
        scores = engine.score_models([hle_strong, aime_strong], sims, QUALITY_ONLY)
        assert scores[0].model_id == "hle-strong"

    def test_all_neutral_weights_reproduce_similarity_only_quality(self):
        """With every weight forced to 1.0 the quality score must equal the
        plain similarity-weighted average (the pre-weights behaviour)."""
        n = BenchmarkNormalizer()
        for name in list(BENCHMARK_WEIGHTS):
            n.register_weight(name, 1.0)
        engine = ScoringEngine(normalizer=n)
        m = _model("m", {"SWE-bench-verified": 40, "GSM8K": 60})
        sims = {"SWE-bench-verified": 0.8, "GSM8K": 0.4}
        [score] = engine.score_models([m], sims, QUALITY_ONLY, top_k=1)
        expected = (
            0.8 * n.normalize("SWE-bench-verified", 40) + 0.4 * n.normalize("GSM8K", 60)
        ) / (0.8 + 0.4)
        assert abs(score.quality_score - expected) < 1e-4


class TestPlausibilityFloors:
    def test_below_floor_is_implausible(self):
        assert is_implausible_benchmark_score("GPQA", 5.0)
        assert is_implausible_benchmark_score("MMLU-Pro", 4.9)

    def test_floors_come_from_the_catalog(self, full_bundle):
        floors = full_bundle.random_chance_floors()
        # MMLU is a full-catalog benchmark: no floor in the starter tables.
        below = floors["MMLU"] - 0.1
        assert not is_implausible_benchmark_score("MMLU", below)
        assert is_implausible_benchmark_score("MMLU", below, floors)
        # The full catalog keeps every starter floor and adds its own.
        starter_floors = starter_bundle().random_chance_floors()
        assert {k: floors[k] for k in starter_floors} == starter_floors
        assert len(floors) > len(starter_floors)

    def test_at_floor_is_plausible(self):
        assert not is_implausible_benchmark_score("GPQA", 10.0)

    def test_above_floor_is_plausible(self):
        assert not is_implausible_benchmark_score("GPQA", 55.0)

    def test_floorless_benchmark_never_implausible(self):
        # Open-ended benchmarks legitimately score near zero.
        assert not is_implausible_benchmark_score("MATH", 0.5)
        assert not is_implausible_benchmark_score("AIME-2025", 1.0)

    def test_from_dict_drops_implausible_scores(self):
        m = ModelInfo.from_dict(
            {
                "model_id": "x/y",
                "provider": "x",
                "benchmark_scores": {"GPQA": 1.3, "MATH": 90.0, "MMLU": 80.0},
            }
        )
        assert "GPQA" not in m.benchmark_scores
        assert m.benchmark_scores == {"MATH": 90.0, "MMLU": 80.0}

    def test_from_dict_keeps_low_scores_on_floorless_benchmarks(self):
        m = ModelInfo.from_dict(
            {
                "model_id": "x/y",
                "provider": "x",
                "benchmark_scores": {"AIME-2024": 0.7},
            }
        )
        assert m.benchmark_scores == {"AIME-2024": 0.7}

    def test_from_dict_still_drops_nulls(self):
        m = ModelInfo.from_dict(
            {
                "model_id": "x/y",
                "provider": "x",
                "benchmark_scores": {"MATH": None, "MMLU": 70.0},
            }
        )
        assert m.benchmark_scores == {"MMLU": 70.0}


class TestShrinkageImputation:
    def test_model_level_is_median_of_normalized_scores(self):
        engine = ScoringEngine()
        n = BenchmarkNormalizer()
        raw = {"HLE": 10, "GPQA": 80, "AIME-2024": 99}
        m = _model("m", raw)
        level, count = engine._model_level(m)
        assert count == 3
        norms = sorted(n.normalize(name, value) for name, value in raw.items())
        assert norms[0] < norms[1] < norms[2]
        assert abs(level - norms[1]) < 1e-9  # middle value

    def test_model_level_even_count_averages_middle_two(self):
        engine = ScoringEngine()
        n = BenchmarkNormalizer()
        raw = {"MATH": 20, "AIME-2024": 40, "AIME-2025": 60, "MMLU-Pro": 72}
        level, count = engine._model_level(_model("m", raw))
        # Which two benchmarks land in the middle depends on the (generated,
        # catalog-derived) ranges, so take the median of the normalised values
        # rather than naming a pair.
        norms = sorted(n.normalize(name, value) for name, value in raw.items())
        mid = (norms[1] + norms[2]) / 2
        assert count == 4
        assert abs(level - mid) < 1e-9

    def test_model_level_neutral_for_empty(self):
        engine = ScoringEngine()
        level, count = engine._model_level(_model("m", {}))
        assert (level, count) == (0.5, 0)

    def test_strong_model_imputes_above_median(self):
        """A high-coverage strong model must not be flattened to 'average'."""
        engine = ScoringEngine()
        # Registry median for MATH comes from the two weak models: 30.
        weak_a = _model("weak-a", {"MATH": 20})
        weak_b = _model("weak-b", {"MATH": 40})
        # Strong on 6 other benchmarks (norms ~0.9), missing MATH entirely.
        strong = _model(
            "strong",
            {
                "AIME-2024": 90,
                "AIME-2025": 90,
                "MMLU-Pro": 81,
                "LiveCodeBench": 81,
                "SWE-bench-verified": 72,
                "GPQA": 85.5,
            },
        )
        sims = {"MATH": 0.9}
        scores = engine.score_models([weak_a, weak_b, strong], sims, QUALITY_ONLY)
        by_id = {s.model_id: s for s in scores}
        # alpha = 6/9; imputed = 6/9*0.9 + 3/9*norm(MATH,30) = 0.7 > both weak norms.
        assert by_id["strong"].quality_score > by_id["weak-b"].quality_score
        assert "imputed: 1/1" in by_id["strong"].reasoning

    def test_sparse_model_stays_near_median(self):
        """A one-benchmark model can't inflate itself via its own level."""
        engine = ScoringEngine()
        alpha = 1 / (1 + IMPUTATION_SHRINKAGE_K)
        n = BenchmarkNormalizer()
        # Registry: two models carrying MATH (median 50), sparse carries only AIME-2024=100.
        a = _model("a", {"MATH": 40})
        b = _model("b", {"MATH": 60})
        sparse = _model("sparse", {"AIME-2024": 100})
        sims = {"MATH": 1.0}
        scores = engine.score_models([a, b, sparse], sims, QUALITY_ONLY)
        by_id = {s.model_id: s for s in scores}
        expected = alpha * 1.0 + (1 - alpha) * n.normalize("MATH", 50)
        assert abs(by_id["sparse"].quality_score - expected) < 1e-3

    def test_benchmark_with_no_data_anywhere_still_skipped(self):
        """If NO model has a benchmark, there is nothing to impute from --
        the engine keeps the long-standing skip semantic."""
        engine = ScoringEngine()
        m1 = _model("m1", {"MATH": 80})
        m2 = _model("m2", {"MATH": 60})
        sims = {"MATH": 0.9, "HLE": 0.8}  # nobody has HLE
        scores = engine.score_models([m1, m2], sims, QUALITY_ONLY)
        assert {s.model_id for s in scores} == {"m1", "m2"}
        for s in scores:
            assert "imputed" not in s.reasoning


class TestRemoteScaleNormalization:
    """Each family normalises against its own catalog-derived p25..max window.

    The windows move with every catalog sync, so these assert the *shape* --
    floor at lo, ceiling at hi, 0.5 at the midpoint, clamps outside -- against
    the live range rather than against numbers that only held for the old
    hand-written table.
    """

    @staticmethod
    def _assert_window(benchmark: str, n: BenchmarkNormalizer = None) -> None:
        n = n or BenchmarkNormalizer()
        r = n.get_range(benchmark)
        assert r is not None, benchmark
        lo, hi = r.min_score, r.max_score
        assert lo < hi
        span = hi - lo
        assert n.normalize(benchmark, lo) == 0.0
        assert n.normalize(benchmark, hi) == 1.0
        assert abs(n.normalize(benchmark, (lo + hi) / 2) - 0.5) < 1e-9
        assert n.normalize(benchmark, lo - span) == 0.0
        assert n.normalize(benchmark, hi + span) == 1.0

    def test_elo_normalization(self):
        self._assert_window("Chatbot Arena Elo")
        n = BenchmarkNormalizer()
        r = n.get_range("Chatbot Arena Elo")
        # ELO ratings, not percentages -- the headroom window stays in the
        # 1000-1600 band even as the catalog moves.
        assert 1000 < r.min_score < r.max_score < 1600

    def test_elo_clamps_out_of_range(self):
        n = BenchmarkNormalizer()
        assert n.normalize("Chatbot Arena Elo", 900) == 0.0
        assert n.normalize("Chatbot Arena Elo", 1700) == 1.0

    def test_livebench_fraction_normalization(self, full_bundle):
        full = BenchmarkNormalizer.from_bundle(full_bundle)
        for benchmark in ("LiveBench", "LiveBench-Coding"):
            self._assert_window(benchmark, full)
            r = full.get_range(benchmark)
            # Fractions, not percentages.
            assert 0.0 < r.min_score < r.max_score <= 1.0

    def test_scicode_tight_range(self):
        self._assert_window("SciCode")
        n = BenchmarkNormalizer()
        # SciCode's practical ceiling is well under 100; above it clamps to 1.
        assert n.get_range("SciCode").max_score < 100
        assert n.normalize("SciCode", 100) == 1.0

    def test_hle_tight_range(self):
        self._assert_window("HLE")
        n = BenchmarkNormalizer()
        # HLE is very hard: the whole catalog still sits far below the 100 ceiling.
        # The 2026-10 snapshot pushed the catalog max to 61.4, so the old "bottom
        # half" (< 60) phrasing no longer holds; what is worth pinning is that the
        # window stays a small slice of the nominal 0-100 scale.
        assert n.get_range("HLE").max_score < 70
        assert n.normalize("HLE", 100) == 1.0
