/**
 * Main Router -- the primary public API for TryAii-DRE.
 *
 * Usage:
 *   import { Router } from 'tryaii-dre';
 *
 *   const router = new Router();
 *   const result = router.route('Write a Python function to merge sorted arrays');
 *   console.log(result.bestModel);   // e.g., "gpt-5.2"
 *   console.log(result.scores);      // Top models with scores and reasoning
 */

import { BenchmarkRegistry, BenchmarkDefinition } from './benchmarks/registry.js';
import { NormalizationRange } from './scoring/benchmarks.js';
import { CentroidLoader } from './centroids/loader.js';
import { BaseClassifier, ClassificationResult } from './classifiers/base.js';
import { EmbeddingClassifier } from './classifiers/embedding.js';
import { HybridClassifier } from './classifiers/hybrid.js';
import { KeywordClassifier } from './classifiers/keyword.js';
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
  private _classifier: BaseClassifier | null = null;

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

  /** Lazy-initialize the classifier on first use. */
  private _ensureClassifier(): BaseClassifier {
    if (this._classifier !== null) return this._classifier;

    const keywordClassifier = new KeywordClassifier();

    if (this._config.classifier === 'keyword') {
      this._classifier = keywordClassifier;
      return this._classifier;
    }

    // Initialize embedding provider
    if (this._embeddingProvider === null) {
      this._embeddingProvider = new LocalEmbeddingProvider(
        `Xenova/${this._config.embeddingModel}`,
      );
    }

    // Initialize centroid loader
    const centroidLoader = new CentroidLoader(
      this._embeddingProvider,
      centroidFilePath(this._config),
    );

    const embeddingClassifier = new EmbeddingClassifier(
      this._embeddingProvider,
      centroidLoader,
      {
        embeddingCacheSize: this._config.cache.embeddingCacheSize,
        classificationCacheSize: this._config.cache.classificationCacheSize,
        ttlSeconds: this._config.cache.ttlSeconds,
      },
    );
    this._classifier = new HybridClassifier(
      embeddingClassifier,
      keywordClassifier,
      this._config.confidenceThreshold,
    );

    return this._classifier;
  }

  /**
   * Route a prompt to the best AI model.
   *
   * @param prompt - The user's input text to classify and route.
   * @param opts - Routing options (priorities, filters, topK).
   * @returns RouteResult with the best model and full scoring breakdown.
   */
  route(prompt: string, opts?: RouteOptions): RouteResult {
    const priorities = opts?.priorities ?? DEFAULT_PRIORITIES;
    const topK = opts?.topK ?? 5;

    // 1. Classify the prompt
    const classifier = this._ensureClassifier();
    const classification = classifier.classify(prompt);

    // 2. Get available models (with optional filters)
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

    // 3. Score and rank models
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
   * Route using only the keyword classifier (no embeddings needed).
   *
   * Useful for environments without @xenova/transformers installed,
   * or when you need instant results without model loading time.
   */
  routeKeywordOnly(prompt: string, opts?: { priorities?: Priorities; topK?: number }): RouteResult {
    const priorities = opts?.priorities ?? DEFAULT_PRIORITIES;
    const topK = opts?.topK ?? 5;

    const keywordClassifier = new KeywordClassifier();
    const classification = keywordClassifier.classify(prompt);

    const scores = this._scoringEngine.scoreModels(
      this._registry.allModels,
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
   * @param name - Benchmark name (e.g., "CustomerSupportQA").
   * @param queries - Representative prompts for this benchmark (10-20 recommended).
   * @param description - Human-readable description.
   * @param minScore - Minimum score for normalization.
   * @param maxScore - Maximum score for normalization.
   */
  addBenchmark(
    name: string,
    queries: string[],
    description = '',
    minScore = 0,
    maxScore = 100,
  ): void {
    // Register in benchmark registry
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

    // Update scoring engine normalizer
    const normalizer = this._benchmarkRegistry.getNormalizer();
    this._scoringEngine = new ScoringEngine(normalizer);

    // Generate centroid if classifier is initialized
    if (this._classifier !== null && this._embeddingProvider !== null) {
      const centroidLoader = new CentroidLoader(
        this._embeddingProvider,
        centroidFilePath(this._config),
      );
      centroidLoader.addBenchmarkCentroid(name, queries);
    }
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
