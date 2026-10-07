/**
 * Black-box contract tests for `tryaii login` / `logout` / `whoami` (Node CLI).
 *
 * Every expectation here is derived from docs/auth/CONTRACT-auth-v1.md
 * (section 4 = HTTP API, section 6 = SDK / CLI, section 7 = v1.1
 * amendments; tests whose expectation v1.1 changed cite the item) -- NOT
 * from the implementation. The CLI is spawned as a child process (`node dist/cli.js`,
 * so run `npm run build` first) against a minimal, per-test programmable fake
 * HTTP server built on node:http. Each test gets its own temp
 * TRYAII_DRE_DATA_DIR and a temp HOME/USERPROFILE, so the real ~/.tryaii is
 * never touched and no real server is ever contacted.
 */

import { spawn } from 'node:child_process';
import {
  closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readdirSync, readFileSync, rmSync, statSync,
  writeFileSync,
} from 'node:fs';
import { createServer, type IncomingHttpHeaders, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeAll, describe, expect, it } from 'vitest';

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_DIR = resolve(HERE, '..');
const CLI = join(PKG_DIR, 'dist', 'cli.js');
const SPEC = resolve(PKG_DIR, '..', '..', 'docs', 'auth', 'CONTRACT-auth-v1.md');
// The catalog contract (increment 2) amends the CLI output of login / whoami
// and the help text; its section 5 ("CLI changes") is read for those.
const CATALOG_SPEC = resolve(PKG_DIR, '..', '..', 'docs', 'catalog', 'CONTRACT-catalog-v1.md');

const DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code';
const ISO_Z = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const IS_WIN = process.platform === 'win32';

// ---------------------------------------------------------------------------
// Fake API server
// ---------------------------------------------------------------------------

interface Recorded {
  method: string;
  path: string;
  headers: IncomingHttpHeaders;
  raw: string;
  body: any;
  t: number;
}

interface Reply {
  status?: number;
  json?: unknown;
  raw?: string;       // sent verbatim instead of json
  hang?: boolean;     // never answer
  headers?: Record<string, string>; // extra response headers (e.g. Location)
}

type Responder = Reply | ((req: Recorded, n: number) => Reply);

class FakeApi {
  server!: Server;
  url = '';
  requests: Recorded[] = [];
  private routes = new Map<string, Responder[]>();
  private counts = new Map<string, number>();
  private hanging: Array<() => void> = [];

  /** Queue replies for `METHOD /path`; the last one repeats forever. */
  on(method: string, path: string, ...replies: Responder[]): this {
    this.routes.set(`${method} ${path}`, replies);
    return this;
  }

  reqs(method: string, path: string): Recorded[] {
    return this.requests.filter((r) => r.method === method && r.path === path);
  }

  async start(): Promise<void> {
    this.server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on('data', (c: Buffer) => chunks.push(c));
      req.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        let body: any;
        try { body = raw ? JSON.parse(raw) : undefined; } catch { body = undefined; }
        const path = (req.url ?? '').split('?')[0];
        const rec: Recorded = {
          method: req.method ?? '', path, headers: req.headers, raw, body, t: Date.now(),
        };
        this.requests.push(rec);
        const key = `${rec.method} ${path}`;
        const list = this.routes.get(key);
        const n = this.counts.get(key) ?? 0;
        this.counts.set(key, n + 1);
        let reply: Reply;
        if (!list || list.length === 0) {
          reply = { status: 404, json: { error: 'not_found' } };
        } else {
          const r = list[Math.min(n, list.length - 1)];
          reply = typeof r === 'function' ? r(rec, n) : r;
        }
        if (reply.hang) {
          this.hanging.push(() => res.destroy());
          return;
        }
        const payload = reply.raw !== undefined ? reply.raw : JSON.stringify(reply.json ?? {});
        res.writeHead(reply.status ?? 200, {
          'Content-Type': reply.raw !== undefined ? 'text/plain' : 'application/json',
          'Content-Length': Buffer.byteLength(payload),
          ...(reply.headers ?? {}),
        });
        res.end(payload);
      });
    });
    await new Promise<void>((ok) => this.server.listen(0, '127.0.0.1', () => ok()));
    const { port } = this.server.address() as AddressInfo;
    this.url = `http://127.0.0.1:${port}`;
  }

  async stop(): Promise<void> {
    for (const h of this.hanging) h();
    this.server.closeAllConnections?.();
    await new Promise<void>((ok) => this.server.close(() => ok()));
  }
}

/** A loopback URL on which nothing is listening. */
async function deadUrl(): Promise<string> {
  const s = createServer();
  await new Promise<void>((ok) => s.listen(0, '127.0.0.1', () => ok()));
  const { port } = s.address() as AddressInfo;
  await new Promise<void>((ok) => s.close(() => ok()));
  return `http://127.0.0.1:${port}`;
}

// ---------------------------------------------------------------------------
// Per-test sandbox + CLI runner
// ---------------------------------------------------------------------------

interface Run { code: number | null; signal: string | null; stdout: string; stderr: string; ms: number }

let cleanup: Array<() => Promise<void> | void> = [];

afterEach(async () => {
  const fns = cleanup;
  cleanup = [];
  for (const f of fns.reverse()) await f();
});

function sandbox() {
  const root = mkdtempSync(join(tmpdir(), 'tryaii-auth-bb-'));
  const dataDir = join(root, 'data');
  const home = join(root, 'home');
  mkdirSync(dataDir, { recursive: true });
  mkdirSync(home, { recursive: true });
  cleanup.push(() => rmSync(root, { recursive: true, force: true }));
  return { root, dataDir, home, credPath: join(dataDir, 'credentials.json') };
}

async function fake(): Promise<FakeApi> {
  const f = new FakeApi();
  await f.start();
  cleanup.push(() => f.stop());
  return f;
}

