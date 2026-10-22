/**
 * Data save: model origin / provider policies (shared/datasave/SPEC.md).
 * Mirrors packages/python/tests/test_policy.py.
 *
 *  - fixtures -- the Node engine reproduces shared/datasave/fixtures/expected.json
 *    byte for byte (the file the Python engine generates);
 *  - ModelPolicy -- normalization, merge, summary, validation;
 *  - bundle -- origins.json is an optional, hash-checked data file (contract v1.2);
 *  - Router -- filters candidates, fails closed without origin data, pins hosts;
 *  - OpenRouter payload -- `provider.only` is sent exactly when hosts are constrained;
 *  - saved policy + CLI -- `tryaii policy`, flags overriding the saved policy;
 *  - daemon -- allowedModels only goes to a daemon that advertises support.
 */

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  BUNDLE_DATA_FILES,
  listedDataFiles,
  BundleIntegrityError,
  CatalogBundle,
  STARTER_BUNDLE_DIR,
  bundleFromTexts,
  loadBundle,
  starterBundle,
} from '../src/catalog/bundle.js';
import * as catalogClient from '../src/catalog/client.js';
import type { ClassificationResult } from '../src/classifiers/base.js';
import { createDefaultConfig } from '../src/config.js';
import * as daemon from '../src/daemon.js';
import { OpenRouterIntegration } from '../src/integrations/openrouter.js';
import {
  ModelPolicy as ExportedModelPolicy,
  PolicyCatalogError as ExportedPolicyCatalogError,
  PolicyError as ExportedPolicyError,
} from '../src/index.js';
import {
  MSG_NEEDS_FULL_CATALOG,
  ModelPolicy,
  PolicyCatalogError,
  PolicyError,
  baseModelId,
  clearSavedPolicy,
  evaluate,
  loadSavedPolicy,
  originLabel,
  policyPath,
  resolvePolicy,
  savePolicy,
} from '../src/policy.js';
import type { PolicyDict } from '../src/policy.js';
import { Router } from '../src/router.js';
import type { RouteOptions, RouteResult } from '../src/router.js';
import { Priorities } from '../src/scoring/priorities.js';
import { serve } from '../src/server.js';
import { REPO_ROOT } from './_catalog.js';
import { signManifest } from './_signing.js';

const FIXTURES = join(REPO_ROOT, 'shared', 'datasave', 'fixtures');

interface FixtureInput {
  origins: Record<string, unknown>;
  model_ids: string[];
  cases: Array<{ name: string; policy: Record<string, unknown> }>;
  labels: string[];
}

function fixtureInput(): FixtureInput {
  return JSON.parse(readFileSync(join(FIXTURES, 'input.json'), 'utf-8')) as FixtureInput;
}

// ---------------------------------------------------------------- fixtures
/** The same object scripts/gen-datasave-fixtures.py builds from the Python engine. */
function runCase(
  c: { name: string; policy: Record<string, unknown> },
  origins: unknown,
  ids: string[],
): Record<string, unknown> {
  let policy: ModelPolicy;
  let result;
  try {
    policy = ModelPolicy.fromDict(c.policy);
    result = evaluate(policy, origins, ids);
  } catch (error) {
    if (error instanceof PolicyError) return { name: c.name, error: error.message };
    throw error;
  }
  const provider: Record<string, unknown> = {};
  for (const mid of result.allowed) provider[mid] = result.providerPreferences(mid);
  return {
    name: c.name,
    policy: policy.toDict(),
    summary: policy.summary(),
    active: policy.active,
    allowed: result.allowed,
    excluded: result.excluded,
    provider,
  };
}

describe('datasave fixtures (shared/datasave/fixtures)', () => {
  it('the Node engine reproduces expected.json byte for byte', () => {
    const data = fixtureInput();
    const labels: Record<string, string> = {};
    for (const mid of data.labels) labels[mid] = originLabel(data.origins, mid);
    const built = {
      cases: data.cases.map((c) => runCase(c, data.origins, data.model_ids)),
      labels,
      no_origins: runCase(
        { name: 'active policy without origins.json', policy: { origin: 'us-eu' } },
        null,
        data.model_ids,
      ),
    };
    const expected = readFileSync(join(FIXTURES, 'expected.json'), 'utf-8').replace(/\r\n/g, '\n');
    expect(JSON.stringify(built, null, 2) + '\n').toBe(expected);
  });

  it('case by case (readable diffs)', () => {
    const data = fixtureInput();
    const expected = JSON.parse(readFileSync(join(FIXTURES, 'expected.json'), 'utf-8'));
    data.cases.forEach((c, i) => {
      expect(runCase(c, data.origins, data.model_ids), c.name).toEqual(expected.cases[i]);
    });
    for (const [mid, label] of Object.entries(expected.labels)) {
      expect(originLabel(data.origins, mid), mid).toBe(label);
    }
    expect(expected.no_origins.error).toBe(MSG_NEEDS_FULL_CATALOG);
  });
});

