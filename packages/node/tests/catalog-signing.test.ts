/**
 * Catalog signing: docs/catalog/CONTRACT-catalog-v1.md section 6.
 *
 *  - Ed25519 through node:crypto (`ed25519Verify`) against the RFC 8032
 *    section 7.1 vectors (shared/catalog/parity/rfc8032_ed25519_vectors.json,
 *    the same vectors the Python vendored verifier is tested with) and the
 *    negative cases; weak (small-order / non-canonical) trusted keys refused;
 *  - the section 6 rules of src/catalog/signing.ts: signed message, ASCII
 *    only, strict base64url, key_id format, trusted-keys file + the
 *    TRYAII_CATALOG_TRUSTED_KEYS override, the packaged list;
 *  - the loader hooks (`loadBundle` / `bundleFromTexts` with verifySignature);
 *  - the release mode of scripts/verify-dist.mjs.
 *
 * The client behaviour (download / cache / fallbacks) is in catalog-client.test.ts.
 */

import { spawnSync } from 'node:child_process';
import { createHash, sign as cryptoSign } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, describe, expect, it } from 'vitest';

import {
  BUNDLE_DATA_FILES,
  BundleIntegrityError,
  BundleSchemaError,
  BundleSignatureError,
  STARTER_BUNDLE_DIR,
  bundleFromTexts,
  loadBundle,
  starterBundle,
  verifyManifestSignature,
} from '../src/catalog/bundle.js';
import {
  PACKAGED_TRUSTED_KEYS,
  TRUSTED_KEYS_ENV,
  b64urlDecode,
  b64urlEncode,
  ed25519Verify,
  isValidKeyId,
  isValidPublicKey,
  loadTrustedKeys,
  signedMessage,
  trustedKeysPath,
} from '../src/catalog/signing.js';
import { FULL_BUNDLE_DIR, REPO_ROOT } from './_catalog.js';
import {
  OTHER_KEY_ID,
  OTHER_SEED,
  TEST_KEY_ID,
  TEST_PUBLIC_KEY,
  TEST_SEED,
  keyEntry,
  privateKey,
  publicKeyB64,
  signManifest,
  writeTrustedKeys,
} from './_signing.js';

interface Vector {
  name: string;
  secret: string;
  public: string;
  message: string;
  signature: string;
}

const VECTORS: Vector[] = JSON.parse(
  readFileSync(join(REPO_ROOT, 'shared', 'catalog', 'parity', 'rfc8032_ed25519_vectors.json'), 'utf-8'),
).vectors;
const P = (1n << 255n) - 19n;
const L = (1n << 252n) + 27742317777372353535851937790883648493n;

function le(n: bigint): Buffer {
  const b = Buffer.alloc(32);
  for (let i = 0; i < 32; i++) {
    b[i] = Number(n & 255n);
    n >>= 8n;
  }
  return b;
}

function leToBig(b: Uint8Array): bigint {
  let n = 0n;
  for (let i = b.length - 1; i >= 0; i--) n = (n << 8n) | BigInt(b[i]);
  return n;
}

function vec(name: string): [Buffer, Buffer, Buffer] {
  const t = VECTORS.find((v) => v.name === name)!;
  return [Buffer.from(t.public, 'hex'), Buffer.from(t.message, 'hex'), Buffer.from(t.signature, 'hex')];
}

function flip(data: Buffer, index: number): Buffer {
  const out = Buffer.from(data);
  out[index] ^= 1;
  return out;
}

const tmpRoots: string[] = [];
function tmp(): string {
  const d = mkdtempSync(join(tmpdir(), 'tryaii-signing-'));
  tmpRoots.push(d);
  return d;
}
afterAll(() => {
  for (const d of tmpRoots) rmSync(d, { recursive: true, force: true });
});

