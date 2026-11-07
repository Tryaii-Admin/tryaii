/**
 * Static embedding provider: the H4 router's Model2Vec model in plain
 * TypeScript. Mirrors packages/python/tryaii/embeddings/static_provider.py.
 *
 * The default router's encoder since H4 (router id `h4-he-en`). A Model2Vec
 * static model is a table of token vectors: a text's vector is the mean of the
 * vectors of its tokens, L2-normalised. No neural network runs, so there is no
 * onnxruntime and nothing async: the provider is sync (`supportsSync`).
 *
 * The payload (`vectors.safetensors`, `tokenizer.json`, `meta.json`, plus the
 * head files the classifier reads) ships in the `tryaii-router-h4` package (an
 * optional peer dependency); `resolveRouterDir` (modelStore.ts) finds it
 * (`TRYAII_ROUTER_MODEL_PATH` first) and checks its pinned hash.
 *
 * Inference, exactly as the Python SDK (model2vec 0.10 `StaticModel.encode`):
 *
 * 1. cut the text to `char_cap` code points (700 * 5 = 3 500);
 * 2. tokenize without special tokens (`UnigramTokenizer`, id-for-id HF
 *    `tokenizers`), keep the first `max_tokens` (700) ids, then drop every
 *    unknown-token id;
 * 3. mean of the int8 rows of those ids -- summed as exact integers, divided
 *    once, rounded to float32;
 * 4. L2-normalise (float32 results); a text with no known tokens is the zero
 *    vector.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { BaseEmbeddingProvider } from './base.js';
import {
  DEFAULT_ROUTER_MODEL,
  ROUTERS,
  RouterUnavailableError,
  resolveRouterDir,
  type RouterSpec,
} from './modelStore.js';
import { UnigramTokenizer, sliceCodePoints } from './unigram.js';

// -- safetensors ----------------------------------------------------------------

export type SafeTensor =
  | { dtype: 'I8'; shape: number[]; data: Int8Array }
  | { dtype: 'F32'; shape: number[]; data: Float32Array }
  | { dtype: 'I64'; shape: number[]; data: BigInt64Array };

/**
 * The tensors of a safetensors file (I8 / F32 / I64 only), read
 * synchronously: an 8-byte little-endian header length, the JSON header, then
 * the raw little-endian data. A tiny reader, so the SDK needs no package.
 */
export function loadSafetensors(path: string): Record<string, SafeTensor> {
  const buf = readFileSync(path);
  if (buf.length < 8) throw new Error(`${path}: not a safetensors file`);
  const n = Number(buf.readBigUInt64LE(0));
  if (!Number.isSafeInteger(n) || 8 + n > buf.length) throw new Error(`${path}: bad safetensors header`);
  const header = JSON.parse(buf.subarray(8, 8 + n).toString('utf-8')) as Record<string, unknown>;
  const body = 8 + n;
  const out: Record<string, SafeTensor> = {};
  for (const [name, raw] of Object.entries(header)) {
    if (name === '__metadata__') continue;
    const info = raw as { dtype: string; shape: number[]; data_offsets: [number, number] };
    const [start, end] = info.data_offsets;
    const shape = info.shape.map((x) => Number(x));
    const count = shape.reduce((a, b) => a * b, 1);
    if (!(start >= 0 && end >= start && body + end <= buf.length)) {
      throw new Error(`${path}: ${name} is out of bounds`);
    }
    const view = new DataView(buf.buffer, buf.byteOffset + body + start, end - start);
    if (info.dtype === 'I8') {
      if (end - start !== count) throw new Error(`${path}: ${name} has the wrong size`);
      const data = new Int8Array(count);
      data.set(new Int8Array(buf.buffer, buf.byteOffset + body + start, count));
      out[name] = { dtype: 'I8', shape, data };
    } else if (info.dtype === 'F32') {
      if (end - start !== count * 4) throw new Error(`${path}: ${name} has the wrong size`);
      const data = new Float32Array(count);
      for (let i = 0; i < count; i++) data[i] = view.getFloat32(i * 4, true);
      out[name] = { dtype: 'F32', shape, data };
    } else if (info.dtype === 'I64') {
      if (end - start !== count * 8) throw new Error(`${path}: ${name} has the wrong size`);
      const data = new BigInt64Array(count);
      for (let i = 0; i < count; i++) data[i] = view.getBigInt64(i * 8, true);
      out[name] = { dtype: 'I64', shape, data };
    } else {
      throw new Error(`${path}: unsupported dtype ${info.dtype} for ${name}`);
    }
  }
  return out;
}

// -- the payload ------------------------------------------------------------------

/** `meta.json` of a router payload (the fields this SDK reads). */
export interface RouterMeta {
  router_id: string;
  version: string;
  dim: number;
  vocab_size: number;
  max_tokens: number;
  median_token_length: number;
  char_cap: number;
  unk_id: number | null;
  temperature: number;
  num_classes: number;
  normalizer: string;
  [key: string]: unknown;
}

interface StaticPayload {
  directory: string;
  meta: RouterMeta;
  tokenizer: UnigramTokenizer;
  /** Row-major [vocab, dim] int8 vectors. */
  vectors: Int8Array;
}

