/**
 * `tryaii apps` (shared/monitor/SPEC.md §7, fixture suite `apps`) and the
 * HTTP helper behind it (SPEC.md §6).
 *
 * The conformance goldens spawn the built Node CLI (`node dist/cli.js`, so
 * run `npm run build` first) exactly as scripts/gen-monitor-fixtures.py's
 * run_apps_case runs the Python one: a fresh temp data dir and cwd, the
 * TRYAII_API_* / TRYAII_HTTP_FIXTURE* variables scrubbed, the canned routes
 * and the request log in a SEPARATE temp dir, `signed_in` writing the
 * fixture credentials. stdout, stderr, the exit code AND the request log
 * must match the frozen expectations byte for byte (no network). The unit
 * tests drive `monitor/api.ts` through an injected fetch, the fixture seam,
 * and one loopback server for the real node:http path (redirects).
 */

import { spawnSync } from 'node:child_process';
import { createServer, type Server } from 'node:http';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import * as api from '../src/monitor/api.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_DIR = resolve(HERE, '..');
const CLI = join(PKG_DIR, 'dist', 'cli.js');
const FIXTURES = resolve(PKG_DIR, '..', '..', 'shared', 'monitor', 'fixtures');
const CASES = join(FIXTURES, 'apps', 'cases.json');
const KEY = 'tryaii_k1a2b3c4d5e6_fixtureSecretfixtureSecretfixtureSecretfixt';

/** The API base every `apps` case talks to (never resolvable). */
const FIXTURE_API_URL = 'https://api.tryaii.test';
/** credentials.json for `"signed_in": true` (SPEC.md §7.4), valid until 2099. */
const FIXTURE_CREDENTIALS = {
  version: 1,
  api_url: FIXTURE_API_URL,
  user: { id: 'google-sub-1', email: 'dev@example.com', name: 'Dev Example' },
  entitlements: ['catalog:full'],
  refresh_token: 'tair_fixture_refresh',
  refresh_expires_at: '2099-01-01T00:00:00Z',
  access_token: 'eyJ.fixture.access',
  access_expires_at: '2099-01-01T00:00:00Z',
  created_at: '2026-10-12T00:00:00Z',
};
const SCRUBBED = ['TRYAII_API_KEY', 'TRYAII_API_URL', 'TRYAII_HTTP_FIXTURE', 'TRYAII_HTTP_FIXTURE_LOG'];

interface AppsInput {
  argv: string[];
  signed_in?: boolean;
  http?: Record<string, unknown>;
  env?: Record<string, string>;
}

interface AppsCase {
  name: string;
  input: AppsInput;
  expected: {
    stdout_golden: string;
    stderr: string | null;
    exit_code: number;
    requests: Record<string, unknown>[];
  } | null;
}

function loadCases(): AppsCase[] {
  if (!existsSync(CASES)) return [];
  return (JSON.parse(readFileSync(CASES, 'utf-8')) as { cases: AppsCase[] }).cases;
}

function normalize(buf: Buffer): string {
  return buf.toString('utf-8').replace(/\r\n/g, '\n');
}

/** Run one apps case; returns [exitCode, stdout, stderr, requests]. */
function runCase(input: AppsInput): [number, string, string, Record<string, unknown>[]] {
  const tmp = mkdtempSync(join(tmpdir(), 'tryaii-apps-cli-'));
  const side = mkdtempSync(join(tmpdir(), 'tryaii-apps-side-'));
  try {
    const httpPath = join(side, 'http.json');
    const logPath = join(side, 'requests.jsonl');
    writeFileSync(httpPath, JSON.stringify({ routes: input.http ?? {} }), 'utf-8');
    const env: NodeJS.ProcessEnv = { ...process.env };
    for (const key of SCRUBBED) delete env[key];
    Object.assign(env, {
      TRYAII_NO_BANNER: '1',
      TRYAII_DRE_DATA_DIR: tmp,
      TRYAII_API_URL: FIXTURE_API_URL,
      TRYAII_HTTP_FIXTURE: httpPath,
      TRYAII_HTTP_FIXTURE_LOG: logPath,
      ...(input.env ?? {}),
    });
    if (input.signed_in) {
      writeFileSync(join(tmp, 'credentials.json'), JSON.stringify(FIXTURE_CREDENTIALS, null, 2) + '\n', 'utf-8');
    }
    const proc = spawnSync(process.execPath, [CLI, ...input.argv], { cwd: tmp, env });
    const requests = existsSync(logPath)
      ? readFileSync(logPath, 'utf-8')
          .split('\n')
          .filter((line) => line.trim())
          .map((line) => JSON.parse(line) as Record<string, unknown>)
      : [];
    return [proc.status ?? -1, normalize(proc.stdout), normalize(proc.stderr), requests];
  } finally {
    rmSync(tmp, { recursive: true, force: true });
    rmSync(side, { recursive: true, force: true });
  }
}

