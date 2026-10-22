/**
 * TS-253: the Node monitor routes in-process with the int8 ONNX model; the
 * daemon is the fallback for one release; with neither, calls are recorded
 * metadata-only and the reason is reported at most once per machine per day.
 * Plus the model store (resolution, verification, throttled report).
 */

import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as daemon from '../src/daemon.js';
import { classificationLine } from '../src/cli-monitor.js';
import * as modelStore from '../src/embeddings/modelStore.js';
import { loadOrt } from '../src/embeddings/onnx.js';
import { InProcessEngine } from '../src/monitor/engine.js';
import { Transport } from '../src/monitor/transport.js';

vi.mock('../src/catalog/client.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/catalog/client.js')>();
  const { starterBundle } = await import('../src/catalog/bundle.js');
  return {
    ...actual,
    selectCatalog: async () => ({ bundle: starterBundle(), notice: null, checked: false }),
  };
});

const realModel =
  (await loadOrt()) !== null &&
  (await modelStore.resolveModelPath(modelStore.DEFAULT_ONNX_MODEL)) !== null;

let dataDir: string;
let stderr: string[];

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'tryaii-engine-'));
  stderr = [];
  vi.spyOn(process.stderr, 'write').mockImplementation((chunk: unknown) => {
    stderr.push(String(chunk));
    return true;
  });
  vi.spyOn(daemon, 'isDisabled').mockReturnValue(false);
  modelStore.resetModelStoreCache();
});

afterEach(() => {
  vi.restoreAllMocks();
  modelStore.setPackageLocator(null);
  delete process.env[modelStore.ENV_MODEL_PATH];
  rmSync(dataDir, { recursive: true, force: true });
});

function transport(): Transport {
  return new Transport({ embeddingModel: 'x', dataDir } as never);
}

describe.skipIf(!realModel)('in-process engine (real model)', () => {
  it('routes in this process: classification, ranking, embedding; no daemon', async () => {
    const ensure = vi.spyOn(daemon, 'ensureDaemon');
    const t = transport();
    t.start();
    await t.drain(60_000);
    expect(t.inProcess).toBe(true);
    expect(ensure).not.toHaveBeenCalled();
    const out = await t.route('Write a Python function that merges two sorted lists.', {
      includeEmbedding: true,
    });
    expect(out.status).toBe('ok');
    expect((out.classification as { broadCategory: string }).broadCategory).toBe('TECHNICAL');
    expect(out.scores.length).toBeGreaterThan(10);
    expect(out.embedding?.model).toBe('all-MiniLM-L6-v2-onnx-int8');
    expect(out.embedding?.dim).toBe(384);
    const limited = await t.route('Solve 3x + 7 = 22.', {
      includeEmbedding: false,
      allowedModels: ['openai/gpt-5-mini'],
    });
    expect(limited.scores.map((s) => s.modelId)).toEqual(['openai/gpt-5-mini']);
    expect(limited.embedding).toBeNull();
    expect(stderr.join('')).toBe('');
  });
});

describe('without the engine', () => {
  beforeEach(() => {
    vi.spyOn(InProcessEngine, 'create').mockResolvedValue({
      engine: null,
      reason: 'package tryaii-minilm-int8 is not installed',
    });
  });

  it('falls back to a daemon (one release), with no warning', async () => {
    const state = { runtime: 'node', features: [] } as unknown as daemon.DaemonState;
    vi.spyOn(daemon, 'ensureDaemon').mockResolvedValue(state);
    const t = transport();
    t.start();
    await t.drain(5_000);
    expect(t.inProcess).toBe(false);
    expect(stderr.join('')).toBe('');
    expect(classificationLine(dataDir)).toBeNull();
  });

  it('with no daemon either: unavailable, one warning a day, and a status line', async () => {
    vi.spyOn(daemon, 'ensureDaemon').mockResolvedValue(null);
    for (let i = 0; i < 3; i++) {
      modelStore.resetModelStoreCache(); // three process starts
      const t = transport();
      t.start();
      await t.drain(5_000);
      const out = await t.route('hello', { includeEmbedding: false });
      expect(out.status).toBe('unavailable');
    }
    const warnings = stderr.filter((s) => s.includes('is not available'));
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('package tryaii-minilm-int8 is not installed');
    expect(warnings[0]).toContain('npm install onnxruntime-node tryaii-minilm-int8');
    const line = classificationLine(dataDir) as string;
    expect(line).toMatch(/^classification: off since \d{4}-\d{2}-\d{2}T\S+Z \(package tryaii-minilm-int8 is not installed\)$/);
  });

  it('a later process that finds the engine clears the state', async () => {
    vi.spyOn(daemon, 'ensureDaemon').mockResolvedValue(null);
    const t = transport();
    t.start();
    await t.drain(5_000);
    expect(classificationLine(dataDir)).not.toBeNull();
    vi.spyOn(InProcessEngine, 'create').mockResolvedValue({
      engine: {} as InProcessEngine,
      reason: null,
    });
    const t2 = transport();
    t2.start();
    await t2.drain(5_000);
    expect(classificationLine(dataDir)).toBeNull();
  });
});

