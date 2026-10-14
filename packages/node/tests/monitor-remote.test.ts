/**
 * Remote (dashboard) config: the long poll and how the wrapper applies it
 * (shared/monitor/API.md §3.3, SPEC.md §3.3).
 *
 * The poller runs one request at a time (`pollOnce`) through an injected
 * fetch; the wrapper tests use a duck-typed client and an injected routeFn
 * (no network, no daemon, no embedding model). Port of
 * packages/python/tests/test_monitor_remote.py.
 */

import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { clock, store, wrap, type Monitor } from '../src/monitor/index.js';
import * as remote from '../src/monitor/remote.js';
import * as upload from '../src/monitor/upload.js';
import { DOC_RECHECK_MS } from '../src/monitor/wrapOpenai.js';

const KEY = 'tryaii_k1a2b3c4d5e6_fixtureSecretfixtureSecretfixtureSecretfixt';
const BASE = 'https://api.test';
const FETCHED = '2026-10-12T09:00:00.000Z';
const CONFIG_1 = {
  schema: 'tryaii.monitor.remote/1',
  etag: 'e1',
  app_id: 'app_abcdefghijkl',
  default_mode: null,
  processes: { checkout: { mode: 'managed', model: 'openai/gpt-5-mini' } },
  updated_at: '2026-10-12T08:00:00.000Z',
  updated_by: 'google-sub-1',
};

type Answer = [number, unknown] | Error;

class FakeFetch {
  readonly answers: Answer[];
  readonly urls: string[] = [];
  readonly auth: (string | null)[] = [];
  constructor(...answers: Answer[]) {
    this.answers = answers;
  }

  readonly fetch = (async (url: string, init: RequestInit = {}) => {
    this.urls.push(url);
    this.auth.push(((init.headers ?? {}) as Record<string, string>).Authorization ?? null);
    const answer = this.answers.length > 1 ? (this.answers.shift() as Answer) : this.answers[0];
    if (answer instanceof Error) throw answer;
    const [status, body] = answer;
    return new Response(status === 304 || body === null ? null : JSON.stringify(body), { status });
  }) as unknown as typeof fetch;
}

let dataDir: string;
const savedEnv: Record<string, string | undefined> = {};
const savedNow = clock.now;
const warned: string[] = [];
const savedWrite = upload.warnings.write;

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'tryaii-monitor-remote-'));
  for (const key of [
    'TRYAII_DRE_DATA_DIR',
    'TRYAII_MONITOR_MODE',
    'TRYAII_APP',
    'TRYAII_MONITOR_VECTORS',
    'TRYAII_API_KEY',
    'TRYAII_HTTP_FIXTURE',
  ]) {
    savedEnv[key] = process.env[key];
    delete process.env[key];
  }
  process.env.TRYAII_DRE_DATA_DIR = dataDir;
  clock.now = () => Date.UTC(2026, 9, 12, 9, 0, 0);
  upload.resetKeyRejectedLog();
  warned.length = 0;
  upload.warnings.write = (text: string) => {
    warned.push(text);
  };
});

