/**
 * Catalog client: which catalog to route on, and keeping the full one fresh.
 *
 * Contract: docs/catalog/CONTRACT-catalog-v1.md, section 5 (sections 1 and 4
 * for the bundle and wire formats). Mirrors
 * `packages/python/tryaii/catalog/client.py`.
 *
 * Cache layout under the data dir (TRYAII_DRE_DATA_DIR or ~/.tryaii):
 *
 *   catalog/full/<version>/   the six bundle files exactly as received
 *   catalog/state.json        {"version": "<live version or null>", "checked_at": "<ISO Z>"}
 *   catalog/nudge.json        {"last_shown": "<YYYY-MM-DD local date>"}
 *
 * Every write goes through the credentials file's atomic helper
 * (`writeFileAtomic`); only the newest version directory is kept.
 * `state.version` is null after a 403 (the account has no access to the full
 * catalog), which is how a run inside the 24 h window knows to say so without
 * asking the server again. Failures (network, 5xx, 429, 404, hash, signature
 * or schema problems) never touch state.json -- the next use retries. A full
 * catalog is accepted only with a valid signature from a trusted key
 * (contract section 6, `signing.ts`), both when it is downloaded (before the
 * cache is written) and when the cache is loaded.
 *
 * The library never prints: `selectCatalog` returns a `CatalogSelection`
 * whose `notice` tells the CLI what to print (`noticeMessage`, `maybeNudge`).
 *
 * The network check is async (fetch); `resolveBundle` and the Router
 * constructor are sync, so they start from `selectCatalogOffline` (local
 * cache only) and `Router.route()` / `Router.ready()` complete the check.
 */

import { existsSync, readdirSync, readFileSync, rmSync, statSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';

import type { AuthDeps } from '../auth/flow.js';
import { currentSession, ensureAccessToken } from '../auth/flow.js';
import {
  dataDir,
  deleteCredentials,
  isoUtc,
  loadCredentials,
  renameWithRetry,
  writeFileAtomic,
} from '../auth/store.js';
import { AuthProtocolError, TIMEOUT_MS } from '../auth/transport.js';
import {
  BUNDLE_DATA_FILES,
  BundleSchemaError,
  CatalogBundle,
  MANIFEST_FILE,
  bundleFromTexts,
  loadBundle,
  starterBundle,
} from './bundle.js';
import type { BundleManifest } from './bundle.js';

export const CATALOG_DIR = 'catalog';
export const FULL_DIR = 'full';
export const STATE_FILE = 'state.json';
export const NUDGE_FILE = 'nudge.json';
export const LIVE_PATH = '/v1/catalog/live';
/** How often auto / full ask the server (contract section 5). */
export const CHECK_INTERVAL_MS = 24 * 3600 * 1000;
/**
 * How long the library memo keeps a selection whose check FAILED (network,
 * 5xx, 429, 404, hash / shape / schema problems) before the next use retries:
 * short enough that a process started during a network blip soon moves to the
 * full catalog, long enough not to hammer the server on every Router.
 */
export const FAILURE_RETRY_MS = 60 * 1000;
export const CATALOG_MODES = ['auto', 'starter', 'full'] as const;
export type CatalogMode = (typeof CATALOG_MODES)[number];
const FREE_SUFFIX = ':free';
const VERSION_RE = /^\d{4}\.\d{2}\.\d{2}\.\d{1,6}$/;
const ISO_Z_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

// --- user-facing text (byte-identical to the Python SDK) -------------------
export const MSG_SESSION_ENDED = 'Your session has ended. Run: tryaii login';
export const MSG_LOGIN_REQUIRED = 'Not logged in. Run: tryaii login';
export const MSG_NO_ENTITLEMENT =
  'This account does not have access to the full catalog; using the starter catalog.';
export const MSG_DOWNLOAD_FAILED =
  'Could not download the full catalog; using the starter catalog for now.';
export const MSG_SCHEMA_TOO_NEW =
  'The full catalog needs a newer tryaii version; using the starter catalog.';
export const MSG_LOGIN_DOWNLOAD_FAILED =
  'Could not download the full catalog now; it will be fetched on next use.';

/** Why a selection landed on the starter catalog (null = nothing to say). */
export type CatalogNotice = 'not_logged_in' | 'no_entitlement' | 'download_failed' | 'schema_too_new';

const NOTICE_MESSAGES: Record<string, string> = {
  no_entitlement: MSG_NO_ENTITLEMENT,
  download_failed: MSG_DOWNLOAD_FAILED,
  schema_too_new: MSG_SCHEMA_TOO_NEW,
};

/** Base class of the catalog client's errors. */
export class CatalogError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CatalogError';
  }
}

