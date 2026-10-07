/**
 * Catalog bundle loader (contract: docs/catalog/CONTRACT-catalog-v1.md, section 1).
 *
 * A bundle is the complete routing data the engine runs on -- models, the
 * benchmark taxonomy, normalization ranges, classifier centroids and the
 * training queries the centroids were built from. The package ships one bundle
 * (the *starter* catalog, `catalog/data/starter/`); a *full* bundle has the same
 * six files and is loaded the same way.
 *
 * Integrity rules (all enforced by `loadBundle` / `bundleFromTexts`):
 *  - every one of the five data files in `BUNDLE_DATA_FILES` must be present,
 *    and the sha256 of its exact UTF-8 bytes must equal `manifest.files[name]`
 *    -- the bytes are hashed as read, never re-serialized;
 *  - `manifest.schema` must be an integer no greater than `SUPPORTED_SCHEMA`
 *    (a newer schema throws `BundleSchemaError`);
 *  - the benchmark name sets of benchmarks.json, normalization_ranges.json,
 *    centroids.json and training_queries.json must be identical, and the
 *    centroids must be built for `manifest.embedding_model`;
 *  - with `verifySignature` (the full catalog: downloaded or loaded from the
 *    cache) the manifest's Ed25519 signature must verify against a trusted key
 *    (contract section 6, `signing.ts`) -- checked after the schema and before
 *    any data file is hashed or parsed. The packaged starter bundle is not
 *    required to be signed.
 *
 * This module is pure data: it imports nothing from the engine, so scoring /
 * registry / classifier modules can derive their tables from a bundle at import
 * time without import cycles. Mirrors `packages/python/tryaii/catalog/bundle.py`.
 */

import { createHash } from 'node:crypto';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { CentroidsJson, ModelData, ModelsJson, TrainingQueriesJson } from '../types.js';
// Call-time only (import cycle is safe): the catalog client picks the default bundle.
import { selectedBundleSync } from './client.js';
import type { CatalogMode } from './client.js';
import { signatureProblem } from './signing.js';

/** Highest manifest `schema` this SDK understands. */
export const SUPPORTED_SCHEMA = 1;

export const MANIFEST_FILE = 'manifest.json';

/** The five data files of a bundle (`manifest.json` is the sixth). */
export const BUNDLE_DATA_FILES = [
  'models.json',
  'benchmarks.json',
  'normalization_ranges.json',
  'centroids.json',
  'training_queries.json',
] as const;

export type BundleDataFile = (typeof BUNDLE_DATA_FILES)[number];

export const BUNDLE_KINDS = ['starter', 'full'] as const;
export type BundleKind = (typeof BUNDLE_KINDS)[number];

/** Where the packaged starter bundle lives. */
export const STARTER_BUNDLE_DIR = fileURLToPath(new URL('./data/starter/', import.meta.url));

/** A catalog bundle is missing, malformed or inconsistent. */
export class BundleError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BundleError';
  }
}

/** A bundle file is missing or its sha256 does not match the manifest. */
export class BundleIntegrityError extends BundleError {
  constructor(message: string) {
    super(message);
    this.name = 'BundleIntegrityError';
  }
}

/** The bundle's manifest `schema` is newer than this SDK supports. */
export class BundleSchemaError extends BundleError {
  constructor(message: string) {
    super(message);
    this.name = 'BundleSchemaError';
  }
}

/**
 * A full catalog's signature is missing, from an unknown key, or invalid
 * (contract section 6). A `BundleIntegrityError`: handled like a hash mismatch.
 */
export class BundleSignatureError extends BundleIntegrityError {
  constructor(message: string) {
    super(message);
    this.name = 'BundleSignatureError';
  }
}

/**
 * Throw `BundleSignatureError` unless `manifest` carries a valid signature
 * from a trusted key (contract section 6). `env` is where
 * TRYAII_CATALOG_TRUSTED_KEYS is read (default `process.env`). Returns the
 * key_id.
 */
export function verifyManifestSignature(
  manifest: unknown,
  env: NodeJS.ProcessEnv = process.env,
): string {
  const problem = signatureProblem(manifest, env);
  if (problem !== null) throw new BundleSignatureError(problem);
  return String((manifest as Record<string, unknown>).key_id);
}

/** Options of `bundleFromTexts` / `loadBundle`. */
export interface BundleLoadOptions {
  /** Require a valid signature from a trusted key (the full catalog). */
  verifySignature?: boolean;
  /** Where TRYAII_CATALOG_TRUSTED_KEYS is read (default `process.env`). */
  env?: NodeJS.ProcessEnv;
}

