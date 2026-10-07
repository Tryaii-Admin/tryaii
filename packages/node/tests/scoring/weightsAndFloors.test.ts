/**
 * Tests for the remote-catalog scoring additions (Node side).
 *
 * Covers BENCHMARK_WEIGHTS in the quality aggregation, plausibility-floor
 * filtering in ModelInfo.fromDict, shrinkage imputation, and the remote
 * benchmark scales. Mirrors
 * packages/python/tests/test_scoring/test_weights_and_floors.py.
 */

import { describe, it, expect } from 'vitest';

import { ModelInfo, ModelPricing } from '../../src/registry/models.js';
import {
  BENCHMARK_WEIGHTS,
  DEFAULT_BENCHMARK_WEIGHT,
  BenchmarkNormalizer,
  NORMALIZATION_RANGES,
  isImplausibleBenchmarkScore,
  rangesFromBundle,
} from '../../src/scoring/benchmarks.js';
import { ScoringEngine } from '../../src/scoring/engine.js';
import { Priorities } from '../../src/scoring/priorities.js';
import { starterBundle } from '../../src/catalog/bundle.js';
import { FULL_ONLY, HAS_FULL_BUNDLE, fullBundle } from '../_catalog.js';

const QUALITY_ONLY = new Priorities(5, 1, 1);

function model(modelId: string, benchmarks: Record<string, number>): ModelInfo {
  return new ModelInfo({
    modelId,
    provider: 'test',
    benchmarkScores: benchmarks,
    pricing: new ModelPricing(0.001, 0.002),
    latency: 'fast',
  });
}

describe('normalizer weights', () => {
  it('returns the table weight for known benchmarks', () => {
    const n = new BenchmarkNormalizer();
    expect(n.getWeight('LiveCodeBench')).toBe(BENCHMARK_WEIGHTS['LiveCodeBench']);
  });

  it.skipIf(!HAS_FULL_BUNDLE)(`carries the full catalog weights via fromBundle ${FULL_ONLY}`, () => {
    const bundle = fullBundle();
    const n = BenchmarkNormalizer.fromBundle(bundle);
    const weights = bundle.benchmarkWeights();
    expect(Object.keys(weights).length).toBeGreaterThan(Object.keys(starterBundle().benchmarkWeights()).length);
    for (const [name, w] of Object.entries(weights)) expect(n.getWeight(name)).toBe(w);
    expect(n.getWeight('MyCustomBench')).toBe(DEFAULT_BENCHMARK_WEIGHT);
  });

  it('defaults to neutral for unknown benchmarks', () => {
    const n = new BenchmarkNormalizer();
    expect(n.getWeight('MyCustomBench')).toBe(DEFAULT_BENCHMARK_WEIGHT);
  });

  it('registerWeight overrides without leaking across instances', () => {
    const a = new BenchmarkNormalizer();
    a.registerWeight('GPQA', 3.0);
    expect(a.getWeight('GPQA')).toBe(3.0);
    expect(new BenchmarkNormalizer().getWeight('GPQA')).toBe(BENCHMARK_WEIGHTS['GPQA']);
  });

  it('tilts ranking toward the higher-trust benchmark', () => {
    // HLE (1.4) vs AIME-2024 (0.9), both in the starter catalog.
    const engine = new ScoringEngine();
    const n = new BenchmarkNormalizer();
    expect(n.getWeight('HLE')).toBeGreaterThan(n.getWeight('AIME-2024'));
    const hle = n.getRange('HLE')!;
    const aime = n.getRange('AIME-2024')!;
    const hleStrong = model('hle-strong', { HLE: hle.maxScore, 'AIME-2024': aime.minScore });
    const aimeStrong = model('aime-strong', { HLE: hle.minScore, 'AIME-2024': aime.maxScore });
    const sims = { HLE: 0.6, 'AIME-2024': 0.6 };
    const scores = engine.scoreModels([hleStrong, aimeStrong], sims, QUALITY_ONLY);
    expect(scores[0].modelId).toBe('hle-strong');
  });

  it('all-neutral weights reproduce similarity-only quality', () => {
    const n = new BenchmarkNormalizer();
    for (const name of Object.keys(BENCHMARK_WEIGHTS)) n.registerWeight(name, 1.0);
    const engine = new ScoringEngine(n);
    const m = model('m', { 'SWE-bench-verified': 40, 'GSM8K': 60 });
    const sims = { 'SWE-bench-verified': 0.8, 'GSM8K': 0.4 };
    const [score] = engine.scoreModels([m], sims, QUALITY_ONLY, 1);
    const expected =
      (0.8 * n.normalize('SWE-bench-verified', 40) + 0.4 * n.normalize('GSM8K', 60)) / (0.8 + 0.4);
    expect(Math.abs(score.qualityScore - expected)).toBeLessThan(1e-4);
  });
});

