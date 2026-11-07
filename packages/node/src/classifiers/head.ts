/**
 * Head classifier: the H4 router (default since H4). Mirrors
 * packages/python/tryaii/classifiers/head.py.
 *
 * The prompt's static embedding (`StaticEmbeddingProvider`) goes through a
 * one-hidden-layer head trained on the benchmark labels:
 *
 *     logits = W1 relu(W0 e + b0) + b1
 *     p      = softmax(logits / T)        T = 2.0 (meta.json "temperature")
 *
 * `p` -- one probability per benchmark in `classes.json` -- is what the
 * scoring engine gets as `benchmarkScores` (in place of centroid cosine
 * similarities), restricted to the benchmarks of the loaded catalog: classes
 * whose benchmark the catalog does not have are dropped and the rest are
 * renormalised to sum to 1 (computed as the softmax of the kept logits / T,
 * which is the same thing). `maskedClasses` on the result says how many were
 * dropped; `confidence` is the top kept probability. When the catalog has none
 * of the classes the head cannot route, and the Router falls back to MiniLM
 * (`loadH4Classifier` in selection.ts). Difficulty keeps the easy/hard
 * exemplar formula of the embedding classifier, with the exemplars embedded
 * by the same static model. No centroids are used.
 *
 * The head runs in float64 (the weights are float32), like Python: the same
 * numbers on every CPU to well below the 1e-6 the shared cases check.
 */

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { LRUCache } from '../cache/index.js';
import { CatalogBundle } from '../catalog/bundle.js';
import { loadSafetensors, type StaticEmbeddingProvider } from '../embeddings/static.js';
import { cosineSimilarity } from '../utils/cosine.js';
import { vectorMean, vectorNormalize } from '../utils/math.js';
import { BaseClassifier, type ClassificationResult } from './base.js';
import {
  BENCHMARK_CATEGORIES,
  DIFFICULTY_SCALE,
  EASY_EXEMPLARS,
  FALLBACK_CATEGORY,
  HARD_EXEMPLARS,
} from './embedding.js';

/** softmax(logits / temperature), max-shifted, float64. */
export function softmax(logits: ArrayLike<number>, temperature = 1): number[] {
  const x = Array.from(logits, (v) => v / temperature);
  if (x.length === 0) return [];
  const max = Math.max(...x);
  const e = x.map((v) => Math.exp(v - max));
  const sum = e.reduce((a, b) => a + b, 0);
  return e.map((v) => v / sum);
}

/** One dense layer: `w` is row-major [out, in] (float64), `b` is [out]. */
export interface HeadLayer {
  w: Float64Array;
  b: Float64Array;
  out: number;
  in: number;
}

/**
 * The classification head of a router payload (`head.safetensors`,
 * `classes.json`, `meta.json`).
 */
export class RouterHead {
  readonly classes: string[];
  readonly temperature: number;
  readonly inputDim: number;
  private readonly layers: HeadLayer[];

  constructor(layers: HeadLayer[], classes: string[], temperature: number) {
    if (layers.length === 0) throw new Error('a head needs at least one layer');
    if (classes.length !== layers[layers.length - 1].out || new Set(classes).size !== classes.length) {
      throw new Error("classes do not match the head's output size");
    }
    for (const l of layers) {
      if (l.w.length !== l.out * l.in || l.b.length !== l.out) throw new Error('bad head layer shape');
    }
    this.layers = layers;
    this.classes = [...classes];
    this.temperature = Number(temperature);
    this.inputDim = layers[0].in;
  }

  /** A layer from float32 (or any) weights `w` [out][in] given flat, and `b`. */
  static layer(w: ArrayLike<number>, b: ArrayLike<number>, out: number, inDim: number): HeadLayer {
    return { w: Float64Array.from(w), b: Float64Array.from(b), out, in: inDim };
  }

