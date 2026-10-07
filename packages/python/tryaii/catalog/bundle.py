"""
Catalog bundle loader (contract: docs/catalog/CONTRACT-catalog-v1.md, section 1).

A bundle is the complete routing data the engine runs on -- models, the
benchmark taxonomy, normalization ranges, classifier centroids and the
training queries the centroids were built from. The package ships one bundle
(the *starter* catalog, ``tryaii/catalog/data/starter/``); a *full* bundle has
the same six files and is loaded the same way.

Integrity rules (all enforced by :func:`load_bundle` / :func:`bundle_from_texts`):

* every one of the five data files listed in ``BUNDLE_DATA_FILES`` must be
  present, and ``sha256`` of its exact UTF-8 bytes must equal
  ``manifest["files"][name]`` -- the bytes are hashed as read, never
  re-serialized;
* ``manifest["schema"]`` must be an integer no greater than
  :data:`SUPPORTED_SCHEMA` (a newer schema raises :class:`BundleSchemaError`);
* the benchmark name sets of ``benchmarks.json``, ``normalization_ranges.json``,
  ``centroids.json`` and ``training_queries.json`` must be identical, and the
  centroids must be built for ``manifest["embedding_model"]``;
* with ``verify_signature=True`` (the full catalog: downloaded or loaded from
  the cache) the manifest's Ed25519 signature must verify against a trusted
  key (contract section 6, :mod:`tryaii.catalog.signing`) -- checked after the
  schema and before any data file is hashed or parsed. The packaged starter
  bundle is not required to be signed.

This module is pure data: it imports nothing from the engine, so the scoring /
registry / classifier modules can derive their tables from a bundle at import
time without import cycles. Mirrors ``packages/node/src/catalog/bundle.ts``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

#: Highest manifest ``schema`` this SDK understands.
SUPPORTED_SCHEMA = 1

MANIFEST_FILE = "manifest.json"

#: The five data files of a bundle (``manifest.json`` is the sixth).
BUNDLE_DATA_FILES: tuple[str, ...] = (
    "models.json",
    "benchmarks.json",
    "normalization_ranges.json",
    "centroids.json",
    "training_queries.json",
)

BUNDLE_KINDS = ("starter", "full")

#: Where the packaged starter bundle lives.
STARTER_BUNDLE_DIR = Path(__file__).parent / "data" / "starter"


class BundleError(Exception):
    """A catalog bundle is missing, malformed or inconsistent."""


class BundleIntegrityError(BundleError):
    """A bundle file is missing or its sha256 does not match the manifest."""


class BundleSchemaError(BundleError):
    """The bundle's manifest ``schema`` is newer than this SDK supports."""


def canonical_json(obj: Any) -> str:
    """Canonical bundle file text (contract section 1).

    ``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
    -- UTF-8 when encoded, no trailing newline.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CatalogBundle:
    """One loaded, verified catalog bundle.

    The parsed JSON documents are exposed as-is (``models``, ``benchmarks``,
    ``normalization_ranges``, ``centroids``, ``training_queries``); the
    accessor methods below derive the per-benchmark tables the engine reads.
    Treat every attribute as read-only -- registries copy what they need.
    """

    manifest: dict
    models: dict
    benchmarks: dict
    normalization_ranges: dict
    centroids: dict
    training_queries: dict
    directory: Optional[Path] = None

    # ------------------------------------------------------------ manifest
    @property
    def schema(self) -> int:
        return int(self.manifest["schema"])

    @property
    def kind(self) -> str:
        return str(self.manifest["kind"])

    @property
    def version(self) -> str:
        return str(self.manifest["version"])

    @property
    def embedding_model(self) -> str:
        return str(self.manifest["embedding_model"])

    @property
    def counts(self) -> dict:
        return dict(self.manifest.get("counts") or {})

    @property
    def full_counts(self) -> Optional[dict]:
        full = self.manifest.get("full_counts")
        return dict(full) if full is not None else None

    # ---------------------------------------------------------- benchmarks
    @property
    def benchmark_entries(self) -> list[dict]:
        """``benchmarks.json`` entries, in display order."""
        return list(self.benchmarks["benchmarks"])

    @property
    def benchmark_names(self) -> list[str]:
        return [entry["name"] for entry in self.benchmarks["benchmarks"]]

    def benchmark_weights(self) -> dict[str, float]:
        """``{name: importance weight}`` -- the engine's BENCHMARK_WEIGHTS."""
        return {e["name"]: float(e["weight"]) for e in self.benchmarks["benchmarks"]}

    def random_chance_floors(self) -> dict[str, float]:
        """``{name: floor}`` for benchmarks that declare a random-chance floor."""
        return {
            e["name"]: e["random_chance_floor"]
            for e in self.benchmarks["benchmarks"]
            if e.get("random_chance_floor") is not None
        }

    def benchmark_categories(self) -> dict[str, tuple[str, str]]:
        """``{name: (broad_category, primary subcategory)}`` for the classifier."""
        return {
            e["name"]: (
                e["broad_category"],
                e["subcategories"][0] if e.get("subcategories") else "GENERAL",
            )
            for e in self.benchmarks["benchmarks"]
        }

    def range_entries(self) -> dict[str, dict]:
        """``normalization_ranges.json`` ``benchmarks`` map (lo/hi/description...)."""
        return dict(self.normalization_ranges["benchmarks"])

    def training_query_map(self) -> dict[str, list[str]]:
        """``{benchmark: [queries]}`` from ``training_queries.json``."""
        return {
            name: list(entry["queries"])
            for name, entry in self.training_queries["benchmarks"].items()
        }

    def model_entries(self) -> list[dict]:
        """``models.json`` ``models`` array (raw dicts, catalog order)."""
        return list(self.models.get("models", []))

    def __repr__(self) -> str:  # keep the giant documents out of reprs
        return (
            f"CatalogBundle(kind={self.kind!r}, version={self.version!r}, "
            f"models={len(self.model_entries())}, benchmarks={len(self.benchmark_names)})"
        )


