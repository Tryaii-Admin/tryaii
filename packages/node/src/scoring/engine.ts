/**
 * Dynamic model scoring engine -- `satisficing-v1`.
 *
 * Quality, cost and speed are no longer averaged into one weighted blend.
 * Instead (see docs/sdk/routing/scoring.md):
 *
 *   1. quality `q'` comes from the catalog-derived normalization ranges;
 *   2. a *band* of contenders is `{ q' >= q'_best - eps }`, where `eps` grows
 *      with the cost/speed priorities and shrinks with the quality priority;
 *   3. inside the band, models are ranked purely on the cost/speed utilities
 *      `U_c`/`U_s` (log-scaled, so each 10x cheaper is worth a fixed amount);
 *   4. outside the band they are ranked on `q'` and can never outrank a
 *      contender.
 *
 * `finalScore` is an order-preserving map into [0, 1]: contenders land in
 * [0.5, 1.0], non-contenders in [0.0, 0.5]. It is comparable **within one
 * routing call only** (`eps` and `q'_best` are properties of the call); the
 * globally comparable numbers are `qualityScore`, `costScore`, `speedScore`.
 */

// The half-even helpers replicate Python round()/f"{x:.Nf}" exactly (see
// shared/cachelint/SPEC.md §1.2/§1.5) — the Python engine is this engine's
// byte-parity reference, and Math.round/toFixed diverge from it on ties.
// halfEven.ts is a tiny pure module (no tokenizer data comes with it).
import { formatFixed, halfEvenRound } from '../cachelint/util/halfEven.js';
import { computeBenchmarkCoverage, ModelInfo } from '../registry/models.js';
import { BenchmarkNormalizer } from './benchmarks.js';
import { DEFAULT_PRIORITIES, Priorities } from './priorities.js';

export interface ModelScore {
  modelId: string;
  finalScore: number;       // 0-1, comparable within one routing call
  qualityScore: number;     // q' in [0,1] (catalog-normalised, imputation-shrunk)
  costScore: number;        // U_c in [0,1] (higher = cheaper)
  speedScore: number;       // U_s in [0,1] (higher = faster)
  qualityContribution: number;
  costContribution: number;
  speedContribution: number;
  /** 0.5 when in band, else 0 -- makes the contributions sum to finalScore. */
  bandBase: number;
  inBand: boolean;
  /** `eps`: how much quality the user is willing to trade away. */
  qualityTolerance: number;
  /** `q'_best` over the candidates that had a quality signal. */
  qualityBest: number;
  /** Subset of SIGNAL_FLAGS, in that order. */
  signalFlags: string[];
  /** Unrounded `q'` (`qualityScore` is the 4-dp rounded view). */
  qPrime: number;
  /** Unrounded `U_c`. */
  uCost: number;
  /** Unrounded `U_s`. */
  uSpeed: number;
  /** Seconds to a 300-token answer, or null when throughput is unknown. */
  t300: number | null;
  topBenchmarks: Array<[string, number]>; // Most relevant benchmarks for this model
  /**
   * Effective per-term quality weight actually used for each of the prompt's
   * selected benchmarks, rounded to 4 dp:
   *
   *     w(b, m) = similarity(b) * BENCHMARK_WEIGHTS[b]
   *               * coverage(b) ** COVERAGE_EXPONENT
   *               * (IMPUTED_TERM_WEIGHT if this model's term was imputed else 1)
   *
   * Benchmarks no model in the scored set reports at all are absent (they are
   * skipped before a term is formed). Diagnostic only -- it is deliberately
   * NOT part of the daemon/server/diagnose wire payloads.
   */
  benchmarkWeights: Record<string, number>;
  reasoning: string;        // Human-readable explanation
}

/* ------------------------------------------------------------------ cost */

/** $/M at which `U_c` reaches 1.0. */
export const PRICE_LO_PER_M = 0.05;
/** $/M at which `U_c` reaches 0.0. */
export const PRICE_HI_PER_M = 50.0;

/**
 * Cost utility from the model's average price per 1k tokens.
 *
 *     p_M = avgPricePer1k * 1000                     # $ per 1M tokens
 *     U_c = clamp((ln 50 - ln p_M) / ln 1000, 0, 1)
 *
 * Three decades, so every 10x cheaper is worth a flat +1/3. A missing or
 * non-finite price yields **0.0**, not a neutral value: a model whose price we
 * do not know must never win a cost comparison (the caller flags it
 * `cost: unknown`). A free model (p_M <= 0) yields 1.0.
 */
