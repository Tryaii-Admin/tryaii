/**
 * Catalog client: docs/catalog/CONTRACT-catalog-v1.md section 5.
 *
 * One test (or a small group) per rule of section 5 -- cache layout, which
 * catalog (auto / starter / full), the daily check and every server outcome,
 * the nudge, login / logout / whoami, plus the Router option and the daemon /
 * centroid-cache keying. Mirrors packages/python/tests/test_catalog_client.py.
 * Driven against a small in-file HTTP fake of the auth + catalog API that
 * serves small but valid full bundles (the starter data plus a `:free` twin).
 */

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { createServer, type IncomingMessage, type Server } from 'node:http';
import { createServer as createNetServer, type AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { runLogin, runLogout, runWhoami } from '../src/auth/commands.js';
import type { AuthDeps } from '../src/auth/flow.js';
import { isoUtc } from '../src/auth/store.js';
import {
  BUNDLE_DATA_FILES,
  BundleError,
  bundleFromTexts,
  loadBundle,
  resolveBundle,
  STARTER_BUNDLE_DIR,
  starterBundle,
} from '../src/catalog/bundle.js';
import * as client from '../src/catalog/client.js';
import { centroidFilePath, createDefaultConfig } from '../src/config.js';
import * as daemon from '../src/daemon.js';
import { ModelRegistry } from '../src/registry/models.js';
import { Router, type RouteResult } from '../src/router.js';
import { Priorities } from '../src/scoring/priorities.js';
import { serve, type RouterLike } from '../src/server.js';
import { REPO_ROOT } from './_catalog.js';
import {
  OTHER_KEY_ID,
  OTHER_SEED,
  TEST_KEY_ID,
  TRUSTED_KEYS_ENV,
  keyEntry,
  signManifest,
  writeTrustedKeys,
} from './_signing.js';

const T0 = Date.parse('2026-10-03T12:00:00Z');
const DAY = 24 * 3600 * 1000;
const V1 = '2026.10.04.1';
const V2 = '2026.10.05.1';
const DEAD = 'http://127.0.0.1:9';
const NUDGE =
  'Routing on the starter catalog (45 models). Log in for free to use the ' +
  'full catalog (322 models): tryaii login\n';

// ---------------------------------------------------------------- bundles
function makeFullBundle(dest: string, version: string, signed = true): string {
  mkdirSync(dest, { recursive: true });
  const texts: Record<string, string> = {};
  for (const name of BUNDLE_DATA_FILES) {
    texts[name] = readFileSync(join(STARTER_BUNDLE_DIR, name), 'utf-8');
  }
  const models = JSON.parse(texts['models.json']) as { models: Array<Record<string, unknown>> };
  models.models.push({ ...models.models[0], model_id: `${String(models.models[0].model_id)}:free` });
  texts['models.json'] = JSON.stringify(models); // any exact text works: clients hash what they get
  const starter = JSON.parse(readFileSync(join(STARTER_BUNDLE_DIR, 'manifest.json'), 'utf-8'));
  const files: Record<string, string> = {};
  for (const name of BUNDLE_DATA_FILES) {
    files[name] = createHash('sha256').update(Buffer.from(texts[name], 'utf-8')).digest('hex');
    writeFileSync(join(dest, name), texts[name], 'utf-8');
  }
  const manifest = {
    schema: 1, kind: 'full', version,
    embedding_model: starter.embedding_model,
    created_at: '2026-10-04T00:00:00Z',
    counts: { models: models.models.length, benchmarks: starter.counts.benchmarks },
    files, signature: null, key_id: null,
  };
  if (signed) signManifest(manifest); // catalog contract section 6 (test key)
  writeFileSync(join(dest, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n', 'utf-8');
  return dest;
}

let bundleRoot: string;
const bundles: Record<string, string> = {};

beforeAll(() => {
  bundleRoot = mkdtempSync(join(tmpdir(), 'tryaii-bundles-'));
  bundles[V1] = makeFullBundle(join(bundleRoot, V1), V1);
  bundles[V2] = makeFullBundle(join(bundleRoot, V2), V2);
});

afterAll(() => rmSync(bundleRoot, { recursive: true, force: true }));

// ---------------------------------------------------------------- fake API
type Mode =
  | 'ok' | '401' | '403' | '404' | '429' | '500' | 'corrupt' | 'schema'
  // right hashes, wrong shape (benchmarks.json "{}") / a non-string files value
  | 'malformed' | 'badtype'
  // catalog contract section 6: no signature / one flipped bit / signed by a
  // key outside the trusted list / a manifest field changed after signing
  | 'unsigned' | 'badsig' | 'unknownkey' | 'tampered';

interface Req {
  method: string;
  path: string;
  headers: IncomingMessage['headers'];
}

class FakeApi {
  server: Server;
  url = '';
  requests: Req[] = [];
  mode: Mode = 'ok';
  catalogDir: string | null = null;
  entitlements = ['catalog:full'];
  refreshOutcome: 'ok' | 'invalid_grant' | 'rate_limited' = 'ok';
  meOutcome: 'ok' | '401' = 'ok';
  private n = 0;
  private access = new Set<string>();
  private refresh = new Set<string>();

  constructor() {
    this.server = createServer((req, res) => {
      let raw = '';
      req.on('data', (c) => (raw += c));
      req.on('end', () => {
        const path = req.url ?? '';
        this.requests.push({ method: req.method ?? '', path, headers: req.headers });
        const [status, body, headers] = this.handle(req, path, raw);
        let data: Buffer = body === null ? Buffer.alloc(0) : Buffer.from(JSON.stringify(body), 'utf-8');
        const h: Record<string, string> = { ...headers };
        if (body !== null) h['Content-Type'] = 'application/json';
        if (data.length && String(req.headers['accept-encoding'] ?? '').includes('gzip')) {
          data = gzipSync(data);
          h['Content-Encoding'] = 'gzip';
        }
        h['Content-Length'] = String(data.length);
        res.writeHead(status, h);
        res.end(data);
      });
    });
  }

  async start(): Promise<this> {
    await new Promise<void>((ok) => this.server.listen(0, '127.0.0.1', () => ok()));
    this.url = `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`;
    return this;
  }

  async stop(): Promise<void> {
    this.server.closeAllConnections?.();
    await new Promise<void>((ok) => this.server.close(() => ok()));
  }

  issue(expiresIn = 3600): Record<string, unknown> {
    this.n += 1;
    const access = `access-${this.n}`;
    const refresh = `tair_refresh-${this.n}`;
    this.access.add(access);
    this.refresh.add(refresh);
    return {
      access_token: access, token_type: 'Bearer', expires_in: expiresIn,
      refresh_token: refresh, refresh_expires_in: 7776000,
      user: { id: 'u1', email: 'dev@example.com', name: 'Dev User' },
      entitlements: this.entitlements,
    };
  }

  catalogCalls(): Req[] {
    return this.requests.filter((r) => r.path === '/v1/catalog/live');
  }

  private handle(
    req: IncomingMessage,
    path: string,
    raw: string,
  ): [number, unknown, Record<string, string>] {
    const auth = String(req.headers.authorization ?? '');
    const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
    if (req.method === 'POST' && path === '/v1/auth/token') {
      if (this.refreshOutcome === 'rate_limited') return [429, { error: 'slow_down' }, {}];
      const body = JSON.parse(raw || '{}');
      if (this.refreshOutcome === 'invalid_grant' || !this.refresh.has(body.refresh_token)) {
        return [400, { error: 'invalid_grant' }, {}];
      }
      this.refresh.delete(body.refresh_token);
      return [200, this.issue(), {}];
    }
    if (req.method === 'POST' && path === '/v1/auth/revoke') return [200, {}, {}];
    if (req.method === 'GET' && path === '/v1/auth/me') {
      if (this.meOutcome === '401' || !this.access.has(token)) return [401, { error: 'invalid_token' }, {}];
      return [200, {
        user: { id: 'u1', email: 'dev@example.com', name: 'Dev User' },
        entitlements: this.entitlements,
        session: { id: 'fam', created_at: '2026-10-03T12:00:00Z' },
      }, {}];
    }
    if (req.method === 'GET' && path === '/v1/catalog/live') {
      if (this.mode === '401' || !this.access.has(token)) return [401, { error: 'invalid_token' }, {}];
      if (this.mode === '403' || !this.entitlements.includes('catalog:full')) {
        return [403, { error: 'insufficient_entitlement' }, {}];
      }
      if (this.mode === '429') return [429, { error: 'slow_down' }, { 'Retry-After': '120' }];
      if (this.mode === '500') return [500, { error: 'release_corrupt' }, {}];
      if (this.mode === '404' || !this.catalogDir) return [404, { error: 'no_live_release' }, {}];
      const manifest = JSON.parse(readFileSync(join(this.catalogDir, 'manifest.json'), 'utf-8'));
      const files: Record<string, unknown> = {};
      for (const name of BUNDLE_DATA_FILES) {
        files[name] = readFileSync(join(this.catalogDir, name), 'utf-8');
      }
      if (this.mode === 'corrupt') files['models.json'] = (files['models.json'] as string).replace('{', '{ ');
      if (this.mode === 'schema') manifest.schema = 99;
      if (this.mode === 'malformed') {
        files['benchmarks.json'] = '{}';
        manifest.files['benchmarks.json'] = createHash('sha256').update('{}').digest('hex');
      }
      // A validly signed but unusable bundle (tests the schema / shape rules).
      if ((this.mode === 'schema' || this.mode === 'malformed') && manifest.signature) {
        signManifest(manifest);
      }
      if (this.mode === 'badtype') files['models.json'] = 10 ** 12;
      if (this.mode === 'unsigned') {
        manifest.signature = null;
        manifest.key_id = null;
      }
      if (this.mode === 'badsig') {
        const raw = Buffer.from(String(manifest.signature), 'base64url');
        raw[40] ^= 0x01;
        manifest.signature = raw.toString('base64url');
      }
      if (this.mode === 'unknownkey') signManifest(manifest, { seed: OTHER_SEED, keyId: OTHER_KEY_ID });
      if (this.mode === 'tampered') manifest.created_at = '2026-10-04T00:00:01Z';
      const etag = `"${String(manifest.version).trim()}"`; // header-safe
      if (req.headers['if-none-match'] === etag) return [304, null, { ETag: etag }];
      return [200, { manifest, files }, { ETag: etag, 'Cache-Control': 'private, no-store' }];
    }
    return [404, { error: 'not_found' }, {}];
  }
}

// ---------------------------------------------------------------- sandbox
let dir: string;
let api: FakeApi;
let clock: number;
let env: NodeJS.ProcessEnv;
const savedEnv = { ...process.env };

function deps(extra: Partial<AuthDeps> = {}): client.CatalogDeps {
  return { version: '9.9.9', env, now: () => clock, ...extra };
}

/** Store credentials for a fresh session on the fake server. */
function loginTo(expiresIn = 3600 * 24 * 10): void {
  const token = api.issue(expiresIn) as Record<string, any>;
  const creds = {
    version: 1, api_url: api.url, user: token.user, entitlements: token.entitlements,
    refresh_token: token.refresh_token, refresh_expires_at: isoUtc(clock + 90 * DAY),
    access_token: token.access_token, access_expires_at: isoUtc(clock + expiresIn * 1000),
    created_at: isoUtc(clock),
  };
  mkdirSync(join(dir, 'data'), { recursive: true });
  writeFileSync(join(dir, 'data', 'credentials.json'), JSON.stringify(creds, null, 2) + '\n');
}

function creds(): Record<string, any> | null {
  const p = join(dir, 'data', 'credentials.json');
  return existsSync(p) ? JSON.parse(readFileSync(p, 'utf-8')) : null;
}

function cacheVersions(): string[] {
  const base = client.fullDir(env);
  return existsSync(base) ? readdirSync(base).sort() : [];
}

function stateJson(): Record<string, unknown> {
  return JSON.parse(readFileSync(client.statePath(env), 'utf-8'));
}

function expectEverythingGone(): void {
  expect(creds()).toBeNull();
  expect(existsSync(client.fullDir(env))).toBe(false);
  expect(existsSync(client.statePath(env))).toBe(false);
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), 'tryaii-catalog-'));
  // A stored session never talks to TRYAII_API_URL (auth v1.1 item 13).
  env = {
    TRYAII_DRE_DATA_DIR: join(dir, 'data'),
    TRYAII_API_URL: DEAD,
    // catalog contract section 6: trust the tests' signing key (_setup-data-dir.ts)
    [TRUSTED_KEYS_ENV]: process.env[TRUSTED_KEYS_ENV],
  };
  // The library paths (Router, resolveBundle) read process.env.
  process.env.TRYAII_DRE_DATA_DIR = env.TRYAII_DRE_DATA_DIR;
  delete process.env.TRYAII_NO_BANNER;
  clock = T0;
  api = await new FakeApi().start();
  api.catalogDir = bundles[V1];
  client.resetCatalogCache();
});

