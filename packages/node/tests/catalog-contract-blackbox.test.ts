/**
 * Black-box contract tests for the catalog-behind-login behaviour of the Node CLI.
 *
 * Derived ONLY from docs/catalog/CONTRACT-catalog-v1.md (sections 1, 4, 5) and
 * docs/auth/CONTRACT-auth-v1.md (sections 4, 6, 7). The CLI (dist/cli.js) is
 * spawned as a child process against a programmable fake HTTP server; every run
 * gets its own temp data dir and temp HOME/USERPROFILE, inherited TRYAII_* vars
 * are stripped. Never touches the real ~/.tryaii or a real server.
 *
 * Requires `npm run build` first.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

// Catalog contract section 6: a full catalog is accepted only when signed by a
// trusted key. The synthetic bundles below are signed with a TEST key this
// suite controls (tests/_signing.ts: node:crypto, not the implementation) and
// the CLI trusts exactly that key via TRYAII_CATALOG_TRUSTED_KEYS (runCli).
import { signManifest, writeTrustedKeys } from './_signing.js';

const CLI = fileURLToPath(new URL('../dist/cli.js', import.meta.url));
const STARTER_DIR = fileURLToPath(new URL('../dist/catalog/data/starter/', import.meta.url));

// --------------------------------------------------------------------------
// Expected texts (verbatim from the contracts)
// --------------------------------------------------------------------------
const SESSION_ENDED = 'Your session has ended. Run: tryaii login';
const NO_ACCESS = 'This account does not have access to the full catalog; using the starter catalog.';
const DOWNLOAD_FAILED = 'Could not download the full catalog; using the starter catalog for now.';
const SCHEMA_TOO_NEW = 'The full catalog needs a newer tryaii version; using the starter catalog.';
const LOGIN_DL_FAILED = 'Could not download the full catalog now; it will be fetched on next use.';
const HELP_LOGIN_LINE =
  '  login                 Sign in with your tryaii.com account (free; unlocks the full model catalog)';
const HELP_LOGIN_EXPECTED = [
  'tryaii login -- Sign in with your tryaii.com account',
  '',
  'Usage:',
  '  tryaii login',
  '',
  'Starts a device sign-in: prints a URL and a short code. Open the URL in any',
  'browser (it does not have to be on this machine), sign in with Google and',
  'approve the code. Works over SSH and inside containers.',
  '',
  'Credentials are stored in ~/.tryaii/credentials.json (or under',
  'TRYAII_DRE_DATA_DIR).',
  '',
  'After signing in, the full model catalog is downloaded and kept up to date',
  'automatically (checked at most once a day).',
  '',
  'Environment:',
  '  TRYAII_API_URL        Override the API base URL (default https://api.tryaii.com)',
  '',
  'Examples:',
  '  tryaii login',
  '',
  'Exit codes:',
  '  0 success, 1 denied, expired or network failure, 2 bad flag, 130 cancelled.',
].join('\n');

const BUNDLE_DATA_FILES = [
  'models.json',
  'benchmarks.json',
  'normalization_ranges.json',
  'centroids.json',
  'training_queries.json',
] as const;

// --------------------------------------------------------------------------
// Starter bundle (read as DATA only)
// --------------------------------------------------------------------------
const starterManifest = JSON.parse(readFileSync(join(STARTER_DIR, 'manifest.json'), 'utf8'));
const starterModelsDoc = JSON.parse(readFileSync(join(STARTER_DIR, 'models.json'), 'utf8'));
const STARTER_IDS: string[] = starterModelsDoc.models.map((m: { model_id: string }) => m.model_id);
const NUDGE =
  `Routing on the starter catalog (${starterManifest.counts.models} models). ` +
  `Log in for free to use the full catalog (${starterManifest.full_counts.models} models): tryaii login`;

// Canonical texts of the benchmark-side files are reused verbatim from the starter
// (they are already canonical text per section 1).
const starterText = (name: string): string => readFileSync(join(STARTER_DIR, name), 'utf8');

// --------------------------------------------------------------------------
// Canonical text + synthetic full bundles (section 1)
// --------------------------------------------------------------------------
function canonical(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonical(obj[k])).join(',') + '}';
}
const sha256 = (text: string): string => createHash('sha256').update(Buffer.from(text, 'utf8')).digest('hex');

interface Bundle {
  manifest: Record<string, any>;
  files: Record<string, string>;
  routableIds: string[];
  allIds: string[];
}

function synthModel(id: string, provider: string, k: number): Record<string, unknown> {
  // Values chosen so JS and Python serialise them identically (no integral floats).
  return {
    benchmark_scores: {
      GPQA: 50.5 + k,
      'MMLU-Pro': 70.25 + k,
      LiveCodeBench: 40.75 + k,
      'Chatbot Arena Elo': 1300.5 + k * 10,
    },
    capabilities: [],
    description: `Synthetic black-box test model ${id}`,
    latency: 'fast',
    model_id: id,
    pricing: { input_per_1k: 0.0025, output_per_1k: 0.0125 },
    provider,
    tokens_per_second: 55.5,
    ttft_ms: 800,
  };
}

function makeBundle(
  version: string,
  ids: string[],
  opts: { schema?: number; corruptFile?: string } = {},
): Bundle {
  const models = ids.map((id, i) => synthModel(id, id.split('/')[0], i));
  const modelsDoc = {
    generated_from: 'catalog-contract-blackbox synthetic bundle',
    models,
    updated: '2026-10',
    version,
  };
  const files: Record<string, string> = {
    'models.json': canonical(modelsDoc),
    'benchmarks.json': starterText('benchmarks.json'),
    'normalization_ranges.json': starterText('normalization_ranges.json'),
    'centroids.json': starterText('centroids.json'),
    'training_queries.json': starterText('training_queries.json'),
  };
  const hashes: Record<string, string> = {};
  for (const name of BUNDLE_DATA_FILES) hashes[name] = sha256(files[name]);
  if (opts.corruptFile) {
    // Tamper with the transferred text AFTER hashing -> hash mismatch.
    files[opts.corruptFile] = files[opts.corruptFile].replace('Synthetic', 'Tampered!');
    if (files[opts.corruptFile] === starterText(opts.corruptFile)) throw new Error('corruption no-op');
  }
  const manifest = {
    schema: opts.schema ?? 1,
    kind: 'full',
    version,
    embedding_model: 'all-MiniLM-L6-v2',
    created_at: '2026-10-03T12:00:00Z',
    counts: { models: models.length, benchmarks: JSON.parse(files['benchmarks.json']).benchmarks.length },
    files: hashes,
    signature: null,
    key_id: null,
  };
  signManifest(manifest); // catalog contract section 6
  return {
    manifest,
    files,
    allIds: ids,
    routableIds: ids.filter((id) => !id.includes(':free')),
  };
}

const TRUSTED_KEYS_FILE = writeTrustedKeys(
  join(mkdtempSync(join(tmpdir(), 'bb-trusted-keys-')), 'trusted_keys.json'),
);

const V1 = '2026.10.03.11';
const V2 = '2026.10.03.12';
const IDS_V1 = [
  'bbtest/alpha-pro',
  'bbtest/beta-mini',
  'bbtest/gamma-max',
  'zzprov/delta-one',
  'bbtest/alpha-pro:free',
];
const IDS_V2 = ['bbtest/epsilon-new', 'bbtest/zeta-new', 'zzprov/eta-new'];
const BUNDLE_V1 = makeBundle(V1, IDS_V1); // 5 entries, 4 routable
const BUNDLE_V2 = makeBundle(V2, IDS_V2); // 3 entries, 3 routable

// --------------------------------------------------------------------------
// Fake API server (programmable per test)
// --------------------------------------------------------------------------
interface Recorded {
  method: string;
  path: string;
  headers: http.IncomingHttpHeaders;
  body: string;
}
interface Reply {
  status: number;
  json?: unknown;
  empty?: boolean;
  headers?: Record<string, string>;
}
type Route = (r: Recorded) => Reply;

let server: http.Server;
let BASE = '';
let DEAD_BASE = '';
let requests: Recorded[] = [];
let routes: Record<string, Route> = {};

const USER = { id: 'google-sub-123', email: 'bb@example.com', name: 'Black Box' };
const ME_BODY = {
  user: USER,
  entitlements: ['catalog:full'],
  session: { id: 'fam123', created_at: '2026-10-03T12:00:00Z' },
};

function tokenReply(access: string, refresh: string): Reply {
  return {
    status: 200,
    json: {
      access_token: access,
      token_type: 'Bearer',
      expires_in: 3600,
      refresh_token: refresh,
      refresh_expires_in: 7776000,
      user: USER,
      entitlements: ['catalog:full'],
    },
  };
}

function bundleReply(b: Bundle): Reply {
  return {
    status: 200,
    json: { manifest: b.manifest, files: b.files },
    headers: { ETag: `"${b.manifest.version}"`, 'Cache-Control': 'private, no-store' },
  };
}

/** Catalog route that serves a bundle, honouring If-None-Match. */
function serveBundle(b: Bundle): Route {
  return (r) => {
    if (r.headers['if-none-match'] === `"${b.manifest.version}"`) {
      return { status: 304, empty: true, headers: { ETag: `"${b.manifest.version}"` } };
    }
    return bundleReply(b);
  };
}

