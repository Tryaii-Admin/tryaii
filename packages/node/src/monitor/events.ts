/**
 * Event records (SPEC.md §1.2) — key order, rounding and cost estimates.
 * Mirrors monitor/events.py.
 *
 * The integral-float rule (SPEC §1.1) needs no normaliser here: JSON.stringify
 * already emits integral doubles without a fraction, exactly like Python's
 * normalize_numbers + json.dumps.
 */

import { randomUUID } from 'node:crypto';

import { estimateGenerationCost } from '../budget.js';
import { halfEvenRound } from '../cachelint/util/halfEven.js';
import type { ModelRegistry } from '../registry/models.js';

export const EVENT_SCHEMA = 'tryaii.monitor.event/1';
export const VECTOR_SCHEMA = 'tryaii.monitor.vector/1';
export const ROUTE_STATUSES = [
  'ok',
  'unavailable',
  'timeout',
  'disabled',
  'queue_full',
  'skipped',
  'unreachable_managed',
] as const;
export type RouteStatus = (typeof ROUTE_STATUSES)[number];
export const TAXONOMY_VERSION_KEY = 'taxonomy_version';

/** A fresh uuid4 string (the event's idempotency key upstream). */
export function newEventId(): string {
  return randomUUID();
}

/**
 * The benchmark-taxonomy version of a catalog bundle (SPEC.md §3.1):
 * `benchmarks.json`'s top-level `taxonomy_version`, else the manifest's, as
 * a non-empty string; null when neither carries one (true of every bundle
 * released so far).
 */
export function taxonomyVersion(bundle: { benchmarks?: unknown; manifest?: unknown } | null | undefined): string | null {
  if (!bundle) return null;
  for (const doc of [bundle.benchmarks, bundle.manifest]) {
    if (doc !== null && typeof doc === 'object' && !Array.isArray(doc)) {
      const value = (doc as Record<string, unknown>)[TAXONOMY_VERSION_KEY];
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
  }
  return null;
}
export const COST_DECIMALS = 6;

const TS_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$/;

export interface Usage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
}

export interface Classification {
  category: string | null;
  subcategory: string | null;
  confidence: number | null;
}

export interface CallSiteRecord {
  file: string | null;
  line: number | null;
  function: string | null;
}

export interface ErrorRecord {
  status: number | null;
  class: string;
}

/** `YYYY-MM-DDTHH:MM:SS.mmmZ` from epoch milliseconds or a Date (UTC). */
export function formatTs(value: number | Date): string {
  const ms = value instanceof Date ? value.getTime() : value;
  return new Date(ms).toISOString();
}

/**
 * Inverse of formatTs → epoch milliseconds; also accepts whole-second
 * `...SSZ` and a `+00:00` offset. Throws on anything else (the Python
 * reference raises ValueError).
 */
export function parseTs(text: unknown): number {
  if (typeof text !== 'string') throw new Error('timestamp must be a string');
  let value = text.trim();
  if (value.endsWith('Z')) value = value.slice(0, -1);
  else if (value.endsWith('+00:00')) value = value.slice(0, -6);
  const m = TS_RE.exec(value);
  if (m === null) throw new Error(`bad timestamp: ${JSON.stringify(text)}`);
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number);
  const fraction = m[7] ?? '';
  const micros = fraction ? Number(fraction.padEnd(6, '0')) : 0;
  if (month < 1 || month > 12 || day < 1 || hour > 23 || minute > 59 || second > 59) {
    throw new Error(`bad timestamp: ${JSON.stringify(text)}`);
  }
  const ms = Date.UTC(year, month - 1, day, hour, minute, second);
  // strptime rejects impossible days (Feb 30); Date.UTC rolls them over.
  if (new Date(ms).getUTCDate() !== day) throw new Error(`bad timestamp: ${JSON.stringify(text)}`);
  return ms + Math.floor(micros / 1000);
}

export function roundCost(value: number | null | undefined): number | null {
  return value === null || value === undefined ? null : halfEvenRound(value, COST_DECIMALS);
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number';
}

/**
 * USD for one call at catalog prices, 6 dp; null when the model is not in
 * the catalog, unpriced, or the usage has no token counts.
 */
export function estimateCost(
  registry: ModelRegistry,
  modelId: string | null | undefined,
  usage: Record<string, unknown> | null | undefined,
): number | null {
  if (!modelId || !usage || typeof usage !== 'object' || Array.isArray(usage)) return null;
  const prompt = usage.prompt_tokens;
  const completion = usage.completion_tokens;
  if (!isNumber(prompt) || !isNumber(completion)) return null;
  const model = registry.getModel(modelId);
  if (model === undefined) return null;
  return roundCost(estimateGenerationCost(model, Math.trunc(prompt), Math.trunc(completion)));
}

