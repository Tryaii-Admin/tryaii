"""
Sync shared data into each package.

Copies files from shared/ into the bundled data directories of each package,
so each package is self-contained when distributed.

Usage:
    python scripts/sync-shared.py
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
SHARED = ROOT / "shared"

TARGETS = [
    # (source, destinations...)
    (
        SHARED / "models" / "default_models.json",
        [
            ROOT / "packages" / "python" / "tryaii" / "registry" / "presets" / "default_models.json",
            ROOT / "packages" / "node" / "src" / "registry" / "presets" / "defaultModels.json",
        ],
    ),
    (
        SHARED / "training" / "training_queries.json",
        [
            ROOT / "packages" / "python" / "tryaii" / "centroids" / "data" / "training_queries.json",
            ROOT / "packages" / "node" / "src" / "centroids" / "data" / "trainingQueries.json",
        ],
    ),
]

# Centroids: copy all files in shared/centroids/ to both packages
CENTROID_DESTS = [
    ROOT / "packages" / "python" / "tryaii" / "centroids" / "data",
    ROOT / "packages" / "node" / "src" / "centroids" / "data",
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

    # Centroids (all files)
    centroids_dir = SHARED / "centroids"
    if centroids_dir.exists():
        for centroid_file in centroids_dir.glob("*.json"):
            for dest_dir in CENTROID_DESTS:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / centroid_file.name
                shutil.copy2(centroid_file, dest)
                print(f"  {centroid_file.name} -> {dest.relative_to(ROOT)}")
                copied += 1

    print(f"\nSynced {copied} files.")


if __name__ == "__main__":
    print("Syncing shared/ data into packages...\n")
    sync()
