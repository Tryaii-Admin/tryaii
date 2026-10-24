/**
 * Langfuse export of monitor events (SPEC.md "Langfuse export", Jira TS-246).
 * Mirrors monitor/langfuse.py.
 *
 * A second reader of the local event store, next to the uploader, with its
 * own cursor (`langfuse.json`). It turns each event into one OTLP span, posts
 * the spans as OTLP/HTTP JSON to `<host>/api/public/otel/v1/traces` and then
 * TryAii's recommendation as three Langfuse scores to
 * `<host>/api/public/scores`, with Basic auth from the customer's own Langfuse
 * keys. The scores are sent from the SDK only, so TryAii never holds a
 * Langfuse secret key.
 *
 * Off unless `TRYAII_LANGFUSE` is `on` (`1`, `true`) and both `TRYAII_API_KEY`
 * (an app key) and `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set.
 * Metadata only: no prompt or response text (the store holds none) and never
 * the prompt hash or the vectors. Its own async loop on unref'd timers and
 * sockets, never in the LLM call path; every failure is swallowed and retried
 * with backoff. Span and score ids are derived from the event id, so a
 * retried batch never duplicates anything in Langfuse.
 *
 * The pure part (`settings`, `statusLine`, `buildSpan`, `buildTracesBody`,
 * `buildScores`) is frozen by the `langfuse` fixture suite; request bodies are
 * byte-identical to the Python SDK's.
 */

import { createHash } from 'node:crypto';
import { closeSync, mkdirSync, openSync, readFileSync, readSync, statSync, unlinkSync, writeSync } from 'node:fs';
import * as http from 'node:http';
import * as https from 'node:https';
import { basename, join } from 'node:path';
import { performance } from 'node:perf_hooks';

import { writeFileAtomic } from '../auth/store.js';
import * as api from './api.js';
import { ENV_LANGFUSE, LANGFUSE_ON_VALUES, langfuseEnabled } from './config.js';
import { EVENT_SCHEMA, formatTs, parseTs, roundCost } from './events.js';
import { isAbsoluteFile } from './relpath.js';
import * as store from './store.js';
import { findForbidden, validEventId } from './upload.js';

/** The switch lives in config.ts (one helper). */
export const ENV_SWITCH = ENV_LANGFUSE;
export const SWITCH_ON = LANGFUSE_ON_VALUES;
export const ENV_BASE_URL = 'LANGFUSE_BASE_URL';
export const ENV_HOST = 'LANGFUSE_HOST';
export const ENV_PUBLIC_KEY = 'LANGFUSE_PUBLIC_KEY';
export const ENV_SECRET_KEY = 'LANGFUSE_SECRET_KEY';
export const DEFAULT_HOST = 'https://cloud.langfuse.com';
export const TRACES_PATH = '/api/public/otel/v1/traces';
export const SCORES_PATH = '/api/public/scores';

export const CURSOR_SCHEMA = 'tryaii.monitor.langfuse-cursor/1';
export const CURSOR_FILE = 'langfuse.json';
export const LOCK_FILE = 'langfuse.lock';
export const DASHBOARD_URL = 'https://tryaii.com/dashboard/monitor';
export const SERVICE_NAME = 'tryaii-monitor';
export const SCOPE_NAME = 'tryaii.monitor';

export const SCORE_RECOMMENDED = 'tryaii.recommended_model';
export const SCORE_SAVINGS = 'tryaii.est_savings_usd';
export const SCORE_ROUTE_STATUS = 'tryaii.route_status';

export const MAX_EVENTS = 100;
export const BATCH_INTERVAL_MS = 1000;
export const POLL_MS = 2000;
export const SCORE_CONCURRENCY = 8;
export const BACKOFF_MIN_MS = 5000;
export const BACKOFF_MAX_MS = 300_000;
export const DRAIN_TIMEOUT_MS = 2000;
export const LOCK_STALE_MS = 60_000;
export const TIMEOUT_MS = 10_000;

export const REASON_SWITCH = 'TRYAII_LANGFUSE is not on';
export const REASON_NO_APP_KEY = 'TRYAII_API_KEY is not set';
export const REASON_BAD_APP_KEY = 'TRYAII_API_KEY is not an app key';
export const REASON_NO_LANGFUSE_KEYS = 'LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are needed';

