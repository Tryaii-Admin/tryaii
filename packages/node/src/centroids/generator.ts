/**
 * Centroid generator -- creates benchmark centroids from training queries.
 *
 * Centroids are the average embedding of all training queries for a benchmark.
 * They are used by the EmbeddingClassifier to measure how similar a user's
 * prompt is to each benchmark category.
 *
 * Centroids are regenerated when the embedding model changes, because different
 * models produce different vector spaces.
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { BaseEmbeddingProvider } from '../embeddings/base.js';
import type { CentroidsJson, TrainingQueriesJson } from '../types.js';
import { vectorMean, vectorNormalize } from '../utils/math.js';

const currentDir = dirname(fileURLToPath(import.meta.url));

/** Path to bundled training queries. */
export const TRAINING_QUERIES_PATH = join(currentDir, 'data', 'trainingQueries.json');

/**
 * Generates and manages benchmark centroids.
 *
 * Centroids are the average embedding vector of representative queries
 * for each benchmark. When a user sends a prompt, we compute cosine
 * similarity between their prompt's embedding and each centroid to
 * determine what kind of task they're asking about.
 */
export class CentroidGenerator {
  private _provider: BaseEmbeddingProvider;

  constructor(embeddingProvider: BaseEmbeddingProvider) {
    this._provider = embeddingProvider;
  }

  /**
   * Generate centroids from training queries.
   *
   * @param trainingQueries - Dict of benchmark_name -> list of queries.
   *                          If undefined, uses bundled default queries.
   * @returns Dict of benchmark_name -> centroid vector (number array).
   */
  generate(trainingQueries?: Record<string, string[]>): Record<string, number[]> {
    if (!trainingQueries) {
      trainingQueries = this._loadDefaultQueries();
    }

    const centroids: Record<string, number[]> = {};

    for (const [benchmark, queries] of Object.entries(trainingQueries)) {
      // Embed all queries for this benchmark
      const embeddings = this._provider.embedBatch(queries);

      // Centroid = average of all embeddings, then normalize
      const centroid = vectorNormalize(vectorMean(embeddings));
      centroids[benchmark] = centroid;
    }

    return centroids;
  }

  /**
   * Async version of `generate` -- routes through the provider's async path
   * so it works with async-only providers like LocalEmbeddingProvider.
   * Sync providers work too via the base class's default async fallback.
   */
  async generateAsync(trainingQueries?: Record<string, string[]>): Promise<Record<string, number[]>> {
    if (!trainingQueries) {
      trainingQueries = this._loadDefaultQueries();
    }

    const centroids: Record<string, number[]> = {};

    for (const [benchmark, queries] of Object.entries(trainingQueries)) {
      const embeddings = await this._provider.embedBatchAsync(queries);
      const centroid = vectorNormalize(vectorMean(embeddings));
      centroids[benchmark] = centroid;
    }

    return centroids;
  }

  /**
   * Generate a single centroid for a custom benchmark.
   *
   * @param benchmarkName - Name of the benchmark.
   * @param queries - Representative queries for this benchmark.
   * @returns Centroid vector (number array).
   */
  generateFromCustom(benchmarkName: string, queries: string[]): number[] {
    const embeddings = this._provider.embedBatch(queries);
    return vectorNormalize(vectorMean(embeddings));
  }

  /**
   * Async version of `generateFromCustom`. Works with any provider
   * (sync via default fallback, async via its native async path).
   */
  async generateFromCustomAsync(benchmarkName: string, queries: string[]): Promise<number[]> {
    const embeddings = await this._provider.embedBatchAsync(queries);
    return vectorNormalize(vectorMean(embeddings));
  }

  /** Save centroids to a JSON file. */
  save(centroids: Record<string, number[]>, path: string): void {
    mkdirSync(dirname(path), { recursive: true });

    const data: CentroidsJson = {
      metadata: {
        model: this._provider.modelName,
        dimension: this._provider.dimension,
        benchmark_count: Object.keys(centroids).length,
      },
      centroids,
    };

    writeFileSync(path, JSON.stringify(data));
  }

  /** Load centroids from a JSON file. */
  static load(path: string): { centroids: Record<string, number[]>; metadata: CentroidsJson['metadata'] } {
    const raw = readFileSync(path, 'utf-8');
    const data: CentroidsJson = JSON.parse(raw);

    return {
      centroids: data.centroids,
      metadata: data.metadata,
    };
  }

  /** Load bundled training queries. */
  private _loadDefaultQueries(): Record<string, string[]> {
    const raw = readFileSync(TRAINING_QUERIES_PATH, 'utf-8');
    const data: TrainingQueriesJson = JSON.parse(raw);

    const queries: Record<string, string[]> = {};
    for (const [name, benchData] of Object.entries(data.benchmarks)) {
      queries[name] = benchData.queries;
    }
    return queries;
  }
}
