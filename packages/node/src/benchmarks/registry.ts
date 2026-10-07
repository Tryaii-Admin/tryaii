/**
 * Extensible benchmark registry.
 *
 * Allows users to register custom benchmarks with their own training queries
 * and normalization ranges. Designed for high connectivity with external
 * benchmark-creation tools.
 */

import { readFileSync, writeFileSync } from 'node:fs';

import { BenchmarkNormalizer, NormalizationRange } from '../scoring/benchmarks.js';
import { CatalogBundle, resolveBundle } from '../catalog/bundle.js';
import type { BundleLike } from '../catalog/bundle.js';
import type { CatalogMode } from '../catalog/client.js';

/** Complete definition of a benchmark. */
export interface BenchmarkDefinition {
  /** Benchmark name (e.g., "MMLU"). */
  name: string;

  /** Human-readable description. */
  description: string;

  /** Representative prompts for centroid generation. */
  trainingQueries: string[];

  /** Normalization range for raw scores. */
  normalization: NormalizationRange;

  /** Broad category (TECHNICAL, CREATIVE, etc.). */
  broadCategory: string;

  /** Subcategories this benchmark covers. */
  subcategories: string[];

  /** Optional metadata. */
  metadata: Record<string, unknown>;

  /**
   * Catalog fields (benchmarks.json). `weight` undefined = keep the
   * normalizer's default for this name; `randomChanceFloor` undefined/null = no
   * floor. Not part of `benchmarkToDict` (the `tryaii benchmarks --json` shape
   * is unchanged).
   */
  weight?: number;
  randomChanceFloor?: number | null;
  family?: string;
}

/**
 * BenchmarkDefinitions for a bundle's benchmarks.json, in display order.
 *
 * The normalization range comes from the bundle's normalization_ranges.json
 * (fallback 0-100 for a name it lacks); training queries stay empty -- the
 * centroid generator reads them from the bundle when it needs them.
 */
export function definitionsFromBundle(bundle: CatalogBundle): BenchmarkDefinition[] {
  const ranges = bundle.rangeEntries();
  return bundle.benchmarkEntries.map((entry) => {
    const rng = ranges[entry.name];
    return {
      name: entry.name,
      description: entry.description ?? '',
      trainingQueries: [],
      normalization: rng
        ? new NormalizationRange(rng.lo, rng.hi, rng.description ?? '')
        : new NormalizationRange(0, 100),
      broadCategory: entry.broad_category ?? 'TECHNICAL',
      subcategories: [...(entry.subcategories ?? [])],
      metadata: {},
      weight: entry.weight,
      randomChanceFloor: entry.random_chance_floor ?? null,
      family: entry.family ?? '',
    };
  });
}

/** Create a BenchmarkDefinition from a plain object (e.g. loaded from JSON). */
export function benchmarkFromDict(d: Record<string, unknown>): BenchmarkDefinition {
  const norm = (d.normalization ?? {}) as Record<string, number>;
  return {
    name: (d.name as string) ?? '',
    description: (d.description as string) ?? '',
    trainingQueries: (d.training_queries as string[]) ?? (d.trainingQueries as string[]) ?? [],
    normalization: new NormalizationRange(
      norm.min_score ?? norm.minScore ?? 0,
      norm.max_score ?? norm.maxScore ?? 100,
    ),
    broadCategory: (d.broad_category as string) ?? (d.broadCategory as string) ?? 'TECHNICAL',
    subcategories: (d.subcategories as string[]) ?? [],
    metadata: (d.metadata as Record<string, unknown>) ?? {},
    weight: (d.weight as number | undefined) ?? undefined,
    randomChanceFloor:
      (d.random_chance_floor as number | null | undefined) ??
      (d.randomChanceFloor as number | null | undefined) ??
      null,
    family: (d.family as string) ?? '',
  };
}

/** Serialize a BenchmarkDefinition to a plain object. */
export function benchmarkToDict(b: BenchmarkDefinition): Record<string, unknown> {
  return {
    name: b.name,
    description: b.description,
    training_queries: b.trainingQueries,
    normalization: {
      min_score: b.normalization.minScore,
      max_score: b.normalization.maxScore,
    },
    broad_category: b.broadCategory,
    subcategories: b.subcategories,
    metadata: b.metadata,
  };
}