const HEX32 = /^[0-9a-f]{32}$/;
const HEX16 = /^[0-9a-f]{16}$/;

type Env = NodeJS.ProcessEnv | Record<string, string | undefined>;
type Rec = Record<string, unknown>;

/** The sink of the warning lines (tests replace it). */
export const warnings = {
  write(text: string): void {
    process.stderr.write(text);
  },
};

let warned = false;

/** Forget the one-time warning (tests). */
export function resetWarning(): void {
  warned = false;
}

function isRecord(value: unknown): value is Rec {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isInt(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value);
}

function isNum(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function debug(message: string, err?: unknown): void {
  if (process.env.TRYAII_VERBOSE) {
    process.stderr.write(`[tryaii monitor] ${message}${err ? `: ${String(err)}` : ''}\n`);
  }
}

// --- settings ---------------------------------------------------------------

function value(env: Env, name: string): string {
  const v = env[name];
  return typeof v === 'string' ? v.trim() : '';
}

/** `TRYAII_LANGFUSE` is `on`, `1` or `true` (trimmed, any case): `langfuseEnabled` in config.ts. */
export function switchOn(env: Env = process.env): boolean {
  return langfuseEnabled(env);
}

/** `LANGFUSE_BASE_URL`, else `LANGFUSE_HOST`, else Langfuse Cloud (EU); trailing `/` dropped. */
export function host(env: Env = process.env): string {
  for (const name of [ENV_BASE_URL, ENV_HOST]) {
    const v = value(env, name).replace(/\/+$/, '');
    if (v) return v;
  }
  return DEFAULT_HOST;
}

export interface Settings {
  enabled: boolean;
  host: string;
  reason: string | null;
}

/** `{enabled, host, reason}`: `reason` says why the export is off (null when on). Never a key. */
export function settings(env: Env = process.env): Settings {
  const target = host(env);
  let reason: string | null = null;
  if (!switchOn(env)) {
    reason = REASON_SWITCH;
  } else {
    const key = value(env, api.ENV_API_KEY);
    if (!key) reason = REASON_NO_APP_KEY;
    else if (!api.isAppKey(key)) reason = REASON_BAD_APP_KEY;
    else if (!value(env, ENV_PUBLIC_KEY) || !value(env, ENV_SECRET_KEY)) reason = REASON_NO_LANGFUSE_KEYS;
  }
  return { enabled: reason === null, host: target, reason };
}

/**
 * The `tryaii monitor status` line: `langfuse: on (<host>)`, `langfuse: off
 * (<reason>)` while the switch is on but a key is missing, `langfuse: off` for
 * any other switch value, null when `TRYAII_LANGFUSE` is unset or empty.
 */
export function statusLine(env: Env = process.env): string | null {
  if (!value(env, ENV_SWITCH)) return null;
  const s = settings(env);
  if (s.enabled) return `langfuse: on (${s.host})`;
  if (s.reason === REASON_SWITCH) return 'langfuse: off';
  return `langfuse: off (${String(s.reason)})`;
}

// --- the mapping (pure) -----------------------------------------------------

/** `[traceId, spanId]`: the first 32 and the next 16 hex characters of sha256(event_id). */
export function derivedIds(eventId: string): [string, string] {
  const digest = createHash('sha256').update(eventId, 'utf-8').digest('hex');
  return [digest.slice(0, 32), digest.slice(32, 48)];
}

export const ORIGIN_TRYAII = 'tryaii';

/** The event's `otel` when it names a valid span (lowercase hex, not all zeros), else null; `origin` kept only when it is "tryaii". */
export function validOtel(otel: unknown): { trace_id: string; span_id: string; origin?: string } | null {
  if (!isRecord(otel)) return null;
  const { trace_id: traceId, span_id: spanId } = otel;
  if (typeof traceId !== 'string' || !HEX32.test(traceId) || !/[^0]/.test(traceId)) return null;
  if (typeof spanId !== 'string' || !HEX16.test(spanId) || !/[^0]/.test(spanId)) return null;
  const out: { trace_id: string; span_id: string; origin?: string } = { trace_id: traceId, span_id: spanId };
  if (otel.origin === ORIGIN_TRYAII) out.origin = ORIGIN_TRYAII;
  return out;
}

export interface SpanIds {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
}

/** `{trace_id, span_id, parent_span_id}` of the event's span. */
export function spanIds(event: Rec): SpanIds {
  const [traceId, spanId] = derivedIds(String(event.event_id));
  const otel = validOtel(event.otel);
  if (otel === null) return { trace_id: traceId, span_id: spanId, parent_span_id: null };
  // origin "tryaii": the wrapper made the TryAii span the active parent at call time.
  if (otel.origin === ORIGIN_TRYAII) return { trace_id: otel.trace_id, span_id: otel.span_id, parent_span_id: null };
  return { trace_id: otel.trace_id, span_id: spanId, parent_span_id: otel.span_id };
}

/** The sub-process page on the TryAii dashboard; the monitor overview when the app id is unknown. */
export function tryaiiUrl(appId: unknown, process: unknown): string {
  if (typeof appId !== 'string' || !appId) return DASHBOARD_URL;
  try {
    const app = encodeURIComponent(appId);
    if (typeof process !== 'string' || !process) return `${DASHBOARD_URL}/${app}`;
    return `${DASHBOARD_URL}/${app}/features/${encodeURIComponent(process)}`;
  } catch {
    return DASHBOARD_URL; // a lone surrogate
  }
}

function text(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v);
}

