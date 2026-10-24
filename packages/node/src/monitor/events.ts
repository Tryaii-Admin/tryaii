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
import { cachedTokensFromUsage, type CacheBlock } from './cache.js';

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

/** Cache lifetimes a write or a request policy can name (SPEC.md §3.1b). */
export const CACHE_TTLS = ['5m', '1h', '30m', '24h', 'explicit'] as const;
export type CacheTtl = (typeof CACHE_TTLS)[number];
/** Provider API shapes `normalizeCacheUsage` knows (SPEC.md §3.1b). */
export const CACHE_PROVIDERS = [
  'openai',
  'openai_responses',
  'openrouter',
  'azure',
  'anthropic',
  'bedrock',
  'gemini',
] as const;
export type CacheProvider = (typeof CACHE_PROVIDERS)[number];
/** The event's `usage` keys, in contract order (SPEC.md §3.1, §3.1b). */
export const USAGE_TOKEN_KEYS = ['prompt_tokens', 'completion_tokens', 'total_tokens'] as const;
export const USAGE_CACHE_KEYS = ['cached_tokens', 'cache_write_tokens', 'cache_writes'] as const;
/** API.md v1.7: the reasoning share of `completion_tokens` (SPEC.md §3.1). */
export const USAGE_REASONING_KEYS = ['reasoning_tokens'] as const;

export interface CacheWrite {
  tokens: number;
  ttl: CacheTtl;
}

export interface CacheUsage {
  cached_tokens: number | null;
  cache_write_tokens: number | null;
  cache_writes: CacheWrite[] | null;
}

export interface Usage extends CacheUsage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  /** API.md v1.7: part of `completion_tokens`, never added on top; null = not reported. */
  reasoning_tokens: number | null;
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

/** `obj[key]` for an object (attributes and dict keys alike); undefined when absent. */
function get(obj: unknown, key: string): unknown {
  if (obj === null || obj === undefined || typeof obj !== 'object') return undefined;
  return (obj as Record<string, unknown>)[key];
}

/**
 * A token count: a non-negative number truncated to an integer; booleans,
 * negatives, NaN and non-numbers count as absent (SPEC.md §3.1b rule 5).
 */
function count(value: unknown): number | null {
  if (!isNumber(value) || !Number.isFinite(value) || value < 0) return null;
  return Math.trunc(value);
}

/** A lifetime of CACHE_TTLS (lower-cased, trimmed), else null. */
function ttlOf(value: unknown): CacheTtl | null {
  if (typeof value !== 'string') return null;
  const text = value.trim().toLowerCase();
  return (CACHE_TTLS as readonly string[]).includes(text) ? (text as CacheTtl) : null;
}

/**
 * `[{tokens, ttl}, ...]` from a breakdown list; entries with zero tokens, a
 * bad count or an unknown ttl are dropped; null when `writes` is not a list
 * (no breakdown).
 */
export function normalizeCacheWrites(writes: unknown): CacheWrite[] | null {
  if (!Array.isArray(writes)) return null;
  const out: CacheWrite[] = [];
  for (const entry of writes) {
    const tokens = count(get(entry, 'tokens'));
    const ttl = ttlOf(get(entry, 'ttl'));
    if (tokens && ttl) out.push({ tokens, ttl });
  }
  return out;
}

function cacheFields(read: unknown, write: unknown, writes: unknown): CacheUsage {
  const cached = count(read);
  let total = count(write);
  const breakdown = normalizeCacheWrites(writes);
  if (total === null && breakdown !== null) total = breakdown.reduce((acc, w) => acc + w.tokens, 0);
  return { cached_tokens: cached, cache_write_tokens: total, cache_writes: breakdown };
}

function openaiCache(raw: unknown, detailsKey: string, topLevelRead = false): CacheUsage {
  const details = get(raw, detailsKey);
  let read = get(details, 'cached_tokens');
  if ((read === null || read === undefined) && topLevelRead) read = get(raw, 'cached_tokens');
  let write = get(details, 'cache_write_tokens');
  if (write === null || write === undefined) write = get(raw, 'cache_write_tokens');
  return cacheFields(read, write, null);
}

function anthropicCache(raw: unknown): CacheUsage {
  const creation = get(raw, 'cache_creation');
  let writes: unknown = null;
  if (creation !== null && creation !== undefined) {
    writes = [
      { tokens: get(creation, 'ephemeral_5m_input_tokens'), ttl: '5m' },
      { tokens: get(creation, 'ephemeral_1h_input_tokens'), ttl: '1h' },
    ];
  }
  return cacheFields(get(raw, 'cache_read_input_tokens'), get(raw, 'cache_creation_input_tokens'), writes);
}

