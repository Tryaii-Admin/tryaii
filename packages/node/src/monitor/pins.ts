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
  pinned_at: string | null;
  /** null: an owner's pin from the server (API.md v1.8), held until changed. */
  expires_at: string | null;
  /** The remote `pin_version` the pin belongs to (v1.8; missing = 0). */
  version?: number;
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

/**
 * True while `now` is before the pin's `expires_at`; a pin whose
 * `expires_at` is null (an owner's pin from the server, API.md v1.8) holds
 * until the server changes it.
 */
export function pinValid(pin: unknown, now: string): pin is Pin {
  if (!isRecord(pin) || !pin.model) return false;
  if ('expires_at' in pin && pin.expires_at === null) return true;
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

// -- server-owned pins (API.md v1.8, TS-307; SPEC.md §2.5) ------------------

export const SERVER_WINDOW = 'server';
const STABLE = 'stable';

function nonNegInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
}

/** A local pin's `version`; missing or invalid (a pins.json from before v1.8) counts as 0. */
export function pinVersionOf(pin: unknown): number {
  if (!isRecord(pin)) return 0;
  return nonNegInt(pin.version) ?? 0;
}

/** `pin` with `version` set, placed after `expires_at`. */
export function withVersion(pin: Pin, version: number): Pin {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(pin)) {
    if (key === 'version') continue;
    out[key] = value;
    if (key === 'expires_at') out.version = version;
  }
  if (!('version' in out)) out.version = version;
  return out as unknown as Pin;
}

function remoteEntry(remote: unknown, process: string): Record<string, unknown> | null {
  if (!isRecord(remote)) return null;
  const processes = remote.processes;
  if (!isRecord(processes)) return null;
  const entry = Object.prototype.hasOwnProperty.call(processes, process) ? processes[process] : undefined;
  return isRecord(entry) ? entry : null;
}

/**
 * The process's `pin_version` from remote.json; null (= do not compare)
 * without a remote config, a process entry or the key (v1.7).
 */
export function remotePinVersion(remote: unknown, process: string): number | null {
  const entry = remoteEntry(remote, process);
  return entry === null ? null : nonNegInt(entry.pin_version);
}

/**
 * The server's pin for `process` in the local pins.json form, or null: only
 * when the remote entry's mode is `stable` and its `pin` names a model.
 */
export function serverPin(remote: unknown, process: string): Pin | null {
  const entry = remoteEntry(remote, process);
  if (entry === null || entry.mode !== STABLE) return null;
  const pin = entry.pin;
  if (!isRecord(pin)) return null;
  const model = pin.model;
  if (typeof model !== 'string' || !model.trim()) return null;
  const version = nonNegInt(pin.version) ?? nonNegInt(entry.pin_version) ?? 0;
  return {
    model: model.trim(),
    pinned_at: typeof pin.pinned_at === 'string' ? pin.pinned_at : null,
    expires_at: typeof pin.expires_at === 'string' ? pin.expires_at : null,
    version,
    basis: { window: SERVER_WINDOW, events: 0, counts: {} },
  };
}

export interface PinReconciliation {
  action: 'server' | 'keep' | 'evaluate' | 'remove' | 'none';
  pin: Pin | null;
  version: number | null;
  write: boolean;
  pins: PinsDoc;
  reason: string;
}

/** Python's `==` on JSON values (key order ignored). */
function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => jsonEqual(v, b[i]));
  }
  if (!isRecord(a) || !isRecord(b)) return false;
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && jsonEqual(a[k], b[k]));
}

function reconciled(
  action: PinReconciliation['action'],
  pin: Pin | null,
  version: number | null,
  pins: PinsDoc,
  write: boolean,
  reason: string,
): PinReconciliation {
  return { action, pin, version, write, pins, reason };
}

/**
 * Reconcile the local pin of `process` with the dashboard (SPEC.md §2.5,
 * TS-307) before a call. Mirrors pins.py `reconcile_pin`:
 * - `mode` not `stable`: `remove` when the remote config set that mode
 *   (`modeSource === 'remote'`) and an entry exists, else `none`;
 * - remote mode `stable` with a server `pin` → `server` (the entry is
 *   replaced when it differs);
 * - else a local pin whose `version` differs from the remote `pin_version`
 *   is dropped (no `pin_version` = do not compare); a valid pin → `keep`;
 *   otherwise `evaluate` (new pins store `version`).
 */
export function reconcilePin(
  remote: unknown,
  pins: Partial<PinsDoc> | null | undefined,
  process: string,
  now: string,
  mode: string,
  modeSource: string | null | undefined,
): PinReconciliation {
  const processes = isRecord(pins?.processes) ? { ...(pins?.processes as Record<string, Pin>) } : {};
  let doc: PinsDoc = { schema: PINS_SCHEMA, processes };
  const has = Object.prototype.hasOwnProperty.call(processes, process);
  let current: Pin | null = has ? (processes[process] ?? null) : null;
  if (mode !== STABLE) {
    if (modeSource === 'remote' && has) {
      return reconciled('remove', null, null, setPin(doc, process, null), true, `remote mode is ${mode}`);
    }
    return reconciled('none', null, null, doc, false, `mode is ${mode}`);
  }
  const served = serverPin(remote, process);
  if (served !== null) {
    const source = (remoteEntry(remote, process)?.pin as Record<string, unknown> | undefined)?.source;
    const reason = typeof source === 'string' ? `server pin (${source})` : 'server pin';
    if (jsonEqual(current, served)) return reconciled('server', served, served.version ?? 0, doc, false, reason);
    return reconciled('server', served, served.version ?? 0, setPin(doc, process, served), true, reason);
  }
  const wanted = remotePinVersion(remote, process);
  let write = false;
  let reason: string | null = null;
  if (current !== null && wanted !== null && pinVersionOf(current) !== wanted) {
    reason = `local pin version ${pinVersionOf(current)} != remote pin_version ${wanted}`;
    doc = setPin(doc, process, null);
    current = null;
    write = true;
  }
  if (pinValid(current, now)) return reconciled('keep', current, pinVersionOf(current), doc, false, 'pin valid');
  return reconciled(
    'evaluate',
    current,
    wanted ?? 0,
    doc,
    write,
    reason ?? (current !== null ? 'pin expired' : 'no pin'),
  );
}