// ======================================================================= Ed25519
describe('Ed25519 (RFC 8032 section 7.1)', () => {
  it.each(VECTORS.map((v) => [v.name, v] as const))('%s verifies', (_name, v) => {
    expect(
      ed25519Verify(Buffer.from(v.public, 'hex'), Buffer.from(v.message, 'hex'), Buffer.from(v.signature, 'hex')),
    ).toBe(true);
  });

  it('covers TEST 1, 2, 3 and the 1023-byte TEST 1024', () => {
    expect(['TEST 1', 'TEST 2', 'TEST 3', 'TEST 1024'].map((n) => vec(n)[1].length)).toEqual([0, 1, 2, 1023]);
  });

  it.each(['TEST 1', 'TEST 2', 'TEST 3', 'TEST 1024'])('%s: a flipped bit in sig / msg / key fails', (name) => {
    const [pub, msg, sig] = vec(name);
    expect(ed25519Verify(pub, msg, flip(sig, 3))).toBe(false);
    expect(ed25519Verify(pub, msg, flip(sig, 40))).toBe(false);
    expect(ed25519Verify(pub, msg.length ? flip(msg, msg.length >> 1) : Buffer.from([0]), sig)).toBe(false);
    expect(ed25519Verify(flip(pub, 7), msg, sig)).toBe(false);
  });

  it('S >= L is rejected (malleability)', () => {
    const [pub, msg, sig] = vec('TEST 2');
    const s = leToBig(sig.subarray(32));
    expect(ed25519Verify(pub, msg, Buffer.concat([sig.subarray(0, 32), le(s + L)]))).toBe(false);
    expect(ed25519Verify(pub, msg, Buffer.concat([sig.subarray(0, 32), Buffer.alloc(32, 0xff)]))).toBe(false);
  });

  it('wrong lengths are rejected', () => {
    const [pub, msg, sig] = vec('TEST 3');
    expect(ed25519Verify(pub.subarray(0, 31), msg, sig)).toBe(false);
    expect(ed25519Verify(Buffer.concat([pub, Buffer.alloc(1)]), msg, sig)).toBe(false);
    expect(ed25519Verify(pub, msg, sig.subarray(0, 63))).toBe(false);
    expect(ed25519Verify(pub, msg, Buffer.concat([sig, Buffer.alloc(1)]))).toBe(false);
  });

  it('a non-canonical or small-order R is rejected', () => {
    const [pub, msg, sig] = vec('TEST 2');
    expect(ed25519Verify(pub, msg, Buffer.concat([le(P + 1n), sig.subarray(32)]))).toBe(false);
    expect(ed25519Verify(pub, msg, Buffer.concat([le(1n), sig.subarray(32)]))).toBe(false);
  });

  it('weak public keys (small order, non-canonical, off-curve) are refused as trusted keys', () => {
    expect(isValidPublicKey(Buffer.from(TEST_PUBLIC_KEY, 'base64url'))).toBe(true);
    for (const t of VECTORS) expect(isValidPublicKey(Buffer.from(t.public, 'hex'))).toBe(true);
    expect(isValidPublicKey(le(1n))).toBe(false); // identity
    expect(isValidPublicKey(le(P - 1n))).toBe(false); // order 2
    expect(isValidPublicKey(Buffer.alloc(32))).toBe(false); // y = 0: order 4
    expect(isValidPublicKey(le(P))).toBe(false); // y = p: non-canonical
    expect(isValidPublicKey(le(1n | (1n << 255n)))).toBe(false); // x = -0
    expect(isValidPublicKey(le(2n))).toBe(false); // not on the curve
    expect(isValidPublicKey(Buffer.alloc(31))).toBe(false);
  });

  it('the test key matches the Python helper (same seed, deterministic signatures)', () => {
    expect(publicKeyB64(TEST_SEED)).toBe(TEST_PUBLIC_KEY);
    // produced by packages/python/tests/_catalog_signing.py: b64url(sign(msg))
    const fromPython =
      'Z1Hyv88fShtUXPCkW8qkU66cBocCZx1fT4g_p3RaFhqgp6dyF27ApUDTjKLxG9_jmiZXSYXkwvfyTScLT3f5BA';
    const msg = Buffer.from('tryaii catalog contract section 6');
    expect(cryptoSign(null, msg, privateKey()).toString('base64url')).toBe(fromPython);
    expect(ed25519Verify(Buffer.from(TEST_PUBLIC_KEY, 'base64url'), msg, Buffer.from(fromPython, 'base64url'))).toBe(
      true,
    );
  });
});

// ======================================================================= section 6 rules
function manifest(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema: 1,
    kind: 'full',
    version: '2026.10.04.1',
    embedding_model: 'all-MiniLM-L6-v2',
    created_at: '2026-10-04T00:00:00Z',
    counts: { models: 3, benchmarks: 2 },
    files: { 'models.json': 'ab'.repeat(32) },
    signature: null,
    key_id: null,
    ...extra,
  };
}

const signed = (opts: { seed?: Buffer; keyId?: string } = {}) => signManifest(manifest(), opts);

