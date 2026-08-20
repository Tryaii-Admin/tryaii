/**
 * Orchestrator: inventory -> findings document (SPEC.md §2–§3).
 *
 * Deterministic and clock-free: run_id / generated_at / version arrive via
 * `opts`, classification arrives via each site's `_classification` seam or
 * the injected classifyFn — the engine itself never touches a model, the
 * network, or a clock. Mirrors diagnose/api.py.
 */

import { readFileSync } from 'node:fs';

import { analyzeItem, buildCanonical } from '../cachelint/analyzer.js';
import { halfEvenRound } from '../cachelint/util/halfEven.js';
import { ModelRegistry } from '../registry/models.js';
import { Priorities } from '../scoring/priorities.js';
import { runCost, type CacheCtx } from './cost.js';
import { runHygiene } from './hygiene.js';
import { cleanClassification, normalizeInventory } from './intake.js';
import { runModelFit, type FitInternal } from './modelfit.js';
import { resolveModelId } from './resolve.js';

export const DEFAULT_CHECKS = ['model_fit', 'cache_readiness', 'cost_exposure', 'hygiene'];

export const CACHE_TOP_FINDINGS = 3;

let costmodelCache: Record<string, number | null> | null = null;

export function readDiscountFactors(): Record<string, number | null> {
  if (costmodelCache === null) {
    const raw = JSON.parse(
      readFileSync(new URL('./data/costmodel.json', import.meta.url), 'utf-8'),
    ) as { read_discount_factors: Record<string, number | null> };
    costmodelCache = raw.read_discount_factors;
  }
  return costmodelCache;
}

function insufficient(reason: string): Record<string, unknown> {
  return { status: 'insufficient_data', reason };
}

function skipped(): Record<string, unknown> {
  return { status: 'skipped', reason: 'not selected' };
}

function selectChecks(requested: string[] | undefined | null): string[] {
  if (requested == null) return [...DEFAULT_CHECKS];
  const unknown = requested.filter((c) => !DEFAULT_CHECKS.includes(c));
  if (unknown.length) throw new Error(`unknown check '${unknown[0]}'`);
  const selected = DEFAULT_CHECKS.filter((c) => requested.includes(c));
  if (!selected.length) throw new Error('no checks selected');
  return selected;
}

function runCacheReadiness(
  prompt: unknown,
  provider: string | null,
  model: string | null,
): [Record<string, unknown>, CacheCtx | null] {
  if (prompt === null) return [insufficient('no prompt'), null];
  if (provider === null) return [insufficient('no provider declared'), null];
  let item;
  try {
    item = analyzeItem({ prompt, llm: { provider, name: model ?? '' } });
  } catch (err) {
    return [insufficient((err as Error).message), null];
  }
  const data = item.data as Record<string, any>;
  const verdictCode = data.verdict.code as string;
  const payload: Record<string, unknown> = {
    status: verdictCode === 'CACHEABLE' ? 'ok' : 'finding',
    reason: null,
    verdict: data.verdict,
    threshold_min_tokens: data.threshold.min_tokens,
    total_tokens: data.token_report.total.tokens,
    stable_prefix: {
      tokens: data.stable_prefix.tokens,
      pct_of_prompt: data.stable_prefix.pct_of_prompt,
      if_first_fixed_tokens: data.stable_prefix.if_first_fixed_tokens,
    },
    findings_count: (data.findings as unknown[]).length,
    top_findings: (data.findings as unknown[]).slice(0, CACHE_TOP_FINDINGS),
    recommendations: data.recommendations,
  };
  const cacheCtx: CacheCtx = {
    verdict_code: verdictCode,
    stable_tokens: data.stable_prefix.tokens,
    total_tokens: data.token_report.total.tokens,
    provider_key: data.provider_key,
    upstream: data.upstream,
  };
  return [payload, cacheCtx];
}

export interface AnalyzeInventoryOpts {
  run_id?: string;
  now?: string | null;
  version?: string;
  priorities?: { quality?: number; cost?: number; speed?: number };
  goal?: string | null;
  checks?: string[] | null;
  calls_per_day?: number | null;
  output_tokens?: number | null;
}

export type ClassifyFn = (
  canonical: string,
) => Record<string, unknown> | null | Promise<Record<string, unknown> | null>;

/**
 * Run the selected checks over an inventory; returns the findings document
 * (SPEC.md §3). Throws on an unusable inventory or an unknown check name;
 * everything site-level degrades per SPEC.md §2.0.
 */
