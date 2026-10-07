/**
 * Coverage-aware benchmark weighting.
 *
 * The per-term quality weight is
 *
 *     w(b, m) = similarity(b) * BENCHMARK_WEIGHTS[b]
 *               * coverage(b) ** COVERAGE_EXPONENT
 *               * (IMPUTED_TERM_WEIGHT if m's term for b is imputed else 1)
 *
 * where `coverage(b)` is the fraction of the ROUTABLE model set (everything
 * but `:free` ids) that reports a real, finite value for `b`. Without it, much
 * of the selected benchmark weight could land on benchmarks only a small
 * minority of models report, so frontier models were judged largely on imputed
 * values (see docs/sdk/routing/scoring.md).
 *
 * The fixture below is hand-computable: three custom benchmarks on four models
 * with coverage exactly 1.0 / 0.5 / 0.25, custom 0-100 normalization ranges and
 * custom importance weights, so every expected number in this file is written
 * out longhand in its comment.
 *
 * Mirrors packages/python/tests/test_scoring/test_coverage_weighting.py.
 */

import { describe, it, expect } from 'vitest';

import * as pkg from '../../src/index.js';
import {
  ModelInfo,
  ModelPricing,
  ModelRegistry,
  computeBenchmarkCoverage,
} from '../../src/registry/models.js';
import { BenchmarkNormalizer } from '../../src/scoring/benchmarks.js';
import * as scoring from '../../src/scoring/index.js';
import {
  COVERAGE_EXPONENT,
  IMPUTED_TERM_WEIGHT,
  ScoringEngine,
} from '../../src/scoring/engine.js';
import { Priorities } from '../../src/scoring/priorities.js';
import { FULL_ONLY, HAS_FULL_BUNDLE, fullBundle } from '../_catalog.js';

const QUALITY_ONLY = new Priorities(5, 1, 1);

const FULL = 'COV-FULL';
const HALF = 'COV-HALF';
const QUARTER = 'COV-QUARTER';
const NONE = 'COV-NONE';

/** Importance weights, registered on the normalizer (not the shipped table). */
const WEIGHT: Record<string, number> = { [FULL]: 1.0, [HALF]: 2.0, [QUARTER]: 0.5 };

/** Prompt similarities; NONE is deliberately the *highest* so it is selected. */
const SIMILARITIES: Record<string, number> = {
  [NONE]: 0.9,
  [FULL]: 0.5,
  [HALF]: 0.4,
  [QUARTER]: 0.2,
};

function model(modelId: string, benchmarks: Record<string, number>): ModelInfo {
  return new ModelInfo({
    modelId,
    provider: 'test',
    benchmarkScores: benchmarks,
    pricing: new ModelPricing(0.001, 0.002),
    tokensPerSecond: 100,
    ttftMs: 500,
  });
}

/**
 * 4 routable models. COV-FULL: all four (coverage 1.0). COV-HALF: m1, m2
 * (0.5). COV-QUARTER: m1 only (0.25). COV-NONE: nobody (absent -> 0).
 */
function fixtureModels(): ModelInfo[] {
  return [
    model('t/m1', { [FULL]: 80, [HALF]: 60, [QUARTER]: 40 }),
    model('t/m2', { [FULL]: 70, [HALF]: 50 }),
    model('t/m3', { [FULL]: 60 }),
    model('t/m4', { [FULL]: 50 }),
  ];
}

function fixtureEngine(): ScoringEngine {
  const normalizer = new BenchmarkNormalizer();
  for (const name of [FULL, HALF, QUARTER, NONE]) {
    normalizer.registerRange(name, 0, 100);
  }
  for (const [name, w] of Object.entries(WEIGHT)) normalizer.registerWeight(name, w);
  normalizer.registerWeight(NONE, 1.0);
  return new ScoringEngine(normalizer);
}

function scoreFixture() {
  const scores = fixtureEngine().scoreModels(fixtureModels(), SIMILARITIES, QUALITY_ONLY, 10);
  return new Map(scores.map((s) => [s.modelId, s]));
}

describe('exported constants', () => {
  it('pins the two dials of coverage weighting', () => {
    expect(IMPUTED_TERM_WEIGHT).toBe(0.5);
    expect(COVERAGE_EXPONENT).toBe(1.0);
  });

  it('re-exports them from scoring/index and the package root', () => {
    expect(scoring.IMPUTED_TERM_WEIGHT).toBe(IMPUTED_TERM_WEIGHT);
    expect(scoring.COVERAGE_EXPONENT).toBe(COVERAGE_EXPONENT);
    expect(scoring.computeBenchmarkCoverage).toBe(computeBenchmarkCoverage);
    expect(pkg.IMPUTED_TERM_WEIGHT).toBe(IMPUTED_TERM_WEIGHT);
    expect(pkg.COVERAGE_EXPONENT).toBe(COVERAGE_EXPONENT);
  });
});