  static fromDir(directory: string): RouterHead {
    const meta = JSON.parse(readFileSync(join(directory, 'meta.json'), 'utf-8')) as {
      temperature: number;
      dim: number;
      num_classes: number;
    };
    const tensors = loadSafetensors(join(directory, 'head.safetensors'));
    const layers: HeadLayer[] = [];
    for (let i = 0; tensors[`W${i}`] !== undefined; i++) {
      const w = tensors[`W${i}`];
      const b = tensors[`b${i}`];
      if (w.dtype !== 'F32' || b === undefined || b.dtype !== 'F32' || w.shape.length !== 2) {
        throw new Error(`${directory}: head layer ${i} is not float32`);
      }
      layers.push(RouterHead.layer(w.data, b.data, w.shape[0], w.shape[1]));
    }
    const classes = JSON.parse(readFileSync(join(directory, 'classes.json'), 'utf-8')) as string[];
    const head = new RouterHead(layers, classes, Number(meta.temperature));
    if (head.inputDim !== Number(meta.dim) || classes.length !== Number(meta.num_classes)) {
      throw new Error(`${directory}: head does not match meta.json`);
    }
    return head;
  }

  logits(embedding: ArrayLike<number>): Float64Array {
    let x = Float64Array.from(embedding);
    const last = this.layers.length - 1;
    this.layers.forEach((layer, i) => {
      const y = new Float64Array(layer.out);
      for (let o = 0; o < layer.out; o++) {
        let acc = 0;
        const row = o * layer.in;
        for (let k = 0; k < layer.in; k++) acc += layer.w[row + k] * x[k];
        acc += layer.b[o];
        y[o] = i < last && acc < 0 ? 0 : acc;
      }
      x = y;
    });
    return x;
  }

  probabilities(embedding: ArrayLike<number>): number[] {
    return softmax(this.logits(embedding), this.temperature);
  }

  /**
   * `{benchmark: probability}` for every class, or -- with `keep` -- for the
   * classes in `keep` only, renormalised to sum to 1 (the softmax of their
   * logits / T). Empty when no class is kept.
   */
  scores(embedding: ArrayLike<number>, keep: ReadonlySet<string> | null = null): Record<string, number> {
    const logits = this.logits(embedding);
    const idx: number[] = [];
    this.classes.forEach((name, i) => {
      if (keep === null || keep.has(name)) idx.push(i);
    });
    const out: Record<string, number> = {};
    if (idx.length === 0) return out;
    const p = softmax(idx.map((i) => logits[i]), this.temperature);
    idx.forEach((i, j) => {
      out[this.classes[i]] = p[j];
    });
    return out;
  }
}

export interface HeadClassifierOptions {
  /** The head; default: read from the provider's payload directory. */
  head?: RouterHead;
  embeddingCacheSize?: number;
  classificationCacheSize?: number;
  ttlSeconds?: number;
  /**
   * Catalog bundle whose category labels to use (else the starter table) and
   * whose benchmarks the classes are masked to.
   */
  bundle?: CatalogBundle | null;
  categories?: Record<string, [string, string]>;
  /** The benchmark names to keep (default: the bundle's; neither: every class). */
  catalogBenchmarks?: Iterable<string> | null;
}

/** The H4 router as a classifier (see the module comment). */
export class HeadClassifier extends BaseClassifier {
  static readonly classifierName = 'head';

  private readonly _provider: StaticEmbeddingProvider;
  private _head: RouterHead | null;
  private readonly _categories: Record<string, [string, string]>;
  private readonly _catalog: ReadonlySet<string> | null;
  private readonly _embeddingCache: LRUCache<number[]>;
  private readonly _classificationCache: LRUCache<ClassificationResult>;
  private _easyCentroid: number[] | null = null;
  private _hardCentroid: number[] | null = null;

  constructor(provider: StaticEmbeddingProvider, opts: HeadClassifierOptions = {}) {
    super();
    this._provider = provider;
    this._head = opts.head ?? null;
    const bundle = opts.bundle instanceof CatalogBundle ? opts.bundle : null;
    this._categories = {
      ...(opts.categories ?? (bundle !== null ? bundle.benchmarkCategories() : BENCHMARK_CATEGORIES)),
    };
    const names = opts.catalogBenchmarks ?? (bundle !== null ? bundle.benchmarkNames : null);
    this._catalog = names != null ? new Set(names) : null;
    this._embeddingCache = new LRUCache<number[]>(opts.embeddingCacheSize ?? 300, opts.ttlSeconds ?? 300);
    this._classificationCache = new LRUCache<ClassificationResult>(
      opts.classificationCacheSize ?? 150,
      opts.ttlSeconds ?? 300,
    );
  }

