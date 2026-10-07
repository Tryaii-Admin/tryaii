"""Shared fixtures: the catalog bundles the tests route on.

The package ships only the *starter* catalog bundle. Tests that pin
full-catalog behaviour load the full bundle from ``build/catalog/full`` (built
by maintainers from maintainer-only inputs; never committed, never packaged)
and skip with a clear reason when it is absent.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Catalog contract section 5: Router() / ModelRegistry.default() pick the
# catalog from the data dir (credentials + catalog cache). Never let the
# developer's real ~/.tryaii (a logged-in session, a downloaded full catalog)
# leak into the suite: unless a test sets its own, every test and every CLI
# subprocess sees an empty temp data dir -> the starter catalog.
if not os.environ.get("TRYAII_DRE_DATA_DIR"):
    os.environ["TRYAII_DRE_DATA_DIR"] = tempfile.mkdtemp(prefix="tryaii-tests-")

# Catalog contract section 6: a full catalog is accepted only when signed by a
# trusted key. The synthetic full bundles of the tests (fake_auth_server.
# make_full_bundle) are signed with the TEST key of tests/_catalog_signing.py;
# trust exactly that key unless a test (or the caller) sets its own list.
# Tests of the packaged list delete the variable with monkeypatch.
if not os.environ.get("TRYAII_CATALOG_TRUSTED_KEYS"):
    from tests._catalog_signing import write_trusted_keys

    os.environ["TRYAII_CATALOG_TRUSTED_KEYS"] = str(write_trusted_keys(
        Path(tempfile.mkdtemp(prefix="tryaii-test-keys-")) / "trusted_keys.json"))

REPO_ROOT = Path(__file__).resolve().parents[3]
FULL_BUNDLE_DIR = REPO_ROOT / "build" / "catalog" / "full"
FULL_BUNDLE_SKIP = (
    "full catalog bundle not present (build/catalog/full) -- full-catalog tests "
    "need maintainer-only build inputs and skip without them"
)


_FULL = None


def load_full_bundle():
    """The full catalog bundle (loaded once), or pytest.skip when not built."""
    global _FULL
    from tryaii.catalog import load_bundle

    if _FULL is None:
        if not (FULL_BUNDLE_DIR / "manifest.json").is_file():
            pytest.skip(FULL_BUNDLE_SKIP)
        _FULL = load_bundle(FULL_BUNDLE_DIR)
    return _FULL


@pytest.fixture(scope="session")
def full_bundle():
    return load_full_bundle()


@pytest.fixture(scope="session")
def starter_bundle():
    from tryaii.catalog import starter_bundle as _starter

    return _starter()