describe.skipIf(!existsSync(CLI) || !existsSync(CASES))('tryaii apps CLI (Node) -- apps fixture suite', () => {
  const list = loadCases();
  it('has cases', () => {
    expect(list.length).toBeGreaterThan(0);
  });
  for (const c of list) {
    it(c.name, () => {
      if (c.expected === null) return;
      const [code, stdout, stderr, requests] = runCase(c.input);
      const golden = readFileSync(join(FIXTURES, 'apps', c.expected.stdout_golden), 'utf-8').replace(/\r\n/g, '\n');
      expect(stderr || null, 'stderr').toBe(c.expected.stderr);
      expect(code, 'exit code').toBe(c.expected.exit_code);
      expect(stdout).toBe(golden);
      expect(requests).toEqual(c.expected.requests);
      // Key order of every logged request (the log is byte-compared upstream).
      expect(JSON.stringify(requests)).toBe(JSON.stringify(c.expected.requests));
    });
  }

  it('help apps and apps <verb> --help print the frozen pages without a request', () => {
    const [code, stdout] = runCase({ argv: ['help', 'apps'] });
    expect(code).toBe(0);
    expect(stdout.startsWith('tryaii apps -- tryaii.com apps and app keys for monitor upload\n')).toBe(true);
    for (const verb of ['create', 'list', 'keys', 'check']) {
      const [vcode, vout, , requests] = runCase({ argv: ['apps', verb, '--help'] });
      expect(vcode, verb).toBe(0);
      expect(vout.startsWith(`tryaii apps ${verb} -- `), verb).toBe(true);
      expect(requests).toEqual([]);
    }
  });

  it('the request log never holds a credential', () => {
    for (const c of list) {
      for (const request of c.expected?.requests ?? []) {
        expect(Object.keys(request).sort()).toEqual(['auth', 'body', 'method', 'path', 'query']);
        expect([null, 'bearer', 'app_key']).toContain(request.auth);
        expect(JSON.stringify(request).includes('fixtureSecret')).toBe(false);
        expect(JSON.stringify(request).includes('eyJ.fixture')).toBe(false);
      }
    }
  });
});

// --- monitor/api.ts -------------------------------------------------------------

interface Seen {
  url: string;
  init: RequestInit;
}

function fakeFetch(answers: (Response | Error)[], seen: Seen[]): typeof fetch {
  return (async (url: string, init: RequestInit = {}) => {
    seen.push({ url, init });
    const answer = answers.shift() as Response | Error;
    if (answer instanceof Error) throw answer;
    return answer;
  }) as unknown as typeof fetch;
}

