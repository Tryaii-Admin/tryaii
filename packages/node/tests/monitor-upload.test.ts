/**
 * The background uploader (shared/monitor/API.md §3.2, SPEC.md §3.4).
 *
 * Every test drives `Uploader.runOnce()` directly through an injected fetch
 * and a fake monotonic clock: no loop, no network, no daemon. Port of
 * packages/python/tests/test_monitor_upload.py.
 */

import { appendFileSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, utimesSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NOTICE_VERSION, Transport, buildEvent, store } from '../src/monitor/index.js';
import { RemotePoller } from '../src/monitor/remote.js';
import * as upload from '../src/monitor/upload.js';

const KEY = 'tryaii_k1a2b3c4d5e6_fixtureSecretfixtureSecretfixtureSecretfixt';
const BASE = 'https://api.test';
const VALID = {
  app_id: 'app_abcdefghijkl',
  app_name: 'shop',
  key_id: 'k1a2b3c4d5e6',
  scopes: ['monitor:write'],
  expires_at: null,
};

type Answer = [number, unknown] | Error;

interface Call {
  method: string;
  path: string;
  auth: string | null;
  data: Buffer | null;
}

/** Answers by path; each path's list is served in order (last repeats). */
class FakeFetch {
  readonly routes: Map<string, Answer[]>;
  readonly calls: Call[] = [];
  constructor(routes: Record<string, Answer[]>) {
    this.routes = new Map(Object.entries(routes).map(([k, v]) => [k, [...v]]));
  }

  readonly fetch = (async (url: string, init: RequestInit = {}) => {
    const path = new URL(url).pathname;
    const headers = (init.headers ?? {}) as Record<string, string>;
    const body = init.body;
    this.calls.push({
      method: String(init.method),
      path,
      auth: headers.Authorization ?? null,
      data: body === undefined || body === null ? null : Buffer.from(body as Uint8Array),
    });
    const queue = this.routes.get(path);
    if (!queue) throw new TypeError('fetch failed');
    const answer = queue.length > 1 ? (queue.shift() as Answer) : queue[0];
    if (answer instanceof Error) throw answer;
    const [status, payload] = answer;
    return new Response(payload === null || payload === undefined ? null : JSON.stringify(payload), { status });
  }) as unknown as typeof fetch;

  posts(): Call[] {
    return this.calls.filter((c) => c.method === 'POST');
  }
}

class Clock {
  t = 1_000_000;
  readonly now = (): number => this.t;
}

function event(i: number, extra: Record<string, unknown> = {}, ts = '2026-10-12T08:00:00.000Z'): Record<string, unknown> {
  const e = buildEvent({
    ts,
    app: 'shop',
    process: 'p',
    call_site: null,
    provider: 'openai',
    model_requested: 'gpt-5.5',
    model_resolved: 'openai/gpt-5.5',
    model_used: 'gpt-5.5',
    mode: 'off',
    winner: null,
    winner_reachable: false,
    route_status: 'ok',
    classification: null,
    usage: null,
    est_cost_usd: null,
    est_cost_winner_usd: null,
    latency_ms: i,
    stream: false,
    error: null,
    prompt_hash: 'h',
    vector: false,
    event_id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
  });
  return { ...e, ...extra };
}

let dir: string;

function writeEvents(events: Record<string, unknown>[], day = '2026-10-12'): string {
  const path = join(dir, `events-${day}.jsonl`);
  for (const e of events) appendFileSync(path, store.dumpsLine(e));
  return path;
}

function make(routes: Record<string, Answer[]>, opts: { validate?: boolean } = {}) {
  const fake = new FakeFetch(routes);
  const clock = new Clock();
  const up = new upload.Uploader(KEY, BASE, {
    fetchFn: fake.fetch,
    clock: clock.now,
    nowTs: () => '2026-10-12T09:00:00.000Z',
    version: '0.7.0',
    ...opts,
  });
  up.register(dir);
  return { up, fake, clock };
}

