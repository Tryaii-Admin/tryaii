/**
 * Tests for the ScoringEngine.
 */

import { describe, it, expect } from 'vitest';
import { ScoringEngine } from '../../src/scoring/engine.js';
import { BenchmarkNormalizer, NormalizationRange } from '../../src/scoring/benchmarks.js';
import { Priorities } from '../../src/scoring/priorities.js';
import { ModelInfo, ModelPricing } from '../../src/registry/models.js';

/** Create a mock model for testing. */
function mockModel(opts: {
  modelId: string;
  provider: string;
  benchmarks: Record<string, number>;
  pricing?: [number, number];
  latency?: 'very fast' | 'fast' | 'medium' | 'slow' | 'very slow';
}): ModelInfo {
  return new ModelInfo({
    modelId: opts.modelId,
    provider: opts.provider,
    benchmarkScores: opts.benchmarks,
    pricing: opts.pricing ? new ModelPricing(opts.pricing[0], opts.pricing[1]) : null,
    latency: opts.latency ?? null,
  });
}

/**
 * A fixed, catalog-independent taxonomy for the benchmark names these engine
 * unit tests use (illustrative round numbers, not any catalog's values), so the
 * tests do not depend on which catalog bundle the package ships. Every other name
 * falls back to the engine's defaults (0-100 scale, weight 1.0).
 */
const TEST_RANGES: Record<string, NormalizationRange> = {
  MMLU: new NormalizationRange(65, 95),
  HumanEval: new NormalizationRange(70, 100),
  GSM8K: new NormalizationRange(55, 95),
  LiveBench: new NormalizationRange(0.3, 0.8),
};
const TEST_WEIGHTS: Record<string, number> = { MMLU: 0.8, HumanEval: 0.5, GSM8K: 0.5, LiveBench: 1.0 };