  /**
   * Load (and verify) the payload now; throws when it is unavailable or
   * broken. The Router calls this to decide on the fallback.
   */
  load(): this {
    const directory = this._provider.ensureLoaded();
    if (this._head === null) this._head = RouterHead.fromDir(directory);
    if (this._head.inputDim !== this._provider.dimension) {
      throw new Error('head and embedding dimensions differ');
    }
    return this;
  }

  get head(): RouterHead {
    this.load();
    return this._head as RouterHead;
  }

  get provider(): StaticEmbeddingProvider {
    return this._provider;
  }

  get modelId(): string {
    return this._provider.modelName;
  }

  /** The head's classes the catalog has (all of them without a catalog). */
  get keptClasses(): string[] {
    const classes = this.head.classes;
    const catalog = this._catalog;
    return catalog === null ? [...classes] : classes.filter((c) => catalog.has(c));
  }

  /** How many of the head's classes the catalog does not have. */
  get maskedClasses(): number {
    return this.head.classes.length - this.keptClasses.length;
  }

  classify(prompt: string): ClassificationResult {
    const start = performance.now();
    const key = HeadClassifier._hash(`${this.modelId}:classify:${prompt}`);
    const cached = this._classificationCache.get(key);
    if (cached !== undefined) {
      return { ...cached, cacheHit: true, processingTimeMs: performance.now() - start };
    }

    const head = this.head;
    const embedding = this._getEmbeddingSync(prompt);
    const scores = head.scores(embedding, this._catalog);
    const names = Object.keys(scores);
    if (names.length === 0) throw new Error("none of the H4 router's classes is in the catalog");
    // Top class: highest probability, ties by name (Python's min(-p, name)).
    let top = names[0];
    for (const name of names) {
      if (scores[name] > scores[top] || (scores[name] === scores[top] && name < top)) top = name;
    }
    const [broad, sub] = this._categories[top] ?? FALLBACK_CATEGORY;
    this._ensureDifficultyCentroids();
    const result: ClassificationResult = {
      benchmarkScores: scores,
      broadCategory: broad,
      subcategory: sub,
      confidence: scores[top],
      classifierUsed: HeadClassifier.classifierName,
      cacheHit: false,
      processingTimeMs: performance.now() - start,
      difficulty: this._intrinsicDifficulty(embedding),
      maskedClasses: head.classes.length - names.length,
    };
    this._classificationCache.set(key, result);
    return result;
  }

  /** Clear cached classifications (the Router calls it like the embedding classifier's). */
  clearCache(): void {
    this._classificationCache.clear();
  }

  _getEmbeddingSync(text: string): number[] {
    const key = HeadClassifier._hash(`${this.modelId}:${this._provider.dimension}:${text}`);
    const cached = this._embeddingCache.get(key);
    if (cached !== undefined) return cached;
    const embedding = this._provider.embed(text);
    this._embeddingCache.set(key, embedding);
    return embedding;
  }

  async _getEmbeddingAsync(text: string): Promise<number[]> {
    return this._getEmbeddingSync(text);
  }

  private _ensureDifficultyCentroids(): void {
    if (this._easyCentroid !== null && this._hardCentroid !== null) return;
    this._easyCentroid = vectorNormalize(vectorMean(this._provider.embedBatch(EASY_EXEMPLARS)));
    this._hardCentroid = vectorNormalize(vectorMean(this._provider.embedBatch(HARD_EXEMPLARS)));
  }

  private _intrinsicDifficulty(embedding: number[]): number {
    if (this._easyCentroid === null || this._hardCentroid === null) return 0;
    const simHard = cosineSimilarity(embedding, this._hardCentroid);
    const simEasy = cosineSimilarity(embedding, this._easyCentroid);
    return 1 / (1 + Math.exp(-DIFFICULTY_SCALE * (simHard - simEasy)));
  }

  isReady(): boolean {
    return true;
  }

  private static _hash(text: string): string {
    return createHash('md5').update(text).digest('hex');
  }
}
