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

/** The int8 ONNX MiniLM (the monitor's in-process MiniLM; modelStore.ts). */
const ONNX_MINILM_MODEL = 'all-MiniLM-L6-v2-onnx-int8';

/**
 * Which router classifies prompts (TRYAII_ROUTER): "h4" (default) -- the H4
 * static model + classification head (package tryaii-router-h4), Hebrew and
 * English; "minilm" -- the embedding model above against catalog centroids.
 * H4 falls back to MiniLM by itself when its package is missing or broken,
 * and applies only while embeddingModel is a default MiniLM (a custom
 * embedding model or provider means centroid routing). Same values as the
 * Python SDK's TryaiiDreConfig.router.
 */
export const ROUTER_ENV = 'TRYAII_ROUTER';
export const ROUTER_H4 = 'h4';
export const ROUTER_MINILM = 'minilm';
export type RouterChoice = typeof ROUTER_H4 | typeof ROUTER_MINILM;
export const DEFAULT_ROUTER: RouterChoice = ROUTER_H4;

const ROUTER_ALIASES: Record<string, RouterChoice> = {
  h4: ROUTER_H4,
  'h4-he-en': ROUTER_H4,
  minilm: ROUTER_MINILM,
  embedding: ROUTER_MINILM,
  centroids: ROUTER_MINILM,
  [ONNX_MINILM_MODEL.toLowerCase()]: ROUTER_MINILM,
};

const warnedRouterValues = new Set<string>();

/**
 * "h4" or "minilm" for a TRYAII_ROUTER value; empty -> the default; anything
 * unknown -> the default, with a warning (once per value per process).
 */
export function normalizeRouter(value: string | null | undefined): RouterChoice {
  const raw = (value ?? '').trim().toLowerCase();
  if (!raw) return DEFAULT_ROUTER;
  const router = ROUTER_ALIASES[raw];
  if (router === undefined) {
    if (!warnedRouterValues.has(raw)) {
      warnedRouterValues.add(raw);
      process.stderr.write(
        `tryaii: ${ROUTER_ENV}=${JSON.stringify(value)} is not a known router (h4, minilm); using ${DEFAULT_ROUTER}\n`,
      );
    }
    return DEFAULT_ROUTER;
  }
  return router;
}

/**
 * True when H4 is the requested router and applies: the default MiniLM
 * embedding model (the Router's `all-MiniLM-L6-v2` or the monitor's int8
 * ONNX copy), as Python's `TryaiiDreConfig.uses_h4`.
 */
export function usesH4(config: Pick<TryaiiDreConfig, 'router' | 'embeddingModel'>): boolean {
  const router = config.router ?? normalizeRouter(process.env[ROUTER_ENV]);
  return (
    router === ROUTER_H4 &&
    (config.embeddingModel === DEFAULT_EMBEDDING_MODEL || config.embeddingModel === ONNX_MINILM_MODEL)
  );
}

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

  /**
   * The router: "h4" (default) or "minilm" -- see ROUTER_ENV. Default:
   * TRYAII_ROUTER, else "h4".
   */
  router?: RouterChoice;
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
    router: normalizeRouter(overrides?.router ?? process.env[ROUTER_ENV]),
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