afterEach(async () => {
  await api.stop();
  client.resetCatalogCache();
  process.env.TRYAII_DRE_DATA_DIR = savedEnv.TRYAII_DRE_DATA_DIR;
  rmSync(dir, { recursive: true, force: true });
});

// ---------------------------------------------------------------- which catalog
describe('which catalog (contract section 5)', () => {
  it('not logged in: starter with the nudge notice, no network', async () => {
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.notice, sel.checked]).toEqual(['starter', 'not_logged_in', false]);
    expect(api.requests).toHaveLength(0);
  });

  it('starter mode never touches the network', async () => {
    loginTo();
    const sel = await client.selectCatalog('starter', deps());
    expect([sel.bundle.kind, sel.notice]).toEqual(['starter', null]);
    expect(api.requests).toHaveLength(0);
  });

  it('full mode requires a login', async () => {
    await expect(client.selectCatalog('full', deps())).rejects.toThrow(
      new client.LoginRequiredError(),
    );
    await expect(client.selectCatalog('full', deps())).rejects.toThrow(
      'Not logged in. Run: tryaii login',
    );
  });

  it('rejects an unknown mode', async () => {
    await expect(client.selectCatalog('private' as client.CatalogMode, deps())).rejects.toThrow();
  });

  it('first check downloads, verifies and caches the bundle', async () => {
    loginTo();
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.bundle.version, sel.notice, sel.checked]).toEqual([
      'full', V1, null, true,
    ]);
    const [call] = api.catalogCalls();
    expect(call.headers['if-none-match']).toBeUndefined();
    expect(call.headers.authorization).toBe(`Bearer ${creds()!.access_token}`);
    expect(String(call.headers['accept-encoding'])).toContain('gzip');
    expect(call.headers['user-agent']).toBe('tryaii/9.9.9');
    const vdir = join(client.fullDir(env), V1);
    expect(readdirSync(vdir).sort()).toEqual([...BUNDLE_DATA_FILES, 'manifest.json'].sort());
    for (const name of BUNDLE_DATA_FILES) {
      expect(readFileSync(join(vdir, name))).toEqual(readFileSync(join(bundles[V1], name)));
    }
    expect(JSON.parse(readFileSync(join(vdir, 'manifest.json'), 'utf-8'))).toEqual(
      JSON.parse(readFileSync(join(bundles[V1], 'manifest.json'), 'utf-8')),
    );
    // Same bytes as the Python SDK writes.
    expect(readFileSync(client.statePath(env), 'utf-8')).toBe(
      `{"version": "${V1}", "checked_at": "2026-10-03T12:00:00Z"}`,
    );
    expect(sel.bundle.directory).toBe(vdir);
  });

  it('within 24 h the cache is used without asking', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    clock = T0 + DAY - 1000;
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.version, sel.checked]).toEqual([V1, false]);
    expect(api.catalogCalls()).toHaveLength(1);
  });

  it('after 24 h a 304 (If-None-Match) touches checked_at', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    clock = T0 + DAY;
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.version, sel.checked]).toEqual([V1, true]);
    const calls = api.catalogCalls();
    expect(calls).toHaveLength(2);
    expect(calls[1].headers['if-none-match']).toBe(`"${V1}"`);
    expect(stateJson().checked_at).toBe(isoUtc(T0 + DAY));
    expect(cacheVersions()).toEqual([V1]);
  });

  it('a new release replaces the old version directory', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    api.catalogDir = bundles[V2];
    clock = T0 + DAY + 5000;
    const sel = await client.selectCatalog('auto', deps());
    expect(sel.bundle.version).toBe(V2);
    expect(cacheVersions()).toEqual([V2]);
    expect(stateJson().version).toBe(V2);
  });

  it('a checked_at in the future counts as stale', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    client.writeState(V1, T0 + 3 * DAY, env);
    await client.selectCatalog('auto', deps());
    expect(api.catalogCalls()).toHaveLength(2);
  });

  it('an expired access token is refreshed first and persisted', async () => {
    loginTo(0);
    const old = creds()!;
    const sel = await client.selectCatalog('auto', deps());
    expect(sel.bundle.kind).toBe('full');
    expect(api.requests.map((r) => r.path)).toEqual(['/v1/auth/token', '/v1/catalog/live']);
    const now = creds()!;
    expect(now.refresh_token).not.toBe(old.refresh_token);
    expect(api.catalogCalls()[0].headers.authorization).toBe(`Bearer ${now.access_token}`);
  });

  it('the stored api_url wins over TRYAII_API_URL', async () => {
    loginTo();
    env.TRYAII_API_URL = DEAD;
    expect((await client.selectCatalog('auto', deps())).bundle.kind).toBe('full');
    expect(api.catalogCalls()).toHaveLength(1);
  });
});

