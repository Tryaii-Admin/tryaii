/**
 * Provider class, reachability, policy and best-for-less winner
 * (SPEC.md §2.2–§2.4, §2.8–§2.9).
 *
 * Pure functions over the daemon's route wire shape. `scores` may be
 * `ModelScore` objects or the wire dicts the daemon returns
 * (`{modelId, qualityScore, ...}`); both are read through `scoreId` /
 * `scoreQuality`. Mirrors monitor/winner.py.
 *
 * `allowed` arguments are the data-save policy's allowed catalog ids (any
 * iterable) or null for "no policy" (unconstrained).
 */

import { resolveModelId } from '../diagnose/resolve.js';
import { MODEL_ID_TO_OPENROUTER } from '../integrations/index.js';
import { evaluate, PolicyError, type ModelPolicy } from '../policy.js';
import type { ModelRegistry } from '../registry/models.js';

export const PROVIDER_OPENAI = 'openai';
export const PROVIDER_OPENROUTER = 'openrouter';
export const PROVIDER_OTHER = 'other';
export const PROVIDERS = [PROVIDER_OPENAI, PROVIDER_OPENROUTER, PROVIDER_OTHER] as const;
export type Provider = (typeof PROVIDERS)[number];
/**
 * The event `provider` of a Messages-API client (Anthropic, Bedrock, Vertex;
 * SPEC.md §3.1). Not a §2.2 class: every routing rule treats it like `other`
 * (only the anchor is reachable, never a swap). It tells the server that
 * `usage.prompt_tokens` excludes the cached and written tokens (§3.1b).
 */
export const PROVIDER_ANTHROPIC = 'anthropic';
/** A §2.2 provider class, or `anthropic` for a Messages-API client. */
export type EventProvider = Provider | typeof PROVIDER_ANTHROPIC;

export const MODE_OFF = 'off';
export const MODE_STABLE = 'stable';
export const MODE_DYNAMIC = 'dynamic';
export const MODE_MANAGED = 'managed';
export const MODES = [MODE_OFF, MODE_STABLE, MODE_DYNAMIC, MODE_MANAGED] as const;
export type Mode = (typeof MODES)[number];

/** `route_status` when a managed model could not be sent (SPEC.md §2.9). */
export const STATUS_UNREACHABLE_MANAGED = 'unreachable_managed';
/** `route_status` when the server's stable pin could not be sent (SPEC.md §2.5, API.md v1.8). */
export const STATUS_UNREACHABLE_PIN = 'unreachable_pin';

/** The policy's allowed ids (any iterable), or null = unconstrained. */
export type Allowed = Iterable<string> | null | undefined;

/** Shared with diagnose's cost-exposure swap rule (modelfit.py). */
export const SWAP_QUALITY_EPSILON = 0.05;

export interface WinnerPick {
  winner: string | null;
  rank: number | null;
  method: 'none' | 'top' | 'band' | 'anchor';
}

/** A score in either shape: the daemon's wire dict or a ModelScore. */
export type ScoreLike = Record<string, unknown> | { modelId: string; qualityScore: number };

/**
 * Classify a client's base URL: `openai` (api.openai.com or unset),
 * `openrouter` (openrouter.ai) or `other` (any other host, including Azure
 * OpenAI and local OpenAI-compatible servers).
 */
export function providerClass(baseUrl: unknown): Provider {
  if (!baseUrl || typeof baseUrl !== 'string') return PROVIDER_OPENAI;
  let text = baseUrl.trim();
  if (!text) return PROVIDER_OPENAI;
  if (!text.includes('//')) text = 'https://' + text;
  let host = '';
  try {
    host = new URL(text).hostname.toLowerCase();
  } catch {
    host = '';
  }
  // Python's urlparse keeps IPv6 brackets off the hostname; WHATWG URL keeps
  // them. Neither is an OpenAI/OpenRouter host, so the class is unaffected.
  if (!host) return PROVIDER_OTHER;
  if (host === 'openai.com' || host.endsWith('.openai.com')) return PROVIDER_OPENAI;
  if (host === 'openrouter.ai' || host.endsWith('.openrouter.ai')) return PROVIDER_OPENROUTER;
  return PROVIDER_OTHER;
}

/** The catalog id of the developer's model, or null (diagnose §2.1). */
export function resolveAnchor(model: string | null | undefined, registry: ModelRegistry): string | null {
  const [modelId] = resolveModelId(model, registry);
  return modelId;
}

/**
 * Can the wrapped client send a request for `modelId`? `openrouter`
 * reaches the whole catalog; `openai` only `openai/*` ids; `other` only the
 * anchor itself (never a swap).
 */
