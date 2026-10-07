/**
 * Test-only catalog signing (docs/catalog/CONTRACT-catalog-v1.md, section 6).
 *
 * Tests sign their synthetic full bundles with a TEST key they control and
 * point TRYAII_CATALOG_TRUSTED_KEYS at a file listing it. Independent of the
 * implementation (own canonical text, node:crypto signing). The key is derived
 * from a fixed public seed -- it protects nothing and must never be trusted
 * outside tests. packages/python/tests/_catalog_signing.py derives the same
 * key from the same seed (public key TEST_PUBLIC_KEY below).
 */

import { createHash, createPrivateKey, createPublicKey, sign as cryptoSign } from 'node:crypto';
import type { KeyObject } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

export const TRUSTED_KEYS_ENV = 'TRYAII_CATALOG_TRUSTED_KEYS';
export const TEST_KEY_ID = 'test-catalog-1';
export const TEST_SEED = createHash('sha256')
  .update('tryaii catalog contract section 6 test key')
  .digest();
/** base64url raw public key of TEST_SEED (same value as the Python helper). */
export const TEST_PUBLIC_KEY = 'UJTdwlAEPjMNskl1Ful7nqIP10IBbEYnOgPH41PGoIQ';
/** A second key, never listed in the trusted file (unknown-key cases). */
export const OTHER_KEY_ID = 'test-catalog-other';
export const OTHER_SEED = createHash('sha256')
  .update('tryaii catalog contract section 6 other key')
  .digest();

// PKCS#8 DER prefix of an Ed25519 private key (RFC 8410) + the 32-byte seed.
const PKCS8_PREFIX = Buffer.from('302e020100300506032b657004220420', 'hex');

export function privateKey(seed: Buffer = TEST_SEED): KeyObject {
  return createPrivateKey({ key: Buffer.concat([PKCS8_PREFIX, seed]), format: 'der', type: 'pkcs8' });
}

export function publicKeyB64(seed: Buffer = TEST_SEED): string {
  return String(createPublicKey(privateKey(seed)).export({ format: 'jwk' }).x);
}

/** Python json.dumps(sort_keys=True, separators=(",", ":")) for ASCII JSON values. */
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const o = value as Record<string, unknown>;
    return `{${Object.keys(o)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical(o[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}

/** Manifest minus signature/key_id, canonical text, UTF-8. */
export function signedMessage(manifest: Record<string, unknown>): Buffer {
  const body: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(manifest)) {
    if (k !== 'signature' && k !== 'key_id') body[k] = v;
  }
  return Buffer.from(canonical(body), 'utf-8');
}

/** Set signature + key_id on `manifest` (in place) and return it. */
export function signManifest<T extends Record<string, unknown>>(
  manifest: T,
  opts: { seed?: Buffer; keyId?: string } = {},
): T {
  const sig = cryptoSign(null, signedMessage(manifest), privateKey(opts.seed ?? TEST_SEED));
  (manifest as Record<string, unknown>).signature = sig.toString('base64url');
  (manifest as Record<string, unknown>).key_id = opts.keyId ?? TEST_KEY_ID;
  return manifest;
}

export function keyEntry(
  keyId: string = TEST_KEY_ID,
  seed: Buffer = TEST_SEED,
  env: 'development' | 'production' = 'development',
): { key_id: string; public_key: string; env: string } {
  return { key_id: keyId, public_key: publicKeyB64(seed), env };
}

/** Write a trusted-keys file (default: the test key only). */
export function writeTrustedKeys(
  path: string,
  entries: Array<Record<string, unknown>> = [keyEntry()],
): string {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify({ keys: entries }, null, 2) + '\n', 'utf-8');
  return path;
}