describe('session ended (contract section 5)', () => {
  it('refresh invalid_grant ends the session and drops the cache', async () => {
    loginTo(0);
    await client.selectCatalog('auto', deps());
    api.refreshOutcome = 'invalid_grant';
    clock = T0 + DAY;
    // The stored access token is expired by then (expires_in 3600 from the refresh).
    await expect(client.selectCatalog('auto', deps())).rejects.toThrow(
      'Your session has ended. Run: tryaii login',
    );
    expectEverythingGone();
  });

  it('a catalog 401 ends the session; afterwards simply logged out', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    api.mode = '401';
    clock = T0 + DAY;
    await expect(client.selectCatalog('auto', deps())).rejects.toBeInstanceOf(
      client.SessionEndedError,
    );
    expectEverythingGone();
    expect((await client.selectCatalog('auto', deps())).notice).toBe('not_logged_in');
  });

  it('a rate-limited refresh keeps the session (download failure)', async () => {
    loginTo(0);
    api.refreshOutcome = 'rate_limited';
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.notice]).toEqual(['starter', 'download_failed']);
    expect(creds()).not.toBeNull();
  });
});

describe('403 insufficient_entitlement (contract section 5)', () => {
  it('starter + notice, remembered for 24 h without asking again', async () => {
    api.entitlements = [];
    loginTo();
    let sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.notice]).toEqual(['starter', 'no_entitlement']);
    expect(stateJson().version).toBeNull();
    clock = T0 + 60_000;
    sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.notice, sel.checked]).toEqual(['starter', 'no_entitlement', false]);
    expect(api.catalogCalls()).toHaveLength(1);
    expect(creds()).not.toBeNull();
    expect(client.noticeMessage('no_entitlement')).toBe(
      'This account does not have access to the full catalog; using the starter catalog.',
    );
  });

  it('drops a previously cached full catalog', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    api.mode = '403';
    clock = T0 + DAY;
    expect((await client.selectCatalog('auto', deps())).notice).toBe('no_entitlement');
    expect(cacheVersions()).toEqual([]);
  });
});