describe('ScoringEngine', () => {
  const engine = new ScoringEngine(new BenchmarkNormalizer(TEST_RANGES, TEST_WEIGHTS));

  const models = [
    mockModel({
      modelId: 'gpt-5',
      provider: 'openai',
      benchmarks: {
        'MMLU': 92,
        'HumanEval': 90,
        'GSM8K': 95,
        'Chatbot Arena (LMSys)': 1400,
        'MT-Bench': 9.5,
      },
      pricing: [0.01, 0.03],
      latency: 'medium',
    }),
    mockModel({
      modelId: 'gpt-4o-mini',
      provider: 'openai',
      benchmarks: {
        'MMLU': 82,
        'HumanEval': 80,
        'GSM8K': 85,
        'Chatbot Arena (LMSys)': 1300,
        'MT-Bench': 8.5,
      },
      pricing: [0.0002, 0.0006],
      latency: 'very fast',
    }),
    mockModel({
      modelId: 'claude-sonnet-4',
      provider: 'anthropic',
      benchmarks: {
        'MMLU': 90,
        'HumanEval': 88,
        'SWE-bench': 75,
        'GSM8K': 92,
        'Chatbot Arena (LMSys)': 1380,
        'MT-Bench': 9.2,
      },
      pricing: [0.003, 0.015],
      latency: 'fast',
    }),
  ];

  it('should score and rank models', () => {
    const benchmarkSimilarities = {
      'HumanEval': 0.8,
      'SWE-bench': 0.6,
      'GSM8K': 0.3,
    };

    const scores = engine.scoreModels(models, benchmarkSimilarities, new Priorities(3, 3, 3));

    expect(scores.length).toBeGreaterThan(0);
    expect(scores.length).toBeLessThanOrEqual(5);

    // Scores should be sorted descending
    for (let i = 1; i < scores.length; i++) {
      expect(scores[i - 1].finalScore).toBeGreaterThanOrEqual(scores[i].finalScore);
    }

    // Each score should have required fields
    for (const s of scores) {
      expect(s.modelId).toBeTruthy();
      expect(s.finalScore).toBeGreaterThanOrEqual(0);
      expect(s.finalScore).toBeLessThanOrEqual(1);
      expect(s.reasoning).toBeTruthy();
    }
  });

  it('should favor quality when quality priority is high', () => {
    const benchmarkSimilarities = {
      'HumanEval': 0.9,
      'SWE-bench': 0.7,
    };

    const qualityPriorities = Priorities.performance(); // quality=5, cost=1, speed=1
    const scores = engine.scoreModels(models, benchmarkSimilarities, qualityPriorities);

    // gpt-5 should rank high for quality
    expect(scores.length).toBeGreaterThan(0);
    const topModel = scores[0];
    expect(topModel.qualityScore).toBeGreaterThan(0);
  });

  it('should favor cost when cost priority is high', () => {
    const benchmarkSimilarities = {
      'HumanEval': 0.8,
      'MMLU': 0.5,
    };

    const budgetPriorities = Priorities.budget(); // quality=2, cost=5, speed=3
    const scores = engine.scoreModels(models, benchmarkSimilarities, budgetPriorities);

    expect(scores.length).toBeGreaterThan(0);
    // gpt-4o-mini should rank higher due to much lower cost
    const miniIndex = scores.findIndex((s) => s.modelId === 'gpt-4o-mini');
    if (miniIndex >= 0) {
      expect(scores[miniIndex].costScore).toBeGreaterThan(0);
    }
  });

  it('should respect topK parameter', () => {
    const benchmarkSimilarities = { 'MMLU': 0.7 };
    const scores = engine.scoreModels(models, benchmarkSimilarities, new Priorities(), 2);
    expect(scores.length).toBeLessThanOrEqual(2);
  });

  it('falls back to neutral quality when a prompt matches no benchmark', () => {
    // No model has this benchmark and there's no median to impute from, so every
    // model is signal-less. Rather than returning nothing -- which would make a
    // single route() throw and a budget run report the whole dataset infeasible
    // -- the engine falls back to a neutral quality so the prompt stays routable
    // on cost/speed, flagged in the reasoning so the case is observable.
    const benchmarkSimilarities = { 'NonExistentBenchmark': 0.9 };
    const scores = engine.scoreModels(models, benchmarkSimilarities, new Priorities(5, 1, 1));
    expect(scores.length).toBe(models.length);
    for (const s of scores) {
      expect(s.reasoning).toContain('No benchmark signal');
    }
    // Quality is neutralised, so cost/speed decide -> the cheap, very-fast model.
    expect(scores[0].modelId).toBe('gpt-4o-mini');
  });

  it('should include reasoning text', () => {
    const benchmarkSimilarities = { 'HumanEval': 0.8 };
    const scores = engine.scoreModels(models, benchmarkSimilarities, new Priorities());

    for (const s of scores) {
      expect(s.reasoning).toMatch(/^q'=\d\.\d\d \(\d real of \d\)/);
      expect(s.reasoning).toContain('| cost ');
      expect(s.reasoning).toContain('| speed ');
      // Exactly one exchange-rate sentence, always last.
      expect(s.reasoning).toMatch(
        / \| at 3\/3\/3 you accept up to 0\.136 less quality for a cheaper or faster model; this pick gave up \d\.\d\d vs \S+$/,
      );
    }
  });

  it('should impute missing benchmark data (shrinkage toward the registry median)', () => {
    // Build a tiny registry where the LiveBench median is unambiguous (0.62).
    // LiveBench is a 0-1 fraction scale in the remote catalog.
    const a = mockModel({ modelId: 'a', provider: 'test', benchmarks: { LiveBench: 0.5 } });
    const b = mockModel({ modelId: 'b', provider: 'test', benchmarks: { LiveBench: 0.62 } });
    const c = mockModel({ modelId: 'c', provider: 'test', benchmarks: { LiveBench: 0.8 } });
    // Missing LiveBench entirely -- should be imputed (blend of its own MMLU
    // level with the 0.62 median) and survive scoring rather than being dropped
    // (which would return scores.length === 3 only).
    const noData = mockModel({ modelId: 'no-data', provider: 'test', benchmarks: { MMLU: 70 } });

    const scores = engine.scoreModels(
      [a, b, c, noData],
      { LiveBench: 0.9 }, // single benchmark for clarity
      Priorities.performance(),
    );

    const noDataScore = scores.find((s) => s.modelId === 'no-data');
    expect(noDataScore).toBeDefined();
    // Reasoning string surfaces the fact that we imputed a benchmark.
    expect(noDataScore!.reasoning).toContain('imputed:');
    // Imputed value = alpha*ownLevel + (1-alpha)*medianNorm with alpha = 1/(1+3):
    // 0.25*norm(MMLU 70) + 0.75*0.62 ~= 0.63 -> between a (0.5) and c (0.8).
    const idx = (id: string) => scores.findIndex((s) => s.modelId === id);
    expect(idx('c')).toBeLessThan(idx('no-data'));
    expect(idx('no-data')).toBeLessThan(idx('a'));
  });

  it('should use the top-5 most relevant benchmarks (not just top-3)', () => {
    // Two models with identical scores on the top-5 of the prompt's 7
    // similarities, but model B has data on the 4th and 5th benchmarks
    // while model A only has data on the top-3. After the change, B should
    // either tie or beat A -- before the change, A would tie B because the
    // 4th and 5th benchmarks weren't considered at all.
    //
    // We assert "no regression vs A": scoring sees benchmarks 4 and 5, not
    // just 1-3, so B's broader coverage matters.
    const aBenchmarks = { MMLU: 90, HumanEval: 90, GSM8K: 90 };
    const bBenchmarks = { ...aBenchmarks, 'SWE-bench': 80, DROP: 80 };
    const a = mockModel({ modelId: 'top3-only', provider: 'test', benchmarks: aBenchmarks });
    const b = mockModel({ modelId: 'top5-coverage', provider: 'test', benchmarks: bBenchmarks });

    const sims = {
      MMLU: 0.50,
      HumanEval: 0.40,
      GSM8K: 0.30,
      'SWE-bench': 0.25, // 4th -- ignored under old top-3 cutoff
      DROP: 0.20,        // 5th -- ignored under old top-3 cutoff
      ARC: 0.10,         // 6th -- still ignored after the change
      TruthfulQA: 0.05,  // 7th -- still ignored after the change
    };

    const scores = engine.scoreModels([a, b], sims, Priorities.performance());
    const aScore = scores.find((s) => s.modelId === 'top3-only')!;
    const bScore = scores.find((s) => s.modelId === 'top5-coverage')!;

    // The reasoning's "imputed: X/5" tag is the cleanest signal that the
    // engine is now considering 5 benchmarks. Under the old top-3 cutoff,
    // benchmarks 4 and 5 wouldn't have been looked at at all -- A would
    // have appeared fully covered and never logged any imputation. The
    // "/5" denominator itself confirms the slice grew from 3 to 5.
    expect(aScore.reasoning).toContain('imputed: 2/5');
    expect(bScore.reasoning).not.toContain('imputed:');
  });

  it('should not let sparse-data models beat fully-covered ones (regression)', () => {
    // Regression for the sparse-data inflation bug:
    //   Old behaviour silently dropped a missing benchmark from the model's
    //   weighted-quality average. A model with `{HumanEval: 95}` only would
    //   score 100% on the only benchmark it had data for, while a model with
    //   `{HumanEval: 95, LiveBench: 60}` got dragged down by including the
    //   LiveBench score -- so the broader-coverage model lost.
    //
    // After the fix (median imputation), the missing benchmark is replaced
    // by the registry median, so the broader-coverage model wins when it
    // matches the prompt better.
    const sparse = mockModel({
      modelId: 'sparse-but-perfect',
      provider: 'test',
      benchmarks: { HumanEval: 95 }, // no LiveBench data at all
      pricing: [0.001, 0.005],
      latency: 'fast',
    });
    const broad = mockModel({
      modelId: 'broad-coverage',
      provider: 'test',
      // Strictly above the registry median for LiveBench (see anchor below)
      // so the broad model has a real edge once the sparse model can't hide
      // its missing data.
      benchmarks: { HumanEval: 95, LiveBench: 80 },
      pricing: [0.001, 0.005],
      latency: 'fast',
    });
    const anchor = mockModel({
      modelId: 'low-livebench-anchor',
      provider: 'test',
      // Anchors the registry median for LiveBench at 65 (median of {50, 80}).
      benchmarks: { HumanEval: 95, LiveBench: 50 },
      pricing: [0.001, 0.005],
      latency: 'fast',
    });

    // Prompt emphasises LiveBench enough that the broad model's actual 80
    // beats the sparse model's imputed 65.
    const benchmarkSimilarities = { HumanEval: 0.5, LiveBench: 0.4 };
    const scores = engine.scoreModels(
      [sparse, broad, anchor],
      benchmarkSimilarities,
      Priorities.performance(), // quality=5, cost=1, speed=1 -- isolate the bug
    );

    const sparseRank = scores.findIndex((s) => s.modelId === 'sparse-but-perfect');
    const broadRank = scores.findIndex((s) => s.modelId === 'broad-coverage');
    expect(sparseRank).toBeGreaterThan(-1);
    expect(broadRank).toBeGreaterThan(-1);
    // Broad-coverage model must strictly outrank the sparse one.
    expect(broadRank).toBeLessThan(sparseRank);
  });

  // The old 0.1..0.95 min-max rescale is gone: finalScore is now an absolute
  // map into [0,1] where contenders sit in [0.5,1] and everyone else in [0,0.5].
  it('maps final scores into [0,1] and splits on the band', () => {
    const benchmarkSimilarities = {
      'MMLU': 0.8,
      'HumanEval': 0.7,
      'GSM8K': 0.6,
    };

    const scores = engine.scoreModels(models, benchmarkSimilarities, new Priorities());

    expect(scores.length).toBeGreaterThan(1);
    for (const s of scores) {
      expect(s.finalScore).toBeGreaterThanOrEqual(0.0);
      expect(s.finalScore).toBeLessThanOrEqual(1.0);
      if (s.inBand) {
        expect(s.finalScore).toBeGreaterThanOrEqual(0.5);
        expect(s.bandBase).toBe(0.5);
      } else {
        expect(s.finalScore).toBeLessThanOrEqual(0.5);
        expect(s.bandBase).toBe(0.0);
      }
    }
  });

  it('contributions plus bandBase reproduce finalScore', () => {
    const sims = { 'MMLU': 0.8, 'HumanEval': 0.7, 'GSM8K': 0.6 };
    for (const p of [new Priorities(3, 3, 3), new Priorities(5, 1, 1), new Priorities(1, 5, 1), new Priorities(1, 1, 5)]) {
      for (const s of engine.scoreModels(models, sims, p)) {
        const sum =
          s.qualityContribution + s.costContribution + s.speedContribution + s.bandBase;
        expect(Math.abs(sum - s.finalScore)).toBeLessThan(1e-4);
      }
    }
  });

  it('exposes the band parts consistently across the candidate set', () => {
    const sims = { 'MMLU': 0.8, 'HumanEval': 0.7 };
    const scores = engine.scoreModels(models, sims, new Priorities(3, 3, 3));
    const eps = scores[0].qualityTolerance;
    expect(eps).toBeCloseTo(0.136, 12);
    for (const s of scores) {
      // eps and q'_best are properties of the CALL, identical for every model.
      expect(s.qualityTolerance).toBe(eps);
      expect(s.qualityBest).toBe(scores[0].qualityBest);
      expect(s.inBand).toBe(s.qPrime >= s.qualityBest - eps);
    }
    expect(Math.max(...scores.map((s) => s.qPrime))).toBe(scores[0].qualityBest);
  });
});