// ---------------------------------------------------------------- policy object
describe('ModelPolicy', () => {
  it('normalizes lists and case', () => {
    const p = new ModelPolicy({
      origin: ' US-EU ',
      providers: 'OpenAI, anthropic,openai',
      denyCountries: ['cn', 'CN'],
      platform: 'Bedrock',
    });
    expect(p.origin).toBe('us-eu');
    expect(p.platform).toBe('bedrock');
    expect(p.providers).toEqual(['anthropic', 'openai']);
    expect(p.denyCountries).toEqual(['CN']);
    expect(p.summary()).toBe(
      'origin us-eu; providers anthropic,openai; deny-countries CN; platform bedrock',
    );
  });

  it('an inactive policy allows everything without origins', () => {
    const result = evaluate(new ModelPolicy(), null, ['a/b', 'c/d']);
    expect(result.allowed).toEqual(['a/b', 'c/d']);
    expect(result.excluded).toEqual({});
    expect(result.providerPreferences('a/b')).toBeNull();
  });

  it('an active policy without origins fails closed', () => {
    expect(() => evaluate(new ModelPolicy({ origin: 'no-prc-models' }), null, ['a/b'])).toThrow(
      PolicyCatalogError,
    );
    expect(() => evaluate(new ModelPolicy({ origin: 'no-prc-models' }), null, ['a/b'])).toThrow(
      /need the full catalog/,
    );
  });

  it('merges field by field', () => {
    const saved = new ModelPolicy({ origin: 'no-prc-models', providers: ['openai'] });
    const merged = saved.merged({ providers: 'anthropic', platform: null, origin: '' });
    expect(merged.origin).toBe('no-prc-models');
    expect(merged.providers).toEqual(['anthropic']);
  });

  it('round-trips through toDict / fromDict (snake_case keys, Python order)', () => {
    const p = new ModelPolicy({ origin: 'no-prc-hosting', allowCountries: ['US', 'FR'] });
    expect(Object.keys(p.toDict())).toEqual([
      'origin', 'providers', 'exclude_providers', 'allow_countries', 'deny_countries', 'platform',
    ]);
    expect(ModelPolicy.fromDict(p.toDict()).equals(p)).toBe(true);
  });

  it('a host-only policy does not constrain models', () => {
    let p = new ModelPolicy({ origin: 'no-prc-hosting' });
    expect([p.constrainsHosts, p.constrainsModels]).toEqual([true, false]);
    p = new ModelPolicy({ origin: 'no-prc-models' });
    expect([p.constrainsHosts, p.constrainsModels]).toEqual([false, true]);
  });

  it('error messages quote values like Python repr', () => {
    expect(() => new ModelPolicy({ origin: "it's" })).toThrow(
      `unknown origin "it's"; use one of: no-prc-models, no-prc-hosting, us-eu`,
    );
    expect(() => ModelPolicy.fromDict({ platform: 5 })).toThrow(
      'unknown platform 5; use one of: bedrock, azure, vertex',
    );
    expect(() => ModelPolicy.fromDict({ providers: 5 })).toThrow(
      'providers must be a list or a comma-separated string',
    );
    expect(() => ModelPolicy.fromDict({ providers: [1] })).toThrow('providers must contain strings');
    expect(() => ModelPolicy.fromDict([])).toThrow('a policy must be a JSON object');
    expect(() => resolvePolicy('saved' as 'auto')).toThrow(
      "policy must be 'auto', None, a dict or a ModelPolicy; got 'saved'",
    );
  });

  it('is exported from the package root', () => {
    expect(ExportedModelPolicy).toBe(ModelPolicy);
    expect(ExportedPolicyError).toBe(PolicyError);
    expect(ExportedPolicyCatalogError).toBe(PolicyCatalogError);
    expect(new PolicyCatalogError()).toBeInstanceOf(PolicyError);
  });
});

