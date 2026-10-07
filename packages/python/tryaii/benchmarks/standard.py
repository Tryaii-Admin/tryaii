"""
Standard benchmark definitions -- the benchmarks of the packaged starter catalog.

The benchmark taxonomy (names, descriptions, broad/sub categories, families,
importance weights, random-chance floors) is catalog data: it lives in each
catalog bundle's ``benchmarks.json`` (docs/catalog/CONTRACT-catalog-v1.md,
Appendix A), with its normalization ranges in ``normalization_ranges.json``.
``STANDARD_BENCHMARKS`` is the packaged starter bundle's set, kept for
backwards compatibility; use ``BenchmarkRegistry.from_bundle(bundle)`` for any
other catalog. Mirrors the Node package's ``standard.ts``.
"""

from tryaii.benchmarks.registry import BenchmarkDefinition, definitions_from_bundle
from tryaii.catalog.bundle import starter_bundle

STANDARD_BENCHMARKS: list[BenchmarkDefinition] = definitions_from_bundle(starter_bundle())
