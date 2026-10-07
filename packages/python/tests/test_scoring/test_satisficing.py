"""The five hand-computable reference candidates of ``satisficing-v1``.

This is the cross-SDK contract: the same five candidates, the same ``eps``, the
same ``final_score`` to 4 dp and the same order are asserted in
``packages/node/tests/scoring/satisficing.test.ts``. Every number here is
hand-checkable with a calculator from the formulas in
``docs/sdk/routing/scoring.md``.

The candidates bypass the benchmark pipeline: ``q'`` is supplied directly, so a
catalog sync cannot move these expectations.
"""

import pytest

from tryaii.registry.models import ModelInfo, ModelPricing
from tryaii.scoring.engine import (
    SPEED_UNKNOWN_FLAG,
    RegistryStats,
    ScoringEngine,
    _Candidate,
    _speed_parts,
    cost_utility,
    quality_tolerance,
    satisficing_combine,
)
from tryaii.scoring.priorities import Priorities

# The §9 reference table was computed against these fallbacks; the engine
# normally derives them from the candidate set, so they are pinned here to keep
# the hand-computable expectations stable.
STATS = RegistryStats(median_ttft_ms=750.0, p25_speed_utility=0.242)

#            id      q'      $/M     tps     ttft_ms
REFERENCE = [
    ("A-frontier", 1.000, 20.00, 50.0, 800.0),
    ("B-balanced", 0.950, 2.00, 150.0, 500.0),
    ("C-cheap-fast", 0.880, 0.20, 600.0, 200.0),
    ("D-budget", 0.700, 0.05, 1000.0, 100.0),
    ("E-no-tps", 0.960, 5.00, None, None),
]

# Unquantised utilities (the reasoning string shows these at 4 dp).
EXPECTED_UTILITIES = {
    "A-frontier": (0.13265, 0.32231),
    "B-balanced": (0.46598, 0.53959),
    "C-cheap-fast": (0.79931, 0.81601),
    "D-budget": (1.00000, 0.93753),
    "E-no-tps": (0.33333, 0.24200),
}

# The ranker compares the 3-dp quantised values (banker's rounding).
EXPECTED_QUANTISED = {
    "A-frontier": (0.133, 0.322),
    "B-balanced": (0.466, 0.540),
    "C-cheap-fast": (0.799, 0.816),
    "D-budget": (1.000, 0.938),
    "E-no-tps": (0.333, 0.242),
}


def _candidates(n_real=5, quality_overrides=None):
    out = []
    for model_id, quality, price_per_m, tps, ttft in REFERENCE:
        half = price_per_m / 1000.0 / 2
        model = ModelInfo(
            model_id=model_id,
            provider="Test",
            pricing=ModelPricing(input_per_1k=half, output_per_1k=half),
            tokens_per_second=tps,
            ttft_ms=ttft,
        )
        u_cost, cost_flag = cost_utility(price_per_m / 1000.0)
        u_speed, speed_flag, t300 = _speed_parts(model, STATS)
        if quality_overrides and model_id in quality_overrides:
            quality = quality_overrides[model_id]
        out.append(
            _Candidate(
                model=model,
                quality=quality,
                n_real=n_real,
                n_top=5,
                imputed=5 - n_real,
                top_benchmarks=[(f"B{i}", 0.5) for i in range(n_real)],
                cost_utility=u_cost,
                speed_utility=u_speed,
                t300=t300,
                price_per_m=price_per_m,
                flags=[f for f in (cost_flag, speed_flag) if f],
                no_signal=False,
            )
        )
    return out


def _run(priorities, candidates=None):
    scores = ScoringEngine()._score_banded(
        candidates if candidates is not None else _candidates(), priorities
    )
    return scores, {s.model_id: s for s in scores}