function defaultRoutes(): Record<string, Route> {
  return {
    'POST /v1/auth/device/code': () => ({
      status: 200,
      json: {
        device_code: 'dc-blackbox-device-code',
        user_code: 'BCDF-GHJK',
        verification_uri: `${BASE}/device`,
        verification_uri_complete: `${BASE}/device?user_code=BCDF-GHJK`,
        expires_in: 600,
        interval: 1,
      },
    }),
    'POST /v1/auth/token': (r) => {
      const body = JSON.parse(r.body || '{}');
      if (body.grant_type === 'refresh_token') return tokenReply('acc-refreshed', 'tair_refreshed');
      return tokenReply('acc-login', 'tair_login');
    },
    'POST /v1/auth/revoke': () => ({ status: 200, json: {} }),
    'GET /v1/auth/me': () => ({ status: 200, json: ME_BODY }),
    'GET /v1/catalog/live': () => ({ status: 500, json: { error: 'release_corrupt' } }),
  };
}

function send(req: http.IncomingMessage, res: http.ServerResponse, reply: Reply): void {
  const headers: Record<string, string> = { ...(reply.headers ?? {}) };
  if (reply.empty || reply.status === 304) {
    res.writeHead(reply.status, headers);
    res.end();
    return;
  }
  let payload: Buffer = Buffer.from(JSON.stringify(reply.json ?? {}), 'utf8');
  headers['Content-Type'] = 'application/json';
  const accept = String(req.headers['accept-encoding'] ?? '');
  if (/\bgzip\b/i.test(accept)) {
    payload = gzipSync(payload);
    headers['Content-Encoding'] = 'gzip';
  }
  headers['Content-Length'] = String(payload.length);
  res.writeHead(reply.status, headers);
  res.end(payload);
}

