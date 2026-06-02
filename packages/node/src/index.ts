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
export { Router, routeResultTopK, routeResultBestScore, routeResultBestReasoning } from './router.js';
export type { RouteResult, RouteOptions } from './router.js';

// High-level client
export { DREClient } from './client.js';
export type {
  ChatOptions,
  ChatResponse,
  DREClientOptions,
  ModelScore as ClientModelScore,
  Priorities as ClientPriorities,
  RouteOptions as ClientRouteOptions,
  RouteResult as ClientRouteResult,
  TokenUsage,
} from './client-types.js';

// Integrations
export { OpenRouterIntegration, MODEL_ID_TO_OPENROUTER } from './integrations/index.js';
export type { OpenRouterResponse, OpenRouterChatOptions } from './integrations/index.js';

// Configuration
export { createDefaultConfig, DEFAULT_DATA_DIR, DEFAULT_EMBEDDING_MODEL } from './config.js';
export type { TryaiiDreConfig, CacheConfig } from './config.js';

// Registry
export { ModelRegistry, ModelInfo, ModelPricing } from './registry/index.js';

// Scoring
export { ScoringEngine, SPEED_SCORES } from './scoring/engine.js';
export type { ModelScore } from './scoring/engine.js';
export { Priorities, DEFAULT_PRIORITIES } from './scoring/priorities.js';
export type { PrioritiesData } from './scoring/priorities.js';
export { BenchmarkNormalizer, NormalizationRange, NORMALIZATION_RANGES } from './scoring/benchmarks.js';

// Classifiers
export { BaseClassifier, emptyClassificationResult, topBenchmarks } from './classifiers/base.js';
export type { ClassificationResult } from './classifiers/base.js';
export { EmbeddingClassifier } from './classifiers/embedding.js';

// Embeddings
export { BaseEmbeddingProvider } from './embeddings/base.js';
export { LocalEmbeddingProvider } from './embeddings/local.js';

// Centroids
export { CentroidGenerator } from './centroids/generator.js';
export { CentroidLoader } from './centroids/loader.js';

// Benchmarks
export { BenchmarkRegistry } from './benchmarks/registry.js';
export type { BenchmarkDefinition } from './benchmarks/registry.js';
export { STANDARD_BENCHMARKS } from './benchmarks/standard.js';

// Types
export type { LatencyTier, ScoringStrategy, ModelData, ModelsJson, CentroidsJson, TrainingQueriesJson } from './types.js';

// Dashboard (HTML report generator for eval runs)
export { renderDashboard } from './dashboard/index.js';
export type { DashboardSummary, DashboardLinks } from './dashboard/index.js';

// Budget optimization
export {
  batchPercentileRanks,
  computeDifficulty,
  costUnitForBudget,
  DEFAULT_DIFFICULTY_GAMMA,
  DEFAULT_DIFFICULTY_SOURCE,
  estimateGenerationCost,
  estimateTokens,
  optimizeBudgetCandidates,
  paretoPrune,
  resolveDifficulty,
  routeDatasetWithBudget,
} from './budget.js';
export type {
  BudgetCandidate,
  BudgetedRouteResult,
  BudgetMode,
  BudgetOptimizationResult,
  DifficultySource,
  RouteDatasetWithBudgetOptions,
} from './budget.js';
