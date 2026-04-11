/**
 * Shared type definitions for TryAii-DRE.
 */

/** Latency tier for a model. */
export type LatencyTier = 'very fast' | 'fast' | 'medium' | 'slow' | 'very slow';

/** Scoring strategy preset name. */
export type ScoringStrategy = 'balanced' | 'performance' | 'cost' | 'speed';

/** Pricing per 1k tokens in USD. */
export interface ModelPricingData {
  input_per_1k: number;
  output_per_1k: number;
}

/** Raw model data as stored in JSON. */
export interface ModelData {
  model_id: string;
  provider: string;
  benchmark_scores?: Record<string, number | null>;
  capabilities?: string[];
  pricing?: ModelPricingData | null;
  latency?: LatencyTier | null;
  description?: string;
}

/** Models JSON file structure. */
export interface ModelsJson {
  version?: string;
  updated?: string;
  models: ModelData[];
}

/** Training queries JSON file structure. */
export interface TrainingQueriesJson {
  version?: string;
  description?: string;
  benchmarks: Record<string, {
    description: string;
    queries: string[];
  }>;
}

/** Centroids JSON file structure. */
export interface CentroidsJson {
  metadata: {
    model: string;
    dimension: number;
    benchmark_count: number;
  };
  centroids: Record<string, number[]>;
}