class TestSatisficingCombine:
    """The decision rule as a pure function -- the smallest cross-SDK surface."""

    def test_in_band_shape(self):
        r = satisficing_combine(0.88, 0.799, 0.816, 1.0, 0.136, 0.5, 0.5)
        assert r.in_band is True
        assert r.band_base == 0.5
        assert r.sec == pytest.approx(0.8075, abs=1e-12)
        assert r.utility == pytest.approx(1.8075, abs=1e-12)
        assert r.final_score == pytest.approx(0.90375, abs=1e-12)
        assert r.quality_contribution == 0.0
        assert r.cost_contribution + r.speed_contribution + r.band_base == (
            pytest.approx(r.final_score, abs=1e-12)
        )

    def test_out_of_band_shape(self):
        r = satisficing_combine(0.70, 1.0, 0.938, 1.0, 0.136, 0.5, 0.5)
        assert r.in_band is False
        assert r.band_base == 0.0
        assert r.sec is None
        assert r.utility == 0.70
        assert r.final_score == pytest.approx(0.35, abs=1e-12)
        assert r.quality_contribution == pytest.approx(0.35, abs=1e-12)

    def test_strict_quality_shape(self):
        r = satisficing_combine(0.96, 0.333, 0.242, 1.0, 0.0, 0.0, 0.0)
        assert r.final_score == 0.96
        assert r.utility == 0.96
        assert r.quality_contribution == 0.96
        assert r.band_base == 0.0
        assert r.sec is None
        # eps = 0: only the q' leader is nominally in band, and it carries no
        # weight because the secondary term is off.
        assert r.in_band is False
        assert satisficing_combine(1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0).in_band is True

    def test_no_contender_can_be_outranked_by_a_non_contender(self):
        best = satisficing_combine(0.90, 0.0, 0.0, 1.0, 0.136, 0.5, 0.5)
        worst = satisficing_combine(0.99, 1.0, 1.0, 1.0, 0.0, 0.5, 0.5)
        assert best.utility >= 1.0
        assert worst.utility <= 1.0 or worst.in_band


class TestReferenceUtilities:
    def test_utilities_match_the_reference_table(self):
        for c in _candidates():
            u_cost, u_speed = EXPECTED_UTILITIES[c.model.model_id]
            assert c.cost_utility == pytest.approx(u_cost, abs=5e-6)
            assert c.speed_utility == pytest.approx(u_speed, abs=5e-6)

    def test_quantised_utilities_match_the_reference_table(self):
        for c in _candidates():
            cost_q, speed_q = EXPECTED_QUANTISED[c.model.model_id]
            assert round(c.cost_utility, 3) == cost_q
            assert round(c.speed_utility, 3) == speed_q

    def test_the_no_tps_candidate_is_flagged(self):
        e = next(c for c in _candidates() if c.model.model_id == "E-no-tps")
        assert e.flags == [SPEED_UNKNOWN_FLAG]
        assert e.t300 is None


class TestEps:
    @pytest.mark.parametrize(
        "priorities,eps",
        [
            ((5, 1, 1), 0.0000),
            ((3, 3, 3), 0.1360),
            ((4, 3, 3), 0.1020),
            ((2, 3, 3), 0.2040),
            ((1, 5, 1), 0.4080),
            ((1, 1, 5), 0.4080),
            ((2, 5, 3), 0.3060),
            # NOTE: the spec table printed 0.2040 for (2,3,5); eps is symmetric
            # in cost/speed by construction, so it is 0.3060 like (2,5,3).
            ((2, 3, 5), 0.3060),
            ((3, 5, 5), 0.2720),
        ],
    )
    def test_eps_table(self, priorities, eps):
        assert quality_tolerance(Priorities(*priorities)) == pytest.approx(
            eps, abs=5e-5
        )

    def test_eps_is_zero_exactly_when_the_secondary_term_is_off(self):
        for quality in range(1, 6):
            p = Priorities(quality=quality, cost=1, speed=1)
            assert quality_tolerance(p) == 0.0
            assert p.cost_weight + p.speed_weight == 0.0

    def test_eps_is_linear_in_the_cost_plus_speed_excess(self):
        base = quality_tolerance(Priorities(3, 2, 1))
        for cost, speed in ((2, 1), (1, 2), (3, 1), (1, 3), (2, 2)):
            p = Priorities(3, cost, speed)
            excess = (cost - 1) + (speed - 1)
            assert quality_tolerance(p) == pytest.approx(base * excess, abs=1e-12)


