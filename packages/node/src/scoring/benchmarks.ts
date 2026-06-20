/**
 * Benchmark score normalization.
 *
 * Different benchmarks use different scales (0-100%, ELO ratings, etc.).
 * This module normalizes them all to a 0-1 range for fair comparison.
 */

export class NormalizationRange {
  readonly minScore: number;
  readonly maxScore: number;
  readonly description: string;

  constructor(minScore: number, maxScore: number, description = '') {
    this.minScore = minScore;
    this.maxScore = maxScore;
    this.description = description;
  }

  /** Normalize a raw benchmark score to 0-1. */
  normalize(rawScore: number): number {
    if (this.maxScore === this.minScore) return 0.5;
    const normalized = (rawScore - this.minScore) / (this.maxScore - this.minScore);
    return Math.max(0.0, Math.min(1.0, normalized));
  }
}

/**
 * Standard benchmark normalization ranges.
 *
 * Fit to the observed min/max of the shipped model catalog so it spreads across
 * most of 0-1. Loose ranges crush frontier models into a narrow high band where
 * quality can't differentiate them and routing collapses onto cost/speed; re-fit
 * when the catalog changes substantially. Keep in sync with STANDARD_BENCHMARKS.
 */
export const NORMALIZATION_RANGES: Record<string, NormalizationRange> = {
  'MMLU': new NormalizationRange(40, 96, 'Academic knowledge across 57 subjects'),
  'HellaSwag': new NormalizationRange(68, 99, 'Commonsense reasoning'),
  'HumanEval': new NormalizationRange(30, 97, 'Code generation'),
  'SWE-bench': new NormalizationRange(8, 86, 'Real-world software engineering'),
  'TruthfulQA': new NormalizationRange(40, 86, 'Truthful question answering'),
  'ARC': new NormalizationRange(70, 96, 'Science exam questions'),
  'GSM8K': new NormalizationRange(65, 99, 'Grade school math'),
  'DROP': new NormalizationRange(48, 91, 'Reading comprehension with arithmetic'),
  'SuperGLUE': new NormalizationRange(48, 95, 'Natural language understanding'),
  'Chatbot Arena (LMSys)': new NormalizationRange(1300, 1520, 'Human-rated chat quality'),
  'MT-Bench': new NormalizationRange(6, 10, 'Multi-turn conversation quality'),
  'LiveBench': new NormalizationRange(58, 84, 'Fresh, contamination-resistant evaluation'),
};

/**
 * Normalizes benchmark scores across different scales.
 *
 * Supports standard benchmarks out of the box and allows
 * registering custom normalization ranges.
 */
export class BenchmarkNormalizer {
  private _ranges: Map<string, NormalizationRange>;

  constructor() {
    this._ranges = new Map(Object.entries(NORMALIZATION_RANGES));
  }

  /** Normalize a raw benchmark score to 0-1. */
  normalize(benchmark: string, rawScore: number): number {
    const range = this._ranges.get(benchmark);
    if (!range) {
      // Unknown benchmark -- assume 0-100 percentage scale
      return Math.max(0.0, Math.min(1.0, rawScore / 100.0));
    }
    return range.normalize(rawScore);
  }

  /** Register a custom normalization range for a benchmark. */
  registerRange(
    benchmark: string,
    minScore: number,
    maxScore: number,
    description = '',
  ): void {
    this._ranges.set(benchmark, new NormalizationRange(minScore, maxScore, description));
  }

  /** Get the normalization range for a benchmark. */
  getRange(benchmark: string): NormalizationRange | undefined {
    return this._ranges.get(benchmark);
  }

  /** List all benchmarks with registered normalization ranges. */
  get knownBenchmarks(): string[] {
    return [...this._ranges.keys()];
  }
}
