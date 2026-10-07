/**
 * Catalog bundles (Node side): loader integrity, the resolveBundle seam, and
 * the full-catalog routing regression.
 *
 * Contract: docs/catalog/CONTRACT-catalog-v1.md (section 1, Appendix A).
 * Mirrors packages/python/tests/test_catalog_bundle.py; the cross-SDK engine
 * parity check lives there (it drives this package's dist build).
 */

import { createHash } from 'node:crypto';
import {
  copyFileSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { BenchmarkRegistry } from '../src/benchmarks/registry.js';
import {
  BUNDLE_DATA_FILES,
  BundleError,
  BundleIntegrityError,
  BundleSchemaError,
  CatalogBundle,
  STARTER_BUNDLE_DIR,
  SUPPORTED_SCHEMA,
  bundleFromTexts,
  loadBundle,
  resolveBundle,
  starterBundle,
} from '../src/catalog/bundle.js';
import { ModelRegistry } from '../src/registry/models.js';
import { Router } from '../src/router.js';
import { ScoringEngine } from '../src/scoring/engine.js';
import { Priorities } from '../src/scoring/priorities.js';
import { FULL_ONLY, HAS_FULL_BUNDLE, REPO_ROOT, fullBundle } from './_catalog.js';

const tempDirs: string[] = [];

function bundleCopy(): string {
  const dir = mkdtempSync(join(tmpdir(), 'tryaii-bundle-'));
  tempDirs.push(dir);
  for (const name of readdirSync(STARTER_BUNDLE_DIR)) {
    copyFileSync(join(STARTER_BUNDLE_DIR, name), join(dir, name));
  }
  return dir;
}

afterEach(() => {
  while (tempDirs.length > 0) rmSync(tempDirs.pop()!, { recursive: true, force: true });
});

const readManifest = (dir: string) => JSON.parse(readFileSync(join(dir, 'manifest.json'), 'utf-8'));
const writeManifest = (dir: string, m: unknown) =>
  writeFileSync(join(dir, 'manifest.json'), JSON.stringify(m));

describe('catalog bundle loader', () => {
  it('loads the packaged starter bundle', () => {
    const bundle = starterBundle();
    expect(bundle).toBeInstanceOf(CatalogBundle);
    expect(bundle.kind).toBe('starter');
    expect(bundle.schema).toBe(SUPPORTED_SCHEMA);
    expect(bundle.embeddingModel).toBe('all-MiniLM-L6-v2');
    expect(bundle.counts).toEqual({ models: 45, benchmarks: 16 });
    expect(bundle.fullCounts!.models).toBeGreaterThan(45);
    expect(bundle.modelEntries()).toHaveLength(45);
    expect(bundle.benchmarkNames).toHaveLength(16);
  });

  it('manifest hashes are the sha256 of the exact file bytes', () => {
    const manifest = readManifest(STARTER_BUNDLE_DIR);
    for (const name of BUNDLE_DATA_FILES) {
      const digest = createHash('sha256')
        .update(readFileSync(join(STARTER_BUNDLE_DIR, name)))
        .digest('hex');
      expect(manifest.files[name], name).toBe(digest);
    }
  });

  it('refuses a tampered file', () => {
    const dir = bundleCopy();
    const path = join(dir, 'models.json');
    writeFileSync(path, readFileSync(path, 'utf-8').replace('"openai/gpt-4o"', '"openai/gpt-4x"'));
    expect(() => loadBundle(dir)).toThrow(BundleIntegrityError);
    expect(() => loadBundle(dir)).toThrow(/models\.json does not match/);
  });

  it('refuses re-serialized JSON even when the data is equal', () => {
    const dir = bundleCopy();
    const path = join(dir, 'benchmarks.json');
    writeFileSync(path, JSON.stringify(JSON.parse(readFileSync(path, 'utf-8')), null, 2));
    expect(() => loadBundle(dir)).toThrow(BundleIntegrityError);
  });

  it('refuses a wrong manifest hash, a missing file and a missing manifest', () => {
    const dir = bundleCopy();
    const manifest = readManifest(dir);
    manifest.files['centroids.json'] = '0'.repeat(64);
    writeManifest(dir, manifest);
    expect(() => loadBundle(dir)).toThrow(/centroids\.json/);

    const dir2 = bundleCopy();
    unlinkSync(join(dir2, 'training_queries.json'));
    expect(() => loadBundle(dir2)).toThrow(BundleIntegrityError);

    const dir3 = bundleCopy();
    unlinkSync(join(dir3, 'manifest.json'));
    expect(() => loadBundle(dir3)).toThrow(/manifest\.json missing/);
  });

  it('refuses a newer schema before reading the data files', () => {
    const dir = bundleCopy();
    const manifest = readManifest(dir);
    manifest.schema = SUPPORTED_SCHEMA + 1;
    writeManifest(dir, manifest);
    unlinkSync(join(dir, 'models.json'));
    expect(() => loadBundle(dir)).toThrow(BundleSchemaError);
    expect(() => loadBundle(dir)).toThrow(/schema 2 is newer/);
    expect(new BundleSchemaError('x')).toBeInstanceOf(BundleError);
  });

  it.each([0, -1, '1', 1.5, null, true])('refuses invalid schema %s', (schema) => {
    const dir = bundleCopy();
    const manifest = readManifest(dir);
    manifest.schema = schema;
    writeManifest(dir, manifest);
    expect(() => loadBundle(dir)).toThrow(/invalid schema/);
  });

  it('refuses invalid kind / version / embedding model and missing file entries', () => {
    for (const [field, value] of [['kind', 'premium'], ['version', ''], ['embedding_model', null]]) {
      const dir = bundleCopy();
      const manifest = readManifest(dir);
      manifest[field as string] = value;
      writeManifest(dir, manifest);
      expect(() => loadBundle(dir), String(field)).toThrow(BundleError);
    }
    const dir = bundleCopy();
    const manifest = readManifest(dir);
    delete manifest.files['normalization_ranges.json'];
    writeManifest(dir, manifest);
    expect(() => loadBundle(dir)).toThrow(/normalization_ranges\.json/);
  });

  it('refuses re-hashed but inconsistent benchmark sets', () => {
    const dir = bundleCopy();
    const path = join(dir, 'normalization_ranges.json');
    const data = JSON.parse(readFileSync(path, 'utf-8'));
    data.benchmarks['Imaginary-Bench'] = { lo: 0, hi: 1, description: '' };
    // Key order does not matter for this check; only the hash must match the text.
    const text = JSON.stringify(data);
    writeFileSync(path, text);
    const manifest = readManifest(dir);
    manifest.files['normalization_ranges.json'] = createHash('sha256').update(text).digest('hex');
    writeManifest(dir, manifest);
    expect(() => loadBundle(dir)).toThrow(/Imaginary-Bench/);
  });

  it('bundleFromTexts accepts the wire format', () => {
    const manifest = readManifest(STARTER_BUNDLE_DIR);
    const files: Record<string, string> = {};
    for (const name of BUNDLE_DATA_FILES) {
      files[name] = readFileSync(join(STARTER_BUNDLE_DIR, name), 'utf-8');
    }
    const bundle = bundleFromTexts(manifest, files);
    expect(bundle.version).toBe(starterBundle().version);
    expect(() =>
      bundleFromTexts(manifest, { ...files, 'models.json': `${files['models.json']} ` }),
    ).toThrow(BundleIntegrityError);
    const { ['benchmarks.json']: _dropped, ...partial } = files;
    expect(() => bundleFromTexts(manifest, partial)).toThrow(/missing/);
  });

  it('python and node read the same starter bundle', () => {
    for (const name of [...BUNDLE_DATA_FILES, 'manifest.json']) {
      const shared = readFileSync(join(REPO_ROOT, 'shared', 'catalog', 'starter', name));
      expect(readFileSync(join(STARTER_BUNDLE_DIR, name)).equals(shared), name).toBe(true);
    }
  });
});

describe('resolveBundle seam', () => {
  it('defaults to the packaged starter and passes bundles through', () => {
    expect(resolveBundle()).toBe(starterBundle());
    expect(resolveBundle(null)).toBe(starterBundle());
    const b = starterBundle();
    expect(resolveBundle(b)).toBe(b);
  });

  it('loads a path', () => {
    const dir = bundleCopy();
    const bundle = resolveBundle(dir);
    expect(bundle.directory).toBe(dir);
    expect(bundle.version).toBe(starterBundle().version);
  });

  it('the Router takes all of its data from the bundle', () => {
    const dir = bundleCopy();
    const router = new Router({ bundle: dir });
    expect(router.bundle.directory).toBe(dir);
    expect(router.models.length).toBe(45);
    expect(router.benchmarks.names).toEqual(starterBundle().benchmarkNames);
    expect(new Router().bundle).toBe(starterBundle());
    expect(ModelRegistry.default().length).toBe(45);
    expect(BenchmarkRegistry.default().names).toEqual(starterBundle().benchmarkNames);
  });
});

describe.skipIf(!HAS_FULL_BUNDLE)(`full catalog bundle ${FULL_ONLY}`, () => {
  it('routes the full catalog through the Router', () => {
    const router = new Router({ bundle: fullBundle() });
    expect(router.bundle.kind).toBe('full');
    expect(router.models.length).toBe(322);
    expect(router.benchmarks.length).toBe(33);
    expect(router.benchmarks.getNormalizer().getWeight('SWE-bench-verified')).toBe(
      fullBundle().benchmarkWeights()['SWE-bench-verified'],
    );
  });

  const BASELINE_PATH = join(REPO_ROOT, 'shared', 'catalog', 'parity', 'full_baseline_routes.json');

  it.skipIf(!existsSync(BASELINE_PATH))('reproduces the routing of the pre-bundle engine', () => {
    const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf-8')) as {
      n_routable_models: number;
      prompts: Record<string, { similarities: Record<string, number> }>;
      routes: Array<{ prompt: string; priorities: [number, number, number]; top: string[]; final: number[] }>;
    };
    const bundle = fullBundle();
    const registry = ModelRegistry.fromBundle(bundle);
    expect(registry.length).toBe(baseline.n_routable_models);
    const engine = new ScoringEngine(BenchmarkRegistry.fromBundle(bundle).getNormalizer());
    const coverage = registry.benchmarkCoverage();
    const mismatches: unknown[] = [];
    for (const route of baseline.routes) {
      const [q, c, s] = route.priorities;
      const scores = engine.scoreModels(
        registry.allModels,
        baseline.prompts[route.prompt].similarities,
        new Priorities(q, c, s),
        5,
        coverage,
      );
      const got = scores.map((x) => x.modelId);
      if (JSON.stringify(got) !== JSON.stringify(route.top)) {
        mismatches.push([route.prompt, route.priorities, route.top, got]);
        continue;
      }
      expect(scores.map((x) => x.finalScore)).toEqual(route.final);
    }
    expect(mismatches).toEqual([]);
  });
});