function runCli(
  args: string[],
  opts: { apiUrl: string; dataDir: string; home: string; env?: Record<string, string>;
          timeoutMs?: number; onSpawn?: (kill: (sig: NodeJS.Signals) => void) => void },
): Promise<Run> {
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined) continue;
    if (k.toUpperCase().startsWith('TRYAII_')) continue; // never inherit a real token/url
    env[k] = v;
  }
  Object.assign(env, {
    TRYAII_NO_BANNER: '1',
    TRYAII_API_URL: opts.apiUrl,
    TRYAII_DRE_DATA_DIR: opts.dataDir,
    HOME: opts.home,
    USERPROFILE: opts.home,
    NO_COLOR: '1',
  }, opts.env ?? {});
  const started = Date.now();
  return new Promise((ok, fail) => {
    const child = spawn(process.execPath, [CLI, ...args], { env, cwd: opts.home, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', (d) => { stdout += d; });
    child.stderr.setEncoding('utf8').on('data', (d) => { stderr += d; });
    const guard = setTimeout(() => child.kill('SIGKILL'), opts.timeoutMs ?? 30000);
    opts.onSpawn?.((sig) => child.kill(sig));
    child.on('error', fail);
    child.on('close', (code, signal) => {
      clearTimeout(guard);
      ok({ code, signal, stdout, stderr, ms: Date.now() - started });
    });
  });
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

const VERIFY_URI = 'https://tryaii.test/device';
const USER_CODE = 'BCDF-GHJK';

function deviceCodeReply(over: Partial<Record<string, unknown>> = {}): Reply {
  return {
    status: 200,
    json: {
      device_code: 'dev-code-123', user_code: USER_CODE,
      verification_uri: VERIFY_URI,
      verification_uri_complete: `${VERIFY_URI}?user_code=${USER_CODE}`,
      expires_in: 600, interval: 1, ...over,
    },
  };
}

function tokenReply(over: Partial<Record<string, unknown>> = {}): Reply {
  return {
    status: 200,
    json: {
      access_token: 'eyJ.new-access', token_type: 'Bearer', expires_in: 3600,
      refresh_token: 'tair_new-refresh', refresh_expires_in: 7776000,
      user: { id: 'u-1', email: 'a@b.com', name: 'A B' },
      entitlements: ['catalog:full'], ...over,
    },
  };
}

const err = (error: string, status = 400): Reply => ({
  status, json: { error, error_description: `desc of ${error}` },
});

function meReply(over: Partial<Record<string, unknown>> = {}): Reply {
  return {
    status: 200,
    json: {
      user: { id: 'u-1', email: 'a@b.com', name: 'A B' },
      entitlements: ['catalog:full'],
      session: { id: 'fam123', created_at: '2026-10-03T12:00:00Z' },
      ...over,
    },
  };
}

function seedCreds(credPath: string, apiUrl: string, over: Record<string, unknown> = {}): string {
  const now = Date.now();
  const doc = {
    version: 1, api_url: apiUrl,
    user: { id: 'u-old', email: 'old@b.com', name: 'Old User' },
    entitlements: ['catalog:full'],
    refresh_token: 'tair_old-refresh', refresh_expires_at: iso(now + 80 * 86400e3),
    access_token: 'eyJ.old-access', access_expires_at: iso(now + 3600e3),
    created_at: iso(now - 600e3),
    ...over,
  };
  const text = JSON.stringify(doc, null, 2);
  writeFileSync(credPath, text);
  return text;
}

function loginBlock(): string {
  return [
    'To sign in, open this URL in a browser:',
    `  ${VERIFY_URI}`,
    `and enter the code: ${USER_CODE}`,
    '',
    `Or open directly: ${VERIFY_URI}?user_code=${USER_CODE}`,
    '',
    'Waiting for approval (expires in 10 minutes, Ctrl+C to cancel)...',
  ].join('\n') + '\n';
}

function specBlock(name: string): string {
  const text = readFileSync(SPEC, 'utf8').replace(/\r\n/g, '\n');
  const m = new RegExp('`' + name + '`:\\n```\\n([\\s\\S]*?)\\n```').exec(text);
  if (!m) throw new Error(`block ${name} not found in spec`);
  return m[1];
}

const SESSION_ENDED = 'Your session has ended. Run: tryaii login\n';

// Catalog contract v1 (docs/catalog/CONTRACT-catalog-v1.md section 5): `login`
// downloads the full catalog right after "Logged in as <email>." -- this fake
// has no GET /v1/catalog/live route (404), so every login here ends with the
// failure line (stdout, exit code stays 0) -- and `whoami` prints a third line,
// "Catalog: not downloaded yet" when no full catalog is cached. The
// entitlement is catalog:full (section 4).
const LOGIN_CATALOG_FAILED = 'Could not download the full catalog now; it will be fetched on next use.\n';
const WHOAMI_NO_CATALOG = 'Catalog: not downloaded yet\n';

/** Catalog contract section 5: HELP_LOGIN gains a paragraph after the credentials one. */
function catalogHelpLogin(): string {
  const spec = readFileSync(CATALOG_SPEC, 'utf8').replace(/\r\n/g, '\n');
  const m = /HELP_LOGIN gains, after the credentials paragraph:\n((?:\s+`[^`\n]*`\n)+)/.exec(spec);
  if (!m) throw new Error('HELP_LOGIN amendment not found in the catalog contract');
  const extra = [...m[1].matchAll(/`([^`\n]*)`/g)].map((x) => x[1]).join('\n');
  const base = specBlock('HELP_LOGIN');
  const anchor = 'TRYAII_DRE_DATA_DIR).\n';
  if (!base.includes(anchor)) throw new Error('credentials paragraph not found in HELP_LOGIN');
  return base.replace(anchor, `${anchor}\n${extra}\n`);
}

/** Catalog contract section 5: the global HELP login line. */
function catalogHelpLoginLine(): string {
  const spec = readFileSync(CATALOG_SPEC, 'utf8').replace(/\r\n/g, '\n');
  const m = /Global HELP login line becomes:\n\s+`([^`\n]*)`/.exec(spec);
  if (!m) throw new Error('global HELP login line not found in the catalog contract');
  return m[1];
}
const couldNotReach = (u: string) => `Could not reach ${u}. Check your connection and try again.\n`;

beforeAll(() => {
  if (!existsSync(CLI)) throw new Error(`missing ${CLI}; run \`npm run build\` in packages/node first`);
});

// ---------------------------------------------------------------------------
// login
// ---------------------------------------------------------------------------

