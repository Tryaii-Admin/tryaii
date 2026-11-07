/**
 * Router selection: H4 (default) or MiniLM centroids (fallback). Mirrors
 * packages/python/tryaii/classifiers/selection.py.
 *
 * `TRYAII_ROUTER` (`TryaiiDreConfig.router`) picks the router: `h4` (default)
 * or `minilm`. H4 applies only with the default embedding model and no custom
 * embedding provider; when its payload (package `tryaii-router-h4` or
 * `TRYAII_ROUTER_MODEL_PATH`) is missing or broken, routing falls back to
 * MiniLM by itself. The fallback is recorded in `<data dir>/router-model.json`
 * and written to stderr at most once a day (`tryaii monitor status` shows it);
 * the state is dropped as soon as H4 loads again.
 *
 * H4 scores only the benchmarks of the loaded catalog (the other classes are
 * masked and the rest renormalised, see head.ts). A catalog that has none of
 * its classes cannot be routed by H4: the Router uses MiniLM for it and warns
 * (once per process and catalog).
 */

import type { CatalogBundle } from '../catalog/bundle.js';
import { usesH4, type TryaiiDreConfig } from '../config.js';
import {
  DEFAULT_ONNX_MODEL,
  DEFAULT_ROUTER_MODEL,
  ENV_ROUTER_MODEL_PATH,
  ROUTERS,
  ROUTER_STATE_FILE,
  RouterUnavailableError,
  clearUnavailable,
  lastRouterError,
  reportUnavailable,
  resolveRouterDir,
} from '../embeddings/modelStore.js';
import { StaticEmbeddingProvider } from '../embeddings/static.js';
import { HeadClassifier, type HeadClassifierOptions } from './head.js';

type Log = (msg: string) => void;

const stderrLog: Log = (msg) => {
  process.stderr.write(`${msg}\n`);
};

export function fallbackMessage(reason: string): string {
  const spec = ROUTERS[DEFAULT_ROUTER_MODEL];
  return (
    `tryaii: router model ${spec.routerId} is not available (${reason}); ` +
    `routing with ${DEFAULT_ONNX_MODEL} instead. ` +
    `Fix: npm install ${spec.distribution}, or set ` +
    `${ENV_ROUTER_MODEL_PATH} to a copy of its directory. ` +
    'Shown at most once a day; see: tryaii monitor status'
  );
}

/** Record + (throttled) report that H4 is unavailable. Never throws. */
export function recordH4Unavailable(config: TryaiiDreConfig, reason: string, warn: Log = stderrLog): void {
  try {
    reportUnavailable(DEFAULT_ROUTER_MODEL, reason, {
      dataDir: config.dataDir,
      warn,
      message: fallbackMessage(reason),
      stateFile: ROUTER_STATE_FILE,
    });
  } catch {
    /* reporting must never break routing */
  }
}

export function recordH4Available(config: TryaiiDreConfig): void {
  try {
    clearUnavailable(config.dataDir, ROUTER_STATE_FILE);
  } catch {
    /* ignore */
  }
}

/**
 * Cheap check (no load): is a verified H4 payload in place? Records the state
 * either way. False when H4 is not the configured router.
 */
export function h4Ready(config: TryaiiDreConfig, warn: Log = stderrLog): boolean {
  if (!usesH4(config)) return false;
  let path: string | null;
  try {
    path = resolveRouterDir(DEFAULT_ROUTER_MODEL);
  } catch {
    path = null;
  }
  if (path === null) {
    recordH4Unavailable(config, lastRouterError() ?? 'not found', warn);
    return false;
  }
  recordH4Available(config);
  return true;
}

/**
 * The H4 classifier, loaded and verified, or null (state recorded) when the
 * payload is missing or broken -- or when the catalog has none of its classes
 * (warned once per process and catalog).
 */
export function loadH4Classifier(
  config: TryaiiDreConfig,
  bundle: CatalogBundle | null = null,
  opts: Omit<HeadClassifierOptions, 'bundle'> = {},
  warn: Log = stderrLog,
): HeadClassifier | null {
  let classifier: HeadClassifier;
  try {
    classifier = new HeadClassifier(new StaticEmbeddingProvider(), { ...opts, bundle }).load();
  } catch (err) {
    const reason =
      err instanceof RouterUnavailableError
        ? err.reason
        : `${err instanceof Error ? err.name : 'Error'}: ${err instanceof Error ? err.message : String(err)}`;
    recordH4Unavailable(config, reason, warn);
    return null;
  }
  recordH4Available(config);
  if (classifier.keptClasses.length === 0) {
    warnNoClasses(bundle, warn);
    return null;
  }
  return classifier;
}

const warnedNoClasses = new Set<string>();

function warnNoClasses(bundle: CatalogBundle | null, warn: Log): void {
  const key = bundle ? `${bundle.kind}-${bundle.version}` : '';
  if (warnedNoClasses.has(key)) return;
  warnedNoClasses.add(key);
  warn(
    `tryaii: none of the benchmarks the router model ${DEFAULT_ROUTER_MODEL} scores is in ` +
      `${key || 'the catalog'}; routing with ${DEFAULT_ONNX_MODEL} instead`,
  );
}

/** Forget the once-per-process warnings (tests). */
export function resetSelectionWarnings(): void {
  warnedNoClasses.clear();
}