/**
 * Langfuse's usage keys (as its own OpenAI integration sends them): `input` =
 * prompt - cached, `output` = completion - reasoning, `total`,
 * `input_cached_tokens`, `output_reasoning_tokens`; only the numbers the event
 * has. Null when there are none.
 */
export function usageDetails(usage: unknown): Rec | null {
  if (!isRecord(usage)) return null;
  const { prompt_tokens: prompt, completion_tokens: completion, total_tokens: total } = usage;
  const { cached_tokens: cached, reasoning_tokens: reasoning } = usage;
  const out: Rec = {};
  if (isInt(prompt)) out.input = isInt(cached) ? Math.max(prompt - cached, 0) : prompt;
  if (isInt(completion)) out.output = isInt(reasoning) ? Math.max(completion - reasoning, 0) : completion;
  if (isInt(total)) out.total = total;
  if (isInt(cached)) out.input_cached_tokens = cached;
  if (isInt(reasoning)) out.output_reasoning_tokens = reasoning;
  return Object.keys(out).length ? out : null;
}

function callSite(site: unknown): string | null {
  if (!isRecord(site) || typeof site.file !== 'string' || !site.file) return null;
  let file = site.file;
  if (isAbsoluteFile(file)) file = file.split(/[\\/]/).pop() as string; // TS-226
  return isInt(site.line) ? `${file}:${site.line}` : file;
}

function attr(key: string, v: string): Rec {
  return { key, value: { stringValue: v } };
}

