/**
 * Main Router -- the primary public API for TryAii-DRE.
 *
 * Usage:
 *   import { Router } from 'tryaii-dre';
 *
 *   const router = new Router();
 *   const result = await router.route('Write a Python function to merge sorted arrays');
 *   console.log(result.bestModel);   // e.g., "gpt-5.2"
 *   console.log(result.scores);      // Top models with scores and reasoning
 *
 * The default `LocalEmbeddingProvider` is async-only, so `route()` itself is
 * async. Callers that have injected a sync embedding provider can use the
 * niche `routeSync()` method for a blocking call.
 */

import { BenchmarkRegistry, BenchmarkDefinition } from './benchmarks/registry.js';
import { NormalizationRange } from './scoring/benchmarks.js';
import { CentroidLoader } from './centroids/loader.js';
import { ClassificationResult } from './classifiers/base.js';
import { EmbeddingClassifier } from './classifiers/embedding.js';
import { TryaiiDreConfig, createDefaultConfig, centroidFilePath } from './config.js';
import { BaseEmbeddingProvider } from './embeddings/base.js';
import { LocalEmbeddingProvider } from './embeddings/local.js';
import { ModelRegistry, ModelInfo } from './registry/models.js';
import { ModelScore, ScoringEngine } from './scoring/engine.js';
import { DEFAULT_PRIORITIES, Priorities } from './scoring/priorities.js';
import type { LatencyTier } from './types.js';

/**
 * Result of routing a prompt.
 *
 * Contains the recommended model, all scored models, and classification details.
 */
export interface RouteResult {
  /** The top recommended model ID. */
  bestModel: string;

  /** All scored models (sorted by score descending). */
  scores: ModelScore[];

  /** Classification details (what kind of task was detected). */
  classification: ClassificationResult | null;

  /** Priorities used for this routing decision. */
  priorities: Priorities;
}

/** Get list of model IDs in ranked order from a RouteResult. */
export function routeResultTopK(result: RouteResult): string[] {
  return result.scores.map((s) => s.modelId);
}

/** Get the score of the top model. */
export function routeResultBestScore(result: RouteResult): number {
  return result.scores[0]?.finalScore ?? 0;
}

/** Get the reasoning for why the top model was chosen. */
export function routeResultBestReasoning(result: RouteResult): string {
  return result.scores[0]?.reasoning ?? '';
}

/** Options for route(). */
export interface RouteOptions {
  /** Quality/cost/speed priorities. Defaults to balanced. */
  priorities?: Priorities;
  /** Number of top models to return. */
  topK?: number;
  /** Only consider models from this provider. */
  filterProvider?: string;
  /** Only consider models with this capability. */
  filterCapability?: string;
  /** Only consider models cheaper than this (input $/1k tokens). */
  filterMaxCost?: number;
}

/**
 * Semantic AI model router.
 *
 * Analyzes user prompts using embeddings, matches them against benchmark
 * centroids, and recommends the best AI model based on benchmark performance,
 * pricing, latency, and user priorities.
 */
export class Router {
  private _config: TryaiiDreConfig;
  private _registry: ModelRegistry;
  private _benchmarkRegistry: BenchmarkRegistry;
  private _scoringEngine: ScoringEngine;
  private _embeddingProvider: BaseEmbeddingProvider | null;
  private _centroidLoader: CentroidLoader | null = null;
  private _classifier: EmbeddingClassifier | null = null;

  constructor(opts?: {
    config?: Partial<TryaiiDreConfig>;
    registry?: ModelRegistry;
    benchmarkRegistry?: BenchmarkRegistry;
    embeddingProvider?: BaseEmbeddingProvider;
  }) {
    this._config = createDefaultConfig(opts?.config);

    // Model registry
    this._registry = opts?.registry ?? ModelRegistry.default();

    // Benchmark registry
    this._benchmarkRegistry = opts?.benchmarkRegistry ?? BenchmarkRegistry.default();

    // Scoring engine with normalizer from benchmark registry
    const normalizer = this._benchmarkRegistry.getNormalizer();
    this._scoringEngine = new ScoringEngine(normalizer);

    // Embedding provider (lazy -- only initialized when needed)
    this._embeddingProvider = opts?.embeddingProvider ?? null;
  }

