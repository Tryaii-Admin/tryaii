/**
 * Model registry -- stores metadata about AI models.
 *
 * Each model has benchmark scores, pricing, latency, and capabilities.
 * The default registry is the active catalog bundle's models.json (the
 * packaged starter catalog unless another bundle is given -- see
 * `catalog/bundle.ts`); users can add/remove/override.
 */

import { isImplausibleBenchmarkScore } from '../scoring/benchmarks.js';
import { CatalogBundle, resolveBundle } from '../catalog/bundle.js';
import type { BundleLike } from '../catalog/bundle.js';
import type { CatalogMode } from '../catalog/client.js';
import type { LatencyTier, ModelData, ModelsJson } from '../types.js';

/** OpenRouter marks ephemeral free-tier variants with a ":free" id suffix. */
export const FREE_TIER_SUFFIX = ':free';

/**
 * True for ephemeral free-tier model variants (OpenRouter `:free` ids).
 *
 * Free tiers come and go -- often within days -- so regular routing ignores
 * them entirely: they are excluded from the default registry and never scored.
 * The preset data still ships them so a future dedicated free-models feature
 * can opt in via `loadPreset(name, { includeFree: true })`.
 */
export function isFreeTier(modelId: string): boolean {
  return modelId.endsWith(FREE_TIER_SUFFIX);
}

/**
 * Accept a `tokens_per_second` value only when it is a finite number > 0;
 * anything else (null, NaN, Infinity, 0, negatives, strings) means "unknown".
 */
function parseTokensPerSecond(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : undefined;
}

/**
 * Accept a `ttft_ms` value only when it is a finite number > 0; anything else
 * (null, NaN, Infinity, 0, negatives, strings) means "unknown".
 */
function parseTtftMs(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : undefined;
}

export class ModelPricing {
  readonly inputPer1k: number;
  readonly outputPer1k: number;

  constructor(inputPer1k = 0, outputPer1k = 0) {
    this.inputPer1k = inputPer1k;
    this.outputPer1k = outputPer1k;
  }

  get averagePer1k(): number {
    return (this.inputPer1k + this.outputPer1k) / 2;
  }
}

export class ModelInfo {
  readonly modelId: string;
  readonly provider: string;
  readonly benchmarkScores: Record<string, number>;
  readonly capabilities: string[];
  readonly pricing: ModelPricing | null;
  readonly latency: LatencyTier | null;
  /**
   * Measured output throughput in tokens/second, when known. Drives the
   * continuous speed score; `latency` is the fallback when this is undefined.
   */
  readonly tokensPerSecond?: number;
  /**
   * Measured time to first token in milliseconds, when known. Pairs with
   * `tokensPerSecond` to give the whole latency of an answer.
   */
  readonly ttftMs?: number;
  readonly description: string;

  constructor(opts: {
    modelId: string;
    provider: string;
    benchmarkScores?: Record<string, number>;
    capabilities?: string[];
    pricing?: ModelPricing | null;
    latency?: LatencyTier | null;
    tokensPerSecond?: number;
    ttftMs?: number;
    description?: string;
  }) {
    this.modelId = opts.modelId;
    this.provider = opts.provider;
    this.benchmarkScores = opts.benchmarkScores ?? {};
    this.capabilities = opts.capabilities ?? [];
    this.pricing = opts.pricing ?? null;
    this.latency = opts.latency ?? null;
    this.tokensPerSecond = parseTokensPerSecond(opts.tokensPerSecond);
    this.ttftMs = parseTtftMs(opts.ttftMs);
    this.description = opts.description ?? '';
  }

  toDict(): ModelData {
    return {
      model_id: this.modelId,
      provider: this.provider,
      benchmark_scores: this.benchmarkScores,
      capabilities: this.capabilities,
      pricing: this.pricing
        ? { input_per_1k: this.pricing.inputPer1k, output_per_1k: this.pricing.outputPer1k }
        : null,
      latency: this.latency,
      tokens_per_second: this.tokensPerSecond ?? null,
      ttft_ms: this.ttftMs ?? null,
      description: this.description,
    };
  }

