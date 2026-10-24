/** Status aggregation for `tryaii monitor status` (SPEC.md §2.7). Mirrors monitor/status.py. */

import { halfEvenRound } from '../cachelint/util/halfEven.js';
import { resolveMode } from './config.js';
import { parseTs, roundCost } from './events.js';
import { pinValid } from './pins.js';

export const STATUS_SCHEMA = 'tryaii.monitor.status/1';
export const WINDOWS: Record<string, number> = {
  '24h': 24 * 3600 * 1000,
  '7d': 7 * 24 * 3600 * 1000,
  '30d': 30 * 24 * 3600 * 1000,
};

type EventLike = Record<string, unknown>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Python truthiness for the JSON-shaped values an event can hold. */
function truthy(value: unknown): boolean {
  if (value === null || value === undefined || value === false || value === 0 || value === '') return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value as object).length > 0;
  return true;
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

export function windowSeconds(since: string): number {
  if (!(since in WINDOWS)) throw new Error('since must be 24h, 7d or 30d');
  return WINDOWS[since] / 1000;
}

export function displayPath(path: unknown): string | null {
  return typeof path === 'string' ? path.replace(/\\/g, '/') : null;
}

/** A call whose model was replaced (`model_used != model_requested`). */
function swapped(event: EventLike): boolean {
  const used = event.model_used;
  const requested = event.model_requested;
  return truthy(used) && truthy(requested) && used !== requested;
}

/**
 * `{kind, etag, fetched_at}`: `local` without a usable remote.json, else
 * `remote` with its etag and fetch time.
 */
export function sourceInfo(remote: unknown): Record<string, unknown> {
  if (isRecord(remote)) {
    return {
      kind: 'remote',
      etag: typeof remote.etag === 'string' ? remote.etag : null,
      fetched_at: typeof remote.fetched_at === 'string' ? remote.fetched_at : null,
    };
  }
  return { kind: 'local', etag: null, fetched_at: null };
}

/**
 * `{key_app, key_app_mismatch}` when the cached remote.json names the key's
 * app (`app_name`, API.md v1.8, TS-308), else `{}`.
 */
export function keyAppInfo(app: string, remote: unknown): Record<string, unknown> {
  const name = isRecord(remote) ? remote.app_name : undefined;
  if (typeof name !== 'string' || !name) return {};
  return { key_app: name, key_app_mismatch: name !== app };
}

/**
 * `{state, events, last_ok, last_error}` from upload.json: `off` without a
 * cursor; `error` while `last_error` is set; else `ok`. `events` = the event
 * lines the cursor covers.
 */
export function uploadInfo(cursor: unknown, uploadedEvents = 0): Record<string, unknown> {
  if (!isRecord(cursor)) return { state: 'off', events: 0, last_ok: null, last_error: null };
  return {
    state: truthy(cursor.last_error) ? 'error' : 'ok',
    events: Math.trunc(uploadedEvents),
    last_ok: cursor.last_ok ?? null,
    last_error: cursor.last_error ?? null,
  };
}

/**
 * Per-process roll-up of `events` within `since` of `now`. Processes =
 * those with events in the window ∪ those named in `config.processes` ∪
 * `remote.processes` ∪ those with a pin, sorted by name. Costs are 6 dp; `est_cost_winner_usd` /
 * `savings_usd` only count events that carry BOTH cost estimates (and are
 * null when none do).
 */