describe('login (contract section 6)', () => {
  it('success: exact stdout, exit 0, empty stderr', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', tokenReply());
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stderr).toBe('');
    expect(r.stdout).toBe(loginBlock() + 'Logged in as a@b.com.\n' + LOGIN_CATALOG_FAILED);
    expect(r.code).toBe(0);
  });

  it('sends the contract request bodies and headers', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', err('authorization_pending'), tokenReply());
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);

    const [dc] = api.reqs('POST', '/v1/auth/device/code');
    expect(dc).toBeDefined();
    expect(dc.headers['content-type']).toMatch(/^application\/json/);
    expect(String(dc.headers['user-agent'])).toMatch(/^tryaii\/\S+/);
    expect(dc.body.client_id).toBe('tryaii-cli');
    expect(dc.body.client.sdk).toBe('node');
    for (const k of ['version', 'os']) {
      expect(typeof dc.body.client[k]).toBe('string');
      expect(dc.body.client[k].length).toBeGreaterThan(0);
      expect(dc.body.client[k].length).toBeLessThanOrEqual(64);
    }

    const polls = api.reqs('POST', '/v1/auth/token');
    expect(polls.length).toBe(2);
    for (const p of polls) {
      expect(p.headers['content-type']).toMatch(/^application\/json/);
      expect(String(p.headers['user-agent'])).toMatch(/^tryaii\/\S+/);
      expect(p.body).toEqual({ grant_type: DEVICE_GRANT, device_code: 'dev-code-123', client_id: 'tryaii-cli' });
    }
  });

  it('writes credentials.json with the contract key set and ISO-8601 Z timestamps', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', tokenReply({ expires_in: 3600, refresh_expires_in: 7776000 }));
    const before = Date.now();
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    const after = Date.now();
    expect(r.code).toBe(0);

    // atomic write: only the final file remains in the data dir
    expect(readdirSync(sb.dataDir)).toEqual(['credentials.json']);
    const c = JSON.parse(readFileSync(sb.credPath, 'utf8'));
    expect(Object.keys(c).sort()).toEqual([
      'access_expires_at', 'access_token', 'api_url', 'created_at', 'entitlements',
      'refresh_expires_at', 'refresh_token', 'user', 'version',
    ]);
    expect(c.version).toBe(1);
    expect(c.api_url).toBe(api.url);
    expect(c.user).toEqual({ id: 'u-1', email: 'a@b.com', name: 'A B' });
    expect(c.entitlements).toEqual(['catalog:full']);
    expect(c.refresh_token).toBe('tair_new-refresh');
    expect(c.access_token).toBe('eyJ.new-access');
    for (const k of ['refresh_expires_at', 'access_expires_at', 'created_at']) {
      expect(c[k]).toMatch(ISO_Z);
    }
    const slack = 5000;
    const t = (s: string) => Date.parse(s);
    expect(t(c.access_expires_at)).toBeGreaterThanOrEqual(before + 3600e3 - slack);
    expect(t(c.access_expires_at)).toBeLessThanOrEqual(after + 3600e3 + slack);
    expect(t(c.refresh_expires_at)).toBeGreaterThanOrEqual(before + 7776000e3 - slack);
    expect(t(c.refresh_expires_at)).toBeLessThanOrEqual(after + 7776000e3 + slack);
    expect(t(c.created_at)).toBeGreaterThanOrEqual(before - slack);
    expect(t(c.created_at)).toBeLessThanOrEqual(after + slack);
    // never under the (sandboxed) home
    expect(existsSync(join(sb.home, '.tryaii'))).toBe(false);
  });

  it.skipIf(IS_WIN)('credentials file has mode 0600 on POSIX', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply()).on('POST', '/v1/auth/token', tokenReply());
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);
    expect(statSync(sb.credPath).mode & 0o777).toBe(0o600);
  });

  it('creates a not-yet-existing data dir (inference: file lives at <data dir>/credentials.json)', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply()).on('POST', '/v1/auth/token', tokenReply());
    const nested = join(sb.root, 'fresh', 'dir');
    const r = await runCli(['login'], { apiUrl: api.url, ...sb, dataDir: nested });
    expect(r.code).toBe(0);
    expect(existsSync(join(nested, 'credentials.json'))).toBe(true);
  });

  it('access_denied -> stderr "Login denied in the browser." exit 1, no credentials', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', err('authorization_pending'), err('access_denied'));
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe(loginBlock());
    expect(r.stderr).toBe('Login denied in the browser.\n');
    expect(r.code).toBe(1);
    expect(existsSync(sb.credPath)).toBe(false);
  });

  it('expired_token -> stderr "The code expired. Run tryaii login again." exit 1', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', err('expired_token'));
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe(loginBlock());
    expect(r.stderr).toBe('The code expired. Run tryaii login again.\n');
    expect(r.code).toBe(1);
    expect(existsSync(sb.credPath)).toBe(false);
  });

  it('network failure (unreachable port) -> stderr "Could not reach <api_url>..." exit 1', async () => {
    const sb = sandbox();
    const url = await deadUrl();
    const r = await runCli(['login'], { apiUrl: url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe(couldNotReach(url));
    expect(r.code).toBe(1);
    expect(existsSync(sb.credPath)).toBe(false);
  });

  it('unexpected status while polling (500) maps to network', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', { status: 500, raw: 'Internal Server Error' });
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe(loginBlock());
    expect(r.stderr).toBe(couldNotReach(api.url));
    expect(r.code).toBe(1);
  });

  it('non-JSON body from device/code maps to network', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', { status: 200, raw: '<html>nope</html>' });
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe(couldNotReach(api.url));
    expect(r.code).toBe(1);
  });

  it('other error code while polling -> "Login failed: <error>." exit 1', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', err('invalid_grant'));
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe(loginBlock());
    expect(r.stderr).toBe('Login failed: invalid_grant.\n');
    expect(r.code).toBe(1);
  });

  it('other error code from device/code (400 invalid_client) -> "Login failed: invalid_client." exit 1', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', err('invalid_client'));
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe('Login failed: invalid_client.\n');
    expect(r.code).toBe(1);
  });

  it('polling honours the server interval (1 s): gaps >= 1 s and not a hard-coded 5 s', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply({ interval: 1 }))
      .on('POST', '/v1/auth/token', err('authorization_pending'), err('authorization_pending'), tokenReply());
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);
    const polls = api.reqs('POST', '/v1/auth/token');
    expect(polls.length).toBe(3);
    const gaps = polls.slice(1).map((p, i) => p.t - polls[i].t);
    for (const g of gaps) {
      expect(g).toBeGreaterThanOrEqual(950);
      expect(g).toBeLessThan(3000);
    }
  });

  it('slow_down adds 5 s to the interval (1 s -> 6 s)', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply({ interval: 1 }))
      .on('POST', '/v1/auth/token', err('authorization_pending'), err('slow_down'), tokenReply());
    const r = await runCli(['login'], { apiUrl: api.url, ...sb, timeoutMs: 40000 });
    expect(r.code).toBe(0);
    const polls = api.reqs('POST', '/v1/auth/token');
    expect(polls.length).toBe(3);
    const g1 = polls[1].t - polls[0].t;
    const g2 = polls[2].t - polls[1].t;
    expect(g1).toBeGreaterThanOrEqual(950);
    expect(g1).toBeLessThan(3000);
    expect(g2).toBeGreaterThanOrEqual(5950);
    expect(g2).toBeLessThan(9000);
    expect(r.stdout).toBe(loginBlock() + 'Logged in as a@b.com.\n' + LOGIN_CATALOG_FAILED);
  }, 45000);

  it('already logged in: notice first line, normal flow, new session stored, old one revoked', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', tokenReply())
      .on('POST', '/v1/auth/revoke', { status: 200, json: {} });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stderr).toBe('');
    expect(r.stdout).toBe(
      'Already logged in as old@b.com. Signing in again replaces that session.\n'
      + loginBlock() + 'Logged in as a@b.com.\n' + LOGIN_CATALOG_FAILED,
    );
    expect(r.code).toBe(0);
    const c = JSON.parse(readFileSync(sb.credPath, 'utf8'));
    expect(c.refresh_token).toBe('tair_new-refresh');
    expect(c.user.email).toBe('a@b.com');
    const rev = api.reqs('POST', '/v1/auth/revoke');
    expect(rev.length).toBe(1);
    expect(rev[0].body).toEqual({ refresh_token: 'tair_old-refresh', client_id: 'tryaii-cli' });
    // revocation happens after success, i.e. after the winning poll
    const lastPoll = api.reqs('POST', '/v1/auth/token').at(-1)!;
    expect(rev[0].t).toBeGreaterThanOrEqual(lastPoll.t);
  });

  it('already logged in: failing old-session revoke is ignored (best effort)', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', tokenReply())
      .on('POST', '/v1/auth/revoke', { status: 500, raw: 'boom' });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
    expect(r.stdout.endsWith('Logged in as a@b.com.\n' + LOGIN_CATALOG_FAILED)).toBe(true);
    expect(JSON.parse(readFileSync(sb.credPath, 'utf8')).refresh_token).toBe('tair_new-refresh');
  });

  it('already logged in + denied: old session is neither revoked nor replaced', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', err('access_denied'))
      .on('POST', '/v1/auth/revoke', { status: 200, json: {} });
    const seeded = seedCreds(sb.credPath, api.url);
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe(
      'Already logged in as old@b.com. Signing in again replaces that session.\n' + loginBlock(),
    );
    expect(r.stderr).toBe('Login denied in the browser.\n');
    expect(r.code).toBe(1);
    expect(api.reqs('POST', '/v1/auth/revoke').length).toBe(0);
    expect(readFileSync(sb.credPath, 'utf8')).toBe(seeded);
  });

  it.skipIf(IS_WIN)('Ctrl+C (SIGINT) while polling -> stderr "Login cancelled." exit 130', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', err('authorization_pending'));
    let kill: ((s: NodeJS.Signals) => void) | undefined;
    const p = runCli(['login'], { apiUrl: api.url, ...sb, onSpawn: (k) => { kill = k; } });
    const deadline = Date.now() + 10000;
    while (api.reqs('POST', '/v1/auth/token').length === 0 && Date.now() < deadline) {
      await new Promise((ok) => setTimeout(ok, 50));
    }
    kill!('SIGINT');
    const r = await p;
    expect(r.stderr).toBe('Login cancelled.\n');
    expect(r.code).toBe(130);
    expect(existsSync(sb.credPath)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// logout
// ---------------------------------------------------------------------------

describe('logout (contract section 6)', () => {
  it('logged in via file: revokes, deletes the file, prints "Logged out." exit 0', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/revoke', { status: 200, json: {} });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['logout'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('Logged out.\n');
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
    expect(existsSync(sb.credPath)).toBe(false);
    const rev = api.reqs('POST', '/v1/auth/revoke');
    expect(rev.length).toBe(1);
    expect(rev[0].body).toEqual({ refresh_token: 'tair_old-refresh', client_id: 'tryaii-cli' });
    expect(rev[0].headers['content-type']).toMatch(/^application\/json/);
    expect(String(rev[0].headers['user-agent'])).toMatch(/^tryaii\/\S+/);
  });

  it('revoke failure (unreachable server) is ignored: still "Logged out." exit 0', async () => {
    const sb = sandbox();
    const url = await deadUrl();
    seedCreds(sb.credPath, url);
    const r = await runCli(['logout'], { apiUrl: url, ...sb });
    expect(r.stdout).toBe('Logged out.\n');
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
    expect(existsSync(sb.credPath)).toBe(false);
  });

  it('revoke failure (500) is ignored: still "Logged out." exit 0', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/revoke', { status: 500, raw: 'boom' });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['logout'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('Logged out.\n');
    expect(r.code).toBe(0);
    expect(existsSync(sb.credPath)).toBe(false);
  });

  it('not logged in: "Not logged in." exit 0', async () => {
    const sb = sandbox();
    const api = await fake();
    const r = await runCli(['logout'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('Not logged in.\n');
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
  });

  it('TRYAII_TOKEN set: ignored, the file session is logged out as usual', async () => {
    // v1.1 (section 7, item 13): TRYAII_TOKEN is not supported and is ignored.
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/revoke', { status: 200, json: {} });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['logout'], { apiUrl: api.url, ...sb, env: { TRYAII_TOKEN: 'tair_env-token' } });
    expect(r.stdout).toBe('Logged out.\n');
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
    expect(existsSync(sb.credPath)).toBe(false);
    const rev = api.reqs('POST', '/v1/auth/revoke');
    expect(rev.map((x) => x.body.refresh_token)).toEqual(['tair_old-refresh']);
    for (const q of api.requests) expect(q.raw).not.toContain('tair_env-token');
  });
});

// ---------------------------------------------------------------------------
// whoami
// ---------------------------------------------------------------------------

describe('whoami (contract section 6)', () => {
  it('success with a fresh access token: no refresh, Bearer /me, exact text', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('GET', '/v1/auth/me', meReply());
    const seeded = seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() + 3600e3) });
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.stderr).toBe('');
    expect(r.stdout).toBe('Logged in as a@b.com (A B)\nEntitlements: catalog:full\n' + WHOAMI_NO_CATALOG);
    expect(r.code).toBe(0);
    expect(api.reqs('POST', '/v1/auth/token').length).toBe(0);
    const me = api.reqs('GET', '/v1/auth/me');
    expect(me.length).toBe(1);
    expect(me[0].headers.authorization).toBe('Bearer eyJ.old-access');
    expect(String(me[0].headers['user-agent'])).toMatch(/^tryaii\/\S+/);
    expect(readFileSync(sb.credPath, 'utf8')).toBe(seeded);
  });

  it('entitlements: comma+space joined, or "none"', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('GET', '/v1/auth/me',
      meReply({ entitlements: ['catalog:full', 'beta:x'] }),
      meReply({ entitlements: [] }));
    seedCreds(sb.credPath, api.url);
    const r1 = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r1.stdout).toBe('Logged in as a@b.com (A B)\nEntitlements: catalog:full, beta:x\n' + WHOAMI_NO_CATALOG);
    expect(r1.code).toBe(0);
    const r2 = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r2.stdout).toBe('Logged in as a@b.com (A B)\nEntitlements: none\n' + WHOAMI_NO_CATALOG);
    expect(r2.code).toBe(0);
  });

  it('--json: the /me JSON pretty-printed with 2-space indent, keys in server order', async () => {
    const sb = sandbox();
    const api = await fake();
    // deliberately non-alphabetical, non-default order
    const raw = '{"session":{"created_at":"2026-10-03T12:00:00Z","id":"fam123"},'
      + '"user":{"name":"A B","id":"u-1","email":"a@b.com"},"entitlements":["catalog:full","z:y"]}';
    api.on('GET', '/v1/auth/me', { status: 200, raw });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['whoami', '--json'], { apiUrl: api.url, ...sb });
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
    const expected = [
      '{',
      '  "session": {',
      '    "created_at": "2026-10-03T12:00:00Z",',
      '    "id": "fam123"',
      '  },',
      '  "user": {',
      '    "name": "A B",',
      '    "id": "u-1",',
      '    "email": "a@b.com"',
      '  },',
      '  "entitlements": [',
      '    "catalog:full",',
      '    "z:y"',
      '  ]',
      '}',
    ].join('\n') + '\n';
    expect(r.stdout).toBe(expected);
  });

  it('not logged in: stderr "Not logged in. Run: tryaii login" exit 1, no requests', async () => {
    const sb = sandbox();
    const api = await fake();
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe('Not logged in. Run: tryaii login\n');
    expect(r.code).toBe(1);
    expect(api.requests.length).toBe(0);
  });

  it('expired access token: refreshes first, persists the rotated tokens, uses the new access token', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/token', tokenReply({
      access_token: 'eyJ.rotated-access', refresh_token: 'tair_rotated', expires_in: 3600,
      refresh_expires_in: 7776000,
    })).on('GET', '/v1/auth/me', meReply());
    seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() - 10e3) });
    const before = Date.now();
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    const after = Date.now();
    expect(r.stderr).toBe('');
    expect(r.stdout).toBe('Logged in as a@b.com (A B)\nEntitlements: catalog:full\n' + WHOAMI_NO_CATALOG);
    expect(r.code).toBe(0);

    const tok = api.reqs('POST', '/v1/auth/token');
    expect(tok.length).toBe(1);
    expect(tok[0].body).toEqual({ grant_type: 'refresh_token', refresh_token: 'tair_old-refresh', client_id: 'tryaii-cli' });
    expect(tok[0].headers['content-type']).toMatch(/^application\/json/);
    const me = api.reqs('GET', '/v1/auth/me');
    expect(me.length).toBe(1);
    expect(me[0].t).toBeGreaterThanOrEqual(tok[0].t);
    expect(me[0].headers.authorization).toBe('Bearer eyJ.rotated-access');

    const c = JSON.parse(readFileSync(sb.credPath, 'utf8'));
    expect(c.refresh_token).toBe('tair_rotated');
    expect(c.access_token).toBe('eyJ.rotated-access');
    for (const k of ['refresh_expires_at', 'access_expires_at', 'created_at']) expect(c[k]).toMatch(ISO_Z);
    expect(Date.parse(c.access_expires_at)).toBeGreaterThanOrEqual(before + 3600e3 - 5000);
    expect(Date.parse(c.access_expires_at)).toBeLessThanOrEqual(after + 3600e3 + 5000);
    expect(Date.parse(c.refresh_expires_at)).toBeGreaterThanOrEqual(before + 7776000e3 - 5000);
    expect(Object.keys(c).sort()).toEqual([
      'access_expires_at', 'access_token', 'api_url', 'created_at', 'entitlements',
      'refresh_expires_at', 'refresh_token', 'user', 'version',
    ]);
    expect(readdirSync(sb.dataDir)).toEqual(['credentials.json']);

    // A second whoami uses the persisted rotated access token without refreshing.
    const r2 = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r2.code).toBe(0);
    expect(api.reqs('POST', '/v1/auth/token').length).toBe(1);
    expect(api.reqs('GET', '/v1/auth/me').at(-1)!.headers.authorization).toBe('Bearer eyJ.rotated-access');
  });

  it('access token within 60 s of expiry (30 s left) is refreshed', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/token', tokenReply({ access_token: 'eyJ.rotated-access', refresh_token: 'tair_rotated' }))
      .on('GET', '/v1/auth/me', meReply());
    seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() + 30e3) });
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);
    expect(api.reqs('POST', '/v1/auth/token').length).toBe(1);
    expect(api.reqs('GET', '/v1/auth/me')[0].headers.authorization).toBe('Bearer eyJ.rotated-access');
    expect(JSON.parse(readFileSync(sb.credPath, 'utf8')).refresh_token).toBe('tair_rotated');
  });

  it('access token with 120 s left is NOT refreshed', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/token', tokenReply()).on('GET', '/v1/auth/me', meReply());
    seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() + 120e3) });
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);
    expect(api.reqs('POST', '/v1/auth/token').length).toBe(0);
    expect(api.reqs('GET', '/v1/auth/me')[0].headers.authorization).toBe('Bearer eyJ.old-access');
  });

  it('refresh rejected (invalid_grant): deletes file, stderr session-ended, exit 1', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/token', err('invalid_grant')).on('GET', '/v1/auth/me', meReply());
    seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() - 10e3) });
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe(SESSION_ENDED);
    expect(r.code).toBe(1);
    expect(existsSync(sb.credPath)).toBe(false);
    expect(api.reqs('GET', '/v1/auth/me').length).toBe(0);
  });

  it('/me 401 invalid_token: deletes file, stderr session-ended, exit 1', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('GET', '/v1/auth/me', { status: 401, json: { error: 'invalid_token' } });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe(SESSION_ENDED);
    expect(r.code).toBe(1);
    expect(existsSync(sb.credPath)).toBe(false);
  });

  it('network failure (unreachable port): stderr could-not-reach, exit 1, file kept', async () => {
    const sb = sandbox();
    const url = await deadUrl();
    const seeded = seedCreds(sb.credPath, url);
    const r = await runCli(['whoami'], { apiUrl: url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe(couldNotReach(url));
    expect(r.code).toBe(1);
    expect(readFileSync(sb.credPath, 'utf8')).toBe(seeded);
  });

  it('network failure during refresh: could-not-reach, exit 1, file kept', async () => {
    const sb = sandbox();
    const url = await deadUrl();
    const seeded = seedCreds(sb.credPath, url, { access_expires_at: iso(Date.now() - 10e3) });
    const r = await runCli(['whoami'], { apiUrl: url, ...sb });
    expect(r.stderr).toBe(couldNotReach(url));
    expect(r.code).toBe(1);
    expect(readFileSync(sb.credPath, 'utf8')).toBe(seeded);
  });

  it('/me 500 (unexpected status) maps to network; file kept', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('GET', '/v1/auth/me', { status: 500, raw: 'oops' });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe(couldNotReach(api.url));
    expect(r.code).toBe(1);
    expect(existsSync(sb.credPath)).toBe(true);
  });

  it('/me 200 with a non-JSON body maps to network', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('GET', '/v1/auth/me', { status: 200, raw: 'definitely not json' });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe(couldNotReach(api.url));
    expect(r.code).toBe(1);
  });

  it('a /me that never answers times out (~10 s) and maps to network', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('GET', '/v1/auth/me', { hang: true });
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb, timeoutMs: 30000 });
    expect(r.stderr).toBe(couldNotReach(api.url));
    expect(r.code).toBe(1);
    expect(r.ms).toBeGreaterThanOrEqual(9000);
    expect(r.ms).toBeLessThan(20000);
  }, 35000);
});