class TestBalanced333:
    """eps = 0.1360, band q' >= 0.8640, wc = ws = 0.5. D is out of band."""

    EXPECTED_SEC = {
        "C-cheap-fast": 0.80750,
        "B-balanced": 0.50300,
        "E-no-tps": 0.28750,
        "A-frontier": 0.22750,
    }
    EXPECTED_FINAL = {
        "C-cheap-fast": 0.9038,
        "B-balanced": 0.7515,
        "E-no-tps": 0.6438,
        "A-frontier": 0.6138,
        "D-budget": 0.3500,
    }

    def test_order(self):
        scores, _ = _run(Priorities(3, 3, 3))
        assert [s.model_id for s in scores] == [
            "C-cheap-fast",
            "B-balanced",
            "E-no-tps",
            "A-frontier",
            "D-budget",
        ]

    def test_final_scores(self):
        _, by_id = _run(Priorities(3, 3, 3))
        for model_id, final in self.EXPECTED_FINAL.items():
            assert by_id[model_id].final_score == final

    def test_sec_to_five_decimals(self):
        """``sec`` is the mean of the two 3-dp quantised utilities at (3,3,3).

        Asserted to 5 dp as well as through ``final_score``, because C's
        pre-rounding final is 0.90375 -- a half-even knife edge where a
        formatting disagreement between the SDKs would otherwise hide.
        """
        _, by_id = _run(Priorities(3, 3, 3))
        for model_id, sec in self.EXPECTED_SEC.items():
            cost_q, speed_q = EXPECTED_QUANTISED[model_id]
            assert round((0.5 * cost_q + 0.5 * speed_q) / 1.0, 5) == sec
            assert by_id[model_id].final_score == round(0.5 + 0.5 * sec, 4)
        assert by_id["C-cheap-fast"].final_score == 0.9038

    def test_band_membership(self):
        _, by_id = _run(Priorities(3, 3, 3))
        for model_id, s in by_id.items():
            assert s.in_band == (model_id != "D-budget")
            assert s.quality_tolerance == 0.136
            assert s.quality_best == 1.0
        assert by_id["D-budget"].final_score < 0.5
        assert all(
            s.final_score >= 0.5 for mid, s in by_id.items() if mid != "D-budget"
        )

    def test_the_expensive_frontier_model_is_last_among_contenders(self):
        scores, _ = _run(Priorities(3, 3, 3))
        contenders = [s.model_id for s in scores if s.in_band]
        assert contenders[-1] == "A-frontier"

    def test_contributions_sum_to_final(self):
        scores, _ = _run(Priorities(3, 3, 3))
        for s in scores:
            total = (
                s.quality_contribution
                + s.cost_contribution
                + s.speed_contribution
                + s.band_base
            )
            assert abs(total - s.final_score) < 2.5e-4

    def test_reasoning_format(self):
        _, by_id = _run(Priorities(3, 3, 3))
        assert by_id["B-balanced"].reasoning == (
            "q'=0.95 (5 real of 5) | cost 0.4660 ($2.00/M) "
            "| speed 0.5396 (2.50 s to 300 tok) | within 0.136 of the best (1.00) "
            "| at 3/3/3 you accept up to 0.136 less quality for a cheaper or "
            "faster model; this pick gave up 0.12 vs A-frontier"
        )
        assert by_id["E-no-tps"].reasoning == (
            "q'=0.96 (5 real of 5) | cost 0.3333 ($5.00/M) "
            "| speed unknown (catalog p25) | within 0.136 of the best (1.00) "
            "| at 3/3/3 you accept up to 0.136 less quality for a cheaper or "
            "faster model; this pick gave up 0.12 vs A-frontier"
        )
        assert by_id["D-budget"].reasoning == (
            "q'=0.70 (5 real of 5) | cost 1.0000 ($0.05/M) "
            "| speed 0.9375 (0.40 s to 300 tok) "
            "| 0.300 below the best (1.00) -- outside the 0.136 tolerance "
            "| at 3/3/3 you accept up to 0.136 less quality for a cheaper or "
            "faster model; this pick gave up 0.12 vs A-frontier"
        )


class TestStrictQuality511:
    """eps = 0, wc + ws = 0: final_score = q' for everyone, no band."""

    def test_order_and_scores(self):
        scores, by_id = _run(Priorities(5, 1, 1))
        assert [s.model_id for s in scores] == [
            "A-frontier",
            "E-no-tps",
            "B-balanced",
            "C-cheap-fast",
            "D-budget",
        ]
        assert [s.final_score for s in scores] == [1.0, 0.96, 0.95, 0.88, 0.70]
        for i, s in enumerate(scores):
            assert s.final_score == s.quality_score
            # eps = 0, so the band degenerates to the q' leader alone -- and it
            # carries no weight anyway, since the secondary term is off.
            assert s.in_band == (i == 0)
            assert s.band_base == 0.0
            assert s.quality_contribution == s.final_score
            assert s.cost_contribution == 0.0
            assert s.speed_contribution == 0.0

    def test_reasoning_has_no_exchange_rate(self):
        _, by_id = _run(Priorities(5, 1, 1))
        assert by_id["A-frontier"].reasoning == (
            "q'=1.00 (5 real of 5) | cost 0.1326 ($20.00/M) "
            "| speed 0.3223 (6.80 s to 300 tok) | quality only (no tolerance) "
            "| at 5/1/1 only quality counts"
        )


class TestCostOnly151:
    """eps = 0.4080, all five in band, wc = 1.0, ws = 0: sec = U_c_q."""

    def test_order_and_scores(self):
        scores, _ = _run(Priorities(1, 5, 1))
        assert [s.model_id for s in scores] == [
            "D-budget",
            "C-cheap-fast",
            "B-balanced",
            "E-no-tps",
            "A-frontier",
        ]
        assert [s.final_score for s in scores] == [
            1.0000,
            0.8995,
            0.7330,
            0.6665,
            0.5665,
        ]
        assert all(s.in_band for s in scores)
        assert all(s.speed_contribution == 0.0 for s in scores)

    def test_the_model_333_rejected_now_wins(self):
        scores, _ = _run(Priorities(1, 5, 1))
        assert scores[0].model_id == "D-budget"


