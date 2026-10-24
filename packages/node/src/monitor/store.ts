/**
 * The local monitor store: `<data_dir>/monitor/<app>/` (SPEC.md §3).
 *
 * Append-only daily JSONL files for events and vectors (one append write per
 * line, so concurrent processes interleave whole lines), plus `pins.json` /
 * `config.json` written atomically. Mirrors monitor/store.py.
 */

import { appendFileSync, closeSync, mkdirSync, openSync, readdirSync, readFileSync, readSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';

import { dataDir, writeFileAtomic } from '../auth/store.js';
import { emptyConfig, REMOTE_SCHEMA, type ConfigDoc } from './config.js';
import { parseTs } from './events.js';
import { emptyPins, type PinsDoc } from './pins.js';

export const MONITOR_DIR = 'monitor';
export const EVENTS_PREFIX = 'events-';
export const VECTORS_PREFIX = 'vectors-';
export const PINS_FILE = 'pins.json';
export const CONFIG_FILE = 'config.json';
export const REMOTE_FILE = 'remote.json';
export const UPLOAD_FILE = 'upload.json';
export const UPLOAD_LOCK_FILE = 'upload.lock';
export const UPLOAD_CURSOR_SCHEMA = 'tryaii.monitor.upload-cursor/1';

const seenHashes = new Map<string, Set<string>>();

export function monitorRoot(dataDir: string): string {
  return join(dataDir, MONITOR_DIR);
}

export function appDir(dataDir: string, app: string): string {
  return join(monitorRoot(dataDir), app);
}

function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

function isFile(path: string): boolean {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

/** App names with a monitor directory, sorted. */
export function listApps(dataDir: string): string[] {
  const root = monitorRoot(dataDir);
  if (!isDir(root)) return [];
  return readdirSync(root)
    .filter((name) => isDir(join(root, name)))
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

export function dayOf(ts: string): string {
  return new Date(parseTs(ts)).toISOString().slice(0, 10);
}

export function eventsPath(directory: string, ts: string): string {
  return join(directory, `${EVENTS_PREFIX}${dayOf(ts)}.jsonl`);
}

export function vectorsPath(directory: string, ts: string): string {
  return join(directory, `${VECTORS_PREFIX}${dayOf(ts)}.jsonl`);
}

/** Minified JSON, key order preserved, non-ASCII verbatim, `\n`. */
export function dumpsLine(obj: unknown): string {
  return JSON.stringify(obj) + '\n';
}

export function appendLine(path: string, obj: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, dumpsLine(obj), { encoding: 'utf-8', flag: 'a', mode: 0o600 });
}

export function appendEvent(directory: string, event: Record<string, unknown>): string {
  const path = eventsPath(directory, event.ts as string);
  appendLine(path, event);
  return path;
}

function hashesIn(path: string): Set<string> {
  let seen = seenHashes.get(path);
  if (seen === undefined) {
    seen = new Set();
    if (isFile(path)) {
      for (const line of readFileSync(path, 'utf-8').split(/\r?\n/)) {
        try {
          const obj = JSON.parse(line);
          if (obj && typeof obj === 'object' && typeof obj.prompt_hash === 'string') seen.add(obj.prompt_hash);
        } catch {
          continue;
        }
      }
    }
    seenHashes.set(path, seen);
  }
  return seen;
}

/**
 * Append a vectors line unless the day's file already has this
 * `prompt_hash`. Returns true when written.
 */
export function appendVector(directory: string, record: Record<string, unknown>): boolean {
  const path = vectorsPath(directory, record.ts as string);
  const seen = hashesIn(path);
  const hash = record.prompt_hash as string;
  if (seen.has(hash)) return false;
  seen.add(hash);
  appendLine(path, record);
  return true;
}

function dayFiles(directory: string, prefix: string, startMs: number, endMs: number): string[] {
  if (!isDir(directory)) return [];
  const first = new Date(startMs).toISOString().slice(0, 10);
  const last = new Date(endMs + 24 * 3600 * 1000).toISOString().slice(0, 10);
  const out: string[] = [];
  for (const name of readdirSync(directory).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))) {
    if (!(name.startsWith(prefix) && name.endsWith('.jsonl'))) continue;
    const day = name.slice(prefix.length, -'.jsonl'.length);
    if (first <= day && day <= last) out.push(join(directory, name));
  }
  return out;
}