/**
 * Loaded payloads by directory: a Router per catalog bundle (and the monitor
 * engine next to it) share one copy of the 10 MB vector table.
 */
const payloads = new Map<string, StaticPayload>();

function loadPayload(directory: string, routerId: string): StaticPayload {
  const cached = payloads.get(directory);
  if (cached !== undefined) return cached;
  const meta = JSON.parse(readFileSync(join(directory, 'meta.json'), 'utf-8')) as RouterMeta;
  if (meta.router_id !== routerId) {
    throw new Error(`${directory}: meta.json is for ${JSON.stringify(meta.router_id)}`);
  }
  const tensors = loadSafetensors(join(directory, 'vectors.safetensors'));
  const vectors = tensors.embeddings;
  if (vectors === undefined || vectors.dtype !== 'I8' || vectors.shape.length !== 2 ||
      vectors.shape[1] !== meta.dim) {
    throw new Error(`${directory}: unexpected vectors ${vectors?.dtype} ${JSON.stringify(vectors?.shape)}`);
  }
  const tokenizer = new UnigramTokenizer(
    JSON.parse(readFileSync(join(directory, 'tokenizer.json'), 'utf-8')),
  );
  if (tokenizer.vocabSize !== vectors.shape[0]) {
    throw new Error(`${directory}: tokenizer and vectors disagree on the vocabulary`);
  }
  const payload: StaticPayload = { directory, meta, tokenizer, vectors: vectors.data };
  payloads.set(directory, payload);
  return payload;
}

/** Forget loaded payloads (tests). */
export function resetStaticPayloadCache(): void {
  payloads.clear();
}

export interface StaticProviderOptions {
  /** An explicit payload directory (skips resolution and the hash check; tests, scripts). */
  modelDir?: string;
}

/**
 * Model2Vec static embeddings (the H4 router's encoder). Lazy: the payload is
 * resolved, verified and loaded on first use (sync), then shared.
 */
export class StaticEmbeddingProvider extends BaseEmbeddingProvider {
  private readonly _modelName: string;
  private readonly spec: RouterSpec;
  private readonly explicitDir: string | null;
  private payload: StaticPayload | null = null;

  constructor(modelName: string = DEFAULT_ROUTER_MODEL, opts: StaticProviderOptions = {}) {
    super();
    const spec = ROUTERS[modelName];
    if (!spec) throw new Error(`unknown router model ${JSON.stringify(modelName)}`);
    this._modelName = modelName;
    this.spec = spec;
    this.explicitDir = opts.modelDir ?? null;
  }

  /**
   * Load the payload (once); return its directory. Throws
   * RouterUnavailableError when there is no verified payload, and any load
   * error for a broken one.
   */
  ensureLoaded(): string {
    if (this.payload !== null) return this.payload.directory;
    let directory = this.explicitDir;
    if (directory === null) {
      directory = resolveRouterDir(this._modelName);
      if (directory === null) throw new RouterUnavailableError(this._modelName);
    }
    this.payload = loadPayload(directory, this._modelName);
    return directory;
  }

  get meta(): RouterMeta {
    this.ensureLoaded();
    return (this.payload as StaticPayload).meta;
  }

  get directory(): string {
    return this.ensureLoaded();
  }

  /**
   * The ids the vector is the mean of (after the char cap, the token cap and
   * dropping the unknown token).
   */
  tokenIds(text: string): number[] {
    this.ensureLoaded();
    const { meta, tokenizer } = this.payload as StaticPayload;
    const ids = tokenizer.encode(sliceCodePoints(text, meta.char_cap)).slice(0, meta.max_tokens);
    const unk = meta.unk_id;
    return ids.filter((i) => i !== unk);
  }

  override embed(text: string): number[] {
    const ids = this.tokenIds(text);
    const { meta, vectors } = this.payload as StaticPayload;
    const dim = meta.dim;
    if (ids.length === 0) return new Array<number>(dim).fill(0);
    // Exact integer sum (|sum| <= 700 * 128), one division, then float32:
    // the same numbers numpy gives, whatever the order of accumulation.
    const total = new Int32Array(dim);
    for (const id of ids) {
      const row = id * dim;
      for (let d = 0; d < dim; d++) total[d] += vectors[row + d];
    }
    const mean = new Float32Array(dim);
    let sq = 0;
    for (let d = 0; d < dim; d++) {
      mean[d] = total[d] / ids.length;
      sq += mean[d] * mean[d];
    }
    const norm = Math.fround(Math.sqrt(sq));
    if (!Number.isFinite(norm) || norm <= 0) return new Array<number>(dim).fill(0);
    const out = new Array<number>(dim);
    for (let d = 0; d < dim; d++) out[d] = Math.fround(mean[d] / norm);
    return out;
  }

  override embedBatch(texts: string[]): number[][] {
    return texts.map((t) => this.embed(t));
  }

  override get supportsSync(): boolean {
    return true;
  }

  get dimension(): number {
    return this.spec.dimension;
  }

  get modelName(): string {
    return this._modelName;
  }
}