export function reachable(provider: string, modelId: string | null | undefined, anchorId: string | null = null): boolean {
  if (!modelId) return false;
  if (provider === PROVIDER_OPENROUTER) return true;
  if (provider === PROVIDER_OPENAI) return modelId.startsWith('openai/');
  return anchorId !== null && anchorId !== undefined && modelId === anchorId;
}

/**
 * The `allowedModels` list for a daemon route request: null for
 * `openrouter` (unfiltered), the `openai/*` ids for `openai`, and
 * `[anchor]` (or `[]`) for `other`. Order follows `modelIds`.
 */
export function allowedModels(provider: string, modelIds: Iterable<string>, anchorId: string | null = null): string[] | null {
  if (provider === PROVIDER_OPENROUTER) return null;
  return Array.from(modelIds).filter((m) => reachable(provider, m, anchorId));
}

/**
 * The catalog ids a data-save `policy` allows (SPEC.md §2.8). null when the
 * policy is missing or inactive (unconstrained). An active policy that
 * cannot be evaluated (no `origins.json` in the bundle, an unknown provider
 * name) fails CLOSED: `[]` -- tryaii then picks nothing and the developer's
 * model is sent. Pure: never loads the embedding model and never touches
 * the daemon.
 */
export function policyAllowed(
  policy: ModelPolicy | null | undefined,
  origins: unknown,
  modelIds: Iterable<string>,
): string[] | null {
  if (!policy || !policy.active) return null;
  try {
    return Array.from(evaluate(policy, origins, modelIds).allowed);
  } catch (error) {
    if (error instanceof PolicyError) return [];
    throw error;
  }
}

/** `scores` restricted to `allowed` ids, order kept (null = all). */
export function filterRanking<T extends ScoreLike>(scores: Iterable<T> | null | undefined, allowed: Allowed): T[] {
  const ranked = Array.from(scores ?? []);
  if (allowed === null || allowed === undefined) return ranked;
  const only = new Set(allowed);
  return ranked.filter((s) => only.has(scoreId(s)));
}

/**
 * The `allowedModels` of the request whose ranking picks the model actually
 * SENT: reachable ∩ policy-allowed, in `modelIds` order; null when both are
 * unconstrained (an `openrouter` client, no policy).
 */
export function routeAllowed(
  provider: string,
  modelIds: Iterable<string>,
  anchorId: string | null = null,
  allowed: Allowed = null,
): string[] | null {
  const ids = Array.from(modelIds);
  const reach = allowedModels(provider, ids, anchorId);
  if (allowed === null || allowed === undefined) return reach;
  const only = new Set(allowed);
  const base = reach === null ? ids : reach;
  return base.filter((m) => only.has(m));
}

/**
 * The `allowedModels` of the SHADOW request (the recommendation): the
 * policy's ids in `modelIds` order, null without a policy.
 */
export function shadowAllowed(modelIds: Iterable<string>, allowed: Allowed = null): string[] | null {
  if (allowed === null || allowed === undefined) return null;
  const only = new Set(allowed);
  return Array.from(modelIds).filter((m) => only.has(m));
}

/**
 * The id to put in the request for a catalog id: `openai` strips the
 * namespace (`openai/gpt-5.5` → `gpt-5.5`); `openrouter` maps through
 * MODEL_ID_TO_OPENROUTER (identity for slugs); `other` is unchanged.
 */
export function nativeModelId(provider: string, modelId: string): string {
  if (provider === PROVIDER_OPENAI) {
    const slash = modelId.indexOf('/');
    return slash >= 0 ? modelId.slice(slash + 1) : modelId;
  }
  if (provider === PROVIDER_OPENROUTER) return MODEL_ID_TO_OPENROUTER[modelId] ?? modelId;
  return modelId;
}

export function scoreId(score: ScoreLike | null | undefined): string {
  if (!score || typeof score !== 'object') return '';
  const s = score as Record<string, unknown>;
  const value = s.modelId ?? s.model_id;
  return typeof value === 'string' ? value : '';
}

export function scoreQuality(score: ScoreLike | null | undefined): number {
  if (!score || typeof score !== 'object') return 0;
  const s = score as Record<string, unknown>;
  let value = s.qualityScore;
  if (value === null || value === undefined) value = s.quality_score ?? 0;
  return Number(value);
}

/** The scoring engine's cost basis: mean of input/output per-1k prices. */
export function blendedPrice(
  model: { pricing?: { inputPer1k: number; outputPer1k: number } | null } | undefined,
): number | null {
  if (!model || !model.pricing) return null;
  return (model.pricing.inputPer1k + model.pricing.outputPer1k) / 2;
}


/**
 * best-for-less over a value-ranked `scores` list (SPEC.md §2.3).
 *
 * `method` is `top` (no anchor in the ranking → the top model), `band` (the
 * first model with quality ≥ anchor − 0.05 and blended price ≤ the
 * anchor's), `anchor` (nothing cheaper-and-as-good, or the anchor is
 * unpriced), or `none` (empty ranking). `allowed` (the policy's ids) first
 * restricts the ranking, so a disallowed model -- the anchor included -- is
 * never the winner; `rank` is 1-based in the restricted ranking.
 */