describe('monitor/api.ts', () => {
  const saved: Record<string, string | undefined> = {};
  beforeEach(() => {
    for (const key of SCRUBBED) {
      saved[key] = process.env[key];
      delete process.env[key];
    }
    api.resetFixtureState();
  });
  afterEach(() => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    api.resetFixtureState();
  });

  it('returns every status and decodes JSON', async () => {
    const seen: Seen[] = [];
    const resp = await api.request('POST', 'https://api.test/v1/apps', { name: 'n' }, {
      bearer: 'tok',
      fetchFn: fakeFetch([new Response('{"app_id": "app_x"}', { status: 201, headers: { 'X-A': '1' } })], seen),
    });
    expect(resp.status).toBe(201);
    expect(resp.body).toEqual({ app_id: 'app_x' });
    expect(resp.headers['x-a']).toBe('1');
    const headers = seen[0].init.headers as Record<string, string>;
    expect(seen[0].init.method).toBe('POST');
    expect(headers.Authorization).toBe('Bearer tok');
    expect(headers['Content-Type']).toBe('application/json');
    expect(headers['User-Agent']).toMatch(/^tryaii\/\d+\.\d+\.\d+/);
    expect(JSON.parse(String(seen[0].init.body))).toEqual({ name: 'n' });
    expect(seen[0].init.redirect).toBe('manual');
  });

  it('maps HTTP errors and network failures', async () => {
    const resp = await api.request('POST', 'https://api.test/x', {}, {
      fetchFn: fakeFetch([new Response('{"error":"key_limit"}', { status: 409 })], []),
    });
    expect(resp.status).toBe(409);
    expect(resp.error).toBe('key_limit');
    expect(resp.detail).toBeNull();
    await expect(
      api.request('GET', 'https://api.test/x', null, { fetchFn: fakeFetch([new TypeError('down')], []) }),
    ).rejects.toBeInstanceOf(api.ApiNetworkError);
    const empty = await api.request('DELETE', 'https://api.test/x', null, {
      fetchFn: fakeFetch([new Response(null, { status: 204 })], []),
    });
    expect(empty.status).toBe(204);
    expect(empty.body).toBeNull();
  });

  it('sends a raw body verbatim', async () => {
    const seen: Seen[] = [];
    await api.request('POST', 'https://api.test/x', null, {
      rawBody: Buffer.from('{"a":1}'),
      fetchFn: fakeFetch([new Response('{}', { status: 202 })], seen),
    });
    expect(Buffer.from(seen[0].init.body as Uint8Array).toString()).toBe('{"a":1}');
  });

  it('app key shape', () => {
    expect(api.isAppKey(KEY)).toBe(true);
    expect(api.isAppKey('tryaii_SHORT_x')).toBe(false);
    expect(api.isAppKey('tai_websitekey')).toBe(false);
    expect(api.isAppKey(null)).toBe(false);
  });

  it('api url and key from the environment', () => {
    expect(api.apiUrl({})).toBe('https://api.tryaii.com');
    expect(api.apiUrl({ TRYAII_API_URL: '' })).toBe('https://api.tryaii.com');
    expect(api.apiKey({})).toBeNull();
    expect(api.apiUrl({ TRYAII_API_URL: 'http://localhost:8080/' })).toBe('http://localhost:8080');
    expect(api.apiKey({ TRYAII_API_KEY: `  ${KEY}  ` })).toBe(KEY);
  });

  it('the fixture seam serves routes in order and logs every request', async () => {
    const side = mkdtempSync(join(tmpdir(), 'tryaii-api-seam-'));
    try {
      const fixture = join(side, 'http.json');
      const log = join(side, 'log.jsonl');
      writeFileSync(
        fixture,
        JSON.stringify({
          routes: {
            'GET /v1/monitor/config': [{ status: 200, body: { etag: '1' } }, { status: 304 }],
            'POST /v1/monitor/events': [{ network: true }],
          },
        }),
      );
      const env = { TRYAII_HTTP_FIXTURE: fixture, TRYAII_HTTP_FIXTURE_LOG: log };
      const url = 'https://api.test/v1/monitor/config?etag=&wait=25';
      expect((await api.request('GET', url, null, { bearer: KEY, env })).body).toEqual({ etag: '1' });
      expect((await api.request('GET', url, null, { bearer: KEY, env })).status).toBe(304);
      expect((await api.request('GET', url, null, { bearer: KEY, env })).status).toBe(304); // the last one repeats
      await expect(
        api.request('POST', 'https://api.test/v1/monitor/events', null, { rawBody: '{"a":1}', bearer: 'jwt', env }),
      ).rejects.toBeInstanceOf(api.ApiNetworkError);
      await expect(api.request('GET', 'https://api.test/unrouted', null, { env })).rejects.toBeInstanceOf(
        api.ApiNetworkError,
      );
      const lines = readFileSync(log, 'utf-8').trimEnd().split('\n').map((l) => JSON.parse(l));
      expect(lines[0]).toEqual({ method: 'GET', path: '/v1/monitor/config', query: { etag: '', wait: '25' }, auth: 'app_key', body: null });
      expect(lines[3]).toEqual({ method: 'POST', path: '/v1/monitor/events', query: null, auth: 'bearer', body: { a: 1 } });
      expect(lines[4]).toEqual({ method: 'GET', path: '/unrouted', query: null, auth: null, body: null });
      expect(lines).toHaveLength(5);
      // Minified, key order method, path, query, auth, body.
      expect(readFileSync(log, 'utf-8').split('\n')[0]).toBe(
        '{"method":"GET","path":"/v1/monitor/config","query":{"etag":"","wait":"25"},"auth":"app_key","body":null}',
      );
    } finally {
      rmSync(side, { recursive: true, force: true });
    }
  });

  describe('node:http path (loopback server)', () => {
    let server: Server;
    let base: string;
    const hits: { method?: string; url?: string; auth?: string; body: string }[] = [];
    beforeEach(async () => {
      hits.length = 0;
      server = createServer((req, res) => {
        let body = '';
        req.on('data', (c) => (body += c));
        req.on('end', () => {
          hits.push({ method: req.method, url: req.url, auth: req.headers.authorization, body });
          if (req.url === '/redirect') {
            res.writeHead(302, { Location: `${base}/target` });
            res.end();
            return;
          }
          res.writeHead(200, { 'Content-Type': 'application/json', 'X-Echo': 'yes' });
          res.end(JSON.stringify({ ok: true, len: body.length }));
        });
      });
      await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
      base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    });
    afterEach(async () => {
      await new Promise<void>((done) => server.close(() => done()));
    });

    it('never follows a redirect (the bearer stays on this host)', async () => {
      const resp = await api.request('GET', `${base}/redirect`, null, { bearer: 'secret' });
      expect(resp.status).toBe(302);
      expect(hits.map((h) => h.url)).toEqual(['/redirect']);
    });

    it('sends JSON with the headers and reads the answer (unref keeps working)', async () => {
      const resp = await api.request('POST', `${base}/v1/x`, { name: 'é' }, { bearer: 'tok', unref: true });
      expect(resp.status).toBe(200);
      expect(resp.body).toEqual({ ok: true, len: '{"name":"é"}'.length });
      expect(resp.headers['x-echo']).toBe('yes');
      expect(hits[0]).toMatchObject({ method: 'POST', url: '/v1/x', auth: 'Bearer tok', body: '{"name":"é"}' });
    });

    it('a refused connection is one network error', async () => {
      const port = (server.address() as AddressInfo).port;
      await new Promise<void>((done) => server.close(() => done()));
      await expect(api.request('GET', `http://127.0.0.1:${port}/x`)).rejects.toBeInstanceOf(api.ApiNetworkError);
      server = createServer();
      await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
    });
  });
});