describe('download failures (contract section 5)', () => {
  const failing: Array<Mode | 'network'> = ['404', '429', '500', 'corrupt', 'network'];

  function breakIt(mode: Mode | 'network'): client.CatalogDeps {
    if (mode === 'network') {
      const fetchFn = (async (url: unknown, init: unknown) => {
        if (String(url).endsWith('/v1/catalog/live')) throw new TypeError('fetch failed');
        return fetch(url as string, init as RequestInit);
      }) as typeof fetch;
      return deps({ fetchFn });
    }
    api.mode = mode;
    return deps();
  }

  for (const mode of failing) {
    it(`${mode} without a cache: starter + download notice, state untouched`, async () => {
      loginTo();
      const sel = await client.selectCatalog('auto', breakIt(mode));
      expect([sel.bundle.kind, sel.notice]).toEqual(['starter', 'download_failed']);
      expect(existsSync(client.statePath(env))).toBe(false);
      expect(creds()).not.toBeNull();
      expect(cacheVersions()).toEqual([]);
    });
  }

  for (const mode of [...failing, 'schema' as Mode]) {
    it(`${mode} with a cache: the cached catalog, silently`, async () => {
      loginTo();
      await client.selectCatalog('auto', deps());
      const before = readFileSync(client.statePath(env), 'utf-8');
      api.catalogDir = bundles[V2]; // a newer release (not a 304) that fails to arrive
      const d = breakIt(mode);
      clock = T0 + DAY;
      const sel = await client.selectCatalog('auto', d);
      expect([sel.bundle.kind, sel.bundle.version, sel.notice]).toEqual(['full', V1, null]);
      expect(readFileSync(client.statePath(env), 'utf-8')).toBe(before);
    });
  }

  it('schema too new without a cache has its own notice', async () => {
    loginTo();
    api.mode = 'schema';
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.notice]).toEqual(['starter', 'schema_too_new']);
    expect(client.noticeMessage(sel.notice)).toBe(
      'The full catalog needs a newer tryaii version; using the starter catalog.',
    );
    expect(client.noticeMessage('download_failed')).toBe(
      'Could not download the full catalog; using the starter catalog for now.',
    );
  });
});

describe('counts and nudge (contract sections 1 and 5)', () => {
  it('routable count skips :free variants; starter full_counts are routable', () => {
    const full = loadBundle(bundles[V1]);
    expect(full.counts!.models).toBe(46);
    expect(client.routableCount(full)).toBe(45);
    expect(client.routableCount(starterBundle())).toBe(45);
    expect(starterBundle().fullCounts).toEqual({ models: 322, benchmarks: 33 });
  });

  it('nudge: once per local day, exact text, nudge.json', async () => {
    const lines: string[] = [];
    const sel = await client.selectCatalog('auto', deps());
    expect(client.maybeNudge(sel, (t) => lines.push(t), deps())).toBe(true);
    expect(client.maybeNudge(sel, (t) => lines.push(t), deps())).toBe(false);
    expect(lines).toEqual([NUDGE]);
    const d = new Date(T0);
    const pad = (n: number) => String(n).padStart(2, '0');
    const today = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    expect(readFileSync(client.nudgePath(env), 'utf-8')).toBe(`{"last_shown": "${today}"}`);
    clock = T0 + DAY;
    expect(client.maybeNudge(sel, (t) => lines.push(t), deps())).toBe(true);
    expect(lines).toHaveLength(2);
  });

  it('nudge: suppressed by TRYAII_NO_BANNER', async () => {
    env.TRYAII_NO_BANNER = '1';
    const lines: string[] = [];
    const sel = await client.selectCatalog('auto', deps());
    expect(client.maybeNudge(sel, (t) => lines.push(t), deps())).toBe(false);
    expect(lines).toEqual([]);
    expect(existsSync(client.nudgePath(env))).toBe(false);
  });

  it('nudge: only when not logged in', () => {
    const lines: string[] = [];
    for (const notice of [null, 'no_entitlement', 'download_failed'] as const) {
      const sel = { bundle: starterBundle(), notice, checked: false };
      expect(client.maybeNudge(sel, (t) => lines.push(t), deps())).toBe(false);
    }
    expect(lines).toEqual([]);
  });
});

describe('library: catalog option (contract section 5)', () => {
  it('Router catalog option and resolveBundle', async () => {
    expect(new Router({ catalog: 'starter' }).bundle.kind).toBe('starter');
    expect(new Router().bundle.kind).toBe('starter');
    expect(() => new Router({ catalog: 'full' })).toThrow(client.LoginRequiredError);
    expect(() => new Router({ catalog: 'everything' as client.CatalogMode })).toThrow();
    expect(resolveBundle(null, 'starter').kind).toBe('starter');

    loginTo();
    // sync constructor: local cache only (nothing cached yet) ...
    const router = new Router();
    expect(router.bundle.kind).toBe('starter');
    expect(api.catalogCalls()).toHaveLength(0);
    // ... ready() (and route()) completes the check and switches.
    await router.ready();
    expect([router.bundle.kind, router.bundle.version]).toEqual(['full', V1]);
    expect(router.models.allModels).toHaveLength(45);
    // now cached + memoized: new routers, registries and resolveBundle agree
    expect(new Router().bundle.version).toBe(V1);
    expect(ModelRegistry.default().allModels).toHaveLength(45);
    expect(new Router({ catalog: 'full' }).bundle.kind).toBe('full');
    // an explicit bundle wins over the catalog option
    expect(new Router({ bundle: starterBundle(), catalog: 'full' }).bundle.kind).toBe('starter');
    expect(api.catalogCalls()).toHaveLength(1);
  });

  it('library session end surfaces as SessionEndedError from ready()', async () => {
    loginTo(0);
    api.refreshOutcome = 'invalid_grant';
    await expect(new Router().ready()).rejects.toBeInstanceOf(client.SessionEndedError);
  });
});

// ---------------------------------------------------------------- CLI bodies
function capture() {
  const io = { out: '', err: '' };
  return {
    io,
    w: {
      stdout: (t: string) => void (io.out += t),
      stderr: (t: string) => void (io.err += t),
    },
  };
}

