"""
Bump (or verify) the version across all packages in the monorepo.

The version lives in four spots, which must always agree:
    packages/node/package.json                 -> "version"
    packages/node/package-lock.json            -> top-level "version" AND packages[""].version
    packages/python/pyproject.toml             -> [project] version = "..."
    packages/python/tryaii/__init__.py         -> __version__ = "..."

Usage:
    python scripts/bump-version.py 0.2.0          # write 0.2.0 into all four spots
    python scripts/bump-version.py --check        # exit 0 if all four agree (prints the version),
                                                  # exit 1 listing each spot and its value otherwise
    python scripts/bump-version.py --check 0.2.0  # additionally assert the agreed version is 0.2.0

Versions must be strict semver: MAJOR.MINOR.PATCH with an optional -pre suffix
(e.g. 0.2.0, 1.0.0-rc.1).

Files are rewritten in place with a targeted substitution so existing formatting
(indentation, key order, CRLF/LF line endings, trailing newline) is preserved.
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent

FILES = {
    "pyproject": ROOT / "packages" / "python" / "pyproject.toml",
    "python_init": ROOT / "packages" / "python" / "tryaii" / "__init__.py",
    "node_package": ROOT / "packages" / "node" / "package.json",
    "node_lock": ROOT / "packages" / "node" / "package-lock.json",
}

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")

# One regex per spot. Each has exactly one capture group holding the version.
PYPROJECT_RE = re.compile(r'^version = "([^"]*)"', re.MULTILINE)
INIT_RE = re.compile(r'^__version__ = "([^"]*)"', re.MULTILINE)
# package.json: the top-level "version" key (2-space indent).
PKG_RE = re.compile(r'^(  "version": )"([^"]*)"', re.MULTILINE)
# package-lock.json: top-level "version" (2-space indent) ...
LOCK_TOP_RE = re.compile(r'^(  "version": )"([^"]*)"', re.MULTILINE)
# ... and packages[""].version: the first "version" key after the `"": {` entry (6-space indent).
LOCK_ROOT_PKG_RE = re.compile(
    r'(^    "": \{\r?\n(?:      "[^"\n]*": [^\n]*\r?\n)*?      "version": )"([^"]*)"',
    re.MULTILINE,
)


def _read(path: Path) -> str:
    # newline="" keeps CRLF/LF exactly as on disk so we can write it back byte-for-byte.
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _sub_one(pattern: re.Pattern, text: str, new_version: str, label: str) -> str:
    """Replace the version inside exactly one match of `pattern`; fail loudly otherwise."""
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise SystemExit(f"error: expected exactly one match for {label}, found {len(matches)}")
    m = matches[0]
    # The version is always the last group; groups before it are preserved prefix text.
    version_group = pattern.groups
    start, end = m.span(version_group)
    return text[:start] + new_version + text[end:]


def validate_version(version: str) -> None:
    if not SEMVER_RE.match(version):
        raise SystemExit(
            f"error: invalid version {version!r}; expected MAJOR.MINOR.PATCH "
            f"with optional -pre suffix (e.g. 0.2.0 or 1.0.0-rc.1)"
        )


def read_versions() -> dict:
    """Return {spot_label: version_or_None} for all four spots (five values: lock has two)."""
    found = {}

    def grab(label, path, pattern):
        rel = str(path.relative_to(ROOT))
        if not path.exists():
            found[f"{rel} ({label})" if label else rel] = None
            return
        text = _read(path)
        ms = list(pattern.finditer(text))
        key = f"{rel} ({label})" if label else rel
        found[key] = ms[0].group(pattern.groups) if len(ms) == 1 else None

    grab("", FILES["pyproject"], PYPROJECT_RE)
    grab("", FILES["python_init"], INIT_RE)
    grab("", FILES["node_package"], PKG_RE)
    grab("top-level version", FILES["node_lock"], LOCK_TOP_RE)
    grab('packages[""].version', FILES["node_lock"], LOCK_ROOT_PKG_RE)
    return found


def check(expected: Optional[str] = None) -> int:
    found = read_versions()
    values = set(found.values())
    ok = len(values) == 1 and None not in values
    if ok and expected is not None and expected not in values:
        ok = False

    if ok:
        print(next(iter(values)))
        return 0

    if expected is not None and len(values) == 1 and None not in values:
        print(f"error: version is {next(iter(values))}, expected {expected}")
    else:
        print("error: version mismatch across the monorepo:")
    for spot, value in found.items():
        print(f"  {spot}: {value if value is not None else '<missing>'}")
    return 1


def bump(new_version: str) -> None:
    validate_version(new_version)
    updated = []

    pyproject = FILES["pyproject"]
    if pyproject.exists():
        _write(pyproject, _sub_one(PYPROJECT_RE, _read(pyproject), new_version, "pyproject version"))
        updated.append(str(pyproject.relative_to(ROOT)))

    init_file = FILES["python_init"]
    if init_file.exists():
        _write(init_file, _sub_one(INIT_RE, _read(init_file), new_version, "__version__"))
        updated.append(str(init_file.relative_to(ROOT)))

    node_pkg = FILES["node_package"]
    if node_pkg.exists():
        text = _sub_one(PKG_RE, _read(node_pkg), new_version, "package.json version")
        assert json.loads(text)["version"] == new_version
        _write(node_pkg, text)
        updated.append(str(node_pkg.relative_to(ROOT)))

    node_lock = FILES["node_lock"]
    if node_lock.exists():
        text = _read(node_lock)
        text = _sub_one(LOCK_TOP_RE, text, new_version, "package-lock.json top-level version")
        text = _sub_one(LOCK_ROOT_PKG_RE, text, new_version, 'package-lock.json packages[""].version')
        data = json.loads(text)
        assert data["version"] == new_version
        assert data["packages"][""]["version"] == new_version
        _write(node_lock, text)
        updated.append(str(node_lock.relative_to(ROOT)))

    print(f"Bumped to {new_version} in:")
    for f in updated:
        print(f"  {f}")

    if not updated:
        print("  (no files found)")


def usage() -> None:
    print("Usage: python scripts/bump-version.py <new-version>")
    print("       python scripts/bump-version.py --check [expected-version]")
    print("Example: python scripts/bump-version.py 0.2.0")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or len(args) > 2:
        usage()
        sys.exit(1)

    if args[0] == "--check":
        expected = args[1] if len(args) == 2 else None
        if expected is not None:
            validate_version(expected)
        sys.exit(check(expected))

    if len(args) != 1:
        usage()
        sys.exit(1)

    bump(args[0])
