/**
 * Dynamic model scoring engine.
 *
 * Combines benchmark performance, cost, and speed into a single score
 * weighted by user priorities. This is the heart of the routing logic.
 */

import { ModelInfo } from '../registry/models.js';
import { BenchmarkNormalizer } from './benchmarks.js';
import { DEFAULT_PRIORITIES, Priorities } from './priorities.js';

export interface ModelScore {
  modelId: string;
  finalScore: number;       // 0-1 combined score
  qualityScore: number;     // 0-1 benchmark quality
  costScore: number;        // 0-1 (higher = cheaper)
  speedScore: number;       // 0-1 (higher = faster)
  qualityContribution: number;
  costContribution: number;
  speedContribution: number;
  topBenchmarks: Array<[string, number]>; // Most relevant benchmarks for this model
  reasoning: string;        // Human-readable explanation
}

/** Speed tier -> numeric score. */
export const SPEED_SCORES: Record<string, number> = {
  'very fast': 1.0,
  'fast': 0.8,
  'medium': 0.6,
  'slow': 0.3,
  'very slow': 0.1,
};

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
    values.sort((a, b) => a - b);
    const mid = Math.floor(values.length / 2);
    medians[name] = values.length % 2 === 1
      ? values[mid]
      : (values[mid - 1] + values[mid]) / 2;
  }
  return medians;
}

/**
 * Scores models against a classified prompt.
 *
 * Takes benchmark similarity scores (from the classifier) and user priorities,
 * then ranks all available models using a three-factor weighted algorithm:
 *
 *     final = (quality * qW + cost * cW + speed * sW) / (qW + cW + sW)
 *
 * Where weights are derived from user priorities (1-5 scale).
 */
export class ScoringEngine {
  private _normalizer: BenchmarkNormalizer;

  constructor(normalizer?: BenchmarkNormalizer) {
    this._normalizer = normalizer ?? new BenchmarkNormalizer();
  }

  /**
   * Score and rank models based on benchmark similarities and priorities.
   */
  scoreModels(
    models: ModelInfo[],
    benchmarkSimilarities: Record<string, number>,
    priorities: Priorities = DEFAULT_PRIORITIES,
    topK = 5,
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

    const scores: ModelScore[] = [];

    for (const model of models) {
      const score = this._scoreSingleModel(model, topBenchmarkDict, benchmarkMedians, priorities);
      if (score !== null) {
        scores.push(score);
      }
    }

    // Fallback: if NO model scored, the prompt matched no benchmark at all (its
    // embedding is orthogonal/negative to every centroid, so all similarities
    // clamped to 0). Rather than return nothing -- which makes a single route()
    // throw and a budget run report the whole dataset infeasible -- re-score
    // every model on a neutral quality baseline so the prompt stays routable on
    // cost/speed. The per-model skip above still applies when only *some* models
    // lack signal.
    if (scores.length === 0) {
      for (const model of models) {
        const score = this._scoreSingleModel(
          model,
          topBenchmarkDict,
          benchmarkMedians,
          priorities,
          true,
        );
        if (score !== null) {
          scores.push(score);
        }
      }
    }

    // Sort by final score descending, with a deterministic secondary key so
    // median-imputation ties favour real data and are reproducible:
    //   1. higher finalScore
    //   2. more real (non-imputed) benchmark coverage -- topBenchmarks only
    //      holds the model's own non-imputed benchmarks
    //   3. ascending modelId by Unicode code point (NOT locale-aware)
    scores.sort((a, b) => {
      if (b.finalScore !== a.finalScore) return b.finalScore - a.finalScore;
      if (b.topBenchmarks.length !== a.topBenchmarks.length) {
        return b.topBenchmarks.length - a.topBenchmarks.length;
      }
      return a.modelId < b.modelId ? -1 : a.modelId > b.modelId ? 1 : 0;
    });

    if (scores.length > 1) {
      // Normalize to 0.1-0.95 range (best model ~ 0.95)
      const maxRaw = scores[0].finalScore;
      const minRaw = scores[scores.length - 1].finalScore;

      for (const s of scores) {
        if (maxRaw === minRaw) {
          s.finalScore = 0.5;
        } else {
          const normalized = (s.finalScore - minRaw) / (maxRaw - minRaw);
          s.finalScore = Math.round((0.1 + 0.85 * normalized) * 10000) / 10000;
        }
      }
    } else if (scores.length === 1) {
      // Single surviving model: don't rescale against a hardcoded 0 floor
      // (that forced ~0.95 regardless of how good the model actually is).
      // Surface its own unnormalized weighted score, clamped to [0,1].
      const only = scores[0];
      only.finalScore = Math.round(Math.max(0, Math.min(1, only.finalScore)) * 10000) / 10000;
    }

    return scores.slice(0, topK);
  }