  /**
   * `randomChanceFloors` are the catalog's (benchmarks.json); undefined means
   * the packaged starter catalog's floors.
   */
  static fromDict(d: ModelData, randomChanceFloors?: Record<string, number>): ModelInfo {
    // Pricing with a missing component is unknown, not free: coercing null
    // to 0 would hand the model a perfect cost score.
    let pricing: ModelPricing | null = null;
    if (d.pricing && d.pricing.input_per_1k != null && d.pricing.output_per_1k != null) {
      pricing = new ModelPricing(d.pricing.input_per_1k, d.pricing.output_per_1k);
    }

    // Drop null and implausible (corrupt) benchmark values such as
    // below-random-chance multiple-choice scores -- keeping them would both
    // crater the model and poison the registry-wide imputation medians.
    const benchmarkScores: Record<string, number> = {};
    if (d.benchmark_scores) {
      for (const [k, v] of Object.entries(d.benchmark_scores)) {
        if (v != null && !isImplausibleBenchmarkScore(k, v, randomChanceFloors)) {
          benchmarkScores[k] = v;
        }
      }
    }

    return new ModelInfo({
      modelId: d.model_id,
      provider: d.provider,
      benchmarkScores,
      capabilities: d.capabilities ?? [],
      pricing,
      latency: d.latency ?? null,
      tokensPerSecond: parseTokensPerSecond(d.tokens_per_second),
      ttftMs: parseTtftMs(d.ttft_ms),
      description: d.description ?? '',
    });
  }
}

/**
 * Per-benchmark registry coverage: `n_real(b) / N`.
 *
 * `N` is the size of the **routable** set -- the handed-in models minus
 * ephemeral `:free` variants, i.e. exactly the population the Router routes
 * over -- and `n_real(b)` is how many of them carry a real, finite score for
 * `b`. Benchmarks no routable model reports are *absent* from the result; the
 * caller reads a missing key as coverage 0 (the term then contributes nothing).
 *
 * This is a pure function of the model set so the Python SDK can mirror it
 * exactly (`compute_benchmark_coverage` there); ScoringEngine and
 * ModelRegistry both memoise it.
 */
