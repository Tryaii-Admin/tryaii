/**
 * CLI sign-in (login / logout / whoami) -- docs/auth/CONTRACT-auth-v1.md
 * section 6. Mirrors the Python auth tests. House style: no mock
 * libraries; the fake server is an injected fetchFn, sleep and clock are
 * injected, and every test runs against a temp TRYAII_DRE_DATA_DIR (the
 * real ~/.tryaii is never touched).
 */

import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  AuthDeps,
  AuthNetworkError,
  AuthRateLimitedError,
  Credentials,
  credentialsPath,
  deleteCredentials,
  renameWithRetry,
  requestJson,
  runLogin,
  runLogout,
  runWhoami,
  saveCredentials,
} from '../src/auth/index.js';

const API = 'http://api.test';
const T0 = Date.parse('2026-10-03T12:00:00Z');

type Reply = { status: number; body: unknown } | 'network';

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: any;
}

/** A scripted fake api server: per-path reply queues (last reply repeats). */
function fakeServer(routes: Record<string, Reply[]>) {
  const calls: Call[] = [];
  const fetchFn = (async (url: any, init: any) => {
    const u = new URL(String(url));
    calls.push({
      url: String(url),
      method: init.method,
      headers: init.headers,
      body: init.body ? JSON.parse(init.body) : undefined,
    });
    const queue = routes[u.pathname];
    if (!queue || queue.length === 0) throw new TypeError(`no route ${u.pathname}`);
    const reply = queue.length > 1 ? queue.shift()! : queue[0];
    if (reply === 'network') throw new TypeError('fetch failed');
    const text = typeof reply.body === 'string' ? reply.body : JSON.stringify(reply.body);
    return new Response(text, { status: reply.status });
  }) as typeof fetch;
  return { fetchFn, calls };
}

const DEVICE = {
  device_code: 'dev-code',
  user_code: 'BCDF-GHJK',
  verification_uri: 'https://tryaii.com/device',
  verification_uri_complete: 'https://tryaii.com/device?user_code=BCDF-GHJK',
  expires_in: 600,
  interval: 5,
};

function tokenBody(n: number, email = 'a@b.com') {
  return {
    access_token: `access-${n}`,
    token_type: 'Bearer',
    expires_in: 3600,
    refresh_token: `tair_refresh-${n}`,
    refresh_expires_in: 7776000,
    user: { id: 'u1', email, name: 'A B' },
    entitlements: ['catalog:full'],
  };
}

const ME = {
  user: { id: 'u1', email: 'a@b.com', name: 'A B' },
  entitlements: ['catalog:full'],
  session: { id: 'fam1', created_at: '2026-10-03T12:00:00Z' },
};

const pending = { status: 400, body: { error: 'authorization_pending' } };

// Catalog contract (docs/catalog/CONTRACT-catalog-v1.md section 5): login
// downloads the full catalog right after "Logged in as" (the scripted fake has
// no /v1/catalog/live route -> the failure line), whoami adds a third line.
const LOGIN_DL_FAILED = 'Could not download the full catalog now; it will be fetched on next use.\n';
const NO_CATALOG = 'Catalog: not downloaded yet\n';

let dir: string;
let env: NodeJS.ProcessEnv;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'tryaii-auth-'));
  env = { TRYAII_DRE_DATA_DIR: dir, TRYAII_API_URL: API };
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function capture() {
  const io = { out: '', err: '' };
  return {
    io,
    writers: {
      stdout: (t: string) => {
        io.out += t;
      },
      stderr: (t: string) => {
        io.err += t;
      },
    },
  };
}

/** Deps with a virtual clock that sleep() advances. */
function makeDeps(fetchFn: typeof fetch, extra: Partial<AuthDeps> = {}) {
  let clock = T0;
  const sleeps: number[] = [];
  const deps: AuthDeps = {
    version: '9.9.9',
    fetchFn,
    env,
    platform: 'testos',
    now: () => clock,
    sleep: async (ms: number) => {
      sleeps.push(ms);
      clock += ms;
    },
    ...extra,
  };
  return { deps, sleeps, advance: (ms: number) => (clock += ms) };
}

function readCreds(): Credentials {
  return JSON.parse(readFileSync(credentialsPath(env), 'utf-8')) as Credentials;
}

function writeCreds(overrides: Partial<Credentials> = {}): Credentials {
  const creds: Credentials = {
    version: 1,
    api_url: API,
    user: { id: 'u0', email: 'old@b.com', name: 'Old' },
    entitlements: ['catalog:full'],
    refresh_token: 'tair_old',
    refresh_expires_at: '2027-01-01T00:00:00Z',
    access_token: 'access-old',
    access_expires_at: '2026-10-03T13:00:00Z',
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  };
  writeFileSync(credentialsPath(env), JSON.stringify(creds, null, 2));
  return creds;
}

