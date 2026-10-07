/**
 * Cost and speed utilities (`satisficing-v1`, spec §2/§3).
 *
 * The log anchors, the clamps, the two speed fallbacks and the runtime
 * registry statistics that feed them. Mirrors
 * packages/python/tests/test_scoring/test_utilities.py -- both SDKs must agree
 * to 1e-9 on every number here.
 */

import { describe, it, expect } from 'vitest';

import { ModelInfo, ModelPricing, ModelRegistry } from '../../src/registry/models.js';
import {
  EPS_UNIT,
  FALLBACK_MEDIAN_TTFT_MS,
  FALLBACK_P25_SPEED_UTILITY,
  avgPricePer1k,
  costUtility,
  qualityTolerance,
  registrySpeedStats,
  speedUtility,
  speedUtilityFromT300,
  t300Seconds,
} from '../../src/scoring/engine.js';
import { Priorities } from '../../src/scoring/priorities.js';

/** The utilities take $/1k; the spec quotes $/M. */
function perM(dollarsPerMillion: number): number {
  return dollarsPerMillion / 1000;
}

const STATS = { medianTtftMs: 800, p25SpeedUtility: 0.242 };

describe('cost utility', () => {
  it('anchors at $0.05/M -> 1 and $50/M -> 0', () => {
    expect(costUtility(perM(0.05))).toBeCloseTo(1.0, 12);
    expect(costUtility(perM(50.0))).toBeCloseTo(0.0, 12);
  });

  it('is exactly 1/3 per decade (three decades over the range)', () => {
    // $50 -> $5 -> $0.50 -> $0.05
    expect(costUtility(perM(5.0))).toBeCloseTo(1 / 3, 12);
    expect(costUtility(perM(0.5))).toBeCloseTo(2 / 3, 12);
    const a = costUtility(perM(2.0));
    const b = costUtility(perM(0.2));
    expect(b - a).toBeCloseTo(1 / 3, 12);
  });

  it('matches the spec reference values for the five synthetic candidates', () => {
    expect(costUtility(perM(20.0))).toBeCloseTo(0.13265, 5);
    expect(costUtility(perM(2.0))).toBeCloseTo(0.46598, 5);
    expect(costUtility(perM(0.2))).toBeCloseTo(0.79931, 5);
    expect(costUtility(perM(0.05))).toBeCloseTo(1.0, 5);
    expect(costUtility(perM(5.0))).toBeCloseTo(0.33333, 5);
  });

  it('clamps outside the anchors', () => {
    expect(costUtility(perM(0.001))).toBe(1.0);
    expect(costUtility(perM(500.0))).toBe(0.0);
  });

  it('is 1.0 for a free model and 0.0 when the price is unknown', () => {
    // Zero, not neutral: a model whose price we do not know must never win a
    // cost comparison.
    expect(costUtility(0)).toBe(1.0);
    expect(costUtility(null)).toBe(0.0);
    expect(costUtility(undefined)).toBe(0.0);
    expect(costUtility(NaN)).toBe(0.0);
    expect(costUtility(Infinity)).toBe(0.0);
  });

  it('blends input and output 50:50', () => {
    const m = new ModelInfo({
      modelId: 'x/y',
      provider: 'x',
      benchmarkScores: {},
      pricing: new ModelPricing(0.001, 0.003),
    });
    expect(avgPricePer1k(m)).toBeCloseTo(0.002, 12);
    expect(avgPricePer1k(new ModelInfo({ modelId: 'a/b', provider: 'a', benchmarkScores: {} }))).toBe(
      null,
    );
  });
});