describe('signed message (section 6)', () => {
  it('is the canonical manifest without signature and key_id (Python json.dumps form)', () => {
    const m = manifest();
    const expected =
      '{"counts":{"benchmarks":2,"models":3},"created_at":"2026-10-04T00:00:00Z",' +
      '"embedding_model":"all-MiniLM-L6-v2","files":{"models.json":"' + 'ab'.repeat(32) + '"},' +
      '"kind":"full","schema":1,"version":"2026.10.04.1"}';
    expect(signedMessage(m).toString('utf-8')).toBe(expected);
    const s = signed();
    expect(signedMessage(s)).toEqual(signedMessage(m));
    const reversed = Object.fromEntries(Object.entries(s).reverse());
    expect(signedMessage(reversed)).toEqual(signedMessage(m));
  });

  it.each([
    ['string value', { created_at: '2026-10-04T00:00:00Zé' }],
    ['key', { 'noteé': 'x' }],
    ['nested', { counts: { models: 3, benchmarks: 2, x: ['☃'] } }],
  ])('non-ASCII manifests are rejected (%s), even when validly signed', (_label, extra) => {
    const m = signManifest(manifest(extra));
    expect(() => signedMessage(m)).toThrow(/ASCII/);
    expect(() => verifyManifestSignature(m)).toThrow(BundleSignatureError);
  });

  it.each([1.5, 2 ** 53])('non-integer or unsafe numbers are rejected (%s)', (value) => {
    expect(() => signedMessage(manifest({ counts: { models: value, benchmarks: 2 } }))).toThrow();
  });

  it('strict base64url without padding', () => {
    const raw = Buffer.from(Array.from({ length: 64 }, (_, i) => i));
    const good = b64urlEncode(raw);
    expect(good).toHaveLength(86);
    expect(good).not.toContain('=');
    expect(b64urlDecode(good, 64)).toEqual(raw);
    expect(b64urlDecode(good + '==', 64)).toBeNull();
    expect(b64urlDecode(raw.toString('base64'), 64)).toBeNull();
    expect(b64urlDecode(good.slice(0, -1), 64)).toBeNull();
    expect(b64urlDecode(good, 63)).toBeNull();
    expect(b64urlDecode(` ${good}`, 64)).toBeNull();
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
    const alt = good.slice(0, -1) + alphabet[alphabet.indexOf(good.slice(-1)) | 1];
    expect(alt).not.toBe(good);
    expect(b64urlDecode(alt, 64)).toBeNull(); // non-zero trailing bits
    expect(b64urlDecode(null, 64)).toBeNull();
  });

  it.each([
    ['tryaii-catalog-2026-1', true], ['dev-catalog-2026-10', true], ['abc', true], ['a'.repeat(64), true],
    ['ab', false], ['a'.repeat(65), false], ['Dev-key', false], ['dev_key', false], ['dev key', false],
    ['dev-key\n', false], ['', false], [null, false],
  ])('key_id %j valid=%s', (keyId, ok) => {
    expect(isValidKeyId(keyId)).toBe(ok);
  });
});