describe('login / logout / whoami (contract section 5, CLI changes)', () => {
  const instantSleep = async () => undefined;

  async function login(): Promise<string> {
    // device flow against the fake: approve on the first poll
    let polled = false;
    const fetchFn = (async (url: unknown, init: any) => {
      const u = String(url);
      if (u.endsWith('/v1/auth/device/code')) {
        return new Response(JSON.stringify({
          device_code: 'dc', user_code: 'BCDF-GHJK',
          verification_uri: 'https://tryaii.com/device',
          verification_uri_complete: 'https://tryaii.com/device?user_code=BCDF-GHJK',
          expires_in: 600, interval: 1,
        }), { status: 200 });
      }
      if (u.endsWith('/v1/auth/token') && !polled && init.body.includes('device_code')) {
        polled = true;
        return new Response(JSON.stringify(api.issue()), { status: 200 });
      }
      return fetch(u, init);
    }) as typeof fetch;
    env.TRYAII_API_URL = api.url;
    const { io, w } = capture();
    expect(await runLogin(w, { ...deps({ fetchFn, sleep: instantSleep }), version: '9.9.9' }))
      .toBe(0);
    expect(io.err).toBe('');
    return io.out;
  }

  it('login downloads the full catalog right away', async () => {
    const out = await login();
    expect(out.endsWith(
      'Logged in as dev@example.com.\nDownloaded the full catalog (45 models).\n',
    )).toBe(true);
    expect(cacheVersions()).toEqual([V1]);
  });

  for (const setup of [
    () => (api.entitlements = []),
    () => (api.mode = '500'),
    () => (api.catalogDir = null),
    () => (api.mode = 'schema'),
  ]) {
    it(`login: any download failure prints the failure line, exit 0 (${setup.toString()})`, async () => {
      setup();
      const out = await login();
      expect(out.endsWith(
        'Logged in as dev@example.com.\n' +
          'Could not download the full catalog now; it will be fetched on next use.\n',
      )).toBe(true);
      expect(creds()).not.toBeNull();
    });
  }

  it('whoami third line: not downloaded yet, then the cached release', async () => {
    loginTo();
    let { io, w } = capture();
    expect(await runWhoami(w, { ...deps(), version: '9.9.9' }, { json: false })).toBe(0);
    expect(io.out.endsWith('Catalog: not downloaded yet\n')).toBe(true);
    await client.selectCatalog('auto', deps());
    ({ io, w } = capture());
    expect(await runWhoami(w, { ...deps(), version: '9.9.9' }, { json: false })).toBe(0);
    expect(io.out).toBe(
      'Logged in as dev@example.com (Dev User)\nEntitlements: catalog:full\n' +
        `Catalog: full, release ${V1} (45 models)\n`,
    );
    ({ io, w } = capture());
    expect(await runWhoami(w, { ...deps(), version: '9.9.9' }, { json: true })).toBe(0);
    expect(io.out).not.toContain('Catalog');
  });

  it('whoami session ended drops the cache too', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    api.meOutcome = '401';
    const { w } = capture();
    expect(await runWhoami(w, { ...deps(), version: '9.9.9' }, { json: false })).toBe(1);
    expectEverythingGone();
  });

  for (const revokeOk of [true, false]) {
    it(`logout deletes catalog/full and state.json (revoke ${revokeOk ? 'ok' : 'fails'})`, async () => {
      loginTo();
      await client.selectCatalog('auto', deps());
      writeFileSync(client.nudgePath(env), '{"last_shown": "2026-10-03"}');
      const fetchFn = (async (url: unknown, init: unknown) => {
        if (!revokeOk && String(url).endsWith('/v1/auth/revoke')) throw new TypeError('down');
        return fetch(url as string, init as RequestInit);
      }) as typeof fetch;
      const { io, w } = capture();
      expect(await runLogout(w, { ...deps({ fetchFn }), version: '9.9.9' })).toBe(0);
      expect(io.out).toBe('Logged out.\n');
      expectEverythingGone();
      expect(existsSync(client.nudgePath(env))).toBe(true);
    });
  }
});

// ---------------------------------------------------------------- daemon / centroids
describe('daemon and centroid cache follow the catalog', () => {
  it('daemon catalog key / spec round-trip; another catalog is not live', async () => {
    const config = createDefaultConfig({ dataDir: join(dir, 'd') });
    const starter = starterBundle();
    expect(daemon.catalogKey(starter)).toBe(`starter:${starter.version}`);
    expect(daemon.catalogSpec(starter)).toBe('starter');
    expect(daemon.bundleFromSpec('starter')).toBe(starter);
    expect(daemon.bundleFromSpec('')).toBe(starter);
    const full = loadBundle(bundles[V1]);
    expect(daemon.catalogSpec(full)).toBe(bundles[V1]);
    expect(daemon.bundleFromSpec(bundles[V1]).version).toBe(V1);
    mkdirSync(config.dataDir, { recursive: true });
    daemon.writeState(config, {
      runtime: 'node', version: '0', embeddingModel: config.embeddingModel, catalog: 'starter:x',
      host: '127.0.0.1', port: 9, token: 't', pid: 0, startedAtMs: 0,
    });
    expect(await daemon.liveState(config, `full:${V1}`)).toBeNull();
  });

  it('the daemon records the catalog it was handed', async () => {
    const config = createDefaultConfig({ dataDir: join(dir, 'd2') });
    const fake: RouterLike = { route: async () => { throw new Error('unused'); } };
    const closed = serve(
      { embeddingModel: config.embeddingModel, dataDir: config.dataDir },
      { idleTimeout: 0, router: fake, log: () => undefined, catalog: bundles[V1] },
    );
    let state = null;
    for (let i = 0; i < 100 && !state; i++) {
      state = await daemon.liveState(config, `full:${V1}`);
      if (!state) await new Promise((r) => setTimeout(r, 50));
    }
    try {
      expect(state?.catalog).toBe(`full:${V1}`);
      expect((await daemon.status(config))?.catalog).toBe(`full:${V1}`);
      expect(await daemon.liveState(config, `starter:${starterBundle().version}`)).toBeNull();
    } finally {
      await daemon.stop(config);
      await closed;
    }
  });

  it('centroid cache path is keyed by catalog kind + version', () => {
    const config = createDefaultConfig({ dataDir: dir, embeddingModel: 'org/other-model' });
    const starter = starterBundle();
    const full = loadBundle(bundles[V1]);
    const a = centroidFilePath(config, starter);
    const b = centroidFilePath(config, full);
    expect(a).not.toBe(b);
    expect(a.endsWith(`centroids_org__other-model__starter-${starter.version}.json`)).toBe(true);
    expect(b.endsWith(`centroids_org__other-model__full-${V1}.json`)).toBe(true);
    // Same names as the Python SDK's TryaiiDreConfig.centroid_file_for.
    expect(centroidFilePath(config).endsWith('centroids_org__other-model.json')).toBe(true);
  });
});

