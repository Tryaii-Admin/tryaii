/**
 * Catalog signatures (docs/catalog/CONTRACT-catalog-v1.md, section 6).
 *
 * A full catalog is trusted only when its manifest carries a `signature` made
 * with a key the tryaii team holds offline. The signed message is the manifest
 * WITHOUT its `signature` and `key_id` keys, as canonical text (section 1:
 * Python `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`)
 * encoded as UTF-8. Manifests must be ASCII-only so the Python and Node
 * canonical forms are byte-identical; numbers must be (safe) integers for the
 * same reason (a float's text differs between the two runtimes).
 *
 *  - `signature`: base64url without padding of the 64-byte Ed25519 signature.
 *  - `key_id`: `[a-z0-9-]{3,64}`, names a key in the trusted list.
 *  - Trusted keys: the packaged `data/trusted_keys.json`
 *    `{"keys": [{"key_id", "public_key" (base64url raw 32 bytes), "env"}]}`.
 *    `TRYAII_CATALOG_TRUSTED_KEYS` (path to a file of the same shape) REPLACES
 *    the packaged list -- for development and tests only.
 *
 * Verification is `crypto.verify(null, message, publicKey, signature)` (pure
 * Ed25519). This module never throws bundle errors itself: `signatureProblem`
 * returns the reason a manifest is not trusted, and `bundle.ts` turns it into
 * a `BundleSignatureError` (a `BundleIntegrityError`, so callers treat it
 * exactly like a hash mismatch). Mirrors
 * `packages/python/tryaii/catalog/signing.py`.
 */

import { createPublicKey, verify as cryptoVerify } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/**
 * Environment variable naming a trusted-keys file that REPLACES the packaged
 * list (development and tests only).
 */
export const TRUSTED_KEYS_ENV = 'TRYAII_CATALOG_TRUSTED_KEYS';

/** The packaged trusted public keys (a copy of shared/catalog/trusted_keys.json). */
export const PACKAGED_TRUSTED_KEYS = fileURLToPath(
  new URL('./data/trusted_keys.json', import.meta.url),
);

/** Manifest keys left out of the signed message. */
export const UNSIGNED_KEYS = ['signature', 'key_id'] as const;

export const KEY_ENVS = ['production', 'development'] as const;
export type KeyEnv = (typeof KEY_ENVS)[number];

export interface TrustedKey {
  publicKey: Buffer;
  env: KeyEnv;
}

const KEY_ID_RE = /^[a-z0-9-]{3,64}$/;
const B64URL_RE = /^[A-Za-z0-9_-]+$/;
const ASCII_RE = /^[\x00-\x7f]*$/;

/** A manifest that cannot be signed / verified, or an unreadable key list. */
export class SignatureProblem extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SignatureProblem';
  }
}

export function isValidKeyId(value: unknown): value is string {
  return typeof value === 'string' && KEY_ID_RE.test(value);
}

/** base64url without padding. */
export function b64urlEncode(data: Uint8Array): string {
  return Buffer.from(data).toString('base64url');
}

/**
 * Strict base64url-without-padding decode of exactly `length` bytes, or null.
 * Only the canonical encoding is accepted (no padding, no whitespace, no
 * non-zero trailing bits) -- same rule as the Python SDK.
 */
export function b64urlDecode(text: unknown, length: number): Buffer | null {
  if (typeof text !== 'string' || !B64URL_RE.test(text)) return null;
  const data = Buffer.from(text, 'base64url');
  if (data.length !== length || data.toString('base64url') !== text) return null;
  return data;
}

/**
 * Python's `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
 * for the JSON values a manifest may hold (objects, arrays, strings, safe
 * integers, booleans, null). Byte-identical to Python for ASCII text.
 */
export function canonicalJson(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) {
      throw new SignatureProblem(
        Number.isInteger(value)
          ? 'manifest.json has an integer too large to sign'
          : 'manifest.json has a non-integer number (cannot be signed)',
      );
    }
    return String(value === 0 ? 0 : value); // -0 -> "0", like Python's int
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(obj[k])}`).join(',')}}`;
  }
  throw new SignatureProblem(`manifest.json has an unsupported value: ${typeof value}`);
}

