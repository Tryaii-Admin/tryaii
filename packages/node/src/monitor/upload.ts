/**
 * Background upload of monitor events (shared/monitor/API.md §3.2, SPEC.md
 * §3.4). Mirrors monitor/upload.py.
 *
 * The `Uploader` runs its own async loop (never the routing jobs, so a slow
 * upload never delays recording), started by the transport only when
 * `TRYAII_API_KEY` is set and no opt-out is set (`TRYAII_MONITOR_UPLOAD=0`,
 * `TRYAII_TELEMETRY=0`, `DO_NOT_TRACK=1`; SPEC.md §9). It reads
 * the UNREAD bytes of every `events-*.jsonl` of the app directories this
 * process records to, batches whole lines (at most 500 events and a 1 MiB
 * body, one file per batch), and POSTs them to `/v1/monitor/events` -- at
 * most one batch every 5 s, with exponential backoff (5 s -> 5 min) on
 * 429/5xx/network. `upload.json` (the cursor) says how many bytes of each
 * file were uploaded. Local files are never deleted or rewritten.
 *
 * Privacy (SPEC.md §9): a pass pauses an app directory while the local
 * setting (`tryaii monitor upload off`) or the account switch (remote.json
 * `"upload": false`, or a 403 `upload_disabled`) says off; every sent line
 * carries the keyed `prompt_hash` (HMAC with the per-installation secret),
 * never the plain one; the first upload of a machine prints the Data Notice
 * once to a terminal.
 *
 * Hard rules: prompt text never leaves the machine (a line carrying a field
 * named `prompt`, `messages`, `response`, `content`, `text` or `raw` at any
 * depth is never sent); every failure is swallowed (fail-open: the app's
 * calls are never affected). The start-up key check
 * (`GET /v1/apps/keys/validate`) runs here, never on the hot path.
 *
 * Node specifics: the loop's timers and sockets are unref'd, so a pending
 * upload never keeps the process alive (Python runs it on a daemon thread);
 * the exit pass (`drain`) keeps the process alive only through its own 2 s
 * timer, like Python's `join(timeout)` on a daemon thread.
 */

import { closeSync, openSync, readSync, statSync, unlinkSync, writeSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { basename, dirname, join } from 'node:path';
import { performance } from 'node:perf_hooks';

import * as api from './api.js';
import { EVENT_SCHEMA, formatTs } from './events.js';
import * as privacy from './privacy.js';
import { isAbsoluteFile } from './relpath.js';
import * as store from './store.js';

export const UPLOAD_SCHEMA = 'tryaii.monitor.upload/1';
export const ENV_UPLOAD = privacy.ENV_UPLOAD;
export const MAX_EVENTS = 500;
export const MAX_BYTES = 1024 * 1024;
export const INTERVAL_MS = 5000;
export const BACKOFF_MIN_MS = 5000;
export const BACKOFF_MAX_MS = 300_000;
export const DRAIN_TIMEOUT_MS = 2000;
export const LOCK_STALE_MS = 60_000;
export const FORBIDDEN_FIELDS = ['prompt', 'messages', 'response', 'content', 'text', 'raw'] as const;
/** Mirrors `tryaii.monitor.NOTICE_VERSION` (monitor/index.ts re-exports it). */
export const NOTICE_VERSION = privacy.NOTICE_VERSION;

export const KEY_REJECTED_MESSAGE =
  'tryaii monitor: TRYAII_API_KEY was rejected (invalid_key); upload and remote config are off for this process';

let rejectedLogged = false;

/** False when an environment opt-out is set (SPEC.md §9). */
export function uploadEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return privacy.uploadDecision(env)[0];
}

/** The sink of the one-time Data Notice (tests replace it). */
export const notices = {
  write(text: string): void {
    process.stderr.write(text);
  },
};

/** The sink of the one warning line (tests replace it). */
export const warnings = {
  write(text: string): void {
    process.stderr.write(text);
  },
};