// ---------------------------------------------------------------------------
// TRYAII_TOKEN
// ---------------------------------------------------------------------------

describe('TRYAII_TOKEN (contract section 6, Configuration; v1.1 item 13: ignored)', () => {
  it('whoami with only TRYAII_TOKEN: ignored -> not logged in, no requests, writes nothing', async () => {
    // v1.1 (section 7, item 13): TRYAII_TOKEN is not supported and is ignored.
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/token', tokenReply({ access_token: 'eyJ.env-access', refresh_token: 'tair_env-rotated' }))
      .on('GET', '/v1/auth/me', meReply());
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb, env: { TRYAII_TOKEN: 'tair_env-token' } });
    expect(r.stdout).toBe('');
    expect(r.stderr).toBe('Not logged in. Run: tryaii login\n');
    expect(r.code).toBe(1);
    expect(api.requests.length).toBe(0);
    expect(readdirSync(sb.dataDir)).toEqual([]);
    expect(existsSync(join(sb.home, '.tryaii'))).toBe(false);
  });

  it('TRYAII_TOKEN does not replace an existing credentials file, which is used as usual', async () => {
    // v1.1 (section 7, item 13): the file session wins; the env value is never sent.
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/token', tokenReply({ access_token: 'eyJ.env-access', refresh_token: 'tair_env-rotated' }))
      .on('GET', '/v1/auth/me', meReply());
    const seeded = seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() + 3600e3) });
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb, env: { TRYAII_TOKEN: 'tair_env-token' } });
    expect(r.code).toBe(0);
    expect(api.reqs('POST', '/v1/auth/token').length).toBe(0);
    expect(api.reqs('GET', '/v1/auth/me')[0].headers.authorization).toBe('Bearer eyJ.old-access');
    expect(readFileSync(sb.credPath, 'utf8')).toBe(seeded);
    expect(readdirSync(sb.dataDir)).toEqual(['credentials.json']);
    for (const q of api.requests) expect(q.raw).not.toContain('tair_env-token');
  });

  it('TRYAII_TOKEN plays no part in a refresh: the file token is rotated and persisted', async () => {
    // v1.1 (section 7, item 13): a server that would reject the env token
    // never sees it, so it cannot end the file session.
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/token', (req) => (
      req.body?.refresh_token === 'tair_old-refresh'
        ? tokenReply({ refresh_token: 'tair_rotated' })
        : err('invalid_grant')))
      .on('GET', '/v1/auth/me', meReply());
    seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() - 10e3) });
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb, env: { TRYAII_TOKEN: 'tair_env-token' } });
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
    expect(api.reqs('POST', '/v1/auth/token').map((q) => q.body.refresh_token)).toEqual(['tair_old-refresh']);
    expect(JSON.parse(readFileSync(sb.credPath, 'utf8')).refresh_token).toBe('tair_rotated');
  });
});

