# Contributing to TryAii

## Development Setup

### Python package

```bash
cd packages/python
pip install -e ".[dev]"
pytest tests/ -v
```

### Node package

```bash
cd packages/node
npm install
npm test
```

## Shared Data

The `shared/` directory is the single source of truth for data and specs that
both SDKs must agree on. The routing data reaches the SDKs as **catalog
bundles** (`docs/catalog/CONTRACT-catalog-v1.md`):

- `shared/catalog/starter/` — the starter catalog bundle (models, benchmark
  taxonomy, normalization ranges, training queries, pre-computed centroids)
  that ships in both packages.
- `shared/catalog/trusted_keys.json` — the public keys a downloaded full
  catalog must be signed with (catalog contract, section 6).

After changing anything under `shared/`, run `python scripts/sync-shared.py`
to copy it into each package; parity tests fail if a package copy drifts.

Maintainers regenerate the starter catalog and publish the full catalog; you
don't need to (and can't) rebuild them from this repository. Tests that
exercise the full catalog itself skip automatically when its maintainer-only
build inputs are not present, so the regular test suites run green on a fresh
clone.

### Catalog signing

The SDKs accept a full catalog only when its manifest is signed (Ed25519) with
a key listed in `shared/catalog/trusted_keys.json`; the tryaii team holds the
signing key offline. A released package must contain only `production` keys:
`python scripts/check-release-keys.py` and `npm run verify:release` (both run
by the release workflow) enforce this. `TRYAII_CATALOG_TRUSTED_KEYS=<file>`
replaces the built-in list (development and tests only); the test suites use it
to trust a throwaway test key.

## Running Tests

```bash
# Python
cd packages/python
pytest tests/ -v

# Node
cd packages/node
npm test
```

A faster subset skips the slow suites (black-box contract suites, end-to-end
CLI flows, cross-CLI parity) and keeps the shared-fixture parity cases:

```bash
pytest tests/ -m "not slow"   # Python: tests marked @pytest.mark.slow are skipped
npm run test:light            # Node: SLOW_TEST_FILES in vitest.light.config.ts are skipped
```

CI runs on pull requests into `main` (and by hand from the Actions tab), only
when the package's files change. The development repository runs this light
subset on one Python and one Node version; add the `full-ci` label to the pull
request, or run the workflow by hand with `full: true`, for the full matrix.
The public repository always runs the full matrix and the publish checks.

## Pull Requests

- One PR per feature/fix
- Include tests for new functionality
- Run the test suite before submitting
- Update CHANGELOG.md for user-facing changes