  /**
   * Lazy-initialize the embedding provider + centroid loader.
   *
   * The loader is stored on `this` so that `addBenchmark()` mutates the
   * same instance the classifier reads from -- custom benchmarks added
   * via the Router become visible to subsequent `route()` calls without
   * re-instantiating anything.
   */
  private _ensureCentroidLoader(): CentroidLoader {
    if (this._centroidLoader !== null) return this._centroidLoader;

    if (this._embeddingProvider === null) {
      this._embeddingProvider = new LocalEmbeddingProvider(
        `Xenova/${this._config.embeddingModel}`,
      );
    }

    this._centroidLoader = new CentroidLoader(
      this._embeddingProvider,
      centroidFilePath(this._config),
    );

    return this._centroidLoader;
  }

  /** Lazy-initialize the embedding classifier on first use. */
  private _ensureClassifier(): EmbeddingClassifier {
    if (this._classifier !== null) return this._classifier;

    const centroidLoader = this._ensureCentroidLoader();
    // `_embeddingProvider` is non-null after _ensureCentroidLoader returns.
    const provider = this._embeddingProvider as BaseEmbeddingProvider;

    this._classifier = new EmbeddingClassifier(
      provider,
      centroidLoader,
      {
        embeddingCacheSize: this._config.cache.embeddingCacheSize,
        classificationCacheSize: this._config.cache.classificationCacheSize,
        ttlSeconds: this._config.cache.ttlSeconds,
      },
    );

    return this._classifier;
  }

  /**
   * Route a prompt to the best AI model.
   *
   * Async by default -- works with any embedding provider, including the
   * default async `LocalEmbeddingProvider`. For a blocking call backed by a
   * sync provider, see `routeSync()`.
   *
   * @param prompt - The user's input text to classify and route.
   * @param opts - Routing options (priorities, filters, topK).
   * @returns RouteResult with the best model and full scoring breakdown.
   */
  async route(prompt: string, opts?: RouteOptions): Promise<RouteResult> {
    const classifier = this._ensureClassifier();
    const classification = await classifier.classifyAsync(prompt);
    return this._buildResult(classification, opts);
  }

  /**
   * Synchronous version of `route()`.
   *
   * Requires the injected embedding provider to support sync calls
   * (`supportsSync === true`). Throws otherwise -- the default
   * `LocalEmbeddingProvider` is async-only, so calling `routeSync()` on a
   * default `Router` will fail. Inject a sync provider (e.g. a custom
   * cached provider) to use this path.
   */
  routeSync(prompt: string, opts?: RouteOptions): RouteResult {
    const classifier = this._ensureClassifier();
    if (this._embeddingProvider !== null && !this._embeddingProvider.supportsSync) {
      throw new Error(
        `routeSync() requires an embedding provider that supports sync calls. ` +
          `${this._embeddingProvider.constructor.name} is async-only -- ` +
          `use route() (async) instead, or inject a sync provider.`,
      );
    }
    const classification = classifier.classify(prompt);
    return this._buildResult(classification, opts);
  }

  /** Shared post-classification path: filter models, score, return RouteResult. */
  private _buildResult(
    classification: ClassificationResult,
    opts?: RouteOptions,
  ): RouteResult {
    const priorities = opts?.priorities ?? DEFAULT_PRIORITIES;
    const topK = opts?.topK ?? 5;

    let models = this._registry.allModels;
    if (opts?.filterProvider) {
      const providerLower = opts.filterProvider.toLowerCase();
      models = models.filter((m) => m.provider.toLowerCase() === providerLower);
    }
    if (opts?.filterCapability) {
      const cap = opts.filterCapability;
      models = models.filter((m) => m.capabilities.includes(cap));
    }
    if (opts?.filterMaxCost != null) {
      const maxCost = opts.filterMaxCost;
      models = models.filter((m) => m.pricing != null && m.pricing.inputPer1k <= maxCost);
    }

    if (models.length === 0) {
      return {
        bestModel: '',
        scores: [],
        classification,
        priorities,
      };
    }

    const scores = this._scoringEngine.scoreModels(
      models,
      classification.benchmarkScores,
      priorities,
      topK,
    );

    const best = scores[0]?.modelId ?? '';

    return {
      bestModel: best,
      scores,
      classification,
      priorities,
    };
  }