export function pickWinner(
  scores: ScoreLike[] | null | undefined,
  anchorId: string | null | undefined,
  registry: ModelRegistry,
  allowed: Allowed = null,
): WinnerPick {
  const ranked = filterRanking(scores, allowed);
  if (ranked.length === 0) return { winner: null, rank: null, method: 'none' };
  let anchor: ScoreLike | null = null;
  let anchorRank: number | null = null;
  if (anchorId) {
    for (let i = 0; i < ranked.length; i++) {
      if (scoreId(ranked[i]) === anchorId) {
        anchor = ranked[i];
        anchorRank = i + 1;
        break;
      }
    }
  }
  if (anchor === null || !anchorId) return { winner: scoreId(ranked[0]), rank: 1, method: 'top' };
  const cap = blendedPrice(registry.getModel(anchorId));
  if (cap === null) return { winner: anchorId, rank: anchorRank, method: 'anchor' };
  const floor = scoreQuality(anchor) - SWAP_QUALITY_EPSILON;
  for (let i = 0; i < ranked.length; i++) {
    const s = ranked[i];
    if (scoreQuality(s) < floor) continue;
    const price = blendedPrice(registry.getModel(scoreId(s)));
    if (price === null || price > cap) continue;
    if (scoreId(s) === anchorId) return { winner: anchorId, rank: anchorRank, method: 'anchor' };
    return { winner: scoreId(s), rank: i + 1, method: 'band' };
  }
  return { winner: anchorId, rank: anchorRank, method: 'anchor' };
}

/**
 * The catalog id the call should be sent to, or null = keep the
 * developer's model as written (SPEC.md §2.4): `dynamic` → the winner;
 * `stable` → the pinned model; `off` (and `managed`, see `managedTarget`) →
 * null. A target equal to the anchor, unreachable, or outside `allowed`
 * yields null.
 */
export function routedModel(
  mode: string,
  provider: string,
  anchorId: string | null | undefined,
  winner: string | null | undefined,
  pinModel: string | null | undefined,
  allowed: Allowed = null,
): string | null {
  let target: string | null | undefined = null;
  if (mode === MODE_DYNAMIC) target = winner;
  else if (mode === MODE_STABLE) target = pinModel;
  return sendable(provider, anchorId, target, allowed);
}

/** `target` when it may replace the anchor, else null. */
export function sendable(
  provider: string,
  anchorId: string | null | undefined,
  target: string | null | undefined,
  allowed: Allowed = null,
): string | null {
  if (!target || target === anchorId) return null;
  if (!reachable(provider, target, anchorId ?? null)) return null;
  if (allowed !== null && allowed !== undefined && !new Set(allowed).has(target)) return null;
  return target;
}

export interface ManagedDecision {
  model: string | null;
  target: string | null;
  route_status: string | null;
}

/**
 * What a `managed` call sends (SPEC.md §2.9). `model` (the dashboard- or
 * `monitor set`-assigned id) is resolved like the anchor (diagnose §2.1).
 * `target` = the catalog id to send, or null = the anchor; `route_status` =
 * `unreachable_managed` when the managed model is missing, unknown,
 * unreachable from this client or outside `allowed` (the anchor is sent
 * instead), else null. A managed model equal to the anchor is sent as
 * written (target null, no status).
 */
export function managedTarget(
  provider: string,
  anchorId: string | null | undefined,
  model: unknown,
  registry: ModelRegistry,
  allowed: Allowed = null,
): ManagedDecision {
  let resolved: string | null = null;
  if (typeof model === 'string' && model.trim()) resolved = resolveAnchor(model.trim(), registry);
  if (resolved !== null && resolved === anchorId) return { model: resolved, target: null, route_status: null };
  const target = sendable(provider, anchorId, resolved, allowed);
  if (target === null) return { model: resolved, target: null, route_status: STATUS_UNREACHABLE_MANAGED };
  return { model: resolved, target, route_status: null };
}

/**
 * What a `stable` call sends when the server pins the process (SPEC.md
 * §2.5, API.md v1.8): `managedTarget` applied to `pin.model`, with
 * `route_status` = `unreachable_pin` when the pinned model cannot be sent
 * (the anchor is sent instead).
 */
export function serverPinTarget(
  provider: string,
  anchorId: string | null | undefined,
  model: unknown,
  registry: ModelRegistry,
  allowed: Allowed = null,
): ManagedDecision {
  const decision = managedTarget(provider, anchorId, model, registry, allowed);
  if (decision.route_status !== null) return { ...decision, route_status: STATUS_UNREACHABLE_PIN };
  return decision;
}
