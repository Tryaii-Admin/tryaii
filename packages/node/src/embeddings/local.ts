/**
 * Local embedding provider using @xenova/transformers.
 *
 * Default model: all-MiniLM-L6-v2 (22M params, 384 dimensions)
 * - Runs on CPU on any modern machine
 * - No API keys needed
 * - ~50ms per embedding on average hardware
 *
 * @xenova/transformers is an optional dependency -- lazy-loaded on first use.
 */

import { BaseEmbeddingProvider } from './base.js';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Pipeline = any;

/**
 * Local embedding provider using @xenova/transformers (ONNX-based).
 *
 * Lazy-loads the model on first use to keep import times fast.
 *
 * @param modelName - HuggingFace model name. Default: "Xenova/all-MiniLM-L6-v2"
 */
export class LocalEmbeddingProvider extends BaseEmbeddingProvider {
  private _modelName: string;
  private _pipeline: Pipeline | null = null;
  private _dimension: number | null = null;

  constructor(modelName = 'Xenova/all-MiniLM-L6-v2') {
    super();
    this._modelName = modelName;
  }

  private _ensureLoaded(): void {
    if (this._pipeline !== null) return;

    // Dynamic import to keep the dependency optional
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    let transformers: { pipeline: Function };
    try {
      // Use createRequire for ESM compatibility with dynamic optional deps
      transformers = require('@xenova/transformers');
    } catch {
      throw new Error(
        '@xenova/transformers is required for local embeddings. ' +
          'Install it with: npm install @xenova/transformers',
      );
    }

    // The pipeline will be stored as a promise that must be awaited
    // Since this is a sync API, we initialize lazily using a sync-compatible pattern.
    // For the first call we do a blocking-style initialization.
    this._pipeline = transformers.pipeline;
  }

  /**
   * Internal async initializer -- called once to set up the ONNX pipeline.
   * Returns the feature-extraction pipeline instance.
   */
  private async _getExtractor(): Promise<Pipeline> {
    if (this._pipeline !== null && typeof this._pipeline !== 'function') {
      return this._pipeline;
    }

    let pipelineFn: Function;
    try {
      const mod = await import('@xenova/transformers');
      pipelineFn = mod.pipeline ?? (mod as any).default?.pipeline;
    } catch {
      throw new Error(
        '@xenova/transformers is required for local embeddings. ' +
          'Install it with: npm install @xenova/transformers',
      );
    }

    if (!pipelineFn) {
      throw new Error('Could not find pipeline export in @xenova/transformers');
    }

    const extractor = await pipelineFn('feature-extraction', this._modelName, {
      quantized: true,
    });

    // Determine dimension from a test embedding
    const testOutput = await extractor('test', { pooling: 'mean', normalize: true });
    this._dimension = testOutput.dims[testOutput.dims.length - 1];
    this._pipeline = extractor;

    return extractor;
  }

  embed(text: string): number[] {
    // Sync wrapper -- throws if not yet initialized.
    // For first use, callers should use embedAsync() or embedBatchAsync().
    throw new Error(
      'LocalEmbeddingProvider.embed() is not available synchronously. ' +
        'Use embedAsync() instead, or pre-initialize with init().',
    );
  }

  embedBatch(texts: string[]): number[][] {
    throw new Error(
      'LocalEmbeddingProvider.embedBatch() is not available synchronously. ' +
        'Use embedBatchAsync() instead, or pre-initialize with init().',
    );
  }

  /** Initialize the model. Call this once before using embedAsync/embedBatchAsync. */
  async init(): Promise<void> {
    await this._getExtractor();
  }

  /** Generate embedding for a single text (async). */
  async embedAsync(text: string): Promise<number[]> {
    const extractor = await this._getExtractor();
    const output = await extractor(text, { pooling: 'mean', normalize: true });
    return Array.from(output.data as Float32Array);
  }

  /** Generate embeddings for multiple texts (async). */
  async embedBatchAsync(texts: string[]): Promise<number[][]> {
    const results: number[][] = [];
    for (const text of texts) {
      results.push(await this.embedAsync(text));
    }
    return results;
  }

  get dimension(): number {
    if (this._dimension === null) {
      return 384; // Default for all-MiniLM-L6-v2
    }
    return this._dimension;
  }

  get modelName(): string {
    // Return short name (without Xenova/ prefix) for compatibility with centroid files
    return this._modelName.replace('Xenova/', '');
  }
}
