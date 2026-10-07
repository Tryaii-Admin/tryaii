#!/usr/bin/env python3
"""
Release guard for the catalog signing keys (docs/catalog/CONTRACT-catalog-v1.md, section 6).

A released package must trust ONLY production keys. This check FAILS (exit 1)
when ``shared/catalog/trusted_keys.json`` or a packaged copy of it

* is missing or malformed,
* contains a ``development`` key, or
* contains no ``production`` key,

or when the packaged copies differ from the shared master.

The committed list must hold production keys only. To exercise the SDKs
against development-signed catalogs, point ``TRYAII_CATALOG_TRUSTED_KEYS`` at
a local key list instead of editing the committed one. Rotating the production
key: generate it offline (``scripts/gen-catalog-signing-key.py --env
production``), add the printed entry to ``shared/catalog/trusted_keys.json``,
run ``scripts/sync-shared.py``, and re-sign the full catalog. It is wired into the release workflow
(.github/workflows/release.yml) and into ``npm run verify:release``; it is
deliberately NOT part of the default test runs.

Usage:
    python scripts/check-release-keys.py            # shared master + both package copies
    python scripts/check-release-keys.py FILE...     # specific files (e.g. a built dist copy)
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "shared" / "catalog" / "trusted_keys.json"
PACKAGED = (
    ROOT / "packages" / "python" / "tryaii" / "catalog" / "data" / "trusted_keys.json",
    ROOT / "packages" / "node" / "src" / "catalog" / "data" / "trusted_keys.json",
)
_KEY_ID_RE = re.compile(r"[a-z0-9-]{3,64}", re.ASCII)
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)


def _public_key_ok(value) -> bool:
    if not isinstance(value, str) or not _B64URL_RE.fullmatch(value):
        return False
    try:
        raw = base64.urlsafe_b64decode(value + "=")
    except ValueError:
        return False
    return len(raw) == 32 and base64.urlsafe_b64encode(raw).decode().rstrip("=") == value


def check_file(path: Path) -> list[str]:
    """Release problems of one trusted-keys file (empty list = OK)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{path}: cannot read: {exc}"]
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return [f"{path}: must be an object with a 'keys' array"]
    errors: list[str] = []
    production = 0
    for entry in keys:
        if not isinstance(entry, dict):
            errors.append(f"{path}: an entry is not an object")
            continue
        key_id = entry.get("key_id")
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            errors.append(f"{path}: invalid key_id {key_id!r}")
        if not _public_key_ok(entry.get("public_key")):
            errors.append(f"{path}: {key_id}: public_key is not base64url of 32 bytes")
        env = entry.get("env")
        if env == "development":
            errors.append(f"{path}: contains the DEVELOPMENT key {key_id!r} "
                          "(a release must ship production keys only)")
        elif env == "production":
            production += 1
        else:
            errors.append(f"{path}: {key_id}: env must be production or development, got {env!r}")
    if production == 0:
        errors.append(f"{path}: has no production key")
    return errors


def check(paths=None) -> list[str]:
    """Problems with the master + packaged copies (or the given files)."""
    if paths:
        errors: list[str] = []
        for path in paths:
            errors += check_file(Path(path))
        return errors
    errors = check_file(MASTER)
    master = MASTER.read_bytes() if MASTER.is_file() else None
    for copy in PACKAGED:
        if not copy.is_file():
            errors.append(f"{copy}: missing (run scripts/sync-shared.py)")
        elif master is not None and copy.read_bytes() != master:
            errors.append(f"{copy}: differs from {MASTER} (run scripts/sync-shared.py)")
    return errors


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    errors = check(argv)
    if errors:
        print("RELEASE KEY CHECK FAILED (catalog contract section 6):")
        for line in errors:
            print(f"  {line}")
        print("The production key is added at the deploy step: see the docstring of "
              "scripts/check-release-keys.py.")
        return 1
    print("OK: trusted catalog keys are production-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