/**
 * The refresh token was rejected (invalid_grant) or the catalog endpoint
 * answered 401. The credentials file and the full cache have been deleted.
 * Same message as the CLI.
 */
export class SessionEndedError extends CatalogError {
  constructor(message: string = MSG_SESSION_ENDED) {
    super(message);
    this.name = 'SessionEndedError';
  }
}

/** catalog: 'full' without a stored session. Same message as the CLI. */
export class LoginRequiredError extends CatalogError {
  constructor(message: string = MSG_LOGIN_REQUIRED) {
    super(message);
    this.name = 'LoginRequiredError';
  }
}

/** The catalog to route on plus what (if anything) the CLI should say. */
export interface CatalogSelection {
  bundle: CatalogBundle;
  notice: CatalogNotice | null;
  /** True when this selection contacted the server. */
  checked: boolean;
  /**
   * True when the server check failed (network, 5xx, 429, 404, a bad or
   * too-new bundle) and the selection fell back (to the cached full catalog
   * or the starter). The library memo retries these soon. Absent otherwise.
   */
  failed?: boolean;
}

/** Seams for tests: fetch, clock, environment (all optional). */
export type CatalogDeps = Partial<AuthDeps>;

export function noticeMessage(notice: CatalogNotice | null | undefined): string | null {
  return notice ? NOTICE_MESSAGES[notice] ?? null : null;
}

/**
 * User-facing model count: entries whose id is not a `:free` variant
 * (contract section 1, "User-facing model counts").
 */
export function routableCount(bundle: CatalogBundle): number {
  return bundle.modelEntries().filter((m) => !String(m.model_id ?? '').endsWith(FREE_SUFFIX))
    .length;
}

export function assertCatalogMode(catalog: unknown): asserts catalog is CatalogMode {
  if (!(CATALOG_MODES as readonly unknown[]).includes(catalog)) {
    throw new Error(`catalog must be one of ${CATALOG_MODES.join(', ')}; got ${String(catalog)}`);
  }
}

// ------------------------------------------------------------------ deps
let packageVersion: string | null = null;

function sdkVersion(): string {
  if (packageVersion === null) {
    try {
      const pkg = JSON.parse(
        readFileSync(new URL('../../package.json', import.meta.url), 'utf-8'),
      ) as { version?: string };
      packageVersion = pkg.version ?? '0.0.0';
    } catch {
      packageVersion = '0.0.0';
    }
  }
  return packageVersion;
}

function envOf(deps: CatalogDeps): NodeJS.ProcessEnv {
  return deps.env ?? process.env;
}

function nowOf(deps: CatalogDeps): number {
  return deps.now ? deps.now() : Date.now();
}

function authDeps(deps: CatalogDeps): AuthDeps {
  return { ...deps, version: deps.version ?? sdkVersion() };
}

// ------------------------------------------------------------------ paths
export function catalogDir(env: NodeJS.ProcessEnv = process.env): string {
  return join(dataDir(env), CATALOG_DIR);
}

export function fullDir(env: NodeJS.ProcessEnv = process.env): string {
  return join(catalogDir(env), FULL_DIR);
}

export function statePath(env: NodeJS.ProcessEnv = process.env): string {
  return join(catalogDir(env), STATE_FILE);
}

export function nudgePath(env: NodeJS.ProcessEnv = process.env): string {
  return join(catalogDir(env), NUDGE_FILE);
}