export function costUtility(avgPrice: number | null | undefined): number {
  if (avgPrice == null || !Number.isFinite(avgPrice)) return 0.0;
  const pM = avgPrice * 1000.0;
  if (pM <= 0) return 1.0;
  return clamp01(
    (Math.log(PRICE_HI_PER_M) - Math.log(pM)) /
      (Math.log(PRICE_HI_PER_M) - Math.log(PRICE_LO_PER_M)),
  );
}

/** Average price per 1k tokens (input/output 50:50), or null when unpriced. */
export function avgPricePer1k(model: Pick<ModelInfo, 'pricing'>): number | null {
  const p = model.pricing;
  if (p == null) return null;
  if (!Number.isFinite(p.inputPer1k) || !Number.isFinite(p.outputPer1k)) return null;
  return (p.inputPer1k + p.outputPer1k) / 2;
}

/* ----------------------------------------------------------------- speed */

/** Seconds-to-300-tokens at which `U_s` reaches 1.0. */
export const T300_LO_S = 0.3;
/** Seconds-to-300-tokens at which `U_s` reaches 0.0. */
export const T300_HI_S = 30.0;

/**
 * Last-resort fallbacks, used only when the scored model set carries no
 * throughput data at all (e.g. hand-built test registries). With a real catalog
 * both values are computed from it -- see registrySpeedStats.
 */
export const FALLBACK_MEDIAN_TTFT_MS = 800.0;
export const FALLBACK_P25_SPEED_UTILITY = 0.242;

/** Seconds to a 300-token answer: time-to-first-token plus generation. */
export function t300Seconds(tokensPerSecond: number, ttftMs: number): number {
  return ttftMs / 1000.0 + 300.0 / tokensPerSecond;
}

/**
 * Speed utility from T300: `clamp((ln 30 - ln T300) / ln 100, 0, 1)`.
 * Two decades, so halving the wait is worth +0.1505.
 */
export function speedUtilityFromT300(t300: number): number {
  return clamp01(
    (Math.log(T300_HI_S) - Math.log(t300)) / (Math.log(T300_HI_S) - Math.log(T300_LO_S)),
  );
}

/** Runtime speed fallbacks derived from the model set being scored. */
export interface RegistrySpeedStats {
  /** Median `ttftMs` over models that have one. */
  medianTtftMs: number;
  /** 25th percentile (linear interpolation) of `U_s` over models with both signals. */
  p25SpeedUtility: number;
}

/**
 * Compute the speed fallbacks from a model set.
 *
 * `medianTtftMs` fills in a missing time-to-first-token for a model whose
 * throughput *is* known; `p25SpeedUtility` is the whole `U_s` for a model with
 * no throughput at all. The p25 choice is deliberate and pessimistic (below the
 * catalog median), so an unmeasured model can never win a speed-first request.
 */
export function registrySpeedStats(models: ModelInfo[]): RegistrySpeedStats {
  const ttfts: number[] = [];
  const utilities: number[] = [];
  for (const m of models) {
    const tps = m.tokensPerSecond;
    const ttft = m.ttftMs;
    const hasTtft = ttft != null && Number.isFinite(ttft) && ttft > 0;
    const hasTps = tps != null && Number.isFinite(tps) && tps > 0;
    if (hasTtft) ttfts.push(ttft as number);
    if (hasTps && hasTtft) {
      utilities.push(speedUtilityFromT300(t300Seconds(tps as number, ttft as number)));
    }
  }
  return {
    medianTtftMs: ttfts.length > 0 ? median(ttfts) : FALLBACK_MEDIAN_TTFT_MS,
    p25SpeedUtility:
      utilities.length > 0 ? percentileLinear(utilities, 0.25) : FALLBACK_P25_SPEED_UTILITY,
  };
}

/** `U_s` plus what had to be guessed to get it. */
export interface SpeedUtility {
  value: number;
  /** null when throughput is unknown (no T300 is computed in that case). */
  t300: number | null;
  ttftEstimated: boolean;
  tpsUnknown: boolean;
}

/**
 * Speed utility for a model, with the two documented fallbacks: throughput
 * known but TTFT missing -> the median TTFT (`ttft: estimated`); throughput
 * missing -> the p25 of `U_s` (`speed: unknown`, and no T300 is computed).
 */
export function speedUtility(
  model: Pick<ModelInfo, 'tokensPerSecond' | 'ttftMs'>,
  stats: RegistrySpeedStats,
): SpeedUtility {
  const tps = model.tokensPerSecond;
  if (tps == null || !Number.isFinite(tps) || tps <= 0) {
    return { value: stats.p25SpeedUtility, t300: null, ttftEstimated: false, tpsUnknown: true };
  }
  const own = model.ttftMs;
  const hasOwn = own != null && Number.isFinite(own) && own > 0;
  const ttft = hasOwn ? (own as number) : stats.medianTtftMs;
  const t = t300Seconds(tps, ttft);
  return { value: speedUtilityFromT300(t), t300: t, ttftEstimated: !hasOwn, tpsUnknown: false };
}