export async function analyzeInventory(
  data: unknown,
  opts: AnalyzeInventoryOpts = {},
  classifyFn?: ClassifyFn,
  registry?: ModelRegistry,
): Promise<Record<string, unknown>> {
  const norm = normalizeInventory(data);
  const priorities = Priorities.fromDict(opts.priorities ?? {});
  const checks = selectChecks(opts.checks);
  const reg = registry ?? ModelRegistry.default();
  const factors = readDiscountFactors();

  const defaultCalls = opts.calls_per_day != null ? opts.calls_per_day : norm.defaults.calls_per_day;
  const defaultOutput =
    opts.output_tokens != null ? opts.output_tokens : norm.defaults.output_tokens;
  const goal = opts.goal || null;

  const sitesOut: Record<string, unknown>[] = [];
  for (const site of norm.sites) {
    const prompt = site.prompt;
    const canonical = prompt !== null ? buildCanonical(prompt)[0] : null;

    const [resolvedId] = resolveModelId(site.model, reg);

    let classification = site.classification;
    if (classification === null && classifyFn !== undefined && canonical !== null) {
      classification = cleanClassification(await classifyFn(canonical));
    }

    // model_fit
    let fitInternal: FitInternal | null = null;
    let fitPayload: Record<string, unknown>;
    if (!checks.includes('model_fit')) {
      fitPayload = skipped();
    } else if (prompt === null) {
      fitPayload = insufficient('no prompt');
    } else if (classification === null) {
      fitPayload = insufficient('no classifier available');
    } else {
      [fitPayload, fitInternal] = runModelFit(
        classification,
        resolvedId,
        site.model,
        priorities,
        reg,
      );
    }

    // cache_readiness
    let cacheCtx: CacheCtx | null = null;
    let cachePayload: Record<string, unknown>;
    if (!checks.includes('cache_readiness')) {
      cachePayload = skipped();
    } else {
      [cachePayload, cacheCtx] = runCacheReadiness(prompt, site.provider, site.model);
    }

    // cost_exposure
    let costPayload: Record<string, unknown>;
    if (!checks.includes('cost_exposure')) {
      costPayload = skipped();
    } else {
      costPayload = runCost({
        canonicalText: canonical,
        resolvedModelId: resolvedId,
        declaredModel: site.model,
        registry: reg,
        outputTokens: site.output_tokens ?? defaultOutput,
        callsPerDay: site.calls_per_day !== null ? site.calls_per_day : defaultCalls,
        cacheCtx,
        fitInternal,
        readDiscountFactors: factors,
      });
    }

    // hygiene
    let hygienePayload: Record<string, unknown>;
    if (!checks.includes('hygiene')) {
      hygienePayload = skipped();
    } else if (prompt === null) {
      hygienePayload = insufficient('no prompt');
    } else {
      hygienePayload = runHygiene(prompt);
    }

    const entry: Record<string, unknown> = {
      site_id: site.site_id,
      file: site.file,
      line: site.line,
      provider: site.provider,
      model: site.model,
      resolved_model_id: resolvedId,
    };
    if (site.notes !== null) entry.notes = site.notes;
    entry.checks = {
      model_fit: fitPayload,
      cache_readiness: cachePayload,
      cost_exposure: costPayload,
      hygiene: hygienePayload,
    };
    sitesOut.push(entry);
  }

  const runId = opts.run_id || 'run';
  const generatedAt = opts.now ?? null;
  const interview = {
    priorities: priorities.toDict(),
    goal,
    defaults: { calls_per_day: defaultCalls, output_tokens: defaultOutput },
  };

  return {
    version: 1,
    run_id: runId,
    generated_at: generatedAt,
    tool: { name: 'tryaii', version: opts.version || '0.0.0' },
    interview,
    inventory: { site_count: sitesOut.length, skipped: norm.skipped },
    sites: sitesOut,
    summary: summarize(runId, generatedAt, goal, priorities, sitesOut),
  };
}

/**
 * The redacted layer (SPEC.md §3.1) — no code, prompts, paths, reasoning
 * text, or goal text ever lands here.
 */
function summarize(
  runId: string,
  generatedAt: string | null,
  goal: string | null,
  priorities: Priorities,
  sites: Record<string, unknown>[],
): Record<string, unknown> {
  const statusCounts: Record<string, Record<string, number>> = {};
  for (const check of DEFAULT_CHECKS) {
    statusCounts[check] = { ok: 0, finding: 0, insufficient_data: 0, skipped: 0 };
  }
  const verdictCounts: Record<string, number> = {};
  const monthlyCosts: number[] = [];
  const cacheSavings: number[] = [];
  const swapSavings: number[] = [];
  let trafficSites = 0;
  let swapSites = 0;
  const ranks: number[] = [];

  for (const site of sites) {
    const siteChecks = site.checks as Record<string, Record<string, any>>;
    for (const [check, payload] of Object.entries(siteChecks)) {
      statusCounts[check][payload.status as string] += 1;
    }

    const cache = siteChecks.cache_readiness;
    if (cache.status === 'ok' || cache.status === 'finding') {
      const code = cache.verdict.code as string;
      verdictCounts[code] = (verdictCounts[code] ?? 0) + 1;
    }

    const cost = siteChecks.cost_exposure;
    const monthly = cost.monthly as Record<string, unknown> | null | undefined;
    if (monthly && typeof monthly === 'object' && monthly.status === 'ok') {
      trafficSites += 1;
      monthlyCosts.push(monthly.cost_usd as number);
      if ('cache_savings_usd' in monthly) cacheSavings.push(monthly.cache_savings_usd as number);
    }
    const swap = cost.swap as Record<string, unknown> | null | undefined;
    if (swap != null) {
      swapSites += 1;
      if ('monthly_savings_usd' in swap) swapSavings.push(swap.monthly_savings_usd as number);
    }

    const fit = siteChecks.model_fit;
    if (fit.current_rank != null) ranks.push(fit.current_rank as number);
  }

  const total = (values: number[]): number | null =>
    values.length ? halfEvenRound(values.reduce((a, b) => a + b, 0), 4) : null;

  const median = (values: number[]): number | null => {
    if (!values.length) return null;
    const s = [...values].sort((a, b) => a - b);
    const mid = Math.floor(s.length / 2);
    return s.length % 2 === 1 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
  };

  return {
    schema: 'tryaii.diagnose.summary/1',
    run_id: runId,
    generated_at: generatedAt,
    site_count: sites.length,
    goal_present: goal !== null,
    priorities: priorities.toDict(),
    check_status_counts: statusCounts,
    cache_verdict_counts: verdictCounts,
    totals: {
      est_monthly_cost_usd: total(monthlyCosts),
      est_monthly_cache_savings_usd: total(cacheSavings),
      est_monthly_swap_savings_usd: total(swapSavings),
      sites_with_traffic_data: trafficSites,
    },
    swap_stats: {
      sites_with_cheaper_swap: swapSites,
      median_current_rank: median(ranks),
    },
  };
}
