/**
 * ONNX embedding provider: the int8 all-MiniLM-L6-v2 file on onnxruntime-node
 * (TS-253). The same model file, tokenizer, pooling and normalisation as the
 * Python SDK (tryaii/embeddings/onnx_provider.py), so both SDKs produce the
 * same vectors and are scored against the same centroids
 * (`centroids.all-MiniLM-L6-v2-onnx-int8.json`).
 *
 * `onnxruntime-node` and `tryaii-minilm-int8` are optional peer dependencies,
 * loaded on first use: `npm install onnxruntime-node tryaii-minilm-int8`.
 * Without them, embedding throws `ModelUnavailableError` and the monitor
 * fails open.
 *
 * One text per run, never a padded batch, like Python: a text goes through
 * exactly the same computation whatever else is embedded. The model is
 * weight-only int8 (fp32 arithmetic), so every CPU computes the same vectors.
 */

import { readFileSync } from 'node:fs';

import { BaseEmbeddingProvider } from './base.js';
import {
  DEFAULT_ONNX_MODEL,
  MODELS,
  ModelUnavailableError,
  resolveModelPath,
  tokenizerPath,
  type ModelSpec,
} from './modelStore.js';
import { WordPieceTokenizer, type TokenizerJson } from './wordpiece.js';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Ort = any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Session = any;

let ortModule: Ort | null = null;

/** Load onnxruntime-node (optional peer dependency); null when absent. */
export async function loadOrt(): Promise<Ort | null> {
  if (ortModule !== null) return ortModule;
  try {
    const mod = await import('onnxruntime-node');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ortModule = (mod as any).InferenceSession ? mod : (mod as any).default;
    return ortModule;
  } catch {
    return null;
  }
}

export interface OnnxProviderOptions {
  /** An explicit model file (skips resolution and the checksum; tests, scripts). */
  modelPath?: string;
}

export class OnnxEmbeddingProvider extends BaseEmbeddingProvider {
  private readonly spec: ModelSpec;
  private readonly explicitPath: string | null;
  private loading: Promise<void> | null = null;
  private session: Session | null = null;
  private tokenizer: WordPieceTokenizer | null = null;
  private ort: Ort | null = null;
  private inputNames: string[] = [];

  constructor(modelName: string = DEFAULT_ONNX_MODEL, opts: OnnxProviderOptions = {}) {
    super();
    const spec = MODELS[modelName];
    if (!spec) throw new Error(`unknown ONNX embedding model ${JSON.stringify(modelName)}`);
    this.spec = spec;
    this.explicitPath = opts.modelPath ?? null;
  }

  /** Load the session and tokenizer (once). Throws ModelUnavailableError. */
  async init(): Promise<void> {
    if (this.session !== null) return;
    if (this.loading === null) {
      this.loading = this.load().catch((err) => {
        this.loading = null;
        throw err;
      });
    }
    await this.loading;
  }

  private async load(): Promise<void> {
    const ort = await loadOrt();
    if (ort === null) {
      throw new ModelUnavailableError(this.spec.modelId, 'onnxruntime-node is not installed');
    }
    const path = this.explicitPath ?? (await resolveModelPath(this.spec.modelId));
    if (path === null) throw new ModelUnavailableError(this.spec.modelId);
    const json = JSON.parse(readFileSync(tokenizerPath(this.spec.modelId), 'utf-8')) as TokenizerJson;
    const tokenizer = new WordPieceTokenizer(json, this.spec.maxLength);
    const session = await ort.InferenceSession.create(path, {
      executionProviders: ['cpu'],
      graphOptimizationLevel: 'all',
      logSeverityLevel: 3,
    });
    this.inputNames = [...session.inputNames];
    this.tokenizer = tokenizer;
    this.ort = ort;
    this.session = session;
  }

  async embedAsync(text: string): Promise<number[]> {
    await this.init();
    const enc = (this.tokenizer as WordPieceTokenizer).encode(text);
    const n = enc.ids.length;
    const ort = this.ort as Ort;
    const toTensor = (values: number[]) =>
      new ort.Tensor('int64', BigInt64Array.from(values, (v) => BigInt(v)), [1, n]);
    const feeds: Record<string, unknown> = {
      input_ids: toTensor(enc.ids),
      attention_mask: toTensor(enc.attentionMask),
    };
    if (this.inputNames.includes('token_type_ids')) feeds.token_type_ids = toTensor(enc.ids.map(() => 0));
    const out = await (this.session as Session).run(feeds);
    const hidden = out[(this.session as Session).outputNames[0]];
    const data = hidden.data as Float32Array;
    const dim = hidden.dims[2] as number;
    // Mean pooling over the attention mask (all ones: one text, no padding), then L2.
    const pooled = new Float64Array(dim);
    for (let t = 0; t < n; t++) {
      for (let d = 0; d < dim; d++) pooled[d] += data[t * dim + d];
    }
    let norm = 0;
    for (let d = 0; d < dim; d++) {
      pooled[d] /= n;
      norm += pooled[d] * pooled[d];
    }
    norm = Math.max(Math.sqrt(norm), 1e-12);
    // Round through float32 like the Python provider's float32 output.
    return Array.from(Float32Array.from(pooled, (v) => v / norm));
  }

  async embedBatchAsync(texts: string[]): Promise<number[][]> {
    const out: number[][] = [];
    for (const text of texts) out.push(await this.embedAsync(text));
    return out;
  }

  get dimension(): number {
    return this.spec.dimension;
  }

  get modelName(): string {
    return this.spec.modelId;
  }
}
