/**
 * HTTP to the tryaii API for monitor upload, remote config and `tryaii apps`
 * (shared/monitor/API.md, SPEC.md §6). Mirrors monitor/api.py.
 *
 * node:http / node:https (an injected `fetchFn` in tests). Redirects are
 * never followed (the Authorization header must not travel to another
 * host). One attempt per call; callers own retries and backoff. `unref`
 * requests (the uploader and long-poll loops) never keep the process alive,
 * like Python's daemon threads.
 *
 * `request` resolves to an `ApiResponse` for every HTTP status and rejects
 * with `ApiNetworkError` only for transport failures (connection, timeout,
 * unreadable body). Seams, in order of precedence:
 *
 * 1. `fetchFn` -- an injected fetch (unit tests);
 * 2. env `TRYAII_HTTP_FIXTURE` -- the path of a JSON file of canned
 *    responses (SPEC.md §6.3; the CLI fixture suites and both SDKs' parity
 *    drivers use it, so no test ever touches the network). With
 *    `TRYAII_HTTP_FIXTURE_LOG` set too, every request is appended to that
 *    file as one JSON line `{method, path, query, auth, body}` (`auth` =
 *    `app_key` | `bearer` | null -- never the credential);
 * 3. the real network.
 */

import { appendFileSync, readFileSync } from 'node:fs';
import * as http from 'node:http';
import * as https from 'node:https';

export const DEFAULT_API_URL = 'https://api.tryaii.com';
export const TIMEOUT_MS = 10_000;
/** Node only: how long an unref'd (background) request may stay connecting. */
export const BACKGROUND_CONNECT_TIMEOUT_MS = 3000;
export const ENV_API_URL = 'TRYAII_API_URL';
export const ENV_API_KEY = 'TRYAII_API_KEY';
export const ENV_HTTP_FIXTURE = 'TRYAII_HTTP_FIXTURE';
export const ENV_HTTP_FIXTURE_LOG = 'TRYAII_HTTP_FIXTURE_LOG';

/** `tryaii_<key_id>_<secret>` (API.md §1). */
export const APP_KEY_RE = /^tryaii_([a-z0-9]{12})_([A-Za-z0-9_-]{43})$/;

/** The request never produced an HTTP status (refused, timeout, ...). */
export class ApiNetworkError extends Error {
  constructor(message = 'network') {
    super(message);
    this.name = 'ApiNetworkError';
  }
}

/**
 * One HTTP answer: `status`, the decoded JSON `body` (null when empty or not
 * JSON) and the response `headers` (lower-cased names).
 */
export class ApiResponse {
  constructor(
    readonly status: number,
    readonly body: unknown,
    readonly headers: Record<string, string> = {},
  ) {}

  /** The `error` code of an error body, else null. */
  get error(): string | null {
    const b = this.body;
    if (b !== null && typeof b === 'object' && !Array.isArray(b)) {
      const value = (b as Record<string, unknown>).error;
      if (typeof value === 'string') return value || null;
    }
    return null;
  }

  get detail(): string | null {
    const b = this.body;
    if (b !== null && typeof b === 'object' && !Array.isArray(b)) {
      const value = (b as Record<string, unknown>).detail;
      if (typeof value === 'string') return value || null;
    }
    return null;
  }
}

/** `TRYAII_API_URL` (an empty value falls through) or the default, without trailing slashes. */
export function apiUrl(env: NodeJS.ProcessEnv = process.env): string {
  return (env[ENV_API_URL] || DEFAULT_API_URL).replace(/\/+$/, '');
}

/** `TRYAII_API_KEY` trimmed, or null. */
export function apiKey(env: NodeJS.ProcessEnv = process.env): string | null {
  const value = (env[ENV_API_KEY] ?? '').trim();
  return value || null;
}

export function isAppKey(value: unknown): value is string {
  return typeof value === 'string' && APP_KEY_RE.test(value);
}

let packageVersion: string | null = null;

/** The SDK version (`User-Agent: tryaii/<version>`, upload `sdk.version`). */
export function sdkVersion(): string {
  if (packageVersion === null) {
    try {
      const pkg = JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf-8')) as {
        version?: string;
      };
      packageVersion = pkg.version ?? '0.0.0';
    } catch {
      packageVersion = '0.0.0';
    }
  }
  return packageVersion;
}