describe('computeBenchmarkCoverage', () => {
  it('is n_real / N over the routable set', () => {
    const coverage = computeBenchmarkCoverage(fixtureModels());
    expect(coverage[FULL]).toBe(1.0);
    expect(coverage[HALF]).toBe(0.5);
    expect(coverage[QUARTER]).toBe(0.25);
    // A benchmark nobody reports is absent, not 0 -- callers read the miss as 0.
    expect(coverage[NONE]).toBeUndefined();
    expect(Object.keys(coverage).sort()).toEqual([FULL, HALF, QUARTER].sort());
  });

  it('excludes :free variants from both n_real and N', () => {
    const models = [
      ...fixtureModels(),
      model('t/m5:free', { [FULL]: 90, [HALF]: 90 }),
    ];
    const coverage = computeBenchmarkCoverage(models);
    expect(coverage[FULL]).toBe(1.0); // 4/4, not 5/5
    expect(coverage[HALF]).toBe(0.5); // 2/4, not 3/5
  });

  it('ignores NaN/Infinity as not-reported', () => {
    const coverage = computeBenchmarkCoverage([
      model('t/a', { [FULL]: 50 }),
      model('t/b', { [FULL]: Number.NaN }),
      model('t/c', { [FULL]: Number.POSITIVE_INFINITY }),
      model('t/d', {}),
    ]);
    expect(coverage[FULL]).toBe(0.25);
  });

  it('is empty for an empty model set', () => {
    expect(computeBenchmarkCoverage([])).toEqual({});
  });
});

describe('effective per-term weights', () => {
  it('matches similarity * weight * coverage for an all-real model', () => {
    // m1 reports all three:
    //   FULL    0.5 * 1.0 * 1.00 * 1 = 0.5
    //   HALF    0.4 * 2.0 * 0.50 * 1 = 0.4
    //   QUARTER 0.2 * 0.5 * 0.25 * 1 = 0.025
    const s = scoreFixture().get('t/m1')!;
    expect(s.benchmarkWeights).toEqual({ [FULL]: 0.5, [HALF]: 0.4, [QUARTER]: 0.025 });
  });

  it('halves an imputed term', () => {
    // m2 has FULL and HALF for real; QUARTER is imputed:
    //   QUARTER 0.2 * 0.5 * 0.25 * 0.5 = 0.0125
    const s = scoreFixture().get('t/m2')!;
    expect(s.benchmarkWeights).toEqual({ [FULL]: 0.5, [HALF]: 0.4, [QUARTER]: 0.0125 });
  });

  it('halves every imputed term independently', () => {
    // m3 has only FULL; HALF and QUARTER are both imputed:
    //   HALF    0.4 * 2.0 * 0.50 * 0.5 = 0.2
    //   QUARTER 0.2 * 0.5 * 0.25 * 0.5 = 0.0125
    const s = scoreFixture().get('t/m3')!;
    expect(s.benchmarkWeights).toEqual({ [FULL]: 0.5, [HALF]: 0.2, [QUARTER]: 0.0125 });
  });

  it('rounds the published weights to 4 dp', () => {
    for (const s of scoreFixture().values()) {
      for (const w of Object.values(s.benchmarkWeights)) {
        expect(w).toBeCloseTo(Number(w.toFixed(4)), 12);
      }
    }
  });
});

describe('zero coverage contributes nothing', () => {
  it('omits the unreported benchmark from the weights entirely', () => {
    for (const s of scoreFixture().values()) {
      expect(Object.keys(s.benchmarkWeights)).not.toContain(NONE);
    }
  });

  it('leaves q1 unchanged when a zero-coverage benchmark is added', () => {
    const engine = fixtureEngine();
    const models = fixtureModels();
    const withoutNone = { [FULL]: 0.5, [HALF]: 0.4, [QUARTER]: 0.2 };
    const a = engine.scoreModels(models, withoutNone, QUALITY_ONLY, 10);
    const b = fixtureEngine().scoreModels(models, SIMILARITIES, QUALITY_ONLY, 10);
    const byId = new Map(b.map((s) => [s.modelId, s]));
    for (const s of a) {
      expect(byId.get(s.modelId)!.qPrime).toBeCloseTo(s.qPrime, 12);
    }
  });

  it('falls back to the no-signal path when every term has zero weight', () => {
    // Only the zero-coverage benchmark is relevant -> no model has any weight.
    const scores = fixtureEngine().scoreModels(
      fixtureModels(),
      { [NONE]: 0.9 },
      QUALITY_ONLY,
      10,
    );
    expect(scores).toHaveLength(4);
    for (const s of scores) {
      expect(s.signalFlags).toContain('no benchmark signal');
      expect(s.qPrime).toBe(0.5);
      expect(s.benchmarkWeights).toEqual({});
      expect(s.reasoning).toBe('No benchmark signal -- routed on cost/speed');
    }
  });
});

