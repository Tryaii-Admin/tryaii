/**
 * Stable-mode pins (SPEC.md §2.5). Clock-free: `now` is an input. Mirrors monitor/pins.py.
 *
 * The pinned model is "the winner of a sub-process": the most frequent
 * reachable per-call winner over the pin window, ties broken by the higher
 * catalog benchmark score, then the lower blended price, then the model id
 * (never by list or event order). The monitor backend computes the
 * dashboard's `pin_candidate` with the same rule over the uploaded events.
 */

import { halfEvenRound } from '../cachelint/util/halfEven.js';
import { formatTs, parseTs } from './events.js';

export const PINS_SCHEMA = 'tryaii.monitor.pins/1';
export const PIN_TTL_MS = 24 * 3600 * 1000;
export const WINDOW_SHORT: [string, number] = ['24h', 24 * 3600 * 1000];
export const WINDOW_LONG: [string, number] = ['7d', 7 * 24 * 3600 * 1000];
export const MIN_EVENTS_SHORT = 20;

export interface PinBasis {
  window: string;
  events: number;
  counts: Record<string, number>;
}

export interface Pin {
  model: string;
  pinned_at: string;
  expires_at: string;
  basis: PinBasis;
}

export interface PinsDoc {
  schema: string;
  processes: Record<string, Pin>;
}

export interface PinDecision {
  action: 'keep' | 'pin' | 'none';
  pin: Pin | null;
  reason: string;
}

type EventLike = Record<string, unknown>;

/** A model's tie-break inputs, defined as the server's `GET /v1/monitor/models` rows. */
export interface ModelFacts {
  quality: number | null;
  blended_price_usd_per_m: number | null;
}

export type ModelFactsMap = Record<string, ModelFacts>;

/** The parts of a catalog bundle `modelFacts` reads. */
export interface FactsBundle {
  models?: { models?: unknown[] } | null;
  normalizationRanges?: { benchmarks?: Record<string, unknown> } | null;
}

