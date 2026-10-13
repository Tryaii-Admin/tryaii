/** Stable-mode pins (SPEC.md §2.5). Clock-free: `now` is an input. Mirrors monitor/pins.py. */

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

/** Python's `sorted(counts.items(), key=(-count, id))` — code-unit order on ties. */
export function sortCounts(counts: Record<string, number>): Record<string, number> {
  const entries = Object.entries(counts).sort((a, b) =>
    b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0),
  );
  const out: Record<string, number> = {};
  for (const [k, v] of entries) out[k] = v;
  return out;
}

/**
 * `[window, counts]`: reachable winners of `process` in the last 24 h, or
 * the last 7 d when the 24 h window holds fewer than 20 of them. `counts` is
 * ordered by count desc, then model id asc.
 */
export function winnerCounts(events: Iterable<EventLike>, process: string, now: string): [string, Record<string, number>] {
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
  return [chosen[0], sortCounts(chosen[1])];
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
 * pins it via pinFromFirstCall).
 */
export function evaluatePin(events: Iterable<EventLike>, pin: unknown, now: string, process: string): PinDecision {
  if (pinValid(pin, now)) return { action: 'keep', pin, reason: 'pin valid' };
  const [window, counts] = winnerCounts(events, process, now);
  const first = Object.keys(counts)[0];
  if (first === undefined) return { action: 'none', pin: null, reason: 'no reachable winners in history' };
  return { action: 'pin', pin: makePin(first, now, window, counts), reason: `most frequent winner over ${window}` };
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