// ---------------------------------------------------------------- review fixes
const DIST_CLI = fileURLToPath(new URL('../dist/cli.js', import.meta.url));
const VERIFY_DIST = fileURLToPath(new URL('../scripts/verify-dist.mjs', import.meta.url));
const HAS_DIST = existsSync(DIST_CLI);
const DIST_ONLY = '[needs dist/: npm run build]';
const INVENTORY = join(REPO_ROOT, 'shared', 'diagnose', 'fixtures', 'corpus', 'inventory-mixed.json');

/** Run a node script asynchronously (the fake API serves from this process). */
function runNode(
  args: string[],
  extraEnv: Record<string, string>,
): Promise<{ code: number | null; stdout: string; stderr: string }> {
  const childEnv: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined && !k.toUpperCase().startsWith('TRYAII_')) childEnv[k] = v;
  }
  Object.assign(childEnv, {
    TRYAII_DRE_DATA_DIR: env.TRYAII_DRE_DATA_DIR!,
    TRYAII_API_URL: DEAD,
    TRYAII_NO_DAEMON: '1',
    TRYAII_NO_BANNER: '1',
    // catalog contract section 6: trust the tests' signing key
    [TRUSTED_KEYS_ENV]: process.env[TRUSTED_KEYS_ENV]!,
    ...extraEnv,
  });
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { env: childEnv, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', (d) => (stdout += d));
    child.stderr.setEncoding('utf8').on('data', (d) => (stderr += d));
    child.on('error', reject);
    child.on('close', (code) =>
      resolve({ code, stdout: stdout.replace(/\r\n/g, '\n'), stderr: stderr.replace(/\r\n/g, '\n') }),
    );
  });
}

describe('finding 1: a malformed 200 is a failed download, never a crash', () => {
  for (const mode of ['malformed', 'badtype'] as const) {
    it(`${mode} without a cache: starter + download notice`, async () => {
      loginTo();
      api.mode = mode;
      const sel = await client.selectCatalog('auto', deps());
      expect([sel.bundle.kind, sel.notice, sel.failed]).toEqual(['starter', 'download_failed', true]);
      expect(cacheVersions()).toEqual([]);
      expect(existsSync(client.statePath(env))).toBe(false);
    });

    it(`${mode} with a cache: the cached catalog`, async () => {
      loginTo();
      await client.selectCatalog('auto', deps());
      api.catalogDir = bundles[V2];
      api.mode = mode;
      clock = T0 + DAY;
      const sel = await client.selectCatalog('auto', deps());
      expect([sel.bundle.kind, sel.bundle.version, sel.notice]).toEqual(['full', V1, null]);
      expect(cacheVersions()).toEqual([V1]);
    });
  }

  it('bundleFromTexts throws BundleError for non-text values and wrong shapes', () => {
    const texts: Record<string, string> = {};
    for (const name of BUNDLE_DATA_FILES) {
      texts[name] = readFileSync(join(STARTER_BUNDLE_DIR, name), 'utf-8');
    }
    const manifest = JSON.parse(readFileSync(join(STARTER_BUNDLE_DIR, 'manifest.json'), 'utf-8'));
    for (const bad of [10 ** 12, { a: 1 }, null]) {
      expect(() =>
        bundleFromTexts(manifest, { ...texts, 'models.json': bad as unknown as string }),
      ).toThrow(BundleError);
    }
    manifest.files['benchmarks.json'] = createHash('sha256').update('{}').digest('hex');
    expect(() => bundleFromTexts(manifest, { ...texts, 'benchmarks.json': '{}' })).toThrow(
      /malformed/,
    );
  });
});

describe('finding 2: the library memo retries failed checks after a short backoff', () => {
  it('download_failed is retried after FAILURE_RETRY_MS; a success is kept', async () => {
    loginTo();
    api.mode = '500';
    let now = Date.now();
    const spy = vi.spyOn(Date, 'now').mockImplementation(() => now);
    try {
      expect((await client.selectedBundle()).kind).toBe('starter');
      api.mode = 'ok';
      now += client.FAILURE_RETRY_MS - 1000;
      expect((await client.selectedBundle()).kind).toBe('starter'); // backoff
      expect(client.selectedBundleSync().kind).toBe('starter');
      expect(api.catalogCalls()).toHaveLength(1);
      now += 2000;
      expect((await client.selectedBundle()).version).toBe(V1); // retried
      expect(api.catalogCalls()).toHaveLength(2);
      now += client.CHECK_INTERVAL_MS - 10_000;
      expect((await client.selectedBundle()).version).toBe(V1);
      expect(api.catalogCalls()).toHaveLength(2);
    } finally {
      spy.mockRestore();
    }
  });

  it('no_entitlement is a final answer, kept for 24 h', async () => {
    loginTo();
    api.entitlements = [];
    let now = Date.now();
    const spy = vi.spyOn(Date, 'now').mockImplementation(() => now);
    try {
      expect((await client.selectedBundle()).kind).toBe('starter');
      now += client.FAILURE_RETRY_MS * 10;
      expect((await client.selectedBundle()).kind).toBe('starter');
      expect(api.catalogCalls()).toHaveLength(1);
    } finally {
      spy.mockRestore();
    }
  });
});

describe.skipIf(!HAS_DIST)(`finding 3: diagnose check selects the catalog once ${DIST_ONLY}`, () => {
  const argv = (): string[] => [
    DIST_CLI, 'diagnose', 'check', INVENTORY, '--json', '--run-id', 'r',
    '--now', '2026-01-01T00:00:00Z', '--out-dir', join(dir, 'out'),
  ];

  it('session ended: the contract line, exit 1', async () => {
    loginTo();
    api.mode = '401';
    const r = await runNode(argv(), {});
    expect([r.code, r.stdout, r.stderr]).toEqual([1, '', 'Your session has ended. Run: tryaii login\n']);
    expectEverythingGone();
  }, 60_000);

  it('a failed download: one check, one line, the report still runs', async () => {
    loginTo();
    api.mode = '500';
    const r = await runNode(argv(), {});
    expect([r.code, r.stderr]).toEqual([
      0, 'Could not download the full catalog; using the starter catalog for now.\n',
    ]);
    expect(JSON.parse(r.stdout).summary.site_count).toBeGreaterThan(0);
    expect(api.catalogCalls()).toHaveLength(1);
  }, 60_000);
});