const catalogRequests = (): Recorded[] => requests.filter((r) => r.path === '/v1/catalog/live');

// --------------------------------------------------------------------------
// Temp dirs, credentials, cache seeding
// --------------------------------------------------------------------------
const tmpRoots: string[] = [];
interface Ctx {
  home: string;
  data: string;
}
function newCtx(): Ctx {
  const root = mkdtempSync(join(tmpdir(), 'tryaii-catbb-'));
  tmpRoots.push(root);
  const home = join(root, 'home');
  const data = join(root, 'data');
  mkdirSync(home, { recursive: true });
  mkdirSync(data, { recursive: true });
  return { home, data };
}

const isoZ = (ms: number): string => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
function localDate(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function writeCreds(ctx: Ctx, opts: { apiUrl?: string; accessExpired?: boolean } = {}): void {
  const now = Date.now();
  const creds = {
    version: 1,
    api_url: opts.apiUrl ?? BASE,
    user: USER,
    entitlements: ['catalog:full'],
    refresh_token: 'tair_seeded',
    refresh_expires_at: isoZ(now + 80 * 86400e3),
    access_token: 'acc-seeded',
    access_expires_at: isoZ(opts.accessExpired ? now - 3600e3 : now + 3600e3),
    created_at: isoZ(now - 86400e3),
  };
  writeFileSync(join(ctx.data, 'credentials.json'), JSON.stringify(creds));
}

const catalogDir = (ctx: Ctx) => join(ctx.data, 'catalog');
const fullDir = (ctx: Ctx) => join(ctx.data, 'catalog', 'full');
const statePath = (ctx: Ctx) => join(ctx.data, 'catalog', 'state.json');
const credsPath = (ctx: Ctx) => join(ctx.data, 'credentials.json');

/** Seed a full cache as a previous successful download would have left it. */
function seedCache(ctx: Ctx, b: Bundle, checkedAgoMs: number | null): void {
  const dir = join(fullDir(ctx), b.manifest.version);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, 'manifest.json'), JSON.stringify(b.manifest));
  for (const name of BUNDLE_DATA_FILES) writeFileSync(join(dir, name), b.files[name], 'utf8');
  const state: Record<string, unknown> = { version: b.manifest.version };
  if (checkedAgoMs !== null) state.checked_at = isoZ(Date.now() - checkedAgoMs);
  writeFileSync(statePath(ctx), JSON.stringify(state));
}

function expectCacheExactly(ctx: Ctx, b: Bundle): void {
  const v = b.manifest.version;
  expect(readdirSync(fullDir(ctx))).toEqual([v]);
  const dir = join(fullDir(ctx), v);
  expect(readdirSync(dir).sort()).toEqual(['manifest.json', ...BUNDLE_DATA_FILES].sort());
  expect(JSON.parse(readFileSync(join(dir, 'manifest.json'), 'utf8'))).toEqual(b.manifest);
  for (const name of BUNDLE_DATA_FILES) {
    const bytes = readFileSync(join(dir, name));
    expect(bytes.equals(Buffer.from(b.files[name], 'utf8')), `${name} bytes`).toBe(true);
  }
}

const H = 3600e3;