export function readLines(path: string): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  if (!isFile(path)) return out;
  for (const raw of readFileSync(path, 'utf-8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    let obj: unknown;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (obj !== null && typeof obj === 'object' && !Array.isArray(obj)) out.push(obj as Record<string, unknown>);
  }
  return out;
}

/**
 * Events with `start <= ts <= now` across the daily files, in file order
 * (which is append order). Malformed lines are skipped.
 */
export function readEvents(directory: string, now: string, sinceSeconds: number): Record<string, unknown>[] {
  const end = parseTs(now);
  const start = end - sinceSeconds * 1000;
  const out: Record<string, unknown>[] = [];
  for (const path of dayFiles(directory, EVENTS_PREFIX, start, end)) {
    for (const event of readLines(path)) {
      let ts: number;
      try {
        ts = parseTs(event.ts);
      } catch {
        continue;
      }
      if (start <= ts && ts <= end) out.push(event);
    }
  }
  return out;
}

/** The last `limit` events across all daily files (oldest first). */
export function readRecentEvents(directory: string, limit: number): Record<string, unknown>[] {
  if (!isDir(directory) || limit <= 0) return [];
  const files = readdirSync(directory)
    .filter((name) => name.startsWith(EVENTS_PREFIX) && name.endsWith('.jsonl'))
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  let out: Record<string, unknown>[] = [];
  for (const name of files.reverse()) {
    out = readLines(join(directory, name)).concat(out);
    if (out.length >= limit) break;
  }
  return out.slice(-limit);
}

function dumpDoc(doc: unknown): Uint8Array {
  return Buffer.from(JSON.stringify(doc, null, 2) + '\n', 'utf-8');
}

function readDoc<T extends Record<string, unknown>>(path: string, fallback: T): T {
  if (!isFile(path)) return fallback;
  let obj: unknown;
  try {
    obj = JSON.parse(readFileSync(path, 'utf-8'));
  } catch {
    return fallback;
  }
  return obj !== null && typeof obj === 'object' && !Array.isArray(obj) ? (obj as T) : fallback;
}

export function readPins(directory: string): PinsDoc {
  return readDoc(join(directory, PINS_FILE), emptyPins() as PinsDoc & Record<string, unknown>);
}

export function writePins(directory: string, pins: PinsDoc): string {
  const path = join(directory, PINS_FILE);
  writeFileAtomic(path, dumpDoc(pins));
  return path;
}

export function readConfig(directory: string): ConfigDoc {
  return readDoc(join(directory, CONFIG_FILE), emptyConfig() as ConfigDoc & Record<string, unknown>);
}

export function writeConfig(directory: string, config: ConfigDoc): string {
  const path = join(directory, CONFIG_FILE);
  writeFileAtomic(path, dumpDoc(config));
  return path;
}

export function configMtime(directory: string): number | null {
  try {
    return statSync(join(directory, CONFIG_FILE)).mtimeMs / 1000;
  } catch {
    return null;
  }
}

/** The SDK data dir (`TRYAII_DRE_DATA_DIR` or `~/.tryaii`), as auth/store.ts resolves it. */
export function dataDirFromEnv(env: NodeJS.ProcessEnv = process.env): string {
  return dataDir(env);
}

// --- remote.json (SPEC.md §3.3) ----------------------------------------------

/**
 * The cached remote config, or null when the file is missing, not JSON, or
 * not a `tryaii.monitor.remote/1` object ("no remote config": exactly the
 * local behaviour).
 */
export function readRemote(directory: string): Record<string, unknown> | null {
  const path = join(directory, REMOTE_FILE);
  if (!isFile(path)) return null;
  let obj: unknown;
  try {
    obj = JSON.parse(readFileSync(path, 'utf-8'));
  } catch {
    return null;
  }
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) return null;
  if ((obj as Record<string, unknown>).schema !== REMOTE_SCHEMA) return null;
  return obj as Record<string, unknown>;
}

/**
 * Bumped by every `writeRemote` in this process, so the wrapper's cached copy
 * is re-read on the very next call after the long poll wrote a new answer
 * (no 5 s re-check delay, no reliance on mtime resolution; TS-296).
 */
let remoteWrites = 0;

/** How many times this process wrote a `remote.json` (any app). */
export function remoteGeneration(): number {
  return remoteWrites;
}

