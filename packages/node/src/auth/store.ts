/**
 * Credentials storage (docs/auth/CONTRACT-auth-v1.md section 6).
 *
 * `<data dir>/credentials.json`, data dir = TRYAII_DRE_DATA_DIR or
 * ~/.tryaii. Written atomically (contract section 7, item 18): a temp file
 * with an unpredictable name in the same directory, created exclusively
 * ('wx' = O_EXCL) with mode 0600, fsynced, then renamed over the target --
 * retried on Windows while another process briefly holds the file.
 */

import { randomBytes } from 'node:crypto';
import {
  chmodSync,
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  unlinkSync,
  writeSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

export const CREDENTIALS_FILE = 'credentials.json';
export const CREDENTIALS_VERSION = 1;

export interface AuthUser {
  id: string;
  email: string;
  name: string;
}

/** On-disk schema, keys in contract order. */
export interface Credentials {
  version: number;
  api_url: string;
  user: AuthUser;
  entitlements: string[];
  refresh_token: string;
  refresh_expires_at: string;
  access_token: string;
  access_expires_at: string;
  created_at: string;
}

export function dataDir(env: NodeJS.ProcessEnv = process.env): string {
  return env.TRYAII_DRE_DATA_DIR || join(homedir(), '.tryaii');
}

export function credentialsPath(env: NodeJS.ProcessEnv = process.env): string {
  return join(dataDir(env), CREDENTIALS_FILE);
}

/** ISO 8601 UTC with Z, second precision. */
export function isoUtc(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export function parseIso(value: string): number {
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : 0;
}

/**
 * Read the credentials file. Missing, unreadable, non-object, another
 * schema version or no refresh token all read as null.
 */
export function loadCredentials(env: NodeJS.ProcessEnv = process.env): Credentials | null {
  let raw: string;
  try {
    raw = readFileSync(credentialsPath(env), 'utf-8');
  } catch {
    return null;
  }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return null;
  const c = data as Partial<Credentials>;
  if (c.version !== CREDENTIALS_VERSION) return null;
  if (typeof c.refresh_token !== 'string' || !c.refresh_token) return null;
  return c as Credentials;
}

/** Rebuild in contract key order so the file layout is stable. */
function ordered(c: Credentials): Credentials {
  return {
    version: c.version,
    api_url: c.api_url,
    user: { id: c.user.id, email: c.user.email, name: c.user.name },
    entitlements: c.entitlements,
    refresh_token: c.refresh_token,
    refresh_expires_at: c.refresh_expires_at,
    access_token: c.access_token,
    access_expires_at: c.access_expires_at,
    created_at: c.created_at,
  };
}

/** Windows rename retry (item 18): up to 5 retries, 50 ms apart. */
export const RENAME_RETRIES = 5;
export const RENAME_RETRY_DELAY_MS = 50;
const RENAME_RETRY_CODES = new Set(['EPERM', 'EACCES', 'EBUSY']);

/** Synchronous sleep (the store API is synchronous). */
function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

export interface RenameOptions {
  platform?: string;
  rename?: (src: string, dst: string) => void;
  sleep?: (ms: number) => void;
}

/**
 * renameSync; on Windows retried up to 5 times, 50 ms apart, on
 * EPERM/EACCES/EBUSY (an antivirus, indexer or a parallel tryaii holding
 * the target open) so a rotated refresh token is not lost.
 */
export function renameWithRetry(src: string, dst: string, opts: RenameOptions = {}): void {
  const rename = opts.rename ?? renameSync;
  const sleep = opts.sleep ?? sleepSync;
  const attempts = 1 + ((opts.platform ?? process.platform) === 'win32' ? RENAME_RETRIES : 0);
  for (let attempt = 0; ; attempt++) {
    try {
      rename(src, dst);
      return;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code ?? '';
      if (attempt >= attempts - 1 || !RENAME_RETRY_CODES.has(code)) throw error;
      sleep(RENAME_RETRY_DELAY_MS);
    }
  }
}

/**
 * Atomically write the credentials file: exclusive 0600 temp file in the
 * same directory, fsync, then rename (with the Windows retry).
 */
export function saveCredentials(
  creds: Credentials,
  env: NodeJS.ProcessEnv = process.env,
  renameOpts: RenameOptions = {},
): void {
  const data = Buffer.from(JSON.stringify(ordered(creds), null, 2) + '\n', 'utf-8');
  writeFileAtomic(credentialsPath(env), data, '.credentials-', renameOpts);
}

/**
 * THE atomic-write helper (item 18), shared by the credentials file and the
 * catalog cache (catalog contract section 5): an exclusive ('wx' = O_EXCL)
 * 0600 temp file in the same directory, fsync, then rename over `path`
 * (retried on Windows).
 */
export function writeFileAtomic(
  path: string,
  data: Uint8Array,
  prefix = '.tryaii-',
  renameOpts: RenameOptions = {},
): void {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = join(dirname(path), `${prefix}${randomBytes(8).toString('hex')}.tmp`);
  let fd: number | null = null;
  try {
    fd = openSync(tmp, 'wx', 0o600);
    if (process.platform !== 'win32') chmodSync(tmp, 0o600); // umask-proof
    let off = 0;
    while (off < data.length) off += writeSync(fd, data, off, data.length - off);
    fsyncSync(fd);
    closeSync(fd);
    fd = null;
    renameWithRetry(tmp, path, renameOpts);
  } catch (error) {
    if (fd !== null) {
      try {
        closeSync(fd);
      } catch {
        // already failing; keep the original error
      }
    }
    rmSync(tmp, { force: true });
    throw error;
  }
}

export function credentialsFileExists(env: NodeJS.ProcessEnv = process.env): boolean {
  return existsSync(credentialsPath(env));
}

/**
 * Delete the credentials file. True when a file was removed, false when
 * there was none (ENOENT is not an error). Any other failure throws --
 * logout reports it (contract section 7, item 19).
 */
export function deleteCredentials(env: NodeJS.ProcessEnv = process.env): boolean {
  try {
    unlinkSync(credentialsPath(env));
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw error;
  }
}