// ---------------------------------------------------------------- bundle
function starterFiles(): { manifest: Record<string, any>; files: Record<string, string> } {
  const manifest = JSON.parse(readFileSync(join(STARTER_BUNDLE_DIR, 'manifest.json'), 'utf-8'));
  const files: Record<string, string> = {};
  for (const name of listedDataFiles(manifest)) {
    files[name] = readFileSync(join(STARTER_BUNDLE_DIR, name), 'utf-8');
  }
  return { manifest, files };
}

function sha256(text: string): string {
  return createHash('sha256').update(Buffer.from(text, 'utf-8')).digest('hex');
}

const CN_NAMESPACES = ['qwen', 'deepseek', 'z-ai', 'moonshotai', 'minimax'];

/** Synthetic origins over real starter ids: every model is US except the CN_NAMESPACES ones. */
function originsFor(modelIds: string[]): Record<string, unknown> {
  const orgs = {
    usco: { name: 'US Co', country: 'US', control_country: 'US', flags: [], namespaces: [] },
    cnco: { name: 'CN Co', country: 'CN', control_country: 'CN', flags: [], namespaces: [] },
  };
  const hosts = {
    ushost: { name: 'US Host', org: 'usco', headquarters: 'US', datacenters: ['US'] },
    cnhost: { name: 'CN Host', org: 'cnco', headquarters: 'CN', datacenters: ['CN'] },
  };
  const models: Record<string, unknown> = {};
  for (const mid of modelIds) {
    const cn = CN_NAMESPACES.includes(mid.split('/')[0]);
    models[baseModelId(mid)] = {
      developer: cn ? 'cnco' : 'usco', lineage: [cn ? 'cnco' : 'usco'],
      known: true, kind: 'model', open_weights: null, license: null,
      hosts: ['cnhost', 'ushost'], platforms: {},
    };
  }
  return { schema: 1, as_of: null, orgs, hosts, models };
}

function bundleWithOriginsTexts(): {
  manifest: Record<string, any>;
  files: Record<string, string>;
  ids: string[];
} {
  const { manifest, files } = starterFiles();
  const ids = (JSON.parse(files['models.json']).models as Array<{ model_id: string }>).map(
    (m) => m.model_id,
  );
  const text = JSON.stringify(originsFor(ids));
  return {
    manifest: { ...manifest, files: { ...manifest.files, 'origins.json': sha256(text) } },
    files: { ...files, 'origins.json': text },
    ids,
  };
}

function bundleWithOrigins(): { bundle: CatalogBundle; ids: string[] } {
  const { manifest, files, ids } = bundleWithOriginsTexts();
  return { bundle: bundleFromTexts(manifest, files), ids };
}

describe('origins.json in the catalog bundle (contract v1.2)', () => {
  it('is optional and parsed when listed', () => {
    expect(starterBundle().origins).toBeNull();
    const { bundle } = bundleWithOrigins();
    expect(Object.keys(bundle.origins ?? {})).toEqual(
      expect.arrayContaining(['orgs', 'hosts', 'models']),
    );
  });

  it('must be present and match when listed', () => {
    const { manifest, files } = starterFiles();
    const listed = { ...manifest, files: { ...manifest.files, 'origins.json': '0'.repeat(64) } };
    expect(() => bundleFromTexts(listed, files)).toThrow(BundleIntegrityError);
    expect(() => bundleFromTexts(listed, files)).toThrow(/missing origins\.json/);
    expect(() => bundleFromTexts(listed, { ...files, 'origins.json': '{}' })).toThrow(
      /origins\.json does not match/,
    );
  });

  it('is ignored when not listed', () => {
    const { manifest, files } = starterFiles();
    const bundle = bundleFromTexts(manifest, { ...files, 'origins.json': 'not even json' });
    expect(bundle.origins).toBeNull();
  });

  it('must have orgs, hosts and models objects; a non-string hash is rejected', () => {
    const { manifest, files } = starterFiles();
    const text = '{"orgs": {}, "hosts": []}';
    expect(() =>
      bundleFromTexts(
        { ...manifest, files: { ...manifest.files, 'origins.json': sha256(text) } },
        { ...files, 'origins.json': text },
      ),
    ).toThrow("origins.json must have 'orgs', 'hosts' and 'models' objects");
    expect(() =>
      bundleFromTexts({ ...manifest, files: { ...manifest.files, 'origins.json': 5 } }, files),
    ).toThrow("manifest.json 'files' has a non-string hash for: origins.json");
  });

  it('loadBundle reads a listed origins.json', () => {
    const { manifest, files } = bundleWithOriginsTexts();
    const dir = mkdtempSync(join(tmpdir(), 'tryaii-origins-'));
    for (const [name, text] of Object.entries(files)) writeFileSync(join(dir, name), text, 'utf-8');
    writeFileSync(join(dir, 'manifest.json'), JSON.stringify(manifest), 'utf-8');
    expect(loadBundle(dir).origins).toEqual(JSON.parse(files['origins.json']));
  });

  it('the cache keeps origins.json (saveFull writes listed optional files)', () => {
    const { manifest, files } = bundleWithOriginsTexts();
    const env = { ...process.env, TRYAII_DRE_DATA_DIR: mkdtempSync(join(tmpdir(), 'tryaii-cache-')) };
    const full = signManifest({ ...manifest, kind: 'full', version: '2026.10.10.1' });
    const bundle = bundleFromTexts(full, files, null, { verifySignature: true, env });
    const placed = catalogClient.saveFull(full, files, bundle, env);
    expect(placed.origins).toEqual(bundle.origins);
    expect(existsSync(join(placed.directory as string, 'origins.json'))).toBe(true);
    expect(loadBundle(placed.directory as string, { verifySignature: true, env }).origins).toEqual(
      bundle.origins,
    );
  });
});