/** One OTLP span (JSON encoding, hex ids) for one event. */
export function buildSpan(event: Rec, appId: string | null = null): Rec {
  const ids = spanIds(event);
  const ownTrace = ids.parent_span_id === null;
  // A generation only when nothing else records the call: with any otel the
  // integration's own generation carries model, usage and cost.
  const generation = validOtel(event.otel) === null;
  const proc = typeof event.process === 'string' ? event.process : 'default';
  const name = `tryaii ${proc}`;
  const url = tryaiiUrl(appId, proc);
  const usage: Rec = isRecord(event.usage) ? event.usage : {};
  const classification: Rec = isRecord(event.classification) ? event.classification : {};
  const error = isRecord(event.error) ? event.error : null;

  const attrs: Rec[] = [attr('langfuse.observation.type', generation ? 'generation' : 'span')];
  if (ownTrace) {
    attrs.push(attr('langfuse.trace.name', name));
    attrs.push(attr('langfuse.trace.metadata.tryaii_url', url));
  }
  if (generation) {
    if (typeof event.model_used === 'string' && event.model_used) {
      attrs.push(attr('langfuse.observation.model.name', event.model_used));
    }
    const details = usageDetails(usage);
    if (details !== null) attrs.push(attr('langfuse.observation.usage_details', JSON.stringify(details)));
    if (isNum(event.est_cost_usd)) {
      attrs.push(attr('langfuse.observation.cost_details', JSON.stringify({ total: event.est_cost_usd })));
    }
  }
  if (error !== null) {
    let message = String(error.class || 'error');
    if (isInt(error.status)) message = `${message} (HTTP ${error.status})`;
    attrs.push(attr('langfuse.observation.level', 'ERROR'));
    attrs.push(attr('langfuse.observation.status_message', message));
  }
  const metadata: [string, unknown][] = [
    ['tryaii_url', url],
    ['event_id', event.event_id],
    ['app', event.app],
    ['process', event.process],
    ['mode', event.mode],
    ['provider', event.provider],
    ['model_requested', event.model_requested],
    ['model_resolved', event.model_resolved],
    ['model_used', event.model_used],
    ['winner', event.winner],
    ['winner_reachable', event.winner_reachable],
    ['route_status', event.route_status],
    ['category', classification.category],
    ['subcategory', classification.subcategory],
    ['prompt_tokens', usage.prompt_tokens],
    ['completion_tokens', usage.completion_tokens],
    ['total_tokens', usage.total_tokens],
    ['cached_tokens', usage.cached_tokens],
    ['reasoning_tokens', usage.reasoning_tokens],
    ['est_cost_usd', event.est_cost_usd],
    ['est_cost_winner_usd', event.est_cost_winner_usd],
    ['est_cost_anchor_usd', event.est_cost_anchor_usd],
    ['latency_ms', event.latency_ms],
    ['stream', event.stream],
    ['call_site', callSite(event.call_site)],
    ['error_class', error ? error.class : null],
    ['error_status', error ? error.status : null],
  ];
  for (const [key, v] of metadata) {
    if (v !== null && v !== undefined && typeof v !== 'object') {
      attrs.push(attr(`langfuse.observation.metadata.${key}`, text(v)));
    }
  }

  const start = BigInt(parseTs(event.ts)) * 1_000_000n;
  const latency = event.latency_ms;
  const end = start + (isNum(latency) ? BigInt(Math.max(Math.trunc(latency), 0)) * 1_000_000n : 0n);
  const span: Rec = { traceId: ids.trace_id, spanId: ids.span_id };
  if (!ownTrace) span.parentSpanId = ids.parent_span_id;
  span.name = name;
  span.kind = 3; // SPAN_KIND_CLIENT
  span.startTimeUnixNano = start.toString();
  span.endTimeUnixNano = end.toString();
  span.attributes = attrs;
  span.status = error !== null ? { code: 2 } : { code: 1 };
  return span;
}

/** The OTLP/HTTP JSON request for a batch of spans. */
export function buildTracesBody(spans: Rec[], version: string): Rec {
  return {
    resourceSpans: [
      {
        resource: { attributes: [attr('service.name', SERVICE_NAME)] },
        scopeSpans: [{ scope: { name: SCOPE_NAME, version }, spans }],
      },
    ],
  };
}

/**
 * The event's Langfuse scores (`POST /api/public/scores` bodies), in the order
 * recommended_model, est_savings_usd, route_status; each only when the event
 * has its value. Score id = `<event_id>-<name without "tryaii.">`.
 */
export function buildScores(event: Rec): Rec[] {
  const ids = spanIds(event);
  const eventId = String(event.event_id);
  const score = (name: string, v: unknown, dataType: string): Rec => ({
    id: `${eventId}-${name.slice('tryaii.'.length)}`,
    traceId: ids.trace_id,
    observationId: ids.span_id,
    name,
    value: v,
    dataType,
  });
  const out: Rec[] = [];
  const winner = event.winner;
  if (typeof winner === 'string' && winner) out.push(score(SCORE_RECOMMENDED, winner, 'CATEGORICAL'));
  const cost = event.est_cost_usd;
  const atWinner = event.est_cost_winner_usd;
  if (isNum(cost) && isNum(atWinner)) out.push(score(SCORE_SAVINGS, roundCost(cost - atWinner), 'NUMERIC'));
  const status = event.route_status;
  if (typeof status === 'string' && status) out.push(score(SCORE_ROUTE_STATUS, status, 'CATEGORICAL'));
  return out;
}