const warned: string[] = [];
const savedWrite = upload.warnings.write;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'tryaii-monitor-upload-'));
  upload.resetKeyRejectedLog();
  warned.length = 0;
  upload.warnings.write = (text: string) => {
    warned.push(text);
  };
});

afterEach(() => {
  upload.warnings.write = savedWrite;
  upload.resetKeyRejectedLog();
  rmSync(dir, { recursive: true, force: true });
});

// --- happy path ---------------------------------------------------------------

describe('happy path', () => {
  it('validates once, then uploads the unread lines', async () => {
    const path = writeEvents([event(1), event(2)]);
    const { up, fake, clock } = make({
      '/v1/apps/keys/validate': [[200, VALID]],
      '/v1/monitor/events': [[202, { accepted: 2, batch_id: 'x' }]],
    });
    expect(await up.runOnce()).toBe('sent');
    expect(fake.calls.map((c) => c.path)).toEqual(['/v1/apps/keys/validate', '/v1/monitor/events']);
    const post = fake.posts()[0];
    expect(post.auth).toBe(`Bearer ${KEY}`);
    const body = JSON.parse(String(post.data));
    expect(Object.keys(body)).toEqual(['schema', 'batch_id', 'sdk', 'notice_version', 'events']);
    expect(body.schema).toBe('tryaii.monitor.upload/1');
    expect(body.sdk).toEqual({ lang: 'node', version: '0.7.0' });
    expect(body.notice_version).toBe(NOTICE_VERSION);
    expect(NOTICE_VERSION).toBe('2026-10');
    expect(body.events.map((e: Record<string, unknown>) => e.event_id)).toEqual([
      '00000000-0000-4000-8000-000000000001',
      '00000000-0000-4000-8000-000000000002',
    ]);
    // The stored lines are spliced in verbatim.
    const lines = readFileSync(path, 'utf-8').trimEnd().split('\n');
    expect(String(post.data).endsWith('[' + lines.join(',') + ']}')).toBe(true);
    expect(store.readUploadCursor(dir)).toEqual({
      schema: 'tryaii.monitor.upload-cursor/1',
      files: { 'events-2026-10-12.jsonl': statSync(path).size },
      last_ok: '2026-10-12T09:00:00.000Z',
      last_error: null,
    });
    expect(up.validated).toEqual(VALID);
    expect(up.sentEvents).toBe(2);

    // At most one batch every 5 s; nothing new afterwards.
    writeEvents([event(3)]);
    expect(await up.runOnce()).toBe('wait');
    clock.t += upload.INTERVAL_MS;
    expect(await up.runOnce()).toBe('sent');
    expect(fake.posts()).toHaveLength(2);
    expect(fake.calls.filter((c) => c.path === '/v1/apps/keys/validate')).toHaveLength(1);
    clock.t += upload.INTERVAL_MS;
    expect(await up.runOnce()).toBe('idle');
  });

  it('never modifies local files', async () => {
    const path = writeEvents([event(1)]);
    const before = readFileSync(path);
    const { up } = make({ '/v1/apps/keys/validate': [[200, VALID]], '/v1/monitor/events': [[202, {}]] });
    await up.runOnce();
    expect(readFileSync(path)).toEqual(before);
    expect(readdirSync(dir).sort()).toEqual(['events-2026-10-12.jsonl', 'upload.json']);
  });

  it('holds at most 500 events per batch', async () => {
    writeEvents(Array.from({ length: 501 }, (_, i) => event(i)));
    const { up, fake, clock } = make({ '/v1/apps/keys/validate': [[200, VALID]], '/v1/monitor/events': [[202, {}]] });
    await up.runOnce();
    clock.t += upload.INTERVAL_MS;
    await up.runOnce();
    expect(fake.posts().map((p) => JSON.parse(String(p.data)).events.length)).toEqual([500, 1]);
  });

  it('keeps the body under the byte cap', () => {
    writeEvents(Array.from({ length: 10 }, (_, i) => event(i)));
    const one = Buffer.byteLength(store.dumpsLine(event(0))) - 1;
    const batch = upload.nextBatch(dir, store.emptyUploadCursor(), {
      maxBytes: 100 + 2 + one * 3 + 2,
      headBytes: 100,
    });
    expect(batch?.lines).toHaveLength(3);
    expect(batch?.end).toBe((batch?.start ?? 0) + 3 * (one + 1));
  });

  it('sends one file per batch, oldest first', async () => {
    writeEvents([event(1)], '2026-10-11');
    writeEvents([event(2)], '2026-10-12');
    const { up, fake, clock } = make({ '/v1/apps/keys/validate': [[200, VALID]], '/v1/monitor/events': [[202, {}]] });
    await up.runOnce();
    clock.t += upload.INTERVAL_MS;
    await up.runOnce();
    expect(fake.posts().map((p) => JSON.parse(String(p.data)).events[0].latency_ms)).toEqual([1, 2]);
    expect(Object.keys(store.readUploadCursor(dir)?.files ?? {})).toEqual([
      'events-2026-10-11.jsonl',
      'events-2026-10-12.jsonl',
    ]);
  });
});