describe('finding 4: daemon replacement races and mid-request failures', () => {
  it('stop() keeps a daemon that is already on the wanted catalog', async () => {
    const config = createDefaultConfig({ dataDir: join(dir, 'd4') });
    mkdirSync(config.dataDir, { recursive: true });
    daemon.writeState(config, {
      runtime: 'node', version: '0', embeddingModel: config.embeddingModel, catalog: `full:${V1}`,
      host: '127.0.0.1', port: 9, token: 't', pid: 0, startedAtMs: 0,
    });
    expect(await daemon.stop(config, { keepCatalog: `full:${V1}` })).toBe(false);
    expect(daemon.readState(config)?.catalog).toBe(`full:${V1}`);
    await daemon.stop(config, { keepCatalog: 'starter:x' });
    expect(daemon.readState(config)).toBeNull();
  });

  it('ensureDaemon never stops a daemon a concurrent CLI just started on our catalog', async () => {
    const config = createDefaultConfig({ dataDir: join(dir, 'd5') });
    mkdirSync(config.dataDir, { recursive: true });
    const full = loadBundle(bundles[V1]);
    const key = daemon.catalogKey(full);
    const seen: string[] = [];
    let base: daemon.DaemonState | null = null;
    const fake = createNetServer((sock) => {
      let buf = '';
      sock.on('data', (chunk) => {
        buf += chunk.toString('utf-8');
        const nl = buf.indexOf('\n');
        if (nl === -1) return;
        const cmd = JSON.parse(buf.slice(0, nl)).cmd as string;
        seen.push(cmd);
        // While we look at the old daemon, a concurrent CLI replaces it with
        // one on the catalog we want (same address, for the test).
        if (cmd === 'ping' && seen.length === 1) daemon.writeState(config, { ...base!, catalog: key });
        sock.end(JSON.stringify({ ok: true }) + '\n');
      });
    });
    await new Promise<void>((ok) => fake.listen(0, '127.0.0.1', () => ok()));
    try {
      base = {
        runtime: 'node', version: '0', embeddingModel: config.embeddingModel, catalog: 'starter:x',
        host: '127.0.0.1', port: (fake.address() as AddressInfo).port, token: 't', pid: 0,
        startedAtMs: 0,
      };
      daemon.writeState(config, base);
      const state = await daemon.ensureDaemon(config, { bundle: full, waitTimeoutMs: 2000 });
      expect(state?.catalog).toBe(key);
      expect(seen).not.toContain('shutdown');
      expect(daemon.readState(config)?.catalog).toBe(key);
    } finally {
      await new Promise<void>((ok) => fake.close(() => ok()));
    }
  });

  it('a daemon that dies mid-request falls back to in-process routing', async () => {
    const dead = createNetServer();
    await new Promise<void>((ok) => dead.listen(0, '127.0.0.1', () => ok()));
    const port = (dead.address() as AddressInfo).port;
    await new Promise<void>((ok) => dead.close(() => ok()));
    const state: daemon.DaemonState = {
      runtime: 'node', version: '0', embeddingModel: 'm', host: '127.0.0.1', port, token: 't',
      pid: 0, startedAtMs: 0,
    };
    let built = 0;
    const sentinel = { bestModel: 'in-process' } as unknown as RouteResult;
    const routeFn = daemon.routeWithFallback(state, () => {
      built += 1;
      return async () => sentinel;
    });
    const p = new Priorities(3, 3, 3);
    expect(await routeFn('a', p, 1)).toBe(sentinel);
    expect(await routeFn('b', p, 1)).toBe(sentinel);
    expect(built).toBe(1); // the dead daemon is not retried for every prompt
  });
});

describe('finding 5: versions must match exactly', () => {
  it('a version with a trailing newline is a failed download', async () => {
    api.catalogDir = makeFullBundle(join(dir, 'nl'), `${V1}\n`);
    loginTo();
    const sel = await client.selectCatalog('auto', deps());
    expect(sel.notice).toBe('download_failed');
    expect(cacheVersions()).toEqual([]);
  });
});

describe.skipIf(!HAS_DIST)(`finding 6: verify-dist ignores the developer's data dir ${DIST_ONLY}`, () => {
  it('passes while logged in with a cached full catalog', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    expect(cacheVersions()).toEqual([V1]);
    const r = await runNode([VERIFY_DIST], {});
    expect([r.code, r.stdout.trim()]).toEqual([0, 'dist verification passed']);
  }, 120_000);
});

describe('finding 7: a customized Router still runs the catalog check', () => {
  const custom = {
    modelId: 'custom/model', provider: 'custom', benchmarks: { GPQA: 50 },
    pricing: [0.001, 0.002] as [number, number],
  };

  it('addModel + ready(): a session that ended still throws', async () => {
    loginTo(0);
    api.refreshOutcome = 'invalid_grant';
    const router = new Router();
    router.addModel(custom);
    await expect(router.ready()).rejects.toBeInstanceOf(client.SessionEndedError);
    expectEverythingGone();
  });

  it('addModel + ready(): the check runs but the customized registry is kept', async () => {
    loginTo();
    const router = new Router();
    expect(router.bundle.kind).toBe('starter'); // nothing cached yet
    router.addModel(custom);
    await router.ready();
    expect(api.catalogCalls()).toHaveLength(1);
    expect(router.bundle.kind).toBe('starter');
    expect(router.models.getModel('custom/model')).toBeDefined();
  });
});

describe('finding 8: clearCache drops centroid caches derived from the full catalog', () => {
  it('centroids/*__full-*.json go; starter and legacy caches stay', async () => {
    loginTo();
    await client.selectCatalog('auto', deps());
    const cdir = join(env.TRYAII_DRE_DATA_DIR!, 'centroids');
    mkdirSync(cdir, { recursive: true });
    const fullCache = join(cdir, `centroids_org__model__full-${V1}.json`);
    const starterCache = join(cdir, 'centroids_org__model__starter-2026.10.03.1.json');
    const legacy = join(cdir, 'centroids_org__model.json');
    for (const p of [fullCache, starterCache, legacy]) writeFileSync(p, '{}');
    client.clearCache(env);
    expect(existsSync(fullCache)).toBe(false);
    expect(existsSync(starterCache)).toBe(true);
    expect(existsSync(legacy)).toBe(true);
    expect(cacheVersions()).toEqual([]);
  });
});

