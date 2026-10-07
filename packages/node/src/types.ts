/**
 * Shared type definitions for TryAii.
 */

/**
 * Latency tier for a model. "unknown" is shipped for catalog models whose
 * provider publishes no speed data. This is a *display* tier only: the scoring
 * engine derives speed from `tokens_per_second` and `ttft_ms`, and falls back to
 * the catalog p25 of `U_s` when throughput is missing.
 */
export type LatencyTier = 'very fast' | 'fast' | 'medium' | 'slow' | 'very slow' | 'unknown';

/** Scoring strategy preset name. */
export type ScoringStrategy = 'balanced' | 'performance' | 'cost' | 'speed';

/** Pricing per 1k tokens in USD. Null components mean the price is unknown. */
export interface ModelPricingData {
  input_per_1k: number | null;
  output_per_1k: number | null;
}

/** Raw model data as stored in JSON. */
export interface ModelData {
  model_id: string;
  provider: string;
  benchmark_scores?: Record<string, number | null>;
  capabilities?: string[];
  pricing?: ModelPricingData | null;
  latency?: LatencyTier | null;
  /** Measured output throughput (tokens/second). Absent or null when unknown. */
  tokens_per_second?: number | null;
  /** Measured time to first token (milliseconds). Absent or null when unknown. */
  ttft_ms?: number | null;
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
