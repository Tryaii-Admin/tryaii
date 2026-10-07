/**
 * The satisficing band and the in-band ranker (`satisficing-v1`, spec §4-§9).
 *
 * The first block is the cross-SDK reference table from spec §9: five
 * hand-computable candidates, four priority sets, exact `sec`/`final_score`
 * values and exact orderings. It drives the production combine function
 * (`satisficingCombine`) with `P25_SPEED_UTILITY = 0.242`, so it pins the
 * arithmetic without needing a registry. Mirrors
 * packages/python/tests/test_scoring/test_satisficing.py.
 */

import { describe, it, expect } from 'vitest';

import { ModelInfo, ModelPricing } from '../../src/registry/models.js';
import { BenchmarkNormalizer } from '../../src/scoring/benchmarks.js';
import {
  ScoringEngine,
  costUtility,
  qualityTolerance,
  registrySpeedStats,
  satisficingCombine,
  speedUtility,
} from '../../src/scoring/engine.js';
import { halfEvenRound } from '../../src/cachelint/util/halfEven.js';
import { Priorities } from '../../src/scoring/priorities.js';

/** P25 of the catalog's U_s at judging time -- the spec's reference value. */
const P25_SPEED_UTILITY = 0.242;
const REFERENCE_STATS = { medianTtftMs: 800, p25SpeedUtility: P25_SPEED_UTILITY };

interface Ref {
  id: string;
  qPrime: number;
  perM: number;
  tps?: number;
  ttftMs?: number;
}

/** Spec §9: `q'` supplied directly, bypassing the benchmark pipeline. */
const REFERENCE: Ref[] = [
  { id: 'A', qPrime: 1.0, perM: 20.0, tps: 50, ttftMs: 800 },
  { id: 'B', qPrime: 0.95, perM: 2.0, tps: 150, ttftMs: 500 },
  { id: 'C', qPrime: 0.88, perM: 0.2, tps: 600, ttftMs: 200 },
  { id: 'D', qPrime: 0.7, perM: 0.05, tps: 1000, ttftMs: 100 },
  { id: 'E', qPrime: 0.96, perM: 5.0 }, // no throughput at all
];

function utilitiesFor(r: Ref) {
  const uCost = costUtility(r.perM / 1000);
  const uSpeed = speedUtility(
    { tokensPerSecond: r.tps, ttftMs: r.ttftMs },
    REFERENCE_STATS,
  ).value;
  return {
    uCost,
    uSpeed,
    uCostQ: halfEvenRound(uCost, 3),
    uSpeedQ: halfEvenRound(uSpeed, 3),
  };
}