// --------------------------------------------------------------------------
// CLI runner
// --------------------------------------------------------------------------
interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
}
function runCli(ctx: Ctx, args: string[], extraEnv: Record<string, string> = {}): Promise<RunResult> {
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined || k.toUpperCase().startsWith('TRYAII_')) continue;
    env[k] = v;
  }
  Object.assign(env, {
    HOME: ctx.home,
    USERPROFILE: ctx.home,
    TRYAII_DRE_DATA_DIR: ctx.data,
    TRYAII_API_URL: BASE,
    TRYAII_NO_DAEMON: '1',
    // catalog contract section 6: trust the suite's test signing key only
    TRYAII_CATALOG_TRUSTED_KEYS: TRUSTED_KEYS_FILE,
    NO_COLOR: '1',
    ...extraEnv,
  });
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [CLI, ...args], { env, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', (d) => (stdout += d));
    child.stderr.setEncoding('utf8').on('data', (d) => (stderr += d));
    const timer = setTimeout(() => child.kill(), 45_000);
    child.on('error', reject);
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code, stdout: stdout.replace(/\r\n/g, '\n'), stderr: stderr.replace(/\r\n/g, '\n') });
    });
  });
}

const lines = (s: string): string[] => s.split('\n');
const countLine = (s: string, line: string): number => lines(s).filter((l) => l === line).length;
/** Model ids listed by `tryaii models` ("    - <id> [latency] | $in/out"). */
function listedIds(stdout: string): string[] {
  const out: string[] = [];
  for (const l of lines(stdout)) {
    const m = /^\s+- (\S+) \[/.exec(l);
    if (m) out.push(m[1]);
  }
  return out;
}
function expectStarterListed(stdout: string): void {
  const ids = listedIds(stdout);
  expect(new Set(ids)).toEqual(new Set(STARTER_IDS));
}
function expectFullListed(stdout: string, b: Bundle): void {
  const ids = listedIds(stdout);
  for (const id of b.routableIds) expect(ids).toContain(id);
  for (const id of ids) expect(b.allIds).toContain(id);
}

// --------------------------------------------------------------------------
// Lifecycle
// --------------------------------------------------------------------------
beforeAll(async () => {
  if (!existsSync(CLI)) throw new Error(`build first: ${CLI} missing (npm run build)`);
  server = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const path = (req.url ?? '/').split('?')[0];
      const rec: Recorded = {
        method: req.method ?? 'GET',
        path,
        headers: req.headers,
        body: Buffer.concat(chunks).toString('utf8'),
      };
      requests.push(rec);
      const route = routes[`${rec.method} ${path}`];
      if (!route) return send(req, res, { status: 404, json: { error: 'not_found' } });
      send(req, res, route(rec));
    });
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
  BASE = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  // A port that refuses connections (bind, read the port, close).
  const dead = http.createServer();
  await new Promise<void>((r) => dead.listen(0, '127.0.0.1', () => r()));
  DEAD_BASE = `http://127.0.0.1:${(dead.address() as AddressInfo).port}`;
  await new Promise<void>((r) => dead.close(() => r()));
});

afterAll(async () => {
  await new Promise<void>((r) => server?.close(() => r()));
  for (const d of tmpRoots) rmSync(d, { recursive: true, force: true });
});

beforeEach(() => {
  requests = [];
  routes = defaultRoutes();
});

const T = 60_000;

// ==========================================================================
// Section 1 sanity of our own synthetic bundle (guards the fixture, not the CLI)
// ==========================================================================
describe('synthetic bundle fixture (section 1)', () => {
  it('is self-consistent: hashes over canonical text, routable count excludes :free', () => {
    for (const name of BUNDLE_DATA_FILES) {
      expect(BUNDLE_V1.manifest.files[name]).toBe(sha256(BUNDLE_V1.files[name]));
      expect(BUNDLE_V1.files[name].endsWith('\n')).toBe(false);
    }
    expect(BUNDLE_V1.manifest.counts.models).toBe(5);
    expect(BUNDLE_V1.routableIds.length).toBe(4);
    expect(starterManifest.kind).toBe('starter');
    expect(typeof starterManifest.full_counts.models).toBe('number');
  });
});

