/**
 * Data Notice, upload switch and upload hashing (shared/monitor/SPEC.md §9,
 * API.md §7). Mirrors monitor/privacy.py byte for byte:
 *
 * - `NOTICE_TEXT`: the CLI notice (Confluence "CLI and SDK Data Notice", piece D);
 * - `uploadDecision`: whether this machine uploads, and why not
 *   (env opt-outs > the local `tryaii monitor upload off` setting > on);
 * - `settings.json` and `notice.json` under `<data dir>/monitor/`;
 * - `keyedHash`: the prompt hash as uploaded, an HMAC-SHA256 keyed with a
 *   random per-installation secret (`upload-hash.key`) that never leaves the
 *   machine, so the server cannot test guesses against it.
 *
 * The remote switch (`"upload": false` in remote.json, or a 403
 * `upload_disabled`) is applied by the uploader per app directory.
 */

import { chmodSync, mkdirSync, readFileSync } from 'node:fs';
import { createHmac, randomBytes } from 'node:crypto';
import { join } from 'node:path';

import { writeFileAtomic } from '../auth/store.js';

/** The notice version: its publication date (API.md §7.1). */
export const NOTICE_VERSION = '2026-10-18';
export const NOTICE_URL = 'https://tryaii.com/privacy-policy#cli-sdk';
export const NOTICE_TEXT =
  '[tryaii] monitor records sync to your TryAii account.\n' +
  'Sent: model names, token counts, costs, timings, file paths, a task profile.\n' +
  'Never sent: your prompts, responses or API keys.\n' +
  `Turn off: tryaii monitor upload off   Details: ${NOTICE_URL}\n`;

export const SETTINGS_FILE = 'settings.json';
export const SETTINGS_SCHEMA = 'tryaii.monitor.settings/1';
export const NOTICE_FILE = 'notice.json';
export const NOTICE_SCHEMA = 'tryaii.monitor.notice/1';
export const HASH_KEY_FILE = 'upload-hash.key';

export const ENV_UPLOAD = 'TRYAII_MONITOR_UPLOAD';
export const ENV_TELEMETRY = 'TRYAII_TELEMETRY';
export const ENV_DNT = 'DO_NOT_TRACK';

type Env = Record<string, string | undefined>;

function envOff(value: string | undefined): boolean {
  return value !== undefined && ['0', 'false', 'off', 'no'].includes(value.trim().toLowerCase());
}

function envOn(value: string | undefined): boolean {
  return value !== undefined && ['1', 'true', 'yes', 'on'].includes(value.trim().toLowerCase());
}

function dump(doc: unknown): Uint8Array {
  return Buffer.from(JSON.stringify(doc, null, 2) + '\n', 'utf-8');
}

function read(path: string): Record<string, unknown> | null {
  try {
    const obj: unknown = JSON.parse(readFileSync(path, 'utf-8'));
    return obj !== null && typeof obj === 'object' && !Array.isArray(obj) ? (obj as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

// --- the local switch -------------------------------------------------------------

/** The `tryaii monitor upload on|off` choice, or null when never set. */
export function readLocalUpload(root: string): boolean | null {
  const doc = read(join(root, SETTINGS_FILE));
  if (!doc || doc.schema !== SETTINGS_SCHEMA || typeof doc.upload !== 'boolean') return null;
  return doc.upload;
}

export function writeLocalUpload(root: string, upload: boolean): string {
  const path = join(root, SETTINGS_FILE);
  mkdirSync(root, { recursive: true });
  writeFileAtomic(path, dump({ schema: SETTINGS_SCHEMA, upload: Boolean(upload) }));
  return path;
}

/**
 * `[enabled, reason]`. `reason` names what turned upload off:
 * `TRYAII_MONITOR_UPLOAD=0`, `TRYAII_TELEMETRY=0`, `DO_NOT_TRACK=1` or
 * `tryaii monitor upload off`; null while enabled.
 */
export function uploadDecision(env: Env = process.env, root: string | null = null): [boolean, string | null] {
  if (envOff(env[ENV_UPLOAD])) return [false, `${ENV_UPLOAD}=0`];
  if (envOff(env[ENV_TELEMETRY])) return [false, `${ENV_TELEMETRY}=0`];
  if (envOn(env[ENV_DNT])) return [false, `${ENV_DNT}=1`];
  if (root !== null && readLocalUpload(root) === false) return [false, 'tryaii monitor upload off'];
  return [true, null];
}

// --- the one-time notice -------------------------------------------------------------

export function noticeShown(root: string, version: string = NOTICE_VERSION): boolean {
  const doc = read(join(root, NOTICE_FILE));
  return !!doc && doc.schema === NOTICE_SCHEMA && doc.version === version;
}

export function markNoticeShown(root: string, shownAt: string, version: string = NOTICE_VERSION): void {
  mkdirSync(root, { recursive: true });
  writeFileAtomic(join(root, NOTICE_FILE), dump({ schema: NOTICE_SCHEMA, version, shown_at: shownAt }));
}

/** Only to a person: the stream is a terminal and `CI` is not set. */
export function canShowNotice(stream: { isTTY?: boolean } | null | undefined, env: Env = process.env): boolean {
  if (env.CI) return false;
  return !!stream && stream.isTTY === true;
}

// --- keyed prompt hash -----------------------------------------------------------------

/**
 * The per-installation secret (32 random bytes, hex in `upload-hash.key`),
 * created on first use. Never uploaded.
 */
export function hashKey(root: string): Buffer {
  const path = join(root, HASH_KEY_FILE);
  try {
    const text = readFileSync(path, 'ascii').trim();
    if (/^[0-9a-fA-F]{64}$/.test(text)) return Buffer.from(text, 'hex');
  } catch {
    // created below
  }
  mkdirSync(root, { recursive: true });
  const key = randomBytes(32);
  writeFileAtomic(path, Buffer.from(key.toString('hex') + '\n', 'ascii'));
  try {
    chmodSync(path, 0o600);
  } catch {
    // best effort (Windows)
  }
  return key;
}

/** The uploaded `prompt_hash`: lowercase hex HMAC-SHA256(key, local hash). */
export function keyedHash(key: Buffer, promptHash: string): string {
  return createHmac('sha256', key).update(promptHash, 'ascii').digest('hex');
}
