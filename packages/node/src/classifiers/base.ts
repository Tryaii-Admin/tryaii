/**
 * Abstract base classifier.
 *
 * The embedding classifier implements this interface. The abstraction is
 * kept so custom classifiers can be plugged in for tests or research.
 */

/** Output of any classifier. */
export interface ClassificationResult {
  /** Cosine similarity to each benchmark centroid (0-1). */
  benchmarkScores: Record<string, number>;

  /** Broad category (for display / debugging). */
  broadCategory: string;

  /** Subcategory refinement. */
  subcategory: string;

  /** Confidence of the classification (0-1). */
  confidence: number;

  /** Which classifier produced this result (always "embedding" in the current system). */
  classifierUsed: string;

  /** Whether this result came from cache. */
  cacheHit: boolean;

  /** How long classification took in milliseconds. */
  processingTimeMs: number;

  /**
   * Intrinsic task difficulty in [0, 1] from the easy/hard difficulty centroids
   * (0 = trivial/atomic, 1 = complex/multi-step). Set on the async embedding
   * path; undefined when difficulty centroids aren't available (sync path).
   */
  difficulty?: number;
}

/** Create a default (empty) ClassificationResult. */
export function emptyClassificationResult(): ClassificationResult {
  return {
    benchmarkScores: {},
    broadCategory: '',
    subcategory: '',
    confidence: 0,
    classifierUsed: '',
    cacheHit: false,
    processingTimeMs: 0,
    difficulty: 0,
  };
}

/** Get top benchmarks sorted by similarity score (descending). */
export function topBenchmarks(result: ClassificationResult): Array<[string, number]> {
  return Object.entries(result.benchmarkScores)
    .sort((a, b) => b[1] - a[1]);
}

/**
 * Abstract base class for prompt classifiers.
 *
 * A classifier takes a user prompt and returns benchmark similarity scores.
 * These scores tell us "what kind of task is this?" in terms of which
 * AI benchmarks it most resembles.
 */
export abstract class BaseClassifier {
  /**
   * Classify a prompt synchronously.
   *
   * Implementations whose embedding backend is async-only should throw
   * a clear error here -- callers should use `classifyAsync` instead.
   *
   * @param prompt - The user's input text.
   * @returns ClassificationResult with benchmarkScores populated.
   */
  abstract classify(prompt: string): ClassificationResult;

  /**
   * Classify a prompt asynchronously.
   *
   * Default implementation wraps `classify()` so sync classifiers work
   * out of the box on the async path. Async-only classifiers must override.
   */
  async classifyAsync(prompt: string): Promise<ClassificationResult> {
    return this.classify(prompt);
  }

  /** Check if the classifier is initialized and ready to use. */
  abstract isReady(): boolean;
}