// ---------------------------------------------------------------------------
// flags + help
// ---------------------------------------------------------------------------

describe('flags and help text (contract section 6)', () => {
  for (const args of [['login', '--bogus'], ['login', '--json'], ['logout', '--bogus'], ['whoami', '--bogus']]) {
    it(`bad flag: ${args.join(' ')} -> exit 2, no requests, no credentials`, async () => {
      const sb = sandbox();
      const api = await fake();
      api.on('POST', '/v1/auth/device/code', deviceCodeReply()).on('POST', '/v1/auth/token', tokenReply());
      const r = await runCli(args, { apiUrl: api.url, ...sb });
      expect(r.code).toBe(2);
      expect(api.requests.length).toBe(0);
      expect(existsSync(sb.credPath)).toBe(false);
    });
  }

  for (const [cmd, block] of [['login', 'HELP_LOGIN'], ['logout', 'HELP_LOGOUT'], ['whoami', 'HELP_WHOAMI']]) {
    it(`tryaii help ${cmd} equals the spec's ${block} block`, async () => {
      const sb = sandbox();
      const api = await fake();
      const r = await runCli(['help', cmd], { apiUrl: api.url, ...sb });
      expect(r.code).toBe(0);
      // Catalog contract section 5 amends HELP_LOGIN.
      expect(r.stdout).toBe((block === 'HELP_LOGIN' ? catalogHelpLogin() : specBlock(block)) + '\n');
      expect(api.requests.length).toBe(0);
    });
  }

  it('tryaii login -h prints HELP_LOGIN and does not start a sign-in', async () => {
    const sb = sandbox();
    const api = await fake();
    const r = await runCli(['login', '-h'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);
    expect(r.stdout).toBe(catalogHelpLogin() + '\n'); // catalog contract section 5
    expect(api.requests.length).toBe(0);
  });

  it('global help lists login/logout/whoami right after the regenerate line', async () => {
    const sb = sandbox();
    const api = await fake();
    const r = await runCli(['help'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);
    const spec = readFileSync(SPEC, 'utf8').replace(/\r\n/g, '\n');
    const m = /Global `HELP` gains[^\n]*\n```\n([\s\S]*?)\n```/.exec(spec);
    expect(m).not.toBeNull();
    const lines = r.stdout.split('\n');
    const i = lines.findIndex((l) => /^\s+regenerate\s/.test(l));
    expect(i).toBeGreaterThanOrEqual(0);
    // Catalog contract section 5 replaces the login line.
    const expected = m![1].split('\n');
    expected[0] = catalogHelpLoginLine();
    expect(lines.slice(i + 1, i + 4).join('\n')).toBe(expected.join('\n'));
  });
});

// ---------------------------------------------------------------------------
// v1.1 amendments (contract section 7, items 13-21)
// ---------------------------------------------------------------------------

const RATE_LIMITED = 'Too many sign-in attempts. Wait a minute and try again.\n';
const CANNOT_DELETE = 'Could not remove the credentials file.\n';
const unexpected = (u: string, code: string) => `Unexpected response from ${u}: ${code}.\n`;

describe('v1.1 item 13: TRYAII_API_URL is for login only', () => {
  for (const expired of [false, true]) {
    it(`whoami (${expired ? 'expired' : 'fresh'} token) talks to the stored api_url, never TRYAII_API_URL`, async () => {
      const sb = sandbox();
      const stored = await fake();
      const other = await fake();
      for (const f of [stored, other]) {
        f.on('POST', '/v1/auth/token', tokenReply({ refresh_token: 'tair_rotated' })).on('GET', '/v1/auth/me', meReply());
      }
      seedCreds(sb.credPath, stored.url, { access_expires_at: iso(Date.now() + (expired ? -10e3 : 3600e3)) });
      const r = await runCli(['whoami'], { apiUrl: other.url, ...sb });
      expect(r.stderr).toBe('');
      expect(r.stdout).toBe('Logged in as a@b.com (A B)\nEntitlements: catalog:full\n' + WHOAMI_NO_CATALOG);
      expect(r.code).toBe(0);
      expect(other.requests.length).toBe(0);
      expect(stored.requests.map((q) => q.path)).toEqual(
        expired ? ['/v1/auth/token', '/v1/auth/me'] : ['/v1/auth/me'],
      );
      expect(JSON.parse(readFileSync(sb.credPath, 'utf8')).api_url).toBe(stored.url);
    });
  }

  it('a TRYAII_API_URL host that would reject the token is never contacted; file kept', async () => {
    const sb = sandbox();
    const stored = await fake();
    const other = await fake();
    other.on('POST', '/v1/auth/token', err('invalid_grant'));
    stored.on('POST', '/v1/auth/token', tokenReply()).on('GET', '/v1/auth/me', meReply());
    seedCreds(sb.credPath, stored.url, { access_expires_at: iso(Date.now() - 10e3) });
    const r = await runCli(['whoami'], { apiUrl: other.url, ...sb });
    expect(r.code).toBe(0);
    expect(other.requests.length).toBe(0);
    expect(existsSync(sb.credPath)).toBe(true);
  });

  it('whoami network line names the stored api_url', async () => {
    const sb = sandbox();
    const api = await fake();
    const dead = await deadUrl();
    seedCreds(sb.credPath, dead);
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.stderr).toBe(couldNotReach(dead));
    expect(r.code).toBe(1);
    expect(api.requests.length).toBe(0);
  });

  it('logout revokes at the stored api_url', async () => {
    const sb = sandbox();
    const stored = await fake();
    const other = await fake();
    stored.on('POST', '/v1/auth/revoke', { status: 200, json: {} });
    seedCreds(sb.credPath, stored.url);
    const r = await runCli(['logout'], { apiUrl: other.url, ...sb });
    expect([r.code, r.stdout, r.stderr]).toEqual([0, 'Logged out.\n', '']);
    expect(other.requests.length).toBe(0);
    expect(stored.reqs('POST', '/v1/auth/revoke').map((q) => q.body.refresh_token)).toEqual(['tair_old-refresh']);
  });

  it('login uses TRYAII_API_URL and revokes the replaced session at its own api_url', async () => {
    const sb = sandbox();
    const oldHost = await fake();
    const newHost = await fake();
    oldHost.on('POST', '/v1/auth/revoke', { status: 200, json: {} });
    newHost.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', tokenReply())
      .on('POST', '/v1/auth/revoke', { status: 200, json: {} });
    seedCreds(sb.credPath, oldHost.url);
    const r = await runCli(['login'], { apiUrl: newHost.url, ...sb });
    expect(r.code).toBe(0);
    expect(JSON.parse(readFileSync(sb.credPath, 'utf8')).api_url).toBe(newHost.url);
    expect(newHost.reqs('POST', '/v1/auth/revoke').length).toBe(0);
    expect(oldHost.reqs('POST', '/v1/auth/revoke').map((q) => q.body.refresh_token)).toEqual(['tair_old-refresh']);
  });
});

describe('v1.1 item 14: only invalid_grant (refresh) or /me 401 end the session', () => {
  const cases: Array<[string, string, string, Reply, boolean, string]> = [
    ['refresh invalid_request', 'POST', '/v1/auth/token', err('invalid_request'), true, 'invalid_request'],
    ['refresh 401 invalid_client', 'POST', '/v1/auth/token', err('invalid_client', 401), true, 'invalid_client'],
    ['refresh unsupported_grant_type', 'POST', '/v1/auth/token', err('unsupported_grant_type'), true, 'unsupported_grant_type'],
    ['/me 400 invalid_request', 'GET', '/v1/auth/me', err('invalid_request'), false, 'invalid_request'],
  ];
  for (const [label, method, path, reply, expired, code] of cases) {
    it(`${label}: "Unexpected response from <api_url>: ${code}." exit 1, file kept`, async () => {
      const sb = sandbox();
      const api = await fake();
      api.on(method, path, reply);
      const seeded = seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() + (expired ? -10e3 : 3600e3)) });
      const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
      expect(r.stdout).toBe('');
      expect(r.stderr).toBe(unexpected(api.url, code));
      expect(r.code).toBe(1);
      expect(readFileSync(sb.credPath, 'utf8')).toBe(seeded);
    });
  }

  it('/me 401 with any error code ends the session', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('GET', '/v1/auth/me', err('invalid_client', 401));
    seedCreds(sb.credPath, api.url);
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect([r.code, r.stdout, r.stderr]).toEqual([1, '', SESSION_ENDED]);
    expect(existsSync(sb.credPath)).toBe(false);
  });
});

