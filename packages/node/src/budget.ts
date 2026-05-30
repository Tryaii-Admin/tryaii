/**
 * Budget-aware dataset routing utilities.
 *
 * This solves the routing budget problem as a multiple-choice knapsack:
 * for each prompt, choose one model candidate; maximize total utility while
 * keeping estimated generation cost under one shared budget.
 */

import type { ModelInfo } from './registry/models.js';
import type { Router, RouteResult } from './router.js';
import { Priorities } from './scoring/priorities.js';

export type BudgetMode = 'strict' | 'fit-output';

export interface BudgetCandidate {
  promptIndex: number;
  modelId: string;
  utility: number;
  estimatedCost: number;
  costUnits: number;
  inputTokens: number;
  outputTokens: number;
  finalScore: number;
  reasoning: string;
  normalBestModel: string;
}

export interface BudgetOptimizationResult {
  status: 'optimal' | 'infeasible';
  selected: BudgetCandidate[];
  totalEstimatedCost: number;
  minimumRequiredBudget: number;
  budget: number;
  costUnit: number;
  message?: string;
  budgetMode?: BudgetMode;
  requestedOutputTokens?: number;
  effectiveOutputTokens?: number;
  requestedMinimumRequiredBudget?: number;
  budgetShortfall?: number;
}

export interface BudgetedRouteResult {
  routeResult: RouteResult;
  selected: BudgetCandidate;
  cumulativeCost: number;
  remainingBudget: number;
  routeMs: number;
}

export interface RouteDatasetWithBudgetOptions {
  router: Router;
  prompts: string[];
  priorities: Priorities;
  maxPrice: number;
  outputTokens: number;
  budgetMode?: BudgetMode;
  progressCallback?: (done: number, total: number) => void;
}

interface State {
  utility: number;
  previousCost: number | null;
  candidateIndex: number | null;
}

/** Approximate token count with a deterministic 4 chars ~= 1 token rule. */
export function estimateTokens(text: string): number {
  return Math.max(1, Math.ceil(text.length / 4));
}

/** Estimate USD generation cost for a model, or null when pricing is missing. */
export function estimateGenerationCost(
  model: ModelInfo | undefined,
  inputTokens: number,
  outputTokens: number,
): number | null {
  if (!model?.pricing) return null;
  // Guard against non-finite pricing fields: returning NaN here would poison
  // every downstream comparison/sort, so surface "no estimate" instead.
  if (!Number.isFinite(model.pricing.inputPer1k) || !Number.isFinite(model.pricing.outputPer1k)) {
    return null;
  }
  return (
    (inputTokens / 1000) * model.pricing.inputPer1k
    + (outputTokens / 1000) * model.pricing.outputPer1k
  );
}

export function costUnitForBudget(maxPrice: number): number {
  // Scale the unit to the budget so budgetUnits stays ~constant (~10k) for any
  // budget size. This gives fine *relative* resolution without an absolute
  // floor (the old 1e-5 floor collapsed resolution for sub-cent budgets) while
  // keeping the DP state space bounded -- a fixed maxPrice/1e6 made budgetUnits
  // ~1e6 for a $4 budget and exhausted the heap. Correctness comes from the
  // float feasibility gate + cheapest fallback, so this only affects
  // optimization granularity. Guard maxPrice <= 0 against a zero/negative unit.
  if (maxPrice <= 0) return 1e-9;
  return maxPrice / 10_000;
}