/** Normalise a provider `usage` (object) to the event shape. */
export function usageFromResponse(usage: unknown): Usage | null {
  if (usage === null || usage === undefined) return null;
  const get = (key: string): unknown =>
    typeof usage === 'object' ? (usage as Record<string, unknown>)[key] : undefined;
  const out: Usage = { prompt_tokens: null, completion_tokens: null, total_tokens: null };
  for (const key of ['prompt_tokens', 'completion_tokens', 'total_tokens'] as const) {
    const value = get(key);
    out[key] = isNumber(value) ? Math.trunc(value) : null;
  }
  if (out.prompt_tokens === null && out.completion_tokens === null && out.total_tokens === null) {
    return null;
  }
  return out;
}

/**
 * `{category, subcategory, confidence}` from a daemon classification
 * (`broadCategory`...) or a ClassificationResult-like object.
 */
export function classificationFromWire(cls: unknown): Classification | null {
  if (cls === null || cls === undefined) return null;
  const c = cls as Record<string, unknown>;
  const category = c.broadCategory ?? c.broad_category;
  const sub = c.subcategory;
  const conf = c.confidence;
  return {
    category: typeof category === 'string' && category ? category : null,
    subcategory: typeof sub === 'string' && sub ? sub : null,
    confidence: isNumber(conf) ? halfEvenRound(conf, 4) : null,
  };
}

export interface EventFields {
  ts: string;
  app: string;
  process: string;
  call_site: Partial<CallSiteRecord> | null | undefined;
  provider: string;
  model_requested: string | null;
  model_resolved: string | null;
  model_used: string | null;
  mode: string;
  winner: string | null;
  winner_reachable: boolean;
  route_status: string;
  classification: Classification | null;
  usage: Usage | null;
  est_cost_usd: number | null;
  est_cost_winner_usd: number | null;
  latency_ms: number | null;
  stream: boolean;
  error: ErrorRecord | null;
  prompt_hash: string;
  vector: boolean;
  /** Defaults to a fresh uuid4. */
  event_id?: string | null;
  taxonomy_version?: string | null;
  /** The usage priced at the anchor (equal to est_cost_usd when nothing was swapped). */
  est_cost_anchor_usd?: number | null;
}

/** One event line in contract key order (SPEC.md §3.1). */
export function buildEvent(f: EventFields): Record<string, unknown> {
  let site: CallSiteRecord | null = null;
  if (f.call_site) {
    site = {
      file: f.call_site.file ?? null,
      line: f.call_site.line ?? null,
      function: f.call_site.function ?? null,
    };
  }
  return {
    schema: EVENT_SCHEMA,
    event_id: f.event_id || newEventId(),
    ts: f.ts,
    app: f.app,
    process: f.process,
    call_site: site,
    provider: f.provider,
    model_requested: f.model_requested,
    model_resolved: f.model_resolved,
    model_used: f.model_used,
    mode: f.mode,
    winner: f.winner,
    winner_reachable: Boolean(f.winner_reachable),
    route_status: f.route_status,
    classification: f.classification,
    taxonomy_version: f.taxonomy_version ?? null,
    usage: f.usage,
    est_cost_usd: roundCost(f.est_cost_usd),
    est_cost_winner_usd: roundCost(f.est_cost_winner_usd),
    est_cost_anchor_usd: roundCost(f.est_cost_anchor_usd),
    latency_ms: f.latency_ms === null || f.latency_ms === undefined ? null : Math.trunc(f.latency_ms),
    stream: Boolean(f.stream),
    error: f.error,
    prompt_hash: f.prompt_hash,
    vector: Boolean(f.vector),
  };
}

/** One vectors line: the prompt's embedding, 4 dp (SPEC.md §1.3). */
export function buildVector(f: {
  prompt_hash: string;
  ts: string;
  process: string;
  model: string;
  vector: Iterable<number>;
}): Record<string, unknown> {
  const values = Array.from(f.vector, (x) => halfEvenRound(Number(x), 4));
  return {
    schema: VECTOR_SCHEMA,
    prompt_hash: f.prompt_hash,
    ts: f.ts,
    process: f.process,
    model: f.model,
    dim: values.length,
    v: values,
  };
}
