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
  /**
   * Per-prompt difficulty in [0, 1] (capability sensitivity). Same value for
   * every candidate of a prompt; surfaced here so eval/consumers can report it.
   */
  difficulty: number;
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
  /**
   * Difficulty amplification for the knapsack utility (see DEFAULT_DIFFICULTY_GAMMA).
   * Higher => the optimizer concentrates more budget on harder prompts. 0 disables
   * complexity-aware allocation (utility = raw quality).
   */
  difficultyGamma?: number;
  /** Which difficulty signal to use: 'intrinsic' (default), 'capability', or 'blend'. */
  difficultySource?: DifficultySource;
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

/**
 * Default difficulty amplification for the budget knapsack.
 *
 * utility = quality * (1 + DIFFICULTY_GAMMA * difficulty), so a maximally-hard
 * prompt (difficulty = 1) counts up to (1 + gamma)x as much as an easy one in the
 * shared-budget optimization. This is the lever that makes the optimizer invest
 * more in complex prompts. Tunable via RouteDatasetWithBudgetOptions.difficultyGamma.
 * Must stay in sync with the Python SDK's DEFAULT_DIFFICULTY_GAMMA (budget.py).
 */
export const DEFAULT_DIFFICULTY_GAMMA = 1.0;

/**
 * Which difficulty signal drives allocation:
 *   - 'intrinsic'  : embedding distance to easy/hard centroids (content-based) [default]
 *   - 'capability' : spread of model quality vs price (computeDifficulty)
 *   - 'blend'      : mean of the two
 * Must stay in sync with the Python SDK (DEFAULT_DIFFICULTY_SOURCE).
 */
export type DifficultySource = 'intrinsic' | 'capability' | 'blend';
export const DEFAULT_DIFFICULTY_SOURCE: DifficultySource = 'intrinsic';

/**
 * Combine the two difficulty signals according to the chosen source:
 *   - 'capability' -> model-spread difficulty only
 *   - 'intrinsic'  -> content-based difficulty only (default)
 *   - 'blend'      -> their mean
 * Must stay in sync with the Python SDK's _resolve_difficulty (budget.py).
 */
export function resolveDifficulty(
  source: DifficultySource,
  capability: number,
  intrinsic: number,
): number {
  if (source === 'capability') return capability;
  if (source === 'blend') return 0.5 * (capability + intrinsic);
  return intrinsic;
}

/**
 * Per-prompt difficulty = capability sensitivity: how much the achievable quality
 * depends on which model you pick. We compare the best quality reachable with a
 * *cheap* model against the best quality reachable at all:
 *
 *     difficulty = (qTop - qCheap) / qTop          (clamped to [0, 1])
 *
 *   - difficulty ~ 0 -> even a cheap model is about as good as the frontier -> EASY
 *   - difficulty ~ 1 -> only expensive models reach the top quality         -> HARD
 *
 * "Cheap tier" = the cheapest third of candidates by estimated cost (at least one).
 * Using best-cheap vs best-overall (not min vs max) keeps a single junk model from
 * inflating difficulty, and correctly reports EASY when a cheap-but-strong model
 * exists. Returns 0 for empty input or a non-positive ceiling (nothing to invest in).
 * Must stay in sync with the Python SDK's compute_difficulty (budget.py).
 */
export function computeDifficulty(points: Array<{ quality: number; cost: number }>): number {
  if (points.length === 0) return 0;

  let qTop = Number.NEGATIVE_INFINITY;
  for (const p of points) if (p.quality > qTop) qTop = p.quality;
  if (!(qTop > 0)) return 0;

  const byCost = [...points].sort((a, b) => a.cost - b.cost);
  const tierSize = Math.max(1, Math.floor(byCost.length / 3));
  let qCheap = Number.NEGATIVE_INFINITY;
  for (let i = 0; i < tierSize; i++) if (byCost[i].quality > qCheap) qCheap = byCost[i].quality;

  const difficulty = (qTop - qCheap) / qTop;
  return Math.max(0, Math.min(1, difficulty));
}

/**
 * Map a list of values to their rank within the batch, scaled to [0, 1]
 * (smallest -> 0, largest -> 1, ties share the average rank). Used to spread a
 * compressed difficulty signal across the full range so relative ordering --
 * not tiny absolute differences -- drives budget allocation. Empty / single
 * inputs map to 0 (no relative information to act on). Must stay in sync with
 * the Python SDK's _batch_percentile_ranks (budget.py).
 */