/** The one line a process logs when its app key is rejected. */
export function logKeyRejected(): void {
  if (rejectedLogged) return;
  rejectedLogged = true;
  try {
    warnings.write(KEY_REJECTED_MESSAGE + '\n');
  } catch {
    // fail-open
  }
}

/** Forget that the rejection line was logged (tests). */
export function resetKeyRejectedLog(): void {
  rejectedLogged = false;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * The first forbidden field name anywhere in `obj` (object keys at any
 * depth, arrays included), else null.
 */
export function findForbidden(obj: unknown): string | null {
  if (isRecord(obj)) {
    for (const [key, value] of Object.entries(obj)) {
      if ((FORBIDDEN_FIELDS as readonly string[]).includes(key)) return key;
      const found = findForbidden(value);
      if (found) return found;
    }
  } else if (Array.isArray(obj)) {
    for (const value of obj) {
      const found = findForbidden(value);
      if (found) return found;
    }
  }
  return null;
}

/** Python's `uuid.UUID(value)` acceptance: optional urn/braces/hyphens, 32 hex digits. */
export function validEventId(value: unknown): boolean {
  if (typeof value !== 'string') return false;
  let hex = value.replace(/urn:/g, '').replace(/uuid:/g, '');
  hex = hex.replace(/^[{}]+|[{}]+$/g, '').replace(/-/g, '');
  return /^[0-9a-fA-F]{32}$/.test(hex);
}

/**
 * The request body up to the opening `[` of `events` (API.md §3.2 key order:
 * schema, batch_id, sdk {lang, version}, notice_version).
 */
export function envelopeHead(batchId: string, version: string): Buffer {
  const head = JSON.stringify({
    schema: UPLOAD_SCHEMA,
    batch_id: batchId,
    sdk: { lang: 'node', version },
    notice_version: NOTICE_VERSION,
  });
  return Buffer.from(head.slice(0, -1) + ',"events":[', 'utf-8');
}

/** The exact request body: the envelope with the outgoing event lines (`outgoingLine`) spliced in. */
export function buildBody(batchId: string, version: string, lines: Buffer[]): Buffer {
  return Buffer.concat([envelopeHead(batchId, version), joinLines(lines), Buffer.from(']}')]);
}

function joinLines(lines: Buffer[]): Buffer {
  const parts: Buffer[] = [];
  lines.forEach((line, i) => {
    if (i) parts.push(Buffer.from(','));
    parts.push(line);
  });
  return Buffer.concat(parts);
}

/**
 * Whole lines of ONE events file: `lines` to send (raw bytes without the
 * newline) and the byte range `[start, end)` they cover, skipped lines
 * included.
 */
export class Batch {
  end: number;
  lines: Buffer[] = [];
  skipped = 0;
  forbidden: string | null = null;
  constructor(
    readonly file: string,
    readonly start: number,
  ) {
    this.end = start;
  }
}

const UTF8 = new TextDecoder('utf-8', { fatal: true });

function parseLine(raw: Buffer): unknown {
  let text: string;
  try {
    text = UTF8.decode(raw);
  } catch {
    return null;
  }
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function readRange(path: string, offset: number, length: number): Buffer {
  const fd = openSync(path, 'r');
  try {
    const buf = Buffer.alloc(length);
    let got = 0;
    while (got < length) {
      const n = readSync(fd, buf, got, length - got, offset + got);
      if (n <= 0) break;
      got += n;
    }
    return buf.subarray(0, got);
  } finally {
    closeSync(fd);
  }
}

export interface NextBatchOptions {
  maxEvents?: number;
  maxBytes?: number;
  headBytes?: number;
  /** The per-installation secret: sent lines carry the keyed `prompt_hash`. */
  hashKey?: Buffer | null;
}

/**
 * The next batch of unread lines (oldest file first), or null when every
 * file is fully uploaded. A batch may hold no lines (only skipped ones):
 * the caller then just advances the cursor.
 *
 * Lines are skipped (never sent) when they are not JSON objects of schema
 * `tryaii.monitor.event/1`, lack a uuid `event_id` (slice-1 lines), or carry
 * a forbidden field. A trailing line without a newline is left for later. A
 * file shorter than its cursor (rewritten) restarts at 0.
 */
/**
 * An absolute `call_site.file` never leaves the machine (TS-226): the line is
 * re-serialised with the base name only. Any other line is sent verbatim.
 */
export function stripMachinePath(obj: Record<string, unknown>, raw: Buffer): Buffer {
  return outgoingLine(obj, raw, null);
}

/**
 * The line as uploaded: an absolute `call_site.file` reduced to its base name
 * (TS-226) and, with `hashKey`, `prompt_hash` replaced by its keyed form
 * (SPEC.md §9). Re-serialised only when something changed; otherwise `raw`.
 */
export function outgoingLine(obj: Record<string, unknown>, raw: Buffer, hashKey: Buffer | null): Buffer {
  let changed = false;
  const site = obj.call_site;
  if (isRecord(site) && isAbsoluteFile(site.file)) {
    const parts = String(site.file).split(/[\\/]/);
    site.file = parts[parts.length - 1];
    changed = true;
  }
  if (hashKey !== null && typeof obj.prompt_hash === 'string') {
    obj.prompt_hash = privacy.keyedHash(hashKey, obj.prompt_hash);
    changed = true;
  }
  if (!changed) return raw;
  return Buffer.from(store.dumpsLine(obj).replace(/\n$/, ''), 'utf-8');
}

export function nextBatch(directory: string, cursor: store.UploadCursor, opts: NextBatchOptions = {}): Batch | null {
  const maxEvents = opts.maxEvents ?? MAX_EVENTS;
  const maxBytes = opts.maxBytes ?? MAX_BYTES;
  if (!isRecord(cursor.files)) cursor.files = {};
  const files = cursor.files;
  const budget = maxBytes - (opts.headBytes ?? 0) - 2; // the closing "]}"
  for (const path of store.eventFiles(directory)) {
    const name = basename(path);
    let offset = files[name] ?? 0;
    let size: number;
    try {
      size = statSync(path).size;
    } catch {
      continue;
    }
    if (size < offset) offset = 0;
    if (size === offset) continue;
    let data: Buffer;
    try {
      data = readRange(path, offset, size - offset);
    } catch {
      continue;
    }
    const batch = new Batch(name, offset);
    let used = 0;
    let pos = 0;
    while (pos < data.length) {
      const nl = data.indexOf(0x0a, pos);
      if (nl < 0) break; // partial last line: wait for the writer
      let rawEnd = nl;
      while (rawEnd > pos && data[rawEnd - 1] === 0x0d) rawEnd--;
      const raw = data.subarray(pos, rawEnd);
      const lineEnd = nl + 1;
      let keep = false;
      const obj = parseLine(raw);
      if (isRecord(obj) && obj.schema === EVENT_SCHEMA && validEventId(obj.event_id)) {
        const bad = findForbidden(obj);
        if (bad) batch.forbidden = batch.forbidden || bad;
        else keep = true;
      }
      const line = keep ? outgoingLine(obj as Record<string, unknown>, raw, opts.hashKey ?? null) : raw;
      if (keep) {
        const cost = line.length + (batch.lines.length ? 1 : 0);
        if (batch.lines.length && (batch.lines.length >= maxEvents || used + cost > budget)) break;
        if (!batch.lines.length && cost > budget) {
          batch.skipped += 1; // one line larger than a whole batch
        } else {
          batch.lines.push(Buffer.from(line));
          used += cost;
        }
      } else {
        batch.skipped += 1;
      }
      pos = lineEnd;
      batch.end = offset + lineEnd;
      if (batch.lines.length >= maxEvents) break;
    }
    if (batch.end > batch.start) return batch;
  }
  return null;
}

/**
 * `upload.lock` (O_EXCL): one uploading process per app directory at a
 * time; a lock older than 60 s is taken over (crashed holder).
 */
export class DirLock {
  readonly path: string;
  constructor(directory: string) {
    this.path = join(directory, store.UPLOAD_LOCK_FILE);
  }

  acquire(): boolean {
    for (let attempt = 0; attempt < 2; attempt++) {
      let fd: number;
      try {
        fd = openSync(this.path, 'wx', 0o600);
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code !== 'EEXIST') return false;
        let age: number;
        try {
          age = Date.now() - statSync(this.path).mtimeMs;
        } catch {
          continue;
        }
        if (age < LOCK_STALE_MS) return false;
        try {
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
      // already gone
    }
  }
}

function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

export type UploadOutcome = 'sent' | 'skipped' | 'idle' | 'wait' | 'disabled' | 'backoff' | 'rejected';

export interface UploaderOptions {
  /** Injected fetch (tests); otherwise the fixture seam or the network. */
  fetchFn?: typeof fetch;
  /** Monotonic milliseconds. */
  clock?: () => number;
  /** The `last_ok` timestamp. */
  nowTs?: () => string;
  version?: string;
  /** False = skip the start-up key check. */
  validate?: boolean;
  env?: NodeJS.ProcessEnv;
}

interface PassOptions {
  /** Unref the pass's sockets (the loop and the exit pass; tests leave them ref'd). */
  unref: boolean;
}

/**
 * Uploads the registered app directories' events in the background.
 *
 * Test seam: `fetchFn`, `clock` (monotonic ms) and `nowTs` (the `last_ok`
 * timestamp) are injectable, and `runOnce()` performs one pass without the
 * loop.
 */
export class Uploader {
  readonly key: string;
  readonly baseUrl: string;
  disabled: boolean;
  validated: Record<string, unknown> | null;
  /** Current backoff in ms (0 = none). */
  backoff = 0;
  /** Monotonic ms before which no pass sends. */
  nextAt = 0;
  sentEvents = 0;
  private readonly fetchFn: typeof fetch | undefined;
  private readonly clock: () => number;
  private readonly nowTs: () => string;
  private readonly version: string;
  private readonly env: NodeJS.ProcessEnv | undefined;
  private readonly dirList: string[] = [];
  private passChain: Promise<unknown> = Promise.resolve();
  private running = false;
  private stopped = false;
  private wakePending = false;
  private wakeFn: (() => void) | null = null;

  constructor(key: string, baseUrl?: string | null, opts: UploaderOptions = {}) {
    this.key = key;
    this.baseUrl = (baseUrl || api.apiUrl(opts.env)).replace(/\/+$/, '');
    this.fetchFn = opts.fetchFn;
    this.clock = opts.clock ?? (() => performance.now());
    this.nowTs = opts.nowTs ?? (() => formatTs(Date.now()));
    this.version = opts.version ?? api.sdkVersion();
    this.env = opts.env;
    this.disabled = !api.isAppKey(key);
    this.validated = opts.validate === false ? {} : null;
    if (this.disabled) logKeyRejected();
  }

  // -- registration ----------------------------------------------------

  register(directory: string): void {
    if (!this.dirList.includes(directory)) this.dirList.push(directory);
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

  // -- one pass --------------------------------------------------------

  private fail(now: number): void {
    this.backoff = this.backoff <= 0 ? BACKOFF_MIN_MS : Math.min(this.backoff * 2, BACKOFF_MAX_MS);
    this.nextAt = now + this.backoff;
  }

  private request(method: string, path: string, pass: PassOptions, rawBody?: Buffer): Promise<api.ApiResponse> {
    return api.request(method, `${this.baseUrl}${path}`, null, {
      bearer: this.key,
      fetchFn: this.fetchFn,
      rawBody: rawBody ?? null,
      unref: pass.unref,
      env: this.env,
    });
  }

  /** The cached start-up key check. False = not (yet) usable. */
  private async validateKey(now: number, pass: PassOptions): Promise<boolean> {
    if (this.validated !== null) return true;
    let resp: api.ApiResponse;
    try {
      resp = await this.request('GET', '/v1/apps/keys/validate', pass);
    } catch (err) {
      if (!(err instanceof api.ApiNetworkError)) throw err;
      this.fail(now);
      return false;
    }
    if (resp.status === 200 && isRecord(resp.body)) {
      this.validated = resp.body;
      return true;
    }
    if (resp.status === 401) {
      this.disabled = true;
      this.markRejected();
      logKeyRejected();
      return false;
    }
    this.fail(now);
    return false;
  }

  /**
   * `upload: error invalid_key` in every registered app directory, so
   * `tryaii monitor status` shows a key rejected at start-up too (the warning
   * line is easy to miss).
   */
  private markRejected(): void {
    for (const directory of this.dirs) {
      try {
        const cursor = store.readUploadCursor(directory) ?? store.emptyUploadCursor();
        cursor.last_error = 'invalid_key';
        store.writeUploadCursor(directory, cursor);
      } catch {
        /* fail-open: the status line keeps its last value */
      }
    }
  }

  /**
   * One pass: validate (once), then send at most ONE batch. Resolves to the
   * outcome (`sent`, `skipped`, `idle`, `wait`, `disabled`, `backoff`,
   * `rejected`) or null on an internal error. Never rejects. Passes never
   * overlap.
   */
  runOnce(pass: PassOptions = { unref: false }): Promise<UploadOutcome | null> {
    const run = this.passChain.then(async () => {
      try {
        return await this.pass(pass);
      } catch {
        return null;
      }
    });
    this.passChain = run;
    return run;
  }

  private async pass(pass: PassOptions): Promise<UploadOutcome> {
    if (this.disabled) return 'disabled';
    const now = this.clock();
    if (now < this.nextAt) return 'wait';
    if (!(await this.validateKey(now, pass))) return this.disabled ? 'disabled' : 'backoff';
    for (const directory of this.dirs) {
      const outcome = await this.uploadDir(directory, now, pass);
      if (outcome !== 'idle') return outcome;
    }
    return 'idle';
  }

  /**
   * True (and `last_error` set) while the local setting or the account
   * switch in remote.json says off.
   */
  private paused(directory: string, cursor: store.UploadCursor): boolean {
    const [enabled] = privacy.uploadDecision({}, dirname(directory));
    const remote = store.readRemote(directory);
    let reason: string | null = null;
    if (!enabled) reason = 'upload_off';
    else if (isRecord(remote) && remote.upload === false) reason = 'upload_disabled';
    if (reason === null) return false;
    if (cursor.last_error !== reason) {
      cursor.last_error = reason;
      store.writeUploadCursor(directory, cursor);
    }
    return true;
  }

  /** The Data Notice, once per machine and version, to a terminal only. */
  private firstUploadNotice(root: string): void {
    try {
      if (privacy.noticeShown(root) || !privacy.canShowNotice(process.stderr, this.env ?? process.env)) return;
      notices.write(privacy.NOTICE_TEXT);
      privacy.markNoticeShown(root, this.nowTs());
    } catch {
      // fail-open
    }
  }

  private async uploadDir(directory: string, now: number, pass: PassOptions): Promise<UploadOutcome> {
    const lock = new DirLock(directory);
    if (!isDir(directory) || !lock.acquire()) return 'idle';
    try {
      const cursor = store.readUploadCursor(directory) ?? store.emptyUploadCursor();
      if (this.paused(directory, cursor)) return 'idle';
      const head = envelopeHead(randomUUID(), this.version);
      const batch = nextBatch(directory, cursor, {
        headBytes: head.length,
        hashKey: privacy.hashKey(dirname(directory)),
      });
      if (batch === null) return 'idle';
      if (batch.forbidden) cursor.last_error = `forbidden_field: ${batch.forbidden}`;
      if (!batch.lines.length) {
        cursor.files[batch.file] = batch.end;
        store.writeUploadCursor(directory, cursor);
        return 'skipped';
      }
      const body = Buffer.concat([head, joinLines(batch.lines), Buffer.from(']}')]);
      this.firstUploadNotice(dirname(directory));
      let resp: api.ApiResponse;
      try {
        resp = await this.request('POST', '/v1/monitor/events', pass, body);
      } catch (err) {
        if (!(err instanceof api.ApiNetworkError)) throw err;
        cursor.last_error = 'network';
        store.writeUploadCursor(directory, cursor);
        this.fail(now);
        return 'backoff';
      }
      return this.handle(directory, cursor, batch, resp, now);
    } finally {
      lock.release();
    }
  }

  private handle(
    directory: string,
    cursor: store.UploadCursor,
    batch: Batch,
    resp: api.ApiResponse,
    now: number,
  ): UploadOutcome {
    const status = resp.status;
    if (status >= 200 && status < 300) {
      cursor.files[batch.file] = batch.end;
      cursor.last_ok = this.nowTs();
      if (!batch.forbidden) cursor.last_error = null;
      store.writeUploadCursor(directory, cursor);
      this.sentEvents += batch.lines.length;
      this.backoff = 0;
      this.nextAt = now + INTERVAL_MS;
      return 'sent';
    }
    if (status === 400 || status === 413) {
      // Shape problem: retrying cannot help -- skip the batch.
      cursor.files[batch.file] = batch.end;
      const error = resp.error || (status === 400 ? 'bad_batch' : 'too_large');
      cursor.last_error = resp.detail ? `${error}: ${resp.detail}` : error;
      store.writeUploadCursor(directory, cursor);
      this.nextAt = now + INTERVAL_MS;
      return 'skipped';
    }
    if (status === 403 && resp.error === 'upload_disabled') {
      // The account switch is off: retry rarely; remote.json says when it is on.
      cursor.last_error = 'upload_disabled';
      store.writeUploadCursor(directory, cursor);
      this.nextAt = now + BACKOFF_MAX_MS;
      return 'backoff';
    }
    if (status === 401) {
      cursor.last_error = 'invalid_key';
      store.writeUploadCursor(directory, cursor);
      this.disabled = true;
      logKeyRejected();
      return 'rejected';
    }
    if (status === 429) {
      cursor.last_error = 'rate_limited';
      store.writeUploadCursor(directory, cursor);
      this.fail(now);
      const retry = isRecord(resp.body) ? resp.body.retry_after : null;
      if (typeof retry === 'number' && retry > 0) {
        this.nextAt = Math.max(this.nextAt, now + Math.min(retry * 1000, BACKOFF_MAX_MS));
      }
      return 'backoff';
    }
    cursor.last_error = `http ${status}`;
    store.writeUploadCursor(directory, cursor);
    this.fail(now);
    return 'backoff';
  }

  // -- loop --------------------------------------------------------------

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
      const outcome = await this.runOnce({ unref: true });
      if (outcome === 'disabled' || outcome === 'rejected') return;
      if ((outcome === 'sent' || outcome === 'skipped') && this.clock() >= this.nextAt) continue;
      const delay = Math.max(this.nextAt - this.clock(), 0) || INTERVAL_MS;
      await this.sleep(outcome !== 'backoff' ? Math.min(delay, INTERVAL_MS) : delay);
    }
  }

  /** At exit: one last pass within `timeoutMs` (best effort; only the timer holds the process). */
  async drain(timeoutMs: number = DRAIN_TIMEOUT_MS): Promise<void> {
    if (!this.running || this.disabled) return;
    let timer: NodeJS.Timeout | undefined;
    await Promise.race([
      this.runOnce({ unref: true }),
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, timeoutMs);
      }),
    ]);
    if (timer !== undefined) clearTimeout(timer);
  }

  stop(): void {
    this.stopped = true;
    this.wake();
  }
}