/**
 * A store line the exporter sends: a `tryaii.monitor.event/1` object with a
 * uuid `event_id` and a parseable `ts`, without any forbidden field (the
 * uploader's hard rule, SPEC.md §3.4).
 */
export function exportable(obj: unknown): obj is Rec {
  if (!isRecord(obj) || obj.schema !== EVENT_SCHEMA) return false;
  if (!validEventId(obj.event_id) || findForbidden(obj) !== null) return false;
  try {
    parseTs(obj.ts);
  } catch {
    return false;
  }
  return true;
}

// --- cursor -----------------------------------------------------------------

export interface Cursor {
  schema: string;
  host: string | null;
  files: Record<string, number>;
  last_ok: string | null;
  last_error: string | null;
  /** In-memory only: a note for `last_error` once the batch is done. */
  note?: string | null;
}

function sortedFiles(files: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const name of Object.keys(files).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))) out[name] = files[name];
  return out;
}

export function readCursor(directory: string): Cursor | null {
  let obj: unknown;
  try {
    obj = JSON.parse(readFileSync(join(directory, CURSOR_FILE), 'utf-8'));
  } catch {
    return null;
  }
  if (!isRecord(obj) || obj.schema !== CURSOR_SCHEMA) return null;
  const files: Record<string, number> = {};
  if (isRecord(obj.files)) {
    for (const [name, v] of Object.entries(obj.files)) if (isInt(v) && v >= 0) files[name] = v;
  }
  return {
    schema: CURSOR_SCHEMA,
    host: typeof obj.host === 'string' ? obj.host : null,
    files: sortedFiles(files),
    last_ok: typeof obj.last_ok === 'string' ? obj.last_ok : null,
    last_error: typeof obj.last_error === 'string' ? obj.last_error : null,
  };
}

/** Atomic, indent 2; key order schema, host, files (sorted), last_ok, last_error. */
export function writeCursor(directory: string, cursor: Cursor): void {
  const doc = {
    schema: CURSOR_SCHEMA,
    host: cursor.host,
    files: sortedFiles(cursor.files),
    last_ok: cursor.last_ok,
    last_error: cursor.last_error,
  };
  writeFileAtomic(join(directory, CURSOR_FILE), Buffer.from(JSON.stringify(doc, null, 2) + '\n', 'utf-8'));
}

/** A new cursor at the current end of every events file: the export covers calls from now on (no backfill). */
export function startCursor(directory: string, targetHost: string): Cursor {
  const files: Record<string, number> = {};
  for (const path of store.eventFiles(directory)) {
    try {
      files[basename(path)] = statSync(path).size;
    } catch {
      /* gone */
    }
  }
  return { schema: CURSOR_SCHEMA, host: targetHost, files, last_ok: null, last_error: null };
}

// --- HTTP -------------------------------------------------------------------

/**
 * A POST: resolves to the HTTP status, rejects on a transport failure.
 * Redirects are never followed (the credentials never reach another host).
 */
export type PostFn = (url: string, body: Buffer, auth: string) => Promise<number>;

const defaultPost: PostFn = (url, body, auth) =>
  new Promise((resolve, reject) => {
    let target: URL;
    try {
      target = new URL(url);
    } catch (error) {
      reject(error);
      return;
    }
    const mod = target.protocol === 'http:' ? http : https;
    const req = mod.request(
      target,
      {
        method: 'POST',
        agent: false,
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'User-Agent': `tryaii/${api.sdkVersion()}`,
          Authorization: `Basic ${auth}`,
          'Content-Length': String(body.length),
          Connection: 'close',
        },
      },
      (res) => {
        res.on('data', () => {});
        res.on('error', reject);
        res.on('end', () => resolve(res.statusCode ?? 0));
      },
    );
    req.setTimeout(TIMEOUT_MS, () => req.destroy(new Error('timed out')));
    req.on('error', reject);
    req.on('socket', (socket) => socket.unref()); // never keeps the process alive
    req.write(body);
    req.end();
  });