/** `manifest.json` (contract section 1). */
export interface BundleManifest {
  schema: number;
  kind: BundleKind;
  version: string;
  embedding_model: string;
  created_at?: string;
  counts?: { models: number; benchmarks: number };
  full_counts?: { models: number; benchmarks: number };
  files: Record<BundleDataFile, string>;
  signature?: string | null;
  key_id?: string | null;
  [key: string]: unknown;
}

/** One `benchmarks.json` entry (Appendix A). */
export interface BenchmarkEntry {
  name: string;
  description: string;
  family: string;
  broad_category: string;
  subcategories: string[];
  weight: number;
  random_chance_floor: number | null;
}

/** `benchmarks.json` (Appendix A). */
export interface BenchmarksJson {
  families: Array<{ id: string; label: string }>;
  benchmarks: BenchmarkEntry[];
}

/** One `normalization_ranges.json` entry. */
export interface RangeEntry {
  lo: number;
  hi: number;
  n_real?: number;
  fallback?: boolean;
  description?: string;
}

/** `normalization_ranges.json`. */
export interface NormalizationRangesJson {
  version?: number;
  generated_from?: string;
  benchmarks: Record<string, RangeEntry>;
}

/** sha256 hex of raw bytes. */
export function sha256Hex(data: Uint8Array): string {
  return createHash('sha256').update(data).digest('hex');
}

/**
 * One loaded, verified catalog bundle.
 *
 * The parsed JSON documents are exposed as-is; the accessor methods derive the
 * per-benchmark tables the engine reads. Treat everything as read-only --
 * registries copy what they need.
 */
export class CatalogBundle {
  readonly manifest: BundleManifest;
  readonly models: ModelsJson;
  readonly benchmarks: BenchmarksJson;
  readonly normalizationRanges: NormalizationRangesJson;
  readonly centroids: CentroidsJson & { metadata: CentroidsJson['metadata'] & { benchmark_fingerprint?: string } };
  readonly trainingQueries: TrainingQueriesJson;
  readonly directory: string | null;

  constructor(parts: {
    manifest: BundleManifest;
    models: ModelsJson;
    benchmarks: BenchmarksJson;
    normalizationRanges: NormalizationRangesJson;
    centroids: CentroidsJson;
    trainingQueries: TrainingQueriesJson;
    directory?: string | null;
  }) {
    this.manifest = parts.manifest;
    this.models = parts.models;
    this.benchmarks = parts.benchmarks;
    this.normalizationRanges = parts.normalizationRanges;
    this.centroids = parts.centroids;
    this.trainingQueries = parts.trainingQueries;
    this.directory = parts.directory ?? null;
  }

  get schema(): number {
    return this.manifest.schema;
  }

  get kind(): BundleKind {
    return this.manifest.kind;
  }

  get version(): string {
    return this.manifest.version;
  }

  get embeddingModel(): string {
    return this.manifest.embedding_model;
  }

  get counts(): { models: number; benchmarks: number } | undefined {
    return this.manifest.counts;
  }

  get fullCounts(): { models: number; benchmarks: number } | undefined {
    return this.manifest.full_counts;
  }

  /** benchmarks.json entries, in display order. */
  get benchmarkEntries(): BenchmarkEntry[] {
    return [...this.benchmarks.benchmarks];
  }

  get benchmarkNames(): string[] {
    return this.benchmarks.benchmarks.map((b) => b.name);
  }