// ---------------------------------------------------------------- router
function stubClassification(benchmarks: string[]): ClassificationResult {
  const scores: Record<string, number> = {};
  for (const b of benchmarks) scores[b] = 1.0;
  return {
    benchmarkScores: scores,
    broadCategory: 'GENERAL',
    subcategory: 'GENERAL',
    confidence: 1.0,
    classifierUsed: 'stub',
    cacheHit: false,
    processingTimeMs: 0,
  } as ClassificationResult;
}

function makeRouter(bundle: CatalogBundle, policy: ModelPolicy | PolicyDict | 'auto' | null): Router {
  const router = new Router({ bundle, policy });
  const cls = stubClassification(bundle.benchmarkNames);
  (router as unknown as { _classifier: unknown })._classifier = {
    classify: () => cls,
    classifyAsync: async () => cls,
  };
  return router;
}

describe('Router policy', () => {
  it('rejects an active policy on a bundle without origins', () => {
    expect(() => new Router({ bundle: starterBundle(), policy: { origin: 'no-prc-models' } })).toThrow(
      PolicyCatalogError,
    );
    expect(() => new Router({ bundle: starterBundle(), policy: null })).not.toThrow();
  });

  it('filters candidates and pins hosts', async () => {
    const { bundle } = bundleWithOrigins();
    let router = makeRouter(bundle, new ModelPolicy({ origin: 'no-prc-hosting', providers: null }));
    const first = router.policyResult!.allowed[0];
    expect(router.providerPreferences(first)).toEqual({ only: ['ushost'] });

    router = makeRouter(bundle, { origin: 'no-prc-models' });
    const result = await router.route('hello', { topK: 50 });
    expect(result.scores.length).toBeGreaterThan(0);
    expect(result.scores.some((s) => CN_NAMESPACES.includes(s.modelId.split('/')[0]))).toBe(false);
    expect(router.providerPreferences(result.bestModel)).toBeNull(); // hosts unconstrained
    const excluded = Object.keys(router.policyResult!.excluded)[0];
    if (excluded !== undefined) {
      expect(() => router.providerPreferences(excluded)).toThrow(PolicyError);
      expect(() => router.providerPreferences(excluded)).toThrow(
        `model '${excluded}' is not allowed by the active policy (origin no-prc-models)`,
      );
    }
  });

  it('route allowedModels limits candidates', async () => {
    const { bundle, ids } = bundleWithOrigins();
    const router = makeRouter(bundle, null);
    const result = await router.route('hello', { topK: 50, allowedModels: ids.slice(0, 2) });
    for (const s of result.scores) expect(ids.slice(0, 2)).toContain(s.modelId);
  });

  it('the saved policy is the router default', () => {
    savePolicy(new ModelPolicy({ origin: 'us-eu' }));
    try {
      const { bundle } = bundleWithOrigins();
      expect(makeRouter(bundle, 'auto').policy.origin).toBe('us-eu');
      expect(new Router({ bundle }).policy.origin).toBe('us-eu');
      expect(makeRouter(bundle, null).policy.active).toBe(false);
    } finally {
      clearSavedPolicy();
    }
  });
});