/* ------------------------------------------------------------------ band */

/**
 * The band width in `q'` units per unit of `((cost - 1) + (speed - 1)) / quality`.
 *
 * **This is THE tuning dial of the router.** 0.102 is 1.6x the calibrated base
 * tolerance, picked so `eps` at priorities (3,3,3) (= 0.136) is a large
 * fraction of the typical quality spread among the strongest catalog models:
 * wide enough that balanced routing genuinely trades a little quality for a
 * much cheaper or faster model, narrow enough that it never drops to a clearly
 * weaker tier. Raising it buys cheaper/faster picks at the cost of quality,
 * linearly and predictably. Recalibrate when the catalog's `q'` spread moves
 * (see docs/sdk/routing/scoring.md).
 */
export const EPS_UNIT = 0.102;

/** `eps = EPS_UNIT * ((cost - 1) + (speed - 1)) / quality`, in `q'` units. */
export function qualityTolerance(priorities: Priorities): number {
  return (EPS_UNIT * (priorities.cost - 1 + (priorities.speed - 1))) / priorities.quality;
}

/** The result of the band + in-band ranker for one candidate. */
export interface CombinedScore {
  /** Internal ordering value: `1 + sec` for a contender, else `q'`. */
  utility: number;
  /** The public score: [0.5, 1] for contenders, [0, 0.5] for the rest. */
  finalScore: number;
  /** `(wc*U_c_q + ws*U_s_q) / (wc + ws)` in [0,1]; 0 when the term is off. */
  sec: number;
  inBand: boolean;
  bandBase: number;
  qualityContribution: number;
  costContribution: number;
  speedContribution: number;
}

/**
 * The band + the in-band ranker (see docs/sdk/routing/scoring.md) -- the whole algorithm
 * in one pure function, so the cross-SDK reference table can exercise exactly
 * the code that routes.
 *
 *     wc + ws == 0  ->  utility = final = q'                 (strict quality)
 *     in band       ->  utility = 1 + sec, final = 0.5 + 0.5*sec
 *     out of band   ->  utility = q',      final = 0.5 * q'
 *
 * `uCostQ`/`uSpeedQ` must already be quantised to 3 decimals.
 * `finalScore` is rounded half-even to 4 decimals; `utility` is not rounded
 * (it only ever feeds the comparison).
 */
export function satisficingCombine(
  qPrime: number,
  uCostQ: number,
  uSpeedQ: number,
  qBest: number,
  eps: number,
  wc: number,
  ws: number,
): CombinedScore {
  const wSum = wc + ws;
  const inBand = qPrime >= qBest - eps;
  if (wSum === 0) {
    // Strict quality: any (q,1,1). No band, no secondary term. The quality
    // contribution carries the whole score so the parts still sum to it.
    return {
      utility: qPrime,
      finalScore: halfEvenRound(qPrime, 4),
      sec: 0.0,
      inBand,
      bandBase: 0.0,
      qualityContribution: qPrime,
      costContribution: 0.0,
      speedContribution: 0.0,
    };
  }
  if (inBand) {
    const costPart = (wc * uCostQ) / wSum;
    const speedPart = (ws * uSpeedQ) / wSum;
    const sec = costPart + speedPart;
    return {
      // Every contender is >= 1.0 and every non-contender <= 1.0, so no
      // out-of-band model can ever outrank a contender.
      utility: 1.0 + sec,
      finalScore: halfEvenRound(0.5 + 0.5 * sec, 4),
      sec,
      inBand: true,
      bandBase: 0.5,
      qualityContribution: 0.0,
      costContribution: 0.5 * costPart,
      speedContribution: 0.5 * speedPart,
    };
  }
  return {
    utility: qPrime,
    finalScore: halfEvenRound(0.5 * qPrime, 4),
    sec: 0.0,
    inBand: false,
    bandBase: 0.0,
    qualityContribution: 0.5 * qPrime,
    costContribution: 0.0,
    speedContribution: 0.0,
  };
}

/** Fixed order of the flags reported in `signalFlags` (and only these). */
export const SIGNAL_FLAGS = [
  'cost: unknown',
  'speed: unknown',
  'ttft: estimated',
  'no benchmark signal',
] as const;

/* --------------------------------------------------------------- quality */

/**
 * How many of the prompt's most-relevant benchmarks contribute to model scoring.
 *
 * History: was 3. Bumped to 5 alongside the median-imputation change so a
 * single very-similar benchmark can't dominate the decision -- giving the
 * scorer a wider, more stable view of what the prompt looks like.
 */