class TestSpeedOnly115:
    """eps = 0.4080, all five in band, wc = 0, ws = 1.0: sec = U_s_q."""

    def test_order_and_scores(self):
        scores, _ = _run(Priorities(1, 1, 5))
        assert [s.model_id for s in scores] == [
            "D-budget",
            "C-cheap-fast",
            "B-balanced",
            "A-frontier",
            "E-no-tps",
        ]
        assert [s.final_score for s in scores] == [
            0.9690,
            0.9080,
            0.7700,
            0.6610,
            0.6210,
        ]
        assert all(s.cost_contribution == 0.0 for s in scores)

    def test_missing_evidence_never_wins_a_speed_route(self):
        """E has no measured throughput, so the p25 fallback puts it last."""
        scores, _ = _run(Priorities(1, 1, 5))
        assert scores[-1].model_id == "E-no-tps"
        assert SPEED_UNKNOWN_FLAG in scores[-1].signal_flags


class TestTieBreaks:
    def test_equal_utility_prefers_higher_quality(self):
        """Identical cost/speed, different q': the band cannot separate them."""
        candidates = _candidates()
        for c in candidates:
            c.cost_utility = 0.5
            c.speed_utility = 0.5
            c.t300 = 2.0
        scores, _ = _run(Priorities(3, 5, 5), candidates)
        in_band = [s for s in scores if s.in_band]
        qualities = [s.quality_score for s in in_band]
        assert qualities == sorted(qualities, reverse=True)

    def test_equal_utility_and_quality_prefers_more_real_evidence(self):
        candidates = _candidates()
        for c in candidates:
            c.quality = 0.9
            c.cost_utility = 0.5
            c.speed_utility = 0.5
            c.t300 = 2.0
        # Two real benchmarks for A, five for D, three for the rest.
        n_real = {"A-frontier": 2, "D-budget": 5}
        for c in candidates:
            c.n_real = n_real.get(c.model.model_id, 3)
        scores, _ = _run(Priorities(3, 3, 3), candidates)
        assert scores[0].model_id == "D-budget"
        assert scores[-1].model_id == "A-frontier"

    def test_full_ties_fall_back_to_ascending_model_id(self):
        candidates = _candidates()
        for c in candidates:
            c.quality = 0.9
            c.cost_utility = 0.5
            c.speed_utility = 0.5
            c.t300 = 2.0
            c.n_real = 4
        scores, _ = _run(Priorities(3, 3, 3), candidates)
        ids = [s.model_id for s in scores]
        assert ids == sorted(ids)

    def test_an_out_of_band_model_can_never_outrank_a_contender(self):
        """A perfect-cost/speed model below the band still loses."""
        candidates = _candidates(quality_overrides={"D-budget": 0.10})
        for c in candidates:
            if c.model.model_id == "D-budget":
                c.cost_utility = 1.0
                c.speed_utility = 1.0
        scores, _ = _run(Priorities(3, 3, 3), candidates)
        assert scores[-1].model_id == "D-budget"
        assert not scores[-1].in_band
        assert scores[-1].final_score <= 0.5


class TestMissingEvidence:
    def test_an_all_imputed_model_does_not_win_on_a_tie(self):
        """``n_real`` breaks ties the band and the secondary term cannot."""
        candidates = _candidates()
        for c in candidates:
            c.quality = 0.9
            c.cost_utility = 0.5
            c.speed_utility = 0.5
            c.t300 = 2.0
            c.n_real = 5
            c.imputed = 0
        ghost = candidates[0]
        ghost.n_real = 0
        ghost.imputed = 5
        ghost.top_benchmarks = []
        scores, _ = _run(Priorities(3, 3, 3), candidates)
        assert scores[-1].model_id == ghost.model.model_id

    def test_an_unpriced_model_never_wins_a_cost_route(self):
        candidates = _candidates()
        unpriced = candidates[3]  # D, the cheapest, loses its price
        unpriced.price_per_m = None
        unpriced.cost_utility = cost_utility(None)[0]
        unpriced.flags = ["cost: unknown"]
        scores, by_id = _run(Priorities(1, 5, 1), candidates)
        assert scores[-1].model_id == unpriced.model.model_id
        assert by_id[unpriced.model.model_id].cost_score == 0.0
        assert "cost unknown" in by_id[unpriced.model.model_id].reasoning
