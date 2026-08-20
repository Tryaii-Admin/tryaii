/**
 * Neural embedding classifier.
 *
 * Classifies prompts by computing cosine similarity between the prompt's
 * embedding vector and pre-computed benchmark centroids. This gives a
 * semantic understanding of "what kind of task" a prompt represents.
 *
 * Exposes both sync (`classify`) and async (`classifyAsync`) entry points.
 * The sync path requires an embedding provider whose `supportsSync` is true;
 * the async path works with any provider.
 */

import { createHash } from 'node:crypto';

import { LRUCache } from '../cache/index.js';
import { BaseClassifier, ClassificationResult } from './base.js';
import { cosineSimilarity } from '../utils/cosine.js';
import { vectorMean, vectorNormalize } from '../utils/math.js';
import { BaseEmbeddingProvider } from '../embeddings/base.js';
import { CentroidLoader, benchmarkFingerprint } from '../centroids/loader.js';
import { STANDARD_BENCHMARKS } from '../benchmarks/standard.js';

/**
 * Benchmark -> [broadCategory, subcategory] mapping for display purposes.
 *
 * Derived from the standard benchmark definitions so the classifier's category
 * labels can never drift from the benchmark taxonomy (or its names). The
 * subcategory is the benchmark's primary (first) subcategory.
 */
export const BENCHMARK_CATEGORIES: Record<string, [string, string]> = Object.fromEntries(
  STANDARD_BENCHMARKS.map(
    (b): [string, [string, string]] => [b.name, [b.broadCategory, b.subcategories[0] ?? 'GENERAL']],
  ),
);

/**
 * Logistic steepness for intrinsic difficulty. Only affects the spread of the
 * reported [0,1] value; the ORDERING (which drives batch-normalized allocation)
 * is scale-invariant. Must stay in sync with the Python SDK (DIFFICULTY_SCALE).
 */
export const DIFFICULTY_SCALE = 10;

/**
 * Canonical EASY exemplars (atomic / single-step / lookup), spanning many domains
 * so the difficulty axis encodes COMPLEXITY, not topic. Paired 1:1 by domain with
 * HARD_EXEMPLARS. Must stay in sync with the Python SDK (classifiers/embedding.py).
 */
export const EASY_EXEMPLARS: string[] = [
  'What is the capital of Japan?',
  'Which planet is closest to the sun?',
  'How many continents are there on Earth?',
  'How many days are in a week?',
  'What is 7 times 8?',
  'What is 25 percent of 80?',
  'Round 3.14159 to two decimal places.',
  'What is the square root of 144?',
  "What does the 'len()' function do in Python?",
  "How do I print 'Hello, World!' in JavaScript?",
  'Which command stages all changes in Git?',
  'How do I declare a constant variable in JavaScript?',
  'If all cats are animals and Tom is a cat, is Tom an animal?',
  "Does 'P implies Q' mean Q is true whenever P is true?",
  'If A is taller than B, who is shorter?',
  "Is the statement 'it is raining and it is not raining' true or false?",
  "Translate 'good morning' into German.",
  "What is the plural of 'cactus'?",
  "Correct the spelling: 'recieve'.",
  "How do you say 'thank you' in Japanese?",
  'What does ROI stand for in business?',
  'What is the formula to calculate gross profit?',
  'What does the acronym KPI mean?',
  'Convert 250 US dollars to euros at a rate of 1.08.',
];

/** Canonical HARD exemplars (multi-step / open-ended / design / proof). */
export const HARD_EXEMPLARS: string[] = [
  'Explain how a refrigerator keeps food cold using the principles of thermodynamics.',
  'Assess how the printing press reshaped literacy, religion, and politics across early modern Europe.',
  'Explain why the sky is blue at noon but red at sunset, accounting for light scattering.',
  'Compare the long-term societal trade-offs of nuclear, solar, and coal energy for a national grid.',
  'Prove that the square root of 2 is irrational and justify each step rigorously.',
  'Derive a closed-form expression for the sum of the first n cubes and prove it by induction.',
  'Evaluate the integral of e to the negative x squared from negative infinity to infinity and explain the method used.',
  'Determine the expected number of draws to collect all n distinct coupons and analyze its asymptotic growth.',
  'Design a horizontally scalable, exactly-once message queue and discuss its consistency trade-offs.',
  'Implement a lock-free concurrent hash map and prove it is linearizable.',
  'Architect a multi-region database with automatic failover and explain how you resolve write conflicts.',
  'Refactor a tightly coupled monolith into event-driven microservices while preserving transactional integrity.',
  'Determine whether this argument is valid and name any fallacy it commits, justifying each step.',
  'Prove that the given set of logical premises is inconsistent using natural deduction.',
  'Solve the knights-and-knaves puzzle and explain the chain of inferences leading to each identity.',
  'Resolve the apparent paradox in this self-referential statement and explain why naive interpretations fail.',
  'Translate this poem into French while preserving its rhyme scheme and meter.',
  'Rewrite this technical manual for a sixth-grade reading level without losing accuracy.',
  'Write a persuasive essay arguing both sides of a dilemma, then synthesize a conclusion.',
  'Compare how three languages encode politeness and what that reveals about each culture.',
  'Build a three-year financial model with scenario analysis and justify every assumption.',
  'Design a churn-prediction pipeline from raw event logs and defend your feature choices.',
  'Develop a market-entry strategy for a new region and quantify the risk-adjusted payback.',
  'Construct a data-governance policy covering retention, access control, and regulatory compliance across jurisdictions.',
];

