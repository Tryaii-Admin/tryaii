#!/usr/bin/env python3
"""
Generate an Ed25519 catalog signing key (docs/catalog/CONTRACT-catalog-v1.md, section 6).

Writes the PRIVATE key as an unencrypted PKCS#8 PEM to ``<out>/<key_id>.pem``
(file mode 0600 where the OS supports it; never overwrites) and prints ONLY
the public key entry for ``shared/catalog/trusted_keys.json`` on stdout:

    {"key_id": "<id>", "public_key": "<base64url raw 32 bytes>", "env": "development"}

Never commit a private key. The default output directory, ``build/keys/``, is
gitignored; the script refuses an output directory inside this repository
that git does not ignore. The production key is generated and kept offline
by the key holder (``--env production``); only its public entry is added to
trusted_keys.json, at the release/deploy step.

Dev-only dependency: ``cryptography`` (not a runtime dependency of tryaii):

    pip install "cryptography>=42"

Usage:
    python scripts/gen-catalog-signing-key.py --key-id dev-catalog-2026-10
    python scripts/gen-catalog-signing-key.py --key-id tryaii-catalog-2026-1 --env production --out /secure/offline/dir
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "build" / "keys"
KEY_ID_RE = re.compile(r"[a-z0-9-]{3,64}", re.ASCII)
KEY_ENVS = ("development", "production")


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


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _inside_repo_and_not_ignored(path: Path) -> bool:
    """True when ``path`` lies inside this repository and git does NOT ignore it."""
    try:
        path.resolve().relative_to(ROOT)
    except ValueError:
        return False
    # A non-.pem probe: the *.pem rule must not count, only an ignored directory.
    probe = path / "probe"
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", str(probe)],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: be safe
    return result.returncode != 0


def write_private(path: Path, data: bytes) -> None:
    """Create ``path`` with mode 0600 (where supported); never overwrite."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)  # no-op beyond read-only on Windows
    except OSError:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key-id", required=True, help="[a-z0-9-]{3,64}, e.g. dev-catalog-2026-10")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="directory for <key_id>.pem (default: build/keys, gitignored)")
    parser.add_argument("--env", choices=KEY_ENVS, default="development",
                        help="the 'env' of the printed trusted_keys.json entry")
    args = parser.parse_args(argv)

    if not KEY_ID_RE.fullmatch(args.key_id):
        raise SystemExit(f"invalid --key-id {args.key_id!r}: must match [a-z0-9-]{{3,64}}")
    if args.env == "development" and not args.key_id.startswith("dev-"):
        raise SystemExit("a development key_id must start with 'dev-' (contract section 6)")
    if args.env == "production" and args.key_id.startswith("dev-"):
        raise SystemExit("a production key_id must not start with 'dev-'")

    out = Path(args.out)
    if _inside_repo_and_not_ignored(out):
        raise SystemExit(f"refusing to write a private key to {out}: it is inside the "
                         "repository and not gitignored (use build/keys/ or a path outside)")
    out.mkdir(parents=True, exist_ok=True)
    pem_path = out / f"{args.key_id}.pem"
    if pem_path.exists():
        raise SystemExit(f"{pem_path} already exists; refusing to overwrite a key")

    serialization, Ed25519PrivateKey = _crypto()
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    write_private(pem_path, pem)
    print(f"private key written to {pem_path} (keep it offline; never commit it)",
          file=sys.stderr)
    print(json.dumps({"key_id": args.key_id, "public_key": b64url(public), "env": args.env}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
