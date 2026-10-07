"""
Catalog signatures (docs/catalog/CONTRACT-catalog-v1.md, section 6).

A full catalog is trusted only when its manifest carries a ``signature`` made
with a key the tryaii team holds offline. The signed message is the manifest
WITHOUT its ``signature`` and ``key_id`` keys, as canonical text (section 1:
``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``)
encoded as UTF-8. Manifests must be ASCII-only so the Python and Node
canonical forms are byte-identical; numbers must be integers for the same
reason (a float's text differs between the two runtimes).

* ``signature``: base64url without padding of the 64-byte Ed25519 signature.
* ``key_id``: ``[a-z0-9-]{3,64}``, names a key in the trusted list.
* Trusted keys: the packaged ``data/trusted_keys.json``
  ``{"keys": [{"key_id", "public_key" (base64url raw 32 bytes), "env"}]}``.
  ``TRYAII_CATALOG_TRUSTED_KEYS`` (path to a file of the same shape) REPLACES
  the packaged list -- for development and tests only.

Verification uses the vendored verify-only Ed25519 (:mod:`tryaii.catalog.ed25519`),
so there is no new runtime dependency. Every failure raises
:class:`BundleSignatureError`, a :class:`BundleIntegrityError`, so callers
treat it exactly like a hash mismatch. Mirrors ``packages/node/src/catalog/signing.ts``.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from tryaii.catalog.bundle import BundleIntegrityError, canonical_json

#: Environment variable naming a trusted-keys file that REPLACES the packaged
#: list (development and tests only).
TRUSTED_KEYS_ENV = "TRYAII_CATALOG_TRUSTED_KEYS"

#: The packaged trusted public keys (a copy of shared/catalog/trusted_keys.json).
PACKAGED_TRUSTED_KEYS = Path(__file__).parent / "data" / "trusted_keys.json"

#: Manifest keys left out of the signed message.
UNSIGNED_KEYS = ("signature", "key_id")

KEY_ENVS = ("production", "development")

_KEY_ID_RE = re.compile(r"[a-z0-9-]{3,64}", re.ASCII)
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_MAX_SAFE_INT = 2**53 - 1


class BundleSignatureError(BundleIntegrityError):
    """A full catalog's signature is missing, from an unknown key, or invalid."""


def is_valid_key_id(value: Any) -> bool:
    return isinstance(value, str) and _KEY_ID_RE.fullmatch(value) is not None


def b64url_encode(data: bytes) -> str:
    """base64url without padding."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: Any, length: int) -> Optional[bytes]:
    """Strict base64url-without-padding decode of exactly ``length`` bytes,
    or None. Only the canonical encoding is accepted (no padding, no
    whitespace, no non-zero trailing bits) -- same rule as the Node SDK."""
    if not isinstance(text, str) or _B64URL_RE.fullmatch(text) is None:
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        return None
    if len(data) != length or b64url_encode(data) != text:
        return None
    return data


def _check_numbers(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise BundleSignatureError("manifest.json has an integer too large to sign")
        return
    if isinstance(value, float):
        raise BundleSignatureError("manifest.json has a non-integer number (cannot be signed)")
    if isinstance(value, Mapping):
        for item in value.values():
            _check_numbers(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_numbers(item)
        return
    raise BundleSignatureError(f"manifest.json has an unsupported value: {type(value).__name__}")


def signed_message(manifest: Mapping[str, Any]) -> bytes:
    """The exact bytes a manifest's signature covers (contract section 6).

    Raises :class:`BundleSignatureError` for a non-ASCII manifest (any key or
    string value) or a non-integer number."""
    if not isinstance(manifest, Mapping):
        raise BundleSignatureError("manifest.json must be a JSON object")
    body = {k: v for k, v in manifest.items() if k not in UNSIGNED_KEYS}
    _check_numbers(body)
    text = canonical_json(body)
    if not text.isascii():
        raise BundleSignatureError("manifest.json must be ASCII-only to be signed or verified")
    return text.encode("utf-8")


def parse_trusted_keys(data: Any) -> dict[str, dict]:
    """``{key_id: {"public_key": <32 bytes>, "env": ...}}`` from a parsed
    trusted-keys document. Raises :class:`ValueError` when the document (or
    any entry) is malformed -- a broken list trusts nothing."""
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise ValueError("trusted keys file must be an object with a 'keys' array")
    keys: dict[str, dict] = {}
    for entry in data["keys"]:
        if not isinstance(entry, dict):
            raise ValueError("trusted keys: every entry must be an object")
        key_id = entry.get("key_id")
        if not is_valid_key_id(key_id):
            raise ValueError(f"trusted keys: invalid key_id {key_id!r}")
        public = b64url_decode(entry.get("public_key"), 32)
        if public is None:
            raise ValueError(f"trusted keys: {key_id}: public_key is not base64url of 32 bytes")
        from tryaii.catalog import ed25519

        if not ed25519.is_valid_public_key(public):
            # a non-canonical or small-order key would let anyone "sign"
            raise ValueError(f"trusted keys: {key_id}: public_key is not a valid Ed25519 key")
        if entry.get("env") not in KEY_ENVS:
            raise ValueError(f"trusted keys: {key_id}: env must be one of {', '.join(KEY_ENVS)}")
        if key_id in keys:
            raise ValueError(f"trusted keys: {key_id} is listed twice")
        keys[key_id] = {"public_key": public, "env": entry["env"]}
    return keys


def trusted_keys_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """The trusted-keys file in effect: ``TRYAII_CATALOG_TRUSTED_KEYS`` when
    set (non-empty), else the packaged list."""
    env = os.environ if env is None else env
    override = env.get(TRUSTED_KEYS_ENV)
    return Path(override) if override else PACKAGED_TRUSTED_KEYS


def load_trusted_keys(env: Optional[Mapping[str, str]] = None) -> dict[str, dict]:
    """The trusted keys in effect (see :func:`trusted_keys_path`). Raises
    :class:`BundleSignatureError` when the file is missing or malformed."""
    path = trusted_keys_path(env)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return parse_trusted_keys(data)
    except (OSError, ValueError) as exc:
        raise BundleSignatureError(f"cannot read the trusted catalog keys: {exc}") from exc


def verify_manifest_signature(manifest: Mapping[str, Any],
                              env: Optional[Mapping[str, str]] = None) -> str:
    """Check a manifest's ``signature`` against the trusted keys (contract
    section 6). Returns the ``key_id`` on success; raises
    :class:`BundleSignatureError` when the signature is missing, the key is
    unknown, the encoding is wrong, the manifest is not ASCII, or the
    signature does not verify."""
    if not isinstance(manifest, Mapping):
        raise BundleSignatureError("manifest.json must be a JSON object")
    signature = manifest.get("signature")
    key_id = manifest.get("key_id")
    if signature is None or key_id is None:
        raise BundleSignatureError("the catalog is not signed")
    if not is_valid_key_id(key_id):
        raise BundleSignatureError("manifest.json has an invalid key_id")
    sig = b64url_decode(signature, 64)
    if sig is None:
        raise BundleSignatureError("manifest.json signature is not base64url of 64 bytes")
    message = signed_message(manifest)
    trusted = load_trusted_keys(env)
    key = trusted.get(key_id)
    if key is None:
        raise BundleSignatureError(f"the catalog is signed with an unknown key ({key_id})")
    from tryaii.catalog import ed25519  # deferred: only the full-catalog path verifies

    if not ed25519.verify(key["public_key"], message, sig):
        raise BundleSignatureError("the catalog signature does not verify")
    return key_id