describe("q' with the coverage-aware weights", () => {
  it('is the hand-computed weighted mean for an all-real model', () => {
    // (0.5*0.8 + 0.4*0.6 + 0.025*0.4) / (0.5 + 0.4 + 0.025)
    //   = 0.65 / 0.925 = 0.702702702702...
    const s = scoreFixture().get('t/m1')!;
    expect(s.qPrime).toBeCloseTo(0.65 / 0.925, 12);
    expect(s.qualityScore).toBe(0.7027);
  });

  it('is the hand-computed weighted mean with one imputed term', () => {
    // m2's own level = median([0.7, 0.5]) = 0.6 over 2 benchmarks, so
    // alpha = 2 / (2 + 3) = 0.4. The QUARTER registry median is 40 -> 0.4, so
    // the imputed value is 0.4*0.6 + 0.6*0.4 = 0.48.
    // (0.5*0.7 + 0.4*0.5 + 0.0125*0.48) / (0.5 + 0.4 + 0.0125)
    //   = 0.556 / 0.9125 = 0.609315068493...
    const s = scoreFixture().get('t/m2')!;
    expect(s.qPrime).toBeCloseTo(0.556 / 0.9125, 12);
    expect(s.qualityScore).toBe(0.6093);
  });

  it('is the hand-computed weighted mean with two imputed terms', () => {
    // m3's level = median([0.6]) = 0.6 over 1 benchmark -> alpha = 1/4 = 0.25.
    // HALF median raw = median([60, 50]) = 55 -> 0.55;
    //   imputed = 0.25*0.6 + 0.75*0.55 = 0.5625
    // QUARTER median raw = 40 -> 0.4; imputed = 0.25*0.6 + 0.75*0.4 = 0.45
    // (0.5*0.6 + 0.2*0.5625 + 0.0125*0.45) / (0.5 + 0.2 + 0.0125)
    //   = 0.418125 / 0.7125 = 0.586842105263...
    const s = scoreFixture().get('t/m3')!;
    expect(s.qPrime).toBeCloseTo(0.418125 / 0.7125, 12);
    expect(s.qualityScore).toBe(0.5868);
  });

  it('is q1 itself as the final score at (5,1,1)', () => {
    for (const s of scoreFixture().values()) {
      expect(s.finalScore).toBeCloseTo(Number(s.qPrime.toFixed(4)), 12);
    }
  });

  it('leaves the reasoning string free of coverage (byte parity)', () => {
    const s = scoreFixture().get('t/m2')!;
    expect(s.reasoning).toContain("q'=0.61 (2 real of 4)");
    // 4 selected benchmarks, 1 imputed term (COV-NONE is skipped, not imputed).
    expect(s.reasoning).toContain('imputed: 1/4');
    expect(s.reasoning.toLowerCase()).not.toContain('coverage');
  });
});

describe('selection is unchanged', () => {
  it('still takes the top 5 benchmarks by raw similarity', () => {
    const engine = fixtureEngine();
    const normalizer = new BenchmarkNormalizer();
    for (const name of [FULL, HALF, QUARTER, NONE, 'COV-SIXTH']) {
      normalizer.registerRange(name, 0, 100);
    }
    // Six candidates; the lowest-similarity one must be dropped by selection
    // (not by coverage), so it never appears in the published weights.
    const sims = { ...SIMILARITIES, 'COV-EXTRA': 0.95, 'COV-SIXTH': 0.01 };
    const models = fixtureModels().map((m) =>
      model(m.modelId, { ...m.benchmarkScores, 'COV-SIXTH': 90 }),
    );
    const scores = engine.scoreModels(models, sims, QUALITY_ONLY, 10);
    for (const s of scores) {
      expect(Object.keys(s.benchmarkWeights)).not.toContain('COV-SIXTH');
    }
  });
});

describe('the shipped (starter) catalog', () => {
  const coverage = ModelRegistry.default().benchmarkCoverage();

  it('covers every benchmark the catalog reports', () => {
    expect(Object.keys(coverage)).toHaveLength(16);
  });

  it('is a fraction in [0, 1] for every benchmark', () => {
    for (const [name, value] of Object.entries(coverage)) {
      expect(value, name).toBeGreaterThan(0);
      expect(value, name).toBeLessThanOrEqual(1);
    }
  });

  it('ranks a thin benchmark below a widely-reported one', () => {
    // Terminal-bench-Hard is reported by fewer starter models than LiveCodeBench.
    expect(coverage['Terminal-bench-Hard']).toBeLessThan(coverage['LiveCodeBench']);
  });

  it('memoises until the registry is mutated', () => {
    const registry = ModelRegistry.default();
    const first = registry.benchmarkCoverage();
    expect(registry.benchmarkCoverage()).toBe(first);
    registry.add({ modelId: 't/new', provider: 'test', benchmarks: { 'COV-NEW': 1 } });
    const second = registry.benchmarkCoverage();
    expect(second).not.toBe(first);
    expect(second['COV-NEW']).toBeGreaterThan(0);
  });
});

describe.skipIf(!HAS_FULL_BUNDLE)(`the full catalog ${FULL_ONLY}`, () => {
  it('covers every benchmark the catalog reports', () => {
    const coverage = ModelRegistry.fromBundle(fullBundle()).benchmarkCoverage();
    expect(Object.keys(coverage)).toHaveLength(33);
    // GSM8K is a saturated legacy benchmark few current models report;
    // LiveCodeBench is reported by a large share of the catalog.
    expect(coverage['GSM8K']).toBeLessThan(coverage['LiveCodeBench']);
  });
});