describe('plausibility floors', () => {
  it('flags below-floor scores as implausible', () => {
    expect(isImplausibleBenchmarkScore('GPQA', 5.0)).toBe(true);
    expect(isImplausibleBenchmarkScore('MMLU-Pro', 4.9)).toBe(true);
  });

  it.skipIf(!HAS_FULL_BUNDLE)(`takes the floors from the catalog ${FULL_ONLY}`, () => {
    const floors = fullBundle().randomChanceFloors();
    // MMLU is a full-catalog benchmark: no floor in the starter tables.
    const below = floors['MMLU'] - 0.1;
    expect(isImplausibleBenchmarkScore('MMLU', below)).toBe(false);
    expect(isImplausibleBenchmarkScore('MMLU', below, floors)).toBe(true);
    // The full catalog keeps every starter floor and adds its own.
    const starterFloors = starterBundle().randomChanceFloors();
    for (const [name, f] of Object.entries(starterFloors)) expect(floors[name]).toBe(f);
    expect(Object.keys(floors).length).toBeGreaterThan(Object.keys(starterFloors).length);
  });

  it('keeps at-floor and above-floor scores', () => {
    expect(isImplausibleBenchmarkScore('GPQA', 10.0)).toBe(false);
    expect(isImplausibleBenchmarkScore('GPQA', 55.0)).toBe(false);
  });

  it('never flags floor-less (open-ended) benchmarks', () => {
    expect(isImplausibleBenchmarkScore('MATH', 0.5)).toBe(false);
    expect(isImplausibleBenchmarkScore('AIME-2025', 1.0)).toBe(false);
  });

  it('fromDict drops implausible scores and keeps the rest', () => {
    const m = ModelInfo.fromDict({
      model_id: 'x/y',
      provider: 'x',
      benchmark_scores: { GPQA: 1.3, MATH: 90.0, MMLU: 80.0 },
    });
    expect(m.benchmarkScores).toEqual({ MATH: 90.0, MMLU: 80.0 });
  });

  it('fromDict keeps low scores on floor-less benchmarks', () => {
    const m = ModelInfo.fromDict({
      model_id: 'x/y',
      provider: 'x',
      benchmark_scores: { 'AIME-2024': 0.7 },
    });
    expect(m.benchmarkScores).toEqual({ 'AIME-2024': 0.7 });
  });
});

describe('shrinkage imputation', () => {
  it('a high-coverage strong model imputes above the registry median', () => {
    const engine = new ScoringEngine();
    const weakA = model('weak-a', { MATH: 20 });
    const weakB = model('weak-b', { MATH: 40 });
    const strong = model('strong', {
      'AIME-2024': 90, 'AIME-2025': 90, 'MMLU-Pro': 81,
      'LiveCodeBench': 81, 'SWE-bench-verified': 72, 'GPQA': 85.5,
    });
    const scores = engine.scoreModels([weakA, weakB, strong], { MATH: 0.9 }, QUALITY_ONLY);
    const byId = Object.fromEntries(scores.map((s) => [s.modelId, s]));
    expect(byId['strong'].qualityScore).toBeGreaterThan(byId['weak-b'].qualityScore);
    expect(byId['strong'].reasoning).toContain('imputed: 1/1');
  });

  it('a sparse model stays near the median (cannot self-inflate)', () => {
    const engine = new ScoringEngine();
    const n = new BenchmarkNormalizer();
    const a = model('a', { MATH: 40 });
    const b = model('b', { MATH: 60 });
    const sparse = model('sparse', { 'AIME-2024': 100 });
    const scores = engine.scoreModels([a, b, sparse], { MATH: 1.0 }, QUALITY_ONLY);
    const byId = Object.fromEntries(scores.map((s) => [s.modelId, s]));
    const alpha = 1 / (1 + 3);
    const expected = alpha * 1.0 + (1 - alpha) * n.normalize('MATH', 50);
    expect(Math.abs(byId['sparse'].qualityScore - expected)).toBeLessThan(1e-3);
  });

  it('a benchmark nobody has is still skipped (nothing to impute from)', () => {
    const engine = new ScoringEngine();
    const m1 = model('m1', { MATH: 80 });
    const m2 = model('m2', { MATH: 60 });
    const scores = engine.scoreModels([m1, m2], { MATH: 0.9, HLE: 0.8 }, QUALITY_ONLY);
    expect(new Set(scores.map((s) => s.modelId))).toEqual(new Set(['m1', 'm2']));
    for (const s of scores) expect(s.reasoning).not.toContain('imputed');
  });
});

