"""The two log-scaled utilities of ``satisficing-v1`` (cost and speed).

These replace the whole speed-tier machinery (SPEED_SCORES, tier_from_tps,
speed_score, the 0.18 cliff). The reference values below are the ones the
algorithm was calibrated and judged against, and are asserted identically in
the Node SDK -- they are the cross-SDK contract for the utilities.
"""

import math

import pytest

from tryaii.registry.models import ModelInfo, ModelPricing, ModelRegistry
from tryaii.scoring.engine import (
    COST_UNKNOWN_FLAG,
    FALLBACK_MEDIAN_TTFT_MS,
    FALLBACK_P25_SPEED_UTILITY,
    P_HI,
    P_LO,
    SPEED_UNKNOWN_FLAG,
    T_HI,
    T_LO,
    TTFT_ESTIMATED_FLAG,
    RegistryStats,
    cost_utility,
    registry_speed_stats,
    speed_utility,
    speed_utility_from_t300,
    t300_seconds,
)

STATS = RegistryStats(median_ttft_ms=750.0, p25_speed_utility=0.242)


def _model(model_id="m", price_per_m=None, tps=None, ttft_ms=None):
    pricing = None
    if price_per_m is not None:
        half = price_per_m / 1000.0 / 2
        pricing = ModelPricing(input_per_1k=half, output_per_1k=half)
    return ModelInfo(
        model_id=model_id,
        provider="Test",
        pricing=pricing,
        tokens_per_second=tps,
        ttft_ms=ttft_ms,
    )


class TestCostUtility:
    @pytest.mark.parametrize(
        "price_per_m,expected",
        [
            (0.05, 1.00000),  # P_LO anchor
            (0.20, 0.79931334),
            (2.00, 0.46598),
            (5.00, 0.33333333),
            (20.00, 0.13264667),
            (50.00, 0.00000),  # P_HI anchor
        ],
    )
    def test_reference_values(self, price_per_m, expected):
        utility, flag = cost_utility(price_per_m / 1000.0)
        assert flag is None
        assert utility == pytest.approx(expected, abs=5e-9)

    def test_anchors_are_the_documented_prices(self):
        assert P_LO == 0.05
        assert P_HI == 50.0
        assert math.log(P_HI) - math.log(P_LO) == pytest.approx(6.907755, abs=1e-6)

    def test_ten_times_cheaper_is_one_third_of_the_scale(self):
        a, _ = cost_utility(10.0 / 1000.0)
        b, _ = cost_utility(1.0 / 1000.0)
        assert b - a == pytest.approx(1 / 3, abs=1e-9)

    def test_clamps_outside_the_anchors(self):
        assert cost_utility(0.001 / 1000.0)[0] == 1.0
        assert cost_utility(500.0 / 1000.0)[0] == 0.0

    def test_free_is_perfect(self):
        assert cost_utility(0.0) == (1.0, None)

    def test_unknown_price_scores_zero_and_flags(self):
        """Zero, not neutral: an unpriced model must never win on cost."""
        assert cost_utility(None) == (0.0, COST_UNKNOWN_FLAG)
        assert cost_utility(float("nan")) == (0.0, COST_UNKNOWN_FLAG)
        assert cost_utility(float("inf")) == (0.0, COST_UNKNOWN_FLAG)

    def test_distinguishes_prices_the_old_linear_term_could_not(self):
        cheap, _ = cost_utility(0.10 / 1000.0)
        dearer, _ = cost_utility(0.50 / 1000.0)
        assert cheap > dearer
        assert cheap - dearer > 0.2


class TestT300:
    def test_is_ttft_plus_generation(self):
        assert t300_seconds(150.0, 500.0) == pytest.approx(2.5, abs=1e-12)
        assert t300_seconds(50.0, 800.0) == pytest.approx(6.8, abs=1e-12)
        assert t300_seconds(600.0, 200.0) == pytest.approx(0.7, abs=1e-12)
        assert t300_seconds(1000.0, 100.0) == pytest.approx(0.4, abs=1e-12)