function bedrockCache(raw: unknown): CacheUsage {
  const details = get(raw, 'cacheDetails');
  let writes: unknown = null;
  if (Array.isArray(details)) {
    writes = details.map((d) => ({ tokens: get(d, 'inputTokens'), ttl: get(d, 'ttl') }));
  }
  return cacheFields(get(raw, 'cacheReadInputTokens'), get(raw, 'cacheWriteInputTokens'), writes);
}

function geminiCache(raw: unknown): CacheUsage {
  let meta = get(raw, 'usageMetadata');
  if (meta === null || meta === undefined) meta = get(raw, 'usage_metadata');
  if (meta === null || meta === undefined) meta = raw;
  let read = get(meta, 'cachedContentTokenCount');
  if (read === null || read === undefined) read = get(meta, 'cached_content_token_count');
  return cacheFields(read, null, null);
}

/**
 * Map a provider's raw usage object to the event's cache columns (SPEC.md
 * §3.1b): `{cached_tokens, cache_write_tokens, cache_writes}`, every
 * key present, null for what the provider does not report. `provider` is
 * the API shape (CACHE_PROVIDERS); an unknown shape or no usage answers all
 * null. Pure: TS-229 calls it from the wrappers, the `cache_usage` fixtures
 * freeze it. Mirrors events.normalize_cache_usage.
 */
export function normalizeCacheUsage(provider: unknown, rawUsage: unknown): CacheUsage {
  const empty: CacheUsage = { cached_tokens: null, cache_write_tokens: null, cache_writes: null };
  if (rawUsage === null || rawUsage === undefined || typeof provider !== 'string') return empty;
  const shape = provider.trim().toLowerCase();
  if (shape === 'openai' || shape === 'azure') return openaiCache(rawUsage, 'prompt_tokens_details');
  if (shape === 'openrouter') return openaiCache(rawUsage, 'prompt_tokens_details', true);
  if (shape === 'openai_responses') return openaiCache(rawUsage, 'input_tokens_details');
  if (shape === 'anthropic') return anthropicCache(rawUsage);
  if (shape === 'bedrock') return bedrockCache(rawUsage);
  if (shape === 'gemini') return geminiCache(rawUsage);
  return empty;
}

/**
 * The event's `usage` in contract key order from an event-shaped object:
 * the three token counts plus the §3.1b cache fields, each null when absent
 * or malformed; null when nothing is numeric. Mirrors events.normalize_usage.
 */
export function normalizeUsage(usage: unknown): Usage | null {
  if (usage === null || usage === undefined) return null;
  const cache = cacheFields(get(usage, 'cached_tokens'), get(usage, 'cache_write_tokens'), get(usage, 'cache_writes'));
  const out: Usage = {
    prompt_tokens: count(get(usage, 'prompt_tokens')),
    completion_tokens: count(get(usage, 'completion_tokens')),
    total_tokens: count(get(usage, 'total_tokens')),
    cached_tokens: cache.cached_tokens,
    cache_write_tokens: cache.cache_write_tokens,
    cache_writes: cache.cache_writes,
    reasoning_tokens: count(get(usage, 'reasoning_tokens')),
  };
  if (Object.values(out).every((v) => v === null)) return null;
  return out;
}

/** `cache_ttl_policy`: one of CACHE_TTLS or null (SPEC.md §3.1b). */
export function normalizeCacheTtlPolicy(value: unknown): CacheTtl | null {
  return ttlOf(value);
}

/**
 * Normalise a provider `usage` (object) to the event shape. `cached_tokens`
 * is read from the raw usage (API.md v1.4: `prompt_tokens_details.cached_tokens`
 * or `cache_read_input_tokens`) unless the input already carries it under its
 * event name; the write columns pass through when the input carries them
 * under their event names and are null otherwise (the wrappers call
 * `usageFromProvider`, which adds them from the raw usage). `reasoning_tokens`
 * (API.md v1.7) is read from `completion_tokens_details.reasoning_tokens`
 * unless the input carries it under its event name; null when not reported.
 */
export function usageFromResponse(usage: unknown): Usage | null {
  const out = normalizeUsage(usage);
  // API.md v1.4: the provider's cached prompt tokens (null = not reported).
  if (out !== null && out.cached_tokens === null) out.cached_tokens = cachedTokensFromUsage(usage);
  if (out !== null && out.reasoning_tokens === null) out.reasoning_tokens = reasoningTokensFromUsage(usage);
  return out;
}

/**
 * The event's `usage` from a provider's raw usage of API shape `shape`
 * (CACHE_PROVIDERS, SPEC.md §3.1b): the token counts as `usageFromResponse`
 * reads them, then every cache column `normalizeCacheUsage` finds in the raw
 * usage. Only what the provider reported is filled; the rest stays null.
 * `shape` may be a list of shapes, tried in order for the columns still null
 * (an OpenAI-compatible host that answers with Anthropic-shaped usage).
 * Mirrors events.usage_from_provider.
 */