  private _scoreSingleModel(
    model: ModelInfo,
    topBenchmarks: Record<string, number>,
    benchmarkMedians: Record<string, number>,
    priorities: Priorities,
    // When true, a model with no usable similarity signal is scored on a neutral
    // quality baseline instead of being dropped -- used only for the
    // all-models-signal-less case (see scoreModels).
    neutralFallback = false,
  ): ModelScore | null {
    // --- Quality score ---
    let weightedQualitySum = 0;
    let totalSimilarityWeight = 0;
    let imputedCount = 0;
    // Only the model's *own* benchmark data goes in this list -- it powers
    // the human-readable reasoning string, which should reflect real strengths,
    // not registry-median guesses.
    const modelTopBenchmarks: Array<[string, number]> = [];

    for (const [benchmarkName, userSimilarity] of Object.entries(topBenchmarks)) {
      let rawScore = model.benchmarkScores[benchmarkName];
      let imputed = false;
      // !Number.isFinite treats NaN/Infinity like a missing score so a junk
      // value is imputed from the median rather than poisoning the result.
      if (!Number.isFinite(rawScore)) {
        const median = benchmarkMedians[benchmarkName];
        // No model in the registry has data on this benchmark -> nothing to
        // impute from. Falling through to `continue` here preserves the old
        // "skip the model entirely if it intersects nothing" semantic, which
        // is exactly what the test at line ~140 of engine.test.ts pins.
        if (median == null) continue;
        rawScore = median;
        imputed = true;
        imputedCount += 1;
      }

      const normalized = this._normalizer.normalize(benchmarkName, rawScore);
      weightedQualitySum += userSimilarity * normalized;
      totalSimilarityWeight += userSimilarity;
      if (!imputed) modelTopBenchmarks.push([benchmarkName, normalized]);
    }

    // No usable similarity signal: normally drop the model so models with real
    // signal win. But when EVERY model is signal-less, scoreModels retries with
    // neutralFallback=true so the prompt stays routable on cost/speed -- flagged
    // in the reasoning below so the "no signal" case is observable, not mistaken
    // for a real quality judgement.
    const noSignal = totalSimilarityWeight === 0;
    if (noSignal && !neutralFallback) return null;

    const qualityScore = noSignal ? NEUTRAL_QUALITY_SCORE : weightedQualitySum / totalSimilarityWeight;

    // --- Cost score ---
    // Always compute when pricing data exists: gating on priority dropped the
    // numerator while keeping the weight in totalWeight (denominator), which is
    // mathematically inconsistent. The priority weight already scales the term.
    let costScore = 0;
    if (model.pricing) {
      const avgCost = (model.pricing.inputPer1k + model.pricing.outputPer1k) / 2;
      // Normalize against $0.10/1k tokens baseline
      costScore = Math.max(0.0, 1.0 - avgCost / 0.1);
    }

    // --- Speed score ---
    let speedScore = 0;
    if (model.latency) {
      speedScore = SPEED_SCORES[model.latency] ?? 0.3;
    }

    // --- Combine with priority weights ---
    // In the no-signal fallback, cost/speed must still break ties even if the
    // user suppressed them (priority 1 -> weight 0): the fallback's whole point
    // is to keep the prompt "routable on cost/speed". A small floor restores that
    // without touching normal routing (noSignal is false there).
    const qWeight = priorities.qualityWeight;
    const cWeight = noSignal ? Math.max(priorities.costWeight, 0.1) : priorities.costWeight;
    const sWeight = noSignal ? Math.max(priorities.speedWeight, 0.1) : priorities.speedWeight;

    const qContrib = qualityScore * qWeight;
    const cContrib = costScore * cWeight;
    const sContrib = speedScore * sWeight;

    const totalWeight = qWeight + cWeight + sWeight;
    let final = (qContrib + cContrib + sContrib) / totalWeight;
    final = Math.max(0.0, Math.min(1.0, final));

    // Generate reasoning
    const topBenchStr = modelTopBenchmarks
      .slice(0, 2)
      .map(([b, s]) => `${b} (${Math.round(s * 100)}%)`)
      .join(', ');

    let reasoning: string;
    if (noSignal) {
      reasoning = 'No benchmark signal -- routed on cost/speed';
    } else {
      reasoning = `Quality: ${qualityScore.toFixed(2)} on [${topBenchStr}]`;
      if (imputedCount > 0) {
        // Tell the reader the score is partly an estimate. Useful when reading
        // eval output and wondering why a model with thin coverage ranked here.
        const total = Object.keys(topBenchmarks).length;
        reasoning += ` | imputed: ${imputedCount}/${total}`;
      }
    }
    if (costScore > 0) {
      reasoning += ` | Cost efficiency: ${costScore.toFixed(2)}`;
    }
    if (speedScore > 0) {
      reasoning += ` | Speed: ${speedScore.toFixed(2)} (${model.latency})`;
    }

    return {
      modelId: model.modelId,
      finalScore: final,
      qualityScore: Math.round(qualityScore * 10000) / 10000,
      costScore: Math.round(costScore * 10000) / 10000,
      speedScore: Math.round(speedScore * 10000) / 10000,
      qualityContribution: Math.round(qContrib * 10000) / 10000,
      costContribution: Math.round(cContrib * 10000) / 10000,
      speedContribution: Math.round(sContrib * 10000) / 10000,
      topBenchmarks: modelTopBenchmarks,
      reasoning,
    };
  }
}
