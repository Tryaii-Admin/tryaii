/**
 * Benchmark score normalization.
 *
 * Different benchmarks use different scales (0-100%, ELO ratings, etc.).
 * This module normalizes them all to a 0-1 range for fair comparison.
 */

import { CatalogBundle, starterBundle } from '../catalog/bundle.js';

export class NormalizationRange {
  readonly minScore: number;
  readonly maxScore: number;
  readonly description: string;

  constructor(minScore: number, maxScore: number, description = '') {
    this.minScore = minScore;
    this.maxScore = maxScore;
    this.description = description;
  }

  /** Normalize a raw benchmark score to 0-1. */
  normalize(rawScore: number): number {
    if (this.maxScore === this.minScore) return 0.5;
    const normalized = (rawScore - this.minScore) / (this.maxScore - this.minScore);
    return Math.max(0.0, Math.min(1.0, normalized));
  }
}

/*
 * Benchmark data -- ranges, importance weights and plausibility floors -- is
 * CATALOG data, not code: it comes from the active catalog bundle
 * (docs/catalog/CONTRACT-catalog-v1.md; normalization_ranges.json and
 * benchmarks.json), so the same engine code routes the starter and the full
 * catalog. The module-level tables below are derived from the *packaged starter
 * bundle* and are kept for backwards compatibility; a Router built on another
 * bundle gets its tables through `BenchmarkNormalizer.fromBundle` /
 * `BenchmarkRegistry.fromBundle` instead.
 *
 * Ranges are generated when the catalog is built (lo = p25 of a benchmark's
 * real scores across the routable full catalog, hi = their max) and shipped
 * identically in every bundle, so a model scores the same on both catalogs for
 * every benchmark they share.
 *
 * Scales differ per benchmark and are NOT all 0-100:
 *   - Chatbot Arena variants are ELO ratings.
 *   - LiveBench and its sub-tracks are 0-1 fractions.
 *   - The rest are 0-100 accuracy percentages.
 * Out-of-range outliers simply clamp to [0, 1].
 *
 * Importance weights (benchmarks.json `weight`) are the "how much do we trust
 * this benchmark as a routing signal" axis, orthogonal to the prompt's
 * similarity to the benchmark: higher pulls model ranking harder, 1.0 is
 * neutral. Random-chance floors (`random_chance_floor`) mark scores below a
 * multiple-choice benchmark's random baseline as corrupt; they are dropped on
 * load (see `isImplausibleBenchmarkScore`). Mirrors the Python package.
 */

/** `{benchmark: NormalizationRange}` from a bundle's normalization_ranges.json. */
export function rangesFromBundle(bundle: CatalogBundle): Record<string, NormalizationRange> {
  const ranges: Record<string, NormalizationRange> = {};
  for (const [name, entry] of Object.entries(bundle.rangeEntries())) {
    ranges[name] = new NormalizationRange(entry.lo, entry.hi, entry.description ?? '');
  }
  return ranges;
}

const STARTER = starterBundle();

/** Normalization ranges of the packaged starter catalog. */
export const NORMALIZATION_RANGES: Record<string, NormalizationRange> = rangesFromBundle(STARTER);

/** Importance weights of the packaged starter catalog's benchmarks. */
export const BENCHMARK_WEIGHTS: Record<string, number> = STARTER.benchmarkWeights();

/**
 * Weight used for any benchmark with no explicit entry (neutral). Engine
 * semantics for custom / unknown benchmarks, not catalog data.
 */
export const DEFAULT_BENCHMARK_WEIGHT = 1.0;

/** Random-chance floors of the packaged starter catalog's benchmarks. */
export const RANDOM_CHANCE_FLOORS: Record<string, number> = STARTER.randomChanceFloors();

/**
 * True if a raw benchmark score is implausibly low for its scale (corrupt).
 *
 * `floors` defaults to the starter catalog's `RANDOM_CHANCE_FLOORS`; a registry
 * loading another bundle passes that bundle's floors.
 */
export function isImplausibleBenchmarkScore(
  benchmark: string,
  rawScore: number,
  floors?: Record<string, number>,
): boolean {
  const floor = (floors ?? RANDOM_CHANCE_FLOORS)[benchmark];
  return floor !== undefined && rawScore < floor;
}

/**
 * Normalizes benchmark scores across different scales and tracks each
 * benchmark's importance weight.
 *
 * Supports standard benchmarks out of the box and allows registering custom
 * normalization ranges and weights.
 */
export class BenchmarkNormalizer {
  private _ranges: Map<string, NormalizationRange>;
  private _weights: Map<string, number>;

  /** Defaults: the packaged starter catalog's tables. */
  constructor(
    ranges?: Record<string, NormalizationRange>,
    weights?: Record<string, number>,
  ) {
    this._ranges = new Map(Object.entries(ranges ?? NORMALIZATION_RANGES));
    this._weights = new Map(Object.entries(weights ?? BENCHMARK_WEIGHTS));
  }

  /** A normalizer holding exactly one bundle's ranges and weights. */
  static fromBundle(bundle: CatalogBundle): BenchmarkNormalizer {
    return new BenchmarkNormalizer(rangesFromBundle(bundle), bundle.benchmarkWeights());
  }

  /** Normalize a raw benchmark score to 0-1. */
  normalize(benchmark: string, rawScore: number): number {
    const range = this._ranges.get(benchmark);
    if (!range) {
      // Unknown benchmark -- assume 0-100 percentage scale
      return Math.max(0.0, Math.min(1.0, rawScore / 100.0));
    }
    return range.normalize(rawScore);
  }

  /** Register a custom normalization range for a benchmark. */
  registerRange(
    benchmark: string,
    minScore: number,
    maxScore: number,
    description = '',
  ): void {
    this._ranges.set(benchmark, new NormalizationRange(minScore, maxScore, description));
  }

  /** Get the normalization range for a benchmark. */
  getRange(benchmark: string): NormalizationRange | undefined {
    return this._ranges.get(benchmark);
  }

  /** Set a custom importance weight for a benchmark. */
  registerWeight(benchmark: string, weight: number): void {
    this._weights.set(benchmark, weight);
  }

  /**
   * Importance weight for a benchmark (defaults to DEFAULT_BENCHMARK_WEIGHT
   * for benchmarks with no explicit entry, so unknown/custom benchmarks stay
   * neutral).
   */
  getWeight(benchmark: string): number {
    return this._weights.get(benchmark) ?? DEFAULT_BENCHMARK_WEIGHT;
  }

  /** List all benchmarks with registered normalization ranges. */
  get knownBenchmarks(): string[] {
    return [...this._ranges.keys()];
  }
}