afterEach(() => {
  clock.now = savedNow;
  upload.warnings.write = savedWrite;
  upload.resetKeyRejectedLog();
  for (const [key, value] of Object.entries(savedEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  rmSync(dataDir, { recursive: true, force: true });
});

function poller(answers: Answer[], dirs: string[] = []): [remote.RemotePoller, FakeFetch] {
  const fake = new FakeFetch(...answers);
  const p = new remote.RemotePoller(KEY, BASE, { fetchFn: fake.fetch, nowTs: () => FETCHED });
  for (const d of dirs) p.register(d);
  return [p, fake];
}

function query(url: string): Record<string, string> {
  return Object.fromEntries(new URL(url).searchParams);
}

// --- the poller -----------------------------------------------------------------

describe('the poller', () => {
  it('a 200 writes remote.json everywhere and tracks the etag', async () => {
    const a = join(dataDir, 'a');
    const b = join(dataDir, 'b');
    mkdirSync(a);
    mkdirSync(b);
    const [p, fake] = poller([[200, CONFIG_1], [304, null]], [a, b]);
    expect(await p.pollOnce()).toBe('changed');
    for (const d of [a, b]) {
      const text = readFileSync(join(d, 'remote.json'), 'utf-8');
      const doc = JSON.parse(text);
      expect(Object.keys(doc)).toEqual([...Object.keys(CONFIG_1), 'fetched_at']);
      expect(doc.fetched_at).toBe(FETCHED);
      expect(text).toBe(JSON.stringify({ ...CONFIG_1, fetched_at: FETCHED }, null, 2) + '\n');
    }
    expect(p.etag).toBe('e1');
    expect(await p.pollOnce()).toBe('unchanged');
    expect(query(fake.urls[0])).toEqual({ etag: '', wait: '25' });
    expect(query(fake.urls[1])).toEqual({ etag: 'e1', wait: '25' });
    expect(new URL(fake.urls[0]).pathname).toBe('/v1/monitor/config');
    expect(fake.auth).toEqual([`Bearer ${KEY}`, `Bearer ${KEY}`]);
  });

  it('304 and errors leave the cached config in force', async () => {
    store.writeRemote(dataDir, CONFIG_1, '2026-10-12T07:00:00.000Z');
    const before = readFileSync(join(dataDir, 'remote.json'));
    const [p, fake] = poller(
      [[304, null], [500, { error: 'internal' }], new TypeError('down'), [200, { schema: 'something/else' }]],
      [dataDir],
    );
    expect(p.etag).toBe('e1'); // picked up from the cached file
    const outcomes: string[] = [];
    for (let i = 0; i < 4; i++) outcomes.push(await p.pollOnce());
    expect(outcomes).toEqual(['unchanged', 'error', 'error', 'error']);
    expect(readFileSync(join(dataDir, 'remote.json'))).toEqual(before);
    expect(query(fake.urls[0]).etag).toBe('e1');
  });

  it('a 401 stops polling and logs one line', async () => {
    const [p, fake] = poller([[401, { error: 'invalid_key' }]], [dataDir]);
    expect(await p.pollOnce()).toBe('disabled');
    expect(await p.pollOnce()).toBe('disabled');
    expect(fake.urls).toHaveLength(1);
    expect(warned).toEqual([upload.KEY_REJECTED_MESSAGE + '\n']);
    expect(warned.join('').includes(KEY)).toBe(false);
  });

  it('a malformed key never polls', async () => {
    const fake = new FakeFetch([200, CONFIG_1]);
    const p = new remote.RemotePoller('not-a-key', BASE, { fetchFn: fake.fetch });
    expect(p.disabled).toBe(true);
    expect(await p.pollOnce()).toBe('disabled');
    expect(fake.urls).toEqual([]);
  });

  it('url-encodes the etag like Python quote(safe="")', () => {
    expect(remote.configUrl('https://x/', 'a b/c')).toBe('https://x/v1/monitor/config?etag=a%20b%2Fc&wait=25');
    expect(remote.configUrl('https://x', "W/\"e!'()*~\"")).toBe(
      'https://x/v1/monitor/config?etag=W%2F%22e%21%27%28%29%2A~%22&wait=25',
    );
    expect(remote.configUrl('https://x', null)).toBe('https://x/v1/monitor/config?etag=&wait=25');
  });

  it('env switch', () => {
    expect(remote.remoteEnabled({})).toBe(true);
    expect(remote.remoteEnabled({ TRYAII_MONITOR_REMOTE: '0' })).toBe(false);
  });

  it('through the fixture seam: the query is logged, the key is not', async () => {
    const side = mkdtempSync(join(tmpdir(), 'tryaii-remote-seam-'));
    try {
      const fixture = join(side, 'http.json');
      const log = join(side, 'requests.jsonl');
      writeFileSync(fixture, JSON.stringify({ routes: { 'GET /v1/monitor/config': [{ status: 200, body: CONFIG_1 }, { status: 304 }] } }));
      const env = { TRYAII_HTTP_FIXTURE: fixture, TRYAII_HTTP_FIXTURE_LOG: log };
      const p = new remote.RemotePoller(KEY, BASE, { env, nowTs: () => FETCHED });
      p.register(dataDir);
      expect(await p.pollOnce()).toBe('changed');
      expect(await p.pollOnce()).toBe('unchanged');
      expect(await p.pollOnce()).toBe('unchanged'); // the last answer repeats
      const lines = readFileSync(log, 'utf-8').trimEnd().split('\n').map((l) => JSON.parse(l));
      expect(lines).toEqual([
        { method: 'GET', path: '/v1/monitor/config', query: { etag: '', wait: '25' }, auth: 'app_key', body: null },
        { method: 'GET', path: '/v1/monitor/config', query: { etag: 'e1', wait: '25' }, auth: 'app_key', body: null },
        { method: 'GET', path: '/v1/monitor/config', query: { etag: 'e1', wait: '25' }, auth: 'app_key', body: null },
      ]);
    } finally {
      rmSync(side, { recursive: true, force: true });
    }
  });
});

// --- the wrapper applies remote.json on the next call ---------------------------

const MESSAGES = [{ role: 'user', content: 'Summarize this order' }];
const SCORES = [
  { modelId: 'openai/gpt-5-mini', qualityScore: 0.8 },
  { modelId: 'openai/gpt-5.5', qualityScore: 0.82 },
];

class FakeClient {
  baseURL = 'https://api.openai.com/v1';
  calls: Record<string, unknown>[] = [];
  chat = {
    completions: {
      create: (body: Record<string, unknown>): Promise<unknown> => {
        this.calls.push({ ...body });
        return Promise.resolve({ model: body.model, usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } });
      },
    },
  };
}

const routeFn = (): [unknown[], unknown, unknown] => [SCORES, null, null];

async function call(client: FakeClient, monitor: Monitor): Promise<void> {
  await client.chat.completions.create({ model: 'gpt-5.5', messages: MESSAGES });
  await monitor.drain();
}

describe('the wrapper', () => {
  it('remote beats args and config; env beats remote', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', mode: 'off', routeFn });
    store.writeConfig(monitor.dir, {
      schema: 'tryaii.monitor.config/1',
      source: 'local',
      default_mode: 'off',
      processes: { checkout: { mode: 'dynamic', providers: null } },
    });
    store.writeRemote(monitor.dir, CONFIG_1, FETCHED);
    await call(client, monitor);
    expect(client.calls.at(-1)?.model).toBe('gpt-5-mini');
    process.env.TRYAII_MONITOR_MODE = 'off';
    await call(client, monitor);
    expect(client.calls.at(-1)?.model).toBe('gpt-5.5');
    const events = store.readRecentEvents(monitor.dir, 10);
    expect(events.map((e) => e.mode)).toEqual(['managed', 'off']);
    expect(events[0].route_status).toBe('ok');
  });

  it('a remote change applies after the mtime check', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn });
    await call(client, monitor);
    expect(client.calls.at(-1)?.model).toBe('gpt-5.5');
    store.writeRemote(monitor.dir, CONFIG_1, FETCHED);
    // Force the 5 s mtime re-check on the next call.
    const doc = (monitor as unknown as { remoteDoc: { checkedAt: number; mtime: number | null } }).remoteDoc;
    doc.checkedAt -= DOC_RECHECK_MS + 1;
    doc.mtime = -1;
    await call(client, monitor);
    expect(client.calls.at(-1)?.model).toBe('gpt-5-mini');
  });

  it('a broken remote.json means the local behaviour', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', mode: 'off', routeFn });
    mkdirSync(monitor.dir, { recursive: true });
    writeFileSync(join(monitor.dir, 'remote.json'), '{not json', 'utf-8');
    await call(client, monitor);
    expect(client.calls.at(-1)?.model).toBe('gpt-5.5');
  });
});