// ---------------------------------------------------------------- openrouter payload
function capturePayloads(): Array<Record<string, unknown>> {
  const sent: Array<Record<string, unknown>> = [];
  vi.stubGlobal('fetch', async (_url: string, init: { body: string }) => {
    sent.push(JSON.parse(init.body));
    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ choices: [{ message: { content: 'ok' } }], model: 'x', usage: {} }),
    };
  });
  return sent;
}

describe('OpenRouter provider pinning', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('pins provider.only under a host policy', async () => {
    const { bundle } = bundleWithOrigins();
    const sent = capturePayloads();
    const integ = new OpenRouterIntegration(makeRouter(bundle, { origin: 'no-prc-hosting' }), {
      apiKey: 'k',
    });
    await integ.chat('hello');
    expect(sent[0].provider).toEqual({ only: ['ushost'] });
  });

  it('sends no provider without a host constraint', async () => {
    const { bundle } = bundleWithOrigins();
    for (const policy of [{ origin: 'no-prc-models' }, null]) {
      const sent = capturePayloads();
      await new OpenRouterIntegration(makeRouter(bundle, policy), { apiKey: 'k' }).chat('hello');
      expect('provider' in sent[0]).toBe(false);
    }
  });

  it('tolerates a router without providerPreferences', async () => {
    const sent = capturePayloads();
    const stub = { route: async () => ({ bestModel: 'openai/gpt-x', scores: [] }) };
    await new OpenRouterIntegration(stub, { apiKey: 'k' }).chat('hello');
    expect(sent[0].model).toBeDefined();
    expect('provider' in sent[0]).toBe(false);
  });

  it('refuses an override of an excluded model', async () => {
    const { bundle } = bundleWithOrigins();
    const router = makeRouter(bundle, { origin: 'no-prc-models' });
    const excluded = Object.keys(router.policyResult!.excluded)[0];
    if (excluded === undefined) return; // starter has no PRC model
    const sent = capturePayloads();
    await expect(
      new OpenRouterIntegration(router, { apiKey: 'k' }).chat('hello', { overrideModel: excluded }),
    ).rejects.toThrow(PolicyError);
    expect(sent).toEqual([]);
  });
});

// ---------------------------------------------------------------- saved policy
describe('saved policy', () => {
  afterEach(() => {
    clearSavedPolicy();
  });

  it('writes the same bytes as Python json.dumps(indent=2) + newline', () => {
    savePolicy(new ModelPolicy({ origin: 'no-prc-models', providers: ['openai', 'café'] }));
    expect(readFileSync(policyPath(), 'utf-8')).toBe(
      '{\n  "origin": "no-prc-models",\n  "providers": [\n    "caf\\u00e9",\n    "openai"\n  ],\n' +
        '  "exclude_providers": [],\n  "allow_countries": [],\n  "deny_countries": [],\n' +
        '  "platform": null\n}\n',
    );
    expect(loadSavedPolicy().providers).toEqual(['café', 'openai']);
  });

  it('a missing file is an inactive policy; a broken one throws', () => {
    expect(loadSavedPolicy().active).toBe(false);
    writeFileSync(policyPath(), '{nope', 'utf-8');
    expect(() => loadSavedPolicy()).toThrow(/is not valid JSON/);
    writeFileSync(policyPath(), '{"region": "eu"}', 'utf-8');
    expect(() => loadSavedPolicy()).toThrow('unknown policy field(s): region');
    expect(clearSavedPolicy()).toBe(true);
    expect(clearSavedPolicy()).toBe(false);
  });
});

// ---------------------------------------------------------------- CLI
const DIST_CLI = fileURLToPath(new URL('../dist/cli.js', import.meta.url));
const HAS_DIST = existsSync(DIST_CLI);
const DIST_ONLY = '[needs dist/: npm run build]';

interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

