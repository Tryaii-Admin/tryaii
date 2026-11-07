/**
 * The H4 router: static provider, head, catalog mask, payload integrity,
 * router selection and fallback, monitor/daemon wiring. Mirrors
 * packages/python/tests/test_router_h4.py.
 *
 * Uses the linked `tryaii-router-h4` package (packages/router-h4-model, a
 * devDependency); the cross-SDK cases live in shared/router/h4_cases.json
 * (scripts/gen-h4-fixtures.py, made by the Python SDK).
 */

import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { starterBundle } from '../src/catalog/bundle.js';
import { routerLine } from '../src/cli-monitor.js';
import { BaseEmbeddingProvider } from '../src/embeddings/base.js';
import { HeadClassifier, RouterHead, softmax } from '../src/classifiers/head.js';
import {
  h4Ready,
  loadH4Classifier,
  recordH4Unavailable,
  resetSelectionWarnings,
} from '../src/classifiers/selection.js';
import {
  DEFAULT_ROUTER,
  ROUTER_H4,
  ROUTER_MINILM,
  createDefaultConfig,
  normalizeRouter,
  usesH4,
} from '../src/config.js';
import * as daemon from '../src/daemon.js';
import * as modelStore from '../src/embeddings/modelStore.js';
import { StaticEmbeddingProvider, loadSafetensors } from '../src/embeddings/static.js';
import { InProcessEngine } from '../src/monitor/engine.js';
import { Router } from '../src/router.js';
import { serve } from '../src/server.js';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..', '..');

interface H4Case {
  name: string;
  text: string;
  expected: {
    token_ids: number[];
    probabilities: Record<string, number>;
    top3: string[];
    masked: { probabilities: Record<string, number>; top3: string[]; masked_classes: number };
  };
}
const fixture = JSON.parse(readFileSync(join(repo, 'shared', 'router', 'h4_cases.json'), 'utf-8')) as {
  router_id: string;
  mask: { catalog: string; keep: string[] };
  cases: H4Case[];
};
const SPEC = modelStore.ROUTERS[modelStore.DEFAULT_ROUTER_MODEL];
const TOL = 1e-6;

let dataDir: string;
let stderr: string[];

