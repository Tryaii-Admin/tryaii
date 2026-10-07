/**
 * Centroid loader -- handles lazy initialization and model compatibility.
 *
 * Loading priority:
 *   1. In-memory cache (already loaded)
 *   2. User's cache directory (previously generated for their model), when its
 *      benchmark set matches the catalog bundle's
 *   3. The catalog bundle's centroids.json (when built for this embedding
 *      model -- zero delay)
 *   4. Generate from the bundle's training queries (non-default embedding model)
 */

import { existsSync, readdirSync, unlinkSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';

import { CatalogBundle, resolveBundle } from '../catalog/bundle.js';
import type { BundleLike } from '../catalog/bundle.js';
import { BaseEmbeddingProvider } from '../embeddings/base.js';
import { CentroidGenerator } from './generator.js';

/**
 * Stable fingerprint of the benchmark set present in a centroid file.
 *
 * Defined as the sorted benchmark names joined by "|". Used to detect when a
 * cached/bundled centroid file was generated against a different benchmark set
 * and must be regenerated. Must stay identical to the Python SDK's
 * `benchmark_fingerprint` (centroids/generator.py).
 */
export function benchmarkFingerprint(benchmarkNames: Iterable<string>): string {
  return [...benchmarkNames].sort().join('|');
}

/**
 * Fingerprint of a catalog's benchmark set, derived from its training queries.
 * A centroid file whose benchmark set doesn't match this was built against a
 * different benchmark set (or catalog) and must be regenerated. Mirrors the
 * Python SDK's `CentroidGenerator.default_benchmark_fingerprint(bundle)`.
 */
export function defaultBenchmarkFingerprint(bundle?: BundleLike | null): string {
  return benchmarkFingerprint(Object.keys(resolveBundle(bundle).trainingQueries.benchmarks));
}

/**
 * Whether the provider has reported a real embedding dimension yet.
 *
 * The default `LocalEmbeddingProvider` returns a hardcoded fallback (384)
 * before its model is initialized, so comparing against it would wrongly
 * reject/accept centroid files for non-default-dimension models. We detect the
 * uninitialized state via its private `_dimension` marker; providers that
 * always report a real dimension are treated as known.
 */
function providerDimensionKnown(provider: BaseEmbeddingProvider): boolean {
  const dim = (provider as unknown as { _dimension?: number | null })._dimension;
  // Providers without the marker (custom providers) report a real dimension.
  return dim === undefined ? true : dim !== null;
}

/**
 * Manages centroid lifecycle: load, validate, regenerate.
 *
 * For the catalog bundle's embedding model (all-MiniLM-L6-v2), centroids ship
 * in the bundle -- zero first-run delay. For other models, centroids are
 * generated from the bundle's training queries on first use and cached to
 * disk. `bundle` (a CatalogBundle or directory) defaults to the default
 * catalog -- see `resolveBundle`.
 */
export class CentroidLoader {
  private _provider: BaseEmbeddingProvider;
  private _centroids: Record<string, number[]> | null = null;
  private _generator: CentroidGenerator;
  private _userCachePath: string | null;
  private _bundle: CatalogBundle;

  constructor(
    embeddingProvider: BaseEmbeddingProvider,
    userCachePath?: string,
    bundle?: BundleLike | null,
  ) {
    this._provider = embeddingProvider;
    this._bundle = resolveBundle(bundle);
    this._generator = new CentroidGenerator(embeddingProvider, this._bundle);
    this._userCachePath = userCachePath ?? null;
  }

  /** The catalog bundle this loader serves. */
  get bundle(): CatalogBundle {
    return this._bundle;
  }

  /** This loader's user centroid cache path (null = no disk cache). */
  get cachePath(): string | null {
    return this._userCachePath;
  }

  /**
   * Write the user cache. When the path is keyed by catalog
   * (`centroids_<model>__<kind>-<version>.json`, see centroidFilePath), drop
   * this model's caches for older versions of the same catalog kind -- a
   * full catalog update makes them unreachable.
   */
  private _saveUserCache(centroids: Record<string, number[]>, path: string): void {
    this._generator.save(centroids, path);
    const file = basename(path);
    const match = /^(centroids_.+__(?:starter|full)-).+\.json$/.exec(file);
    if (!match) return;
    try {
      for (const entry of readdirSync(dirname(path))) {
        if (entry !== file && entry.startsWith(match[1]) && entry.endsWith('.json')) {
          unlinkSync(join(dirname(path), entry));
        }
      }
    } catch {
      // best effort
    }
  }

  /**
   * Get centroids, loading from best available source.
   *
   * Priority: memory > user cache > bundled static > generate fresh.
   *
   * Synchronous path -- if regeneration is needed (non-default model,
   * first run) and the provider is async-only, this will throw. Async
   * callers should use `getCentroidsAsync()` instead.
   */
  getCentroids(): Record<string, number[]> {
    const cached = this._tryLoadFromFiles();
    if (cached !== null) return cached;
    return this._regenerate();
  }

  /**
   * Async version of `getCentroids`. Works with any embedding provider --
   * if centroid regeneration is needed, routes through the provider's
   * async path.
   */
  async getCentroidsAsync(): Promise<Record<string, number[]>> {
    const cached = this._tryLoadFromFiles();
    if (cached !== null) return cached;
    return this._regenerateAsync();
  }

  /**
   * Try memory / user cache / bundled centroid file. Returns null if none
   * of these sources have a valid file for the current provider's model.
   * Shared between the sync and async load paths.
   */
  private _tryLoadFromFiles(): Record<string, number[]> | null {
    if (this._centroids !== null) return this._centroids;

    // 1. Try user's cached centroids
    if (this._userCachePath) {
      const loaded = this._tryLoad(this._userCachePath);
      if (loaded !== null) {
        this._centroids = loaded;
        return this._centroids;
      }
    }

    // 2. Try the catalog bundle's centroids (built for its embedding model)
    const loaded = this._tryBundle();
    if (loaded !== null) {
      this._centroids = loaded;
      return this._centroids;
    }

    return null;
  }

  private _tryLoad(path: string): Record<string, number[]> | null {
    if (!existsSync(path)) return null;

    try {
      const { centroids, metadata } = CentroidGenerator.load(path);
      return this._validated(centroids, metadata ?? {});
    } catch {
      return null;
    }
  }

  /** The catalog bundle's centroids, when they fit the current provider. */
  private _tryBundle(): Record<string, number[]> | null {
    const data = this._bundle.centroids;
    // Copy the vectors: callers (addBenchmarkCentroid) mutate the returned map.
    const centroids: Record<string, number[]> = {};
    for (const [name, vector] of Object.entries(data.centroids)) centroids[name] = [...vector];
    return this._validated(centroids, data.metadata ?? {});
  }

  /** `centroids` when they fit the provider and the catalog bundle, else null. */
  private _validated(
    centroids: Record<string, number[]>,
    metadata: { model?: string; dimension?: number },
  ): Record<string, number[]> | null {
    const savedModel = metadata.model ?? '';
    const savedDim = metadata.dimension ?? 0;

    // Model name must always match.
    if (savedModel !== this._provider.modelName) {
      return null;
    }

    // Dimension check: skip while the provider hasn't reported a real
    // dimension (it may still be returning a hardcoded fallback). Otherwise
    // a non-default-dimension file would be wrongly rejected/accepted.
    if (providerDimensionKnown(this._provider) && savedDim !== this._provider.dimension) {
      return null;
    }

    // Benchmark-set check: fingerprint the benchmarks actually present and
    // compare against the catalog bundle's set, so a user cache generated for
    // another catalog (or another benchmark set) is never used.
    if (benchmarkFingerprint(Object.keys(centroids)) !== defaultBenchmarkFingerprint(this._bundle)) {
      return null;
    }

    return centroids;
  }

  private _regenerate(): Record<string, number[]> {
    const centroids = this._generator.generate();

    // Save to user cache for future runs
    if (this._userCachePath) {
      this._saveUserCache(centroids, this._userCachePath);
    }

    this._centroids = centroids;
    return centroids;
  }

  private async _regenerateAsync(): Promise<Record<string, number[]>> {
    const centroids = await this._generator.generateAsync();

    if (this._userCachePath) {
      this._saveUserCache(centroids, this._userCachePath);
    }

    this._centroids = centroids;
    return centroids;
  }

  /**
   * Force regeneration of centroids.
   *
   * @param customQueries - Optional custom training queries. If undefined, uses defaults.
   */
  regenerate(customQueries?: Record<string, string[]>): Record<string, number[]> {
    const centroids = this._generator.generate(customQueries);

    if (this._userCachePath) {
      this._saveUserCache(centroids, this._userCachePath);
    }

    this._centroids = centroids;
    return centroids;
  }

  /**
   * Add a custom benchmark centroid to the existing set (sync).
   *
   * Requires a sync-capable embedding provider. Callers with async-only
   * providers (e.g. LocalEmbeddingProvider) should use
   * `addBenchmarkCentroidAsync()`.
   *
   * @param benchmarkName - Name of the new benchmark.
   * @param queries - Representative queries for this benchmark.
   * @returns The generated centroid vector.
   */
  addBenchmarkCentroid(benchmarkName: string, queries: string[]): number[] {
    const centroids = this.getCentroids();
    const newCentroid = this._generator.generateFromCustom(benchmarkName, queries);
    centroids[benchmarkName] = newCentroid;

    // Save updated centroids to user cache
    if (this._userCachePath) {
      this._saveUserCache(centroids, this._userCachePath);
    }

    return newCentroid;
  }

  /**
   * Async version of `addBenchmarkCentroid`. Works with any provider.
   * Mutates the in-memory centroid map so subsequent `getCentroids()` calls
   * (including from classifiers sharing this loader) see the new benchmark.
   */
  async addBenchmarkCentroidAsync(benchmarkName: string, queries: string[]): Promise<number[]> {
    const centroids = await this.getCentroidsAsync();
    const newCentroid = await this._generator.generateFromCustomAsync(benchmarkName, queries);
    centroids[benchmarkName] = newCentroid;

    if (this._userCachePath) {
      this._saveUserCache(centroids, this._userCachePath);
    }

    return newCentroid;
  }

  /** Remove a benchmark centroid. Returns true if it existed. */
  removeBenchmark(benchmarkName: string): boolean {
    const centroids = this.getCentroids();
    if (benchmarkName in centroids) {
      delete centroids[benchmarkName];
      if (this._userCachePath) {
        this._saveUserCache(centroids, this._userCachePath);
      }
      return true;
    }
    return false;
  }

  /** List all available benchmark names. */
  get availableBenchmarks(): string[] {
    return Object.keys(this.getCentroids());
  }
}