function runCli(dataDir: string, args: string[]): Promise<RunResult> {
  const childEnv: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined && !k.toUpperCase().startsWith('TRYAII_')) childEnv[k] = v;
  }
  Object.assign(childEnv, {
    TRYAII_DRE_DATA_DIR: dataDir,
    TRYAII_API_URL: 'http://127.0.0.1:9',
    TRYAII_NO_DAEMON: '1',
    TRYAII_NO_BANNER: '1',
    TRYAII_CATALOG_TRUSTED_KEYS: process.env.TRYAII_CATALOG_TRUSTED_KEYS as string,
  });
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [DIST_CLI, ...args], {
      env: childEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
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

/** A logged-in data dir whose cached full catalog carries origins.json. */
function dataDirWithFullOrigins(): string {
  const dir = mkdtempSync(join(tmpdir(), 'tryaii-policy-cli-'));
  const env = { ...process.env, TRYAII_DRE_DATA_DIR: dir };
  const { manifest, files } = bundleWithOriginsTexts();
  const version = '2026.10.10.1';
  const full = signManifest({ ...manifest, kind: 'full', version });
  const bundle = bundleFromTexts(full, files, null, { verifySignature: true, env });
  catalogClient.saveFull(full, files, bundle, env);
  catalogClient.writeState(version, Date.now(), env);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, 'credentials.json'),
    JSON.stringify({
      version: 1, api_url: 'http://127.0.0.1:9',
      user: { id: 'u', email: 'u@example.com', name: 'U' }, entitlements: ['catalog:full'],
      refresh_token: 'r', refresh_expires_at: '2099-01-01T00:00:00Z',
      access_token: 'a', access_expires_at: '2099-01-01T00:00:00Z',
      created_at: '2026-10-10T00:00:00Z',
    }),
    'utf-8',
  );
  return dir;
}

describe.skipIf(!HAS_DIST)(`tryaii policy / policy flags ${DIST_ONLY}`, () => {
  let dataDir: string;
  beforeEach(() => {
    dataDir = mkdtempSync(join(tmpdir(), 'tryaii-policy-cli-'));
  });

  it('policy set / show / clear', async () => {
    let r = await runCli(dataDir, ['policy']);
    expect([r.code, r.stdout]).toEqual([0, 'No policy saved. All models are allowed.\n']);
    r = await runCli(dataDir, ['policy', 'set', '--origin', 'no-prc-models', '--providers', 'openai,Anthropic']);
    expect([r.code, r.stdout]).toEqual([
      0,
      'Saved policy: origin no-prc-models; providers anthropic,openai\n',
    ]);
    r = await runCli(dataDir, ['policy', 'set', '--platform', 'bedrock']);
    expect(r.stdout).toBe(
      'Saved policy: origin no-prc-models; providers anthropic,openai; platform bedrock\n',
    );
    r = await runCli(dataDir, ['policy', 'show']);
    expect(r.stdout).toContain('platform bedrock');
    r = await runCli(dataDir, ['policy', 'clear']);
    expect(r.stdout).toBe('Policy cleared.\n');
    r = await runCli(dataDir, ['policy', 'clear']);
    expect(r.stdout).toBe('No policy saved.\n');
  });

  it('policy set without flags or with a bad value exits 2', async () => {
    let r = await runCli(dataDir, ['policy', 'set']);
    expect(r.code).toBe(2);
    expect(r.stderr).toBe(
      'error: policy set needs at least one of --origin, --providers, ' +
        '--exclude-providers, --allow-countries, --deny-countries, --platform\n',
    );
    r = await runCli(dataDir, ['policy', 'set', '--origin', 'no-china']);
    expect(r.code).toBe(2);
    expect(r.stderr).toBe(
      "error: unknown origin 'no-china'; use one of: no-prc-models, no-prc-hosting, us-eu\n",
    );
    expect(existsSync(join(dataDir, 'policy.json'))).toBe(false);
    r = await runCli(dataDir, ['policy', 'bogus']);
    expect(r.code).toBe(2);
  });

  it('policy help is the shared help text', async () => {
    const a = await runCli(dataDir, ['help', 'policy']);
    const b = await runCli(dataDir, ['policy', '--help']);
    expect(a.code).toBe(0);
    expect(a.stdout).toContain('tryaii policy -- Show, set or clear the model origin / provider policy');
    expect(b.stdout).toBe(a.stdout);
  });

  it('models / route with a policy on the starter catalog fail closed', async () => {
    for (const args of [
      ['models', '--origin', 'no-prc-models'],
      ['route', 'hello', '--origin', 'no-prc-models'],
    ]) {
      const r = await runCli(dataDir, args);
      expect(r.code, args.join(' ')).toBe(1);
      expect(r.stderr.trim()).toBe(MSG_NEEDS_FULL_CATALOG);
    }
  });

  it('a bad flag value exits 2', async () => {
    const r = await runCli(dataDir, ['models', '--platform', 'oracle']);
    expect(r.code).toBe(2);
    expect(r.stderr).toBe("error: unknown platform 'oracle'; use one of: bedrock, azure, vertex\n");
  });

  it('a broken saved policy fails closed', async () => {
    writeFileSync(join(dataDir, 'policy.json'), '{nope', 'utf-8');
    const r = await runCli(dataDir, ['models']);
    expect(r.code).toBe(1);
    expect(r.stderr).toContain('not valid JSON');
  });

  it('models lists each origin and filters by the policy', async () => {
    const dir = dataDirWithFullOrigins();
    let r = await runCli(dir, ['models']);
    expect(r.code).toBe(0);
    expect(r.stdout).toContain(' | origin: US');
    expect(r.stdout).not.toContain('Policy:');
    r = await runCli(dir, ['models', '--origin', 'no-prc-models']);
    expect(r.code).toBe(0);
    expect(r.stdout).toMatch(/\nAvailable Models \(\d+\):\nPolicy: origin no-prc-models \(\d+ of \d+ models allowed\)\n-{70}\n/);
    r = await runCli(dir, ['models', '--origin', 'no-prc-models', '--json']);
    const listed = JSON.parse(r.stdout) as Array<Record<string, unknown>>;
    expect(listed.length).toBeGreaterThan(0);
    expect(listed.every((m) => m.origin === 'US')).toBe(true);
    expect(Object.keys(listed[0]).at(-1)).toBe('origin');
  });
});