export function aggregate(
  app: string,
  events: Iterable<EventLike>,
  pins: unknown,
  config: unknown,
  now: string,
  since = '24h',
  env: Record<string, string | undefined> | null = null,
  remote: unknown = null,
  uploadCursor: unknown = null,
  uploadedEvents = 0,
): Record<string, unknown> {
  const end = parseTs(now);
  if (!(since in WINDOWS)) throw new Error('since must be 24h, 7d or 30d');
  const start = end - WINDOWS[since];
  const pinsDoc = isRecord(pins) ? pins : {};
  const configDoc = isRecord(config) ? config : {};
  const byProcess = new Map<string, EventLike[]>();
  for (const event of events) {
    let ts: number;
    try {
      ts = parseTs(event.ts);
    } catch {
      continue;
    }
    if (ts < start || ts > end) continue;
    const name = (typeof event.process === 'string' && event.process) || 'default';
    const rows = byProcess.get(name);
    if (rows) rows.push(event);
    else byProcess.set(name, [event]);
  }
  const names = new Set<string>(byProcess.keys());
  const configProcesses = isRecord(configDoc.processes) ? configDoc.processes : {};
  const pinProcesses = isRecord(pinsDoc.processes) ? pinsDoc.processes : {};
  for (const name of Object.keys(configProcesses)) names.add(name);
  for (const name of Object.keys(pinProcesses)) names.add(name);
  if (isRecord(remote) && isRecord(remote.processes)) for (const name of Object.keys(remote.processes)) names.add(name);

  const processes: Record<string, unknown>[] = [];
  let totalCalls = 0;
  let totalErrors = 0;
  let totalCost = 0;
  let totalPaired = 0;
  let totalWinner = 0;
  let anyPaired = false;
  let totalSwapped = 0;
  let totalAnchor = 0;
  let totalRealised = 0;
  let anyAnchored = false;
  const totalCache: Record<string, number | null> = { cached_tokens: null, cache_write_tokens: null };
  for (const name of Array.from(names).sort(cmp)) {
    const rows = byProcess.get(name) ?? [];
    const mode = resolveMode(name, { env, remote, config: configDoc });
    const pin = pinProcesses[name];
    let pinned: Record<string, unknown> | null = null;
    if (pinValid(pin, now)) pinned = { model: pin.model, expires_at: pin.expires_at };
    const calls = rows.length;
    const errors = rows.filter((e) => truthy(e.error)).length;
    const tokens: Record<string, number> = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
    const cache: Record<string, number | null> = { cached_tokens: null, cache_write_tokens: null };
    for (const e of rows) {
      const usage = isRecord(e.usage) ? e.usage : {};
      for (const key of Object.keys(tokens)) {
        const value = usage[key];
        if (typeof value === 'number') tokens[key] += Math.trunc(value);
      }
      for (const key of Object.keys(cache)) {
        const value = usage[key];
        if (typeof value === 'number') cache[key] = (cache[key] ?? 0) + Math.trunc(value);
      }
    }
    const costOf = (e: EventLike, key: string): number | null =>
      typeof e[key] === 'number' ? (e[key] as number) : null;
    let cost = 0;
    for (const e of rows) {
      const c = costOf(e, 'est_cost_usd');
      if (c !== null) cost += c;
    }
    const paired = rows.filter((e) => costOf(e, 'est_cost_usd') !== null && costOf(e, 'est_cost_winner_usd') !== null);
    let costPaired = 0;
    let costWinner = 0;
    for (const e of paired) {
      costPaired += costOf(e, 'est_cost_usd') as number;
      costWinner += costOf(e, 'est_cost_winner_usd') as number;
    }
    const anchored = rows.filter((e) => costOf(e, 'est_cost_anchor_usd') !== null);
    let costAnchor = 0;
    for (const e of anchored) costAnchor += costOf(e, 'est_cost_anchor_usd') as number;
    const swappedRows = rows.filter(swapped);
    let realised = 0;
    for (const e of swappedRows) {
      const a = costOf(e, 'est_cost_anchor_usd');
      const c = costOf(e, 'est_cost_usd');
      if (a !== null && c !== null) realised += Math.max(0, a - c);
    }
    const counts: Record<string, number> = {};
    for (const e of rows) {
      const winner = e.winner;
      if (typeof winner === 'string' && winner) counts[winner] = (counts[winner] ?? 0) + 1;
    }
    let top: Record<string, unknown> | null = null;
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1] || cmp(a[0], b[0]));
    if (entries.length) {
      const [model, n] = entries[0];
      const total = entries.reduce((acc, [, v]) => acc + v, 0);
      top = { model, share: halfEvenRound(n / total, 4) };
    }
    const fileSet = new Set<string>();
    for (const e of rows) {
      const file = displayPath(isRecord(e.call_site) ? e.call_site.file : null);
      if (file !== null) fileSet.add(file);
    }
    const files = Array.from(fileSet).sort(cmp);
    processes.push({
      process: name,
      mode: mode.mode,
      mode_source: mode.source,
      managed_model: mode.model,
      pinned,
      calls,
      errors,
      prompt_tokens: tokens.prompt_tokens,
      completion_tokens: tokens.completion_tokens,
      total_tokens: tokens.total_tokens,
      cached_tokens: cache.cached_tokens,
      cache_write_tokens: cache.cache_write_tokens,
      est_cost_usd: roundCost(cost),
      est_cost_winner_usd: paired.length ? roundCost(costWinner) : null,
      savings_usd: paired.length ? roundCost(costPaired - costWinner) : null,
      swapped_calls: swappedRows.length,
      est_cost_anchor_usd: anchored.length ? roundCost(costAnchor) : null,
      realised_savings_usd: anchored.length ? roundCost(realised) : null,
      top_winner: top,
      files,
    });
    totalCalls += calls;
    totalErrors += errors;
    for (const key of Object.keys(totalCache)) {
      const value = cache[key];
      if (value !== null) totalCache[key] = (totalCache[key] ?? 0) + value;
    }
    totalCost += cost;
    totalPaired += costPaired;
    totalWinner += costWinner;
    anyPaired = anyPaired || paired.length > 0;
    totalSwapped += swappedRows.length;
    totalAnchor += costAnchor;
    totalRealised += realised;
    anyAnchored = anyAnchored || anchored.length > 0;
  }
  return {
    schema: STATUS_SCHEMA,
    app,
    now,
    since,
    source: sourceInfo(remote),
    ...keyAppInfo(app, remote),
    upload: uploadInfo(uploadCursor, uploadedEvents),
    processes,
    totals: {
      calls: totalCalls,
      errors: totalErrors,
      cached_tokens: totalCache.cached_tokens,
      cache_write_tokens: totalCache.cache_write_tokens,
      est_cost_usd: roundCost(totalCost),
      est_cost_winner_usd: anyPaired ? roundCost(totalWinner) : null,
      savings_usd: anyPaired ? roundCost(totalPaired - totalWinner) : null,
      swapped_calls: totalSwapped,
      est_cost_anchor_usd: anyAnchored ? roundCost(totalAnchor) : null,
      realised_savings_usd: anyAnchored ? roundCost(totalRealised) : null,
    },
  };
}
