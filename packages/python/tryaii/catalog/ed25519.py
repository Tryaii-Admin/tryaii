"""
Verify-only Ed25519 (RFC 8032, "pure" Ed25519 -- no prehash, no context).

Vendored so the SDK can check catalog signatures (docs/catalog/CONTRACT-catalog-v1.md,
section 6) without a new runtime dependency. Pure Python, standard library
only (``hashlib.sha512``). Based on the reference implementation in RFC 8032
section 6, with the decoding checks of section 5.1.3 and the verification
procedure of section 5.1.7:

* a public key or ``R`` whose ``y`` coordinate is not canonical (``y >= p``),
  that does not decode to a curve point, or that encodes ``x = 0`` with the
  sign bit set, is rejected;
* ``S`` must be canonical: ``0 <= S < L`` (rejects the malleable ``S + L``);
* a public key or ``R`` of small order (order dividing the cofactor 8, i.e.
  the identity and the 7 other torsion points) is rejected -- RFC 8032 lets a
  verifier accept those, but no honest signer produces them;
* the check is the cofactorless group equation ``[S]B == R + [k]A`` with
  ``k = SHA-512(R || A || M) mod L`` (RFC 8032 allows either form; this is the
  one the reference code and OpenSSL use), evaluated as ``[S]B + [k](-A) == R``
  with one shared doubling chain (Straus' trick, 4-bit windows) -- about twice
  as fast as two separate scalar multiplications.

There is no signing code here on purpose: the SDK only ever verifies.
Not constant time -- verification handles only public data.
"""

from __future__ import annotations

import hashlib

__all__ = ["verify", "is_valid_public_key", "PUBLIC_KEY_LENGTH", "SIGNATURE_LENGTH"]

PUBLIC_KEY_LENGTH = 32
SIGNATURE_LENGTH = 64

# Curve constants (RFC 8032 section 5.1).
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# Points are extended homogeneous coordinates (X, Y, Z, T), x = X/Z,
# y = Y/Z, x*y = T/Z.
_IDENTITY = (0, 1, 1, 0)


def _recover_x(y: int, sign: int):
    """x for a given y and sign bit, or None (RFC 8032 5.1.3 steps 2-4)."""
    if y >= _P:
        return None  # non-canonical encoding
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None  # not on the curve
    if (x & 1) != sign:
        x = _P - x
    return x


def _decompress(data: bytes):
    """Decode a 32-byte point encoding, or None when it is invalid."""
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _add(p1, p2):
    """Point addition (RFC 8032 section 6 reference formulas)."""
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % _P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % _P
    c = 2 * p1[3] * p2[3] * _D % _P
    d = 2 * p1[2] * p2[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _double(p):
    """Point doubling (dedicated formula, RFC 8032 section 5.1.4)."""
    a = p[0] * p[0] % _P
    b = p[1] * p[1] % _P
    c = 2 * p[2] * p[2] % _P
    h = a + b
    e = h - (p[0] + p[1]) * (p[0] + p[1]) % _P
    g = a - b
    f = c + g
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _neg(p):
    return ((_P - p[0]) % _P, p[1], p[2], (_P - p[3]) % _P)


def _table(p):
    """``[0]P .. [15]P`` for the 4-bit windows of :func:`_double_mul`."""
    table = [_IDENTITY, p]
    for _ in range(14):
        table.append(_add(table[-1], p))
    return table


def _double_mul(s: int, table_p, k: int, table_q):
    """``[s]P + [k]Q`` (Straus / Shamir: one shared doubling chain, 4-bit
    fixed windows) given the ``_table`` of P and Q. ``s`` and ``k`` are
    below 2**256."""
    acc = _IDENTITY
    for shift in range(252, -1, -4):
        acc = _double(_double(_double(_double(acc))))
        ws = (s >> shift) & 15
        if ws:
            acc = _add(acc, table_p[ws])
        wk = (k >> shift) & 15
        if wk:
            acc = _add(acc, table_q[wk])
    return acc


def _equal(p1, p2) -> bool:
    # x1/z1 == x2/z2 and y1/z1 == y2/z2
    if (p1[0] * p2[2] - p2[0] * p1[2]) % _P != 0:
        return False
    return (p1[1] * p2[2] - p2[1] * p1[2]) % _P == 0


def _is_small_order(p) -> bool:
    """True when [8]P is the identity (P lies in the torsion subgroup)."""
    q = _double(_double(_double(p)))
    return _equal(q, _IDENTITY)


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)
_G_TABLE = _table(_G)


def is_valid_public_key(public_key: bytes) -> bool:
    """True when ``public_key`` is a canonical encoding of a curve point that
    is not of small order -- the checks :func:`verify` applies to ``A``.
    Used to refuse weak keys in a trusted-keys list."""
    if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != PUBLIC_KEY_LENGTH:
        return False
    point = _decompress(bytes(public_key))
    return point is not None and not _is_small_order(point)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True iff ``signature`` is a valid Ed25519 signature of ``message``
    under ``public_key`` (RFC 8032 section 5.1.7). Never raises for bad
    input: wrong lengths or types simply return False."""
    if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != PUBLIC_KEY_LENGTH:
        return False
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != SIGNATURE_LENGTH:
        return False
    if not isinstance(message, (bytes, bytearray)):
        return False
    public_key = bytes(public_key)
    signature = bytes(signature)
    a = _decompress(public_key)
    if a is None or _is_small_order(a):
        return False
    r_bytes = signature[:32]
    r = _decompress(r_bytes)
    if r is None or _is_small_order(r):
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    digest = hashlib.sha512(r_bytes + public_key + bytes(message)).digest()
    k = int.from_bytes(digest, "little") % _L
    # [S]B == R + [k]A  <=>  [S]B + [k](-A) == R
    return _equal(_double_mul(s, _G_TABLE, k, _table(_neg(a))), r)