/**
 * Write a server `RemoteConfig` plus `fetched_at` (last key), atomically,
 * indent 2. The server's key order is kept.
 */
export function writeRemote(directory: string, remote: Record<string, unknown>, fetchedAt: string): string {
  const doc: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(remote)) if (key !== 'fetched_at') doc[key] = value;
  doc.fetched_at = fetchedAt;
  const path = join(directory, REMOTE_FILE);
  writeFileAtomic(path, dumpDoc(doc));
  remoteWrites += 1;
  return path;
}

export function remoteMtime(directory: string): number | null {
  try {
    return statSync(join(directory, REMOTE_FILE)).mtimeMs / 1000;
  } catch {
    return null;
  }
}

// --- upload.json (SPEC.md §3.4) ----------------------------------------------

export interface UploadCursor {
  schema: string;
  files: Record<string, number>;
  last_ok: string | null;
  last_error: string | null;
}

export function emptyUploadCursor(): UploadCursor {
  return { schema: UPLOAD_CURSOR_SCHEMA, files: {}, last_ok: null, last_error: null };
}

function sortedFiles(files: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const name of Object.keys(files).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))) out[name] = files[name];
  return out;
}

/**
 * The upload cursor, or null when no upload was ever attempted (or the file
 * is unreadable). `files` is normalised to name -> int.
 */
export function readUploadCursor(directory: string): UploadCursor | null {
  const path = join(directory, UPLOAD_FILE);
  if (!isFile(path)) return null;
  let obj: unknown;
  try {
    obj = JSON.parse(readFileSync(path, 'utf-8'));
  } catch {
    return null;
  }
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) return null;
  const doc = obj as Record<string, unknown>;
  if (doc.schema !== UPLOAD_CURSOR_SCHEMA) return null;
  const files = doc.files !== null && typeof doc.files === 'object' && !Array.isArray(doc.files) ? doc.files : {};
  const clean: Record<string, number> = {};
  for (const [name, value] of Object.entries(files as Record<string, unknown>)) {
    if (typeof value === 'number' && Number.isInteger(value) && value >= 0) clean[name] = value;
  }
  return {
    schema: UPLOAD_CURSOR_SCHEMA,
    files: sortedFiles(clean),
    last_ok: typeof doc.last_ok === 'string' ? doc.last_ok : null,
    last_error: typeof doc.last_error === 'string' ? doc.last_error : null,
  };
}

/** Atomic, indent 2, key order schema, files (sorted), last_ok, last_error. */
export function writeUploadCursor(directory: string, cursor: Partial<UploadCursor>): string {
  const doc: UploadCursor = {
    schema: UPLOAD_CURSOR_SCHEMA,
    files: sortedFiles(cursor.files ?? {}),
    last_ok: cursor.last_ok ?? null,
    last_error: cursor.last_error ?? null,
  };
  const path = join(directory, UPLOAD_FILE);
  writeFileAtomic(path, dumpDoc(doc));
  return path;
}

/** Every `events-*.jsonl` of an app directory, sorted by name (= day). */
export function eventFiles(directory: string): string[] {
  if (!isDir(directory)) return [];
  return readdirSync(directory)
    .filter((name) => name.startsWith(EVENTS_PREFIX) && name.endsWith('.jsonl') && isFile(join(directory, name)))
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
    .map((name) => join(directory, name));
}

/** The first `size` bytes of a file (fewer when it is shorter). */
export function readPrefix(path: string, size: number): Buffer {
  const fd = openSync(path, 'r');
  try {
    const buf = Buffer.alloc(size);
    let got = 0;
    while (got < size) {
      const n = readSync(fd, buf, got, size - got, got);
      if (n <= 0) break;
      got += n;
    }
    return buf.subarray(0, got);
  } finally {
    closeSync(fd);
  }
}

/**
 * The number of event lines inside the uploaded byte prefixes of the
 * cursor's files (every newline counts; files that are gone count 0).
 */
export function uploadedEventCount(directory: string, cursor: UploadCursor | null | undefined): number {
  if (!cursor) return 0;
  let total = 0;
  for (const [name, size] of Object.entries(cursor.files ?? {})) {
    const path = join(directory, name);
    if (!isFile(path) || size <= 0) continue;
    try {
      const buf = readPrefix(path, size);
      for (const byte of buf) if (byte === 0x0a) total++;
    } catch {
      continue;
    }
  }
  return total;
}