export function computeBenchmarkCoverage(models: ModelInfo[]): Record<string, number> {
  const routable = models.filter((m) => !isFreeTier(m.modelId));
  const total = routable.length;
  const coverage: Record<string, number> = {};
  if (total === 0) return coverage;
  const counts = new Map<string, number>();
  for (const model of routable) {
    for (const [name, raw] of Object.entries(model.benchmarkScores)) {
      // Number.isFinite also rejects NaN/Infinity: a junk value is not a
      // reported value, exactly as computeBenchmarkMedians treats it.
      if (!Number.isFinite(raw)) continue;
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
  }
  for (const [name, n] of counts) coverage[name] = n / total;
  return coverage;
}

export class ModelRegistry {
  private _models: Map<string, ModelInfo>;
  /** Memoised benchmarkCoverage(); invalidated whenever the model set changes. */
  private _coverage: Record<string, number> | null = null;

  constructor() {
    this._models = new Map();
  }

  /**
   * Create a registry pre-loaded with the default catalog's models.
   *
   * Ephemeral free-tier variants (`:free` ids) are excluded unless
   * `includeFree` is true -- see `isFreeTier`. `bundle` (a CatalogBundle or
   * bundle directory) overrides the default catalog -- see `resolveBundle`.
   */
  static default(
    includeFree = false,
    bundle?: BundleLike | null,
    catalog: CatalogMode = 'auto',
  ): ModelRegistry {
    return ModelRegistry.fromBundle(resolveBundle(bundle, catalog), { includeFree });
  }

  /** Create a registry holding one catalog bundle's models. */
  static fromBundle(bundle: CatalogBundle, opts?: { includeFree?: boolean }): ModelRegistry {
    const registry = new ModelRegistry();
    registry.loadFromBundle(bundle, opts);
    return registry;
  }

  /**
   * Add a catalog bundle's models (its own random-chance floors apply).
   *
   * @returns Number of models loaded.
   */
  loadFromBundle(bundle: CatalogBundle, opts?: { includeFree?: boolean }): number {
    const floors = bundle.randomChanceFloors();
    let count = 0;
    for (const modelData of bundle.modelEntries()) {
      if (!opts?.includeFree && isFreeTier(modelData.model_id)) continue;
      this.addModel(ModelInfo.fromDict(modelData, floors));
      count++;
    }
    return count;
  }

  /** Add or update a model in the registry. */
  addModel(model: ModelInfo): void {
    this._coverage = null;
    this._models.set(model.modelId, model);
  }

  /** Convenience method to add a model with keyword arguments. */
  add(opts: {
    modelId: string;
    provider: string;
    benchmarks?: Record<string, number>;
    pricing?: [number, number];
    latency?: LatencyTier;
    tokensPerSecond?: number;
    ttftMs?: number;
    capabilities?: string[];
    description?: string;
  }): ModelInfo {
    let modelPricing: ModelPricing | null = null;
    if (opts.pricing) {
      modelPricing = new ModelPricing(opts.pricing[0], opts.pricing[1]);
    }

    const model = new ModelInfo({
      modelId: opts.modelId,
      provider: opts.provider,
      benchmarkScores: opts.benchmarks ?? {},
      pricing: modelPricing,
      latency: opts.latency ?? null,
      tokensPerSecond: opts.tokensPerSecond,
      ttftMs: opts.ttftMs,
      capabilities: opts.capabilities ?? [],
      description: opts.description ?? '',
    });
    this.addModel(model);
    return model;
  }

  /** Remove a model from the registry. Returns true if removed. */
  removeModel(modelId: string): boolean {
    this._coverage = null;
    return this._models.delete(modelId);
  }

  /**
   * Per-benchmark coverage over this registry's **routable** models: for each
   * benchmark, the fraction of routable models (everything but `:free` ids)
   * that report a real, finite score for it.
   *
   * Benchmarks no routable model reports are absent -- read a missing key as
   * coverage 0. This is what the scoring engine's coverage-aware per-term
   * weight is built from (see COVERAGE_EXPONENT); memoised until the registry
   * is mutated.
   */
  benchmarkCoverage(): Record<string, number> {
    if (this._coverage === null) {
      this._coverage = computeBenchmarkCoverage(this.allModels);
    }
    return this._coverage;
  }

  /** Get a model by ID. */
  getModel(modelId: string): ModelInfo | undefined {
    return this._models.get(modelId);
  }

  /** Filter models by criteria. */
  filter(opts?: {
    provider?: string;
    capability?: string;
    maxInputCost?: number;
    latency?: LatencyTier;
  }): ModelInfo[] {
    let results = [...this._models.values()];

    if (opts?.provider) {
      const providerLower = opts.provider.toLowerCase();
      results = results.filter((m) => m.provider.toLowerCase() === providerLower);
    }
    if (opts?.capability) {
      const cap = opts.capability;
      results = results.filter((m) => m.capabilities.includes(cap));
    }
    if (opts?.maxInputCost != null) {
      const maxCost = opts.maxInputCost;
      results = results.filter((m) => m.pricing != null && m.pricing.inputPer1k <= maxCost);
    }
    if (opts?.latency) {
      results = results.filter((m) => m.latency === opts.latency);
    }

    return results;
  }

  /** All registered models. */
  get allModels(): ModelInfo[] {
    return [...this._models.values()];
  }

  /** All registered model IDs. */
  get modelIds(): string[] {
    return [...this._models.keys()];
  }

  get length(): number {
    return this._models.size;
  }

  has(modelId: string): boolean {
    return this._models.has(modelId);
  }

  /**
   * Load a preset model set. Kept for backwards compatibility.
   *
   * Only `'default'` exists: the default catalog bundle's models (the packaged
   * starter catalog). Ephemeral free-tier variants (`:free` ids) are skipped
   * unless `includeFree` is true.
   *
   * @returns Number of models loaded.
   */
  loadPreset(name = 'default', opts?: { includeFree?: boolean }): number {
    if (name !== 'default') {
      throw new Error(
        `Preset '${name}' not found (only 'default' -- the default catalog -- exists)`,
      );
    }
    return this.loadFromBundle(resolveBundle(null), opts);
  }

  /** Export registry to a JSON-serializable object. */
  exportJson(): ModelsJson {
    return {
      models: [...this._models.values()].map((m) => m.toDict()),
    };
  }
}
