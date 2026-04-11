/**
 * Global configuration for TryAii-DRE.
 */

import { homedir } from 'node:os';
import { join } from 'node:path';

import type { ScoringStrategy } from './types.js';

/** Default data directory: ~/.tryaii_dre/ */
export const DEFAULT_DATA_DIR = join(homedir(), '.tryaii_dre');

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

/** Get the centroid file path for the current embedding model. */
export function centroidFilePath(config: TryaiiDreConfig): string {
  const safeName = config.embeddingModel.replace(/\//g, '__');
  return join(centroidsDir(config), `centroids_${safeName}.json`);
}