export function batchPercentileRanks(values: number[]): number[] {
  const n = values.length;
  if (n <= 1) return values.map(() => 0);
  const order = values.map((v, i) => ({ v, i })).sort((a, b) => a.v - b.v);
  const ranks = new Array<number>(n);
  let i = 0;
  while (i < n) {
    let j = i;
    while (j + 1 < n && order[j + 1].v === order[i].v) j++;
    const avg = (i + j) / 2; // 0-based average rank for the tie group
    for (let k = i; k <= j; k++) ranks[order[k].i] = avg / (n - 1);
    i = j + 1;
  }
  return ranks;
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
  difficultySource: DifficultySource,
): Promise<{ routeResult: RouteResult; candidates: BudgetCandidate[]; routeMs: number; difficulty: number }> {
  const started = Date.now();
  const routeResult = await router.route(prompt, {
    topK: router.models.allModels.length,
    priorities,
  });
  const routeMs = Date.now() - started;
  const inputTokens = estimateTokens(prompt);

  // Price every model first so the prompt's difficulty can be read off the
  // (cost, quality) spread before utilities are assigned.
  const priced: Array<{ score: RouteResult['scores'][number]; estimatedCost: number }> = [];
  for (const score of routeResult.scores) {
    const model = router.models.getModel(score.modelId);
    const estimatedCost = estimateGenerationCost(model, inputTokens, outputTokens);
    // Reject null AND non-finite (NaN/Infinity).
    if (estimatedCost === null || !Number.isFinite(estimatedCost)) continue;
    priced.push({ score, estimatedCost });
  }

  // Difficulty = capability sensitivity (see computeDifficulty). The factor
  // scales every candidate of THIS prompt equally, so it never changes which
  // model is best for the prompt -- it only raises the prompt's weight in the
  // cross-prompt budget knapsack, which is exactly how harder prompts win more
  // budget. The old `confidence` multiplier is dropped on purpose: it measured
  // category-match clarity (often higher for easy, prototypical prompts), not
  // difficulty, and so worked mildly against complexity-aware allocation.
  // Pick the difficulty signal. 'capability' = how much model choice changes
  // quality (computeDifficulty); 'intrinsic' = content-based easy/hard centroid
  // distance from the classifier; 'blend' = mean of the two. Intrinsic falls back
  // to capability when the classifier didn't supply it (e.g. the sync path).
  const capabilityDifficulty = computeDifficulty(
    priced.map((p) => ({ quality: p.score.qualityScore, cost: p.estimatedCost })),
  );
  const intrinsicDifficulty = routeResult.classification?.difficulty ?? capabilityDifficulty;
  const difficulty = resolveDifficulty(difficultySource, capabilityDifficulty, intrinsicDifficulty);
  // gamma is applied later in routeDatasetWithBudget, AFTER batch-normalizing
  // difficulty across all prompts, so a compressed raw signal still produces
  // strong relative ordering. Here utility is just raw quality.
  const candidateInputs: Array<{
    score: RouteResult['scores'][number];
    estimatedCost: number;
    utility: number;
  }> = priced.map((p) => ({
    score: p.score,
    estimatedCost: p.estimatedCost,
    utility: p.score.qualityScore,
  }));

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
      difficulty,
    });
  }

  return { routeResult, candidates, routeMs, difficulty };
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
  const difficultyGamma = options.difficultyGamma ?? DEFAULT_DIFFICULTY_GAMMA;
  const difficultySource = options.difficultySource ?? DEFAULT_DIFFICULTY_SOURCE;
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
      difficultySource,
    );
    routeResults.push(prepared.routeResult);
    routeTimes.push(prepared.routeMs);
    candidateGroups.push(prepared.candidates);
    options.progressCallback?.(idx + 1, options.prompts.length);
  }

  // Fix 1: batch-normalize difficulty. Raw capability-sensitivity is compressed
  // (most prompts ~0.05-0.09, because cheap models are strong), so the absolute
  // value barely separates prompts. Replace it with each prompt's RANK within the
  // batch (0 = easiest, 1 = hardest) and apply utility *= 1 + gamma * rank. This
  // preserves the ordering but stretches it to a full 0..1 range, so the knapsack
  // actually reallocates budget toward the relatively-harder prompts.
  const rawDifficulties = candidateGroups.map((group) => group[0]?.difficulty ?? 0);
  const difficultyRanks = batchPercentileRanks(rawDifficulties);
  for (let idx = 0; idx < candidateGroups.length; idx++) {
    const factor = 1 + difficultyGamma * difficultyRanks[idx];
    for (const candidate of candidateGroups[idx]) {
      candidate.utility *= factor;
    }
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
