"""
Bump version across all packages in the monorepo.

Usage:
    python scripts/bump-version.py 0.2.0
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

FILES = {
    "pyproject": ROOT / "packages" / "python" / "pyproject.toml",
    "python_init": ROOT / "packages" / "python" / "tryaii" / "__init__.py",
    "node_package": ROOT / "packages" / "node" / "package.json",
}


def bump(new_version: str):
    updated = []

    # Python pyproject.toml
    pyproject = FILES["pyproject"]
    if pyproject.exists():
        text = pyproject.read_text()
        text = re.sub(r'version = "[^"]*"', f'version = "{new_version}"', text, count=1)
        pyproject.write_text(text)
        updated.append(str(pyproject.relative_to(ROOT)))

    # Python __init__.py
    init_file = FILES["python_init"]
    if init_file.exists():
        text = init_file.read_text()
        text = re.sub(r'__version__ = "[^"]*"', f'__version__ = "{new_version}"', text)
        init_file.write_text(text)
        updated.append(str(init_file.relative_to(ROOT)))

    # Node package.json
    node_pkg = FILES["node_package"]
    if node_pkg.exists():
        data = json.loads(node_pkg.read_text())
        data["version"] = new_version
        node_pkg.write_text(json.dumps(data, indent=2) + "\n")
        updated.append(str(node_pkg.relative_to(ROOT)))

    print(f"Bumped to {new_version} in:")
    for f in updated:
        print(f"  {f}")

    if not updated:
        print("  (no files found)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/bump-version.py <new-version>")
        print("Example: python scripts/bump-version.py 0.2.0")
        sys.exit(1)

    bump(sys.argv[1])