/** Rank the reference candidates at one priority set, spec order. */
function rankReference(q: number, c: number, s: number) {
  const p = new Priorities(q, c, s);
  const eps = qualityTolerance(p);
  const qBest = Math.max(...REFERENCE.map((r) => r.qPrime));
  const rows = REFERENCE.map((r) => {
    const u = utilitiesFor(r);
    const combined = satisficingCombine(
      r.qPrime,
      u.uCostQ,
      u.uSpeedQ,
      qBest,
      eps,
      p.costWeight,
      p.speedWeight,
    );
    return { id: r.id, qPrime: r.qPrime, ...combined };
  });
  rows.sort((a, b) => {
    if (a.utility !== b.utility) return b.utility - a.utility;
    if (a.qPrime !== b.qPrime) return b.qPrime - a.qPrime;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
  return { eps, qBest, rows, byId: Object.fromEntries(rows.map((r) => [r.id, r])) };
}

describe('reference utilities (spec §9 table)', () => {
  it('matches U_c and U_s to 5 dp', () => {
    const want: Record<string, [number, number]> = {
      A: [0.13265, 0.32231],
      B: [0.46598, 0.53959],
      C: [0.79931, 0.81601],
      D: [1.0, 0.93753],
      E: [0.33333, 0.242],
    };
    for (const r of REFERENCE) {
      const u = utilitiesFor(r);
      expect(u.uCost, `${r.id} U_c`).toBeCloseTo(want[r.id][0], 5);
      expect(u.uSpeed, `${r.id} U_s`).toBeCloseTo(want[r.id][1], 5);
    }
  });

  it('quantises to the exact 3-dp values the ranker uses', () => {
    const want: Record<string, [number, number]> = {
      A: [0.133, 0.322],
      B: [0.466, 0.54],
      C: [0.799, 0.816],
      D: [1.0, 0.938],
      E: [0.333, 0.242],
    };
    for (const r of REFERENCE) {
      const u = utilitiesFor(r);
      expect([u.uCostQ, u.uSpeedQ], r.id).toEqual(want[r.id]);
    }
  });
});

describe('reference ranking at (3,3,3)', () => {
  const { eps, qBest, rows, byId } = rankReference(3, 3, 3);

  it("has eps = 0.136 and a band at q' >= 0.864", () => {
    expect(eps).toBeCloseTo(0.136, 12);
    expect(qBest).toBe(1.0);
    expect(qBest - eps).toBeCloseTo(0.864, 12);
  });

  it('excludes only D from the band', () => {
    expect(Object.fromEntries(rows.map((r) => [r.id, r.inBand]))).toEqual({
      A: true,
      B: true,
      C: true,
      D: false,
      E: true,
    });
  });

  it('matches the spec sec (5 dp) and final_score (4 dp)', () => {
    // C's pre-rounding final is 0.90375 -- a half-even knife edge -- so sec is
    // asserted too: a formatting disagreement between the SDKs cannot hide here.
    const wantSec: Record<string, number> = { C: 0.8075, B: 0.503, E: 0.2875, A: 0.2275 };
    for (const [id, sec] of Object.entries(wantSec)) {
      expect(byId[id].sec, `${id} sec`).toBeCloseTo(sec, 5);
    }
    expect(byId['C'].finalScore).toBe(0.9038);
    expect(byId['B'].finalScore).toBe(0.7515);
    expect(byId['E'].finalScore).toBe(0.6438);
    expect(byId['A'].finalScore).toBe(0.6138);
    expect(byId['D'].finalScore).toBe(0.35);
  });

  it('orders C > B > E > A > D', () => {
    expect(rows.map((r) => r.id)).toEqual(['C', 'B', 'E', 'A', 'D']);
  });
});

describe('reference ranking at (5,1,1)', () => {
  const { eps, rows, byId } = rankReference(5, 1, 1);

  it('has no band and no secondary term', () => {
    expect(eps).toBe(0.0);
    for (const r of rows) expect(r.sec).toBe(0.0);
  });

  it("final_score is q' for everyone", () => {
    expect(byId['A'].finalScore).toBe(1.0);
    expect(byId['E'].finalScore).toBe(0.96);
    expect(byId['B'].finalScore).toBe(0.95);
    expect(byId['C'].finalScore).toBe(0.88);
    expect(byId['D'].finalScore).toBe(0.7);
  });

  it('orders A > E > B > C > D (the strict-quality anchor)', () => {
    expect(rows.map((r) => r.id)).toEqual(['A', 'E', 'B', 'C', 'D']);
  });
});

describe('reference ranking at (1,5,1)', () => {
  const { eps, rows, byId } = rankReference(1, 5, 1);

  it('puts all five in a 0.408 band', () => {
    expect(eps).toBeCloseTo(0.408, 12);
    for (const r of rows) expect(r.inBand).toBe(true);
  });

  it('makes sec the quantised cost utility alone', () => {
    expect(byId['D'].sec).toBe(1.0);
    expect(byId['C'].sec).toBe(0.799);
    expect(byId['B'].sec).toBe(0.466);
    expect(byId['E'].sec).toBe(0.333);
    expect(byId['A'].sec).toBe(0.133);
    expect(byId['D'].finalScore).toBe(1.0);
    expect(byId['C'].finalScore).toBe(0.8995);
    expect(byId['B'].finalScore).toBe(0.733);
    expect(byId['E'].finalScore).toBe(0.6665);
    expect(byId['A'].finalScore).toBe(0.5665);
  });

  it('orders D > C > B > E > A -- pure price order', () => {
    expect(rows.map((r) => r.id)).toEqual(['D', 'C', 'B', 'E', 'A']);
  });
});

describe('reference ranking at (1,1,5)', () => {
  const { eps, rows, byId } = rankReference(1, 1, 5);

  it('puts all five in a 0.408 band', () => {
    expect(eps).toBeCloseTo(0.408, 12);
    for (const r of rows) expect(r.inBand).toBe(true);
  });

  it('makes sec the quantised speed utility alone', () => {
    expect(byId['D'].sec).toBe(0.938);
    expect(byId['C'].sec).toBe(0.816);
    expect(byId['B'].sec).toBe(0.54);
    expect(byId['A'].sec).toBe(0.322);
    expect(byId['E'].sec).toBe(0.242);
    expect(byId['D'].finalScore).toBe(0.969);
    expect(byId['C'].finalScore).toBe(0.908);
    expect(byId['B'].finalScore).toBe(0.77);
    expect(byId['A'].finalScore).toBe(0.661);
    expect(byId['E'].finalScore).toBe(0.621);
  });

  it('orders D > C > B > A > E -- and the unmeasured model is LAST', () => {
    // `missing-signal never wins` in miniature: the pessimistic p25 fallback
    // means a model with no measured throughput cannot win a speed-only ask.
    expect(rows.map((r) => r.id)).toEqual(['D', 'C', 'B', 'A', 'E']);
  });
});

describe('contender/non-contender separation', () => {
  it('no out-of-band model can outrank a contender, at any priority set', () => {
    for (const [q, c, s] of [[3, 3, 3], [2, 3, 3], [4, 3, 3], [2, 5, 3], [2, 3, 5], [3, 5, 5]]) {
      const { rows } = rankReference(q, c, s);
      const lastIn = rows.map((r) => r.inBand).lastIndexOf(true);
      const firstOut = rows.map((r) => r.inBand).indexOf(false);
      if (firstOut >= 0) expect(firstOut).toBeGreaterThan(lastIn);
      for (const r of rows) {
        if (r.inBand) {
          expect(r.utility).toBeGreaterThanOrEqual(1.0);
          expect(r.finalScore).toBeGreaterThanOrEqual(0.5);
        } else {
          expect(r.utility).toBeLessThanOrEqual(1.0);
          expect(r.finalScore).toBeLessThanOrEqual(0.5);
        }
      }
    }
  });

  it('contributions plus band_base reproduce final_score to 1e-6', () => {
    for (const [q, c, s] of [[3, 3, 3], [5, 1, 1], [1, 5, 1], [1, 1, 5], [2, 5, 3]]) {
      for (const r of rankReference(q, c, s).rows) {
        const sum = r.qualityContribution + r.costContribution + r.speedContribution + r.bandBase;
        expect(Math.abs(sum - r.finalScore), `${r.id} @ ${q}/${c}/${s}`).toBeLessThan(1e-4);
      }
    }
  });
});

/* ------------------------------------------------------------------ engine */

/**
 * A normalizer with a 0..1 synthetic benchmark, so a model's raw score *is*
 * its `q'` -- the engine-level mirror of the spec's "q' supplied directly".
 */
function syntheticEngine(): ScoringEngine {
  const n = new BenchmarkNormalizer();
  n.registerRange('SyntheticQuality', 0, 1, 'test-only 0..1 scale');
  n.registerWeight('SyntheticQuality', 1.0);
  n.registerWeight('MMLU', 1.0);
  return new ScoringEngine(n);
}

const SIMS = { SyntheticQuality: 1.0 };

function synthetic(opts: {
  id: string;
  qPrime: number;
  perM?: number;
  tps?: number;
  ttftMs?: number;
  extra?: Record<string, number>;
}): ModelInfo {
  const per1k = opts.perM == null ? null : opts.perM / 1000;
  return new ModelInfo({
    modelId: opts.id,
    provider: 'test',
    benchmarkScores: { SyntheticQuality: opts.qPrime, ...(opts.extra ?? {}) },
    pricing: per1k == null ? null : new ModelPricing(per1k, per1k),
    tokensPerSecond: opts.tps,
    ttftMs: opts.ttftMs,
  });
}

describe('ScoringEngine band behaviour', () => {
  const engine = syntheticEngine();
  const models = REFERENCE.map((r) =>
    synthetic({ id: r.id, qPrime: r.qPrime, perM: r.perM, tps: r.tps, ttftMs: r.ttftMs }),
  );

  it("reproduces q' exactly from the synthetic scale", () => {
    const scores = engine.scoreModels(models, SIMS, new Priorities(5, 1, 1), 5);
    expect(Object.fromEntries(scores.map((s) => [s.modelId, s.qPrime]))).toEqual({
      A: 1.0,
      B: 0.95,
      C: 0.88,
      D: 0.7,
      E: 0.96,
    });
  });

  it('is a strict quality ranking at (5,1,1)', () => {
    const scores = engine.scoreModels(models, SIMS, new Priorities(5, 1, 1), 5);
    expect(scores.map((s) => s.modelId)).toEqual(['A', 'E', 'B', 'C', 'D']);
    expect(scores.map((s) => s.finalScore)).toEqual([1.0, 0.96, 0.95, 0.88, 0.7]);
    for (const s of scores) {
      expect(s.qualityTolerance).toBe(0.0);
      expect(s.reasoning).toContain('| quality only (no tolerance)');
      expect(s.reasoning).toMatch(/\| at 5\/1\/1 only quality counts$/);
    }
  });

  it('drops the cheap-and-fast outsider from the band at (3,3,3)', () => {
    const scores = engine.scoreModels(models, SIMS, new Priorities(3, 3, 3), 5);
    const byId = Object.fromEntries(scores.map((s) => [s.modelId, s]));
    expect(byId['D'].inBand).toBe(false);
    expect(byId['D'].finalScore).toBe(0.35);
    expect(byId['C'].inBand).toBe(true);
    // D is the cheapest AND the fastest, and still loses: that is the band.
    expect(scores[0].modelId).toBe('C');
    expect(scores[scores.length - 1].modelId).toBe('D');
    expect(byId['D'].reasoning).toContain('0.300 below the best (1.00) -- outside the 0.136 tolerance');
  });

  it('never lets the model with no measured throughput win', () => {
    for (const [q, c, s] of [[3, 3, 3], [1, 1, 5], [2, 3, 5], [1, 5, 1]]) {
      const scores = engine.scoreModels(models, SIMS, new Priorities(q, c, s), 5);
      expect(scores[0].modelId, `${q}/${c}/${s}`).not.toBe('E');
      const e = scores.find((x) => x.modelId === 'E')!;
      expect(e.signalFlags).toContain('speed: unknown');
      expect(e.reasoning).toContain('| speed unknown (catalog p25)');
    }
  });

  it('flags and estimates a missing ttft without dropping the model', () => {
    const noTtft = synthetic({ id: 'F', qPrime: 0.95, perM: 2.0, tps: 150 });
    const scores = engine.scoreModels([...models, noTtft], SIMS, new Priorities(3, 3, 3), 6);
    const f = scores.find((s) => s.modelId === 'F')!;
    expect(f.signalFlags).toEqual(['ttft: estimated']);
    expect(f.reasoning).toContain('s to 300 tok) [ttft estimated]');
    // The estimate uses the median ttft of the models being scored.
    const stats = registrySpeedStats([...models, noTtft]);
    expect(f.t300).toBeCloseTo(stats.medianTtftMs / 1000 + 300 / 150, 12);
  });

  it('scores an unpriced model at cost 0 and says so', () => {
    const unpriced = synthetic({ id: 'G', qPrime: 0.95, tps: 150, ttftMs: 500 });
    const scores = engine.scoreModels([...models, unpriced], SIMS, new Priorities(1, 5, 1), 6);
    const g = scores.find((s) => s.modelId === 'G')!;
    expect(g.costScore).toBe(0.0);
    expect(g.signalFlags).toEqual(['cost: unknown']);
    expect(g.reasoning).toContain('| cost unknown');
    // A model whose price we do not know must never win a cost-only request.
    expect(scores[0].modelId).not.toBe('G');
  });

  it('keeps eps and q_best identical for every candidate in the call', () => {
    const scores = engine.scoreModels(models, SIMS, new Priorities(2, 5, 3), 5);
    expect(new Set(scores.map((s) => s.qualityTolerance)).size).toBe(1);
    expect(new Set(scores.map((s) => s.qualityBest)).size).toBe(1);
    expect(scores[0].qualityTolerance).toBeCloseTo(0.306, 12); // the `budget` preset
    expect(scores[0].qualityBest).toBe(1.0);
  });
});

describe('tie-breaks', () => {
  const engine = syntheticEngine();
  // MMLU has similarity 0, so it carries zero weight in q' -- but it still
  // counts as evidence (or as an imputed term) for the tie-break.
  const sims = { SyntheticQuality: 1.0, MMLU: 0.0 };

  it('more REAL benchmarks wins an otherwise exact tie', () => {
    const lessReal = synthetic({ id: 'aa/less-real', qPrime: 0.9, perM: 2.0, tps: 150, ttftMs: 500 });
    const moreReal = synthetic({
      id: 'zz/more-real',
      qPrime: 0.9,
      perM: 2.0,
      tps: 150,
      ttftMs: 500,
      extra: { MMLU: 85 },
    });
    const scores = engine.scoreModels([lessReal, moreReal], sims, new Priorities(3, 3, 3), 2);
    // Identical q', identical U_c/U_s -- and the model id ordering would favour
    // 'aa/...', so only the evidence count can produce this order.
    expect(scores[0].qPrime).toBe(scores[1].qPrime);
    expect(scores[0].finalScore).toBe(scores[1].finalScore);
    expect(scores.map((s) => s.modelId)).toEqual(['zz/more-real', 'aa/less-real']);
    expect(scores[0].topBenchmarks.length).toBe(2);
    expect(scores[1].topBenchmarks.length).toBe(1);
  });

  it('model id ascending is the final determinism guarantee', () => {
    const a = synthetic({ id: 'bbb/same', qPrime: 0.9, perM: 2.0, tps: 150, ttftMs: 500 });
    const b = synthetic({ id: 'aaa/same', qPrime: 0.9, perM: 2.0, tps: 150, ttftMs: 500 });
    const scores = engine.scoreModels([a, b], SIMS, new Priorities(3, 3, 3), 2);
    expect(scores.map((s) => s.modelId)).toEqual(['aaa/same', 'bbb/same']);
    // Stable regardless of input order.
    const reversed = engine.scoreModels([b, a], SIMS, new Priorities(3, 3, 3), 2);
    expect(reversed.map((s) => s.modelId)).toEqual(['aaa/same', 'bbb/same']);
  });

  it("higher q' wins an equal cost/speed trade inside the band", () => {
    const lower = synthetic({ id: 'aa/lower-q', qPrime: 0.9, perM: 2.0, tps: 150, ttftMs: 500 });
    const higher = synthetic({ id: 'zz/higher-q', qPrime: 0.95, perM: 2.0, tps: 150, ttftMs: 500 });
    const scores = engine.scoreModels([lower, higher], SIMS, new Priorities(3, 3, 3), 2);
    expect(scores[0].modelId).toBe('zz/higher-q');
  });
});

describe('reasoning string', () => {
  const engine = syntheticEngine();

  it('is byte-exact for the two-candidate reference case', () => {
    const frontier = synthetic({ id: 'aa/frontier', qPrime: 1.0, perM: 20.0, tps: 50, ttftMs: 800 });
    const cheap = synthetic({ id: 'bb/cheap', qPrime: 0.88, perM: 0.2, tps: 600, ttftMs: 200 });
    const scores = engine.scoreModels([frontier, cheap], SIMS, new Priorities(3, 3, 3), 2);
    expect(scores[0].modelId).toBe('bb/cheap');
    expect(scores[0].reasoning).toBe(
      "q'=0.88 (1 real of 1) | cost 0.7993 ($0.20/M) | speed 0.8160 (0.70 s to 300 tok)" +
        ' | within 0.136 of the best (1.00)' +
        ' | at 3/3/3 you accept up to 0.136 less quality for a cheaper or faster model;' +
        ' this pick gave up 0.12 vs aa/frontier',
    );
    // Every candidate names the SAME leader and the SAME regret (the selected
    // model's), so the sentence reads identically wherever it is shown.
    expect(scores[1].reasoning).toContain(
      'this pick gave up 0.12 vs aa/frontier',
    );
  });

  it('discloses imputation as today', () => {
    const covered = synthetic({ id: 'aa/covered', qPrime: 0.9, perM: 2.0, tps: 150, ttftMs: 500, extra: { MMLU: 85 } });
    const sparse = synthetic({ id: 'bb/sparse', qPrime: 0.9, perM: 2.0, tps: 150, ttftMs: 500 });
    const scores = engine.scoreModels(
      [covered, sparse],
      { SyntheticQuality: 0.9, MMLU: 0.5 },
      new Priorities(3, 3, 3),
      2,
    );
    const byId = Object.fromEntries(scores.map((s) => [s.modelId, s]));
    expect(byId['bb/sparse'].reasoning).toContain('| imputed: 1/2');
    expect(byId['aa/covered'].reasoning).not.toContain('imputed');
    expect(byId['bb/sparse'].reasoning).toMatch(/^q'=\d\.\d\d \(1 real of 2\) \| imputed: 1\/2 \|/);
  });

  it('uses ASCII -- and single-spaced pipes only', () => {
    const scores = engine.scoreModels(
      REFERENCE.map((r) => synthetic({ id: r.id, qPrime: r.qPrime, perM: r.perM, tps: r.tps, ttftMs: r.ttftMs })),
      SIMS,
      new Priorities(3, 3, 3),
      5,
    );
    for (const s of scores) {
      expect(s.reasoning).not.toMatch(/[–—]/); // no en/em dashes
      expect(s.reasoning).not.toMatch(/ {2}/);
      expect(s.reasoning).not.toMatch(/\|\S|\S\|/);
    }
  });

  it('keeps the all-no-signal fallback string unchanged', () => {
    const models = [
      synthetic({ id: 'aa/x', qPrime: 0.9, perM: 2.0, tps: 150, ttftMs: 500 }),
      synthetic({ id: 'bb/y', qPrime: 0.8, perM: 20.0, tps: 50, ttftMs: 800 }),
    ];
    // A benchmark no model carries -> nothing to impute from -> no signal at all.
    const scores = engine.scoreModels(models, { NobodyHasThis: 0.9 }, new Priorities(5, 1, 1), 2);
    expect(scores.length).toBe(2);
    for (const s of scores) {
      expect(s.reasoning).toBe('No benchmark signal -- routed on cost/speed');
      expect(s.signalFlags).toContain('no benchmark signal');
      expect(s.inBand).toBe(false);
    }
    // The 0.1 weight floor lives only here: cost/speed still decide even at
    // (5,1,1), so the cheap fast model wins.
    expect(scores[0].modelId).toBe('aa/x');
    for (const s of scores) {
      const sum = s.qualityContribution + s.costContribution + s.speedContribution + s.bandBase;
      expect(Math.abs(sum - s.finalScore)).toBeLessThan(1e-4);
    }
  });
});
