"""Tests for the scoring engine."""

from tryaii_dre.registry.models import ModelInfo, ModelPricing
from tryaii_dre.scoring.engine import ScoringEngine
from tryaii_dre.scoring.priorities import Priorities


def _make_models() -> list[ModelInfo]:
    """Create test models with varying characteristics."""
    return [
        ModelInfo(
            model_id="expensive-good",
            provider="TestProvider",
            benchmark_scores={"HumanEval": 95.0, "MMLU": 95.0, "GSM8K": 95.0},
            pricing=ModelPricing(input_per_1k=0.01, output_per_1k=0.03),
            latency="medium",
        ),
        ModelInfo(
            model_id="cheap-fast",
            provider="TestProvider",
            benchmark_scores={"HumanEval": 50.0, "MMLU": 45.0, "GSM8K": 55.0},
            pricing=ModelPricing(input_per_1k=0.0001, output_per_1k=0.0004),
            latency="very fast",
        ),
        ModelInfo(
            model_id="balanced-mid",
            provider="TestProvider",
            benchmark_scores={"HumanEval": 75.0, "MMLU": 72.0, "GSM8K": 78.0},
            pricing=ModelPricing(input_per_1k=0.002, output_per_1k=0.008),
            latency="fast",
        ),
    ]


class TestScoringEngine:
    def setup_method(self):
        self.engine = ScoringEngine()
        self.models = _make_models()

    def test_quality_first_prefers_best_model(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.9, "MMLU": 0.5},
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        assert scores[0].model_id == "expensive-good"

    def test_cost_first_boosts_cheaper_models(self):
        scores_cost = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.9, "MMLU": 0.5},
            priorities=Priorities(quality=1, cost=5, speed=1),
        )
        scores_quality = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.9, "MMLU": 0.5},
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        # cheap-fast should rank higher with cost priority than with quality priority
        cheap_rank_cost = next(i for i, s in enumerate(scores_cost) if s.model_id == "cheap-fast")
        cheap_rank_qual = next(
            i for i, s in enumerate(scores_quality) if s.model_id == "cheap-fast"
        )
        assert cheap_rank_cost < cheap_rank_qual

    def test_speed_first_boosts_faster_models(self):
        scores_speed = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.5, "MMLU": 0.5},
            priorities=Priorities(quality=1, cost=1, speed=5),
        )
        scores_quality = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.5, "MMLU": 0.5},
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        # cheap-fast (very fast) should rank higher with speed priority
        fast_rank_speed = next(i for i, s in enumerate(scores_speed) if s.model_id == "cheap-fast")
        fast_rank_qual = next(i for i, s in enumerate(scores_quality) if s.model_id == "cheap-fast")
        assert fast_rank_speed <= fast_rank_qual

    def test_returns_requested_top_k(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.8},
            top_k=2,
        )
        assert len(scores) == 2

    def test_scores_are_normalized(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.8, "MMLU": 0.6},
        )
        for s in scores:
            assert 0.1 <= s.final_score <= 0.95

    def test_scores_sorted_descending(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.8},
        )
        for i in range(len(scores) - 1):
            assert scores[i].final_score >= scores[i + 1].final_score

    def test_empty_models_returns_empty(self):
        scores = self.engine.score_models(
            [],
            benchmark_similarities={"HumanEval": 0.8},
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
            benchmark_similarities={"HumanEval": 0.0, "MMLU": 0.0},
            priorities=Priorities(quality=5, cost=1, speed=1),
        )
        assert len(scores) == len(self.models)
        assert all("No benchmark signal" in s.reasoning for s in scores)
        # Quality is neutralised for every model, so cost/speed decide the
        # winner -> the cheap, very-fast model ranks first.
        assert scores[0].model_id == "cheap-fast"

    def test_reasoning_populated(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.9},
        )
        for s in scores:
            assert len(s.reasoning) > 0

    def test_score_breakdown_present(self):
        scores = self.engine.score_models(
            self.models,
            benchmark_similarities={"HumanEval": 0.8, "MMLU": 0.6},
        )
        for s in scores:
            assert s.quality_score >= 0
            assert s.cost_score >= 0
            assert s.speed_score >= 0