const TOP_BENCHMARKS_FOR_SCORING = 5;

/**
 * Neutral quality used only as a last-resort fallback when a prompt matches no
 * benchmark at all (every similarity clamps to 0), so it stays routable on
 * cost/speed instead of being dropped. See scoreModels' neutralFallback retry.
 */
const NEUTRAL_QUALITY_SCORE = 0.5;

/**
 * Weight floor applied **only** on the all-no-signal fallback path, so cost and
 * speed can still break ties when the user suppressed them. Applying it
 * anywhere else would contaminate the (5,1,1) and (1,5,1) regression anchors.
 */
const NO_SIGNAL_WEIGHT_FLOOR = 0.1;

/**
 * Shrinkage constant for imputing a missing benchmark. The imputed value blends
 * the model's own demonstrated level with the registry median, weighting the
 * model's level by `n / (n + K)` where n is how many benchmarks the model
 * actually has. K=3 means a model needs ~3 real benchmarks before its own level
 * outweighs the median. This stops a sparse *strong* model being flattened to
 * "average" while still preventing a one-benchmark model from inflating itself.
 */
const IMPUTATION_SHRINKAGE_K = 3;

/* -------------------------------------------------- coverage weighting */

/**
 * Multiplier applied to a quality term whose benchmark value had to be
 * **imputed** for this model (shrinkage imputation, see
 * IMPUTATION_SHRINKAGE_K). Imputed evidence is still evidence -- a model with
 * no SWE-bench number is not thereby average -- but it is half as much
 * evidence as a measured number, so it carries half the weight.
 *
 * Rationale (see docs/sdk/routing/scoring.md): without coverage weighting, a
 * large share of the selected benchmark weight can sit on benchmarks only a
 * small minority of models report, so a frontier pick may rest mostly on
 * imputed values. This constant plus COVERAGE_EXPONENT keeps the decision on
 * measured evidence.
 */
export const IMPUTED_TERM_WEIGHT = 0.5;

/**
 * Exponent on a benchmark's registry **coverage** in the per-term weight.
 *
 * `coverage(b)` is the fraction of the routable catalog that reports a real
 * value for `b` (see computeBenchmarkCoverage). 1.0 means the weight is linear in
 * coverage: a benchmark only 15% of the catalog reports contributes 15% of the
 * weight it would get if everyone reported it. Raise it to punish thin
 * benchmarks harder, lower it (towards 0) to go back to ignoring coverage.
 */
export const COVERAGE_EXPONENT = 1.0;

/**
 * Per-benchmark registry coverage, re-exported here because it is the input to
 * the coverage-aware per-term weight above. It lives in `registry/models.ts`
 * (beside `isFreeTier`, which defines "routable") so this module and the
 * registry can share one definition without an import cycle.
 */
export { computeBenchmarkCoverage } from '../registry/models.js';

/* ----------------------------------------------------------------- utils */

function clamp01(x: number): number {
  return x < 0 ? 0.0 : x > 1 ? 1.0 : x;
}

