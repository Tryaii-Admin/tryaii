"""
Benchmark score normalization.

Different benchmarks use different scales (0-100%, ELO ratings, etc.).
This module normalizes them all to a 0-1 range for fair comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class NormalizationRange:
    """Min/max range for normalizing a benchmark score to 0-1."""
    min_score: float
    max_score: float
    description: str = ""

    def normalize(self, raw_score: float) -> float:
        """Normalize a raw benchmark score to 0-1."""
        if self.max_score == self.min_score:
            return 0.5
        normalized = (raw_score - self.min_score) / (self.max_score - self.min_score)
        return max(0.0, min(1.0, normalized))


# Standard benchmark normalization ranges.
# Fit to the observed min/max of the shipped model catalog so it spreads across
# most of 0-1. Loose ranges crush frontier models into a narrow high band where
# quality can't differentiate them and routing collapses onto cost/speed; re-fit
# when the catalog changes substantially. Keep in sync with STANDARD_BENCHMARKS
# (guarded by test_parity.py::test_standalone_ranges_match_standard_benchmarks).
NORMALIZATION_RANGES: dict[str, NormalizationRange] = {
    "MMLU": NormalizationRange(40, 96, "Academic knowledge across 57 subjects"),
    "HellaSwag": NormalizationRange(68, 99, "Commonsense reasoning"),
    "HumanEval": NormalizationRange(30, 97, "Code generation"),
    "SWE-bench": NormalizationRange(8, 86, "Real-world software engineering"),
    "TruthfulQA": NormalizationRange(40, 86, "Truthful question answering"),
    "ARC": NormalizationRange(70, 96, "Science exam questions"),
    "GSM8K": NormalizationRange(65, 99, "Grade school math"),
    "DROP": NormalizationRange(48, 91, "Reading comprehension with arithmetic"),
    "SuperGLUE": NormalizationRange(48, 95, "Natural language understanding"),
    "Chatbot Arena (LMSys)": NormalizationRange(1300, 1520, "Human-rated chat quality"),
    "MT-Bench": NormalizationRange(6, 10, "Multi-turn conversation quality"),
    "LiveBench": NormalizationRange(58, 84, "Fresh, contamination-resistant evaluation"),
}


# Per-benchmark importance weights ("trust" multipliers), orthogonal to
# prompt-similarity: the weight multiplies into the similarity weight in the
# scoring engine, so a higher weight pulls model ranking harder toward that
# benchmark. Weight 1.0 is neutral, and an all-1.0 (or empty) table reproduces
# the old similarity-only behaviour exactly.
#
# Left empty here: the weight *values* are a property of the shipped catalog
# (which benchmarks are saturated/gamed vs contamination-resistant) and belong
# with the catalog data, so the default catalog stays neutral. Keys, when set,
# must be benchmark names present in NORMALIZATION_RANGES.
BENCHMARK_WEIGHTS: dict[str, float] = {}

# Weight used for any benchmark with no explicit entry (neutral).
DEFAULT_BENCHMARK_WEIGHT = 1.0

# Plausibility floors for multiple-choice benchmarks: a real model cannot score
# meaningfully below random chance, so a value under these floors is corrupt
# data (e.g. a normalized sub-score of GPQA=1.3 where the real accuracy is ~90).
# Such values are dropped on load (see ``is_implausible_benchmark_score``) so
# they neither crater the model directly nor poison the imputation medians.
#
# Left empty here: floors are only needed for catalogs whose upstream sources
# emit such corruption, so they ship with the catalog data. The mechanism is
# always active and is a no-op while this table is empty.
RANDOM_CHANCE_FLOORS: dict[str, float] = {}


def is_implausible_benchmark_score(benchmark: str, raw_score: float) -> bool:
    """True if a raw benchmark score is implausibly low for its scale (corrupt)."""
    floor = RANDOM_CHANCE_FLOORS.get(benchmark)
    return floor is not None and raw_score < floor


class BenchmarkNormalizer:
    """
    Normalizes benchmark scores across different scales and tracks each
    benchmark's importance weight.

    Supports standard benchmarks out of the box and allows registering custom
    normalization ranges and weights.
    """

    def __init__(self):
        self._ranges: dict[str, NormalizationRange] = dict(NORMALIZATION_RANGES)
        self._weights: dict[str, float] = dict(BENCHMARK_WEIGHTS)

    def normalize(self, benchmark: str, raw_score: float) -> float:
        """Normalize a raw benchmark score to 0-1."""
        if benchmark not in self._ranges:
            # Unknown benchmark -- assume 0-100 percentage scale
            return max(0.0, min(1.0, raw_score / 100.0))
        return self._ranges[benchmark].normalize(raw_score)

    def register_range(
        self,
        benchmark: str,
        min_score: float,
        max_score: float,
        description: str = "",
    ) -> None:
        """Register a custom normalization range for a benchmark."""
        self._ranges[benchmark] = NormalizationRange(min_score, max_score, description)

    def get_range(self, benchmark: str) -> Optional[NormalizationRange]:
        """Get the normalization range for a benchmark."""
        return self._ranges.get(benchmark)

    def register_weight(self, benchmark: str, weight: float) -> None:
        """Set a custom importance weight for a benchmark."""
        self._weights[benchmark] = weight

    def get_weight(self, benchmark: str) -> float:
        """
        Importance weight for a benchmark (defaults to DEFAULT_BENCHMARK_WEIGHT
        for benchmarks with no explicit entry, so unknown/custom benchmarks stay
        neutral).
        """
        return self._weights.get(benchmark, DEFAULT_BENCHMARK_WEIGHT)

    @property
    def known_benchmarks(self) -> list[str]:
        """List all benchmarks with registered normalization ranges."""
        return list(self._ranges.keys())
