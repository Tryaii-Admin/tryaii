"""Test-only catalog signing (docs/catalog/CONTRACT-catalog-v1.md, section 6).

Tests and the fake servers sign their synthetic full bundles with a TEST key
they control and point ``TRYAII_CATALOG_TRUSTED_KEYS`` at a file listing it.

This module is deliberately independent of the implementation: it carries
its own small Ed25519 *signer* (RFC 8032 section 6 reference algorithm,
stdlib only) and its own canonical-text function, so the SDK's vendored
verifier is checked against a separate implementation. The test key is
derived from a fixed public seed -- it protects nothing and must never be
trusted outside tests. The Node test helper (packages/node/tests/_signing.ts)
derives the same key from the same seed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

TRUSTED_KEYS_ENV = "TRYAII_CATALOG_TRUSTED_KEYS"

TEST_KEY_ID = "test-catalog-1"
#: 32-byte Ed25519 seed (the "secret key" of RFC 8032) of the test key.
TEST_SEED = hashlib.sha256(b"tryaii catalog contract section 6 test key").digest()
#: A second test key, never listed in the trusted file (unknown-key / wrong-key cases).
OTHER_KEY_ID = "test-catalog-other"
OTHER_SEED = hashlib.sha256(b"tryaii catalog contract section 6 other key").digest()

# --- RFC 8032 section 6 reference arithmetic (signing side) ------------------
_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(p1, p2):
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % _P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % _P
    c = 2 * p1[3] * p2[3] * _D % _P
    d = 2 * p1[2] * p2[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _recover_x(y, sign):
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(p) -> bytes:
    zinv = pow(p[2], _P - 2, _P)
    x = p[0] * zinv % _P
    y = p[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _expand(seed: bytes):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _hash_mod_q(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _Q


def public_key(seed: bytes = TEST_SEED) -> bytes:
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def sign(message: bytes, seed: bytes = TEST_SEED) -> bytes:
    """Pure Ed25519 signature (RFC 8032 5.1.6)."""
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = _hash_mod_q(prefix + message)
    r_enc = _compress(_mul(r, _G))
    s = (r + _hash_mod_q(r_enc + pub + message) * a) % _Q
    return r_enc + int.to_bytes(s, 32, "little")


# --- section 6 helpers ---------------------------------------------------------
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode_any(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def signed_message(manifest: dict) -> bytes:
    """Manifest minus signature/key_id, canonical text (section 1), UTF-8."""
    body = {k: v for k, v in manifest.items() if k not in ("signature", "key_id")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_manifest(manifest: dict, *, seed: bytes = TEST_SEED,
                  key_id: str = TEST_KEY_ID) -> dict:
    """Set ``signature`` + ``key_id`` on ``manifest`` (in place) and return it."""
    manifest["signature"] = b64url(sign(signed_message(manifest), seed))
    manifest["key_id"] = key_id
    return manifest


def key_entry(key_id: str = TEST_KEY_ID, seed: bytes = TEST_SEED,
              env: str = "development") -> dict:
    return {"key_id": key_id, "public_key": b64url(public_key(seed)), "env": env}


def write_trusted_keys(path, entries=None) -> Path:
    """Write a trusted-keys file (default: the test key only)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"keys": list(entries) if entries is not None else [key_entry()]}
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path
