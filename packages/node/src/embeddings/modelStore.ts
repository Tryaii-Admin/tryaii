/**
 * Where the int8 ONNX embedding model file comes from (TS-253). Mirrors
 * packages/python/tryaii/embeddings/model_store.py.
 *
 * The file ships in its own npm package, `tryaii-minilm-int8` (the same
 * bytes as the PyPI package of that name), an optional peer dependency next
 * to `onnxruntime-node`: `npm install tryaii` stays small for users who do
 * not monitor. Nothing is downloaded at run time.
 *
 * Resolution: `TRYAII_EMBEDDING_MODEL_PATH` when set (a file, or a directory
 * holding `model.onnx`; then the only source), else the installed package.
 * Every file must match the size and sha256 pinned in `MODELS`.
 *
 * A missing model is reported like Python (SPEC.md §3.1a): the state lives
 * in `<data dir>/embedding-model.json`, the warning is written at most once
 * per machine per local day for the same reason, and `tryaii monitor status`
 * prints `classification: off since <since> (<reason>)` while it exists.
 */

import { createHash } from 'node:crypto';
import { createReadStream, existsSync, readFileSync, statSync, unlinkSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { writeFileAtomic } from '../auth/store.js';

export const ENV_MODEL_PATH = 'TRYAII_EMBEDDING_MODEL_PATH';
export const MODEL_FILE = 'model.onnx';
export const STATE_FILE = 'embedding-model.json';

export interface ModelSpec {
  modelId: string;
  sha256: string;
  sizeBytes: number;
  dimension: number;
  maxLength: number;
  baseModel: string;
  /** npm (and PyPI) package that ships the file. */
  distribution: string;
}

export const MODELS: Readonly<Record<string, ModelSpec>> = {
  'all-MiniLM-L6-v2-onnx-int8': {
    modelId: 'all-MiniLM-L6-v2-onnx-int8',
    sha256: '8199be6f699a395f99f89df973f9dc5285fbcfcdb2ae1a4e1201dab014f3d2a6',
    sizeBytes: 22928619,
    dimension: 384,
    maxLength: 256,
    baseModel: 'all-MiniLM-L6-v2',
    distribution: 'tryaii-minilm-int8',
  },
};

export const DEFAULT_ONNX_MODEL = 'all-MiniLM-L6-v2-onnx-int8';

export function isOnnxModel(modelId: string): boolean {
  return Object.prototype.hasOwnProperty.call(MODELS, modelId);
}

/** The tokenizer.json shipped in this package (dist/embeddings/data/<id>/). */
export function tokenizerPath(modelId: string): string {
  const here = dirname(fileURLToPath(import.meta.url));
  return join(here, 'data', modelId, 'tokenizer.json');
}

export class ModelUnavailableError extends Error {
  readonly modelId: string;
  readonly reason: string;

  constructor(modelId: string, reason?: string) {
    const why = reason ?? lastError() ?? 'not found';
    const spec = MODELS[modelId];
    const fix = spec
      ? `install it (npm install onnxruntime-node ${spec.distribution}) or `
      : '';
    super(`embedding model ${modelId} is not available (${why}); ${fix}set ${ENV_MODEL_PATH} to a copy of the model file`);
    this.name = 'ModelUnavailableError';
    this.modelId = modelId;
    this.reason = why;
  }
}

// -- verification memo -------------------------------------------------------
const verified = new Map<string, boolean>();
let lastErr: string | null = null;

export function lastError(): string | null {
  return lastErr;
}

export async function sha256File(path: string): Promise<string> {
  const hash = createHash('sha256');
  await new Promise<void>((resolve, reject) => {
    createReadStream(path)
      .on('data', (chunk) => hash.update(chunk))
      .on('end', () => resolve())
      .on('error', reject);
  });
  return hash.digest('hex');
}

async function isVerified(path: string, spec: ModelSpec): Promise<boolean> {
  let size: number;
  let mtime: number;
  try {
    const st = statSync(path);
    if (!st.isFile()) return false;
    size = st.size;
    mtime = st.mtimeMs;
  } catch {
    return false;
  }
  const key = `${path}|${size}|${mtime}`;
  const hit = verified.get(key);
  if (hit !== undefined) return hit;
  const ok = size === spec.sizeBytes && (await sha256File(path)) === spec.sha256;
  verified.set(key, ok);
  return ok;
}

function overridePath(raw: string): string {
  try {
    if (statSync(raw).isDirectory()) return join(raw, MODEL_FILE);
  } catch {
    /* a missing path is reported by the verification */
  }
  return raw;
}

/** The installed npm package's model file, or null when not installed. */
export function packageModelPath(spec: ModelSpec): string | null {
  try {
    const require = createRequire(import.meta.url);
    return require.resolve(`${spec.distribution}/${MODEL_FILE}`);
  } catch {
    return null;
  }
}

/** Test seam: replace how the package is located. */
let locatePackage: (spec: ModelSpec) => string | null = packageModelPath;
export function setPackageLocator(fn: ((spec: ModelSpec) => string | null) | null): void {
  locatePackage = fn ?? packageModelPath;
}

/**
 * A verified model file for `modelId`, or null; `lastError()` says why.
 * Never throws for a missing or wrong file.
 */
export async function resolveModelPath(modelId: string): Promise<string | null> {
  const spec = MODELS[modelId];
  if (!spec) {
    lastErr = `unknown model ${modelId}`;
    return null;
  }
  const override = process.env[ENV_MODEL_PATH];
  if (override) {
    const path = overridePath(override);
    if (await isVerified(path, spec)) {
      lastErr = null;
      return path;
    }
    lastErr = `${ENV_MODEL_PATH} points at a missing file or one that does not match`;
    return null;
  }
  const path = locatePackage(spec);
  if (path === null) {
    lastErr = `package ${spec.distribution} is not installed`;
    return null;
  }
  if (!(await isVerified(path, spec))) {
    lastErr = `the installed ${spec.distribution} does not match the expected sha256`;
    return null;
  }
  lastErr = null;
  return path;
}

// -- reporting a missing model (throttled, SPEC.md §3.1a) ---------------------
export interface ModelState {
  model: string;
  reason: string;
  since: string;
  last_warned: string | null;
}

const warnedThisProcess = new Set<string>();

export function statePath(dataDir: string): string {
  return join(dataDir, STATE_FILE);
}

export function readState(dataDir: string): ModelState | null {
  try {
    const doc = JSON.parse(readFileSync(statePath(dataDir), 'utf-8')) as ModelState;
    return doc && typeof doc === 'object' && doc.model ? doc : null;
  } catch {
    return null;
  }
}

function localToday(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/**
 * Record that `modelId` is unavailable and report it through `warn` at most
 * once per machine per local day for the same reason (once per process when
 * the data dir is read-only); `debug` otherwise. Returns true when warned.
 */
export function reportUnavailable(
  modelId: string,
  reason: string,
  opts: { dataDir: string; warn: (msg: string) => void; debug?: (msg: string) => void;
          message?: string; today?: string },
): boolean {
  const today = opts.today ?? localToday();
  const text = opts.message ?? `embedding model ${modelId} is not available (${reason})`;
  const previous = readState(opts.dataDir);
  const same = previous !== null && previous.model === modelId && previous.reason === reason;
  const doc: ModelState = {
    model: modelId,
    reason,
    since: same && previous?.since ? previous.since : nowIso(),
    last_warned: same ? previous?.last_warned ?? null : null,
  };
  let warn = doc.last_warned !== today;
  const key = `${modelId}|${reason}`;
  try {
    if (warn) doc.last_warned = today;
    if (warn || !same) {
      writeFileAtomic(statePath(opts.dataDir), Buffer.from(`${JSON.stringify(doc)}\n`, 'utf-8'),
        '.embedding-model-');
    }
  } catch {
    if (warn) warn = !warnedThisProcess.has(key);
  }
  if (warn) {
    warnedThisProcess.add(key);
    opts.warn(text);
  } else {
    opts.debug?.(text);
  }
  return warn;
}

/** The model is available (again): drop the state file. */
export function clearUnavailable(dataDir: string): boolean {
  const path = statePath(dataDir);
  if (!existsSync(path)) return false;
  try {
    unlinkSync(path);
    return true;
  } catch {
    return false;
  }
}

/** Forget verified files and per-process warnings (tests). */
export function resetModelStoreCache(): void {
  verified.clear();
  warnedThisProcess.clear();
  lastErr = null;
}