function isNum(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/**
 * The catalog-level benchmark score of a model (API.md §3.4 `quality`): the
 * mean over its benchmarks of `(score - lo) / (hi - lo)` clipped to 0..1, with
 * `lo`/`hi` from the bundle's `normalization_ranges.json`; 4 dp; null without
 * any ranged benchmark. Raw catalog scores, like the server.
 */
export function catalogQuality(scores: unknown, ranges: unknown): number | null {
  if (!isRecord(scores) || !isRecord(ranges)) return null;
  const parts: number[] = [];
  for (const [name, value] of Object.entries(scores)) {
    const rng = ranges[name];
    if (!isRecord(rng) || !isNum(value)) continue;
    const lo = rng.lo;
    const hi = rng.hi;
    if (!isNum(lo) || !isNum(hi) || hi <= lo) continue;
    parts.push(Math.min(1, Math.max(0, (value - lo) / (hi - lo))));
  }
  if (parts.length === 0) return null;
  let sum = 0;
  for (const part of parts) sum += part;
  return halfEvenRound(sum / parts.length, 4);
}

/** USD per 1M tokens: mean of the input and output per-1k prices x 1000, 6 dp; null when unpriced. */
export function blendedPricePerM(pricing: unknown): number | null {
  if (!isRecord(pricing)) return null;
  const i = pricing.input_per_1k;
  const o = pricing.output_per_1k;
  if (!isNum(i) || !isNum(o)) return null;
  return halfEvenRound(((i + o) / 2) * 1000, 6);
}

/** `{model id: {quality, blended_price_usd_per_m}}` for every model of a catalog bundle. */
export function modelFacts(bundle: FactsBundle | null | undefined): ModelFactsMap {
  const ranges = bundle?.normalizationRanges?.benchmarks ?? {};
  const models = bundle?.models?.models ?? [];
  const out: ModelFactsMap = {};
  for (const data of models) {
    if (!isRecord(data) || typeof data.model_id !== 'string') continue;
    out[data.model_id] = {
      quality: catalogQuality(data.benchmark_scores, ranges),
      blended_price_usd_per_m: blendedPricePerM(data.pricing),
    };
  }
  return out;
}

function facts(models: ModelFactsMap | null | undefined, id: string): [number | null, number | null] {
  const row = models && Object.prototype.hasOwnProperty.call(models, id) ? models[id] : undefined;
  const q = row?.quality;
  const p = row?.blended_price_usd_per_m;
  return [isNum(q) ? q : null, isNum(p) ? p : null];
}

function cmpStr(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * Winner order (SPEC.md §2.5): count desc, then benchmark score desc, then
 * blended price asc (unknown score / price after known ones), then model id asc.
 */
export function compareWinners(
  a: [string, number],
  b: [string, number],
  models?: ModelFactsMap | null,
): number {
  if (a[1] !== b[1]) return b[1] - a[1];
  const [qa, pa] = facts(models, a[0]);
  const [qb, pb] = facts(models, b[0]);
  if ((qa === null) !== (qb === null)) return qa === null ? 1 : -1;
  if (qa !== null && qb !== null && qa !== qb) return qb - qa;
  if ((pa === null) !== (pb === null)) return pa === null ? 1 : -1;
  if (pa !== null && pb !== null && pa !== pb) return pa - pb;
  return cmpStr(a[0], b[0]);
}

/**
 * What decided between the first two entries of an ordered `counts` with the
 * same count: `benchmark score`, `price` or `model id`; null when the first
 * entry has strictly more wins.
 */
export function tieBreak(counts: Record<string, number>, models?: ModelFactsMap | null): string | null {
  const items = Object.entries(counts);
  if (items.length < 2 || items[0][1] !== items[1][1]) return null;
  const [qa, pa] = facts(models, items[0][0]);
  const [qb, pb] = facts(models, items[1][0]);
  if (qa !== qb) return 'benchmark score';
  if (pa !== pb) return 'price';
  return 'model id';
}

export function emptyPins(): PinsDoc {
  return { schema: PINS_SCHEMA, processes: {} };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** True while `now` is before the pin's `expires_at`. */
export function pinValid(pin: unknown, now: string): pin is Pin {
  if (!isRecord(pin) || !pin.model) return false;
  try {
    return parseTs(now) < parseTs(pin.expires_at);
  } catch {
    return false;
  }
}

function inWindow(event: EventLike, process: string, start: number, end: number): boolean {
  if (event.process !== process) return false;
  let ts: number;
  try {
    ts = parseTs(event.ts);
  } catch {
    return false;
  }
  return start <= ts && ts <= end;
}

/** `counts` in winner order (compareWinners); ids compare in code-unit order, like Python. */
export function sortCounts(counts: Record<string, number>, models?: ModelFactsMap | null): Record<string, number> {
  const entries = Object.entries(counts).sort((a, b) => compareWinners(a, b, models));
  const out: Record<string, number> = {};
  for (const [k, v] of entries) out[k] = v;
  return out;
}

/**
 * `[window, counts]`: reachable winners of `process` in the last 24 h, or
 * the last 7 d when the 24 h window holds fewer than 20 of them. `counts` is
 * ordered by compareWinners (`models` = modelFacts; null = ids only).
 */
export function winnerCounts(
  events: Iterable<EventLike>,
  process: string,
  now: string,
  models?: ModelFactsMap | null,
): [string, Record<string, number>] {
  const end = parseTs(now);
  const list = Array.from(events);
  let chosen: [string, Record<string, number>] = [WINDOW_SHORT[0], {}];
  for (const [name, span] of [WINDOW_SHORT, WINDOW_LONG]) {
    const start = end - span;
    const counts: Record<string, number> = {};
    for (const event of list) {
      if (!inWindow(event, process, start, end)) continue;
      const winner = event.winner;
      if (!winner || typeof winner !== 'string' || !event.winner_reachable) continue;
      counts[winner] = (counts[winner] ?? 0) + 1;
    }
    chosen = [name, counts];
    if (Object.values(counts).reduce((a, b) => a + b, 0) >= MIN_EVENTS_SHORT) break;
  }
  return [chosen[0], sortCounts(chosen[1], models)];
}

export function makePin(model: string, now: string, window: string, counts: Record<string, number>): Pin {
  const start = parseTs(now);
  return {
    model,
    pinned_at: formatTs(start),
    expires_at: formatTs(start + PIN_TTL_MS),
    basis: { window, events: Object.values(counts).reduce((a, b) => a + b, 0), counts },
  };
}

/** The pin for a process with no history: this call's winner. */
export function pinFromFirstCall(winner: string, now: string): Pin {
  return makePin(winner, now, 'first_call', { [winner]: 1 });
}

/**
 * Decide the pin for `process` at `now`: `keep` (the current pin is still
 * valid), `pin` (a new pin built from history) or `none` (no usable history
 * — the caller routes this call in-path and, if the winner is reachable,
 * pins it via pinFromFirstCall). `models` (modelFacts) breaks ties between
 * equally frequent winners; without it ties fall back to the model id.
 */
export function evaluatePin(
  events: Iterable<EventLike>,
  pin: unknown,
  now: string,
  process: string,
  models?: ModelFactsMap | null,
): PinDecision {
  if (pinValid(pin, now)) return { action: 'keep', pin, reason: 'pin valid' };
  const [window, counts] = winnerCounts(events, process, now, models);
  const first = Object.keys(counts)[0];
  if (first === undefined) return { action: 'none', pin: null, reason: 'no reachable winners in history' };
  let reason = `most frequent winner over ${window}`;
  const decided = tieBreak(counts, models);
  if (decided !== null) reason += ` (tie broken by ${decided})`;
  return { action: 'pin', pin: makePin(first, now, window, counts), reason };
}

/** Python's `dict(sorted(d.items()))` — keys in code-unit order. */
export function sortedByKey<T>(obj: Record<string, T>): Record<string, T> {
  const out: Record<string, T> = {};
  for (const key of Object.keys(obj).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))) out[key] = obj[key];
  return out;
}

/** A new pins document with `process` pinned (or cleared when null). */
export function setPin(pins: Partial<PinsDoc> | null | undefined, process: string, pin: Pin | null): PinsDoc {
  const processes: Record<string, Pin> = { ...((pins?.processes as Record<string, Pin> | undefined) ?? {}) };
  if (pin === null) delete processes[process];
  else processes[process] = pin;
  return { schema: PINS_SCHEMA, processes: sortedByKey(processes) };
}
