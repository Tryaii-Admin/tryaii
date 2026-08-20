/**
 * cost_exposure check (SPEC.md §2.4).
 *
 * Pure arithmetic over the budget-module primitives; every number the check
 * cannot ground in the inventory or the other checks is itemized as
 * insufficient — never blended or guessed. Mirrors diagnose/cost.py.
 */

import { estimateGenerationCost } from '../budget.js';
import { halfEvenRound } from '../cachelint/util/halfEven.js';
import type { ModelRegistry } from '../registry/models.js';
import type { FitInternal } from './modelfit.js';

export const DAYS_PER_MONTH = 30;
export const SWAP_QUALITY_EPSILON = 0.05;
const CACHEABLE_VERDICTS = ['CACHEABLE', 'CACHEABLE_WITH_ACTION', 'CACHEABLE_PREFIX'];

export interface CacheCtx {
  verdict_code: string;
  stable_tokens: number;
  total_tokens: number;
  provider_key: string;
  upstream: string | null;
}

function round4(x: number): number {
  return halfEvenRound(x, 4);
}

/**
 * SPEC §2.4 token estimate: max(1, ceil(len/4)) with len in Unicode CODE
 * POINTS — Python's len(); NOT the UTF-16 text.length the budget module's
 * estimateTokens uses (they agree for BMP text).
 */
function estimateTokensCp(text: string): number {
  let cp = 0;
  for (const _ of text) cp++;
  return Math.max(1, Math.ceil(cp / 4));
}

function shell(
  reason: string,
  outputTokens: number,
  inputTokens: number | null = null,
  tokenMethod: string | null = null,
): Record<string, unknown> {
  return {
    status: 'insufficient_data',
    reason,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    token_method: tokenMethod,
    cost_per_call_usd: null,
    monthly: null,
    swap: null,
  };
}

export interface RunCostOptions {
  canonicalText: string | null;
  resolvedModelId: string | null;
  declaredModel: string | null;
  registry: ModelRegistry;
  outputTokens: number;
  callsPerDay: number | null;
  cacheCtx: CacheCtx | null;
  fitInternal: FitInternal | null;
  readDiscountFactors: Record<string, number | null>;
}

export function runCost(opts: RunCostOptions): Record<string, unknown> {
  const {
    canonicalText,
    resolvedModelId,
    declaredModel,
    registry,
    outputTokens,
    callsPerDay,
    cacheCtx,
    fitInternal,
    readDiscountFactors,
  } = opts;

  // --- input tokens -----------------------------------------------------
  let inputTokens: number;
  let tokenMethod: string;
  if (cacheCtx !== null) {
    inputTokens = cacheCtx.total_tokens;
    tokenMethod = 'tokenizer';
  } else if (canonicalText !== null) {
    inputTokens = estimateTokensCp(canonicalText);
    tokenMethod = 'chars/4';
  } else {
    return shell('no prompt', outputTokens);
  }

  // --- per-call cost ----------------------------------------------------
  if (resolvedModelId === null) {
    const reason = declaredModel ? `unknown model '${declaredModel}'` : 'no model declared';
    return shell(reason, outputTokens, inputTokens, tokenMethod);
  }
  const model = registry.getModel(resolvedModelId);
  const rawCost = estimateGenerationCost(model, inputTokens, outputTokens);
  if (rawCost === null) {
    return shell(`no pricing for '${resolvedModelId}'`, outputTokens, inputTokens, tokenMethod);
  }
  const costPerCall = round4(rawCost);

  // --- monthly ----------------------------------------------------------
  let monthly: Record<string, unknown>;
  if (callsPerDay !== null) {
    monthly = {
      status: 'ok',
      calls_per_day: callsPerDay,
      cost_usd: round4(costPerCall * callsPerDay * DAYS_PER_MONTH),
    };
    const savings = cacheSavings(cacheCtx, resolvedModelId, registry, callsPerDay, readDiscountFactors);
    if (savings !== null) monthly.cache_savings_usd = savings;
  } else {
    monthly = { status: 'insufficient_data', reason: 'no traffic estimate' };
  }

  // --- swap -------------------------------------------------------------
  const swap = findSwap(fitInternal, registry, inputTokens, outputTokens, costPerCall, callsPerDay);

  return {
    status: swap !== null ? 'finding' : 'ok',
    reason: null,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    token_method: tokenMethod,
    cost_per_call_usd: costPerCall,
    monthly,
    swap,
  };
}

/** Upper-bound monthly savings from prompt caching (SPEC.md §2.4). */
function cacheSavings(
  cacheCtx: CacheCtx | null,
  resolvedModelId: string,
  registry: ModelRegistry,
  callsPerDay: number,
  factors: Record<string, number | null>,
): number | null {
  if (cacheCtx === null || !CACHEABLE_VERDICTS.includes(cacheCtx.verdict_code)) return null;
  const key = cacheCtx.provider_key;
  let factor = factors[key] ?? null;
  if (factor === null && key === 'openrouter') {
    factor = factors[cacheCtx.upstream ?? ''] ?? null;
  }
  const model = registry.getModel(resolvedModelId);
  if (factor === null || !model || !model.pricing) return null;
  return round4(
    (cacheCtx.stable_tokens / 1000) *
      model.pricing.inputPer1k *
      factor *
      callsPerDay *
      DAYS_PER_MONTH,
  );
}

/** Cheapest ranked model within the quality tolerance (SPEC.md §2.4). */
function findSwap(
  fitInternal: FitInternal | null,
  registry: ModelRegistry,
  inputTokens: number,
  outputTokens: number,
  currentCost: number,
  callsPerDay: number | null,
): Record<string, unknown> | null {
  if (fitInternal === null || fitInternal.currentScore === null) return null;
  const floor = fitInternal.currentScore.qualityScore - SWAP_QUALITY_EPSILON;
  let best: { cost: number; rank: number; modelId: string } | null = null;
  for (let rank0 = 0; rank0 < fitInternal.scores.length; rank0++) {
    const score = fitInternal.scores[rank0];
    if (score.qualityScore < floor) continue;
    const candidate = registry.getModel(score.modelId);
    const rawCost = estimateGenerationCost(candidate, inputTokens, outputTokens);
    if (rawCost === null) continue;
    const cost = round4(rawCost);
    if (best === null || cost < best.cost) {
      // ties break on rank (first wins)
      best = { cost, rank: rank0 + 1, modelId: score.modelId };
    }
  }
  if (best === null || best.cost >= currentCost) return null;
  const swap: Record<string, unknown> = {
    model_id: best.modelId,
    cost_per_call_usd: best.cost,
  };
  if (callsPerDay !== null) {
    swap.monthly_savings_usd = round4((currentCost - best.cost) * callsPerDay * DAYS_PER_MONTH);
  }
  return swap;
}