  /** `{name: importance weight}` -- the engine's BENCHMARK_WEIGHTS. */
  benchmarkWeights(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const b of this.benchmarks.benchmarks) out[b.name] = b.weight;
    return out;
  }

  /** `{name: floor}` for benchmarks that declare a random-chance floor. */
  randomChanceFloors(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const b of this.benchmarks.benchmarks) {
      if (b.random_chance_floor !== null && b.random_chance_floor !== undefined) {
        out[b.name] = b.random_chance_floor;
      }
    }
    return out;
  }

  /** `{name: [broadCategory, primary subcategory]}` for the classifier. */
  benchmarkCategories(): Record<string, [string, string]> {
    const out: Record<string, [string, string]> = {};
    for (const b of this.benchmarks.benchmarks) {
      out[b.name] = [b.broad_category, b.subcategories?.[0] ?? 'GENERAL'];
    }
    return out;
  }

  /** normalization_ranges.json `benchmarks` map. */
  rangeEntries(): Record<string, RangeEntry> {
    return { ...this.normalizationRanges.benchmarks };
  }

  /** `{benchmark: [queries]}` from training_queries.json. */
  trainingQueryMap(): Record<string, string[]> {
    const out: Record<string, string[]> = {};
    for (const [name, entry] of Object.entries(this.trainingQueries.benchmarks)) {
      out[name] = [...entry.queries];
    }
    return out;
  }

  /** models.json `models` array (raw entries, catalog order). */
  modelEntries(): ModelData[] {
    return [...(this.models.models ?? [])];
  }
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function parseManifest(manifest: unknown): BundleManifest {
  if (!isPlainObject(manifest)) throw new BundleError('manifest.json must be a JSON object');
  const schema = manifest.schema;
  if (typeof schema !== 'number' || !Number.isInteger(schema) || schema < 1) {
    throw new BundleError(`manifest.json has an invalid schema: ${JSON.stringify(schema)}`);
  }
  if (schema > SUPPORTED_SCHEMA) {
    throw new BundleSchemaError(
      `catalog bundle schema ${schema} is newer than this tryaii version supports ` +
        `(max ${SUPPORTED_SCHEMA})`,
    );
  }
  if (!(BUNDLE_KINDS as readonly unknown[]).includes(manifest.kind)) {
    throw new BundleError(`manifest.json has an invalid kind: ${JSON.stringify(manifest.kind)}`);
  }
  for (const key of ['version', 'embedding_model']) {
    if (typeof manifest[key] !== 'string' || manifest[key] === '') {
      throw new BundleError(`manifest.json is missing '${key}'`);
    }
  }
  const files = manifest.files;
  if (!isPlainObject(files)) throw new BundleError("manifest.json is missing the 'files' map");
  const missing = BUNDLE_DATA_FILES.filter((name) => typeof files[name] !== 'string');
  if (missing.length > 0) {
    throw new BundleError(`manifest.json 'files' is missing: ${missing.join(', ')}`);
  }
  return manifest as unknown as BundleManifest;
}

function sortedDiff(a: Set<string>, b: Set<string>): string[] {
  return [...a].filter((x) => !b.has(x)).sort();
}

function checkConsistency(bundle: CatalogBundle): void {
  const names = bundle.benchmarkNames;
  if (new Set(names).size !== names.length) {
    throw new BundleError('benchmarks.json lists a benchmark name twice');
  }
  const expected = new Set(names);
  const others: Array<[string, Set<string>]> = [
    ['normalization_ranges.json', new Set(Object.keys(bundle.normalizationRanges.benchmarks ?? {}))],
    ['centroids.json', new Set(Object.keys(bundle.centroids.centroids ?? {}))],
    ['training_queries.json', new Set(Object.keys(bundle.trainingQueries.benchmarks ?? {}))],
  ];
  for (const [label, got] of others) {
    const extra = sortedDiff(got, expected);
    const lacking = sortedDiff(expected, got);
    if (extra.length > 0 || lacking.length > 0) {
      throw new BundleError(
        `${label} benchmark set does not match benchmarks.json ` +
          `(only in ${label}: ${JSON.stringify(extra)}, missing: ${JSON.stringify(lacking)})`,
      );
    }
  }
  const model = bundle.centroids.metadata?.model;
  if (model !== bundle.embeddingModel) {
    throw new BundleError(
      `centroids.json was built for ${JSON.stringify(model)}, manifest says ` +
        `${JSON.stringify(bundle.embeddingModel)}`,
    );
  }
  if (!Array.isArray(bundle.models.models)) {
    throw new BundleError("models.json has no 'models' array");
  }
}

/**
 * Verify and parse a bundle given as a manifest plus raw file texts.
 *
 * `files` maps each data file name to its exact text (a string, hashed as
 * UTF-8) or bytes -- the wire format of `GET /v1/catalog/live`; any other
 * value type is rejected. Throws `BundleSchemaError`, `BundleIntegrityError`
 * (`BundleSignatureError` included) or `BundleError` (also for a document
 * whose hash matches but whose shape is wrong). `opts.verifySignature`
 * requires a valid signature from a trusted key.
 */