// ==========================================================================
// Logged out -> starter + nudge (section 5 rule 1, Nudge)
// ==========================================================================
describe('logged out', () => {
  it('models lists the starter catalog, prints the nudge once on stderr, makes no network calls', async () => {
    const ctx = newCtx();
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expectStarterListed(r.stdout);
    expect(countLine(r.stderr, NUDGE)).toBe(1);
    expect(r.stdout).not.toContain('Routing on the starter catalog');
    expect(requests).toEqual([]);
    const nudge = JSON.parse(readFileSync(join(catalogDir(ctx), 'nudge.json'), 'utf8'));
    expect(nudge).toEqual({ last_shown: localDate() });
    expect(existsSync(fullDir(ctx))).toBe(false);
  }, T);

  it('nudge is shown at most once per local calendar day, again on a new day', async () => {
    const ctx = newCtx();
    const first = await runCli(ctx, ['models']);
    expect(countLine(first.stderr, NUDGE)).toBe(1);
    const second = await runCli(ctx, ['models']);
    expect(second.code).toBe(0);
    expect(second.stderr).not.toContain('Routing on the starter catalog');
    expectStarterListed(second.stdout);

    // Pretend it was last shown on an earlier day.
    writeFileSync(join(catalogDir(ctx), 'nudge.json'), JSON.stringify({ last_shown: '2000-01-01' }));
    const third = await runCli(ctx, ['models']);
    expect(countLine(third.stderr, NUDGE)).toBe(1);
    expect(JSON.parse(readFileSync(join(catalogDir(ctx), 'nudge.json'), 'utf8'))).toEqual({
      last_shown: localDate(),
    });
    expect(requests).toEqual([]);
  }, T);

  it('TRYAII_NO_BANNER suppresses the nudge', async () => {
    const ctx = newCtx();
    const r = await runCli(ctx, ['models'], { TRYAII_NO_BANNER: '1' });
    expect(r.code).toBe(0);
    expectStarterListed(r.stdout);
    expect(r.stderr).not.toContain('Routing on the starter catalog');
    expect(requests).toEqual([]);
  }, T);
});

// ==========================================================================
// Login downloads the catalog (section 5 CLI changes)
// ==========================================================================
describe('login', () => {
  it('prints "Downloaded the full catalog (<routable n> models)." and writes the cache exactly', async () => {
    const ctx = newCtx();
    routes['GET /v1/catalog/live'] = serveBundle(BUNDLE_V1);
    const before = Date.now();
    const r = await runCli(ctx, ['login']);
    expect(r.code).toBe(0);
    expect(r.stdout).toBe(
      [
        'To sign in, open this URL in a browser:',
        `  ${BASE}/device`,
        'and enter the code: BCDF-GHJK',
        '',
        `Or open directly: ${BASE}/device?user_code=BCDF-GHJK`,
        '',
        'Waiting for approval (expires in 10 minutes, Ctrl+C to cancel)...',
        `Logged in as ${USER.email}.`,
        `Downloaded the full catalog (${BUNDLE_V1.routableIds.length} models).`,
        '',
      ].join('\n'),
    );

    // Wire: GET /v1/catalog/live with the new access token, no If-None-Match (no cache yet).
    const cat = catalogRequests();
    expect(cat.length).toBe(1);
    expect(cat[0].method).toBe('GET');
    expect(cat[0].headers.authorization).toBe('Bearer acc-login');
    expect(cat[0].headers['if-none-match']).toBeUndefined();
    expect(String(cat[0].headers['user-agent'])).toMatch(/^tryaii\//);

    // Cache layout: catalog/full/<version>/ with six files as received; state.json.
    expectCacheExactly(ctx, BUNDLE_V1);
    const state = JSON.parse(readFileSync(statePath(ctx), 'utf8'));
    expect(state.version).toBe(V1);
    expect(state.checked_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/);
    const checked = Date.parse(state.checked_at);
    expect(checked).toBeGreaterThanOrEqual(before - 2000);
    expect(checked).toBeLessThanOrEqual(Date.now() + 2000);
    expect(existsSync(credsPath(ctx))).toBe(true);

    // Second run within 24 h: no catalog request, full catalog in use, no nudge.
    requests = [];
    const m = await runCli(ctx, ['models']);
    expect(m.code).toBe(0);
    expect(catalogRequests()).toEqual([]);
    expectFullListed(m.stdout, BUNDLE_V1);
    expect(m.stderr).not.toContain('Routing on the starter catalog');
  }, T);

  it('prints the "now" failure line on stdout and still exits 0 when the endpoint fails (500)', async () => {
    const ctx = newCtx();
    routes['GET /v1/catalog/live'] = () => ({ status: 500, json: { error: 'release_corrupt' } });
    const r = await runCli(ctx, ['login']);
    expect(r.code).toBe(0);
    expect(r.stdout.endsWith(`Logged in as ${USER.email}.\n${LOGIN_DL_FAILED}\n`)).toBe(true);
    expect(r.stderr).not.toContain(LOGIN_DL_FAILED);
    expect(existsSync(credsPath(ctx))).toBe(true);
    expect(existsSync(fullDir(ctx)) ? readdirSync(fullDir(ctx)) : []).toEqual([]);
  }, T);

  it('prints the "now" failure line for 403 insufficient_entitlement too (any failure)', async () => {
    const ctx = newCtx();
    routes['GET /v1/catalog/live'] = () => ({ status: 403, json: { error: 'insufficient_entitlement' } });
    const r = await runCli(ctx, ['login']);
    expect(r.code).toBe(0);
    expect(r.stdout.endsWith(`Logged in as ${USER.email}.\n${LOGIN_DL_FAILED}\n`)).toBe(true);
  }, T);
});

// ==========================================================================
// whoami third line (section 5 CLI changes)
// ==========================================================================
describe('whoami', () => {
  it('third line names the full release and routable count when a full cache exists', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 1 * H);
    routes['GET /v1/catalog/live'] = serveBundle(BUNDLE_V1);
    const r = await runCli(ctx, ['whoami']);
    expect(r.code).toBe(0);
    expect(r.stdout).toBe(
      `Logged in as ${USER.email} (${USER.name})\n` +
        'Entitlements: catalog:full\n' +
        `Catalog: full, release ${V1} (${BUNDLE_V1.routableIds.length} models)\n`,
    );
  }, T);

  it('third line is "Catalog: not downloaded yet" without a full cache', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    const r = await runCli(ctx, ['whoami']);
    expect(r.code).toBe(0);
    expect(r.stdout).toBe(
      `Logged in as ${USER.email} (${USER.name})\n` + 'Entitlements: catalog:full\n' + 'Catalog: not downloaded yet\n',
    );
  }, T);

  it('--json output is unchanged (the server /me JSON only)', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 1 * H);
    const r = await runCli(ctx, ['whoami', '--json']);
    expect(r.code).toBe(0);
    expect(r.stdout.trimEnd()).toBe(JSON.stringify(ME_BODY, null, 2));
  }, T);
});

