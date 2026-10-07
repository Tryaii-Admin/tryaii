"""Tests for the scoring engine."""

from tryaii.registry.models import ModelInfo, ModelPricing
from tryaii.scoring.engine import ScoringEngine, quality_tolerance
from tryaii.scoring.priorities import Priorities


def _make_models() -> list[ModelInfo]:
    """Create test models with varying characteristics."""
    return [
        ModelInfo(
            model_id="expensive-good",
            provider="TestProvider",
            benchmark_scores={"LiveCodeBench": 95.0, "MMLU-Pro": 95.0, "AIME-2025": 95.0},
            # Genuinely premium pricing so cost-first routing can actually demote
            # it below the cheap model. With quality normalization fit to the real
            # catalog, a merely-2x price no longer flips the ranking (matches the
            # realistic pricing the Node engine tests already use).
            pricing=ModelPricing(input_per_1k=0.015, output_per_1k=0.075),
            latency="medium",
        ),
        ModelInfo(
            model_id="cheap-fast",
            provider="TestProvider",
            benchmark_scores={"LiveCodeBench": 50.0, "MMLU-Pro": 45.0, "AIME-2025": 55.0},
            pricing=ModelPricing(input_per_1k=0.0001, output_per_1k=0.0004),
            latency="very fast",
        ),
        ModelInfo(
            model_id="balanced-mid",
            provider="TestProvider",
            benchmark_scores={"LiveCodeBench": 75.0, "MMLU-Pro": 72.0, "AIME-2025": 78.0},
            pricing=ModelPricing(input_per_1k=0.002, output_per_1k=0.008),
            latency="fast",
        ),
    ]


def _make_band_models() -> list[ModelInfo]:
    """Three models with *identical* benchmarks, so q' ties and every model is
    inside the band at any priority. Only cost and speed can separate them --
    which is exactly what the in-band ranker is for."""
    benchmarks = {"LiveCodeBench": 92.0, "MMLU-Pro": 88.0, "AIME-2025": 94.0}
    return [
        ModelInfo(
            model_id="pricey-slow",
            provider="TestProvider",
            benchmark_scores=dict(benchmarks),
            pricing=ModelPricing(input_per_1k=0.010, output_per_1k=0.030),
            tokens_per_second=40.0,
            ttft_ms=900.0,
            latency="fast",
        ),
        ModelInfo(
            model_id="mid",
            provider="TestProvider",
            benchmark_scores=dict(benchmarks),
            pricing=ModelPricing(input_per_1k=0.001, output_per_1k=0.003),
            tokens_per_second=150.0,
            ttft_ms=500.0,
            latency="very fast",
        ),
        ModelInfo(
            model_id="cheap-fast",
            provider="TestProvider",
            benchmark_scores=dict(benchmarks),
            pricing=ModelPricing(input_per_1k=0.0001, output_per_1k=0.0003),
            tokens_per_second=600.0,
            ttft_ms=200.0,
            latency="very fast",
        ),
    ]