describe('verifyManifestSignature (section 6)', () => {
  it('accepts a valid signature from a trusted key (the test key, _setup-data-dir.ts)', () => {
    expect(verifyManifestSignature(signed())).toBe(TEST_KEY_ID);
  });

  const cases: Array<[string, (m: Record<string, unknown>) => Record<string, unknown>]> = [
    ['unsigned', (m) => ({ ...m, signature: null, key_id: null })],
    ['no key_id', (m) => { const c = { ...m }; delete c.key_id; return c; }],
    ['unknown key', () => signed({ seed: OTHER_SEED, keyId: OTHER_KEY_ID })],
    ['bad signature', (m) => {
      const raw = Buffer.from(String(m.signature), 'base64url');
      raw[10] ^= 0x80;
      return { ...m, signature: raw.toString('base64url') };
    }],
    ['tampered field', (m) => ({ ...m, counts: { models: 4, benchmarks: 2 } })],
    ['bad key_id', (m) => ({ ...m, key_id: 'Test Catalog' })],
    ['bad encoding', (m) => ({ ...m, signature: `${String(m.signature)}==` })],
    ['wrong key, same key_id', () => signed({ seed: OTHER_SEED })],
  ];
  it.each(cases)('rejects: %s', (_label, change) => {
    expect(() => verifyManifestSignature(change(signed()))).toThrow(BundleSignatureError);
  });

  it('a signature error is an integrity error (handled like a hash mismatch)', () => {
    expect(new BundleSignatureError('x')).toBeInstanceOf(BundleIntegrityError);
  });

  it('TRYAII_CATALOG_TRUSTED_KEYS replaces the packaged list', () => {
    const dir = tmp();
    const m = signed();
    const otherOnly = writeTrustedKeys(join(dir, 'other.json'), [keyEntry(OTHER_KEY_ID, OTHER_SEED)]);
    expect(() => verifyManifestSignature(m, { [TRUSTED_KEYS_ENV]: otherOnly })).toThrow(/unknown key/);
    const both = writeTrustedKeys(join(dir, 'both.json'), [keyEntry(OTHER_KEY_ID, OTHER_SEED), keyEntry()]);
    expect(verifyManifestSignature(m, { [TRUSTED_KEYS_ENV]: both })).toBe(TEST_KEY_ID);
    const packaged = JSON.parse(readFileSync(PACKAGED_TRUSTED_KEYS, 'utf-8')).keys as Array<{ key_id: string }>;
    const inEffect = loadTrustedKeys({ [TRUSTED_KEYS_ENV]: both });
    for (const k of packaged) expect(inEffect.has(k.key_id)).toBe(false);
    // no override: the packaged list (which never holds the test key)
    expect(trustedKeysPath({})).toBe(PACKAGED_TRUSTED_KEYS);
    expect(() => verifyManifestSignature(m, {})).toThrow(/unknown key/);
  });

  const goodKey = TEST_PUBLIC_KEY;
  it.each([
    ['not json', 'not json'],
    ['array', '[]'],
    ['keys object', '{"keys": {}}'],
    ['entry not object', '{"keys": [1]}'],
    ['bad key_id', JSON.stringify({ keys: [{ key_id: 'BAD', public_key: goodKey, env: 'development' }] })],
    ['short key', JSON.stringify({ keys: [{ key_id: 'abc', public_key: 'AAAA', env: 'development' }] })],
    ['bad env', JSON.stringify({ keys: [{ key_id: 'abc', public_key: goodKey, env: 'staging' }] })],
    ['duplicate', JSON.stringify({ keys: [
      { key_id: 'abc', public_key: goodKey, env: 'development' },
      { key_id: 'abc', public_key: goodKey, env: 'development' }] })],
    ['identity key', JSON.stringify({ keys: [{ key_id: 'abc', public_key: le(1n).toString('base64url'), env: 'development' }] })],
    ['order-4 key', JSON.stringify({ keys: [{ key_id: 'abc', public_key: Buffer.alloc(32).toString('base64url'), env: 'development' }] })],
    ['non-canonical key', JSON.stringify({ keys: [{ key_id: 'abc', public_key: le(P).toString('base64url'), env: 'development' }] })],
  ])('a malformed trusted-keys file trusts nothing (%s)', (_label, content) => {
    const path = join(tmp(), 'keys.json');
    writeFileSync(path, content, 'utf-8');
    expect(() => verifyManifestSignature(signed(), { [TRUSTED_KEYS_ENV]: path })).toThrow(/trusted catalog keys/);
  });

  it('a missing trusted-keys file trusts nothing', () => {
    expect(() => verifyManifestSignature(signed(), { [TRUSTED_KEYS_ENV]: join(tmp(), 'missing.json') })).toThrow(
      BundleSignatureError,
    );
  });

  it('the packaged list equals the shared master and never holds the test key', () => {
    const master = join(REPO_ROOT, 'shared', 'catalog', 'trusted_keys.json');
    expect(readFileSync(PACKAGED_TRUSTED_KEYS)).toEqual(readFileSync(master));
    const keys = loadTrustedKeys({});
    expect(keys.size).toBeGreaterThan(0);
    expect(keys.has(TEST_KEY_ID)).toBe(false);
    for (const k of keys.values()) expect(k.publicKey.toString('base64url')).not.toBe(TEST_PUBLIC_KEY);
  });

  it('the starter bundle needs no signature', () => {
    const starter = starterBundle();
    expect(starter.manifest.signature ?? null).toBeNull();
    const none = writeTrustedKeys(join(tmp(), 'none.json'), []);
    expect(loadBundle(STARTER_BUNDLE_DIR, { env: { [TRUSTED_KEYS_ENV]: none } }).kind).toBe('starter');
  });
});