/**
 * Semantic classifier using embedding cosine similarity.
 *
 * Flow:
 *   1. Embed the user prompt using the configured embedding provider
 *   2. Compute cosine similarity against each benchmark centroid
 *   3. Return similarity scores as the classification result
 *
 * Includes LRU caching for both embeddings and full classification results.
 */
export class EmbeddingClassifier extends BaseClassifier {
  private _provider: BaseEmbeddingProvider;
  private _centroidLoader: CentroidLoader;
  private _embeddingCache: LRUCache<number[]>;
  private _classificationCache: LRUCache<ClassificationResult>;
  private _easyCentroid: number[] | null = null;
  private _hardCentroid: number[] | null = null;

  constructor(
    embeddingProvider: BaseEmbeddingProvider,
    centroidLoader: CentroidLoader,
    opts?: {
      embeddingCacheSize?: number;
      classificationCacheSize?: number;
      ttlSeconds?: number;
    },
  ) {
    super();
    this._provider = embeddingProvider;
    this._centroidLoader = centroidLoader;

    this._embeddingCache = new LRUCache<number[]>(
      opts?.embeddingCacheSize ?? 300,
      opts?.ttlSeconds ?? 300,
    );
    this._classificationCache = new LRUCache<ClassificationResult>(
      opts?.classificationCacheSize ?? 150,
      opts?.ttlSeconds ?? 300,
    );
  }

  /**
   * Synchronous classification. Requires the underlying provider to support
   * `embed()` (`supportsSync === true`); otherwise the provider will throw.
   */
  classify(prompt: string): ClassificationResult {
    const start = performance.now();

    // Load centroids first so the classification cache key can include the
    // benchmark-set fingerprint. This is memoized in the loader and does not
    // embed the prompt, so the expensive work is still gated on a cache miss.
    const centroids = this._centroidLoader.getCentroids();
    const cacheKey = this._classificationCacheKey(prompt, centroids);

    const cached = this._readCache(cacheKey, start);
    if (cached !== null) return cached;

    const embedding = this._getEmbeddingSync(prompt);
    return this._scoreAndCache(cacheKey, embedding, centroids, start);
  }

  /**
   * Asynchronous classification. Works with any embedding provider; sync
   * providers route through their default async fallback in BaseEmbeddingProvider.
   */
  async classifyAsync(prompt: string): Promise<ClassificationResult> {
    const start = performance.now();

    // Load centroids first so the classification cache key can include the
    // benchmark-set fingerprint (see `classify` for rationale).
    const centroids = await this._centroidLoader.getCentroidsAsync();
    const cacheKey = this._classificationCacheKey(prompt, centroids);

    const cached = this._readCache(cacheKey, start);
    if (cached !== null) return cached;

    const embedding = await this._getEmbeddingAsync(prompt);
    await this._ensureDifficultyCentroidsAsync();
    return this._scoreAndCache(cacheKey, embedding, centroids, start);
  }

  /** Try to return a cached classification result, stamped with fresh timing. */
  private _readCache(cacheKey: string, start: number): ClassificationResult | null {
    const cached = this._classificationCache.get(cacheKey);
    if (cached === undefined) return null;
    return {
      ...cached,
      cacheHit: true,
      processingTimeMs: performance.now() - start,
    };
  }

