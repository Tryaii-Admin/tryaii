"""Tests for benchmark normalization."""

from tryaii.scoring.benchmarks import BenchmarkNormalizer, NormalizationRange


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
    def test_standard_benchmarks_loaded(self):
        normalizer = BenchmarkNormalizer()
        assert "MMLU" in normalizer.known_benchmarks
        assert "HumanEval" in normalizer.known_benchmarks
        assert len(normalizer.known_benchmarks) >= 12

    def test_normalize_mmlu(self):
        normalizer = BenchmarkNormalizer()
        # MMLU range: 25-95
        score = normalizer.normalize("MMLU", 60)
        assert 0.0 < score < 1.0

    def test_normalize_elo_scale(self):
        normalizer = BenchmarkNormalizer()
        # Chatbot Arena uses ELO: 1000-1550
        score = normalizer.normalize("Chatbot Arena (LMSys)", 1275)
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

    def test_get_range_mmlu(self):
        normalizer = BenchmarkNormalizer()
        r = normalizer.get_range("MMLU")
        assert r is not None
        assert r.min_score == 25
        assert r.max_score == 95

    def test_mt_bench_matches_original(self):
        normalizer = BenchmarkNormalizer()
        r = normalizer.get_range("MT-Bench")
        assert r is not None
        # Canonical MT-Bench range matches STANDARD_BENCHMARKS (the default
        # routing path) and the Node SDK: NormalizationRange(5, 10).
        assert r.min_score == 5.0
        assert r.max_score == 10

    def test_livebench_matches_original(self):
        normalizer = BenchmarkNormalizer()
        r = normalizer.get_range("LiveBench")
        assert r is not None
        assert r.min_score == 0
        assert r.max_score == 100