// --- what is never sent -------------------------------------------------------

describe('never sent', () => {
  it('forbidden fields never leave the machine', async () => {
    const bad = event(2);
    bad.classification = { category: 'x', text: 'the prompt' };
    writeEvents([event(1), bad, event(3, { messages: [{ role: 'user' }] })]);
    const { up, fake } = make({ '/v1/apps/keys/validate': [[200, VALID]], '/v1/monitor/events': [[202, {}]] });
    expect(await up.runOnce()).toBe('sent');
    const body = String(fake.posts()[0].data);
    expect(body.includes('the prompt')).toBe(false);
    expect(body.includes('"messages"')).toBe(false);
    expect(JSON.parse(body).events.map((e: Record<string, unknown>) => e.latency_ms)).toEqual([1]);
    expect(store.readUploadCursor(dir)?.last_error).toBe('forbidden_field: text');
  });

  for (const field of upload.FORBIDDEN_FIELDS) {
    it(`finds a forbidden ${field} at any depth`, () => {
      expect(upload.findForbidden({ a: [{ b: { [field]: 1 } }] })).toBe(field);
      expect(upload.findForbidden({ a: [{ b: { ok: 1 } }] })).toBeNull();
    });
  }

  it('skips legacy, malformed and partial lines', async () => {
    const legacy = event(1);
    delete legacy.event_id;
    const path = writeEvents([legacy]);
    appendFileSync(path, 'not json\n');
    appendFileSync(path, '{"schema":"tryaii.monitor.vector/1"}\n');
    writeEvents([event(2)]);
    appendFileSync(path, store.dumpsLine(event(3)).replace(/\n$/, '')); // mid-write
    const { up, fake } = make({ '/v1/apps/keys/validate': [[200, VALID]], '/v1/monitor/events': [[202, {}]] });
    await up.runOnce();
    const sent = JSON.parse(String(fake.posts()[0].data)).events;
    expect(sent.map((e: Record<string, unknown>) => e.latency_ms)).toEqual([2]);
    const partial = Buffer.byteLength(store.dumpsLine(event(3))) - 1;
    expect(store.readUploadCursor(dir)?.files['events-2026-10-12.jsonl']).toBe(statSync(path).size - partial);
  });

  it('restarts a rewritten shorter file at zero', async () => {
    writeEvents([event(1)]);
    store.writeUploadCursor(dir, { files: { 'events-2026-10-12.jsonl': 10_000 } });
    const { up, fake } = make({ '/v1/apps/keys/validate': [[200, VALID]], '/v1/monitor/events': [[202, {}]] });
    expect(await up.runOnce()).toBe('sent');
    expect(JSON.parse(String(fake.posts()[0].data)).events).toHaveLength(1);
  });

  it('accepts the uuid forms Python accepts', () => {
    expect(upload.validEventId('00000000-0000-4000-8000-000000000001')).toBe(true);
    expect(upload.validEventId('{00000000000040008000000000000001}')).toBe(true);
    expect(upload.validEventId('urn:uuid:00000000-0000-4000-8000-000000000001')).toBe(true);
    expect(upload.validEventId('e-1')).toBe(false);
    expect(upload.validEventId(7)).toBe(false);
  });
});