def _parse_manifest(manifest: Any) -> dict:
    if not isinstance(manifest, dict):
        raise BundleError("manifest.json must be a JSON object")
    schema = manifest.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
        raise BundleError(f"manifest.json has an invalid schema: {schema!r}")
    if schema > SUPPORTED_SCHEMA:
        raise BundleSchemaError(
            f"catalog bundle schema {schema} is newer than this tryaii version "
            f"supports (max {SUPPORTED_SCHEMA})"
        )
    if manifest.get("kind") not in BUNDLE_KINDS:
        raise BundleError(f"manifest.json has an invalid kind: {manifest.get('kind')!r}")
    for key in ("version", "embedding_model"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise BundleError(f"manifest.json is missing {key!r}")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BundleError("manifest.json is missing the 'files' map")
    missing = [name for name in BUNDLE_DATA_FILES if not isinstance(files.get(name), str)]
    if missing:
        raise BundleError(f"manifest.json 'files' is missing: {', '.join(missing)}")
    return manifest


def _check_consistency(bundle: CatalogBundle) -> None:
    names = bundle.benchmark_names
    if len(set(names)) != len(names):
        raise BundleError("benchmarks.json lists a benchmark name twice")
    expected = set(names)
    others = {
        "normalization_ranges.json": set(bundle.normalization_ranges.get("benchmarks", {})),
        "centroids.json": set(bundle.centroids.get("centroids", {})),
        "training_queries.json": set(bundle.training_queries.get("benchmarks", {})),
    }
    for label, got in others.items():
        if got != expected:
            raise BundleError(
                f"{label} benchmark set does not match benchmarks.json "
                f"(only in {label}: {sorted(got - expected)}, "
                f"missing: {sorted(expected - got)})"
            )
    model = (bundle.centroids.get("metadata") or {}).get("model")
    if model != bundle.embedding_model:
        raise BundleError(
            f"centroids.json was built for {model!r}, manifest says "
            f"{bundle.embedding_model!r}"
        )
    if not isinstance(bundle.models.get("models"), list):
        raise BundleError("models.json has no 'models' array")


def _verify_signature(manifest: dict, env: Optional[Mapping[str, str]]) -> None:
    from tryaii.catalog.signing import verify_manifest_signature

    verify_manifest_signature(manifest, env)


def bundle_from_texts(
    manifest: Mapping[str, Any],
    files: Mapping[str, str | bytes],
    directory: Optional[Path] = None,
    *,
    verify_signature: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> CatalogBundle:
    """Verify and parse a bundle given as a manifest plus raw file texts.

    ``files`` maps each data file name to its exact text (``str``, hashed as
    UTF-8) or bytes, i.e. the wire format of ``GET /v1/catalog/live``; any
    other value type is rejected. Raises :class:`BundleSchemaError`,
    :class:`BundleIntegrityError` (``BundleSignatureError`` included) or
    :class:`BundleError` (also for a document whose hash matches but whose
    shape is wrong). ``verify_signature`` requires a valid signature from a
    trusted key (``env`` = where ``TRYAII_CATALOG_TRUSTED_KEYS`` is read,
    default ``os.environ``).
    """
    manifest = _parse_manifest(dict(manifest))
    if verify_signature:
        _verify_signature(manifest, env)
    parsed: dict[str, Any] = {}
    for name in BUNDLE_DATA_FILES:
        if name not in files:
            raise BundleIntegrityError(f"catalog bundle is missing {name}")
        raw = files[name]
        if isinstance(raw, str):
            data = raw.encode("utf-8")
        elif isinstance(raw, (bytes, bytearray, memoryview)):
            data = bytes(raw)
        else:
            # Never bytes(<int>) (allocates that many bytes) or bytes(<dict>).
            raise BundleError(f"{name} must be text or bytes, got {type(raw).__name__}")
        if sha256_hex(data) != manifest["files"][name]:
            raise BundleIntegrityError(f"{name} does not match its manifest sha256")
        try:
            parsed[name] = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BundleError(f"{name} is not valid UTF-8 JSON: {exc}") from exc

    bundle = CatalogBundle(
        manifest=manifest,
        models=parsed["models.json"],
        benchmarks=parsed["benchmarks.json"],
        normalization_ranges=parsed["normalization_ranges.json"],
        centroids=parsed["centroids.json"],
        training_queries=parsed["training_queries.json"],
        directory=directory,
    )
    try:
        _check_consistency(bundle)
    except BundleError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any wrong shape is a bad bundle
        # Matching hashes but a malformed document (e.g. benchmarks.json "{}"):
        # the accessors hit KeyError / TypeError / AttributeError.
        raise BundleError(f"catalog bundle is malformed: {type(exc).__name__}: {exc}") from exc
    return bundle


def load_bundle(
    directory: str | Path,
    *,
    verify_signature: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> CatalogBundle:
    """Load and verify the bundle in ``directory`` (the six files of section 1).

    ``verify_signature`` additionally requires a trusted signature (contract
    section 6) -- used for the cached full catalog."""
    directory = Path(directory)
    manifest_path = directory / MANIFEST_FILE
    if not manifest_path.is_file():
        raise BundleIntegrityError(f"no catalog bundle at {directory} (manifest.json missing)")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BundleError(f"manifest.json is not valid JSON: {exc}") from exc
    manifest = _parse_manifest(manifest)  # schema before touching the data files
    if verify_signature:
        _verify_signature(manifest, env)
    files: dict[str, bytes] = {}
    for name in BUNDLE_DATA_FILES:
        path = directory / name
        if not path.is_file():
            raise BundleIntegrityError(f"catalog bundle at {directory} is missing {name}")
        files[name] = path.read_bytes()
    # The signature (if requested) was checked above, before reading the files.
    return bundle_from_texts(manifest, files, directory=directory)


_STARTER: Optional[CatalogBundle] = None


def starter_bundle() -> CatalogBundle:
    """The starter bundle shipped in the package (loaded once per process)."""
    global _STARTER
    if _STARTER is None:
        _STARTER = load_bundle(STARTER_BUNDLE_DIR)
    return _STARTER


BundleLike = Union[CatalogBundle, str, Path]


def resolve_bundle(bundle: Optional[BundleLike] = None, catalog: str = "auto") -> CatalogBundle:
    """THE seam every default-data consumer goes through.

    * a :class:`CatalogBundle` is returned as-is;
    * a path is loaded with :func:`load_bundle`;
    * ``None`` means "the default catalog", chosen by ``catalog``
      (contract section 5): ``"starter"`` = the packaged starter bundle;
      ``"auto"`` (default) = the full catalog when logged in (downloaded and
      kept fresh by :mod:`tryaii.catalog.client`), else the starter;
      ``"full"`` = like auto but raises ``LoginRequiredError`` when not
      logged in. The selection is memoized per process. May raise
      ``SessionEndedError``.
    """
    if bundle is None:
        from tryaii.catalog.client import selected_bundle

        return selected_bundle(catalog)
    if isinstance(bundle, CatalogBundle):
        return bundle
    if isinstance(bundle, (str, Path)):
        return load_bundle(bundle)
    raise TypeError(f"bundle must be a CatalogBundle or a directory path, got {type(bundle)!r}")