/** Median of a non-empty list (mean of the two middle values when even). */
function median(values: number[]): number {
  const xs = [...values].sort((a, b) => a - b);
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 === 1 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

/**
 * Linear-interpolation percentile, identical to numpy's default
 * (`method="linear"`).
 */
function percentileLinear(values: number[], q: number): number {
  const xs = [...values].sort((a, b) => a - b);
  if (xs.length === 1) return xs[0];
  const h = (xs.length - 1) * q;
  const lo = Math.floor(h);
  if (lo + 1 >= xs.length) return xs[xs.length - 1];
  return xs[lo] + (h - lo) * (xs[lo + 1] - xs[lo]);
}

/**
 * Compute the median raw benchmark score across the registry, per benchmark.
 *
 * Used to impute missing data: if a model has no score on a benchmark that
 * the prompt cares about, we fill in the registry-wide median rather than
 * silently dropping the benchmark. Dropping was the source of a real routing
 * bug (sparse-data models inflated their own averages by erasing weak
 * benchmarks instead of being penalised by them); imputing keeps things
 * neutral instead of harsh.
 *
 * Benchmarks no model in the registry has are *omitted* from the result --
 * the caller treats that as "truly unknown, skip" (preserves the long-standing
 * behaviour of dropping models that don't intersect any of the prompt's top
 * benchmarks).
 */
function computeBenchmarkMedians(
  models: ModelInfo[],
  benchmarkNames: string[],
): Record<string, number> {
  const medians: Record<string, number> = {};
  for (const name of benchmarkNames) {
    const values: number[] = [];
    for (const m of models) {
      const v = m.benchmarkScores[name];
      // Number.isFinite skips NaN/Infinity as well as undefined -- a NaN
      // benchmark value must not poison the registry-wide median.
      if (Number.isFinite(v)) values.push(v);
    }
    if (values.length === 0) continue; // omit -> caller skips this benchmark for this model
    medians[name] = median(values);
  }
  return medians;
}

/** Everything known about one candidate before the band is applied. */
interface Candidate {
  model: ModelInfo;
  qPrime: number;
  nReal: number;
  imputedCount: number;
  nTop: number;
  topBenchmarks: Array<[string, number]>;
  benchmarkWeights: Record<string, number>;
  noSignal: boolean;
  pricePerM: number | null;
  uCost: number;
  uCostQ: number;
  uSpeed: number;
  uSpeedQ: number;
  t300: number | null;
  ttftEstimated: boolean;
  tpsUnknown: boolean;
}

/** A scored candidate, before its reasoning string is rendered. */
interface Ranked extends CombinedScore {
  cand: Candidate;
}

/**
 * Scores models against a classified prompt using `satisficing-v1`
 * (see the module docstring and docs/sdk/routing/scoring.md).
 */
export class ScoringEngine {
  private _normalizer: BenchmarkNormalizer;
  /** Single-slot cache of the speed fallbacks, keyed by the model array identity. */
  private _statsKey: ModelInfo[] | null = null;
  private _statsValue: RegistrySpeedStats | null = null;
  /** Single-slot cache of the per-benchmark coverage, keyed the same way. */
  private _coverageKey: ModelInfo[] | null = null;
  private _coverageValue: Record<string, number> | null = null;

  constructor(normalizer?: BenchmarkNormalizer) {
    this._normalizer = normalizer ?? new BenchmarkNormalizer();
  }

  /**
   * Speed fallbacks for this model set, memoised on the array identity so a
   * registry that is scored repeatedly pays for the percentile once.
   */
  private _speedStats(models: ModelInfo[]): RegistrySpeedStats {
    if (this._statsKey === models && this._statsValue !== null) return this._statsValue;
    const stats = registrySpeedStats(models);
    this._statsKey = models;
    this._statsValue = stats;
    return stats;
  }

  /**
   * Per-benchmark coverage for this model set, memoised on the array identity
   * so a registry that is scored repeatedly pays for the scan once.
   */
  private _coverage(models: ModelInfo[]): Record<string, number> {
    if (this._coverageKey === models && this._coverageValue !== null) {
      return this._coverageValue;
    }
    const coverage = computeBenchmarkCoverage(models);
    this._coverageKey = models;
    this._coverageValue = coverage;
    return coverage;
  }

  /**
   * Score and rank models based on benchmark similarities and priorities.
   */
  scoreModels(
    models: ModelInfo[],
    benchmarkSimilarities: Record<string, number>,
    priorities: Priorities = DEFAULT_PRIORITIES,
    topK = 5,
    // `{benchmark: n_real / N}` over the ROUTABLE catalog (see
    // computeBenchmarkCoverage). The Router passes
    // `registry.benchmarkCoverage()`, so a *filtered* route still weights each
    // benchmark by its coverage of the whole catalog rather than of the filtered
    // slice. Omitted -> derived from `models` (right for an unfiltered call and
    // for hand-built test registries). Mirrors the Python SDK's
    // `benchmark_coverage` keyword.
    benchmarkCoverage?: Record<string, number>,
  ): ModelScore[] {
    // Pick the prompt's most-relevant benchmarks. See TOP_BENCHMARKS_FOR_SCORING
    // for why this is 5 -- short version: a wider view stops one near-perfect
    // similarity from dominating the decision.
    const sortedBenchmarks = Object.entries(benchmarkSimilarities)
      .sort((a, b) => b[1] - a[1])
      .slice(0, TOP_BENCHMARKS_FOR_SCORING);

    const topBenchmarkDict: Record<string, number> = {};
    for (const [name, score] of sortedBenchmarks) {
      topBenchmarkDict[name] = score;
    }

    // Per-benchmark medians for the benchmarks we actually care about. Built
    // once per call from the same `models` argument we're about to score
    // against -- so adding/removing/filtering models flows through correctly
    // without needing a separate "rebuild medians" step.
    const benchmarkMedians = computeBenchmarkMedians(
      models,
      sortedBenchmarks.map(([name]) => name),
    );

    const stats = this._speedStats(models);
    // Coverage is a property of the population being scored, not of the prompt
    // -- so it is computed from the same `models` argument as the medians and
    // the speed fallbacks, minus `:free` ids (computeBenchmarkCoverage does that).
    const coverage = benchmarkCoverage ?? this._coverage(models);

    const candidates: Candidate[] = [];
    for (const model of models) {
      const c = this._evaluate(model, topBenchmarkDict, benchmarkMedians, coverage, stats);
      if (c !== null) candidates.push(c);
    }

    // Fallback: if NO model has a quality signal, the prompt matched no
    // benchmark at all (its embedding is orthogonal/negative to every centroid,
    // so all similarities clamped to 0). Rather than return nothing -- which
    // makes a single route() throw and a budget run report the whole dataset
    // infeasible -- re-evaluate every model on a neutral quality baseline and
    // route on cost/speed alone. The per-model skip above still applies when
    // only *some* models lack signal.
    const allNoSignal = candidates.length === 0;
    if (allNoSignal) {
      for (const model of models) {
        const c = this._evaluate(
          model,
          topBenchmarkDict,
          benchmarkMedians,
          coverage,
          stats,
          true,
        );
        if (c !== null) candidates.push(c);
      }
    }
    if (candidates.length === 0) return [];

    const eps = qualityTolerance(priorities);
    const wc = priorities.costWeight;
    const ws = priorities.speedWeight;
    const qBest = allNoSignal
      ? NEUTRAL_QUALITY_SCORE
      : candidates.reduce((best, c) => (c.qPrime > best ? c.qPrime : best), -Infinity);

    const ranked: Ranked[] = candidates.map((c) =>
      allNoSignal ? this._rankNoSignal(c, wc, ws) : this._rank(c, qBest, eps, wc, ws),
    );

    // One total order, identical in both SDKs:
    //   1. higher internal utility (contenders are >= 1.0, everyone else <= 1.0)
    //   2. higher q' -- quality wins an equal cost/speed trade
    //   3. more REAL (non-imputed) benchmarks -- evidence breaks the tie
    //   4. ascending modelId by Unicode code point (NOT locale-aware)
    ranked.sort((a, b) => {
      if (a.utility !== b.utility) return b.utility - a.utility;
      if (a.cand.qPrime !== b.cand.qPrime) return b.cand.qPrime - a.cand.qPrime;
      if (a.cand.nReal !== b.cand.nReal) return b.cand.nReal - a.cand.nReal;
      const x = a.cand.model.modelId;
      const y = b.cand.model.modelId;
      return x < y ? -1 : x > y ? 1 : 0;
    });

    // The exchange-rate sentence names the same quality leader and the same
    // regret in *every* candidate's string, so both are computed after the
    // ranking: the regret quoted is the selected model's.
    const leader = [...candidates].sort((a, b) => {
      if (a.qPrime !== b.qPrime) return b.qPrime - a.qPrime;
      if (a.nReal !== b.nReal) return b.nReal - a.nReal;
      return a.model.modelId < b.model.modelId ? -1 : a.model.modelId > b.model.modelId ? 1 : 0;
    })[0];
    const regret = qBest - ranked[0].cand.qPrime;

    const scores = ranked.map((r) =>
      this._toScore(r, {
        eps,
        qBest,
        priorities,
        leaderId: leader.model.modelId,
        regret,
        allNoSignal,
      }),
    );
    return scores.slice(0, topK);
  }

  /**
   * The model's own demonstrated quality level: the *median* of its normalized
   * scores across every benchmark it has data for, plus that count. Used as the
   * shrinkage target when imputing missing benchmarks so a strong model isn't
   * imputed as "average". The median (rather than mean) keeps a single corrupt
   * or anomalously-low score from dragging the level down. Returns level 0.5
   * (neutral) for a model with no data.
   */
  private _modelLevel(model: ModelInfo): { level: number; count: number } {
    const entries = Object.entries(model.benchmarkScores).filter(([, raw]) =>
      Number.isFinite(raw),
    );
    if (entries.length === 0) return { level: 0.5, count: 0 };
    const norms = entries.map(([name, raw]) => this._normalizer.normalize(name, raw));
    return { level: median(norms), count: norms.length };
  }

  /**
   * Everything about one model that does not depend on the other candidates:
   * `q'`, the evidence counts, `U_c` and `U_s`. Returns null when the model has
   * no quality signal (and this is not the fallback pass).
   */
  private _evaluate(
    model: ModelInfo,
    topBenchmarks: Record<string, number>,
    benchmarkMedians: Record<string, number>,
    coverage: Record<string, number>,
    stats: RegistrySpeedStats,
    // When true, a model with no usable similarity signal is scored on a neutral
    // quality baseline instead of being dropped -- used only for the
    // all-models-signal-less case (see scoreModels).
    neutralFallback = false,
  ): Candidate | null {
    // --- Quality: q' ---
    let weightedQualitySum = 0;
    let totalSimilarityWeight = 0;
    let imputedCount = 0;
    // Only the model's *own* benchmark data goes in this list -- it powers
    // the human-readable reasoning string, which should reflect real strengths,
    // not registry-median guesses.
    const modelTopBenchmarks: Array<[string, number]> = [];
    // Effective per-term weights, for ModelScore.benchmarkWeights.
    const benchmarkWeights: Record<string, number> = {};

    // The model's own demonstrated level and how much we trust it, used to
    // impute missing benchmarks via shrinkage toward the registry median.
    const { level: modelLevel, count: knownCount } = this._modelLevel(model);
    const alpha = knownCount / (knownCount + IMPUTATION_SHRINKAGE_K);

    for (const [benchmarkName, userSimilarity] of Object.entries(topBenchmarks)) {
      const rawScore = model.benchmarkScores[benchmarkName];
      let normalized: number;
      let imputed = false;
      // !Number.isFinite treats NaN/Infinity like a missing score so a junk
      // value is imputed rather than poisoning the result.
      if (!Number.isFinite(rawScore)) {
        const medianRaw = benchmarkMedians[benchmarkName];
        // No model in the registry has data on this benchmark -> nothing to
        // impute from. Falling through to `continue` here preserves the old
        // "skip the model entirely if it intersects nothing" semantic.
        if (medianRaw == null) continue;
        // Shrinkage imputation: blend the model's own level with the registry
        // median (in normalized space). A high-coverage strong model keeps a
        // high imputed value instead of being dragged to the median; a sparse
        // model stays near the median so it can't inflate itself.
        const medianNorm = this._normalizer.normalize(benchmarkName, medianRaw);
        normalized = alpha * modelLevel + (1 - alpha) * medianNorm;
        imputed = true;
        imputedCount += 1;
      } else {
        normalized = this._normalizer.normalize(benchmarkName, rawScore);
      }

      // Combine four orthogonal factors, multiplied in exactly this order (the
      // Python SDK does the same, so the 1e-9 parity holds):
      //
      //   1. similarity     -- how much this prompt looks like the benchmark;
      //   2. BENCHMARK_WEIGHTS -- how much we trust the benchmark as a signal;
      //   3. coverage^E     -- how much of the routable catalog reports it, so
      //      a benchmark almost nobody reports cannot carry the decision on
      //      imputed values alone (coverage 0 => the term drops out entirely);
      //   4. IMPUTED_TERM_WEIGHT -- an imputed term is half-strength evidence.
      const cov = coverage[benchmarkName] ?? 0;
      const weight =
        userSimilarity *
        this._normalizer.getWeight(benchmarkName) *
        Math.pow(cov, COVERAGE_EXPONENT) *
        (imputed ? IMPUTED_TERM_WEIGHT : 1.0);
      weightedQualitySum += weight * normalized;
      totalSimilarityWeight += weight;
      benchmarkWeights[benchmarkName] = halfEvenRound(weight, 4);
      if (!imputed) modelTopBenchmarks.push([benchmarkName, normalized]);
    }

    const noSignal = totalSimilarityWeight === 0;
    if (noSignal && !neutralFallback) return null;

    const qPrime = noSignal
      ? NEUTRAL_QUALITY_SCORE
      : weightedQualitySum / totalSimilarityWeight;

    // --- Cost: U_c ---
    const price = avgPricePer1k(model);
    const uCost = costUtility(price);

    // --- Speed: U_s ---
    const speed = speedUtility(model, stats);

    // The utilities are quantised to 3 decimals *before* being compared: U_s is
    // linear in ln T300, so 3 dp is a ~0.7% resolution in seconds -- below the
    // measurement noise of a 5-row tps/ttft sample -- and it removes the
    // near-ties that a +/-20% perturbation would otherwise reorder. The
    // unquantised values are what the reasoning string prints.
    return {
      model,
      qPrime,
      nReal: modelTopBenchmarks.length,
      imputedCount,
      nTop: Object.keys(topBenchmarks).length,
      topBenchmarks: modelTopBenchmarks,
      benchmarkWeights,
      noSignal,
      pricePerM: price == null ? null : price * 1000.0,
      uCost,
      uCostQ: halfEvenRound(uCost, 3),
      uSpeed: speed.value,
      uSpeedQ: halfEvenRound(speed.value, 3),
      t300: speed.t300,
      ttftEstimated: speed.ttftEstimated,
      tpsUnknown: speed.tpsUnknown,
    };
  }

  /** The band + the in-band ranker (see satisficingCombine). */
  private _rank(cand: Candidate, qBest: number, eps: number, wc: number, ws: number): Ranked {
    return {
      cand,
      ...satisficingCombine(cand.qPrime, cand.uCostQ, cand.uSpeedQ, qBest, eps, wc, ws),
    };
  }

  /**
   * All-no-signal fallback: route on cost/speed only. The 0.1 weight floor
   * lives **here and nowhere else** -- the fallback's whole point is to keep the
   * prompt routable even when the user suppressed cost and speed.
   */
  private _rankNoSignal(cand: Candidate, wc: number, ws: number): Ranked {
    const c = Math.max(wc, NO_SIGNAL_WEIGHT_FLOOR);
    const s = Math.max(ws, NO_SIGNAL_WEIGHT_FLOOR);
    const costPart = (c * cand.uCostQ) / (c + s);
    const speedPart = (s * cand.uSpeedQ) / (c + s);
    const value = costPart + speedPart;
    return {
      cand,
      utility: value,
      finalScore: halfEvenRound(value, 4),
      sec: value,
      inBand: false,
      bandBase: 0.0,
      qualityContribution: 0.0,
      costContribution: costPart,
      speedContribution: speedPart,
    };
  }

  /** Render the public ModelScore, reasoning string included. */
  private _toScore(
    r: Ranked,
    ctx: {
      eps: number;
      qBest: number;
      priorities: Priorities;
      leaderId: string;
      regret: number;
      allNoSignal: boolean;
    },
  ): ModelScore {
    const c = r.cand;
    const flags: string[] = [];
    if (c.pricePerM == null) flags.push('cost: unknown');
    if (c.tpsUnknown) flags.push('speed: unknown');
    if (c.ttftEstimated) flags.push('ttft: estimated');
    if (c.noSignal) flags.push('no benchmark signal');

    const p = ctx.priorities;
    let reasoning: string;
    if (ctx.allNoSignal) {
      reasoning = 'No benchmark signal -- routed on cost/speed';
    } else {
      // 1. quality + evidence
      reasoning = `q'=${formatFixed(c.qPrime, 2)} (${c.nReal} real of ${c.nTop})`;
      if (c.imputedCount > 0) {
        reasoning += ` | imputed: ${c.imputedCount}/${c.nTop}`;
      }
      // 2. cost
      reasoning +=
        c.pricePerM == null
          ? ' | cost unknown'
          : ` | cost ${formatFixed(c.uCost, 4)} ($${formatFixed(c.pricePerM, 2)}/M)`;
      // 3. speed
      if (c.tpsUnknown) {
        reasoning += ' | speed unknown (catalog p25)';
      } else {
        reasoning += ` | speed ${formatFixed(c.uSpeed, 4)} (${formatFixed(
          c.t300 as number,
          2,
        )} s to 300 tok)`;
        if (c.ttftEstimated) reasoning += ' [ttft estimated]';
      }
      // 4. the band
      if (ctx.eps === 0) {
        reasoning += ' | quality only (no tolerance)';
      } else if (r.inBand) {
        reasoning += ` | within ${formatFixed(ctx.eps, 3)} of the best (${formatFixed(
          ctx.qBest,
          2,
        )})`;
      } else {
        reasoning += ` | ${formatFixed(ctx.qBest - c.qPrime, 3)} below the best (${formatFixed(
          ctx.qBest,
          2,
        )}) -- outside the ${formatFixed(ctx.eps, 3)} tolerance`;
      }
      // 5. the exchange rate -- exactly one, always last
      reasoning +=
        ctx.eps === 0
          ? ` | at ${p.quality}/${p.cost}/${p.speed} only quality counts`
          : ` | at ${p.quality}/${p.cost}/${p.speed} you accept up to ${formatFixed(
              ctx.eps,
              3,
            )} less quality for a cheaper or faster model; this pick gave up ${formatFixed(
              ctx.regret,
              2,
            )} vs ${ctx.leaderId}`;
    }

    return {
      modelId: c.model.modelId,
      finalScore: r.finalScore,
      qualityScore: halfEvenRound(c.qPrime, 4),
      costScore: halfEvenRound(c.uCost, 4),
      speedScore: halfEvenRound(c.uSpeed, 4),
      qualityContribution: halfEvenRound(r.qualityContribution, 4),
      costContribution: halfEvenRound(r.costContribution, 4),
      speedContribution: halfEvenRound(r.speedContribution, 4),
      bandBase: r.bandBase,
      inBand: r.inBand,
      qualityTolerance: ctx.eps,
      qualityBest: ctx.qBest,
      signalFlags: flags,
      qPrime: c.qPrime,
      uCost: c.uCost,
      uSpeed: c.uSpeed,
      t300: c.t300,
      topBenchmarks: c.topBenchmarks,
      benchmarkWeights: c.benchmarkWeights,
      reasoning,
    };
  }
}