// --- failures -----------------------------------------------------------------

describe('failures', () => {
  it('400 skips the batch and records the error', async () => {
    const path = writeEvents([event(1)]);
    const { up } = make({
      '/v1/apps/keys/validate': [[200, VALID]],
      '/v1/monitor/events': [[400, { error: 'bad_batch', detail: 'events[0].ts' }]],
    });
    expect(await up.runOnce()).toBe('skipped');
    const cursor = store.readUploadCursor(dir);
    expect(cursor?.files['events-2026-10-12.jsonl']).toBe(statSync(path).size);
    expect(cursor?.last_error).toBe('bad_batch: events[0].ts');
    expect(up.backoff).toBe(0);
  });

  it('413 without a body records too_large', async () => {
    writeEvents([event(1)]);
    const { up } = make({ '/v1/apps/keys/validate': [[200, VALID]], '/v1/monitor/events': [[413, null]] });
    expect(await up.runOnce()).toBe('skipped');
    expect(store.readUploadCursor(dir)?.last_error).toBe('too_large');
  });

  it('401 on upload disables the process and logs one line', async () => {
    writeEvents([event(1)]);
    const { up, fake, clock } = make({
      '/v1/apps/keys/validate': [[200, VALID]],
      '/v1/monitor/events': [[401, { error: 'invalid_key' }]],
    });
    expect(await up.runOnce()).toBe('rejected');
    clock.t += 1_000_000;
    expect(await up.runOnce()).toBe('disabled');
    expect(fake.posts()).toHaveLength(1);
    expect(store.readUploadCursor(dir)?.last_error).toBe('invalid_key');
    expect(store.readUploadCursor(dir)?.files).toEqual({});
    expect(warned).toEqual([upload.KEY_REJECTED_MESSAGE + '\n']);
    expect(warned.join('').includes(KEY)).toBe(false);
  });

  it('401 on validate disables before any upload', async () => {
    writeEvents([event(1)]);
    const { up, fake } = make({
      '/v1/apps/keys/validate': [[401, { error: 'invalid_key' }]],
      '/v1/monitor/events': [[202, {}]],
    });
    expect(await up.runOnce()).toBe('disabled');
    expect(fake.posts()).toEqual([]);
    expect(up.disabled).toBe(true);
    // The status line must say so, not stay at the last "ok" (phase 4 smoke).
    expect(store.readUploadCursor(dir)?.last_error).toBe('invalid_key');
  });

  it('never sends a malformed key', async () => {
    const fake = new FakeFetch({});
    const up = new upload.Uploader('tai_websitekey', BASE, { fetchFn: fake.fetch });
    up.register(dir);
    expect(up.disabled).toBe(true);
    expect(await up.runOnce()).toBe('disabled');
    expect(fake.calls).toEqual([]);
    expect(warned).toHaveLength(1);
  });

  it('5xx and network back off exponentially to 5 minutes', async () => {
    writeEvents([event(1)]);
    const { up, clock } = make({
      '/v1/apps/keys/validate': [[200, VALID]],
      '/v1/monitor/events': [[503, {}], new TypeError('down')],
    });
    const delays: number[] = [];
    for (let i = 0; i < 9; i++) {
      clock.t = Math.max(clock.t, up.nextAt);
      expect(await up.runOnce()).toBe('backoff');
      delays.push(up.backoff / 1000);
    }
    expect(delays).toEqual([5, 10, 20, 40, 80, 160, 300, 300, 300]);
    const cursor = store.readUploadCursor(dir);
    expect(cursor?.files['events-2026-10-12.jsonl'] ?? 0).toBe(0);
    expect(cursor?.last_error).toBe('network');
  });

  it('429 honours retry_after', async () => {
    writeEvents([event(1)]);
    const { up, clock } = make({
      '/v1/apps/keys/validate': [[200, VALID]],
      '/v1/monitor/events': [[429, { error: 'rate_limited', retry_after: 42 }]],
    });
    expect(await up.runOnce()).toBe('backoff');
    expect(up.nextAt).toBe(clock.t + 42_000);
    expect(store.readUploadCursor(dir)?.last_error).toBe('rate_limited');
  });

  it('a validate network error retries later', async () => {
    writeEvents([event(1)]);
    const { up, clock } = make({
      '/v1/apps/keys/validate': [new TypeError('down'), [200, VALID]],
      '/v1/monitor/events': [[202, {}]],
    });
    expect(await up.runOnce()).toBe('backoff');
    expect(await up.runOnce()).toBe('wait');
    clock.t = up.nextAt;
    expect(await up.runOnce()).toBe('sent');
  });

  it('success clears the last error', async () => {
    writeEvents([event(1)]);
    const { up, clock } = make({
      '/v1/apps/keys/validate': [[200, VALID]],
      '/v1/monitor/events': [[503, {}], [202, {}]],
    });
    await up.runOnce();
    expect(store.readUploadCursor(dir)?.last_error).toBe('http 503');
    clock.t = up.nextAt;
    expect(await up.runOnce()).toBe('sent');
    expect(store.readUploadCursor(dir)?.last_error).toBeNull();
    expect(up.backoff).toBe(0);
  });

  it('runOnce never rejects', async () => {
    writeEvents([event(1)]);
    const { up } = make({ '/v1/apps/keys/validate': [[200, VALID]] }, { validate: false });
    const spy = vi.spyOn(store, 'readUploadCursor').mockImplementation(() => {
      throw new Error('disk on fire');
    });
    try {
      expect(await up.runOnce()).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });
});

// --- lock, switches, transport wiring -----------------------------------------

describe('lock and wiring', () => {
  it('the directory lock is exclusive and stale locks are taken over', () => {
    const a = new upload.DirLock(dir);
    const b = new upload.DirLock(dir);
    expect(a.acquire()).toBe(true);
    expect(readFileSync(a.path, 'utf-8')).toBe(String(process.pid));
    expect(b.acquire()).toBe(false);
    const old = (Date.now() - upload.LOCK_STALE_MS - 5000) / 1000;
    utimesSync(a.path, old, old);
    expect(b.acquire()).toBe(true);
    b.release();
    expect(existsSync(a.path)).toBe(false);
  });

  it('a held lock skips the directory', async () => {
    writeEvents([event(1)]);
    const { up, fake } = make({ '/v1/apps/keys/validate': [[200, VALID]], '/v1/monitor/events': [[202, {}]] });
    const held = new upload.DirLock(dir);
    expect(held.acquire()).toBe(true);
    expect(await up.runOnce()).toBe('idle');
    expect(fake.posts()).toEqual([]);
    held.release();
  });

  it('env switch', () => {
    expect(upload.uploadEnabled({})).toBe(true);
    expect(upload.uploadEnabled({ TRYAII_MONITOR_UPLOAD: '0' })).toBe(false);
    expect(upload.uploadEnabled({ TRYAII_MONITOR_UPLOAD: '1' })).toBe(true);
  });

  const SWITCHES: [Record<string, string>, boolean, boolean][] = [
    [{}, false, false],
    [{ TRYAII_API_KEY: KEY }, true, true],
    [{ TRYAII_API_KEY: KEY, TRYAII_MONITOR_UPLOAD: '0' }, false, true],
    [{ TRYAII_API_KEY: KEY, TRYAII_MONITOR_REMOTE: '0' }, true, false],
  ];
  for (const [env, expectUpload, expectRemote] of SWITCHES) {
    it(`the transport starts services only with a key: ${JSON.stringify(Object.keys(env))}`, () => {
      const saved: Record<string, string | undefined> = {};
      for (const name of ['TRYAII_API_KEY', 'TRYAII_MONITOR_UPLOAD', 'TRYAII_MONITOR_REMOTE']) {
        saved[name] = process.env[name];
        delete process.env[name];
      }
      Object.assign(process.env, env);
      const started: string[] = [];
      const upSpy = vi.spyOn(upload.Uploader.prototype, 'start').mockImplementation(() => {
        started.push('upload');
      });
      const remoteSpy = vi.spyOn(RemotePoller.prototype, 'start').mockImplementation(() => {
        started.push('remote');
      });
      try {
        const transport = new Transport();
        transport.registerApp(dir);
        (transport as unknown as { startServices(): void }).startServices();
        expect(transport.uploader !== null).toBe(expectUpload);
        expect(transport.remote !== null).toBe(expectRemote);
        if (expectUpload) expect((transport.uploader as upload.Uploader).dirs).toEqual([dir]);
        expect(started.sort()).toEqual([...(expectUpload ? ['upload'] : []), ...(expectRemote ? ['remote'] : [])].sort());
      } finally {
        upSpy.mockRestore();
        remoteSpy.mockRestore();
        for (const [name, value] of Object.entries(saved)) {
          if (value === undefined) delete process.env[name];
          else process.env[name] = value;
        }
      }
    });
  }

  it('injected transports never start services', () => {
    const saved = process.env.TRYAII_API_KEY;
    process.env.TRYAII_API_KEY = KEY;
    try {
      const transport = new Transport(null, { routeFn: () => [[], null, null], inline: true });
      transport.start();
      expect(transport.uploader).toBeNull();
      expect(transport.remote).toBeNull();
    } finally {
      if (saved === undefined) delete process.env.TRYAII_API_KEY;
      else process.env.TRYAII_API_KEY = saved;
    }
  });

  it('drain without a running loop is a no-op', async () => {
    const { up, fake } = make({ '/v1/apps/keys/validate': [[200, VALID]] });
    await up.drain(100);
    expect(fake.calls).toEqual([]);
  });

  it('through the fixture seam: the request log names the auth kind, never the key', async () => {
    writeEvents([event(1)]);
    const side = mkdtempSync(join(tmpdir(), 'tryaii-upload-seam-'));
    try {
      const fixture = join(side, 'http.json');
      const log = join(side, 'requests.jsonl');
      appendFileSync(
        fixture,
        JSON.stringify({
          routes: {
            'GET /v1/apps/keys/validate': [{ status: 200, body: VALID }],
            'POST /v1/monitor/events': [{ status: 202, body: { accepted: 1 } }],
          },
        }),
      );
      const env = { TRYAII_HTTP_FIXTURE: fixture, TRYAII_HTTP_FIXTURE_LOG: log };
      const up = new upload.Uploader(KEY, BASE, { env, version: '0.7.0', nowTs: () => 't' });
      up.register(dir);
      expect(await up.runOnce()).toBe('sent');
      const lines = readFileSync(log, 'utf-8').trimEnd().split('\n').map((l) => JSON.parse(l));
      expect(lines.map((l) => [l.method, l.path, l.auth])).toEqual([
        ['GET', '/v1/apps/keys/validate', 'app_key'],
        ['POST', '/v1/monitor/events', 'app_key'],
      ]);
      expect(lines[1].body.events[0].latency_ms).toBe(1);
      expect(readFileSync(log, 'utf-8').includes('fixtureSecret')).toBe(false);
    } finally {
      rmSync(side, { recursive: true, force: true });
    }
  });
});