  /** Score an embedding against the given centroids, cache the result, and return it. */
  private _scoreAndCache(
    cacheKey: string,
    embedding: number[],
    centroids: Record<string, number[]>,
    start: number,
  ): ClassificationResult {
    // Empty centroid map: return a well-defined zero-confidence result with no
    // fabricated label, rather than confidence=-1 / a default TECHNICAL guess.
    if (Object.keys(centroids).length === 0) {
      const empty: ClassificationResult = {
        benchmarkScores: {},
        broadCategory: '',
        subcategory: '',
        confidence: 0,
        classifierUsed: 'embedding',
        cacheHit: false,
        processingTimeMs: performance.now() - start,
        difficulty: this._intrinsicDifficulty(embedding),
      };
      this._classificationCache.set(cacheKey, empty);
      return empty;
    }

    const benchmarkScores: Record<string, number> = {};
    for (const [benchmarkName, centroid] of Object.entries(centroids)) {
      const similarity = cosineSimilarity(embedding, centroid);
      // Clamp to [0, 1] -- negative similarities are not meaningful here
      benchmarkScores[benchmarkName] = Math.max(0.0, similarity);
    }

    let topBenchmark = '';
    // Init to 0 (not -1): scores are already clamped to >= 0, so confidence
    // stays >= 0 even in degenerate cases.
    let topScore = 0;
    for (const [name, score] of Object.entries(benchmarkScores)) {
      if (score > topScore) {
        topScore = score;
        topBenchmark = name;
      }
    }

    const categories = BENCHMARK_CATEGORIES[topBenchmark] ?? ['TECHNICAL', 'CODE_TECHNICAL'];

    const result: ClassificationResult = {
      benchmarkScores,
      broadCategory: categories[0],
      subcategory: categories[1],
      confidence: topScore,
      classifierUsed: 'embedding',
      cacheHit: false,
      processingTimeMs: performance.now() - start,
      difficulty: this._intrinsicDifficulty(embedding),
    };

    this._classificationCache.set(cacheKey, result);
    return result;
  }

  private _getEmbeddingSync(text: string): number[] {
    const cacheKey = this._embeddingCacheKey(text);
    const cached = this._embeddingCache.get(cacheKey);
    if (cached !== undefined) return cached;

    const embedding = this._provider.embed(text);
    this._embeddingCache.set(cacheKey, embedding);
    return embedding;
  }

  private async _getEmbeddingAsync(text: string): Promise<number[]> {
    const cacheKey = this._embeddingCacheKey(text);
    const cached = this._embeddingCache.get(cacheKey);
    if (cached !== undefined) return cached;

    const embedding = await this._provider.embedAsync(text);
    this._embeddingCache.set(cacheKey, embedding);
    return embedding;
  }

  /** Build the easy/hard difficulty centroids once, from the bundled exemplars. */
  private async _ensureDifficultyCentroidsAsync(): Promise<void> {
    if (this._easyCentroid !== null && this._hardCentroid !== null) return;
    const easy = await this._provider.embedBatchAsync(EASY_EXEMPLARS);
    const hard = await this._provider.embedBatchAsync(HARD_EXEMPLARS);
    this._easyCentroid = vectorNormalize(vectorMean(easy));
    this._hardCentroid = vectorNormalize(vectorMean(hard));
  }

  /**
   * Intrinsic difficulty in [0, 1]: how much closer the prompt sits to the HARD
   * exemplars than the EASY ones, squashed through a logistic. Returns 0 when the
   * difficulty centroids haven't been built (e.g. the sync path).
   */
  private _intrinsicDifficulty(embedding: number[]): number {
    if (this._easyCentroid === null || this._hardCentroid === null) return 0;
    const simHard = cosineSimilarity(embedding, this._hardCentroid);
    const simEasy = cosineSimilarity(embedding, this._easyCentroid);
    return 1 / (1 + Math.exp(-DIFFICULTY_SCALE * (simHard - simEasy)));
  }

  isReady(): boolean {
    return true; // Lazy initialization handles readiness
  }

  /**
   * Embedding cache key. Includes the embedding model name and dimension so
   * vectors from different models/dimensions never collide for the same prompt.
   */
  private _embeddingCacheKey(prompt: string): string {
    return EmbeddingClassifier._hash(
      `${this._provider.modelName} ${this._provider.dimension} ${prompt}`,
    );
  }

  /**
   * Classification cache key. Includes the embedding model name, dimension, and
   * benchmark-set fingerprint (not just md5(prompt)) so cached classifications
   * are invalidated when the model, dimension, or benchmark set changes. Must
   * stay identical to the Python SDK's classification cache key.
   */
  private _classificationCacheKey(prompt: string, centroids: Record<string, number[]>): string {
    const fingerprint = benchmarkFingerprint(Object.keys(centroids));
    return EmbeddingClassifier._hash(
      `${this._provider.modelName} ${this._provider.dimension} ${fingerprint} ${prompt}`,
    );
  }

  private static _hash(value: string): string {
    return createHash('md5').update(value).digest('hex');
  }
}