const LOGIN_BLOCK =
  'To sign in, open this URL in a browser:\n' +
  '  https://tryaii.com/device\n' +
  'and enter the code: BCDF-GHJK\n' +
  '\n' +
  'Or open directly: https://tryaii.com/device?user_code=BCDF-GHJK\n' +
  '\n' +
  'Waiting for approval (expires in 10 minutes, Ctrl+C to cancel)...\n';

describe('login', () => {
  it('succeeds after pending polls and writes the contract credentials file', async () => {
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [pending, pending, { status: 200, body: tokenBody(1) }],
    });
    const { deps, sleeps } = makeDeps(fetchFn);
    const { io, writers } = capture();

    expect(await runLogin(writers, deps)).toBe(0);
    expect(io.out).toBe(LOGIN_BLOCK + 'Logged in as a@b.com.\n' + LOGIN_DL_FAILED);
    expect(io.err).toBe('');
    expect(sleeps).toEqual([5000, 5000, 5000]);
    expect(calls.map((c) => new URL(c.url).pathname).at(-1)).toBe('/v1/catalog/live');

    expect(calls[0].url).toBe(`${API}/v1/auth/device/code`);
    expect(calls[0].body).toEqual({
      client_id: 'tryaii-cli',
      client: { sdk: 'node', version: '9.9.9', os: 'testos' },
    });
    expect(calls[0].headers['User-Agent']).toBe('tryaii/9.9.9');
    expect(calls[0].headers['Content-Type']).toBe('application/json');
    expect(calls[1].body).toEqual({
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      device_code: 'dev-code',
      client_id: 'tryaii-cli',
    });

    const creds = readCreds();
    expect(Object.keys(creds)).toEqual([
      'version', 'api_url', 'user', 'entitlements', 'refresh_token',
      'refresh_expires_at', 'access_token', 'access_expires_at', 'created_at',
    ]);
    // Clock at success = T0 + 15 s of polling.
    expect(creds).toEqual({
      version: 1,
      api_url: API,
      user: { id: 'u1', email: 'a@b.com', name: 'A B' },
      entitlements: ['catalog:full'],
      refresh_token: 'tair_refresh-1',
      refresh_expires_at: '2027-01-01T12:00:15Z',
      access_token: 'access-1',
      access_expires_at: '2026-10-03T13:00:15Z',
      created_at: '2026-10-03T12:00:15Z',
    });
  });

  it('adds 5 s to the interval on each slow_down', async () => {
    const slow = { status: 400, body: { error: 'slow_down' } };
    const { fetchFn } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [slow, pending, slow, { status: 200, body: tokenBody(1) }],
    });
    const { deps, sleeps } = makeDeps(fetchFn);
    const { writers } = capture();
    expect(await runLogin(writers, deps)).toBe(0);
    expect(sleeps).toEqual([5000, 10000, 10000, 15000]);
  });

  it.each([
    ['access_denied', 'Login denied in the browser.\n'],
    ['expired_token', 'The code expired. Run tryaii login again.\n'],
    ['invalid_grant', 'Login failed: invalid_grant.\n'],
  ])('maps %s to its stderr line and exit 1', async (code, line) => {
    const { fetchFn } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [pending, { status: 400, body: { error: code } }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(1);
    expect(io.out).toBe(LOGIN_BLOCK);
    expect(io.err).toBe(line);
    expect(existsSync(credentialsPath(env))).toBe(false);
  });

  it('expires locally once expires_in has elapsed while still pending', async () => {
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: { ...DEVICE, expires_in: 12 } }],
      '/v1/auth/token': [pending],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(1);
    expect(io.err).toBe('The code expired. Run tryaii login again.\n');
    // v1.1 item 17: polls at t+5, t+10 and ONE final poll after the deadline (t+15).
    expect(calls.filter((c) => c.url.endsWith('/v1/auth/token'))).toHaveLength(3);
  });

  it.each([
    ['network error', ['network'] as Reply[]],
    ['non-JSON body', [{ status: 200, body: '<html>' }] as Reply[]],
    ['unexpected status', [{ status: 502, body: { error: 'bad_gateway' } }] as Reply[]],
    // v1.1 item 15: 503 stays network (429 is rate_limited, tested below).
    ['503', [{ status: 503, body: { error: 'temporarily_unavailable' } }] as Reply[]],
    // v1.1 item 16: a 3xx is never followed.
    ['302', [{ status: 302, body: '' }] as Reply[]],
    ['400 without an error code', [{ status: 400, body: { detail: 'x' } }] as Reply[]],
  ])('maps a device-code %s to the network line', async (_label, replies) => {
    const { fetchFn } = fakeServer({ '/v1/auth/device/code': replies });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(1);
    expect(io.out).toBe('');
    expect(io.err).toBe(`Could not reach ${API}. Check your connection and try again.\n`);
  });

  it('maps a network failure while polling to the network line', async () => {
    const { fetchFn } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [pending, 'network'],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(1);
    expect(io.err).toBe(`Could not reach ${API}. Check your connection and try again.\n`);
  });

  it('uses https://api.tryaii.com when TRYAII_API_URL is empty', async () => {
    env.TRYAII_API_URL = '';
    const { fetchFn, calls } = fakeServer({ '/v1/auth/device/code': ['network'] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(1);
    expect(calls[0].url).toBe('https://api.tryaii.com/v1/auth/device/code');
    expect(io.err).toBe(
      'Could not reach https://api.tryaii.com. Check your connection and try again.\n',
    );
  });

  it('drops trailing slashes from TRYAII_API_URL', async () => {
    env.TRYAII_API_URL = `${API}//`;
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [{ status: 200, body: tokenBody(1) }],
    });
    const { deps } = makeDeps(fetchFn);
    const { writers } = capture();
    expect(await runLogin(writers, deps)).toBe(0);
    expect(calls[0].url).toBe(`${API}/v1/auth/device/code`);
    expect(readCreds().api_url).toBe(API);
  });

  it('replaces an existing session and revokes the old one after success', async () => {
    writeCreds();
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [{ status: 200, body: tokenBody(2, 'new@b.com') }],
      '/v1/auth/revoke': [{ status: 200, body: {} }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(0);
    expect(io.out).toBe(
      'Already logged in as old@b.com. Signing in again replaces that session.\n' +
        LOGIN_BLOCK +
        'Logged in as new@b.com.\n' +
        LOGIN_DL_FAILED,
    );
    expect(readCreds().refresh_token).toBe('tair_refresh-2');
    const revokeCall = calls.find((c) => c.url.endsWith('/v1/auth/revoke'))!;
    expect(revokeCall.body).toEqual({ refresh_token: 'tair_old', client_id: 'tryaii-cli' });
  });

  it('keeps the new session when revoking the old one fails', async () => {
    writeCreds();
    const { fetchFn } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [{ status: 200, body: tokenBody(2) }],
      '/v1/auth/revoke': ['network'],
    });
    const { deps } = makeDeps(fetchFn);
    const { writers } = capture();
    expect(await runLogin(writers, deps)).toBe(0);
    expect(readCreds().refresh_token).toBe('tair_refresh-2');
  });

  it('prints Login cancelled. and returns 130 when aborted while polling', async () => {
    const controller = new AbortController();
    const { fetchFn } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [pending],
    });
    let polls = 0;
    const { deps } = makeDeps(fetchFn, {
      signal: controller.signal,
      sleep: async () => {
        if (++polls === 2) controller.abort();
      },
    });
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(130);
    expect(io.out).toBe(LOGIN_BLOCK);
    expect(io.err).toBe('Login cancelled.\n');
    expect(existsSync(credentialsPath(env))).toBe(false);
  });

  it.skipIf(process.platform === 'win32')('writes the credentials file with mode 0600', async () => {
    const { fetchFn } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [{ status: 200, body: tokenBody(1) }],
    });
    const { deps } = makeDeps(fetchFn);
    const { writers } = capture();
    expect(await runLogin(writers, deps)).toBe(0);
    expect(statSync(credentialsPath(env)).mode & 0o777).toBe(0o600);
  });
});