describe('model store', () => {
  const PAYLOAD = Buffer.from('not really an onnx model, but the right bytes');
  const SPEC: modelStore.ModelSpec = {
    ...modelStore.MODELS[modelStore.DEFAULT_ONNX_MODEL],
    sha256: createHash('sha256').update(PAYLOAD).digest('hex'),
    sizeBytes: PAYLOAD.length,
  };

  beforeEach(() => {
    (modelStore.MODELS as Record<string, modelStore.ModelSpec>)['test-model'] = { ...SPEC, modelId: 'test-model' };
  });
  afterEach(() => {
    delete (modelStore.MODELS as Record<string, modelStore.ModelSpec>)['test-model'];
  });

  it('pins the same file as the Python SDK', () => {
    const spec = modelStore.MODELS[modelStore.DEFAULT_ONNX_MODEL];
    expect(spec.sha256).toBe('8199be6f699a395f99f89df973f9dc5285fbcfcdb2ae1a4e1201dab014f3d2a6');
    expect(spec.sizeBytes).toBe(22_928_619);
    expect(spec.maxLength).toBe(256);
    expect(spec.distribution).toBe('tryaii-minilm-int8');
  });

  it('uses the installed package, verified', async () => {
    const file = join(dataDir, 'model.onnx');
    writeFileSync(file, PAYLOAD);
    modelStore.setPackageLocator(() => file);
    expect(await modelStore.resolveModelPath('test-model')).toBe(file);
    expect(modelStore.lastError()).toBeNull();
    writeFileSync(file, Buffer.concat([PAYLOAD, Buffer.from('x')]));
    modelStore.resetModelStoreCache();
    modelStore.setPackageLocator(() => file);
    expect(await modelStore.resolveModelPath('test-model')).toBeNull();
    expect(modelStore.lastError()).toContain('does not match');
  });

  it('reports a missing package', async () => {
    modelStore.setPackageLocator(() => null);
    expect(await modelStore.resolveModelPath('test-model')).toBeNull();
    expect(modelStore.lastError()).toBe('package tryaii-minilm-int8 is not installed');
  });

  it('TRYAII_EMBEDDING_MODEL_PATH wins (file or directory) and a wrong one never falls back', async () => {
    const good = join(dataDir, 'placed');
    mkdirSync(good);
    writeFileSync(join(good, 'model.onnx'), PAYLOAD);
    const pkg = join(dataDir, 'pkg.onnx');
    writeFileSync(pkg, PAYLOAD);
    modelStore.setPackageLocator(() => pkg);
    process.env[modelStore.ENV_MODEL_PATH] = good;
    expect(await modelStore.resolveModelPath('test-model')).toBe(join(good, 'model.onnx'));
    process.env[modelStore.ENV_MODEL_PATH] = join(dataDir, 'missing.onnx');
    expect(await modelStore.resolveModelPath('test-model')).toBeNull();
    expect(modelStore.lastError()).toContain(modelStore.ENV_MODEL_PATH);
  });

  it('reports at most once a day per reason, again the next day or for a new reason', () => {
    const warned: string[] = [];
    const report = (reason: string, today: string) =>
      modelStore.reportUnavailable('test-model', reason, { dataDir, warn: (m) => warned.push(m), today });
    report('a', '2026-10-20');
    modelStore.resetModelStoreCache();
    report('a', '2026-10-20');
    expect(warned).toHaveLength(1);
    report('a', '2026-10-21');
    expect(warned).toHaveLength(2);
    report('b', '2026-10-21');
    expect(warned).toHaveLength(3);
    expect(modelStore.readState(dataDir)?.reason).toBe('b');
    expect(modelStore.clearUnavailable(dataDir)).toBe(true);
    expect(modelStore.readState(dataDir)).toBeNull();
  });
});
