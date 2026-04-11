/**
 * Local embedding provider using @xenova/transformers.
 *
 * Default model: all-MiniLM-L6-v2 (22M params, 384 dimensions)
 * - Runs on CPU on any modern machine
 * - No API keys needed
 * - ~50ms per embedding on average hardware
 *
 * @xenova/transformers is an optional dependency -- lazy-loaded on first use.
 *
 * This provider is async-only. The ONNX runtime exposed by
 * @xenova/transformers returns Promises for both pipeline construction and
 * per-call inference, so there is no honest way to offer a sync `embed()`.
 * Use `Router.route()` (async) with this provider; `Router.routeSync()` will
 * reject it via `supportsSync === false`.
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
  private _extractor: Pipeline | null = null;
  private _dimension: number | null = null;

  constructor(modelName = 'Xenova/all-MiniLM-L6-v2') {
    super();
    this._modelName = modelName;
  }

  /**
   * Internal async initializer -- called once to set up the ONNX pipeline.
   * Returns the feature-extraction pipeline instance.
   */
  private async _getExtractor(): Promise<Pipeline> {
    if (this._extractor !== null) {
      return this._extractor;
    }

    let pipelineFn: Function;
    try {
      const mod = await import('@xenova/transformers');
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
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
    this._extractor = extractor;

    return extractor;
  }

  /** Pre-initialize the model. Optional -- the first embedAsync() call also triggers init. */
  async init(): Promise<void> {
    await this._getExtractor();
  }

  async embedAsync(text: string): Promise<number[]> {
    const extractor = await this._getExtractor();
    const output = await extractor(text, { pooling: 'mean', normalize: true });
    return Array.from(output.data as Float32Array);
  }

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