/**
 * The exact bytes a manifest's signature covers (contract section 6).
 * Throws `SignatureProblem` for a non-ASCII manifest (any key or string
 * value) or a non-integer number.
 */
export function signedMessage(manifest: unknown): Buffer {
  if (manifest === null || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw new SignatureProblem('manifest.json must be a JSON object');
  }
  const body: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(manifest as Record<string, unknown>)) {
    if (!(UNSIGNED_KEYS as readonly string[]).includes(k)) body[k] = v;
  }
  const text = canonicalJson(body);
  if (!ASCII_RE.test(text)) {
    throw new SignatureProblem('manifest.json must be ASCII-only to be signed or verified');
  }
  return Buffer.from(text, 'utf-8');
}

/**
 * `key_id -> {publicKey, env}` from a parsed trusted-keys document. Throws
 * `SignatureProblem` when the document (or any entry) is malformed -- a
 * broken list trusts nothing.
 */
export function parseTrustedKeys(data: unknown): Map<string, TrustedKey> {
  const keysField =
    data !== null && typeof data === 'object' && !Array.isArray(data)
      ? (data as Record<string, unknown>).keys
      : undefined;
  if (!Array.isArray(keysField)) {
    throw new SignatureProblem("trusted keys file must be an object with a 'keys' array");
  }
  const keys = new Map<string, TrustedKey>();
  for (const entry of keysField as unknown[]) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
      throw new SignatureProblem('trusted keys: every entry must be an object');
    }
    const e = entry as Record<string, unknown>;
    if (!isValidKeyId(e.key_id)) {
      throw new SignatureProblem(`trusted keys: invalid key_id ${JSON.stringify(e.key_id)}`);
    }
    const publicKey = b64urlDecode(e.public_key, 32);
    if (publicKey === null) {
      throw new SignatureProblem(`trusted keys: ${e.key_id}: public_key is not base64url of 32 bytes`);
    }
    if (!isValidPublicKey(publicKey)) {
      // a non-canonical or small-order key would let anyone "sign"
      throw new SignatureProblem(`trusted keys: ${e.key_id}: public_key is not a valid Ed25519 key`);
    }
    if (!(KEY_ENVS as readonly unknown[]).includes(e.env)) {
      throw new SignatureProblem(`trusted keys: ${e.key_id}: env must be one of ${KEY_ENVS.join(', ')}`);
    }
    if (keys.has(e.key_id)) throw new SignatureProblem(`trusted keys: ${e.key_id} is listed twice`);
    keys.set(e.key_id, { publicKey, env: e.env as KeyEnv });
  }
  return keys;
}

/**
 * The trusted-keys file in effect: TRYAII_CATALOG_TRUSTED_KEYS when set
 * (non-empty), else the packaged list.
 */
export function trustedKeysPath(env: NodeJS.ProcessEnv = process.env): string {
  const override = env[TRUSTED_KEYS_ENV];
  return override ? override : PACKAGED_TRUSTED_KEYS;
}

/** The trusted keys in effect. Throws `SignatureProblem` when unreadable or malformed. */
export function loadTrustedKeys(env: NodeJS.ProcessEnv = process.env): Map<string, TrustedKey> {
  let data: unknown;
  try {
    data = JSON.parse(readFileSync(trustedKeysPath(env), 'utf-8'));
  } catch (err) {
    throw new SignatureProblem(`cannot read the trusted catalog keys: ${(err as Error).message}`);
  }
  try {
    return parseTrustedKeys(data);
  } catch (err) {
    throw new SignatureProblem(`cannot read the trusted catalog keys: ${(err as Error).message}`);
  }
}

// --- public key validation (RFC 8032 5.1.3 decoding + small-order check) ----
// OpenSSL's verify accepts small-order public keys (e.g. the identity, for
// which S = 0 "verifies" any message), so a trusted-keys list is checked here,
// with the same rules the Python verifier applies to every key.
const P = (1n << 255n) - 19n;

function modP(a: bigint): bigint {
  const r = a % P;
  return r >= 0n ? r : r + P;
}