// ---------------------------------------------------------------- daemon
function emptyResult(priorities: Priorities): RouteResult {
  return { bestModel: '', scores: [], classification: null, priorities };
}

describe('daemon allowedModels', () => {
  it('only goes to a daemon that advertises support', async () => {
    const config = createDefaultConfig({ dataDir: mkdtempSync(join(tmpdir(), 'tryaii-daemon-')) });
    const seen: Array<RouteOptions | undefined> = [];
    let resolveReady: (s: daemon.DaemonState) => void;
    const ready = new Promise<daemon.DaemonState>((resolve) => {
      resolveReady = resolve;
    });
    const closed = serve(
      { embeddingModel: config.embeddingModel, dataDir: config.dataDir },
      {
        idleTimeout: 0,
        router: {
          async route(_prompt: string, opts?: RouteOptions): Promise<RouteResult> {
            seen.push(opts);
            return emptyResult(opts?.priorities ?? new Priorities());
          },
        },
        log: () => {},
        onReady: (s) => resolveReady(s),
      },
    );
    try {
      const state = await ready;
      expect(state.features).toContain(daemon.FEATURE_ALLOWED_MODELS);

      // An older daemon (no features): refused before any request is sent.
      const { features: _f, ...old } = state;
      await expect(daemon.routeViaDaemon(old, 'p', new Priorities(), 1, ['a/b'])).rejects.toThrow(
        /allowedModels/,
      );
      expect(seen).toEqual([]);

      await daemon.routeViaDaemon(state, 'p', new Priorities(), 1, ['a/b']);
      expect(seen.at(-1)?.allowedModels).toEqual(['a/b']);

      // No policy: the router is called exactly as before (no allowedModels key).
      await daemon.routeViaDaemon(state, 'p', new Priorities(), 1);
      expect(seen.at(-1) && 'allowedModels' in (seen.at(-1) as object)).toBe(false);

      const resp = await daemon.request(
        state,
        { cmd: 'route', prompt: 'p', allowedModels: 'a/b' },
        5000,
      );
      expect(resp).toEqual({ ok: false, error: 'allowedModels must be a list of model ids' });
    } finally {
      await daemon.stop(config);
      await closed;
    }
  });

  it('routeWithFallback falls back in-process when the daemon lacks the feature', async () => {
    const state = {
      runtime: 'node', version: '0', embeddingModel: 'm', host: '127.0.0.1', port: 9,
      token: 't', pid: 0, startedAtMs: 0,
    } as daemon.DaemonState;
    let fellBack = 0;
    const fn = daemon.routeWithFallback(
      state,
      () => async (_p, priorities) => {
        fellBack++;
        return emptyResult(priorities);
      },
      ['a/b'],
    );
    await fn('p', new Priorities(), 1);
    expect(fellBack).toBe(1);
  });
});
