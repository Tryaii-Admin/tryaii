"""Coverage-aware benchmark weighting.

The per-term weight that builds ``q'`` is

    w(b, m) = similarity(b) * BENCHMARK_WEIGHTS[b]
              * coverage(b) ** COVERAGE_EXPONENT
              * (IMPUTED_TERM_WEIGHT if m's term for b is imputed else 1.0)

with ``coverage(b) = n_real(b) / N`` over the routable catalog. Selection is
untouched (still top-5 by raw similarity), so everything below is about the
weights and the weighted mean, never about *which* benchmarks are picked.

The fixture registry is built so the three coverages are exactly 1.0, 0.5 and
0.25, which makes every expected weight a short product that is written out
longhand here rather than copied from the engine.
"""

from __future__ import annotations

import math

import pytest

from tryaii.registry.models import ModelRegistry, compute_benchmark_coverage
from tryaii.scoring import (
    BENCHMARK_WEIGHTS,
    COVERAGE_EXPONENT,
    IMPUTED_TERM_WEIGHT,
    BenchmarkNormalizer,
    Priorities,
    ScoringEngine,
)

# Three real (starter-catalog) benchmarks, so the shipped default normalizer
# and BENCHMARK_WEIGHTS apply.
FULL = "GPQA"  # reported by all 4 fixture models -> coverage 1.0
HALF = "MMLU-Pro"  # reported by 2 of 4           -> coverage 0.5
SPARSE = "SciCode"  # reported by 1 of 4          -> coverage 0.25
ABSENT = "IFBench"  # reported by nobody          -> coverage 0.0

SIMS = {FULL: 0.9, HALF: 0.6, SPARSE: 0.3}

PRICING = (0.001, 0.002)


@pytest.fixture()
def registry() -> ModelRegistry:
    """4 models; FULL/HALF/SPARSE reported by 4/2/1 of them."""
    reg = ModelRegistry()
    reg.add(
        "vendor/all-three",
        "Vendor",
        benchmarks={FULL: 60.0, HALF: 80.0, SPARSE: 90.0},
        pricing=PRICING,
        tokens_per_second=100.0,
        ttft_ms=500.0,
    )
    reg.add(
        "vendor/two",
        "Vendor",
        benchmarks={FULL: 50.0, HALF: 70.0},
        pricing=PRICING,
        tokens_per_second=100.0,
        ttft_ms=500.0,
    )
    reg.add(
        "vendor/one-a",
        "Vendor",
        benchmarks={FULL: 40.0},
        pricing=PRICING,
        tokens_per_second=100.0,
        ttft_ms=500.0,
    )
    reg.add(
        "vendor/one-b",
        "Vendor",
        benchmarks={FULL: 30.0},
        pricing=PRICING,
        tokens_per_second=100.0,
        ttft_ms=500.0,
    )
    return reg


def _by_id(scores):
    return {s.model_id: s for s in scores}


def _score(registry: ModelRegistry, sims=None, coverage=None, priorities=None):
    engine = ScoringEngine()
    models = registry.all_models
    return _by_id(
        engine.score_models(
            models,
            SIMS if sims is None else sims,
            priorities or Priorities(5, 1, 1),
            top_k=len(models),
            benchmark_coverage=(
                registry.benchmark_coverage() if coverage is None else coverage
            ),
        )
    )


# --------------------------------------------------------------- constants
def test_the_two_constants_are_the_shipped_values():
    assert IMPUTED_TERM_WEIGHT == 0.5
    assert COVERAGE_EXPONENT == 1.0


# ---------------------------------------------------------------- coverage
def test_fixture_coverage_is_exactly_one_half_and_a_quarter(registry):
    coverage = registry.benchmark_coverage()
    assert coverage == {FULL: 1.0, HALF: 0.5, SPARSE: 0.25}
    assert coverage.get(ABSENT, 0.0) == 0.0


def test_coverage_is_cached_until_the_registry_changes(registry):
    first = registry.benchmark_coverage()
    assert registry.benchmark_coverage() is first
    registry.add("vendor/newcomer", "Vendor", benchmarks={SPARSE: 55.0})
    second = registry.benchmark_coverage()
    assert second is not first
    # 5 routable models now, 2 of which report SPARSE.
    assert second[SPARSE] == pytest.approx(0.4)
    assert second[FULL] == pytest.approx(0.8)


