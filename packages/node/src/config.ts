/**
 * Global configuration for TryAii.
 */

import { homedir } from 'node:os';
import { join } from 'node:path';

import type { ScoringStrategy } from './types.js';

/** Default data directory: ~/.tryaii/ */
export const DEFAULT_DATA_DIR = join(homedir(), '.tryaii');

/** Default embedding model -- small, fast, runs on any modern CPU. */
export const DEFAULT_EMBEDDING_MODEL = 'all-MiniLM-L6-v2';

/** Default embedding dimension for all-MiniLM-L6-v2. */
export const DEFAULT_EMBEDDING_DIMENSION = 384;

/** Cache configuration. */
export interface CacheConfig {
  /** Max number of cached embedding vectors. */
  embeddingCacheSize: number;
  /** Max number of cached classification results. */
  classificationCacheSize: number;
  /** Time-to-live for cache entries in seconds. */
  ttlSeconds: number;
}

/**
 * Main configuration object.
 *
 * Can be passed to Router() to override defaults.
 */
export interface TryaiiDreConfig {
  /** Embedding model name (sentence-transformers / HuggingFace model). */
  embeddingModel: string;

  /** Where to store centroids, cached models, etc. */
  dataDir: string;

  /** Cache settings. */
  cache: CacheConfig;

  /** Scoring strategy preset. */
  strategy: ScoringStrategy;

  /** OpenAI API key (only needed if using OpenAI embeddings). */
  openaiApiKey: string | undefined;

  /** OpenRouter API key (only needed for active routing integration). */
  openrouterApiKey: string | undefined;
}

/** Create a TryaiiDreConfig with safe defaults. */
export function createDefaultConfig(overrides?: Partial<TryaiiDreConfig>): TryaiiDreConfig {
  return {
    embeddingModel: DEFAULT_EMBEDDING_MODEL,
    dataDir: DEFAULT_DATA_DIR,
    cache: {
      embeddingCacheSize: 300,
      classificationCacheSize: 150,
      ttlSeconds: 300,
    },
    strategy: 'balanced',
    openaiApiKey: undefined,
    openrouterApiKey: undefined,
    ...overrides,
  };
}

/** Get the centroids directory path from config. */
export function centroidsDir(config: TryaiiDreConfig): string {
  return join(config.dataDir, 'centroids');
}

/**
 * User centroid cache path for the current embedding model AND catalog.
 *
 * With a bundle it is keyed by the catalog's kind + version too
 * (`centroids_<model>__<kind>-<version>.json`), so routing alternately on the
 * starter and the full catalog never makes one overwrite (and regenerate) the
 * other's cache. Without one: the unkeyed legacy name. Same names as the
 * Python SDK's `TryaiiDreConfig.centroid_file_for`.
 */
export function centroidFilePath(
  config: TryaiiDreConfig,
  bundle?: { kind: string; version: string } | null,
): string {
  const safeName = config.embeddingModel.replace(/\//g, '__');
  if (!bundle) return join(centroidsDir(config), `centroids_${safeName}.json`);
  return join(centroidsDir(config), `centroids_${safeName}__${bundle.kind}-${bundle.version}.json`);
}