// ---------------------------------------------------------------- signing (section 6)
// Catalog contract section 6: a full catalog is accepted only with a valid
// signature from a trusted key; missing signature / unknown key_id / bad
// signature (and any manifest change after signing) is handled exactly like a
// hash mismatch. _setup-data-dir.ts trusts the tests' signing key through
// TRYAII_CATALOG_TRUSTED_KEYS; makeFullBundle signs with it.
describe('signing (contract section 6)', () => {
  const failures: Mode[] = ['unsigned', 'badsig', 'unknownkey', 'tampered'];

  it('a validly signed catalog is accepted and cached with its signature as received', async () => {
    loginTo();
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.bundle.version, sel.notice]).toEqual(['full', V1, null]);
    const cached = JSON.parse(readFileSync(join(client.fullDir(env), V1, 'manifest.json'), 'utf-8'));
    const sent = JSON.parse(readFileSync(join(bundles[V1], 'manifest.json'), 'utf-8'));
    expect(cached.key_id).toBe(TEST_KEY_ID);
    expect(cached.signature).toBe(sent.signature);
  });

  for (const mode of failures) {
    it(`${mode} without a cache: starter + download notice, nothing written`, async () => {
      loginTo();
      api.mode = mode;
      const sel = await client.selectCatalog('auto', deps());
      expect([sel.bundle.kind, sel.notice, sel.failed]).toEqual(['starter', 'download_failed', true]);
      expect(cacheVersions()).toEqual([]);
      expect(existsSync(client.statePath(env))).toBe(false);
      expect(creds()).not.toBeNull();
    });

    it(`${mode} with a cache: the cached catalog, silently`, async () => {
      loginTo();
      await client.selectCatalog('auto', deps());
      const before = readFileSync(client.statePath(env), 'utf-8');
      api.catalogDir = bundles[V2];
      api.mode = mode;
      clock = T0 + DAY;
      const sel = await client.selectCatalog('auto', deps());
      expect([sel.bundle.kind, sel.bundle.version, sel.notice]).toEqual(['full', V1, null]);
      expect(cacheVersions()).toEqual([V1]);
      expect(readFileSync(client.statePath(env), 'utf-8')).toBe(before);
    });

    it(`${mode}: the login download fails and caches nothing`, async () => {
      loginTo();
      api.mode = mode;
      expect(await client.downloadAfterLogin(deps())).toBeNull();
      expect(cacheVersions()).toEqual([]);
    });
  }

  it('a non-ASCII manifest is rejected even when validly signed', async () => {
    const bad = makeFullBundle(join(dir, 'nonascii'), V1);
    const m = JSON.parse(readFileSync(join(bad, 'manifest.json'), 'utf-8'));
    m.created_at = '2026-10-04T00:00:00Z é';
    signManifest(m); // the test signer signs anything
    writeFileSync(join(bad, 'manifest.json'), JSON.stringify(m), 'utf-8');
    api.catalogDir = bad;
    loginTo();
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.notice]).toEqual(['starter', 'download_failed']);
    expect(cacheVersions()).toEqual([]);
  });

  const tamperings: Array<[string, (m: Record<string, unknown>) => void]> = [
    ['unsigned', (m) => { m.signature = null; m.key_id = null; }],
    ['tampered', (m) => { m.created_at = '2026-10-04T00:00:09Z'; }],
    ['unknown key', (m) => { m.key_id = OTHER_KEY_ID; }],
  ];
  for (const [label, change] of tamperings) {
    it(`the cache is re-verified when loaded (${label})`, async () => {
      loginTo();
      expect((await client.selectCatalog('auto', deps())).bundle.kind).toBe('full');
      client.resetCatalogCache(); // a new process
      expect(client.cachedFull(client.readState(env), env)).not.toBeNull();
      const path = join(client.fullDir(env), V1, 'manifest.json');
      const m = JSON.parse(readFileSync(path, 'utf-8'));
      change(m);
      writeFileSync(path, client.manifestText(m), 'utf-8');
      client.resetCatalogCache();
      expect(client.cachedFull(client.readState(env), env)).toBeNull(); // treated as absent
      expect(client.selectCatalogOffline('auto', deps()).bundle.kind).toBe('starter');
      // within 24 h: state names V1 but the cache is unusable -> check now (200)
      const calls = api.catalogCalls().length;
      const sel = await client.selectCatalog('auto', deps());
      expect([sel.bundle.kind, sel.bundle.version, sel.checked]).toEqual(['full', V1, true]);
      expect(api.catalogCalls()).toHaveLength(calls + 1);
      expect(api.catalogCalls().at(-1)!.headers['if-none-match']).toBeUndefined();
      client.resetCatalogCache();
      expect(client.cachedFull(client.readState(env), env)).not.toBeNull();
    });
  }

  it('the cache is re-verified against the trusted list in effect', async () => {
    loginTo();
    expect((await client.selectCatalog('auto', deps())).bundle.kind).toBe('full');
    client.resetCatalogCache();
    env[TRUSTED_KEYS_ENV] = writeTrustedKeys(join(dir, 'other.json'), [keyEntry(OTHER_KEY_ID, OTHER_SEED)]);
    expect(client.cachedFull(client.readState(env), env)).toBeNull();
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.notice]).toEqual(['starter', 'download_failed']);
  });

  it('the packaged list does not trust the test key', async () => {
    delete env[TRUSTED_KEYS_ENV];
    loginTo();
    const sel = await client.selectCatalog('auto', deps());
    expect([sel.bundle.kind, sel.notice]).toEqual(['starter', 'download_failed']);
  });

  it('the starter needs no signature, even with an empty trusted list', async () => {
    env[TRUSTED_KEYS_ENV] = writeTrustedKeys(join(dir, 'none.json'), []);
    expect(starterBundle().manifest.signature ?? null).toBeNull();
    expect((await client.selectCatalog('starter', deps())).bundle.kind).toBe('starter');
    const sel = await client.selectCatalog('auto', deps()); // not logged in
    expect([sel.bundle.kind, sel.notice]).toEqual(['starter', 'not_logged_in']);
  });
});

describe('daemon re-verifies the cache (contract section 6)', () => {
  it("a cache dir is re-verified; a caller's own bundle dir is loaded as given", async () => {
    loginTo();
    const sel = await client.selectCatalog('auto', deps());
    const spec = daemon.catalogSpec(sel.bundle)!;
    expect(daemon.bundleFromSpec(spec).version).toBe(V1);
    const path = join(spec, 'manifest.json');
    const m = JSON.parse(readFileSync(path, 'utf-8'));
    m.created_at = '2026-10-04T00:00:09Z';
    writeFileSync(path, client.manifestText(m), 'utf-8');
    expect(() => daemon.bundleFromSpec(spec)).toThrow(/signature/);
    const own = makeFullBundle(join(dir, 'own'), V1, false);
    expect(daemon.bundleFromSpec(own).kind).toBe('full');
  });
});
