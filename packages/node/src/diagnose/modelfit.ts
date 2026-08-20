/**
 * model_fit check (SPEC.md §2.2).
 *
 * Ranks the FULL catalog for the site's classification and reports where the
 * currently-used model lands. Only rank and the absolute raw dimension
 * scores are emitted — never the per-set renormalized finalScore, which is
 * meaningless as a cross-set delta. Mirrors diagnose/modelfit.py.
 */

import { halfEvenRound } from '../cachelint/util/halfEven.js';
import type { ModelRegistry } from '../registry/models.js';
import { ScoringEngine, type ModelScore } from '../scoring/engine.js';
import type { Priorities } from '../scoring/priorities.js';

export const TOP_EMITTED = 5;
export const RANK_OK_MAX = 3;
export const RANK_CONSIDER_MAX = 10;
/** recommended_same_price: ±20% of the current model's price (SPEC §2.2.1). */
export const PRICE_BAND = 0.2;

export interface FitInternal {
  scores: ModelScore[];
  currentScore: ModelScore | null;
  currentRank: number | null;
}

function dims(score: ModelScore): Record<string, unknown> {
  return {
    model_id: score.modelId,
    quality_score: score.qualityScore,
    cost_score: score.costScore,
    speed_score: score.speedScore,
  };
}

/** The scoring engine's cost basis: mean of input/output per-1k prices. */
function blendedPrice(model: { pricing?: { inputPer1k: number; outputPer1k: number } | null } | undefined): number | null {
  if (!model || !model.pricing) return null;
  return (model.pricing.inputPer1k + model.pricing.outputPer1k) / 2;
}

/**
 * SPEC §2.2.1: the best-RANKED model within ±20% of the current model's
 * blended price. The current model is itself a candidate — being the best
 * at your price is a positive result. Null when there is no band to search
 * (current unresolved/unranked/unpriced) — never guessed.
 */
function samePriceRecommendation(
  scores: ModelScore[],
  resolvedModelId: string | null,
  registry: ModelRegistry,
): Record<string, unknown> | null {
  if (resolvedModelId === null) return null;
  const currentPrice = blendedPrice(registry.getModel(resolvedModelId));
  if (currentPrice === null) return null;
  const low = currentPrice * (1 - PRICE_BAND);
  const high = currentPrice * (1 + PRICE_BAND);
  for (let rank0 = 0; rank0 < scores.length; rank0++) {
    const score = scores[rank0];
    const price = blendedPrice(registry.getModel(score.modelId));
    if (price === null || price < low || price > high) continue;
    return {
      model_id: score.modelId,
      rank: rank0 + 1,
      quality_score: score.qualityScore,
      cost_score: score.costScore,
      speed_score: score.speedScore,
      is_current: score.modelId === resolvedModelId,
    };
  }
  return null;
}

export function runModelFit(
  classification: Record<string, unknown>,
  resolvedModelId: string | null,
  declaredModel: string | null,
  priorities: Priorities,
  registry: ModelRegistry,
): [Record<string, unknown>, FitInternal] {
  const engine = new ScoringEngine();
  const models = registry.allModels;
  const scores = engine.scoreModels(
    models,
    classification.benchmark_similarities as Record<string, number>,
    priorities,
    models.length,
  );

  const rankOf = new Map<string, number>();
  scores.forEach((s, i) => rankOf.set(s.modelId, i + 1));
  const rankedCount = scores.length;
  const best = scores[0];

  let currentScore: ModelScore | null = null;
  let currentRank: number | null = null;
  if (resolvedModelId !== null && rankOf.has(resolvedModelId)) {
    currentRank = rankOf.get(resolvedModelId) as number;
    currentScore = scores[currentRank - 1];
  }

  let status: string;
  let reason: string | null;
  let summary: string;
  if (currentScore !== null && currentRank !== null) {
    if (currentRank <= RANK_OK_MAX) {
      status = 'ok';
      reason = null;
      summary = `${resolvedModelId} is a strong fit (rank ${currentRank} of ${rankedCount})`;
    } else if (currentRank <= RANK_CONSIDER_MAX) {
      status = 'finding';
      reason = null;
      summary =
        `consider ${best.modelId} (current ${resolvedModelId} ` +
        `ranks ${currentRank} of ${rankedCount})`;
    } else {
      status = 'finding';
      reason = null;
      summary =
        `${resolvedModelId} is a poor fit for this prompt ` +
        `(rank ${currentRank} of ${rankedCount}) — best: ${best.modelId}`;
    }
  } else {
    status = 'insufficient_data';
    if (resolvedModelId !== null) {
      reason = `model '${resolvedModelId}' has no benchmark signal for this prompt`;
    } else if (declaredModel) {
      reason = `unknown model '${declaredModel}'`;
    } else {
      reason = 'no model declared';
    }
    summary = `recommended: ${best.modelId} (no current model to compare)`;
  }

  const round4 = (x: number): number => halfEvenRound(x, 4);

  const payload: Record<string, unknown> = {
    status,
    reason,
    summary,
    classification: {
      broad_category: (classification.broad_category as string) ?? null,
      subcategory: (classification.subcategory as string) ?? null,
      confidence:
        'confidence' in classification ? round4(classification.confidence as number) : null,
    },
    current_rank: currentRank,
    catalog_size: rankedCount,
    current: currentScore !== null ? dims(currentScore) : null,
    recommended: {
      ...dims(best),
      top_benchmarks: best.topBenchmarks.map(([name, v]) => [name, round4(v)]),
      reasoning: best.reasoning,
    },
    recommended_same_price: samePriceRecommendation(
      scores,
      currentScore !== null ? resolvedModelId : null,
      registry,
    ),
    top: scores.slice(0, TOP_EMITTED).map((s, i) => ({
      rank: i + 1,
      ...dims(s),
      reasoning: s.reasoning,
    })),
  };
  return [payload, { scores, currentScore, currentRank }];
}