// --- exporter ---------------------------------------------------------------

export function warnOff(reason: string): void {
  if (warned) return;
  warned = true;
  warnings.write(`tryaii monitor: the Langfuse export is off (${reason})\n`);
}

class DirLock {
  private readonly path: string;

  constructor(directory: string) {
    this.path = join(directory, LOCK_FILE);
  }

  acquire(): boolean {
    for (let attempt = 0; attempt < 2; attempt++) {
      let fd: number;
      try {
        fd = openSync(this.path, 'wx', 0o600);
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code !== 'EEXIST') return false;
        try {
          if (Date.now() - statSync(this.path).mtimeMs < LOCK_STALE_MS) return false;
          unlinkSync(this.path);
        } catch {
          return false;
        }
        continue;
      }
      try {
        writeSync(fd, String(process.pid));
      } finally {
        closeSync(fd);
      }
      return true;
    }
    return false;
  }

  release(): void {
    try {
      unlinkSync(this.path);
    } catch {
      /* gone */
    }
  }
}

export type ExportOutcome = 'sent' | 'idle' | 'wait' | 'backoff' | 'disabled';

export interface ExporterOptions {
  post?: PostFn;
  clock?: () => number;
  nowTs?: () => string;
  version?: string;
}

interface Batch {
  file: string;
  end: number;
  events: Rec[];
}

/**
 * Exports the registered app directories' events to Langfuse. Test seam:
 * `post`, `clock` (monotonic ms) and `nowTs` are injectable; `runOnce()`
 * performs one pass without the loop.
 */
export class Exporter {
  readonly host: string;
  disabled = false;
  backoff = 0;
  nextAt = 0;
  sentSpans = 0;
  sentScores = 0;
  private readonly auth: string;
  private readonly post: PostFn;
  private readonly clock: () => number;
  private readonly nowTs: () => string;
  private readonly version: string;
  private readonly dirList: string[] = [];
  private passChain: Promise<unknown> = Promise.resolve();
  private running = false;
  private stopped = false;
  private wakeFn: (() => void) | null = null;
  private wakePending = false;

  constructor(targetHost: string, publicKey: string, secretKey: string, opts: ExporterOptions = {}) {
    this.host = targetHost.replace(/\/+$/, '');
    this.auth = Buffer.from(`${publicKey}:${secretKey}`, 'utf-8').toString('base64');
    this.post = opts.post ?? defaultPost;
    this.clock = opts.clock ?? (() => performance.now());
    this.nowTs = opts.nowTs ?? (() => formatTs(Date.now()));
    this.version = opts.version ?? api.sdkVersion();
  }

  /** An exporter when the settings say on; null (one warning when the switch is on but a key is missing) otherwise. */
  static fromEnv(env: Env = process.env): Exporter | null {
    const s = settings(env);
    if (!s.enabled) {
      if (s.reason !== REASON_SWITCH) warnOff(String(s.reason));
      return null;
    }
    return new Exporter(s.host, value(env, ENV_PUBLIC_KEY), value(env, ENV_SECRET_KEY));
  }

  // -- registration --------------------------------------------------------

  /** Track `directory`; a missing cursor (or one for another host) starts at the current end of the store. */
  register(directory: string): void {
    if (this.dirList.includes(directory)) return;
    this.dirList.push(directory);
    try {
      const cursor = readCursor(directory);
      if (cursor === null || cursor.host !== this.host) {
        mkdirSync(directory, { recursive: true });
        writeCursor(directory, startCursor(directory, this.host));
      }
    } catch (err) {
      debug('langfuse cursor could not be created', err);
    }
    this.wake();
  }

  get dirs(): string[] {
    return [...this.dirList];
  }

  private wake(): void {
    this.wakePending = true;
    const fn = this.wakeFn;
    if (fn !== null) fn();
  }

  // -- one pass ------------------------------------------------------------

  private fail(now: number): void {
    this.backoff = this.backoff <= 0 ? BACKOFF_MIN_MS : Math.min(this.backoff * 2, BACKOFF_MAX_MS);
    this.nextAt = now + this.backoff;
  }

