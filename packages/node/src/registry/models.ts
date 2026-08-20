/**
 * Model registry -- stores metadata about AI models.
 *
 * Each model has benchmark scores, pricing, latency, and capabilities.
 * Ships with a default preset of 35+ models; users can add/remove/override.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isImplausibleBenchmarkScore } from '../scoring/benchmarks.js';
import type { LatencyTier, ModelData, ModelsJson } from '../types.js';

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
  readonly description: string;

  constructor(opts: {
    modelId: string;
    provider: string;
    benchmarkScores?: Record<string, number>;
    capabilities?: string[];
    pricing?: ModelPricing | null;
    latency?: LatencyTier | null;
    description?: string;
  }) {
    this.modelId = opts.modelId;
    this.provider = opts.provider;
    this.benchmarkScores = opts.benchmarkScores ?? {};
    this.capabilities = opts.capabilities ?? [];
    this.pricing = opts.pricing ?? null;
    this.latency = opts.latency ?? null;
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
      description: this.description,
    };
  }

  static fromDict(d: ModelData): ModelInfo {
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
        if (v != null && !isImplausibleBenchmarkScore(k, v)) benchmarkScores[k] = v;
      }
    }

    return new ModelInfo({
      modelId: d.model_id,
      provider: d.provider,
      benchmarkScores,
      capabilities: d.capabilities ?? [],
      pricing,
      latency: d.latency ?? null,
      description: d.description ?? '',
    });
  }
}

export class ModelRegistry {
  private _models: Map<string, ModelInfo>;

  constructor() {
    this._models = new Map();
  }

  /** Create a registry pre-loaded with the default model preset. */
  static default(): ModelRegistry {
    const registry = new ModelRegistry();
    registry.loadPreset('default');
    return registry;
  }

  /** Add or update a model in the registry. */
  addModel(model: ModelInfo): void {
    this._models.set(model.modelId, model);
  }

  /** Convenience method to add a model with keyword arguments. */
  add(opts: {
    modelId: string;
    provider: string;
    benchmarks?: Record<string, number>;
    pricing?: [number, number];
    latency?: LatencyTier;
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
      capabilities: opts.capabilities ?? [],
      description: opts.description ?? '',
    });
    this.addModel(model);
    return model;
  }

  /** Remove a model from the registry. Returns true if removed. */
  removeModel(modelId: string): boolean {
    return this._models.delete(modelId);
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
   * Load a preset model registry from bundled JSON.
   * @returns Number of models loaded.
   */
  loadPreset(name = 'default'): number {
    const currentDir = dirname(fileURLToPath(import.meta.url));
    const presetFile = join(currentDir, 'presets', `${name}Models.json`);

    let raw: string;
    try {
      raw = readFileSync(presetFile, 'utf-8');
    } catch {
      throw new Error(`Preset '${name}' not found at ${presetFile}`);
    }

    const data: ModelsJson = JSON.parse(raw);
    let count = 0;
    for (const modelData of data.models ?? []) {
      const model = ModelInfo.fromDict(modelData);
      this.addModel(model);
      count++;
    }
    return count;
  }

  /** Export registry to a JSON-serializable object. */
  exportJson(): ModelsJson {
    return {
      models: [...this._models.values()].map((m) => m.toDict()),
    };
  }
}