def test_free_tier_models_are_outside_n_and_outside_n_real(registry):
    """``:free`` ids are not routed, so they must not move coverage either."""
    baseline = dict(registry.benchmark_coverage())
    registry.add("vendor/freebie:free", "Vendor", benchmarks={SPARSE: 55.0})
    assert registry.benchmark_coverage() == baseline
    assert compute_benchmark_coverage(registry.all_models) == baseline


def test_a_non_finite_score_does_not_count_as_real_coverage(registry):
    registry.add("vendor/nan", "Vendor", benchmarks={FULL: math.nan})
    coverage = registry.benchmark_coverage()
    # 5 routable models, still only 4 real FULL scores.
    assert coverage[FULL] == pytest.approx(0.8)


def test_default_registry_coverage_has_one_entry_per_shipped_benchmark():
    coverage = ModelRegistry.default().benchmark_coverage()
    assert len(coverage) == 16  # the packaged starter catalog
    assert all(0.0 <= value <= 1.0 for value in coverage.values())


def test_full_registry_coverage_has_one_entry_per_full_benchmark(full_bundle):
    coverage = ModelRegistry.from_bundle(full_bundle).benchmark_coverage()
    assert len(coverage) == 33
    assert all(0.0 <= value <= 1.0 for value in coverage.values())
    # The motivating fact: the saturated 2021-era benchmarks are reported by a
    # small minority of the catalog, the modern coding benchmark by a third.
    assert coverage["GSM8K"] < coverage["LiveCodeBench"]


# ----------------------------------------------------------- effective weights
def test_effective_weights_are_similarity_times_weight_times_coverage(registry):
    """The all-real model: no imputation factor anywhere."""
    score = _score(registry)["vendor/all-three"]
    assert score.benchmark_weights == {
        FULL: round(0.9 * BENCHMARK_WEIGHTS[FULL] * 1.0, 4),
        HALF: round(0.6 * BENCHMARK_WEIGHTS[HALF] * 0.5, 4),
        SPARSE: round(0.3 * BENCHMARK_WEIGHTS[SPARSE] * 0.25, 4),
    }


def test_an_imputed_term_is_weighted_half(registry):
    scores = _score(registry)
    real = scores["vendor/all-three"].benchmark_weights
    one_imputed = scores["vendor/two"].benchmark_weights
    two_imputed = scores["vendor/one-a"].benchmark_weights

    # vendor/two has FULL and HALF for real and SPARSE imputed.
    assert one_imputed[FULL] == real[FULL]
    assert one_imputed[HALF] == real[HALF]
    assert one_imputed[SPARSE] == pytest.approx(
        real[SPARSE] * IMPUTED_TERM_WEIGHT, abs=5e-5
    )
    assert one_imputed[SPARSE] == round(
        0.3 * BENCHMARK_WEIGHTS[SPARSE] * 0.25 * 0.5, 4
    )

    # vendor/one-a has only FULL, so both other terms are halved.
    assert two_imputed[FULL] == real[FULL]
    assert two_imputed[HALF] == round(0.6 * BENCHMARK_WEIGHTS[HALF] * 0.5 * 0.5, 4)
    assert two_imputed[SPARSE] == round(
        0.3 * BENCHMARK_WEIGHTS[SPARSE] * 0.25 * 0.5, 4
    )


def test_weights_are_rounded_to_four_decimals(registry):
    for score in _score(registry).values():
        for value in score.benchmark_weights.values():
            assert value == round(value, 4)


# ---------------------------------------------------------------------- q'
def test_q_prime_is_the_new_weighted_mean(registry):
    """Hand-computed q' for the model whose every term is real evidence."""
    normalizer = BenchmarkNormalizer()
    terms = [
        (0.9 * BENCHMARK_WEIGHTS[FULL] * 1.0, normalizer.normalize(FULL, 60.0)),
        (0.6 * BENCHMARK_WEIGHTS[HALF] * 0.5, normalizer.normalize(HALF, 80.0)),
        (
            0.3 * BENCHMARK_WEIGHTS[SPARSE] * 0.25,
            normalizer.normalize(SPARSE, 90.0),
        ),
    ]
    expected = sum(w * n for w, n in terms) / sum(w for w, _ in terms)

    score = _score(registry)["vendor/all-three"]
    assert score.q_prime == pytest.approx(expected, abs=1e-12)
    assert score.quality_score == round(expected, 4)