/**
 * Registry for benchmark definitions.
 *
 * Provides a clean interface for:
 *   - Registering custom benchmarks
 *   - Loading benchmarks from JSON files (for tool connectivity)
 *   - Exporting benchmark definitions
 *   - Integrating with the centroid generator and scoring engine
 */
export class BenchmarkRegistry {
  private _benchmarks: Map<string, BenchmarkDefinition>;

  constructor() {
    this._benchmarks = new Map();
  }

  /**
   * Create a registry pre-loaded with the default catalog's benchmarks.
   *
   * `bundle` (a CatalogBundle or bundle directory) overrides the default
   * catalog -- see `resolveBundle`.
   */
  static default(bundle?: BundleLike | null, catalog: CatalogMode = 'auto'): BenchmarkRegistry {
    return BenchmarkRegistry.fromBundle(resolveBundle(bundle, catalog));
  }

  /** Create a registry holding exactly one bundle's benchmarks. */
  static fromBundle(bundle: CatalogBundle): BenchmarkRegistry {
    const registry = new BenchmarkRegistry();
    for (const benchmark of definitionsFromBundle(bundle)) {
      registry._benchmarks.set(benchmark.name, benchmark);
    }
    return registry;
  }

  /** Register a new benchmark or update an existing one. */
  register(benchmark: BenchmarkDefinition): void {
    this._benchmarks.set(benchmark.name, benchmark);
  }

  /** Remove a benchmark. Returns true if it existed. */
  unregister(name: string): boolean {
    return this._benchmarks.delete(name);
  }

  /** Get a benchmark by name. */
  get(name: string): BenchmarkDefinition | undefined {
    return this._benchmarks.get(name);
  }

  /** All registered benchmark names. */
  get names(): string[] {
    return [...this._benchmarks.keys()];
  }

  /** All registered benchmarks. */
  get allBenchmarks(): BenchmarkDefinition[] {
    return [...this._benchmarks.values()];
  }

  /** Get all training queries grouped by benchmark name. */
  getTrainingQueries(): Record<string, string[]> {
    const result: Record<string, string[]> = {};
    for (const [name, b] of this._benchmarks) {
      if (b.trainingQueries.length > 0) {
        result[name] = b.trainingQueries;
      }
    }
    return result;
  }

  /** Create a BenchmarkNormalizer from all registered benchmarks. */
  getNormalizer(): BenchmarkNormalizer {
    const normalizer = new BenchmarkNormalizer();
    for (const [name, benchmark] of this._benchmarks) {
      normalizer.registerRange(
        name,
        benchmark.normalization.minScore,
        benchmark.normalization.maxScore,
        benchmark.description,
      );
      if (benchmark.weight !== undefined) {
        normalizer.registerWeight(name, benchmark.weight);
      }
    }
    return normalizer;
  }

  /** `{name: floor}` for the registered benchmarks that declare one. */
  randomChanceFloors(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const [name, b] of this._benchmarks) {
      if (b.randomChanceFloor !== undefined && b.randomChanceFloor !== null) {
        out[name] = b.randomChanceFloor;
      }
    }
    return out;
  }

  /** `{name: [broadCategory, primary subcategory]}` for the classifier. */
  categories(): Record<string, [string, string]> {
    const out: Record<string, [string, string]> = {};
    for (const [name, b] of this._benchmarks) {
      out[name] = [b.broadCategory, b.subcategories[0] ?? 'GENERAL'];
    }
    return out;
  }

  /**
   * Load benchmarks from a JSON file.
   *
   * @returns Number of benchmarks loaded.
   */
  loadFromFile(path: string): number {
    const raw = readFileSync(path, 'utf-8');
    const data = JSON.parse(raw);

    let count = 0;
    for (const item of data.benchmarks ?? []) {
      const benchmark = benchmarkFromDict(item);
      this.register(benchmark);
      count++;
    }
    return count;
  }

  /** Export all benchmarks to a JSON file. */
  exportToFile(path: string): void {
    const data = {
      benchmarks: [...this._benchmarks.values()].map(benchmarkToDict),
    };
    writeFileSync(path, JSON.stringify(data, null, 2));
  }

  get length(): number {
    return this._benchmarks.size;
  }

  has(name: string): boolean {
    return this._benchmarks.has(name);
  }
}