/** Deterministic Unicode code-point compare (NOT locale-aware) for modelIds. */
function compareModelId(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function candidateSortKey(a: BudgetCandidate, b: BudgetCandidate): number {
  if (a.estimatedCost !== b.estimatedCost) return a.estimatedCost - b.estimatedCost;
  if (a.utility !== b.utility) return b.utility - a.utility;
  return compareModelId(a.modelId, b.modelId);
}

export function paretoPrune(candidates: BudgetCandidate[]): BudgetCandidate[] {
  const ordered = [...candidates].sort(candidateSortKey);
  const kept: BudgetCandidate[] = [];
  let bestUtility = Number.NEGATIVE_INFINITY;
  for (const candidate of ordered) {
    if (candidate.utility > bestUtility + 1e-12) {
      kept.push(candidate);
      bestUtility = candidate.utility;
    }
  }
  return kept;
}

export function optimizeBudgetCandidates(
  candidateGroups: BudgetCandidate[][],
  maxPrice: number,
  costUnit = costUnitForBudget(maxPrice),
): BudgetOptimizationResult {
  if (maxPrice < 0) throw new Error('maxPrice must be non-negative');
  if (candidateGroups.length === 0) {
    return {
      status: 'optimal',
      selected: [],
      totalEstimatedCost: 0,
      minimumRequiredBudget: 0,
      budget: maxPrice,
      costUnit,
    };
  }

  const budgetUnits = Math.floor(maxPrice / costUnit);
  const groups = candidateGroups.map((group) => paretoPrune(group));
  if (groups.some((group) => group.length === 0)) {
    return {
      status: 'infeasible',
      selected: [],
      totalEstimatedCost: 0,
      minimumRequiredBudget: Number.POSITIVE_INFINITY,
      budget: maxPrice,
      costUnit,
      message: 'At least one prompt has no priced model candidates.',
    };
  }

  const cheapest = groups.map((group) => [...group].sort(candidateSortKey)[0]);
  const minimumRequiredBudget = cheapest.reduce((sum, candidate) => sum + candidate.estimatedCost, 0);

  // Float feasibility is the source of truth: if the cheapest float assignment
  // fits, the workload IS feasible. The integer DP below only *optimizes* the
  // assignment; it must never declare infeasibility on its own. When the DP
  // frontier collapses (quantization / pruning artefacts), fall back to the
  // proven-feasible cheapest assignment as an optimal selection.
  const cheapestResult = (): BudgetOptimizationResult => ({
    status: 'optimal',
    selected: cheapest,
    totalEstimatedCost: minimumRequiredBudget,
    minimumRequiredBudget,
    budget: maxPrice,
    costUnit,
  });

  if (minimumRequiredBudget > maxPrice) {
    return {
      status: 'infeasible',
      selected: cheapest,
      totalEstimatedCost: minimumRequiredBudget,
      minimumRequiredBudget,
      budget: maxPrice,
      costUnit,
      message: 'Budget is below the minimum cost required to route every prompt.',
    };
  }

  let states = new Map<number, State>([[0, { utility: 0, previousCost: null, candidateIndex: null }]]);
  const layers: Array<Map<number, State>> = [];

  for (const group of groups) {
    const nextStates = new Map<number, State>();
    for (const [previousCost, previousState] of states.entries()) {
      for (let idx = 0; idx < group.length; idx++) {
        const candidate = group[idx];
        const newCost = previousCost + candidate.costUnits;
        if (newCost > budgetUnits) continue;
        const newUtility = previousState.utility + candidate.utility;
        const existing = nextStates.get(newCost);
        if (!existing || newUtility > existing.utility + 1e-12) {
          nextStates.set(newCost, { utility: newUtility, previousCost, candidateIndex: idx });
        }
      }
    }

    if (nextStates.size === 0) {
      // Float feasibility already proven above -> return the cheapest assignment
      // rather than falsely reporting infeasible.
      return cheapestResult();
    }

    const pruned = new Map<number, State>();
    let bestSeen = Number.NEGATIVE_INFINITY;
    for (const cost of [...nextStates.keys()].sort((a, b) => a - b)) {
      const state = nextStates.get(cost) as State;
      if (state.utility > bestSeen + 1e-12) {
        pruned.set(cost, state);
        bestSeen = state.utility;
      }
    }
    states = pruned;
    layers.push(states);
  }

  if (states.size === 0) {
    // No surviving DP state, but float feasibility holds -> cheapest fallback.
    return cheapestResult();
  }

  const [bestCost] = [...states.entries()].sort((a, b) => {
    if (b[1].utility !== a[1].utility) return b[1].utility - a[1].utility;
    return a[0] - b[0];
  })[0];

  const selected = new Array<BudgetCandidate>(groups.length);
  let currentCost = bestCost;
  for (let promptIndex = groups.length - 1; promptIndex >= 0; promptIndex--) {
    const state = layers[promptIndex].get(currentCost);
    if (!state || state.previousCost == null || state.candidateIndex == null) {
      throw new Error('invalid optimizer backpointer');
    }
    selected[promptIndex] = groups[promptIndex][state.candidateIndex];
    currentCost = state.previousCost;
  }

  const totalEstimatedCost = selected.reduce((sum, candidate) => sum + candidate.estimatedCost, 0);
  return {
    status: 'optimal',
    selected,
    totalEstimatedCost,
    minimumRequiredBudget,
    budget: maxPrice,
    costUnit,
  };
}

function repriceCandidate(
  router: Router,
  candidate: BudgetCandidate,
  outputTokens: number,
  costUnit: number,
): BudgetCandidate | null {
  const model = router.models.getModel(candidate.modelId);
  const estimatedCost = estimateGenerationCost(model, candidate.inputTokens, outputTokens);
  // Reject null AND non-finite (NaN/Infinity); the `=== null` arm also narrows
  // the type for the assignment below.
  if (estimatedCost === null || !Number.isFinite(estimatedCost)) return null;
  return {
    ...candidate,
    estimatedCost,
    // Zero-cost candidates must consume zero budget units; only clamp negatives.
    costUnits: Math.max(0, Math.ceil(estimatedCost / costUnit)),
    outputTokens,
  };
}

function repriceCandidateGroups(
  router: Router,
  candidateGroups: BudgetCandidate[][],
  outputTokens: number,
  costUnit: number,
): BudgetCandidate[][] {
  return candidateGroups.map((group) =>
    group
      .map((candidate) => repriceCandidate(router, candidate, outputTokens, costUnit))
      .filter((candidate): candidate is BudgetCandidate => candidate != null),
  );
}

function minimumRequiredUnits(candidateGroups: BudgetCandidate[][]): number {
  if (candidateGroups.some((group) => group.length === 0)) return Number.POSITIVE_INFINITY;
  return candidateGroups.reduce(
    (sum, group) => sum + Math.min(...group.map((candidate) => candidate.costUnits)),
    0,
  );
}

function fitOutputTokens(
  router: Router,
  candidateGroups: BudgetCandidate[][],
  requestedOutputTokens: number,
  maxPrice: number,
  costUnit: number,
): { outputTokens: number; candidateGroups: BudgetCandidate[][] } | null {
  const budgetUnits = Math.floor(maxPrice / costUnit);
  // Require a positive output-token floor: an answer of 0 tokens is degenerate
  // and must not be reported as a feasible "optimal" fit. If nothing >= 1 fits,
  // bestTokens stays -1 and we return null (caller treats that as infeasible).
  let low = 1;
  let high = requestedOutputTokens;
  let bestTokens = -1;
  let bestGroups: BudgetCandidate[][] = [];

  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const repriced = repriceCandidateGroups(router, candidateGroups, mid, costUnit);
    if (minimumRequiredUnits(repriced) <= budgetUnits) {
      bestTokens = mid;
      bestGroups = repriced;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }

  return bestTokens >= 1 ? { outputTokens: bestTokens, candidateGroups: bestGroups } : null;
}

async function buildBudgetCandidates(
  router: Router,
  prompt: string,
  promptIndex: number,
  priorities: Priorities,
  outputTokens: number,
  costUnit: number,
): Promise<{ routeResult: RouteResult; candidates: BudgetCandidate[]; routeMs: number }> {
  const started = Date.now();
  const routeResult = await router.route(prompt, {
    topK: router.models.allModels.length,
    priorities,
  });
  const routeMs = Date.now() - started;
  const confidence = routeResult.classification?.confidence ?? 0;
  const confidenceFactor = 0.75 + 0.25 * Math.max(0, Math.min(1, confidence));
  const inputTokens = estimateTokens(prompt);
  const candidateInputs: Array<{
    score: RouteResult['scores'][number];
    estimatedCost: number;
    utility: number;
  }> = [];

  for (const score of routeResult.scores) {
    const model = router.models.getModel(score.modelId);
    const estimatedCost = estimateGenerationCost(model, inputTokens, outputTokens);
    // Reject null AND non-finite (NaN/Infinity); the `=== null` arm also narrows
    // the type for the push below.
    if (estimatedCost === null || !Number.isFinite(estimatedCost)) continue;
    candidateInputs.push({
      score,
      estimatedCost,
      utility: score.qualityScore * confidenceFactor,
    });
  }

  const qualityBestModel =
    [...candidateInputs].sort((a, b) => {
      if (b.utility !== a.utility) return b.utility - a.utility;
      if (a.estimatedCost !== b.estimatedCost) return a.estimatedCost - b.estimatedCost;
      return compareModelId(a.score.modelId, b.score.modelId);
    })[0]?.score.modelId ?? routeResult.bestModel;

  const candidates: BudgetCandidate[] = [];
  for (const { score, estimatedCost, utility } of candidateInputs) {
    candidates.push({
      promptIndex,
      modelId: score.modelId,
      utility,
      estimatedCost,
      // Zero-cost candidates must consume zero budget units; only clamp negatives.
      costUnits: Math.max(0, Math.ceil(estimatedCost / costUnit)),
      inputTokens,
      outputTokens,
      finalScore: score.finalScore,
      reasoning: score.reasoning,
      normalBestModel: qualityBestModel,
    });
  }

  return { routeResult, candidates, routeMs };
}

export async function routeDatasetWithBudget(
  options: RouteDatasetWithBudgetOptions,
): Promise<{ results: BudgetedRouteResult[]; optimization: BudgetOptimizationResult }> {
  const budgetMode = options.budgetMode ?? 'strict';
  if (budgetMode !== 'strict' && budgetMode !== 'fit-output') {
    throw new Error("budgetMode must be 'strict' or 'fit-output'");
  }
  if (options.outputTokens < 0) throw new Error('outputTokens must be non-negative');

  const costUnit = costUnitForBudget(options.maxPrice);
  const routeResults: RouteResult[] = [];
  const routeTimes: number[] = [];
  const candidateGroups: BudgetCandidate[][] = [];
  const qualityPriorities = Priorities.performance();

  for (let idx = 0; idx < options.prompts.length; idx++) {
    const prepared = await buildBudgetCandidates(
      options.router,
      options.prompts[idx],
      idx,
      qualityPriorities,
      options.outputTokens,
      costUnit,
    );
    routeResults.push(prepared.routeResult);
    routeTimes.push(prepared.routeMs);
    candidateGroups.push(prepared.candidates);
    options.progressCallback?.(idx + 1, options.prompts.length);
  }

  const requestedOutputTokens = options.outputTokens;
  let optimization = optimizeBudgetCandidates(candidateGroups, options.maxPrice, costUnit);
  const requestedMinimumRequiredBudget = optimization.minimumRequiredBudget;
  const budgetShortfall = Number.isFinite(requestedMinimumRequiredBudget)
    ? Math.max(0, requestedMinimumRequiredBudget - options.maxPrice)
    : Number.POSITIVE_INFINITY;

  optimization = {
    ...optimization,
    budgetMode,
    requestedOutputTokens,
    effectiveOutputTokens: options.outputTokens,
    requestedMinimumRequiredBudget,
    budgetShortfall,
  };

  if (
    budgetMode === 'fit-output' &&
    optimization.status === 'infeasible' &&
    options.outputTokens > 0 &&
    Number.isFinite(optimization.minimumRequiredBudget)
  ) {
    const fitted = fitOutputTokens(
      options.router,
      candidateGroups,
      requestedOutputTokens,
      options.maxPrice,
      costUnit,
    );
    if (fitted) {
      const fittedOptimization = optimizeBudgetCandidates(
        fitted.candidateGroups,
        options.maxPrice,
        costUnit,
      );
      if (fittedOptimization.status === 'optimal') {
        optimization = {
          ...fittedOptimization,
          budgetMode,
          requestedOutputTokens,
          effectiveOutputTokens: fitted.outputTokens,
          requestedMinimumRequiredBudget,
          budgetShortfall,
          message:
            'Requested output tokens did not fit the budget; ' +
            `optimized with ${fitted.outputTokens} output tokens per prompt.`,
        };
      }
    }
  }

  const selectedByIndex = new Map(
    optimization.selected.map((candidate) => [candidate.promptIndex, candidate]),
  );
  const results: BudgetedRouteResult[] = [];
  let cumulativeCost = 0;

  for (let idx = 0; idx < routeResults.length; idx++) {
    const selected = selectedByIndex.get(idx);
    if (!selected) continue;
    cumulativeCost += selected.estimatedCost;
    results.push({
      routeResult: routeResults[idx],
      selected,
      cumulativeCost,
      remainingBudget: options.maxPrice - cumulativeCost,
      routeMs: routeTimes[idx],
    });
  }

  return { results, optimization };
}