export function bundleFromTexts(
  manifest: unknown,
  files: Record<string, string | Uint8Array>,
  directory: string | null = null,
  opts: BundleLoadOptions = {},
): CatalogBundle {
  const parsedManifest = parseManifest(manifest);
  if (opts.verifySignature) verifyManifestSignature(parsedManifest, opts.env);
  const parsed: Record<string, unknown> = {};
  for (const name of BUNDLE_DATA_FILES) {
    if (!(name in files)) throw new BundleIntegrityError(`catalog bundle is missing ${name}`);
    const raw = files[name] as unknown;
    if (typeof raw !== 'string' && !(raw instanceof Uint8Array)) {
      throw new BundleError(`${name} must be text or bytes, got ${raw === null ? 'null' : typeof raw}`);
    }
    const data = typeof raw === 'string' ? Buffer.from(raw, 'utf-8') : Buffer.from(raw);
    if (sha256Hex(data) !== parsedManifest.files[name]) {
      throw new BundleIntegrityError(`${name} does not match its manifest sha256`);
    }
    try {
      parsed[name] = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(data));
    } catch (err) {
      throw new BundleError(`${name} is not valid UTF-8 JSON: ${(err as Error).message}`);
    }
  }
  const bundle = new CatalogBundle({
    manifest: parsedManifest,
    models: parsed['models.json'] as ModelsJson,
    benchmarks: parsed['benchmarks.json'] as BenchmarksJson,
    normalizationRanges: parsed['normalization_ranges.json'] as NormalizationRangesJson,
    centroids: parsed['centroids.json'] as CentroidsJson,
    trainingQueries: parsed['training_queries.json'] as TrainingQueriesJson,
    directory,
  });
  try {
    checkConsistency(bundle);
  } catch (err) {
    if (err instanceof BundleError) throw err;
    // Matching hashes but a malformed document (e.g. benchmarks.json "{}"):
    // the accessors hit a TypeError. Same rule as the Python SDK.
    throw new BundleError(`catalog bundle is malformed: ${String(err)}`);
  }
  return bundle;
}

function isFile(path: string): boolean {
  try {
    return existsSync(path) && statSync(path).isFile();
  } catch {
    return false;
  }
}

/**
 * Load and verify the bundle in `directory` (the six files of section 1).
 * `opts.verifySignature` additionally requires a trusted signature (contract
 * section 6) -- used for the cached full catalog.
 */
export function loadBundle(directory: string, opts: BundleLoadOptions = {}): CatalogBundle {
  const manifestPath = join(directory, MANIFEST_FILE);
  if (!isFile(manifestPath)) {
    throw new BundleIntegrityError(`no catalog bundle at ${directory} (manifest.json missing)`);
  }
  let manifest: unknown;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, 'utf-8'));
  } catch (err) {
    throw new BundleError(`manifest.json is not valid JSON: ${(err as Error).message}`);
  }
  parseManifest(manifest); // schema before touching the data files
  if (opts.verifySignature) verifyManifestSignature(manifest, opts.env);
  const files: Record<string, Uint8Array> = {};
  for (const name of BUNDLE_DATA_FILES) {
    const path = join(directory, name);
    if (!isFile(path)) {
      throw new BundleIntegrityError(`catalog bundle at ${directory} is missing ${name}`);
    }
    files[name] = readFileSync(path);
  }
  // The signature (if requested) was checked above, before reading the files.
  return bundleFromTexts(manifest, files, directory);
}

let starter: CatalogBundle | null = null;

/** The starter bundle shipped in the package (loaded once per process). */
export function starterBundle(): CatalogBundle {
  if (starter === null) starter = loadBundle(STARTER_BUNDLE_DIR);
  return starter;
}

/** A bundle object or a bundle directory path. */
export type BundleLike = CatalogBundle | string;

/**
 * THE seam every default-data consumer goes through.
 *
 *  - a `CatalogBundle` is returned as-is;
 *  - a string is a bundle directory, loaded with `loadBundle`;
 *  - `undefined`/`null` means "the default catalog", chosen by `catalog`
 *    (contract section 5): 'starter' = the packaged starter bundle; 'auto'
 *    (default) = the full catalog when logged in, else the starter; 'full' =
 *    like auto but throws LoginRequiredError when not logged in. This path
 *    is sync, so it uses the selection already made in this process (by
 *    `Router.route()` / `Router.ready()` / the CLI) or, before that, the
 *    local cache only -- see catalog/client.ts.
 */
export function resolveBundle(
  bundle?: BundleLike | null,
  catalog: CatalogMode = 'auto',
): CatalogBundle {
  if (bundle === undefined || bundle === null) return selectedBundleSync(catalog);
  if (bundle instanceof CatalogBundle) return bundle;
  if (typeof bundle === 'string') return loadBundle(bundle);
  throw new TypeError('bundle must be a CatalogBundle or a directory path');
}