// The ranges are no longer hand-written judgement calls: each one is the
// catalog's p25..max of that benchmark's REAL scores (the bundle's
// normalization_ranges.json), so the assertions are made against
// the generated table rather than against remembered constants. A catalog sync
// moves the numbers; it must not move the *shape*. LiveBench and MMLU are
// full-catalog benchmarks, so this suite runs on the full bundle.
describe.skipIf(!HAS_FULL_BUNDLE)(`catalog-derived scale normalization ${FULL_ONLY}`, () => {
  const EXAMPLES = ['Chatbot Arena Elo', 'LiveBench', 'LiveBench-Coding', 'SciCode', 'HLE', 'MMLU'];
  const fullRanges = () => rangesFromBundle(fullBundle());
  const fullNormalizer = () => BenchmarkNormalizer.fromBundle(fullBundle());

  it('maps p25 to 0, max to 1 and the midpoint to 0.5 on every scale', () => {
    const n = fullNormalizer();
    for (const name of EXAMPLES) {
      const r = fullRanges()[name];
      expect(r, name).toBeDefined();
      expect(n.normalize(name, r.minScore)).toBe(0.0);
      expect(n.normalize(name, r.maxScore)).toBe(1.0);
      expect(Math.abs(n.normalize(name, (r.minScore + r.maxScore) / 2) - 0.5)).toBeLessThan(1e-9);
    }
  });

  it('clamps values outside the catalog range', () => {
    const n = fullNormalizer();
    for (const name of EXAMPLES) {
      const r = fullRanges()[name];
      const span = r.maxScore - r.minScore;
      expect(n.normalize(name, r.minScore - span)).toBe(0.0);
      expect(n.normalize(name, r.maxScore + span)).toBe(1.0);
    }
  });

  it('keeps each benchmark on its own units (ELO, 0-1 fractions, percentages)', () => {
    // Pinned from the shipped catalog: the generator is what sets these, and a
    // change here is a CHANGELOG-worthy routing event, not an accident.
    const ranges = fullRanges();
    const elo = ranges['Chatbot Arena Elo'];
    expect([elo.minScore, elo.maxScore]).toEqual([1312.5974, 1504.9163]);
    const live = ranges['LiveBench'];
    expect(live.minScore).toBeGreaterThan(0);
    expect(live.minScore).toBeLessThan(live.maxScore);
    expect(live.maxScore).toBeLessThanOrEqual(1.0); // 0-1 fraction scale
    // Shared benchmarks score identically on both catalogs.
    expect(NORMALIZATION_RANGES['Chatbot Arena Elo']).toEqual(elo);
    const hle = ranges['HLE'];
    expect([hle.minScore, hle.maxScore]).toEqual([6.4, 61.4]);
    // A hard benchmark's headroom is far below 100: that is the whole point of
    // deriving the range from the catalog instead of assuming 0..100.
    expect(hle.maxScore).toBeLessThan(100);
    expect(ranges['SciCode'].maxScore).toBeLessThan(100);
  });

  it('gives frontier models real dispersion instead of a 0.93-1.00 sliver', () => {
    // The regression the new ranges exist to fix: under the old hand-set floors
    // every strong Arena score normalised to ~1. With p25..max, a 20-point ELO
    // gap near the top is clearly visible.
    const n = new BenchmarkNormalizer();
    const r = NORMALIZATION_RANGES['Chatbot Arena Elo'];
    const high = n.normalize('Chatbot Arena Elo', r.maxScore - 20);
    const mid = n.normalize('Chatbot Arena Elo', r.maxScore - 60);
    expect(high - mid).toBeGreaterThan(0.1);
  });
});