describe('v1.1 item 15: HTTP 429 = rate_limited', () => {
  for (const [label, reply] of [
    ['JSON body', { status: 429, json: { error: 'slow_down' } }],
    ['text body', { status: 429, raw: 'Too Many Requests' }],
  ] as Array<[string, Reply]>) {
    it(`login: 429 on device/code (${label}) -> rate-limited line, exit 1`, async () => {
      const sb = sandbox();
      const api = await fake();
      api.on('POST', '/v1/auth/device/code', reply);
      const r = await runCli(['login'], { apiUrl: api.url, ...sb });
      expect([r.code, r.stdout, r.stderr]).toEqual([1, '', RATE_LIMITED]);
      expect(existsSync(sb.credPath)).toBe(false);
    });
  }

  it('login: 429 while polling stops polling -> rate-limited line', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', { status: 429, json: { error: 'slow_down' } });
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect([r.code, r.stdout, r.stderr]).toEqual([1, loginBlock(), RATE_LIMITED]);
    expect(api.reqs('POST', '/v1/auth/token').length).toBe(1);
  });

  it('login: 503 stays network', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', { status: 503, json: { error: 'temporarily_unavailable' } });
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect([r.code, r.stdout, r.stderr]).toEqual([1, '', couldNotReach(api.url)]);
  });

  for (const [label, method, path, expired] of [
    ['refresh', 'POST', '/v1/auth/token', true],
    ['/me', 'GET', '/v1/auth/me', false],
  ] as Array<[string, string, string, boolean]>) {
    it(`whoami: 429 on ${label} -> "Unexpected response ...: rate_limited." file kept`, async () => {
      const sb = sandbox();
      const api = await fake();
      api.on(method, path, { status: 429, json: { error: 'slow_down' } });
      const seeded = seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() + (expired ? -10e3 : 3600e3)) });
      const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
      expect([r.code, r.stdout, r.stderr]).toEqual([1, '', unexpected(api.url, 'rate_limited')]);
      expect(readFileSync(sb.credPath, 'utf8')).toBe(seeded);
    });
  }
});