  /** One pass: at most one batch per app directory. Never rejects; passes never overlap. */
  runOnce(): Promise<ExportOutcome | null> {
    const run = this.passChain.then(async () => {
      try {
        return await this.pass();
      } catch (err) {
        debug('langfuse pass failed', err);
        return null;
      }
    });
    this.passChain = run;
    return run;
  }

  private async pass(): Promise<ExportOutcome> {
    if (this.disabled) return 'disabled';
    const now = this.clock();
    if (now < this.nextAt) return 'wait';
    let result: ExportOutcome = 'idle';
    for (const directory of this.dirs) {
      const outcome = await this.exportDir(directory, now);
      if (outcome === 'backoff' || outcome === 'disabled') return outcome;
      if (outcome === 'sent') result = 'sent';
    }
    if (result === 'sent') this.nextAt = now + BATCH_INTERVAL_MS;
    return result;
  }

  /** The unread whole lines of the oldest file that has any (at most MAX_EVENTS events), or null. */
  private batch(cursor: Cursor, directory: string): Batch | null {
    for (const path of store.eventFiles(directory)) {
      const name = basename(path);
      let offset = cursor.files[name] ?? 0;
      let size: number;
      try {
        size = statSync(path).size;
      } catch {
        continue;
      }
      if (size < offset) offset = 0; // rewritten file
      if (size === offset) continue;
      let data: Buffer;
      try {
        data = Buffer.alloc(size - offset);
        const fd = openSync(path, 'r');
        try {
          readSync(fd, data, 0, data.length, offset);
        } finally {
          closeSync(fd);
        }
      } catch {
        continue;
      }
      const events: Rec[] = [];
      let pos = 0;
      let end = offset;
      while (pos < data.length && events.length < MAX_EVENTS) {
        const nl = data.indexOf(0x0a, pos);
        if (nl < 0) break; // a partial last line waits for the writer
        let raw = data.subarray(pos, nl);
        if (raw.length && raw[raw.length - 1] === 0x0d) raw = raw.subarray(0, raw.length - 1);
        pos = nl + 1;
        end = offset + pos;
        let obj: unknown = null;
        try {
          const textLine = raw.toString('utf-8');
          obj = textLine.trim() ? JSON.parse(textLine) : null;
        } catch {
          obj = null;
        }
        if (exportable(obj)) events.push(obj);
      }
      if (end > offset) return { file: name, end, events };
    }
    return null;
  }

  private async exportDir(directory: string, now: number): Promise<ExportOutcome> {
    const lock = new DirLock(directory);
    if (!lock.acquire()) return 'idle';
    try {
      let cursor = readCursor(directory);
      if (cursor === null || cursor.host !== this.host) {
        cursor = startCursor(directory, this.host);
        writeCursor(directory, cursor);
        return 'idle';
      }
      const batch = this.batch(cursor, directory);
      if (batch === null) return 'idle';
      if (batch.events.length) {
        const failure = await this.postBatch(directory, cursor, batch.events, now);
        if (failure !== null) return failure;
      }
      cursor.files[batch.file] = batch.end;
      if (batch.events.length) {
        cursor.last_ok = this.nowTs();
        cursor.last_error = cursor.note ?? null;
      }
      writeCursor(directory, cursor);
      this.backoff = 0;
      return batch.events.length ? 'sent' : 'idle';
    } finally {
      lock.release();
    }
  }

  private async tryPost(path: string, body: Buffer): Promise<number | null> {
    try {
      return await this.post(this.host + path, body, this.auth);
    } catch {
      return null;
    }
  }

