#!/usr/bin/env python3
"""
Sign a catalog bundle (docs/catalog/CONTRACT-catalog-v1.md, section 6).

Run by the key holder after building the full bundle:

    python scripts/sign-catalog-bundle.py build/catalog/full --key build/keys/<key_id>.pem --key-id <key_id>

What it does:

1. loads the bundle and checks every file hash against the manifest;
2. builds the signed message -- the manifest WITHOUT ``signature`` and
   ``key_id``, as canonical text, UTF-8 -- refusing a non-ASCII manifest (or
   one with a non-integer number);
3. signs it with the Ed25519 private key (PKCS#8 PEM, as written by
   ``scripts/gen-catalog-signing-key.py``);
4. rewrites ``manifest.json`` (atomically, key order preserved, 2-space
   indent, trailing newline) with ``signature`` (base64url, no padding) and
   ``key_id``;
5. re-reads the bundle and verifies the new signature with the SDK's own
   vendored verifier (``tryaii/catalog/ed25519.py``), and checks the key
   against ``shared/catalog/trusted_keys.json`` (a different public key under
   the same key_id is an error; an unlisted key_id is a warning).

Prints the version, key_id and signature (public data). Never prints the
private key.

Dev-only dependency: ``cryptography`` (not a runtime dependency of tryaii):

    pip install "cryptography>=42"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packages" / "python" / "tryaii"
TRUSTED_KEYS = ROOT / "shared" / "catalog" / "trusted_keys.json"


def _catalog_modules():
    """Import tryaii.catalog.{bundle,signing,ed25519} WITHOUT running the
    ``tryaii`` / ``tryaii.catalog`` package __init__ (which loads the engine
    and its data): stub the two parent packages with their real paths."""
    for name, path in (("tryaii", PKG), ("tryaii.catalog", PKG / "catalog")):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(path)]
            sys.modules[name] = module
    from tryaii.catalog import bundle, ed25519, signing

    return bundle, signing, ed25519


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise SystemExit(
            "this script needs the dev-only package 'cryptography': "
            'pip install "cryptography>=42"'
        ) from None
    return serialization, Ed25519PrivateKey


def manifest_text(manifest: dict) -> str:
    """Key order as loaded (the builder writes sorted keys), 2-space indent,
    trailing newline. ASCII is enforced before this is called."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def sign_bundle(bundle_dir: Path, pem: bytes, key_id: str,
                trusted_keys: Path = TRUSTED_KEYS) -> dict:
    """Sign ``bundle_dir`` in place; returns the new manifest."""
    bundle_mod, signing, ed25519 = _catalog_modules()
    serialization, Ed25519PrivateKey = _crypto()

    if not signing.is_valid_key_id(key_id):
        raise SystemExit(f"invalid --key-id {key_id!r}: must match [a-z0-9-]{{3,64}}")
    try:
        private = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise SystemExit(f"cannot read the private key: {exc}") from None
    if not isinstance(private, Ed25519PrivateKey):
        raise SystemExit("the private key is not an Ed25519 key")
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )

    # 1. hashes + schema + consistency (raises on any problem)
    try:
        bundle_mod.load_bundle(bundle_dir)
    except bundle_mod.BundleError as exc:
        raise SystemExit(f"refusing to sign an invalid bundle: {exc}") from None
    manifest_path = bundle_dir / bundle_mod.MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 2. the signed message (ASCII-only, integers only)
    try:
        message = signing.signed_message(manifest)
    except signing.BundleSignatureError as exc:
        raise SystemExit(f"refusing to sign: {exc}") from None

    # 3. + 4. sign and rewrite (keys already present keep their position)
    signature = signing.b64url_encode(private.sign(message))
    manifest["signature"] = signature
    manifest["key_id"] = key_id
    text = manifest_text(manifest)
    if not text.isascii():  # pragma: no cover -- signed_message already checked
        raise SystemExit("refusing to sign: manifest.json must be ASCII-only")
    tmp = manifest_path.with_name(f".{manifest_path.name}.signing.tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, manifest_path)

    # 5. self-verify from disk with the SDK's own verifier
    reread = json.loads(manifest_path.read_text(encoding="utf-8"))
    sig = signing.b64url_decode(reread.get("signature"), 64)
    if sig is None or reread.get("key_id") != key_id or not ed25519.verify(
            public, signing.signed_message(reread), sig):
        raise SystemExit("SELF-CHECK FAILED: the written signature does not verify")
    bundle_mod.load_bundle(bundle_dir)  # hashes still match

    if trusted_keys.is_file():
        keys = signing.parse_trusted_keys(json.loads(trusted_keys.read_text(encoding="utf-8")))
        listed = keys.get(key_id)
        if listed is None:
            print(f"WARNING: key_id {key_id!r} is not in {trusted_keys}; clients using "
                  "that list will reject this bundle", file=sys.stderr)
        elif listed["public_key"] != public:
            raise SystemExit(f"key_id {key_id!r} is listed in {trusted_keys} with a "
                             "DIFFERENT public key -- wrong private key?")
    return reread


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle_dir", help="bundle directory (e.g. build/catalog/full)")
    parser.add_argument("--key", required=True, help="Ed25519 private key, PKCS#8 PEM")
    parser.add_argument("--key-id", required=True, help="the key's id in trusted_keys.json")
    parser.add_argument("--trusted-keys", default=str(TRUSTED_KEYS),
                        help="trusted keys file to cross-check (default: shared/catalog/trusted_keys.json)")
    args = parser.parse_args(argv)

    pem = Path(args.key).read_bytes()
    manifest = sign_bundle(Path(args.bundle_dir), pem, args.key_id, Path(args.trusted_keys))
    print(f"signed {manifest.get('kind')} bundle {manifest.get('version')} "
          f"with key_id {manifest['key_id']}")
    print(f"signature {manifest['signature']}")
    print("self-check OK (vendored Ed25519 verifier)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
