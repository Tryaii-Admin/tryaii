"""Catalog bundles: the routing data (models, benchmarks, ranges, centroids).

See docs/catalog/CONTRACT-catalog-v1.md. The package ships the *starter*
bundle; :func:`resolve_bundle` is the single hook through which every default
consumer (Router, ModelRegistry.default, BenchmarkRegistry.default,
CentroidLoader, ...) obtains its data. A full catalog must be signed with a
trusted key (contract section 6, :mod:`tryaii.catalog.signing`).
"""

from tryaii.catalog.bundle import (
    BUNDLE_DATA_FILES,
    MANIFEST_FILE,
    STARTER_BUNDLE_DIR,
    SUPPORTED_SCHEMA,
    BundleError,
    BundleIntegrityError,
    BundleSchemaError,
    CatalogBundle,
    bundle_from_texts,
    canonical_json,
    load_bundle,
    resolve_bundle,
    sha256_hex,
    starter_bundle,
)
from tryaii.catalog.client import (
    CATALOG_MODES,
    CatalogError,
    CatalogSelection,
    LoginRequiredError,
    SessionEndedError,
    select_catalog,
)
from tryaii.catalog.signing import (
    TRUSTED_KEYS_ENV,
    BundleSignatureError,
    verify_manifest_signature,
)

__all__ = [
    "BUNDLE_DATA_FILES",
    "MANIFEST_FILE",
    "STARTER_BUNDLE_DIR",
    "SUPPORTED_SCHEMA",
    "BundleError",
    "BundleIntegrityError",
    "BundleSchemaError",
    "BundleSignatureError",
    "TRUSTED_KEYS_ENV",
    "verify_manifest_signature",
    "CatalogBundle",
    "bundle_from_texts",
    "canonical_json",
    "load_bundle",
    "resolve_bundle",
    "sha256_hex",
    "starter_bundle",
    "CATALOG_MODES",
    "CatalogError",
    "CatalogSelection",
    "LoginRequiredError",
    "SessionEndedError",
    "select_catalog",
]