// ==========================================================================
// Freshness checks: 24 h, If-None-Match, 304, new version (section 5 rule 2)
// ==========================================================================
describe('logged in, catalog check', () => {
  it('within 24 h of checked_at: no catalog request, cached full catalog is used', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 23 * H);
    routes['GET /v1/catalog/live'] = serveBundle(BUNDLE_V2);
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expect(catalogRequests()).toEqual([]);
    expectFullListed(r.stdout, BUNDLE_V1);
    expect(r.stderr).not.toContain('Routing on the starter catalog');
  }, T);

  it('after 24 h: sends If-None-Match "<cached version>"; 304 keeps the cache and touches checked_at', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 25 * H);
    routes['GET /v1/catalog/live'] = serveBundle(BUNDLE_V1);
    const before = Date.now();
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    const cat = catalogRequests();
    expect(cat.length).toBe(1);
    expect(cat[0].headers['if-none-match']).toBe(`"${V1}"`);
    expect(cat[0].headers.authorization).toBe('Bearer acc-seeded');
    expectFullListed(r.stdout, BUNDLE_V1);
    expectCacheExactly(ctx, BUNDLE_V1);
    const state = JSON.parse(readFileSync(statePath(ctx), 'utf8'));
    expect(state.version).toBe(V1);
    expect(Date.parse(state.checked_at)).toBeGreaterThanOrEqual(before - 2000);
    expect(r.stderr).not.toContain(DOWNLOAD_FAILED);
  }, T);

  it('missing checked_at triggers a check', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, null);
    routes['GET /v1/catalog/live'] = serveBundle(BUNDLE_V1);
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expect(catalogRequests().length).toBe(1);
    expectFullListed(r.stdout, BUNDLE_V1);
  }, T);

  it('after 24 h a new live version (200) replaces the cache; only the newest version dir is kept', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 30 * H);
    routes['GET /v1/catalog/live'] = serveBundle(BUNDLE_V2);
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expect(catalogRequests().length).toBe(1);
    expectFullListed(r.stdout, BUNDLE_V2);
    expectCacheExactly(ctx, BUNDLE_V2);
    expect(JSON.parse(readFileSync(statePath(ctx), 'utf8')).version).toBe(V2);
  }, T);

  it('expired access token: refreshes first, then fetches the catalog with the new token', async () => {
    const ctx = newCtx();
    writeCreds(ctx, { accessExpired: true });
    routes['GET /v1/catalog/live'] = serveBundle(BUNDLE_V1);
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    const refreshIdx = requests.findIndex((q) => q.path === '/v1/auth/token');
    const catIdx = requests.findIndex((q) => q.path === '/v1/catalog/live');
    expect(refreshIdx).toBeGreaterThanOrEqual(0);
    expect(catIdx).toBeGreaterThan(refreshIdx);
    expect(JSON.parse(requests[refreshIdx].body).grant_type).toBe('refresh_token');
    expect(requests[catIdx].headers.authorization).toBe('Bearer acc-refreshed');
    expectFullListed(r.stdout, BUNDLE_V1);
    expectCacheExactly(ctx, BUNDLE_V1);
  }, T);
});

