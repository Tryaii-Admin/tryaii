"""
Benchmark score normalization.

Different benchmarks use different scales (0-100%, ELO ratings, etc.).
This module normalizes them all to a 0-1 range for fair comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tryaii.catalog.bundle import CatalogBundle, starter_bundle


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


# Benchmark data -- ranges, importance weights and plausibility floors -- is
# CATALOG data, not code: it comes from the active catalog bundle
# (docs/catalog/CONTRACT-catalog-v1.md; ``normalization_ranges.json`` and
# ``benchmarks.json``), so the same engine code routes the starter and the full
# catalog. The module-level tables below are derived from the *packaged starter
# bundle* and are kept for backwards compatibility; a Router built on another
# bundle gets its tables through ``BenchmarkNormalizer.from_bundle`` /
# ``BenchmarkRegistry.from_bundle`` instead.
#
# Ranges are generated when the catalog is built (lo = p25 of a benchmark's
# real scores across the routable full catalog, hi = their max) and shipped
# identically in every bundle, so a model scores the same on both catalogs for
# every benchmark they share.
#
# Scales differ per benchmark and are NOT all 0-100:
#   - Chatbot Arena variants are ELO ratings.
#   - LiveBench and its sub-tracks are 0-1 fractions.
#   - The rest are 0-100 accuracy percentages.
# Out-of-range outliers simply clamp to [0, 1].
#
# Importance weights (``benchmarks.json`` ``weight``) are the "how much do we
# trust this benchmark as a routing signal" axis, orthogonal to the prompt's
# similarity to the benchmark: higher pulls model ranking harder, 1.0 is
# neutral. Random-chance floors (``random_chance_floor``) mark scores below a
# multiple-choice benchmark's random baseline as corrupt; they are dropped on
# load (see ``is_implausible_benchmark_score``).


def ranges_from_bundle(bundle: CatalogBundle) -> dict[str, NormalizationRange]:
    """``{benchmark: NormalizationRange}`` from a bundle's normalization_ranges.json."""
    return {
        name: NormalizationRange(entry["lo"], entry["hi"], entry.get("description", ""))
        for name, entry in bundle.range_entries().items()
    }


_STARTER = starter_bundle()

#: Normalization ranges of the packaged starter catalog.
NORMALIZATION_RANGES: dict[str, NormalizationRange] = ranges_from_bundle(_STARTER)

#: Importance weights of the packaged starter catalog's benchmarks.
BENCHMARK_WEIGHTS: dict[str, float] = _STARTER.benchmark_weights()

# Weight used for any benchmark with no explicit entry (neutral). Engine
# semantics for custom / unknown benchmarks, not catalog data.
DEFAULT_BENCHMARK_WEIGHT = 1.0

#: Random-chance floors of the packaged starter catalog's benchmarks.
RANDOM_CHANCE_FLOORS: dict[str, float] = _STARTER.random_chance_floors()


def is_implausible_benchmark_score(
    benchmark: str,
    raw_score: float,
    floors: Optional[dict[str, float]] = None,
) -> bool:
    """True if a raw benchmark score is implausibly low for its scale (corrupt).

    ``floors`` defaults to the starter catalog's ``RANDOM_CHANCE_FLOORS``; a
    registry loading another bundle passes that bundle's floors.
    """
    floor = (RANDOM_CHANCE_FLOORS if floors is None else floors).get(benchmark)
    return floor is not None and raw_score < floor


class BenchmarkNormalizer:
    """
    Normalizes benchmark scores across different scales and tracks each
    benchmark's importance weight.

    Supports standard benchmarks out of the box and allows registering custom
    normalization ranges and weights.
    """

    def __init__(
        self,
        ranges: Optional[dict[str, NormalizationRange]] = None,
        weights: Optional[dict[str, float]] = None,
    ):
        # Defaults: the packaged starter catalog's tables.
        self._ranges: dict[str, NormalizationRange] = dict(
            NORMALIZATION_RANGES if ranges is None else ranges
        )
        self._weights: dict[str, float] = dict(
            BENCHMARK_WEIGHTS if weights is None else weights
        )

    @classmethod
    def from_bundle(cls, bundle: CatalogBundle) -> BenchmarkNormalizer:
        """A normalizer holding exactly one bundle's ranges and weights."""
        return cls(ranges=ranges_from_bundle(bundle), weights=bundle.benchmark_weights())

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
