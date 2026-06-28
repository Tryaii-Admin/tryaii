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


class BenchmarkNormalizer:
    """
    Normalizes benchmark scores across different scales.

    Supports standard benchmarks out of the box and allows
    registering custom normalization ranges.
    """

    def __init__(self):
        self._ranges: dict[str, NormalizationRange] = dict(NORMALIZATION_RANGES)

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

    @property
    def known_benchmarks(self) -> list[str]:
        """List all benchmarks with registered normalization ranges."""
        return list(self._ranges.keys())