// ======================================================================= loader hooks
function makeBundle(dir: string, opts: { sign?: boolean } = {}): string {
  const texts: Record<string, string> = {};
  for (const name of BUNDLE_DATA_FILES) texts[name] = readFileSync(join(STARTER_BUNDLE_DIR, name), 'utf-8');
  const files: Record<string, string> = {};
  for (const name of BUNDLE_DATA_FILES) {
    files[name] = createHash('sha256').update(Buffer.from(texts[name], 'utf-8')).digest('hex');
    writeFileSync(join(dir, name), texts[name], 'utf-8');
  }
  const starter = JSON.parse(readFileSync(join(STARTER_BUNDLE_DIR, 'manifest.json'), 'utf-8'));
  const m: Record<string, unknown> = {
    schema: 1, kind: 'full', version: '2026.10.04.1', embedding_model: starter.embedding_model,
    created_at: '2026-10-04T00:00:00Z', counts: starter.counts, files, signature: null, key_id: null,
  };
  if (opts.sign !== false) signManifest(m);
  writeFileSync(join(dir, 'manifest.json'), JSON.stringify(m, null, 2) + '\n', 'utf-8');
  return dir;
}

function texts(dir: string): [Record<string, unknown>, Record<string, string>] {
  const m = JSON.parse(readFileSync(join(dir, 'manifest.json'), 'utf-8'));
  const files: Record<string, string> = {};
  for (const name of BUNDLE_DATA_FILES) files[name] = readFileSync(join(dir, name), 'utf-8');
  return [m, files];
}

describe('bundle loader hooks (section 6)', () => {
  it('verify only when asked', () => {
    const signedDir = makeBundle(tmp());
    const unsignedDir = makeBundle(tmp(), { sign: false });
    expect(loadBundle(signedDir, { verifySignature: true }).manifest.key_id).toBe(TEST_KEY_ID);
    expect(loadBundle(unsignedDir).kind).toBe('full'); // an explicit path: no signature needed
    expect(() => loadBundle(unsignedDir, { verifySignature: true })).toThrow(BundleSignatureError);
    const [m, files] = texts(signedDir);
    expect(bundleFromTexts(m, files, null, { verifySignature: true }).version).toBe('2026.10.04.1');
    m.created_at = '2026-10-04T00:00:09Z';
    expect(() => bundleFromTexts(m, files, null, { verifySignature: true })).toThrow(BundleSignatureError);
  });

  it('the signature is checked before the data files', () => {
    const dir = makeBundle(tmp(), { sign: false });
    unlinkSync(join(dir, 'models.json'));
    expect(() => loadBundle(dir, { verifySignature: true })).toThrow(BundleSignatureError);
  });

  it('the schema is checked before the signature (its own message, section 5)', () => {
    const dir = makeBundle(tmp(), { sign: false });
    const m = JSON.parse(readFileSync(join(dir, 'manifest.json'), 'utf-8'));
    m.schema = 99;
    writeFileSync(join(dir, 'manifest.json'), JSON.stringify(m), 'utf-8');
    expect(() => loadBundle(dir, { verifySignature: true })).toThrow(BundleSchemaError);
  });

  const fullManifest = join(FULL_BUNDLE_DIR, 'manifest.json');
  const fullSigned = existsSync(fullManifest) && JSON.parse(readFileSync(fullManifest, 'utf-8')).signature;
  it.skipIf(!fullSigned)(
    'build/catalog/full is signed with a packaged key, and Node computes the same message as the Python signer',
    () => {
      const m = JSON.parse(readFileSync(fullManifest, 'utf-8'));
      expect(verifyManifestSignature(m, {})).toBe(m.key_id);
    },
  );
});

// ======================================================================= release guard
const VERIFY_DIST = fileURLToPath(new URL('../scripts/verify-dist.mjs', import.meta.url));
const HAS_DIST = existsSync(fileURLToPath(new URL('../dist/catalog/data/trusted_keys.json', import.meta.url)));

describe.skipIf(!HAS_DIST)('verify-dist --release (section 6) [needs dist/: npm run build]', () => {
  it('fails while the packaged list holds a development key (or no production key)', () => {
    const dist = JSON.parse(
      readFileSync(fileURLToPath(new URL('../dist/catalog/data/trusted_keys.json', import.meta.url)), 'utf-8'),
    ) as { keys: Array<{ env: string }> };
    const releaseReady = dist.keys.length > 0 && dist.keys.every((k) => k.env === 'production');
    const r = spawnSync(process.execPath, [VERIFY_DIST, '--release'], { encoding: 'utf-8', timeout: 120_000 });
    if (releaseReady) {
      expect(r.status).toBe(0);
    } else {
      expect(r.status).not.toBe(0);
      expect(r.stderr).toContain('RELEASE KEY CHECK FAILED');
    }
  }, 120_000);
});