describe('v1.1 item 16: redirects are never followed', () => {
  for (const status of [301, 302, 307, 308]) {
    it(`whoami: /me ${status} -> network, target never hit, file kept`, async () => {
      const sb = sandbox();
      const api = await fake();
      api.on('GET', '/v1/auth/me', { status, raw: '', headers: { Location: `${api.url}/elsewhere` } })
        .on('GET', '/elsewhere', meReply());
      seedCreds(sb.credPath, api.url);
      const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
      expect([r.code, r.stdout, r.stderr]).toEqual([1, '', couldNotReach(api.url)]);
      expect(api.requests.map((q) => q.path)).toEqual(['/v1/auth/me']);
      expect(existsSync(sb.credPath)).toBe(true);
    });
  }

  for (const status of [302, 307]) {
    it(`login: device/code ${status} -> network, target never hit`, async () => {
      const sb = sandbox();
      const api = await fake();
      api.on('POST', '/v1/auth/device/code', { status, raw: '', headers: { Location: `${api.url}/elsewhere` } })
        .on('POST', '/elsewhere', deviceCodeReply())
        .on('GET', '/elsewhere', deviceCodeReply());
      const r = await runCli(['login'], { apiUrl: api.url, ...sb });
      expect([r.code, r.stdout, r.stderr]).toEqual([1, '', couldNotReach(api.url)]);
      expect(api.requests.map((q) => q.path)).toEqual(['/v1/auth/device/code']);
    });
  }
});