  /**
   * Shortcut to add a model to the registry.
   *
   * @see ModelRegistry.add()
   */
  addModel(opts: {
    modelId: string;
    provider: string;
    benchmarks?: Record<string, number>;
    pricing?: [number, number];
    latency?: LatencyTier;
    capabilities?: string[];
    description?: string;
  }): ModelInfo {
    return this._registry.add(opts);
  }

  /**
   * Add a custom benchmark to the routing system.
   *
   * Async by default -- works with any embedding provider, including the
   * async-only `LocalEmbeddingProvider`. The new centroid is generated
   * immediately and added to the shared centroid loader, so subsequent
   * `route()` calls see it without any restart.
   *
   * For sync-provider callers who want a blocking setup step, use
   * `addBenchmarkSync()`.
   *
   * @param name - Benchmark name (e.g., "CustomerSupportQA").
   * @param queries - Representative prompts for this benchmark (10-20 recommended).
   * @param description - Human-readable description.
   * @param minScore - Minimum score for normalization.
   * @param maxScore - Maximum score for normalization.
   */
  async addBenchmark(
    name: string,
    queries: string[],
    description = '',
    minScore = 0,
    maxScore = 100,
  ): Promise<void> {
    this._registerBenchmark(name, queries, description, minScore, maxScore);
    const loader = this._ensureCentroidLoader();
    await loader.addBenchmarkCentroidAsync(name, queries);
  }

  /**
   * Synchronous version of `addBenchmark()`.
   *
   * Requires the injected embedding provider to support sync calls
   * (`supportsSync === true`). The default `LocalEmbeddingProvider` is
   * async-only, so calling `addBenchmarkSync()` on a default `Router`
   * will throw.
   */
  addBenchmarkSync(
    name: string,
    queries: string[],
    description = '',
    minScore = 0,
    maxScore = 100,
  ): void {
    this._registerBenchmark(name, queries, description, minScore, maxScore);
    const loader = this._ensureCentroidLoader();
    if (this._embeddingProvider !== null && !this._embeddingProvider.supportsSync) {
      throw new Error(
        `addBenchmarkSync() requires an embedding provider that supports sync calls. ` +
          `${this._embeddingProvider.constructor.name} is async-only -- ` +
          `use addBenchmark() (async) instead, or inject a sync provider.`,
      );
    }
    loader.addBenchmarkCentroid(name, queries);
  }

  /** Register a benchmark in the registry and rebuild the scoring normalizer. */
  private _registerBenchmark(
    name: string,
    queries: string[],
    description: string,
    minScore: number,
    maxScore: number,
  ): void {
    const benchmark: BenchmarkDefinition = {
      name,
      description,
      trainingQueries: queries,
      normalization: new NormalizationRange(minScore, maxScore, description),
      broadCategory: 'TECHNICAL',
      subcategories: [],
      metadata: {},
    };
    this._benchmarkRegistry.register(benchmark);

    const normalizer = this._benchmarkRegistry.getNormalizer();
    this._scoringEngine = new ScoringEngine(normalizer);
  }

  /** Access the model registry. */
  get models(): ModelRegistry {
    return this._registry;
  }

  /** Access the benchmark registry. */
  get benchmarks(): BenchmarkRegistry {
    return this._benchmarkRegistry;
  }

  /** Access the configuration. */
  get config(): TryaiiDreConfig {
    return this._config;
  }
}