// ==========================================================================
// Rejected bundles: hash mismatch, schema too new (sections 1 + 5)
// ==========================================================================
describe('rejected bundles', () => {
  it('hash mismatch with an existing cache: cache unchanged and used, no new version written', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 30 * H);
    routes['GET /v1/catalog/live'] = serveBundle(makeBundle(V2, IDS_V2, { corruptFile: 'models.json' }));
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expect(catalogRequests().length).toBe(1);
    expectFullListed(r.stdout, BUNDLE_V1);
    expectCacheExactly(ctx, BUNDLE_V1);
    expect(r.stderr).not.toContain(DOWNLOAD_FAILED);
  }, T);

  it('hash mismatch without a cache: starter + the download-failure line, nothing cached', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    routes['GET /v1/catalog/live'] = serveBundle(makeBundle(V2, IDS_V2, { corruptFile: 'models.json' }));
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expectStarterListed(r.stdout);
    expect(countLine(r.stderr, DOWNLOAD_FAILED)).toBe(1);
    expect(r.stderr).not.toContain('Routing on the starter catalog');
    expect(existsSync(fullDir(ctx)) ? readdirSync(fullDir(ctx)) : []).toEqual([]);
  }, T);

  it('schema 2 without a cache: starter + the schema line (not the generic failure line)', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    routes['GET /v1/catalog/live'] = serveBundle(makeBundle(V2, IDS_V2, { schema: 2 }));
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expectStarterListed(r.stdout);
    expect(countLine(r.stderr, SCHEMA_TOO_NEW)).toBe(1);
    expect(r.stderr).not.toContain(DOWNLOAD_FAILED);
    expect(existsSync(fullDir(ctx)) ? readdirSync(fullDir(ctx)) : []).toEqual([]);
  }, T);

  it('schema 2 with an existing cache: the cached full catalog is used', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 30 * H);
    routes['GET /v1/catalog/live'] = serveBundle(makeBundle(V2, IDS_V2, { schema: 2 }));
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expectFullListed(r.stdout, BUNDLE_V1);
    expectCacheExactly(ctx, BUNDLE_V1);
  }, T);
});

// ==========================================================================
// 403 insufficient_entitlement -> starter (section 5 rule 2)
// ==========================================================================
describe('403 insufficient_entitlement', () => {
  it('without a cache: starter + the no-access line, no nudge', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    routes['GET /v1/catalog/live'] = () => ({ status: 403, json: { error: 'insufficient_entitlement' } });
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expectStarterListed(r.stdout);
    expect(countLine(r.stderr, NO_ACCESS)).toBe(1);
    expect(r.stderr).not.toContain('Routing on the starter catalog');
    expect(r.stderr).not.toContain(DOWNLOAD_FAILED);
    expect(existsSync(credsPath(ctx))).toBe(true);
  }, T);

  it('with a stale cache: still starter + the no-access line', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 30 * H);
    routes['GET /v1/catalog/live'] = () => ({ status: 403, json: { error: 'insufficient_entitlement' } });
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expectStarterListed(r.stdout);
    expect(countLine(r.stderr, NO_ACCESS)).toBe(1);
  }, T);
});

// ==========================================================================
// Transient failures: 404 / 429 / 500 / unreachable (section 5 rule 2)
// ==========================================================================
const FAILURES: Array<{ name: string; reply?: Reply; dead?: boolean }> = [
  { name: '404 no_live_release', reply: { status: 404, json: { error: 'no_live_release' } } },
  { name: '429 slow_down', reply: { status: 429, json: { error: 'slow_down' }, headers: { 'Retry-After': '120' } } },
  { name: '500', reply: { status: 500, json: { error: 'release_corrupt' } } },
  { name: 'unreachable', dead: true },
];

describe('download failures', () => {
  for (const f of FAILURES) {
    it(`${f.name} with an existing cache: cached full catalog is used`, async () => {
      const ctx = newCtx();
      writeCreds(ctx, { apiUrl: f.dead ? DEAD_BASE : BASE });
      seedCache(ctx, BUNDLE_V1, 30 * H);
      if (f.reply) routes['GET /v1/catalog/live'] = () => f.reply!;
      const r = await runCli(ctx, ['models']);
      expect(r.code).toBe(0);
      if (!f.dead) expect(catalogRequests().length).toBe(1);
      expectFullListed(r.stdout, BUNDLE_V1);
      expectCacheExactly(ctx, BUNDLE_V1);
      expect(existsSync(credsPath(ctx))).toBe(true);
      expect(r.stderr).not.toContain(DOWNLOAD_FAILED);
      expect(r.stderr).not.toContain('Routing on the starter catalog');
    }, T);

    it(`${f.name} without a cache: starter + the download-failure line`, async () => {
      const ctx = newCtx();
      writeCreds(ctx, { apiUrl: f.dead ? DEAD_BASE : BASE });
      if (f.reply) routes['GET /v1/catalog/live'] = () => f.reply!;
      const r = await runCli(ctx, ['models']);
      expect(r.code).toBe(0);
      if (!f.dead) expect(catalogRequests().length).toBe(1);
      expectStarterListed(r.stdout);
      expect(countLine(r.stderr, DOWNLOAD_FAILED)).toBe(1);
      expect(r.stderr).not.toContain('Routing on the starter catalog');
      expect(existsSync(credsPath(ctx))).toBe(true);
    }, T);
  }
});

