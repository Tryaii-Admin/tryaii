/**
 * Centroid loader -- handles lazy initialization and model compatibility.
 *
 * Loading priority:
 *   1. In-memory cache (already loaded)
 *   2. User's cache directory (previously generated for their model)
 *   3. Bundled static file (ships with package for default model -- zero delay)
 *   4. Generate from training queries (only if using a non-default model)
 */

import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { BaseEmbeddingProvider } from '../embeddings/base.js';
import { CentroidGenerator } from './generator.js';

const currentDir = dirname(fileURLToPath(import.meta.url));

/** Path to bundled centroids (ships with the package). */
const BUNDLED_CENTROIDS_DIR = join(currentDir, 'data');

/** Get path to the bundled centroid file for a given model. */
function bundledCentroidPath(modelName: string): string {
  const safeName = modelName.replace(/\//g, '__');
  return join(BUNDLED_CENTROIDS_DIR, `centroids_${safeName}.json`);
}

/**
 * Manages centroid lifecycle: load, validate, regenerate.
 *
 * For the default embedding model (all-MiniLM-L6-v2), centroids are
 * bundled with the package -- zero first-run delay. For other models,
 * centroids are generated on first use and cached to disk.
 */
export class CentroidLoader {
  private _provider: BaseEmbeddingProvider;
  private _centroids: Record<string, number[]> | null = null;
  private _generator: CentroidGenerator;
  private _userCachePath: string | null;

  constructor(
    embeddingProvider: BaseEmbeddingProvider,
    userCachePath?: string,
  ) {
    this._provider = embeddingProvider;
    this._generator = new CentroidGenerator(embeddingProvider);
    this._userCachePath = userCachePath ?? null;
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

    // 2. Try bundled static centroids (ships with package)
    const bundledPath = bundledCentroidPath(this._provider.modelName);
    const loaded = this._tryLoad(bundledPath);
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

      const savedModel = metadata.model ?? '';
      const savedDim = metadata.dimension ?? 0;

      if (savedModel === this._provider.modelName && savedDim === this._provider.dimension) {
        return centroids;
      }
      // Model mismatch -- skip this file
      return null;
    } catch {
      return null;
    }
  }

  private _regenerate(): Record<string, number[]> {
    const centroids = this._generator.generate();

    // Save to user cache for future runs
    if (this._userCachePath) {
      this._generator.save(centroids, this._userCachePath);
    }

    this._centroids = centroids;
    return centroids;
  }

  private async _regenerateAsync(): Promise<Record<string, number[]>> {
    const centroids = await this._generator.generateAsync();

    if (this._userCachePath) {
      this._generator.save(centroids, this._userCachePath);
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
      this._generator.save(centroids, this._userCachePath);
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
      this._generator.save(centroids, this._userCachePath);
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
      this._generator.save(centroids, this._userCachePath);
    }

    return newCentroid;
  }

  /** Remove a benchmark centroid. Returns true if it existed. */
  removeBenchmark(benchmarkName: string): boolean {
    const centroids = this.getCentroids();
    if (benchmarkName in centroids) {
      delete centroids[benchmarkName];
      if (this._userCachePath) {
        this._generator.save(centroids, this._userCachePath);
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