function powP(base: bigint, exp: bigint): bigint {
  let result = 1n;
  let b = modP(base);
  let e = exp;
  while (e > 0n) {
    if (e & 1n) result = (result * b) % P;
    b = (b * b) % P;
    e >>= 1n;
  }
  return result;
}

const D = modP(-121665n * powP(121666n, P - 2n));
const SQRT_M1 = powP(2n, (P - 1n) / 4n);

type Point = [bigint, bigint, bigint, bigint];

function decodePoint(bytes: Uint8Array): Point | null {
  let y = 0n;
  for (let i = 31; i >= 0; i--) y = (y << 8n) | BigInt(bytes[i]);
  const sign = y >> 255n;
  y &= (1n << 255n) - 1n;
  if (y >= P) return null; // non-canonical
  const x2 = modP((y * y - 1n) * powP(D * y * y + 1n, P - 2n));
  if (x2 === 0n) return sign ? null : [0n, y, 1n, 0n];
  let x = powP(x2, (P + 3n) / 8n);
  if (modP(x * x - x2) !== 0n) x = (x * SQRT_M1) % P;
  if (modP(x * x - x2) !== 0n) return null; // not on the curve
  if ((x & 1n) !== sign) x = P - x;
  return [x, y, 1n, (x * y) % P];
}

function doublePoint([X, Y, Z]: Point): Point {
  const a = modP(X * X);
  const b = modP(Y * Y);
  const c = modP(2n * Z * Z);
  const h = a + b;
  const e = modP(h - (X + Y) * (X + Y));
  const g = a - b;
  const f = c + g;
  return [modP(e * f), modP(g * h), modP(f * g), modP(e * h)];
}

/**
 * True when `publicKey` is a canonical encoding of a curve point that is not
 * of small order (same rule as the Python SDK's `ed25519.is_valid_public_key`).
 */
export function isValidPublicKey(publicKey: Uint8Array): boolean {
  if (publicKey.length !== 32) return false;
  const point = decodePoint(publicKey);
  if (point === null) return false;
  const q = doublePoint(doublePoint(doublePoint(point)));
  // [8]P is the identity (x = 0, y = z) for exactly the small-order points
  return !(q[0] === 0n && modP(q[1] - q[2]) === 0n);
}

/** Raw Ed25519 verification (pure Ed25519, RFC 8032). Never throws. */
export function ed25519Verify(publicKey: Uint8Array, message: Uint8Array, signature: Uint8Array): boolean {
  if (publicKey.length !== 32 || signature.length !== 64) return false;
  try {
    const key = createPublicKey({
      key: { kty: 'OKP', crv: 'Ed25519', x: Buffer.from(publicKey).toString('base64url') },
      format: 'jwk',
    });
    return cryptoVerify(null, message, key, signature);
  } catch {
    return false;
  }
}

/**
 * Why a manifest's signature is not trusted (contract section 6), or null
 * when it verifies against a trusted key: missing signature, invalid
 * key_id, unknown key, bad encoding, non-ASCII manifest, bad signature, or
 * an unreadable trusted-keys file.
 */
export function signatureProblem(
  manifest: unknown,
  env: NodeJS.ProcessEnv = process.env,
): string | null {
  if (manifest === null || typeof manifest !== 'object' || Array.isArray(manifest)) {
    return 'manifest.json must be a JSON object';
  }
  const m = manifest as Record<string, unknown>;
  const signature = m.signature;
  const keyId = m.key_id;
  if (signature === null || signature === undefined || keyId === null || keyId === undefined) {
    return 'the catalog is not signed';
  }
  if (!isValidKeyId(keyId)) return 'manifest.json has an invalid key_id';
  const sig = b64urlDecode(signature, 64);
  if (sig === null) return 'manifest.json signature is not base64url of 64 bytes';
  let message: Buffer;
  let trusted: Map<string, TrustedKey>;
  try {
    message = signedMessage(manifest);
    trusted = loadTrustedKeys(env);
  } catch (err) {
    return (err as Error).message;
  }
  const key = trusted.get(keyId);
  if (!key) return `the catalog is signed with an unknown key (${keyId})`;
  if (!ed25519Verify(key.publicKey, message, sig)) return 'the catalog signature does not verify';
  return null;
}