// ==========================================================================
// Session ended during the check (section 5 rule 2)
// ==========================================================================
describe('session ended during the check', () => {
  it('refresh invalid_grant: stderr session-ended line, exit 1, credentials + full cache deleted', async () => {
    const ctx = newCtx();
    writeCreds(ctx, { accessExpired: true });
    seedCache(ctx, BUNDLE_V1, 30 * H);
    routes['POST /v1/auth/token'] = () => ({
      status: 400,
      json: { error: 'invalid_grant', error_description: 'revoked' },
    });
    routes['GET /v1/catalog/live'] = serveBundle(BUNDLE_V1);
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(1);
    expect(countLine(r.stderr, SESSION_ENDED)).toBe(1);
    expect(listedIds(r.stdout)).toEqual([]);
    expect(existsSync(credsPath(ctx))).toBe(false);
    expect(existsSync(fullDir(ctx)) ? readdirSync(fullDir(ctx)) : []).toEqual([]);
    expect(catalogRequests()).toEqual([]);
  }, T);

  it('catalog 401 invalid_token: stderr session-ended line, exit 1, credentials + full cache deleted', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 30 * H);
    routes['GET /v1/catalog/live'] = () => ({ status: 401, json: { error: 'invalid_token' } });
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(1);
    expect(countLine(r.stderr, SESSION_ENDED)).toBe(1);
    expect(listedIds(r.stdout)).toEqual([]);
    expect(existsSync(credsPath(ctx))).toBe(false);
    expect(existsSync(fullDir(ctx)) ? readdirSync(fullDir(ctx)) : []).toEqual([]);
  }, T);
});

// ==========================================================================
// logout (section 5 cache)
// ==========================================================================
describe('logout', () => {
  it('deletes catalog/full and catalog/state.json', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 1 * H);
    const r = await runCli(ctx, ['logout']);
    expect(r.code).toBe(0);
    expect(r.stdout).toBe('Logged out.\n');
    expect(existsSync(credsPath(ctx))).toBe(false);
    expect(existsSync(fullDir(ctx))).toBe(false);
    expect(existsSync(statePath(ctx))).toBe(false);
  }, T);

  it('deletes catalog/full and state.json also when the server revoke fails', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 1 * H);
    routes['POST /v1/auth/revoke'] = () => ({ status: 500, json: { error: 'server_error' } });
    const r = await runCli(ctx, ['logout']);
    expect(r.code).toBe(0);
    expect(r.stdout).toBe('Logged out.\n');
    expect(existsSync(credsPath(ctx))).toBe(false);
    expect(existsSync(fullDir(ctx))).toBe(false);
    expect(existsSync(statePath(ctx))).toBe(false);
  }, T);

  it('after logout, models is back on the starter catalog with the nudge', async () => {
    const ctx = newCtx();
    writeCreds(ctx);
    seedCache(ctx, BUNDLE_V1, 1 * H);
    await runCli(ctx, ['logout']);
    requests = [];
    const r = await runCli(ctx, ['models']);
    expect(r.code).toBe(0);
    expectStarterListed(r.stdout);
    expect(countLine(r.stderr, NUDGE)).toBe(1);
    expect(requests).toEqual([]);
  }, T);
});

// ==========================================================================
// Help text (section 5 CLI changes)
// ==========================================================================
describe('help text', () => {
  it('global help has the new login line and never labels a catalog public or private', async () => {
    const ctx = newCtx();
    const r = await runCli(ctx, ['--help']);
    expect(r.code).toBe(0);
    expect(lines(r.stdout)).toContain(HELP_LOGIN_LINE);
    expect(r.stdout).not.toMatch(/(public|private) catalog/i);
    expect(requests).toEqual([]);
  }, T);

  it('login --help matches HELP_LOGIN with the catalog paragraph after the credentials paragraph', async () => {
    const ctx = newCtx();
    const r = await runCli(ctx, ['login', '--help']);
    expect(r.code).toBe(0);
    expect(r.stdout.trimEnd()).toBe(HELP_LOGIN_EXPECTED);
    expect(requests).toEqual([]);
  }, T);
});