beforeEach(() => {
  delete process.env.TRYAII_ROUTER;
  delete process.env[modelStore.ENV_ROUTER_MODEL_PATH];
  dataDir = mkdtempSync(join(tmpdir(), 'tryaii-h4-'));
  modelStore.resetModelStoreCache();
  modelStore.setRouterPackageLocator(null);
  resetSelectionWarnings();
  stderr = [];
  vi.spyOn(process.stderr, 'write').mockImplementation((chunk: unknown) => {
    stderr.push(String(chunk));
    return true;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  delete process.env.TRYAII_ROUTER;
  delete process.env[modelStore.ENV_ROUTER_MODEL_PATH];
  modelStore.resetModelStoreCache();
  modelStore.setRouterPackageLocator(null);
});

const provider = new StaticEmbeddingProvider();
const headOf = (): RouterHead => RouterHead.fromDir(provider.directory);

function payloadCopy(): string {
  const src = modelStore.resolveRouterDir();
  expect(src).not.toBeNull();
  const dst = join(mkdtempSync(join(tmpdir(), 'tryaii-h4-copy-')), 'h4');
  mkdirSync(dst);
  for (const name of SPEC.files) copyFileSync(join(src as string, name), join(dst, name));
  return dst;
}

function sortedTop(scores: Record<string, number>): string[] {
  return Object.keys(scores).sort((a, b) => scores[b] - scores[a] || (a < b ? -1 : a > b ? 1 : 0));
}

// -- payload integrity -------------------------------------------------------------

describe('payload', () => {
  it('the installed package matches the pin', async () => {
    const pkg = (await import('tryaii-router-h4')) as {
      MODEL_ID: string; SHA256: string; SIZE_BYTES: number; FILES: string[]; modelDir(): string;
    };
    expect(pkg.MODEL_ID).toBe(SPEC.routerId);
    expect(pkg.SHA256).toBe(SPEC.sha256);
    expect(pkg.SIZE_BYTES).toBe(SPEC.sizeBytes);
    expect([...pkg.FILES]).toEqual([...SPEC.files]);
    expect(modelStore.payloadSha256(pkg.modelDir(), SPEC.files)).toEqual([SPEC.sha256, SPEC.sizeBytes]);
  });

  it('SHA256SUMS is the hashed manifest', () => {
    const dir = modelStore.resolveRouterDir() as string;
    const text = readFileSync(join(dir, 'SHA256SUMS'), 'utf-8');
    expect(text).toBe(modelStore.payloadManifest(dir, SPEC.files)[0]);
    expect(createHash('sha256').update(text, 'utf-8').digest('hex')).toBe(SPEC.sha256);
  });

  it('resolves the package', () => {
    const dir = modelStore.resolveRouterDir();
    expect(dir).not.toBeNull();
    expect(modelStore.lastRouterError()).toBeNull();
  });

  it('override with a good copy', () => {
    const copy = payloadCopy();
    process.env[modelStore.ENV_ROUTER_MODEL_PATH] = copy;
    expect(modelStore.resolveRouterDir()).toBe(copy);
  });

  it('override with a tampered copy (same size)', () => {
    const copy = payloadCopy();
    const meta = join(copy, 'meta.json');
    const data = readFileSync(meta);
    data[data.length - 2] = ' '.charCodeAt(0);
    writeFileSync(meta, data);
    process.env[modelStore.ENV_ROUTER_MODEL_PATH] = copy;
    expect(modelStore.resolveRouterDir()).toBeNull();
    expect(modelStore.lastRouterError()).toContain(modelStore.ENV_ROUTER_MODEL_PATH);
  });

  it('override with a missing file', () => {
    const copy = payloadCopy();
    writeFileSync(join(copy, 'head.safetensors'), '');
    process.env[modelStore.ENV_ROUTER_MODEL_PATH] = copy;
    expect(modelStore.resolveRouterDir()).toBeNull();
  });

  it('package not installed', () => {
    modelStore.setRouterPackageLocator(() => null);
    expect(modelStore.resolveRouterDir()).toBeNull();
    expect(modelStore.lastRouterError()).toContain('not installed');
  });

  it('unknown router', () => {
    expect(modelStore.resolveRouterDir('nope')).toBeNull();
  });

  it('safetensors reader', () => {
    const t = loadSafetensors(join(provider.directory, 'head.safetensors'));
    expect(Object.keys(t).sort()).toEqual(['W0', 'W1', 'b0', 'b1']);
    expect(t.W0.shape).toEqual([512, 256]);
    expect(t.W0.dtype).toBe('F32');
    const v = loadSafetensors(join(provider.directory, 'vectors.safetensors')).embeddings;
    expect(v.shape).toEqual([40000, 256]);
    expect(v.dtype).toBe('I8');
  });

  it('meta', () => {
    const meta = provider.meta;
    expect(meta.router_id).toBe('h4-he-en');
    expect(meta.version).toBe('1.0.0');
    expect(meta.temperature).toBe(2);
    expect(meta.max_tokens).toBe(700);
    expect(meta.dim).toBe(256);
    expect(meta.char_cap).toBe(meta.max_tokens * meta.median_token_length);
  });
});

// -- static provider ---------------------------------------------------------------

describe('StaticEmbeddingProvider', () => {
  it('unit vector of float32 values', () => {
    const v = provider.embed('Write a haiku about autumn leaves.');
    expect(v.length).toBe(256);
    expect(Math.abs(Math.hypot(...v) - 1)).toBeLessThan(1e-5);
    for (const x of v) expect(Math.fround(x)).toBe(x);
  });

  it('deterministic, batch equals single, sync', async () => {
    const texts = ['first prompt', 'שלום עולם', 'third one'];
    const batch = provider.embedBatch(texts);
    texts.forEach((t, i) => expect(provider.embed(t)).toEqual(batch[i]));
    expect(await provider.embedAsync('x y')).toEqual(provider.embed('x y'));
    expect(provider.supportsSync).toBe(true);
  });

  it('empty text is the zero vector', () => {
    expect(provider.tokenIds('')).toEqual([]);
    expect(provider.embed('').every((x) => x === 0)).toBe(true);
  });

  it('mean of int8 rows', () => {
    const text = 'Mean pooling over the int8 token vectors.';
    const ids = provider.tokenIds(text);
    const v = loadSafetensors(join(provider.directory, 'vectors.safetensors')).embeddings.data as Int8Array;
    const mean = new Array<number>(256).fill(0);
    for (const id of ids) for (let d = 0; d < 256; d++) mean[d] += v[id * 256 + d] / ids.length;
    const norm = Math.hypot(...mean);
    const got = provider.embed(text);
    for (let d = 0; d < 256; d++) expect(Math.abs(got[d] - mean[d] / norm)).toBeLessThan(1e-6);
  });

  it('char cap and token cap', () => {
    const cap = provider.meta.char_cap;
    const long = 'word '.repeat(2000);
    expect(provider.tokenIds(long)).toEqual(provider.tokenIds(long.slice(0, cap)));
    expect(provider.tokenIds('a b c d e f g '.repeat(400)).length).toBeLessThanOrEqual(700);
  });

  it('drops the unknown token, adds no special tokens', () => {
    expect(provider.tokenIds('\u{1F600} ☃ அ')).not.toContain(provider.meta.unk_id);
    const ids = provider.tokenIds('hello');
    expect(ids).not.toContain(0);
    expect(ids.length).toBeGreaterThanOrEqual(1);
  });

  it('dimension, name, unknown model', () => {
    const p = new StaticEmbeddingProvider();
    expect(p.dimension).toBe(256);
    expect(p.modelName).toBe('h4-he-en');
    expect(() => new StaticEmbeddingProvider('nope')).toThrow();
  });

  it('unavailable', () => {
    modelStore.setRouterPackageLocator(() => null);
    expect(() => new StaticEmbeddingProvider().ensureLoaded()).toThrow(modelStore.RouterUnavailableError);
  });
});

// -- head ----------------------------------------------------------------------------

describe('RouterHead', () => {
  it('softmax with temperature', () => {
    const p = softmax([2, 0], 2);
    expect(p[0]).toBeCloseTo(Math.E / (Math.E + 1), 12);
    expect(softmax([1000, 1000], 2)).toEqual([0.5, 0.5]);
  });

  it('tiny head math (relu between layers, none after the last)', () => {
    const h = new RouterHead(
      [RouterHead.layer([1, -1, 0.5, 0.5], [0, -1], 2, 2), RouterHead.layer([1, 0, 0, 1, 1, 1], [0, 0, -1], 3, 2)],
      ['a', 'b', 'c'],
      2,
    );
    expect(Array.from(h.logits([1, 0]))).toEqual([1, 0, 0]);
    expect(h.probabilities([1, 0])).toEqual(softmax([1, 0, 0], 2));
    const s = h.scores([1, 0]);
    expect(Object.values(s).reduce((a, b) => a + b, 0)).toBeCloseTo(1, 12);
  });

  it('class mismatch', () => {
    expect(() => new RouterHead([RouterHead.layer([0, 0, 0, 0], [0, 0], 2, 2)], ['only-one'], 2)).toThrow();
  });

  it('payload head', () => {
    const head = headOf();
    expect(head.classes.length).toBe(33);
    expect(head.temperature).toBe(2);
    expect(head.classes).toEqual([...head.classes].sort());
  });

  it('mask renormalises (= softmax of the kept logits / T)', () => {
    const head = headOf();
    const e = provider.embed('Prove that the square root of 2 is irrational.');
    const full = head.scores(e);
    const masked = head.scores(e, new Set(['GPQA', 'MATH', 'HLE', 'not-a-class']));
    expect(Object.keys(masked).sort()).toEqual(['GPQA', 'HLE', 'MATH']);
    expect(Object.values(masked).reduce((a, b) => a + b, 0)).toBeCloseTo(1, 12);
    const total = full.GPQA + full.MATH + full.HLE;
    for (const [n, v] of Object.entries(masked)) expect(v).toBeCloseTo(full[n] / total, 12);
    expect(head.scores(e, new Set())).toEqual({});
  });
});

// -- cross-SDK cases (shared/router/h4_cases.json) -----------------------------------

describe('shared H4 cases', () => {
  const head = headOf();
  it('fixture is for this router', () => {
    expect(fixture.router_id).toBe(SPEC.routerId);
    expect(fixture.cases.length).toBe(14);
  });

  for (const c of fixture.cases) {
    it(`${c.name}: token ids, probabilities, top-3`, () => {
      const exp = c.expected;
      expect(provider.tokenIds(c.text)).toEqual(exp.token_ids);
      const got = head.scores(provider.embed(c.text));
      expect(Object.keys(got).sort()).toEqual(Object.keys(exp.probabilities).sort());
      for (const [name, value] of Object.entries(exp.probabilities)) {
        expect(Math.abs(got[name] - value), name).toBeLessThanOrEqual(TOL);
      }
      expect(sortedTop(got)[0]).toBe(exp.top3[0]);
      expect(sortedTop(got).slice(0, 3)).toEqual(exp.top3);
    });

    it(`${c.name}: masked to ${fixture.mask.catalog}`, () => {
      const exp = c.expected.masked;
      const got = head.scores(provider.embed(c.text), new Set(fixture.mask.keep));
      expect(Object.keys(got).sort()).toEqual([...fixture.mask.keep].sort());
      for (const [name, value] of Object.entries(exp.probabilities)) {
        expect(Math.abs(got[name] - value), name).toBeLessThanOrEqual(TOL);
      }
      expect(sortedTop(got)[0]).toBe(exp.top3[0]);
      expect(sortedTop(got).slice(0, 3)).toEqual(exp.top3);
      if (c.text) {
        const r = new HeadClassifier(provider, { catalogBenchmarks: fixture.mask.keep }).classify(c.text);
        expect(r.maskedClasses).toBe(exp.masked_classes);
        expect(r.confidence).toBe(got[exp.top3[0]]);
      }
    });
  }
});

// -- classifier ----------------------------------------------------------------------

describe('HeadClassifier', () => {
  it('classifies without a catalog: every class', () => {
    const r = new HeadClassifier(provider).load().classify('Fix the segfault in this C program.');
    expect(r.classifierUsed).toBe('head');
    expect(Object.keys(r.benchmarkScores).length).toBe(33);
    expect(r.confidence).toBe(Math.max(...Object.values(r.benchmarkScores)));
    expect(r.difficulty).toBeGreaterThan(0);
    expect(r.difficulty).toBeLessThan(1);
    expect(r.broadCategory).toBeTruthy();
    expect(r.subcategory).toBeTruthy();
    expect(r.maskedClasses).toBe(0);
  });

  it('cache hit', () => {
    const clf = new HeadClassifier(provider);
    const first = clf.classify('cache me');
    const second = clf.classify('cache me');
    expect(first.cacheHit).toBe(false);
    expect(second.cacheHit).toBe(true);
    expect(second.benchmarkScores).toEqual(first.benchmarkScores);
  });

  it('masked to the catalog, renormalised', () => {
    const keep = ['GPQA', 'HLE', 'Chatbot Arena Elo', 'SWE-bench-Pro'];
    const clf = new HeadClassifier(provider, { catalogBenchmarks: [...keep, 'Custom'] });
    expect(clf.keptClasses).toEqual([...keep].sort());
    expect(clf.maskedClasses).toBe(29);
    const r = clf.classify('Fix the segfault in this C program.');
    expect(Object.keys(r.benchmarkScores).sort()).toEqual([...keep].sort());
    expect(Object.values(r.benchmarkScores).reduce((a, b) => a + b, 0)).toBeCloseTo(1, 12);
    expect(r.maskedClasses).toBe(29);
    expect(r.confidence).toBe(Math.max(...Object.values(r.benchmarkScores)));
  });

  it('a bundle masks to its benchmarks', () => {
    const bundle = starterBundle();
    const clf = new HeadClassifier(provider, { bundle });
    const names = new Set(bundle.benchmarkNames);
    expect(clf.keptClasses).toEqual(clf.head.classes.filter((c) => names.has(c)));
    const r = clf.classify('Summarise this contract clause.');
    for (const n of Object.keys(r.benchmarkScores)) expect(names.has(n)).toBe(true);
    expect(r.maskedClasses).toBe(33 - Object.keys(r.benchmarkScores).length);
  });

  it('all classes masked: throws', () => {
    const clf = new HeadClassifier(provider, { catalogBenchmarks: ['Custom'] });
    expect(clf.keptClasses).toEqual([]);
    expect(() => clf.classify('anything')).toThrow();
  });

  it('difficulty orders easy below hard', () => {
    const clf = new HeadClassifier(provider);
    const easy = clf.classify('What is the capital of France?').difficulty as number;
    const hard = clf.classify(
      'Design a distributed consensus protocol tolerant to Byzantine faults and prove its safety and liveness.',
    ).difficulty as number;
    expect(easy).toBeLessThan(hard);
  });
});

// -- selection + fallback --------------------------------------------------------------

class FakeSyncProvider extends BaseEmbeddingProvider {
  override embed(text: string): number[] {
    let seed = 0;
    for (const ch of Buffer.from(text, 'utf-8')) seed = (seed * 31 + ch) >>> 0;
    const v = Array.from({ length: 384 }, (_, i) => Math.sin(seed + i * 1.7));
    const n = Math.hypot(...v);
    return v.map((x) => x / n);
  }
  override get supportsSync(): boolean {
    return true;
  }
  get dimension(): number {
    return 384;
  }
  get modelName(): string {
    return 'fake';
  }
}

describe('selection', () => {
  it('normalizeRouter', () => {
    expect(DEFAULT_ROUTER).toBe(ROUTER_H4);
    expect(normalizeRouter(undefined)).toBe(ROUTER_H4);
    expect(normalizeRouter('')).toBe(ROUTER_H4);
    expect(normalizeRouter(' H4 ')).toBe(ROUTER_H4);
    expect(normalizeRouter('h4-he-en')).toBe(ROUTER_H4);
    expect(normalizeRouter('minilm')).toBe(ROUTER_MINILM);
    expect(normalizeRouter('MiniLM')).toBe(ROUTER_MINILM);
    expect(normalizeRouter('bogus')).toBe(ROUTER_H4);
    expect(stderr.join('')).toContain('is not a known router');
  });

  it('config reads TRYAII_ROUTER', () => {
    expect(createDefaultConfig().router).toBe(ROUTER_H4);
    process.env.TRYAII_ROUTER = 'minilm';
    expect(createDefaultConfig().router).toBe(ROUTER_MINILM);
    expect(usesH4(createDefaultConfig())).toBe(false);
  });

  it('a custom embedding model disables H4', () => {
    expect(usesH4(createDefaultConfig({ embeddingModel: 'all-mpnet-base-v2' }))).toBe(false);
    expect(usesH4(createDefaultConfig())).toBe(true);
    expect(usesH4(createDefaultConfig({ embeddingModel: modelStore.DEFAULT_ONNX_MODEL }))).toBe(true);
  });

  it('the Router default is H4, masked to the catalog', async () => {
    const router = new Router({ catalog: 'starter', policy: null, config: { dataDir } });
    const result = await router.route('Refactor this JavaScript class into hooks.');
    expect(result.classification?.classifierUsed).toBe('head');
    expect(router.routerId).toBe('h4-he-en');
    // The loaded payload's meta.json version.
    const tag = `h4-he-en@${(router as unknown as { _classifier: HeadClassifier })._classifier.provider.meta.version}`;
    expect(tag).toMatch(/^h4-he-en@\d+\.\d+\.\d+$/);
    expect(result.classification?.router).toBe(tag);
    expect(router.routerTag).toBe(tag);
    expect(result.bestModel).toBeTruthy();
    expect((await router.embedPrompt('x')).length).toBe(256);
    const names = new Set(router.bundle.benchmarkNames);
    const scores = result.classification?.benchmarkScores ?? {};
    for (const n of Object.keys(scores)) expect(names.has(n)).toBe(true);
    expect(Object.values(scores).reduce((a, b) => a + b, 0)).toBeCloseTo(1, 12);
    expect(result.classification?.maskedClasses).toBe(33 - Object.keys(scores).length);
    expect(result.classification?.maskedClasses).toBeGreaterThan(0);
  });

  it('routeSync works under H4', () => {
    const router = new Router({ catalog: 'starter', policy: null, config: { dataDir } });
    const result = router.routeSync('Write a SQL query that finds duplicate emails.');
    expect(result.classification?.classifierUsed).toBe('head');
    expect(result.bestModel).toBeTruthy();
  });

  it('TRYAII_ROUTER=minilm routes on centroids', () => {
    process.env.TRYAII_ROUTER = 'minilm';
    const router = new Router({ catalog: 'starter', policy: null, config: { dataDir } });
    expect(router.routerId).toBe('all-MiniLM-L6-v2');
    expect(router.routerTag).toBe('minilm@all-MiniLM-L6-v2');
  });

  it('falls back to MiniLM when H4 is broken, records and warns', () => {
    process.env[modelStore.ENV_ROUTER_MODEL_PATH] = mkdtempSync(join(tmpdir(), 'tryaii-empty-'));
    const config = createDefaultConfig({ dataDir });
    const router = new Router({ catalog: 'starter', policy: null, config });
    expect(router.routerId).toBe('all-MiniLM-L6-v2');
    const state = modelStore.readState(dataDir, modelStore.ROUTER_STATE_FILE);
    expect(state?.model).toBe('h4-he-en');
    expect(stderr.join('')).toContain('h4-he-en is not available');
    // MiniLM classification is not "off": its own state file is untouched.
    expect(modelStore.readState(dataDir)).toBeNull();
  });

  it('falls back when the catalog has none of the classes, with a warning', () => {
    const bundle = starterBundle();
    vi.spyOn(bundle, 'benchmarkNames', 'get').mockReturnValue(['Only-Custom']);
    const router = new Router({ bundle, policy: null, config: { dataDir } });
    expect(router.routerId).toBe('all-MiniLM-L6-v2');
    expect(stderr.join('')).toContain('none of the benchmarks the router model h4-he-en scores');
    expect(loadH4Classifier(createDefaultConfig({ dataDir }), bundle, {}, () => {})).toBeNull();
  });

  it('state is cleared when H4 is back', () => {
    const config = createDefaultConfig({ dataDir });
    recordH4Unavailable(config, 'test', () => {});
    expect(modelStore.readState(dataDir, modelStore.ROUTER_STATE_FILE)).not.toBeNull();
    expect(h4Ready(config)).toBe(true);
    expect(modelStore.readState(dataDir, modelStore.ROUTER_STATE_FILE)).toBeNull();
  });

  it('a custom provider means centroids', async () => {
    const router = new Router({
      catalog: 'starter',
      policy: null,
      config: { dataDir },
      embeddingProvider: new FakeSyncProvider(),
    });
    const r = router.routeSync('hello there');
    expect(r.classification?.classifierUsed).toBe('embedding');
  });

  it('addBenchmark warns under H4', async () => {
    const router = new Router({ catalog: 'starter', policy: null, config: { dataDir } });
    await router.route('warm up');
    await router.addBenchmark('CustomQA', ['q1', 'q2']);
    expect(stderr.join('')).toContain('not scored by the H4 router');
    expect(router.benchmarks.get('CustomQA')).toBeTruthy();
  });
});

// -- monitor / daemon wiring ---------------------------------------------------------

describe('wiring', () => {
  it('the in-process engine routes with H4 and labels vectors h4-he-en', async () => {
    const config = createDefaultConfig({ dataDir });
    const { engine, reason } = await InProcessEngine.create(config, starterBundle(), 3);
    expect(reason).toBeNull();
    expect(engine?.modelId).toBe('h4-he-en');
    const { data } = await (engine as InProcessEngine).route('Sort a list in Python.', true, null);
    const emb = data.embedding as { model: string; dim: number };
    expect(emb.model).toBe('h4-he-en');
    expect(emb.dim).toBe(256);
    expect((data.classification as { classifierUsed: string }).classifierUsed).toBe('head');
    expect((data.classification as { router: string }).router).toMatch(/^h4-he-en@\d+\.\d+\.\d+$/);
  });

  it('the engine falls back to MiniLM (ONNX) when H4 is missing', async () => {
    modelStore.setRouterPackageLocator(() => null);
    modelStore.setPackageLocator(() => null);
    const config = createDefaultConfig({ dataDir });
    const { engine, reason } = await InProcessEngine.create(config, starterBundle(), 3);
    expect(engine).toBeNull();
    expect(reason).toBeTruthy();
    expect(modelStore.readState(dataDir, modelStore.ROUTER_STATE_FILE)?.model).toBe('h4-he-en');
    modelStore.setPackageLocator(null);
  });

  it('status router line', () => {
    expect(routerLine(dataDir)).toBeNull();
    recordH4Unavailable(createDefaultConfig({ dataDir }), 'package missing', () => {});
    const line = routerLine(dataDir) as string;
    expect(line.startsWith('router: h4-he-en unavailable since ')).toBe(true);
    expect(line).toContain('(package missing); routing with all-MiniLM-L6-v2-onnx-int8');
  });

  it('a daemon is reused only with the same router', async () => {
    const config = createDefaultConfig({ dataDir });
    let resolveReady: (s: daemon.DaemonState) => void = () => {};
    const ready = new Promise<daemon.DaemonState>((resolve) => {
      resolveReady = resolve;
    });
    const fake = {
      route: async () => ({ bestModel: '', scores: [], classification: null, priorities: undefined as never }),
    };
    const closed = serve(
      { embeddingModel: config.embeddingModel, dataDir, router: 'h4' },
      { idleTimeout: 0, router: fake, log: () => {}, onReady: (s) => resolveReady(s) },
    );
    const state = await ready;
    try {
      expect(state.router).toBe('h4');
      expect(await daemon.liveState(config)).not.toBeNull();
      expect(await daemon.liveState({ ...config, router: 'minilm' })).toBeNull();
      // A pre-H4 daemon (no "router") routes with MiniLM: not reused under H4.
      const { router: _r, ...old } = state;
      daemon.writeState(config, old as daemon.DaemonState);
      expect(await daemon.liveState(config)).toBeNull();
      expect(await daemon.liveState({ ...config, router: 'minilm' })).not.toBeNull();
      daemon.writeState(config, state);
    } finally {
      await daemon.stop(config);
      await closed;
    }
  });
});