class TestScoringEngine:
    def setup_method(self):
        self.engine = ScoringEngine()
        self.models = _make_models()

    def test_quality_first_prefers_best_model(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.9, "MMLU-Pro": 0.5},
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        assert scores[0].model_id == "expensive-good"

    def test_cost_first_orders_in_band_models_by_price(self):
        scores = self.engine.score_models(
            _make_band_models(),
            benchmark_similarities={"LiveCodeBench": 0.9, "MMLU-Pro": 0.5},
            priorities=Priorities(quality=1, cost=5, speed=1),
        )
        assert all(s.in_band for s in scores)
        assert [s.model_id for s in scores] == ["cheap-fast", "mid", "pricey-slow"]
        # Cost only: sec is U_c alone, so speed cannot contribute.
        assert all(s.speed_contribution == 0.0 for s in scores)

    def test_speed_first_orders_in_band_models_by_t300(self):
        scores = self.engine.score_models(
            _make_band_models(),
            benchmark_similarities={"LiveCodeBench": 0.5, "MMLU-Pro": 0.5},
            priorities=Priorities(quality=1, cost=1, speed=5),
        )
        assert all(s.in_band for s in scores)
        assert [s.model_id for s in scores] == ["cheap-fast", "mid", "pricey-slow"]
        assert all(s.cost_contribution == 0.0 for s in scores)

    def test_quality_priority_cannot_rescue_an_out_of_band_model(self):
        """At (1,5,1) a far-cheaper model still loses when it is below the band.

        The band is the whole point: cost can only reorder models that already
        cleared the quality bar.
        """
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.9, "MMLU-Pro": 0.5},
            priorities=Priorities(quality=1, cost=5, speed=1),
        )
        best = scores[0]
        assert best.model_id == "expensive-good"
        assert best.in_band
        assert not any(s.in_band for s in scores[1:])

    def test_returns_requested_top_k(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.8},
            top_k=2,
        )
        assert len(scores) == 2

    def test_scores_span_the_full_unit_interval(self):
        """satisficing-v1 dropped the 0.1..0.95 per-call min-max rescale."""
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.8, "MMLU-Pro": 0.6},
        )
        for s in scores:
            assert 0.0 <= s.final_score <= 1.0

    def test_contenders_sit_above_the_half_split(self):
        """final_score >= 0.5 iff the model cleared the quality band."""
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.8, "MMLU-Pro": 0.6},
            priorities=Priorities(quality=3, cost=3, speed=3),
        )
        assert any(s.in_band for s in scores)
        for s in scores:
            if s.in_band:
                assert s.final_score >= 0.5
                assert s.band_base == 0.5
            else:
                assert s.final_score <= 0.5
                assert s.band_base == 0.0

    def test_contributions_sum_to_final_score(self):
        for priorities in (
            Priorities(quality=3, cost=3, speed=3),
            Priorities(quality=5, cost=1, speed=1),
            Priorities(quality=1, cost=5, speed=1),
            Priorities(quality=1, cost=1, speed=5),
        ):
            scores = self.engine.score_models(
                self.models,
                benchmark_similarities={"LiveCodeBench": 0.8, "MMLU-Pro": 0.6},
                priorities=priorities,
            )
            for s in scores:
                total = (
                    s.quality_contribution
                    + s.cost_contribution
                    + s.speed_contribution
                    + s.band_base
                )
                # The invariant is exact before rounding; the public fields are
                # rounded to 4 dp, so four of them can drift by half an ulp each.
                assert abs(total - s.final_score) < 2.5e-4

    def test_band_metadata_matches_the_priority_triple(self):
        priorities = Priorities(quality=3, cost=3, speed=3)
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.8, "MMLU-Pro": 0.6},
            priorities=priorities,
        )
        eps = round(quality_tolerance(priorities), 4)
        best = max(s.quality_score for s in scores)
        for s in scores:
            assert s.quality_tolerance == eps
            assert s.quality_best == best
            assert s.in_band == (s.quality_score >= best - eps - 1e-12)

    def test_strict_quality_final_score_is_quality(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.9, "MMLU-Pro": 0.5},
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        for i, s in enumerate(scores):
            assert s.final_score == s.quality_score
            assert s.quality_tolerance == 0.0
            assert s.band_base == 0.0
            assert s.quality_contribution == s.final_score
            # eps = 0, so the band is the leader alone.
            assert s.in_band == (i == 0)
            assert "quality only (no tolerance)" in s.reasoning
            assert s.reasoning.endswith("at 5/1/1 only quality counts")

    def test_scores_sorted_descending(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.8},
        )
        for i in range(len(scores) - 1):
            assert scores[i].final_score >= scores[i + 1].final_score

    def test_empty_models_returns_empty(self):
        scores = self.engine.score_models(
            [],
            benchmark_similarities={"LiveCodeBench": 0.8},
        )
        assert scores == []

    def test_no_signal_prompt_falls_back_to_neutral(self):
        # A prompt whose similarities are all 0 (a degenerate embedding that is
        # orthogonal/negative to every centroid) matches no benchmark, so every
        # model is signal-less. Instead of returning nothing -- which would make
        # a single route() raise and a budget run report the whole dataset
        # infeasible -- the engine falls back to a neutral quality so the prompt
        # stays routable on cost/speed, flagged in the reasoning so the "no
        # signal" case is observable rather than mistaken for a real judgement.
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.0, "MMLU-Pro": 0.0},
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        assert len(scores) == len(self.models)
        assert all("No benchmark signal" in s.reasoning for s in scores)
        # Quality is neutralised for every model, so cost/speed alone decide
        # the winner -> the cheap model ranks first. Both weights are floored
        # at 0.1, so a priority-1 suppression cannot empty the decision.
        assert scores[0].model_id == "cheap-fast"
        for s in scores:
            assert s.quality_score == 0.5
            assert s.quality_best == 0.5
            assert s.quality_contribution == 0.0
            assert not s.in_band
            assert s.band_base == 0.0
            assert s.quality_tolerance == 0.0
            assert "no benchmark signal" in s.signal_flags
            total = s.cost_contribution + s.speed_contribution
            assert abs(total - s.final_score) < 2.5e-4

    def test_reasoning_populated(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.9},
        )
        for s in scores:
            assert len(s.reasoning) > 0

    def test_score_breakdown_present(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"LiveCodeBench": 0.8, "MMLU-Pro": 0.6},
        )
        for s in scores:
            assert s.quality_score >= 0
            assert s.cost_score >= 0
            assert s.speed_score >= 0
