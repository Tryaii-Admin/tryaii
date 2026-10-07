"""
Sync shared data into each package.

Copies files from shared/ into the bundled data directories of each package,
so each package is self-contained when distributed.

Usage:
    python scripts/sync-shared.py
"""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
SHARED = ROOT / "shared"


def pack_json(payload: dict) -> str:
    """Deterministic packed-JSON serialization (both package copies must be
    byte-identical; test_parity.py re-packs and compares)."""
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# Non-JSON masters packed INTO json so they flow through the existing
# JSON-only asset pipeline (copy-assets.mjs) unchanged:
# ([(payload key, source file), ...], destinations)
_PY_DIAGNOSE_DATA = ROOT / "packages" / "python" / "tryaii" / "diagnose" / "data"
_NODE_DIAGNOSE_DATA = ROOT / "packages" / "node" / "src" / "diagnose" / "data"
_PY_DP_DATA = ROOT / "packages" / "python" / "tryaii" / "designpartner" / "data"
_NODE_DP_DATA = ROOT / "packages" / "node" / "src" / "designpartner" / "data"

PACKS = [
    (
        [("html", SHARED / "diagnose" / "report" / "template.html")],
        [
            _PY_DIAGNOSE_DATA / "report_template.json",
            _NODE_DIAGNOSE_DATA / "report_template.json",
        ],
    ),
    (
        [
            ("skill_md", SHARED / "diagnose" / "skill" / "SKILL.md"),
            ("agents_pointer_md", SHARED / "diagnose" / "skill" / "agents-pointer.md"),
        ],
        [
            _PY_DIAGNOSE_DATA / "skill.json",
            _NODE_DIAGNOSE_DATA / "skill.json",
        ],
    ),
    (
        [
            ("skill_md", SHARED / "designpartner" / "skill" / "SKILL.md"),
            ("agents_pointer_md",
             SHARED / "designpartner" / "skill" / "agents-pointer.md"),
        ],
        [
            _PY_DP_DATA / "skill.json",
            _NODE_DP_DATA / "skill.json",
        ],
    ),
]

TARGETS = [
    # (source, destinations...)
    (
        SHARED / "cachelint" / "providers.json",
        [
            ROOT / "packages" / "python" / "tryaii" / "cachelint" / "data" / "providers.json",
            ROOT / "packages" / "node" / "src" / "cachelint" / "data" / "providers.json",
        ],
    ),
    (
        SHARED / "diagnose" / "plan.json",
        [
            ROOT / "packages" / "python" / "tryaii" / "diagnose" / "data" / "plan.json",
            ROOT / "packages" / "node" / "src" / "diagnose" / "data" / "plan.json",
        ],
    ),
    (
        SHARED / "diagnose" / "costmodel.json",
        [
            ROOT / "packages" / "python" / "tryaii" / "diagnose" / "data" / "costmodel.json",
            ROOT / "packages" / "node" / "src" / "diagnose" / "data" / "costmodel.json",
        ],
    ),
    (
        SHARED / "designpartner" / "questions.json",
        [
            _PY_DP_DATA / "questions.json",
            _NODE_DP_DATA / "questions.json",
        ],
    ),
    # Trusted public keys for catalog signatures (catalog contract section 6).
    # Released packages must list production keys only:
    # scripts/check-release-keys.py.
    (
        SHARED / "catalog" / "trusted_keys.json",
        [
            ROOT / "packages" / "python" / "tryaii" / "catalog" / "data" / "trusted_keys.json",
            ROOT / "packages" / "node" / "src" / "catalog" / "data" / "trusted_keys.json",
        ],
    ),
]

# The starter catalog bundle (docs/catalog/CONTRACT-catalog-v1.md) is the
# packages' built-in routing data: all six files of shared/catalog/starter/ are
# copied byte-for-byte (their sha256 is pinned in the bundle manifest). The FULL
# catalog is never copied into a package: it is built and signed separately and
# downloaded by logged-in users.
STARTER_BUNDLE = SHARED / "catalog" / "starter"
STARTER_DESTS = [
    ROOT / "packages" / "python" / "tryaii" / "catalog" / "data" / "starter",
    ROOT / "packages" / "node" / "src" / "catalog" / "data" / "starter",
]


def sync():
    copied = 0

    # Fixed mappings
    for source, dests in TARGETS:
        if not source.exists():
            print(f"  SKIP {source} (not found)")
            continue
        for dest in dests:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            print(f"  {source.name} -> {dest.relative_to(ROOT)}")
            copied += 1

    # Packed masters (non-JSON sources wrapped in JSON)
    for parts, dests in PACKS:
        missing = [source for _key, source in parts if not source.exists()]
        if missing:
            print(f"  SKIP {missing[0]} (not found)")
            continue
        packed = pack_json({key: source.read_text(encoding="utf-8")
                            for key, source in parts})
        names = "+".join(source.name for _key, source in parts)
        for dest in dests:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(packed)
            print(f"  {names} (packed) -> {dest.relative_to(ROOT)}")
            copied += 1

    # Starter catalog bundle (all six files, byte-for-byte)
    if STARTER_BUNDLE.exists():
        for bundle_file in sorted(STARTER_BUNDLE.glob("*.json")):
            for dest_dir in STARTER_DESTS:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / bundle_file.name
                shutil.copyfile(bundle_file, dest)
                print(f"  catalog/starter/{bundle_file.name} -> {dest.relative_to(ROOT)}")
                copied += 1
    else:
        print(f"  SKIP {STARTER_BUNDLE} (not found)")

    print(f"\nSynced {copied} files.")


if __name__ == "__main__":
    print("Syncing shared/ data into packages...\n")
    sync()