describe('v1.1 item 17: polling defaults and the local deadline', () => {
  it('interval 0 defaults to 5 s', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply({ interval: 0 }))
      .on('POST', '/v1/auth/token', tokenReply());
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
    const gap = api.reqs('POST', '/v1/auth/token')[0].t - api.reqs('POST', '/v1/auth/device/code')[0].t;
    expect(gap).toBeGreaterThanOrEqual(4800);
  }, 20000);

  for (const expiresIn of [0, -5]) {
    it(`expires_in ${expiresIn} defaults to 600 s (login still succeeds)`, async () => {
      const sb = sandbox();
      const api = await fake();
      api.on('POST', '/v1/auth/device/code', deviceCodeReply({ interval: 1, expires_in: expiresIn }))
        .on('POST', '/v1/auth/token', err('authorization_pending'), err('authorization_pending'), tokenReply());
      const r = await runCli(['login'], { apiUrl: api.url, ...sb });
      expect([r.code, r.stdout, r.stderr]).toEqual([0, loginBlock() + 'Logged in as a@b.com.\n' + LOGIN_CATALOG_FAILED, '']);
    });
  }

  it('an approval in the last interval wins (one final poll after the deadline)', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/device/code', deviceCodeReply({ interval: 2, expires_in: 1 }))
      .on('POST', '/v1/auth/token', tokenReply());
    const r = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect([r.code, r.stdout, r.stderr]).toEqual([0, loginBlock() + 'Logged in as a@b.com.\n' + LOGIN_CATALOG_FAILED, '']);
  });

  for (const late of ['authorization_pending', 'slow_down', 'invalid_grant']) {
    it(`${late} after the local deadline -> "The code expired." after exactly one poll`, async () => {
      const sb = sandbox();
      const api = await fake();
      api.on('POST', '/v1/auth/device/code', deviceCodeReply({ interval: 2, expires_in: 1 }))
        .on('POST', '/v1/auth/token', err(late));
      const r = await runCli(['login'], { apiUrl: api.url, ...sb });
      expect([r.code, r.stdout, r.stderr]).toEqual([1, loginBlock(), 'The code expired. Run tryaii login again.\n']);
      expect(api.reqs('POST', '/v1/auth/token').length).toBe(1);
    });
  }
});

describe('v1.1 item 18: credentials write', () => {
  it('a refresh leaves only credentials.json (0600 on POSIX)', async () => {
    const sb = sandbox();
    const api = await fake();
    api.on('POST', '/v1/auth/token', tokenReply({ refresh_token: 'tair_rotated' })).on('GET', '/v1/auth/me', meReply());
    seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() - 10e3) });
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);
    expect(readdirSync(sb.dataDir)).toEqual(['credentials.json']);
    expect(JSON.parse(readFileSync(sb.credPath, 'utf8')).refresh_token).toBe('tair_rotated');
    if (!IS_WIN) expect(statSync(sb.credPath).mode & 0o777).toBe(0o600);
  });

  it.skipIf(!IS_WIN)('Windows: a briefly open credentials file does not lose the rotated token', async () => {
    const sb = sandbox();
    const api = await fake();
    let held = false;
    api.on('POST', '/v1/auth/token', () => {
      // Another process (antivirus, indexer) holds the file for 120 ms.
      const fd = openSync(sb.credPath, 'r');
      held = true;
      setTimeout(() => closeSync(fd), 120);
      return tokenReply({ refresh_token: 'tair_rotated' });
    }).on('GET', '/v1/auth/me', meReply());
    seedCreds(sb.credPath, api.url, { access_expires_at: iso(Date.now() - 10e3) });
    const r = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect(r.stderr).toBe('');
    expect(r.code).toBe(0);
    expect(held).toBe(true);
    expect(JSON.parse(readFileSync(sb.credPath, 'utf8')).refresh_token).toBe('tair_rotated');
  });
});

describe('v1.1 item 19: logout cannot delete', () => {
  it('stderr "Could not remove the credentials file." exit 1', async () => {
    const sb = sandbox();
    const api = await fake();
    mkdirSync(sb.credPath);
    writeFileSync(join(sb.credPath, 'keep'), 'x');
    const r = await runCli(['logout'], { apiUrl: api.url, ...sb });
    expect([r.code, r.stdout, r.stderr]).toEqual([1, '', CANNOT_DELETE]);
    expect(statSync(sb.credPath).isDirectory()).toBe(true);
  });
});

describe('v1.1 item 20: non-ASCII account text is printed as UTF-8', () => {
  it('login / whoami / logout with a non-cp1252 email and name', async () => {
    const sb = sandbox();
    const api = await fake();
    const email = 'dév.測試@例子.example';
    const name = 'Zoë 測試';
    api.on('POST', '/v1/auth/device/code', deviceCodeReply())
      .on('POST', '/v1/auth/token', tokenReply({ user: { id: 'u-1', email, name } }))
      .on('GET', '/v1/auth/me', meReply({ user: { id: 'u-1', email, name } }))
      .on('POST', '/v1/auth/revoke', { status: 200, json: {} });
    const r1 = await runCli(['login'], { apiUrl: api.url, ...sb });
    expect([r1.code, r1.stdout, r1.stderr]).toEqual([0, loginBlock() + `Logged in as ${email}.\n` + LOGIN_CATALOG_FAILED, '']);
    const r2 = await runCli(['whoami'], { apiUrl: api.url, ...sb });
    expect([r2.code, r2.stdout, r2.stderr]).toEqual([0, `Logged in as ${email} (${name})\nEntitlements: catalog:full\n` + WHOAMI_NO_CATALOG, '']);
    const r3 = await runCli(['logout'], { apiUrl: api.url, ...sb });
    expect([r3.code, r3.stdout, r3.stderr]).toEqual([0, 'Logged out.\n', '']);
  });
});

describe('v1.1 item 21: help topics', () => {
  it('tryaii help help lists login, logout and whoami as topics', async () => {
    const sb = sandbox();
    const api = await fake();
    const r = await runCli(['help', 'help'], { apiUrl: api.url, ...sb });
    expect(r.code).toBe(0);
    const topics = r.stdout.slice(r.stdout.indexOf('Topics:') + 'Topics:'.length, r.stdout.indexOf('Examples:'));
    const names = topics.replace(/\n/g, ' ').split(',').map((t) => t.trim());
    for (const cmd of ['login', 'logout', 'whoami']) expect(names).toContain(cmd);
  });
});