function decode(raw: Uint8Array | string): unknown {
  const text = typeof raw === 'string' ? raw : Buffer.from(raw).toString('utf-8');
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** Python's urlparse: the path between the host and the first `?` / `#` (never decoded). */
function splitUrl(url: string): { path: string; query: string } {
  let rest = url;
  const scheme = rest.indexOf('://');
  if (scheme >= 0) {
    rest = rest.slice(scheme + 3);
    const cut = rest.search(/[/?#]/);
    rest = cut >= 0 ? rest.slice(cut) : '';
  }
  const hash = rest.indexOf('#');
  if (hash >= 0) rest = rest.slice(0, hash);
  const q = rest.indexOf('?');
  if (q < 0) return { path: rest, query: '' };
  return { path: rest.slice(0, q), query: rest.slice(q + 1) };
}

/** `dict(parse_qsl(query, keep_blank_values=True)) or None`. */
function queryObject(query: string): Record<string, string> | null {
  if (!query) return null;
  const out: Record<string, string> = {};
  let any = false;
  for (const [key, value] of new URLSearchParams(query)) {
    out[key] = value;
    any = true;
  }
  return any ? out : null;
}

// --- the fixture transport (SPEC.md §6.3) -------------------------------------

type FixtureAnswer = Record<string, unknown>;
const fixtureState = new Map<string, Map<string, FixtureAnswer[]>>();

function fixtureRoutes(path: string): Map<string, FixtureAnswer[]> {
  let routes = fixtureState.get(path);
  if (routes === undefined) {
    let doc: unknown = {};
    try {
      doc = JSON.parse(readFileSync(path, 'utf-8'));
    } catch {
      doc = {};
    }
    const raw =
      doc !== null && typeof doc === 'object' && !Array.isArray(doc) ? (doc as Record<string, unknown>).routes : null;
    routes = new Map();
    if (raw !== null && typeof raw === 'object' && !Array.isArray(raw)) {
      for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
        if (Array.isArray(value)) routes.set(key, [...value] as FixtureAnswer[]);
      }
    }
    fixtureState.set(path, routes);
  }
  return routes;
}

function authKind(bearer: string | null | undefined): string | null {
  if (bearer === null || bearer === undefined) return null;
  return isAppKey(bearer) ? 'app_key' : 'bearer';
}

function fixtureRequest(
  fixture: string,
  method: string,
  url: string,
  body: unknown,
  bearer: string | null | undefined,
  env: NodeJS.ProcessEnv,
): ApiResponse {
  const { path, query } = splitUrl(url);
  const log = env[ENV_HTTP_FIXTURE_LOG];
  if (log) {
    const line = { method, path, query: queryObject(query), auth: authKind(bearer), body: body ?? null };
    appendFileSync(log, JSON.stringify(line) + '\n', 'utf-8');
  }
  const queue = fixtureRoutes(fixture).get(`${method} ${path}`);
  if (!queue || queue.length === 0) throw new ApiNetworkError('no fixture route');
  const item = queue.length > 1 ? queue.shift() : queue[0];
  if (item === null || typeof item !== 'object' || Array.isArray(item) || item.network) {
    throw new ApiNetworkError('fixture network failure');
  }
  const status = item.status;
  if (typeof status !== 'number' || !Number.isInteger(status)) throw new ApiNetworkError('fixture without status');
  return new ApiResponse(status, item.body === undefined ? null : item.body, {});
}

/** Forget loaded fixture files (tests). */
export function resetFixtureState(): void {
  fixtureState.clear();
}

// --- request ------------------------------------------------------------------

export interface RequestOptions {
  bearer?: string | null;
  /** Injected fetch (tests); bypasses the fixture seam. */
  fetchFn?: typeof fetch;
  timeoutMs?: number;
  /** The exact body bytes (upload); `body` is ignored when given. */
  rawBody?: Uint8Array | string | null;
  env?: NodeJS.ProcessEnv;
  /** Unref the socket so a pending request never keeps the process alive. */
  unref?: boolean;
}

interface RawAnswer {
  status: number;
  raw: Uint8Array;
  headers: Record<string, string>;
}

/** One request over node:http(s); never follows redirects. */
function nodeRequest(
  method: string,
  url: string,
  headers: Record<string, string>,
  data: Uint8Array | string | null,
  timeoutMs: number,
  unref: boolean,
): Promise<RawAnswer> {
  return new Promise((resolve, reject) => {
    let target: URL;
    try {
      target = new URL(url);
    } catch (error) {
      reject(error);
      return;
    }
    const mod = target.protocol === 'http:' ? http : https;
    const body = data === null ? null : typeof data === 'string' ? Buffer.from(data, 'utf-8') : Buffer.from(data);
    const all: Record<string, string> = { ...headers, Connection: 'close' };
    if (body !== null) all['Content-Length'] = String(body.length);
    const req = mod.request(target, { method, headers: all, agent: false }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (chunk: Buffer) => chunks.push(chunk));
      res.on('error', reject);
      res.on('end', () => {
        const out: Record<string, string> = {};
        for (const [key, value] of Object.entries(res.headers)) {
          if (value !== undefined) out[key.toLowerCase()] = Array.isArray(value) ? value.join(', ') : String(value);
        }
        resolve({ status: res.statusCode ?? 0, raw: Buffer.concat(chunks), headers: out });
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error('timed out')));
    req.on('error', reject);
    if (unref) {
      // An unref'd socket stops holding the process once it is connected
      // (the long poll then waits on a read). A pending TCP connect is a
      // libuv request, which keeps the loop alive whatever the handle's ref
      // state, so a connect that hangs (blackholed host) is abandoned after
      // BACKGROUND_CONNECT_TIMEOUT_MS -- the caller sees a network error and
      // backs off, as it would after Python's 10 s timeout.
      req.on('socket', (socket) => {
        socket.unref();
        setImmediate(() => socket.unref()).unref();
        if (!socket.connecting) return;
        const timer = setTimeout(() => {
          if (socket.connecting) req.destroy(new Error('connect timed out'));
        }, BACKGROUND_CONNECT_TIMEOUT_MS);
        timer.unref();
        socket.once('connect', () => clearTimeout(timer));
        socket.once('close', () => clearTimeout(timer));
      });
    }
    if (body !== null) req.write(body);
    req.end();
  });
}

async function fetchRequest(
  fetchFn: typeof fetch,
  method: string,
  url: string,
  headers: Record<string, string>,
  data: Uint8Array | string | null,
  timeoutMs: number,
): Promise<RawAnswer> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchFn(url, {
      method,
      headers,
      body: data === null ? undefined : (data as string),
      signal: controller.signal,
      redirect: 'manual',
    });
    const raw = new Uint8Array(await response.arrayBuffer());
    const out: Record<string, string> = {};
    response.headers?.forEach((value, key) => {
      out[key.toLowerCase()] = value;
    });
    return { status: response.status, raw, headers: out };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Send one request; JSON `body` (or the exact `rawBody` bytes). Resolves for
 * EVERY HTTP status; rejects with ApiNetworkError for a transport failure.
 * Never follows redirects (a 3xx is returned as is).
 */
export async function request(
  method: string,
  url: string,
  body: unknown = null,
  opts: RequestOptions = {},
): Promise<ApiResponse> {
  const env = opts.env ?? process.env;
  const rawBody = opts.rawBody ?? null;
  if (!opts.fetchFn) {
    const fixture = env[ENV_HTTP_FIXTURE];
    if (fixture) {
      const logged = rawBody !== null ? decode(rawBody) : body;
      return fixtureRequest(fixture, method, url, logged, opts.bearer, env);
    }
  }
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'User-Agent': `tryaii/${sdkVersion()}`,
  };
  let data: Uint8Array | string | null = rawBody;
  if (data === null && body !== null && body !== undefined) data = JSON.stringify(body);
  if (data !== null) headers['Content-Type'] = 'application/json';
  if (opts.bearer !== null && opts.bearer !== undefined) headers.Authorization = `Bearer ${opts.bearer}`;
  const timeoutMs = opts.timeoutMs ?? TIMEOUT_MS;
  let answer: RawAnswer;
  try {
    answer = opts.fetchFn
      ? await fetchRequest(opts.fetchFn, method, url, headers, data, timeoutMs)
      : await nodeRequest(method, url, headers, data, timeoutMs, Boolean(opts.unref));
  } catch (error) {
    throw new ApiNetworkError(error instanceof Error ? error.name : 'network');
  }
  return new ApiResponse(answer.status, decode(answer.raw), answer.headers);
}