  /** Spans first, then the scores. Null = the batch is done; else the outcome of a failure to retry. */
  private async postBatch(directory: string, cursor: Cursor, events: Rec[], now: number): Promise<ExportOutcome | null> {
    const remote = store.readRemote(directory);
    const appId = isRecord(remote) && typeof remote.app_id === 'string' ? remote.app_id : null;
    const spans = events.map((e) => buildSpan(e, appId));
    const body = Buffer.from(JSON.stringify(buildTracesBody(spans, this.version)), 'utf-8');
    let failure = this.outcome(directory, cursor, await this.tryPost(TRACES_PATH, body), now, 'traces');
    if (failure === 'skip') return null;
    if (failure !== null) return failure;
    this.sentSpans += spans.length;
    const bodies = events.flatMap((e) => buildScores(e).map((s) => Buffer.from(JSON.stringify(s), 'utf-8')));
    const statuses: (number | null)[] = [];
    for (let start = 0; start < bodies.length; start += SCORE_CONCURRENCY) {
      const chunk = bodies.slice(start, start + SCORE_CONCURRENCY);
      statuses.push(...(await Promise.all(chunk.map((b) => this.tryPost(SCORES_PATH, b)))));
    }
    for (const status of statuses) {
      failure = this.outcome(directory, cursor, status, now, 'scores');
      if (failure === 'skip') {
        cursor.note = `scores: http ${String(status)}`;
        continue;
      }
      if (failure !== null) return failure; // retried: span and score ids are stable
      this.sentScores += 1;
    }
    return null;
  }

  /** Null for 2xx; `skip` for a 4xx retrying cannot fix; else the pass outcome (cursor kept, `last_error` set). */
  private outcome(
    directory: string,
    cursor: Cursor,
    status: number | null,
    now: number,
    what: string,
  ): ExportOutcome | 'skip' | null {
    if (status !== null && status >= 200 && status < 300) return null;
    if (status === 401 || status === 403) {
      cursor.last_error = `${what}: http ${status} (check LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)`;
      writeCursor(directory, cursor);
      this.disabled = true;
      warnings.write(
        `tryaii monitor: Langfuse rejected the keys (HTTP ${status}); the Langfuse export is off for this process\n`,
      );
      return 'disabled';
    }
    if (status !== null && status >= 400 && status < 500 && status !== 408 && status !== 429) {
      if (what === 'traces') cursor.note = `traces: http ${status}`;
      return 'skip';
    }
    cursor.last_error = `${what}: ${status === null ? 'network' : `http ${status}`}`;
    writeCursor(directory, cursor);
    this.fail(now);
    return 'backoff';
  }

  // -- loop ----------------------------------------------------------------

  start(): void {
    if (this.running || this.disabled) return;
    this.running = true;
    void this.loop();
  }

  /** Sleep `ms` on an unref'd timer; a register() wakes it early. */
  private sleep(ms: number): Promise<void> {
    if (this.wakePending) {
      this.wakePending = false;
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const done = (): void => {
        clearTimeout(timer);
        this.wakeFn = null;
        this.wakePending = false;
        resolve();
      };
      const timer = setTimeout(done, ms);
      timer.unref?.();
      this.wakeFn = done;
    });
  }

  private async loop(): Promise<void> {
    while (!this.stopped && !this.disabled) {
      const outcome = await this.runOnce();
      if (outcome === 'disabled') return;
      let delay: number;
      if (outcome === 'sent') delay = Math.max(this.nextAt - this.clock(), 0);
      else if (outcome === 'backoff' || outcome === 'wait') delay = Math.max(this.nextAt - this.clock(), 0) || POLL_MS;
      else delay = POLL_MS;
      await this.sleep(delay);
    }
  }

  /**
   * At exit: passes until nothing is left, within `timeoutMs` (best effort;
   * only the timer holds the process; a batch cut short is resent by the next
   * process).
   */
  async drain(timeoutMs: number = DRAIN_TIMEOUT_MS): Promise<void> {
    if (!this.running || this.disabled) return;
    let timer: NodeJS.Timeout | undefined;
    let expired = false;
    const passes = async (): Promise<void> => {
      while (!expired) {
        this.nextAt = 0;
        if ((await this.runOnce()) !== 'sent') return;
      }
    };
    await Promise.race([
      passes(),
      new Promise<void>((resolve) => {
        timer = setTimeout(() => {
          expired = true;
          resolve();
        }, timeoutMs);
      }),
    ]);
    if (timer !== undefined) clearTimeout(timer);
  }

  stop(): void {
    this.stopped = true;
    this.wake();
  }
}