class TestSpeedUtility:
    @pytest.mark.parametrize(
        "t300,expected",
        [
            (0.3, 1.00000),  # T_LO anchor
            (0.4, 0.93753),
            (0.7, 0.81601),
            (2.5, 0.53959),
            (6.8, 0.32231),
            (30.0, 0.00000),  # T_HI anchor
        ],
    )
    def test_reference_values(self, t300, expected):
        assert speed_utility_from_t300(t300) == pytest.approx(expected, abs=5e-6)

    def test_anchors_are_the_documented_seconds(self):
        assert T_LO == 0.3
        assert T_HI == 30.0
        assert math.log(T_HI) - math.log(T_LO) == pytest.approx(4.605170, abs=1e-6)

    def test_halving_the_wait_is_a_constant_step(self):
        step = speed_utility_from_t300(2.0) - speed_utility_from_t300(4.0)
        assert step == pytest.approx(0.1505, abs=5e-5)
        other = speed_utility_from_t300(0.75) - speed_utility_from_t300(1.5)
        assert other == pytest.approx(step, abs=1e-9)

    def test_clamps_outside_the_anchors(self):
        assert speed_utility_from_t300(0.05) == 1.0
        assert speed_utility_from_t300(120.0) == 0.0

    def test_measured_model_has_no_flag(self):
        utility, flag = speed_utility(_model(tps=150.0, ttft_ms=500.0), STATS)
        assert flag is None
        assert utility == pytest.approx(0.53959, abs=5e-6)

    def test_missing_ttft_uses_the_catalog_median_and_flags(self):
        utility, flag = speed_utility(_model(tps=150.0), STATS)
        assert flag == TTFT_ESTIMATED_FLAG
        expected = speed_utility_from_t300(
            t300_seconds(150.0, STATS.median_ttft_ms)
        )
        assert utility == pytest.approx(expected, abs=1e-12)

    def test_missing_tps_uses_the_catalog_p25_and_flags(self):
        for model in (_model(), _model(ttft_ms=200.0), _model(tps=0.0)):
            utility, flag = speed_utility(model, STATS)
            assert flag == SPEED_UNKNOWN_FLAG
            assert utility == STATS.p25_speed_utility

    def test_the_p25_fallback_is_pessimistic(self):
        """A model with no measurement must not be able to win on speed.

        p25 sits below the catalog median by construction, so any
        better-than-a-quarter-of-the-catalog model beats it.
        """
        stats = registry_speed_stats(ModelRegistry.default().all_models)
        measured = [
            speed_utility_from_t300(t300_seconds(m.tokens_per_second, m.ttft_ms))
            for m in ModelRegistry.default().all_models
            if m.tokens_per_second and m.ttft_ms is not None
        ]
        measured.sort()
        median = measured[len(measured) // 2]
        assert stats.p25_speed_utility < median
        assert sum(1 for u in measured if u > stats.p25_speed_utility) > 0.7 * len(
            measured
        )


class TestRegistryStats:
    def test_median_and_p25_over_a_hand_built_list(self):
        models = [
            _model("a", tps=1000.0, ttft_ms=100.0),  # T300 0.40  U_s 0.93753
            _model("b", tps=600.0, ttft_ms=200.0),  # T300 0.70  U_s 0.81601
            _model("c", tps=150.0, ttft_ms=500.0),  # T300 2.50  U_s 0.53959
            _model("d", tps=50.0, ttft_ms=800.0),  # T300 6.80  U_s 0.32231
        ]
        stats = registry_speed_stats(models)
        assert stats.median_ttft_ms == 350.0  # (200 + 500) / 2
        # numpy-style linear percentile over the sorted U_s:
        # position 0.25 * 3 = 0.75 between 0.32231 and 0.53959.
        assert stats.p25_speed_utility == pytest.approx(0.48527, abs=5e-6)

    def test_models_without_measurements_are_skipped(self):
        models = [
            _model("a", tps=1000.0, ttft_ms=100.0),
            _model("b", tps=600.0),  # no ttft -> not a stats source
            _model("c", ttft_ms=200.0),  # no tps -> ttft still counts
            _model("d"),
        ]
        stats = registry_speed_stats(models)
        assert stats.median_ttft_ms == 150.0  # median of (100, 200)
        assert stats.p25_speed_utility == pytest.approx(
            speed_utility_from_t300(0.4), abs=1e-12
        )

    def test_empty_candidate_set_uses_the_documented_fallbacks(self):
        stats = registry_speed_stats([])
        assert stats.median_ttft_ms == FALLBACK_MEDIAN_TTFT_MS == 800.0
        assert stats.p25_speed_utility == FALLBACK_P25_SPEED_UTILITY == 0.242

    def test_shipped_catalog_values(self):
        """Sanity window, not a pin: the catalog moves with every refresh.

        The judged harness measured 892 ms / 0.242 on the July snapshot and
        802 ms / 0.243 on the October one (after aggregator ttft rows were
        excluded). Keep the fallbacks (800 ms / 0.242) inside the window so a
        registry with no measurements behaves like a typical one.
        """
        stats = registry_speed_stats(ModelRegistry.default().all_models)
        assert 500.0 <= stats.median_ttft_ms <= 1500.0
        assert 0.15 <= stats.p25_speed_utility <= 0.35

    def test_zero_or_negative_measurements_are_not_sources(self):
        """``> 0``, not merely "present" -- a zeroed row is a missing row."""
        models = [
            _model("a", tps=1000.0, ttft_ms=100.0),
            _model("b", tps=0.0, ttft_ms=0.0),
            _model("c", tps=600.0, ttft_ms=-5.0),
        ]
        stats = registry_speed_stats(models)
        assert stats.median_ttft_ms == 100.0
        assert stats.p25_speed_utility == pytest.approx(
            speed_utility_from_t300(0.4), abs=1e-12
        )

    def test_engine_derives_the_stats_from_the_candidate_set(self):
        """The fallbacks come from the models handed to score_models, and are
        cached per candidate-list identity (both SDKs do exactly this)."""
        from tryaii.scoring.engine import ScoringEngine

        engine = ScoringEngine()
        models = [
            _model("a", price_per_m=1.0, tps=1000.0, ttft_ms=100.0),
            _model("b", price_per_m=1.0, tps=50.0, ttft_ms=800.0),
        ]
        first = engine._speed_stats(models)
        assert engine._speed_stats(models) is first  # same list -> cached
        assert first.median_ttft_ms == 450.0
        assert engine._speed_stats(list(models)) is not first
