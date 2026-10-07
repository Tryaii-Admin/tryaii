/**
 * Standard benchmark definitions -- the benchmarks of the packaged starter catalog.
 *
 * The benchmark taxonomy (names, descriptions, broad/sub categories, families,
 * importance weights, random-chance floors) is catalog data: it lives in each
 * catalog bundle's benchmarks.json (docs/catalog/CONTRACT-catalog-v1.md,
 * Appendix A), with its normalization ranges in normalization_ranges.json.
 * `STANDARD_BENCHMARKS` is the packaged starter bundle's set, kept for
 * backwards compatibility; use `BenchmarkRegistry.fromBundle(bundle)` for any
 * other catalog. Mirrors the Python package's `standard.py`.
 */

import { starterBundle } from '../catalog/bundle.js';
import { definitionsFromBundle } from './registry.js';
import type { BenchmarkDefinition } from './registry.js';

export const STANDARD_BENCHMARKS: BenchmarkDefinition[] = definitionsFromBundle(starterBundle());