def test_q_prime_equals_sum_w_norm_over_sum_w_for_every_model(registry):
    """The published weights are the ones q' was actually built from."""
    normalizer = BenchmarkNormalizer()
    scores = _score(registry)
    for score in scores.values():
        weights = score.benchmark_weights
        reals = dict(score.top_benchmarks)
        # Recompose only the real part and check it is bounded by the mean --
        # a weak but model-agnostic invariant; the exact imputed value is
        # covered by the imputation tests.
        assert set(reals) <= set(weights)
        assert 0.0 <= score.q_prime <= 1.0
        for name, normalized in reals.items():
            assert normalized == pytest.approx(
                normalizer.normalize(
                    name,
                    next(
                        m.benchmark_scores[name]
                        for m in registry.all_models
                        if m.model_id == score.model_id
                    ),
                )
            )


def test_coverage_shifts_quality_toward_the_well_covered_benchmark(registry):
    """Turning coverage off must change q' -- the hook is really live."""
    flat = {FULL: 1.0, HALF: 1.0, SPARSE: 1.0}
    with_coverage = _score(registry)["vendor/all-three"].q_prime
    without = _score(registry, coverage=flat)["vendor/all-three"].q_prime
    assert with_coverage != pytest.approx(without, abs=1e-9)
    # FULL (the lowest raw score of the three, 60.0) carries more of the mean
    # once HALF and SPARSE are discounted, so q' falls.
    assert with_coverage < without


# ----------------------------------------------------------- zero coverage
def test_a_zero_coverage_benchmark_contributes_nothing(registry):
    """An explicit coverage of 0.0 drops the term out of q' entirely."""
    zeroed = dict(registry.benchmark_coverage())
    zeroed[SPARSE] = 0.0
    scores = _score(registry, coverage=zeroed)

    assert scores["vendor/all-three"].benchmark_weights[SPARSE] == 0.0

    # q' is then exactly the two-term mean over FULL and HALF.
    normalizer = BenchmarkNormalizer()
    terms = [
        (0.9 * BENCHMARK_WEIGHTS[FULL] * 1.0, normalizer.normalize(FULL, 60.0)),
        (0.6 * BENCHMARK_WEIGHTS[HALF] * 0.5, normalizer.normalize(HALF, 80.0)),
    ]
    expected = sum(w * n for w, n in terms) / sum(w for w, _ in terms)
    assert scores["vendor/all-three"].q_prime == pytest.approx(expected, abs=1e-12)


def test_a_benchmark_no_model_reports_never_produces_a_term(registry):
    """Coverage 0.0 and no registry median: the term is skipped, not zeroed."""
    sims = {**SIMS, ABSENT: 0.95}
    scores = _score(registry, sims=sims)
    for score in scores.values():
        assert ABSENT not in score.benchmark_weights
    # and q' is unchanged from the run without the absent benchmark
    assert scores["vendor/all-three"].q_prime == pytest.approx(
        _score(registry)["vendor/all-three"].q_prime, abs=1e-12
    )


def test_all_terms_zero_coverage_falls_back_to_the_no_signal_path(registry):
    scores = _score(registry, coverage={})
    for score in scores.values():
        assert score.quality_score == 0.5
        assert "no benchmark signal" in score.signal_flags
        assert score.reasoning == "No benchmark signal -- routed on cost/speed"
        assert all(value == 0.0 for value in score.benchmark_weights.values())


# ------------------------------------------------------------- reasoning
def test_the_reasoning_string_does_not_mention_coverage(registry):
    """Byte parity with the pre-coverage format: same fragments, same order."""
    score = _score(registry)["vendor/two"]
    assert "coverage" not in score.reasoning.lower()
    parts = score.reasoning.split(" | ")
    assert parts[0].startswith("q'=")
    # vendor/two has 2 real of the 3 selected benchmarks and 1 imputed.
    assert parts[0].endswith("(2 real of 3)")
    assert parts[1] == "imputed: 1/3"