describe('speed utility', () => {
  it('computes T300 as ttft + 300 tokens of generation', () => {
    expect(t300Seconds(50, 800)).toBeCloseTo(6.8, 12);
    expect(t300Seconds(150, 500)).toBeCloseTo(2.5, 12);
    expect(t300Seconds(600, 200)).toBeCloseTo(0.7, 12);
    expect(t300Seconds(1000, 100)).toBeCloseTo(0.4, 12);
  });

  it('anchors at 0.3 s -> 1 and 30 s -> 0', () => {
    expect(speedUtilityFromT300(0.3)).toBeCloseTo(1.0, 12);
    expect(speedUtilityFromT300(30.0)).toBeCloseTo(0.0, 12);
  });

  it('pays +0.1505 for halving the wait', () => {
    const halving = Math.log(2) / Math.log(100);
    expect(halving).toBeCloseTo(0.1505, 4);
    expect(speedUtilityFromT300(2.5) - speedUtilityFromT300(5.0)).toBeCloseTo(halving, 12);
    expect(speedUtilityFromT300(1.0) - speedUtilityFromT300(2.0)).toBeCloseTo(halving, 12);
  });

  it('matches the spec reference values for the five synthetic candidates', () => {
    expect(speedUtilityFromT300(6.8)).toBeCloseTo(0.32231, 5);
    expect(speedUtilityFromT300(2.5)).toBeCloseTo(0.53959, 5);
    expect(speedUtilityFromT300(0.7)).toBeCloseTo(0.81601, 5);
    expect(speedUtilityFromT300(0.4)).toBeCloseTo(0.93753, 5);
  });

  it('clamps outside the anchors', () => {
    expect(speedUtilityFromT300(0.1)).toBe(1.0);
    expect(speedUtilityFromT300(120.0)).toBe(0.0);
  });

  it('estimates a missing ttft from the median and flags it', () => {
    const got = speedUtility({ tokensPerSecond: 50, ttftMs: undefined }, STATS);
    expect(got.ttftEstimated).toBe(true);
    expect(got.tpsUnknown).toBe(false);
    expect(got.t300).toBeCloseTo(6.8, 12); // 800 ms median + 300/50
    expect(got.value).toBeCloseTo(speedUtilityFromT300(6.8), 12);
  });

  it('falls back to the p25 utility when throughput is unknown', () => {
    for (const m of [
      { tokensPerSecond: undefined, ttftMs: undefined },
      { tokensPerSecond: undefined, ttftMs: 100 },
      { tokensPerSecond: 0, ttftMs: 100 },
    ]) {
      const got = speedUtility(m, STATS);
      expect(got.value).toBe(0.242);
      expect(got.tpsUnknown).toBe(true);
      expect(got.ttftEstimated).toBe(false);
      // No T300 is computed at all in this case.
      expect(got.t300).toBe(null);
    }
  });

  it('uses its own ttft when it has one', () => {
    const got = speedUtility({ tokensPerSecond: 150, ttftMs: 500 }, STATS);
    expect(got.ttftEstimated).toBe(false);
    expect(got.t300).toBeCloseTo(2.5, 12);
  });
});

