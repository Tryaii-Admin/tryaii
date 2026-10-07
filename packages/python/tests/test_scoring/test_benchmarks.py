"""Tests for benchmark normalization."""

import pytest

from tryaii.scoring.benchmarks import BenchmarkNormalizer, NormalizationRange

# The ranges are catalog-derived (lo = p25 of the benchmark's real scores, hi =
# their max) and regenerated whenever the catalog moves, so the exact numbers
# are asserted against the bundle's normalization_ranges.json rather than
# hard-coded here. What is pinned is the *scale* of each family -- an ELO range
# collapsing to 0-100, or LiveBench ceasing to be a 0-1 fraction, is the failure
# mode that silently craters routing. MMLU, HumanEval and LiveBench are
# full-catalog benchmarks, so those tests run on the full bundle.


@pytest.fixture
def full_normalizer(full_bundle):
    return BenchmarkNormalizer.from_bundle(full_bundle)


@pytest.fixture
def full_ranges(full_bundle):
    return full_bundle.range_entries()


class TestNormalizationRange:
    def test_normalize_mid_range(self):
        r = NormalizationRange(0, 100)
        assert abs(r.normalize(50) - 0.5) < 1e-6

    def test_normalize_at_min(self):
        r = NormalizationRange(20, 80)
        assert abs(r.normalize(20) - 0.0) < 1e-6

    def test_normalize_at_max(self):
        r = NormalizationRange(20, 80)
        assert abs(r.normalize(80) - 1.0) < 1e-6

    def test_normalize_clamps_above(self):
        r = NormalizationRange(0, 100)
        assert r.normalize(150) == 1.0

    def test_normalize_clamps_below(self):
        r = NormalizationRange(0, 100)
        assert r.normalize(-10) == 0.0

    def test_equal_min_max(self):
        r = NormalizationRange(50, 50)
        assert r.normalize(50) == 0.5


class TestBenchmarkNormalizer:
    def test_standard_benchmarks_loaded(self, starter_bundle):
        normalizer = BenchmarkNormalizer()
        # The default normalizer holds the packaged starter catalog's tables.
        assert set(normalizer.known_benchmarks) == set(starter_bundle.benchmark_names)
        assert "GPQA" in normalizer.known_benchmarks
        assert len(normalizer.known_benchmarks) >= 10

    def test_full_bundle_benchmarks_loaded(self, full_normalizer):
        assert "MMLU" in full_normalizer.known_benchmarks
        assert "HumanEval" in full_normalizer.known_benchmarks
        assert len(full_normalizer.known_benchmarks) >= 30

    def test_normalize_mmlu(self, full_normalizer):
        normalizer = full_normalizer
        # MMLU is a 0-100 accuracy; the catalog p25 floor sits in the 60s, so a
        # raw 60 now normalises to the floor instead of landing mid-range.
        r = normalizer.get_range("MMLU")
        mid = (r.min_score + r.max_score) / 2
        assert 0.0 < normalizer.normalize("MMLU", mid) < 1.0
        assert normalizer.normalize("MMLU", 60) == 0.0

    def test_normalize_elo_scale(self):
        normalizer = BenchmarkNormalizer()
        # Chatbot Arena uses ELO: 1200-1550
        score = normalizer.normalize("Chatbot Arena Elo", 1400)
        assert 0.0 < score < 1.0

    def test_unknown_benchmark_assumes_percentage(self):
        normalizer = BenchmarkNormalizer()
        score = normalizer.normalize("UnknownBench", 75)
        assert abs(score - 0.75) < 1e-6

    def test_register_custom_range(self):
        normalizer = BenchmarkNormalizer()
        normalizer.register_range("CustomBench", 0, 200, "Test benchmark")
        score = normalizer.normalize("CustomBench", 100)
        assert abs(score - 0.5) < 1e-6

    def test_get_range_mmlu_comes_from_the_generated_master(
        self, full_normalizer, full_ranges
    ):
        normalizer = full_normalizer
        r = normalizer.get_range("MMLU")
        assert r is not None
        assert r.min_score == full_ranges["MMLU"]["lo"]
        assert r.max_score == full_ranges["MMLU"]["hi"]
        # Percentage scale, with real headroom between the floor and the top.
        assert 0 < r.min_score < r.max_score <= 100

    def test_arena_elo_keeps_its_elo_scale(self, starter_bundle):
        normalizer = BenchmarkNormalizer()
        ranges = starter_bundle.range_entries()
        r = normalizer.get_range("Chatbot Arena Elo")
        assert r is not None
        assert r.min_score == ranges["Chatbot Arena Elo"]["lo"]
        assert r.max_score == ranges["Chatbot Arena Elo"]["hi"]
        # ELO ratings, not percentages.
        assert 1000 < r.min_score < r.max_score < 1600

    def test_livebench_is_fraction_scale(self, full_normalizer, full_ranges):
        normalizer = full_normalizer
        r = normalizer.get_range("LiveBench")
        assert r is not None
        assert r.min_score == full_ranges["LiveBench"]["lo"]
        assert r.max_score == full_ranges["LiveBench"]["hi"]
        # The remote source reports LiveBench as a 0-1 fraction.
        assert 0.0 < r.min_score < r.max_score <= 1.0
