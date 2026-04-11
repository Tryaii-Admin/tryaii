/**
 * TryAii-DRE -- Embedding-based AI Model Router
 *
 * Understands your prompt semantically and routes to the best model
 * based on benchmarks, cost, speed, and quality priorities.
 *
 * Usage:
 *   import { Router } from 'tryaii-dre';
 *
 *   const router = new Router();
 *   const result = await router.route('Write a Python function to merge sorted arrays');
 *   console.log(result.bestModel);
 *   console.log(result.scores);
 */

// Core router
export { Router, RouteResult, RouteOptions, routeResultTopK, routeResultBestScore, routeResultBestReasoning } from './router.js';

// Integrations
export { OpenRouterIntegration, OpenRouterResponse, OpenRouterChatOptions, MODEL_ID_TO_OPENROUTER } from './integrations/index.js';

// Configuration
export { TryaiiDreConfig, CacheConfig, createDefaultConfig, DEFAULT_DATA_DIR, DEFAULT_EMBEDDING_MODEL } from './config.js';

// Registry
export { ModelRegistry, ModelInfo, ModelPricing } from './registry/index.js';

// Scoring
export { ScoringEngine, ModelScore, SPEED_SCORES } from './scoring/engine.js';
export { Priorities, DEFAULT_PRIORITIES } from './scoring/priorities.js';
export type { PrioritiesData } from './scoring/priorities.js';
export { BenchmarkNormalizer, NormalizationRange, NORMALIZATION_RANGES } from './scoring/benchmarks.js';

// Classifiers
export { BaseClassifier, ClassificationResult, emptyClassificationResult, topBenchmarks } from './classifiers/base.js';
export { EmbeddingClassifier } from './classifiers/embedding.js';

// Embeddings
export { BaseEmbeddingProvider } from './embeddings/base.js';
export { LocalEmbeddingProvider } from './embeddings/local.js';

// Centroids
export { CentroidGenerator } from './centroids/generator.js';
export { CentroidLoader } from './centroids/loader.js';

// Benchmarks
export { BenchmarkRegistry, BenchmarkDefinition } from './benchmarks/registry.js';
export { STANDARD_BENCHMARKS } from './benchmarks/standard.js';

// Types
export type { LatencyTier, ScoringStrategy, ModelData, ModelsJson, CentroidsJson, TrainingQueriesJson } from './types.js';