describe('logout', () => {
  it('revokes, deletes the file and prints Logged out.', async () => {
    writeCreds();
    const { fetchFn, calls } = fakeServer({ '/v1/auth/revoke': [{ status: 200, body: {} }] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogout(writers, deps)).toBe(0);
    expect(io.out).toBe('Logged out.\n');
    expect(existsSync(credentialsPath(env))).toBe(false);
    expect(calls[0].body).toEqual({ refresh_token: 'tair_old', client_id: 'tryaii-cli' });
  });

  it('still logs out when revoke fails', async () => {
    writeCreds();
    const { fetchFn } = fakeServer({ '/v1/auth/revoke': ['network'] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogout(writers, deps)).toBe(0);
    expect(io.out).toBe('Logged out.\n');
    expect(existsSync(credentialsPath(env))).toBe(false);
  });

  it('removes an unreadable credentials file without revoking', async () => {
    writeFileSync(credentialsPath(env), '{not json');
    const { fetchFn, calls } = fakeServer({});
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogout(writers, deps)).toBe(0);
    expect(io.out).toBe('Logged out.\n');
    expect(existsSync(credentialsPath(env))).toBe(false);
    expect(calls).toEqual([]);
  });

  it.each([['unset', undefined], ['set elsewhere', API]])(
    'revokes at the stored api_url when TRYAII_API_URL is %s (v1.1 item 13)',
    async (_label, envUrl) => {
      if (envUrl === undefined) delete env.TRYAII_API_URL;
      else env.TRYAII_API_URL = envUrl;
      writeCreds({ api_url: 'http://issuer.test/' });
      const { fetchFn, calls } = fakeServer({ '/v1/auth/revoke': [{ status: 200, body: {} }] });
      const { deps } = makeDeps(fetchFn);
      const { writers } = capture();
      expect(await runLogout(writers, deps)).toBe(0);
      expect(calls.map((c) => c.url)).toEqual(['http://issuer.test/v1/auth/revoke']);
    },
  );

  it('prints Not logged in. with no file', async () => {
    const { fetchFn, calls } = fakeServer({});
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogout(writers, deps)).toBe(0);
    expect(io.out).toBe('Not logged in.\n');
    expect(calls).toEqual([]);
  });

  it('ignores TRYAII_TOKEN and logs the file session out (v1.1 item 13)', async () => {
    writeCreds();
    env.TRYAII_TOKEN = 'tair_env';
    const { fetchFn, calls } = fakeServer({ '/v1/auth/revoke': [{ status: 200, body: {} }] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogout(writers, deps)).toBe(0);
    expect(io.out).toBe('Logged out.\n');
    expect(io.err).toBe('');
    expect(calls.map((c) => c.body.refresh_token)).toEqual(['tair_old']);
    expect(existsSync(credentialsPath(env))).toBe(false);
  });

  it('reports a credentials file it cannot delete and exits 1 (v1.1 item 19)', async () => {
    const path = credentialsPath(env);
    mkdirSync(path);
    writeFileSync(join(path, 'keep'), 'x');
    const { fetchFn, calls } = fakeServer({});
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogout(writers, deps)).toBe(1);
    expect(io.out).toBe('');
    expect(io.err).toBe('Could not remove the credentials file.\n');
    expect(calls).toEqual([]);
    expect(statSync(path).isDirectory()).toBe(true);
  });

  it('a file vanishing mid-logout (ENOENT) is not an error (v1.1 item 19)', async () => {
    writeCreds();
    const { fetchFn: inner } = fakeServer({ '/v1/auth/revoke': [{ status: 200, body: {} }] });
    const fetchFn = (async (url: any, init: any) => {
      rmSync(credentialsPath(env));
      return inner(url, init);
    }) as typeof fetch;
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogout(writers, deps)).toBe(0);
    expect(io.out).toBe('Logged out.\n');
  });
});

