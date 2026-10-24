/**
 * Remote (dashboard) config long poll (shared/monitor/API.md §3.3, SPEC.md
 * §3.3). Mirrors monitor/remote.py.
 *
 * The `RemotePoller` runs its own async loop, started by the transport only
 * when `TRYAII_API_KEY` is set and `TRYAII_MONITOR_REMOTE` is not `"0"`. It
 * long-polls `GET {TRYAII_API_URL}/v1/monitor/config?etag=<etag>&wait=25`: a
 * 200 writes the answer plus `fetched_at` to `remote.json` (atomically, in
 * every registered app directory) and polls again at once; a 304 polls again
 * at once; any error waits 30 s; a 401 stops polling for the process. Polls
 * are at least 1 s apart. The wrapper reads `remote.json` through the same
 * mtime cache as `config.json` and applies it on the next call (precedence
 * env > remote > `wrap(mode=)` > `config.json` > default). Fail-open: no
 * failure here ever reaches the app.
 *
 * Node specifics: the loop's timers and the long-poll socket are unref'd, so
 * a pending poll never keeps the process alive (Python: a daemon thread).
 *
 * First answer (TS-296): the first poll asks with `wait=0` so the server
 * answers at once (200 when the cached etag is stale or missing, else 304),
 * and `firstAnswer` resolves when it returns, whatever the outcome. The
 * transport lets the first call of the process wait for it, at most 500 ms,
 * once.
 */

import { performance } from 'node:perf_hooks';

import * as api from './api.js';
import { REMOTE_SCHEMA } from './config.js';
import { formatTs } from './events.js';
import * as store from './store.js';
import { logKeyRejected } from './upload.js';

export const ENV_REMOTE = 'TRYAII_MONITOR_REMOTE';
export const WAIT_S = 25;
export const HTTP_TIMEOUT_MS = 35_000;
export const ERROR_WAIT_MS = 30_000;
export const MIN_INTERVAL_MS = 1000;

/** False when `TRYAII_MONITOR_REMOTE` is `"0"`. */
export function remoteEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return (env[ENV_REMOTE] || '1').trim() !== '0';
}

/** Python's `urllib.parse.quote(text, safe='')`: only `A-Za-z0-9_.-~` stay literal. */
export function pyQuote(text: string): string {
  return encodeURIComponent(text).replace(/[!'()*]/g, (ch) => '%' + ch.charCodeAt(0).toString(16).toUpperCase());
}

export function configUrl(baseUrl: string, etag: string | null | undefined, wait: number = WAIT_S): string {
  return `${baseUrl.replace(/\/+$/, '')}/v1/monitor/config?etag=${pyQuote(etag || '')}&wait=${wait}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export type PollOutcome = 'changed' | 'unchanged' | 'error' | 'disabled';

export interface RemotePollerOptions {
  fetchFn?: typeof fetch;
  nowTs?: () => string;
  /** Monotonic milliseconds. */
  clock?: () => number;
  env?: NodeJS.ProcessEnv;
}

/**
 * Keeps `remote.json` current. Test seam: `fetchFn`, `nowTs` and
 * `pollOnce()` (one request, without the loop).
 */
export class RemotePoller {
  readonly key: string;
  readonly baseUrl: string;
  etag: string | null = null;
  disabled: boolean;
  private readonly fetchFn: typeof fetch | undefined;
  private readonly nowTs: () => string;
  private readonly clock: () => number;
  private readonly env: NodeJS.ProcessEnv | undefined;
  private readonly dirList: string[] = [];
  private running = false;
  private stopped = false;
  private registered: Promise<void>;
  private markRegistered: () => void = () => undefined;
  private wakeFn: (() => void) | null = null;
  /** True once the first poll returned (any outcome), or the poller cannot run. */
  answered = false;
  /** Resolves when `answered` becomes true; never rejects (TS-296). */
  readonly firstAnswer: Promise<void>;
  private markAnswered: () => void = () => undefined;

  constructor(key: string, baseUrl?: string | null, opts: RemotePollerOptions = {}) {
    this.key = key;
    this.baseUrl = (baseUrl || api.apiUrl(opts.env)).replace(/\/+$/, '');
    this.fetchFn = opts.fetchFn;
    this.nowTs = opts.nowTs ?? (() => formatTs(Date.now()));
    this.clock = opts.clock ?? (() => performance.now());
    this.env = opts.env;
    this.disabled = !api.isAppKey(key);
    this.registered = new Promise((resolve) => {
      this.markRegistered = resolve;
    });
    this.firstAnswer = new Promise((resolve) => {
      this.markAnswered = (): void => {
        this.answered = true;
        resolve();
      };
    });
    if (this.disabled) this.markAnswered();
  }

  register(directory: string): void {
    if (this.dirList.includes(directory)) return;
    this.dirList.push(directory);
    if (this.etag === null) {
      const cached = store.readRemote(directory);
      if (cached && typeof cached.etag === 'string') this.etag = cached.etag;
    }
    this.markRegistered();
  }

  get dirs(): string[] {
    return [...this.dirList];
  }

  /**
   * One long poll (`wait` seconds server-side, default 25). Resolves to
   * `changed`, `unchanged`, `error` or `disabled`. Never rejects.
   */
  async pollOnce(opts: { unref?: boolean; wait?: number } = {}): Promise<PollOutcome> {
    if (this.disabled) return 'disabled';
    let resp: api.ApiResponse;
    try {
      resp = await api.request('GET', configUrl(this.baseUrl, this.etag, opts.wait ?? WAIT_S), null, {
        bearer: this.key,
        fetchFn: this.fetchFn,
        timeoutMs: HTTP_TIMEOUT_MS,
        unref: Boolean(opts.unref),
        env: this.env,
      });
    } catch {
      return 'error';
    }
    if (resp.status === 304) return 'unchanged';
    if (resp.status === 401) {
      this.disabled = true;
      logKeyRejected();
      return 'disabled';
    }
    const body = resp.body;
    if (resp.status !== 200 || !isRecord(body) || body.schema !== REMOTE_SCHEMA) return 'error';
    const fetched = this.nowTs();
    try {
      for (const directory of this.dirs) store.writeRemote(directory, body, fetched);
    } catch {
      return 'error';
    }
    this.etag = typeof body.etag === 'string' ? body.etag : null;
    return 'changed';
  }

  start(): void {
    if (this.running || this.disabled) return;
    this.running = true;
    void this.loop().catch(() => undefined);
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const done = (): void => {
        clearTimeout(timer);
        this.wakeFn = null;
        resolve();
      };
      const timer = setTimeout(done, ms);
      timer.unref?.();
      this.wakeFn = done;
    });
  }

  private async loop(): Promise<void> {
    await this.registered;
    let first = true;
    while (!this.stopped) {
      const started = this.clock();
      // The first poll is answered at once (wait=0): the first call of the
      // process may be waiting for it.
      const outcome = await this.pollOnce({ unref: true, wait: first ? 0 : WAIT_S });
      if (first) {
        first = false;
        this.markAnswered();
      }
      if (outcome === 'disabled') return;
      if (outcome === 'error') {
        await this.sleep(ERROR_WAIT_MS);
        continue;
      }
      // Guard against a server that answers instantly in a loop.
      const spent = this.clock() - started;
      if (spent < MIN_INTERVAL_MS) await this.sleep(MIN_INTERVAL_MS - spent);
    }
  }

  stop(): void {
    this.stopped = true;
    this.markAnswered();
    const fn = this.wakeFn;
    if (fn !== null) fn();
    this.markRegistered();
  }
}