// ------------------------------------------------------------------ state
export interface CatalogState {
  version?: string | null;
  checked_at?: string;
}

export function readState(env: NodeJS.ProcessEnv = process.env): CatalogState | null {
  try {
    const data = JSON.parse(readFileSync(statePath(env), 'utf-8')) as unknown;
    return data !== null && typeof data === 'object' && !Array.isArray(data)
      ? (data as CatalogState)
      : null;
  } catch {
    return null;
  }
}

/** Same bytes as the Python SDK's json.dumps of the same object. */
export function writeState(
  version: string | null,
  checkedAtMs: number,
  env: NodeJS.ProcessEnv = process.env,
): void {
  const text =
    `{"version": ${JSON.stringify(version)}, ` +
    `"checked_at": ${JSON.stringify(isoUtc(checkedAtMs))}}`;
  writeFileAtomic(statePath(env), Buffer.from(text, 'utf-8'), '.state-');
}

function parseIsoZ(value: unknown): number | null {
  if (typeof value !== 'string' || !ISO_Z_RE.test(value)) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

function checkDue(state: CatalogState | null, nowMs: number): boolean {
  if (!state) return true;
  const checked = parseIsoZ(state.checked_at);
  if (checked === null) return true;
  const age = nowMs - checked;
  // A checked_at in the future (clock skew, hand edits) counts as stale.
  return !(age >= 0 && age < CHECK_INTERVAL_MS);
}

/**
 * User centroid caches derived from a full catalog
 * (`centroids/centroids_<model>__full-<version>.json`, see centroidFilePath).
 */
const FULL_CENTROID_RE = /^centroids_.*__full-.*\.json$/;

/**
 * Delete catalog/full/, catalog/state.json and the user centroid caches
 * derived from the full catalog (centroids/*__full-*.json) -- logout, session
 * ended. nudge.json and the starter's centroid caches are kept. Never throws.
 */
export function clearCache(env: NodeJS.ProcessEnv = process.env): void {
  try {
    rmSync(fullDir(env), { recursive: true, force: true });
  } catch {
    // best effort
  }
  try {
    unlinkSync(statePath(env));
  } catch {
    // absent
  }
  const centroids = join(dataDir(env), 'centroids');
  let derived: string[] = [];
  try {
    derived = readdirSync(centroids).filter((name) => FULL_CENTROID_RE.test(name));
  } catch {
    // no centroid caches
  }
  for (const name of derived) {
    try {
      unlinkSync(join(centroids, name));
    } catch {
      // best effort
    }
  }
  forgetSelections();
}

// ------------------------------------------------------------------ cache
function versionKey(name: string): number[] {
  return name.split('.').map((p) => Number(p));
}

function compareVersions(a: string, b: string): number {
  const ka = versionKey(a);
  const kb = versionKey(b);
  for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
    const d = (ka[i] ?? 0) - (kb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

const loaded = new Map<string, CatalogBundle>();

/**
 * Load (and verify) one cached version directory; memoized per path. The
 * cached manifest keeps its signature and is re-verified here (contract
 * section 6): an unsigned or tampered cache is treated as absent.
 */
function loadVersionDir(path: string, name: string, env: NodeJS.ProcessEnv): CatalogBundle | null {
  const hit = loaded.get(path);
  if (hit) return hit;
  let bundle: CatalogBundle;
  try {
    bundle = loadBundle(path, { verifySignature: true, env });
  } catch {
    return null;
  }
  if (bundle.kind !== 'full' || bundle.version !== name) return null;
  loaded.set(path, bundle);
  return bundle;
}

function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/**
 * The cached full bundle (the state's version first, else the newest valid
 * version directory), or null.
 */
export function cachedFull(
  state: CatalogState | null = null,
  env: NodeJS.ProcessEnv = process.env,
): CatalogBundle | null {
  const base = fullDir(env);
  let names: string[];
  try {
    names = readdirSync(base).filter((n) => VERSION_RE.test(n) && isDir(join(base, n)));
  } catch {
    return null;
  }
  const ordered = [...names].sort((a, b) => compareVersions(b, a));
  const preferred = state?.version;
  if (typeof preferred === 'string' && names.includes(preferred)) {
    ordered.splice(ordered.indexOf(preferred), 1);
    ordered.unshift(preferred);
  }
  for (const name of ordered) {
    const bundle = loadVersionDir(join(base, name), name, env);
    if (bundle) return bundle;
  }
  return null;
}

/**
 * How a received manifest is written: as received (key order kept),
 * 2-space indented, trailing newline (same bytes in both SDKs).
 */
export function manifestText(manifest: unknown): string {
  return JSON.stringify(manifest, null, 2) + '\n';
}

/**
 * Write a verified bundle to catalog/full/<version>/ (temp dir + rename; each
 * file through the atomic helper), drop every other version directory, and
 * return the bundle bound to its directory.
 */
export function saveFull(
  manifest: unknown,
  files: Record<string, string>,
  bundle: CatalogBundle,
  env: NodeJS.ProcessEnv = process.env,
): CatalogBundle {
  const version = bundle.version;
  const base = fullDir(env);
  const tmp = join(base, `.tmp-${Math.random().toString(16).slice(2)}${Date.now().toString(16)}`);
  const target = join(base, version);
  try {
    for (const name of BUNDLE_DATA_FILES) {
      writeFileAtomic(join(tmp, name), Buffer.from(files[name], 'utf-8'));
    }
    writeFileAtomic(join(tmp, MANIFEST_FILE), Buffer.from(manifestText(manifest), 'utf-8'));
    if (existsSync(target)) rmSync(target, { recursive: true, force: true });
    renameWithRetry(tmp, target);
  } catch (error) {
    rmSync(tmp, { recursive: true, force: true });
    throw error;
  }
  for (const entry of readdirSync(base)) {
    if (entry !== version) rmSync(join(base, entry), { recursive: true, force: true });
  }
  loaded.clear();
  const placed = new CatalogBundle({
    manifest: bundle.manifest,
    models: bundle.models,
    benchmarks: bundle.benchmarks,
    normalizationRanges: bundle.normalizationRanges,
    centroids: bundle.centroids,
    trainingQueries: bundle.trainingQueries,
    directory: target,
  });
  loaded.set(target, placed);
  return placed;
}

// ------------------------------------------------------------------ transport
/** Network failure, timeout, unexpected status or body. */
class FetchFailure extends Error {}

/**
 * GET /v1/catalog/live: one attempt, 10 s timeout, no redirects (auth-v1
 * transport). Returns status + body text (fetch decompresses gzip).
 */
export async function fetchLive(
  apiUrl: string,
  accessToken: string,
  etag: string | null,
  deps: CatalogDeps = {},
): Promise<{ status: number; text: string }> {
  const fetchFn = deps.fetchFn ?? fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), deps.timeoutMs ?? TIMEOUT_MS);
  const headers: Record<string, string> = {
    'Accept': 'application/json',
    'Accept-Encoding': 'gzip',
    'User-Agent': `tryaii/${deps.version ?? sdkVersion()}`,
    'Authorization': `Bearer ${accessToken}`,
    // No 'Connection: close' here (unlike the auth calls): Node 20's fetch
    // fails a gzip body with "terminated" when the server closes the socket
    // right after it. This is a single request, never a reused idle socket.
  };
  if (etag) headers['If-None-Match'] = `"${etag}"`;
  try {
    const response = await fetchFn(apiUrl.replace(/\/+$/, '') + LIVE_PATH, {
      method: 'GET',
      headers,
      signal: controller.signal,
      redirect: 'manual',
    });
    const text = await response.text();
    return { status: response.status, text };
  } catch {
    throw new FetchFailure('network');
  } finally {
    clearTimeout(timer);
  }
}

function jsonObject(text: string): Record<string, unknown> | null {
  try {
    const data = JSON.parse(text) as unknown;
    return data !== null && typeof data === 'object' && !Array.isArray(data)
      ? (data as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------ the check
type Outcome = 'ok' | 'not_modified' | 'session_ended' | 'forbidden' | 'failed' | 'schema_too_new';

/** Session ended: delete the credentials file and the full cache. */
export function endSession(env: NodeJS.ProcessEnv = process.env): void {
  try {
    deleteCredentials(env);
  } catch {
    // the session is over either way
  }
  clearCache(env);
}

function tryWriteState(version: string | null, nowMs: number, env: NodeJS.ProcessEnv): void {
  try {
    writeState(version, nowMs, env);
  } catch {
    // the next use checks again
  }
}

/**
 * Refresh if needed, then GET /v1/catalog/live with the cached version as
 * If-None-Match. Persists the rotated session, the new bundle and
 * state.json.
 */
async function runCheck(
  cached: CatalogBundle | null,
  deps: CatalogDeps,
): Promise<{ outcome: Outcome; bundle: CatalogBundle | null }> {
  const env = envOf(deps);
  const full = authDeps(deps);
  const session = currentSession(full);
  if (session === null) return { outcome: 'failed', bundle: null };
  let accessToken: string;
  try {
    accessToken = await ensureAccessToken(session, full);
  } catch (error) {
    if (error instanceof AuthProtocolError && error.code === 'invalid_grant') {
      return { outcome: 'session_ended', bundle: null };
    }
    return { outcome: 'failed', bundle: null };
  }
  let status: number;
  let text: string;
  try {
    ({ status, text } = await fetchLive(session.apiUrl, accessToken, cached?.version ?? null, full));
  } catch {
    return { outcome: 'failed', bundle: null };
  }
  const now = nowOf(deps);

  if (status === 304) {
    if (cached === null) return { outcome: 'failed', bundle: null };
    tryWriteState(cached.version, now, env);
    return { outcome: 'not_modified', bundle: cached };
  }
  if (status === 401) return { outcome: 'session_ended', bundle: null };
  if (status === 403) {
    if (jsonObject(text)?.error === 'insufficient_entitlement') {
      // No access: remember it for 24 h and drop any old full cache.
      rmSync(fullDir(env), { recursive: true, force: true });
      tryWriteState(null, now, env);
      return { outcome: 'forbidden', bundle: null };
    }
    return { outcome: 'failed', bundle: null };
  }
  if (status !== 200) return { outcome: 'failed', bundle: null };

  const body = jsonObject(text);
  const manifest = body?.manifest;
  const files = body?.files;
  if (
    manifest === null || typeof manifest !== 'object' || Array.isArray(manifest) ||
    files === null || typeof files !== 'object' || Array.isArray(files)
  ) {
    return { outcome: 'failed', bundle: null };
  }
  const fileTexts = files as Record<string, unknown>;
  for (const name of BUNDLE_DATA_FILES) {
    if (name in fileTexts && typeof fileTexts[name] !== 'string') {
      return { outcome: 'failed', bundle: null };
    }
  }
  let bundle: CatalogBundle;
  try {
    // Signature first (contract section 6), before anything is written to the
    // cache: missing / unknown key / bad signature = a hash mismatch.
    bundle = bundleFromTexts(manifest as BundleManifest, fileTexts as Record<string, string>, null, {
      verifySignature: true,
      env,
    });
  } catch (error) {
    if (error instanceof BundleSchemaError) return { outcome: 'schema_too_new', bundle: null };
    // Any other problem (hash, shape, ...) is a failed download, never a crash.
    return { outcome: 'failed', bundle: null };
  }
  if (bundle.kind !== 'full' || !VERSION_RE.test(bundle.version)) {
    return { outcome: 'failed', bundle: null };
  }
  try {
    bundle = saveFull(manifest, fileTexts as Record<string, string>, bundle, env);
  } catch {
    // Use it for this run; the next use downloads again.
    return { outcome: 'ok', bundle };
  }
  tryWriteState(bundle.version, now, env);
  return { outcome: 'ok', bundle };
}

/**
 * The selection made from local files only (no network): what the sync
 * paths (`resolveBundle`, the Router constructor) start from. Throws
 * LoginRequiredError for catalog 'full' without a session.
 */
export function selectCatalogOffline(
  catalog: CatalogMode = 'auto',
  deps: CatalogDeps = {},
): CatalogSelection {
  assertCatalogMode(catalog);
  if (catalog === 'starter') return { bundle: starterBundle(), notice: null, checked: false };
  const env = envOf(deps);
  if (loadCredentials(env) === null) {
    if (catalog === 'full') throw new LoginRequiredError();
    return { bundle: starterBundle(), notice: 'not_logged_in', checked: false };
  }
  const state = readState(env);
  const cached = cachedFull(state, env);
  if (cached) return { bundle: cached, notice: null, checked: false };
  if (state && state.version === null && !checkDue(state, nowOf(deps))) {
    return { bundle: starterBundle(), notice: 'no_entitlement', checked: false };
  }
  return { bundle: starterBundle(), notice: null, checked: false };
}

/**
 * Pick the catalog per contract section 5 ("Which catalog").
 *
 *  - 'starter': the packaged starter catalog; no network, no nudge.
 *  - 'auto': starter when not logged in (notice not_logged_in); otherwise the
 *    full catalog, checked against the server at most once per 24 h
 *    (`forceCheck` checks now).
 *  - 'full': like auto, but throws LoginRequiredError when not logged in.
 *
 * Throws SessionEndedError (after deleting the credentials file and the full
 * cache) when the session is gone. Never prints.
 */
export async function selectCatalog(
  catalog: CatalogMode = 'auto',
  deps: CatalogDeps = {},
  opts: { forceCheck?: boolean } = {},
): Promise<CatalogSelection> {
  assertCatalogMode(catalog);
  if (catalog === 'starter') return { bundle: starterBundle(), notice: null, checked: false };
  const env = envOf(deps);
  if (loadCredentials(env) === null) {
    if (catalog === 'full') throw new LoginRequiredError();
    return { bundle: starterBundle(), notice: 'not_logged_in', checked: false };
  }

  const state = readState(env);
  const cached = cachedFull(state, env);
  const due = Boolean(opts.forceCheck) || checkDue(state, nowOf(deps));
  if (!due && state) {
    if (cached) return { bundle: cached, notice: null, checked: false };
    if (state.version === null || state.version === undefined) {
      // Checked within 24 h and the account had no access then.
      return { bundle: starterBundle(), notice: 'no_entitlement', checked: false };
    }
    // state names a version but its directory is gone: check now.
  }

  const { outcome, bundle } = await runCheck(cached, deps);
  if ((outcome === 'ok' || outcome === 'not_modified') && bundle) {
    return { bundle, notice: null, checked: true };
  }
  if (outcome === 'session_ended') {
    endSession(env);
    throw new SessionEndedError();
  }
  if (outcome === 'forbidden') {
    return { bundle: starterBundle(), notice: 'no_entitlement', checked: true };
  }
  if (cached) return { bundle: cached, notice: null, checked: true, failed: true };
  return {
    bundle: starterBundle(),
    notice: outcome === 'schema_too_new' ? 'schema_too_new' : 'download_failed',
    checked: true,
    failed: true,
  };
}

/**
 * `tryaii login`'s immediate download: the full bundle (new, or the cached
 * one confirmed by a 304), or null on any failure. Never throws for
 * server/network problems and never deletes the fresh session.
 */
export async function downloadAfterLogin(deps: CatalogDeps = {}): Promise<CatalogBundle | null> {
  const env = envOf(deps);
  if (loadCredentials(env) === null) return null;
  let result: { outcome: Outcome; bundle: CatalogBundle | null };
  try {
    result = await runCheck(cachedFull(readState(env), env), deps);
  } catch {
    return null;
  }
  forgetSelections();
  return result.outcome === 'ok' || result.outcome === 'not_modified' ? result.bundle : null;
}

// ------------------------------------------------------------------ nudge (CLI)
export function nudgeText(starter: CatalogBundle = starterBundle()): string {
  const full = starter.fullCounts?.models ?? 0;
  return (
    `Routing on the starter catalog (${routableCount(starter)} models). ` +
    `Log in for free to use the full catalog (${full} models): tryaii login`
  );
}

function localToday(nowMs: number): string {
  const d = new Date(nowMs);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * CLI only: print the login nudge (stderr) when the selection is the starter
 * catalog because nobody is logged in -- at most once per local calendar
 * day, never when TRYAII_NO_BANNER is set. True when shown.
 */
export function maybeNudge(
  selection: CatalogSelection,
  write: (text: string) => void = (t) => {
    process.stderr.write(t);
  },
  deps: CatalogDeps = {},
): boolean {
  const env = envOf(deps);
  if (selection.notice !== 'not_logged_in' || env.TRYAII_NO_BANNER) return false;
  const today = localToday(nowOf(deps));
  try {
    const data = JSON.parse(readFileSync(nudgePath(env), 'utf-8')) as { last_shown?: unknown };
    if (data && data.last_shown === today) return false;
  } catch {
    // never shown (or unreadable): show it
  }
  write(nudgeText(selection.bundle) + '\n');
  try {
    writeFileAtomic(
      nudgePath(env),
      Buffer.from(`{"last_shown": ${JSON.stringify(today)}}`, 'utf-8'),
      '.nudge-',
    );
  } catch {
    // shown again tomorrow at worst
  }
  return true;
}

// ------------------------------------------------------------------ library memo
// The selection runs once per process (per mode and data dir) and again after
// 24 h, so a library that builds several Routers / registries checks the
// server at most once a day. A selection whose check FAILED (`failed`:
// download_failed, schema_too_new, or a stale cached catalog kept after a
// failure) is kept for FAILURE_RETRY_MS only, so a long-running process that
// started during a network blip retries on a later use instead of staying
// put for a day. Not-logged-in and no-access (403) selections are final
// answers: 24 h.
const selections = new Map<string, { at: number; selection: CatalogSelection }>();

function memoFresh(hit: { at: number; selection: CatalogSelection } | undefined): boolean {
  if (!hit) return false;
  const ttl = hit.selection.failed ? FAILURE_RETRY_MS : CHECK_INTERVAL_MS;
  return Date.now() - hit.at < ttl;
}
const inflight = new Map<string, Promise<CatalogSelection>>();

function forgetSelections(): void {
  selections.clear();
  inflight.clear();
}

/** Forget every in-process memo (tests). */
export function resetCatalogCache(): void {
  forgetSelections();
  loaded.clear();
}

function memoKey(catalog: CatalogMode, env: NodeJS.ProcessEnv): string {
  return `${catalog}\u0000${dataDir(env)}`;
}

/**
 * Sync: the memoized selection's bundle when the async check already ran in
 * this process, else the offline selection (local cache only).
 */
export function selectedBundleSync(catalog: CatalogMode = 'auto'): CatalogBundle {
  assertCatalogMode(catalog);
  if (catalog === 'starter') return starterBundle();
  const hit = selections.get(memoKey(catalog, process.env));
  if (memoFresh(hit)) return hit!.selection.bundle;
  return selectCatalogOffline(catalog).bundle;
}

/** Async: the memoized (per process) full selection for `catalog`. */
export async function selectedBundle(catalog: CatalogMode = 'auto'): Promise<CatalogBundle> {
  assertCatalogMode(catalog);
  if (catalog === 'starter') return starterBundle();
  const key = memoKey(catalog, process.env);
  const hit = selections.get(key);
  if (memoFresh(hit)) return hit!.selection.bundle;
  let pending = inflight.get(key);
  if (!pending) {
    pending = selectCatalog(catalog).finally(() => inflight.delete(key));
    inflight.set(key, pending);
  }
  const selection = await pending;
  selections.set(key, { at: Date.now(), selection });
  return selection.bundle;
}