describe('whoami', () => {
  const ok = 'Logged in as a@b.com (A B)\nEntitlements: catalog:full\n' + NO_CATALOG;

  it('prints the account with a fresh access token (no refresh)', async () => {
    writeCreds();
    const { fetchFn, calls } = fakeServer({ '/v1/auth/me': [{ status: 200, body: ME }] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(0);
    expect(io.out).toBe(ok);
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('GET');
    expect(calls[0].headers.Authorization).toBe('Bearer access-old');
  });

  it('prints none for empty entitlements', async () => {
    writeCreds();
    const { fetchFn } = fakeServer({
      '/v1/auth/me': [{ status: 200, body: { ...ME, entitlements: [] } }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(0);
    expect(io.out).toBe('Logged in as a@b.com (A B)\nEntitlements: none\n' + NO_CATALOG);
  });

  it('--json prints the /me body with 2-space indent in server key order', async () => {
    writeCreds();
    const raw =
      '{"session": {"created_at": "2026-10-03T12:00:00Z", "id": "fam1"}, ' +
      '"user": {"name": "A B", "id": "u1", "email": "a@b.com"}, "entitlements": []}';
    const { fetchFn } = fakeServer({ '/v1/auth/me': [{ status: 200, body: raw }] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: true })).toBe(0);
    expect(io.out).toBe(
      '{\n' +
        '  "session": {\n' +
        '    "created_at": "2026-10-03T12:00:00Z",\n' +
        '    "id": "fam1"\n' +
        '  },\n' +
        '  "user": {\n' +
        '    "name": "A B",\n' +
        '    "id": "u1",\n' +
        '    "email": "a@b.com"\n' +
        '  },\n' +
        '  "entitlements": []\n' +
        '}\n',
    );
  });

  it('reports Not logged in. Run: tryaii login with no credentials', async () => {
    const { fetchFn } = fakeServer({});
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(1);
    expect(io.err).toBe('Not logged in. Run: tryaii login\n');
  });

  it('refreshes within 60 s of expiry and persists the rotated tokens', async () => {
    // Access token expires 30 s after T0 -> inside the 60 s window.
    writeCreds({ access_expires_at: '2026-10-03T12:00:30Z' });
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/token': [{ status: 200, body: tokenBody(7) }],
      '/v1/auth/me': [{ status: 200, body: ME }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(0);
    expect(io.out).toBe(ok);
    expect(calls[0].body).toEqual({
      grant_type: 'refresh_token',
      refresh_token: 'tair_old',
      client_id: 'tryaii-cli',
    });
    expect(calls[1].headers.Authorization).toBe('Bearer access-7');
    const creds = readCreds();
    expect(creds.refresh_token).toBe('tair_refresh-7');
    expect(creds.access_token).toBe('access-7');
    expect(creds.access_expires_at).toBe('2026-10-03T13:00:00Z');
    expect(creds.created_at).toBe('2026-10-01T00:00:00Z');
  });

  it('does not refresh when the access token is valid for more than 60 s', async () => {
    writeCreds({ access_expires_at: '2026-10-03T12:01:01Z' });
    const { fetchFn, calls } = fakeServer({ '/v1/auth/me': [{ status: 200, body: ME }] });
    const { deps } = makeDeps(fetchFn);
    const { writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(0);
    expect(calls.map((c) => new URL(c.url).pathname)).toEqual(['/v1/auth/me']);
  });

  it('deletes the file and reports the session ended on invalid_grant', async () => {
    writeCreds({ access_expires_at: '2026-10-03T11:00:00Z' });
    const { fetchFn } = fakeServer({
      '/v1/auth/token': [{ status: 400, body: { error: 'invalid_grant' } }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(1);
    expect(io.err).toBe('Your session has ended. Run: tryaii login\n');
    expect(existsSync(credentialsPath(env))).toBe(false);
  });

  it('deletes the file and reports the session ended on /me 401', async () => {
    writeCreds();
    const { fetchFn } = fakeServer({
      '/v1/auth/me': [{ status: 401, body: { error: 'invalid_token' } }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(1);
    expect(io.err).toBe('Your session has ended. Run: tryaii login\n');
    expect(existsSync(credentialsPath(env))).toBe(false);
  });

  it.each([['fresh', '2026-10-03T13:00:00Z'], ['expired', '2026-10-03T11:00:00Z']])(
    'talks to the stored api_url, never TRYAII_API_URL (%s token, v1.1 item 13)',
    async (_label, accessExpiresAt) => {
      writeCreds({ api_url: 'http://issuer.test', access_expires_at: accessExpiresAt });
      const { fetchFn, calls } = fakeServer({
        '/v1/auth/token': [{ status: 200, body: tokenBody(4) }],
        '/v1/auth/me': [{ status: 200, body: ME }],
      });
      const { deps } = makeDeps(fetchFn);
      const { writers } = capture();
      expect(await runWhoami(writers, deps, { json: false })).toBe(0);
      expect(calls.length).toBeGreaterThan(0);
      for (const c of calls) expect(c.url.startsWith('http://issuer.test/')).toBe(true);
      expect(readCreds().api_url).toBe('http://issuer.test');
    },
  );

  it('names the stored api_url in the network line (v1.1 item 13)', async () => {
    writeCreds({ api_url: 'http://issuer.test' });
    const { fetchFn } = fakeServer({ '/v1/auth/me': ['network'] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(1);
    expect(io.err).toBe('Could not reach http://issuer.test. Check your connection and try again.\n');
    expect(existsSync(credentialsPath(env))).toBe(true);
  });

  it('ignores a credentials file of another schema version', async () => {
    writeCreds({ version: 2 });
    const { fetchFn } = fakeServer({});
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(1);
    expect(io.err).toBe('Not logged in. Run: tryaii login\n');
  });

  it('keeps the file and reports the network line on a network failure', async () => {
    writeCreds();
    const { fetchFn } = fakeServer({ '/v1/auth/me': ['network'] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(1);
    expect(io.err).toBe(`Could not reach ${API}. Check your connection and try again.\n`);
    expect(existsSync(credentialsPath(env))).toBe(true);
  });

  it('ignores TRYAII_TOKEN without a file: not logged in (v1.1 item 13)', async () => {
    env.TRYAII_TOKEN = 'tair_env';
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/token': [{ status: 200, body: tokenBody(3) }],
      '/v1/auth/me': [{ status: 200, body: ME }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(1);
    expect(io.err).toBe('Not logged in. Run: tryaii login\n');
    expect(calls).toEqual([]);
    expect(existsSync(credentialsPath(env))).toBe(false);
  });

  it('ignores TRYAII_TOKEN with a file: the file token is used (v1.1 item 13)', async () => {
    writeCreds({ access_expires_at: '2026-10-03T11:00:00Z' });
    env.TRYAII_TOKEN = 'tair_env';
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/token': [{ status: 200, body: tokenBody(3) }],
      '/v1/auth/me': [{ status: 200, body: ME }],
    });
    const { deps } = makeDeps(fetchFn);
    const { writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(0);
    expect(calls[0].body.refresh_token).toBe('tair_old');
    expect(JSON.stringify(calls.map((c) => c.body))).not.toContain('tair_env');
    expect(readCreds().refresh_token).toBe('tair_refresh-3');
  });

  it.each([
    ['refresh invalid_request', '/v1/auth/token', { status: 400, body: { error: 'invalid_request' } }, true, 'invalid_request'],
    ['refresh 401 invalid_client', '/v1/auth/token', { status: 401, body: { error: 'invalid_client' } }, true, 'invalid_client'],
    ['refresh unsupported_grant_type', '/v1/auth/token', { status: 400, body: { error: 'unsupported_grant_type' } }, true, 'unsupported_grant_type'],
    ['/me 400 invalid_request', '/v1/auth/me', { status: 400, body: { error: 'invalid_request' } }, false, 'invalid_request'],
    ['refresh 429', '/v1/auth/token', { status: 429, body: { error: 'slow_down' } }, true, 'rate_limited'],
    ['/me 429', '/v1/auth/me', { status: 429, body: 'Too Many Requests' }, false, 'rate_limited'],
  ] as Array<[string, string, Reply, boolean, string]>)(
    '%s keeps the file: Unexpected response (v1.1 items 14-15)',
    async (_label, path, reply, expired, code) => {
      const creds = writeCreds(expired ? { access_expires_at: '2026-10-03T11:00:00Z' } : {});
      const { fetchFn } = fakeServer({ [path]: [reply] });
      const { deps } = makeDeps(fetchFn);
      const { io, writers } = capture();
      expect(await runWhoami(writers, deps, { json: false })).toBe(1);
      expect(io.out).toBe('');
      expect(io.err).toBe(`Unexpected response from ${API}: ${code}.\n`);
      expect(readCreds()).toEqual(creds);
    },
  );

  it('/me 401 with any error code ends the session (v1.1 item 14)', async () => {
    writeCreds();
    const { fetchFn } = fakeServer({
      '/v1/auth/me': [{ status: 401, body: { error: 'invalid_client' } }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runWhoami(writers, deps, { json: false })).toBe(1);
    expect(io.err).toBe('Your session has ended. Run: tryaii login\n');
    expect(existsSync(credentialsPath(env))).toBe(false);
  });
});

const RATE_LIMITED = 'Too many sign-in attempts. Wait a minute and try again.\n';

describe('v1.1 login: 429 and the replaced session (items 13, 15)', () => {
  it.each([
    ['JSON body', { status: 429, body: { error: 'slow_down' } }],
    ['text body', { status: 429, body: 'Too Many Requests' }],
  ] as Array<[string, Reply]>)('429 on device/code (%s) -> rate-limited line', async (_l, reply) => {
    const { fetchFn } = fakeServer({ '/v1/auth/device/code': [reply] });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(1);
    expect(io.out).toBe('');
    expect(io.err).toBe(RATE_LIMITED);
    expect(existsSync(credentialsPath(env))).toBe(false);
  });

  it('429 while polling stops polling -> rate-limited line', async () => {
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [{ status: 429, body: { error: 'slow_down' } }],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(1);
    expect(io.out).toBe(LOGIN_BLOCK);
    expect(io.err).toBe(RATE_LIMITED);
    expect(calls.filter((c) => c.url.endsWith('/v1/auth/token'))).toHaveLength(1);
  });

  it('revokes the replaced session at its own stored api_url', async () => {
    writeCreds({ api_url: 'http://old-issuer.test' });
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: DEVICE }],
      '/v1/auth/token': [{ status: 200, body: tokenBody(2) }],
      '/v1/auth/revoke': [{ status: 200, body: {} }],
    });
    const { deps } = makeDeps(fetchFn);
    const { writers } = capture();
    expect(await runLogin(writers, deps)).toBe(0);
    expect(calls.filter((c) => c.url.endsWith('/v1/auth/revoke')).map((c) => c.url)).toEqual([
      'http://old-issuer.test/v1/auth/revoke',
    ]);
    expect(readCreds().api_url).toBe(API);
  });
});

describe('v1.1 polling (item 17)', () => {
  it.each([[0], [-3], [null], ['5'], [true], [Number.NaN]])(
    'interval %p defaults to 5 s',
    async (bad) => {
      const { fetchFn } = fakeServer({
        '/v1/auth/device/code': [{ status: 200, body: { ...DEVICE, interval: bad } }],
        '/v1/auth/token': [pending, { status: 200, body: tokenBody(1) }],
      });
      const { deps, sleeps } = makeDeps(fetchFn);
      const { writers } = capture();
      expect(await runLogin(writers, deps)).toBe(0);
      expect(sleeps).toEqual([5000, 5000]);
    },
  );

  it.each([[0], [-1], [null], ['600']])('expires_in %p defaults to 600 s', async (bad) => {
    const { fetchFn, calls } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: { ...DEVICE, interval: 100, expires_in: bad } }],
      '/v1/auth/token': [pending],
    });
    const { deps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(1);
    expect(io.err).toBe('The code expired. Run tryaii login again.\n');
    // polls at t+100 ... t+600; the one at the deadline is the final one
    expect(calls.filter((c) => c.url.endsWith('/v1/auth/token'))).toHaveLength(6);
  });

  it('an approval in the final interval still wins', async () => {
    const { fetchFn } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: { ...DEVICE, expires_in: 12 } }],
      '/v1/auth/token': [pending, pending, { status: 200, body: tokenBody(1) }],
    });
    const { deps, sleeps } = makeDeps(fetchFn);
    const { io, writers } = capture();
    expect(await runLogin(writers, deps)).toBe(0);
    expect(io.out).toBe(LOGIN_BLOCK + 'Logged in as a@b.com.\n' + LOGIN_DL_FAILED);
    expect(sleeps).toEqual([5000, 5000, 5000]);
  });

  it.each([['invalid_grant'], ['slow_down'], ['authorization_pending']])(
    '%s received after the deadline is reported as expired',
    async (late) => {
      const { fetchFn, calls } = fakeServer({
        '/v1/auth/device/code': [{ status: 200, body: { ...DEVICE, expires_in: 12 } }],
        '/v1/auth/token': [pending, pending, { status: 400, body: { error: late } }],
      });
      const { deps } = makeDeps(fetchFn);
      const { io, writers } = capture();
      expect(await runLogin(writers, deps)).toBe(1);
      expect(io.err).toBe('The code expired. Run tryaii login again.\n');
      expect(calls.filter((c) => c.url.endsWith('/v1/auth/token'))).toHaveLength(3);
    },
  );

  it('the deadline is judged when the answer arrives', async () => {
    const { fetchFn: inner } = fakeServer({
      '/v1/auth/device/code': [{ status: 200, body: { ...DEVICE, expires_in: 12 } }],
      '/v1/auth/token': [{ status: 400, body: { error: 'invalid_grant' } }],
    });
    let advance: (ms: number) => number = () => 0;
    const slow = (async (url: any, init: any) => {
      if (String(url).endsWith('/v1/auth/token')) advance(20_000); // a 20 s request
      return inner(url, init);
    }) as typeof fetch;
    const made = makeDeps(slow);
    advance = made.advance;
    const { io, writers } = capture();
    expect(await runLogin(writers, made.deps)).toBe(1);
    expect(io.err).toBe('The code expired. Run tryaii login again.\n');
  });
});

describe('v1.1 transport (items 15, 16)', () => {
  it.each([[301], [302], [303], [307], [308]])(
    'a %s is network and the redirect target is never hit',
    async (status) => {
      const hits: string[] = [];
      const server = createServer((req, res) => {
        hits.push(req.url ?? '');
        if (req.url === '/v1/auth/me') {
          res.writeHead(status, { Location: '/target', 'Content-Length': '0' });
          res.end();
          return;
        }
        const body = JSON.stringify(ME);
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
        });
        res.end(body);
      });
      await new Promise<void>((ok) => server.listen(0, '127.0.0.1', () => ok()));
      const { port } = server.address() as AddressInfo;
      try {
        await expect(
          requestJson(
            `http://127.0.0.1:${port}`,
            { method: 'GET', path: '/v1/auth/me', bearer: 'secret' },
            { version: '9.9.9' },
          ),
        ).rejects.toBeInstanceOf(AuthNetworkError);
        expect(hits).toEqual(['/v1/auth/me']);
      } finally {
        server.closeAllConnections?.();
        await new Promise<void>((ok) => server.close(() => ok()));
      }
    },
  );

  it('asks for one connection per request (Connection: close, like urllib)', async () => {
    // A pooled socket reused after the 5 s poll interval races the server's
    // keep-alive timeout (ECONNRESET -> "Could not reach").
    const { fetchFn, calls } = fakeServer({ '/v1/auth/me': [{ status: 200, body: ME }] });
    await requestJson(API, { method: 'GET', path: '/v1/auth/me', bearer: 't' }, { version: '9.9.9', fetchFn });
    expect(calls[0].headers.Connection).toBe('close');
  });

  it('429 from any endpoint is AuthRateLimitedError', async () => {
    for (const path of ['/v1/auth/device/code', '/v1/auth/token', '/v1/auth/revoke', '/v1/auth/me']) {
      const { fetchFn } = fakeServer({ [path]: [{ status: 429, body: { error: 'slow_down' } }] });
      const p = requestJson(API, { method: 'POST', path, body: {} }, { version: '9.9.9', fetchFn });
      await expect(p).rejects.toBeInstanceOf(AuthRateLimitedError);
    }
  });
});

describe('v1.1 credentials write (items 18, 19)', () => {
  const errnoError = (code: string) => Object.assign(new Error(code), { code });

  function flaky(fails: number, code: string) {
    const state = { calls: 0 };
    const rename = (src: string, dst: string) => {
      state.calls += 1;
      if (state.calls <= fails) throw errnoError(code);
      renameSync(src, dst);
    };
    return { state, rename };
  }

  it.each([['EPERM'], ['EACCES'], ['EBUSY']])('retries a Windows rename on %s', (code) => {
    const src = join(dir, 'a.tmp');
    writeFileSync(src, 'x');
    const { state, rename } = flaky(2, code);
    const sleeps: number[] = [];
    renameWithRetry(src, join(dir, 'b'), {
      platform: 'win32',
      rename,
      sleep: (ms) => sleeps.push(ms),
    });
    expect(state.calls).toBe(3);
    expect(sleeps).toEqual([50, 50]);
    expect(readFileSync(join(dir, 'b'), 'utf-8')).toBe('x');
  });

  it('gives up after 5 retries', () => {
    const { state, rename } = flaky(99, 'EPERM');
    const sleeps: number[] = [];
    expect(() =>
      renameWithRetry('a', 'b', { platform: 'win32', rename, sleep: (ms) => sleeps.push(ms) }),
    ).toThrow('EPERM');
    expect(state.calls).toBe(6);
    expect(sleeps).toEqual([50, 50, 50, 50, 50]);
  });

  it('does not retry other errors or other platforms', () => {
    const sleeps: number[] = [];
    const sleep = (ms: number) => sleeps.push(ms);
    const a = flaky(1, 'ENOSPC');
    expect(() => renameWithRetry('a', 'b', { platform: 'win32', rename: a.rename, sleep })).toThrow();
    expect(a.state.calls).toBe(1);
    const b = flaky(1, 'EACCES');
    expect(() => renameWithRetry('a', 'b', { platform: 'linux', rename: b.rename, sleep })).toThrow();
    expect(b.state.calls).toBe(1);
    expect(sleeps).toEqual([]);
  });

  it('renames a complete, closed, unpredictably named temp file', () => {
    const seen: Array<{ name: string; content: string; mode: number }> = [];
    const creds = writeCreds();
    rmSync(credentialsPath(env));
    const rename = (src: string, dst: string) => {
      seen.push({
        name: src.slice(dir.length + 1),
        content: readFileSync(src, 'utf-8'),
        mode: statSync(src).mode & 0o777,
      });
      renameSync(src, dst);
    };
    saveCredentials(creds, env, { rename });
    saveCredentials(creds, env, { rename });
    expect(seen).toHaveLength(2);
    for (const s of seen) {
      expect(s.name).toMatch(/^\.credentials-[0-9a-f]{16}\.tmp$/);
      expect(JSON.parse(s.content)).toEqual(creds);
      expect(s.content.endsWith('}\n')).toBe(true);
      if (process.platform !== 'win32') expect(s.mode).toBe(0o600);
    }
    expect(seen[0].name).not.toBe(seen[1].name);
    expect(readdirSync(dir)).toEqual(['credentials.json']);
  });

  it('a failed rename removes the temp file and keeps the old credentials', () => {
    const creds = writeCreds();
    const before = readFileSync(credentialsPath(env), 'utf-8');
    const rename = () => {
      throw errnoError('EIO');
    };
    expect(() => saveCredentials({ ...creds, refresh_token: 'tair_x' }, env, { rename })).toThrow(
      'EIO',
    );
    expect(readFileSync(credentialsPath(env), 'utf-8')).toBe(before);
    expect(readdirSync(dir)).toEqual(['credentials.json']);
  });

  it('deleteCredentials: ENOENT is false, other failures throw', () => {
    expect(deleteCredentials(env)).toBe(false);
    mkdirSync(credentialsPath(env));
    writeFileSync(join(credentialsPath(env), 'keep'), 'x');
    expect(() => deleteCredentials(env)).toThrow();
  });
});