export function usageFromProvider(shape: string | readonly string[], usage: unknown): Usage | null {
  const out = usageFromResponse(usage);
  if (out === null) return null;
  for (const one of typeof shape === 'string' ? [shape] : shape) {
    const cache = normalizeCacheUsage(one, usage);
    if (out.cached_tokens === null) out.cached_tokens = cache.cached_tokens;
    if (out.cache_write_tokens === null) out.cache_write_tokens = cache.cache_write_tokens;
    if (out.cache_writes === null) out.cache_writes = cache.cache_writes;
  }
  return out;
}

/**
 * Request lifetimes longest first: a request naming several asked for the
 * longest (Anthropic orders 1h breakpoints before 5m ones).
 */
const TTL_RANK: readonly CacheTtl[] = ['24h', '1h', '30m', '5m'];
/**
 * OpenAI `prompt_cache_retention` values that name a lifetime (`in_memory`
 * is best-effort, no promised lifetime -> null).
 */
const RETENTION_TTLS: Record<string, CacheTtl> = { '24h': '24h' };

/**
 * Explicit `cache_control.ttl` values on messages / content parts / tool
 * schemas (OpenRouter and Anthropic-compatible hosts).
 */
function cacheControlTtls(items: unknown, found: Set<CacheTtl>): void {
  if (!Array.isArray(items)) return;
  for (const item of items) {
    const ttl = ttlOf(get(get(item, 'cache_control'), 'ttl'));
    if (ttl !== null) found.add(ttl);
    cacheControlTtls(get(item, 'content'), found);
  }
}

/**
 * `cache_ttl_policy` (SPEC.md §3.1b) from an OpenAI-shaped request:
 * `prompt_cache_retention` (also inside `extra_body`), else the longest
 * explicit `cache_control.ttl` on its messages or tools. Null when the
 * request names no lifetime - a default is never assumed. Mirrors
 * events.cache_ttl_policy_from_request.
 */
export function cacheTtlPolicyFromRequest(params: unknown): CacheTtl | null {
  if (params === null || typeof params !== 'object' || Array.isArray(params)) return null;
  for (const source of [params, get(params, 'extra_body')]) {
    const retention = get(source, 'prompt_cache_retention');
    if (typeof retention === 'string') return RETENTION_TTLS[retention.trim().toLowerCase()] ?? null;
  }
  const found = new Set<CacheTtl>();
  cacheControlTtls(get(params, 'messages'), found);
  cacheControlTtls(get(params, 'tools'), found);
  return TTL_RANK.find((t) => found.has(t)) ?? null;
}

/**
 * `usage.completion_tokens_details.reasoning_tokens` (OpenAI-compatible chat
 * completions, streamed or not); null when the provider does not report it
 * (API.md v1.7).
 */
export function reasoningTokensFromUsage(usage: unknown): number | null {
  return count(get(get(usage, 'completion_tokens_details'), 'reasoning_tokens'));
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
  /** Event-shaped usage; the §3.1b cache keys may be absent (they are emitted as null). */
  usage: Partial<Usage> | null;
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
  /** API.md v1.4: the last key, present only when the call ran the runtime cache lint. */
  cache?: CacheBlock | null;
  /** The cache lifetime the request asked for (SPEC.md §3.1b); unknown values record as null. */
  cache_ttl_policy?: string | null;
  /** The active OpenTelemetry span at call time (SPEC.md "Langfuse export"); emitted only when set. */
  otel?: { trace_id: string; span_id: string; origin?: string } | null;
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
  const event: Record<string, unknown> = {
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
    usage: normalizeUsage(f.usage),
    cache_ttl_policy: normalizeCacheTtlPolicy(f.cache_ttl_policy),
    est_cost_usd: roundCost(f.est_cost_usd),
    est_cost_winner_usd: roundCost(f.est_cost_winner_usd),
    est_cost_anchor_usd: roundCost(f.est_cost_anchor_usd),
    latency_ms: f.latency_ms === null || f.latency_ms === undefined ? null : Math.trunc(f.latency_ms),
    stream: Boolean(f.stream),
    error: f.error,
    prompt_hash: f.prompt_hash,
    vector: Boolean(f.vector),
  };
  if (f.otel !== null && f.otel !== undefined) {
    const otel: Record<string, unknown> = { trace_id: f.otel.trace_id, span_id: f.otel.span_id };
    if (f.otel.origin !== undefined && f.otel.origin !== null) otel.origin = f.otel.origin;
    event.otel = otel;
  }
  if (f.cache !== null && f.cache !== undefined) event.cache = f.cache;
  return event;
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