describe('registry speed statistics', () => {
  function m(id: string, tps?: number, ttft?: number): ModelInfo {
    return new ModelInfo({
      modelId: id,
      provider: 'test',
      benchmarkScores: {},
      tokensPerSecond: tps,
      ttftMs: ttft,
    });
  }

  it('medianTtftMs is the median over models that have one', () => {
    const stats = registrySpeedStats([
      m('a', 100, 100),
      m('b', 100, 200),
      m('c', 100, 900),
      m('d', 100, undefined),
    ]);
    expect(stats.medianTtftMs).toBe(200);
  });

  it('averages the two middle values on an even count', () => {
    const stats = registrySpeedStats([m('a', 100, 100), m('b', 100, 300)]);
    expect(stats.medianTtftMs).toBe(200);
  });

  it('p25SpeedUtility interpolates linearly, like numpy percentile', () => {
    // Four models -> h = (4-1)*0.25 = 0.75, so p25 sits 3/4 of the way from
    // the lowest utility to the second-lowest.
    const models = [m('a', 50, 800), m('b', 150, 500), m('c', 600, 200), m('d', 1000, 100)];
    const stats = registrySpeedStats(models);
    const us = [6.8, 2.5, 0.7, 0.4].map(speedUtilityFromT300).sort((x, y) => x - y);
    expect(stats.p25SpeedUtility).toBeCloseTo(us[0] + 0.75 * (us[1] - us[0]), 12);
  });

  it('only counts models that have BOTH signals in the utility percentile', () => {
    const withBoth = [m('a', 50, 800), m('b', 150, 500)];
    const plusHalfKnown = [...withBoth, m('c', 150, undefined), m('d', undefined, 500)];
    expect(registrySpeedStats(plusHalfKnown).p25SpeedUtility).toBe(
      registrySpeedStats(withBoth).p25SpeedUtility,
    );
  });

  it('uses the documented constants when the model set has no speed data', () => {
    const stats = registrySpeedStats([m('a'), m('b')]);
    expect(stats.medianTtftMs).toBe(FALLBACK_MEDIAN_TTFT_MS);
    expect(stats.p25SpeedUtility).toBe(FALLBACK_P25_SPEED_UTILITY);
  });

  it('is pessimistic on the shipped catalog (p25 below the median)', () => {
    const models = ModelRegistry.default().allModels;
    const stats = registrySpeedStats(models);
    const us = models
      .filter((x) => x.tokensPerSecond != null && x.ttftMs != null)
      .map((x) => speedUtilityFromT300(t300Seconds(x.tokensPerSecond!, x.ttftMs!)))
      .sort((a, b) => a - b);
    // The packaged starter catalog (45 models; most carry speed measurements).
    expect(us.length).toBeGreaterThan(30);
    expect(stats.p25SpeedUtility).toBeLessThan(us[Math.floor(us.length / 2)]);
    expect(stats.p25SpeedUtility).toBeGreaterThan(0);
    expect(stats.medianTtftMs).toBeGreaterThan(0);
  });
});

describe('quality tolerance (the band width)', () => {
  it('is EPS_UNIT * ((c-1)+(s-1)) / q', () => {
    expect(EPS_UNIT).toBe(0.102);
    const eps = (q: number, c: number, s: number) => qualityTolerance(new Priorities(q, c, s));
    // The spec's table, to 4 dp.
    expect(eps(5, 1, 1)).toBe(0.0);
    expect(eps(3, 3, 3)).toBeCloseTo(0.136, 12);
    expect(eps(4, 3, 3)).toBeCloseTo(0.102, 12);
    expect(eps(2, 3, 3)).toBeCloseTo(0.204, 12);
    expect(eps(1, 5, 1)).toBeCloseTo(0.408, 12);
    expect(eps(1, 1, 5)).toBeCloseTo(0.408, 12);
    expect(eps(2, 5, 3)).toBeCloseTo(0.306, 12); // the `budget` preset
    // NOTE: spec §4's table prints 0.2040 for (2,3,5), but its own formula --
    // and its stated symmetry in (c-1)+(s-1) -- give 0.306, the same as the
    // `budget` preset's mirror image. The formula is what is normative.
    expect(eps(2, 3, 5)).toBeCloseTo(0.306, 12); // the `fast` preset
    expect(eps(3, 5, 5)).toBeCloseTo(0.272, 12);
  });

  it('is zero for every (q,1,1) -- strict quality, no band', () => {
    for (const q of [1, 2, 3, 4, 5]) {
      expect(qualityTolerance(new Priorities(q, 1, 1))).toBe(0.0);
    }
  });

  it('grows linearly in (c-1)+(s-1), never stepwise', () => {
    const at = (c: number, s: number) => qualityTolerance(new Priorities(3, c, s));
    const steps = [at(1, 1), at(2, 1), at(3, 1), at(4, 1), at(5, 1)];
    for (let i = 2; i < steps.length; i++) {
      expect(steps[i] - steps[i - 1]).toBeCloseTo(steps[1] - steps[0], 12);
    }
    // Cost and speed enter symmetrically.
    expect(at(5, 1)).toBeCloseTo(at(1, 5), 12);
  });

  it('reflects the priority clamp and round-half-up', () => {
    expect(qualityTolerance(new Priorities(3, 9, 1))).toBeCloseTo(
      qualityTolerance(new Priorities(3, 5, 1)),
      12,
    );
    expect(qualityTolerance(new Priorities(3, 2.5, 1))).toBeCloseTo(
      qualityTolerance(new Priorities(3, 3, 1)),
      12,
    );
  });
});
