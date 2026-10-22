/**
 * Cross-SDK parity of the in-process ONNX classification (TS-253).
 *
 * shared/embeddings/classification_parity.json freezes the Python SDK's
 * classification of the monitor fixture corpus prompts and the catalog
 * parity prompts (int8 model, its own starter centroids). Node, with the same
 * model file, tokenizer and centroids, must give the same category and
 * subcategory and every similarity within 5e-5 (4 decimals).
 *
 * Needs onnxruntime-node and tryaii-minilm-int8 (dev dependencies here);
 * skips without them, like the Python real-model tests.
 */

import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { beforeAll, describe, expect, it } from 'vitest';

import { starterBundle } from '../src/catalog/bundle.js';
import { DEFAULT_ONNX_MODEL, resolveModelPath } from '../src/embeddings/modelStore.js';
import { OnnxEmbeddingProvider, loadOrt } from '../src/embeddings/onnx.js';
import { Router } from '../src/router.js';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(join(here, '..', '..', '..', 'shared', 'embeddings', 'classification_parity.json'), 'utf-8'),
) as {
  model: string;
  starter_version: string;
  cases: { source: string; text: string; category: string; subcategory: string; similarities: Record<string, number> }[];
};

const available = (await loadOrt()) !== null && (await resolveModelPath(DEFAULT_ONNX_MODEL)) !== null;

describe.skipIf(!available)('in-process ONNX classification matches Python', () => {
  let router: Router;

  beforeAll(async () => {
    router = new Router({
      config: { embeddingModel: DEFAULT_ONNX_MODEL, dataDir: mkdtempSync(join(tmpdir(), 'tryaii-parity-')) },
      embeddingProvider: new OnnxEmbeddingProvider(DEFAULT_ONNX_MODEL),
      bundle: starterBundle(),
      policy: null,
    });
  });

  it('runs on the same starter catalog and model as the fixture', () => {
    expect(fixture.model).toBe(DEFAULT_ONNX_MODEL);
    expect(fixture.starter_version).toBe(starterBundle().version);
    expect(fixture.cases.filter((c) => c.source.startsWith('monitor/')).length).toBeGreaterThan(5);
  });

  for (const c of fixture.cases) {
    it(`${c.source}: ${c.text.slice(0, 40)}`, async () => {
      const result = await router.route(c.text, { topK: 1 });
      const cls = result.classification;
      expect(cls?.broadCategory).toBe(c.category);
      expect(cls?.subcategory).toBe(c.subcategory);
      const got = cls?.benchmarkScores ?? {};
      expect(Object.keys(got).sort()).toEqual(Object.keys(c.similarities).sort());
      for (const [name, value] of Object.entries(c.similarities)) {
        expect(Math.abs((got[name] as number) - value), name).toBeLessThan(5e-5);
      }
    });
  }
});
