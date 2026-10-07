"""
Catalog client: which catalog to route on, and keeping the full one fresh.

Contract: docs/catalog/CONTRACT-catalog-v1.md, section 5 (sections 1 and 4
for the bundle and wire formats). Mirrors ``packages/node/src/catalog/client.ts``.

Cache layout under the data dir (``TRYAII_DRE_DATA_DIR`` or ``~/.tryaii``)::

    catalog/full/<version>/   the six bundle files exactly as received
    catalog/state.json        {"version": "<live version or null>", "checked_at": "<ISO Z>"}
    catalog/nudge.json        {"last_shown": "<YYYY-MM-DD local date>"}

Every write goes through the credentials file's atomic helper
(:func:`tryaii.auth.store.write_atomic`); only the newest version directory
is kept. ``state.version`` is null after a 403 (the account has no access to
the full catalog), which is how a run inside the 24 h window knows to say so
without asking the server again. Failures (network, 5xx, 429, 404, hash,
signature or schema problems) never touch ``state.json`` -- the next use
retries. A full catalog is accepted only with a valid signature from a
trusted key (contract section 6, :mod:`tryaii.catalog.signing`), both when it
is downloaded (before the cache is written) and when the cache is loaded.

The library never prints. :func:`select_catalog` returns a
:class:`CatalogSelection` whose ``notice`` tells the CLI what to print
(:func:`notice_message`, :func:`maybe_nudge`).
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from tryaii.auth import flow, store, transport
from tryaii.auth.transport import AuthError, session_api_url
from tryaii.catalog.bundle import (
    BUNDLE_DATA_FILES,
    MANIFEST_FILE,
    BundleSchemaError,
    CatalogBundle,
    bundle_from_texts,
    load_bundle,
    starter_bundle,
)

CATALOG_DIR = "catalog"
FULL_DIR = "full"
STATE_FILE = "state.json"
NUDGE_FILE = "nudge.json"
LIVE_PATH = "/v1/catalog/live"
#: How often ``auto`` / ``full`` ask the server (contract section 5).
CHECK_INTERVAL_SECONDS = 24 * 3600
#: How long the library memo keeps a selection whose check FAILED (network,
#: 5xx, 429, 404, hash / shape / schema problems) before the next use retries:
#: short enough that a process started during a network blip soon moves to
#: the full catalog, long enough not to hammer the server on every Router.
FAILURE_RETRY_SECONDS = 60
CATALOG_MODES = ("auto", "starter", "full")
FREE_SUFFIX = ":free"
# Always used with fullmatch: re.match + "$" would accept a trailing "\n"
# (a version directory with a newline in its name on POSIX). re.ASCII: \d is
# [0-9] only, like JavaScript (a str \d also matches e.g. Arabic-Indic digits).
_VERSION_RE = re.compile(r"\d{4}\.\d{2}\.\d{2}\.\d{1,6}", re.ASCII)
_ISO_Z_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)

# --- user-facing text (byte-identical to the Node SDK) ----------------------
MSG_SESSION_ENDED = "Your session has ended. Run: tryaii login"
MSG_LOGIN_REQUIRED = "Not logged in. Run: tryaii login"
MSG_NO_ENTITLEMENT = (
    "This account does not have access to the full catalog; using the starter catalog."
)
MSG_DOWNLOAD_FAILED = (
    "Could not download the full catalog; using the starter catalog for now."
)
MSG_SCHEMA_TOO_NEW = (
    "The full catalog needs a newer tryaii version; using the starter catalog."
)
MSG_LOGIN_DOWNLOAD_FAILED = (
    "Could not download the full catalog now; it will be fetched on next use."
)

#: Why a selection landed on the starter catalog (None = nothing to say).
NOTICE_NOT_LOGGED_IN = "not_logged_in"
NOTICE_NO_ENTITLEMENT = "no_entitlement"
NOTICE_DOWNLOAD_FAILED = "download_failed"
NOTICE_SCHEMA_TOO_NEW = "schema_too_new"

_NOTICE_MESSAGES = {
    NOTICE_NO_ENTITLEMENT: MSG_NO_ENTITLEMENT,
    NOTICE_DOWNLOAD_FAILED: MSG_DOWNLOAD_FAILED,
    NOTICE_SCHEMA_TOO_NEW: MSG_SCHEMA_TOO_NEW,
}


class CatalogError(Exception):
    """Base class of the catalog client's errors."""


class SessionEndedError(CatalogError):
    """The refresh token was rejected (``invalid_grant``) or the catalog
    endpoint answered 401. The credentials file and the full cache have been
    deleted. Same message as the CLI."""

    def __init__(self, message: str = MSG_SESSION_ENDED):
        super().__init__(message)


class LoginRequiredError(CatalogError):
    """``catalog="full"`` without a stored session. Same message as the CLI."""

    def __init__(self, message: str = MSG_LOGIN_REQUIRED):
        super().__init__(message)


@dataclass(frozen=True)
class CatalogSelection:
    """The catalog to route on plus what (if anything) the CLI should say."""

    bundle: CatalogBundle
    #: One of the ``NOTICE_*`` values, or None.
    notice: Optional[str] = None
    #: True when this selection contacted the server.
    checked: bool = False
    #: True when the server check failed (network, 5xx, 429, 404, a bad or
    #: too-new bundle) and the selection fell back (to the cached full
    #: catalog or the starter). The library memo retries these soon.
    failed: bool = False

    @property
    def kind(self) -> str:
        return self.bundle.kind

    @property
    def version(self) -> str:
        return self.bundle.version


def notice_message(notice: Optional[str]) -> Optional[str]:
    """The CLI's stderr line for a notice (the nudge is separate)."""
    return _NOTICE_MESSAGES.get(notice) if notice else None


def routable_count(bundle: CatalogBundle) -> int:
    """User-facing model count: entries whose id is not a ``:free`` variant
    (contract section 1, "User-facing model counts")."""
    return sum(
        1 for m in bundle.model_entries()
        if not str(m.get("model_id", "")).endswith(FREE_SUFFIX)
    )


# ---------------------------------------------------------------- paths
def catalog_dir() -> Path:
    return store.data_dir() / CATALOG_DIR


def full_dir() -> Path:
    return catalog_dir() / FULL_DIR


def state_path() -> Path:
    return catalog_dir() / STATE_FILE


def nudge_path() -> Path:
    return catalog_dir() / NUDGE_FILE


def _now() -> float:
    return flow.clock()


# ---------------------------------------------------------------- state
def read_state() -> Optional[dict]:
    """``state.json`` as a dict, or None when absent / unreadable."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(version: Optional[str], checked_at: float) -> None:
    text = json.dumps({"version": version, "checked_at": store.iso_z(checked_at)})
    store.write_atomic(state_path(), text.encode("utf-8"), prefix=".state-")


def _check_due(state: Optional[dict], now: float) -> bool:
    if not state:
        return True
    raw = state.get("checked_at")
    # Same strictness as the Node SDK (exact "YYYY-MM-DDTHH:MM:SSZ").
    checked = (store.parse_iso_z(raw)
               if isinstance(raw, str) and _ISO_Z_RE.fullmatch(raw) else None)
    if checked is None:
        return True
    age = now - checked
    # A checked_at in the future (clock skew, hand edits) counts as stale.
    return not (0 <= age < CHECK_INTERVAL_SECONDS)


#: User centroid caches derived from a full catalog (``centroids/
#: centroids_<model>__full-<version>.json``, see TryaiiDreConfig.centroid_file_for).
FULL_CENTROID_GLOB = "centroids_*__full-*.json"


def clear_cache() -> None:
    """Delete ``catalog/full/``, ``catalog/state.json`` and the user centroid
    caches derived from the full catalog (``centroids/*__full-*.json``) --
    logout, session ended. ``nudge.json`` and the starter's centroid caches
    are kept. Never raises."""
    shutil.rmtree(full_dir(), ignore_errors=True)
    try:
        state_path().unlink()
    except OSError:
        pass
    try:
        derived = list((store.data_dir() / "centroids").glob(FULL_CENTROID_GLOB))
    except OSError:
        derived = []
    for path in derived:
        try:
            path.unlink()
        except OSError:
            pass
    _forget_selections()


# ---------------------------------------------------------------- cache
def _version_key(name: str) -> tuple:
    return tuple(int(p) for p in name.split("."))


_LOADED: dict[str, CatalogBundle] = {}
_LOADED_LOCK = threading.Lock()


def _load_version_dir(path: Path) -> Optional[CatalogBundle]:
    """Load (and verify) one cached version directory; memoized per path
    (a version directory is immutable once written). The cached manifest
    keeps its signature and is re-verified here (contract section 6): an
    unsigned or tampered cache is treated as absent."""
    key = str(path)
    with _LOADED_LOCK:
        if key in _LOADED:
            return _LOADED[key]
    try:
        bundle = load_bundle(path, verify_signature=True)
    except Exception:  # noqa: BLE001 -- an unusable cache is just absent
        return None
    if bundle.kind != "full" or bundle.version != path.name:
        return None
    with _LOADED_LOCK:
        _LOADED[key] = bundle
    return bundle


def cached_full(state: Optional[dict] = None) -> Optional[CatalogBundle]:
    """The cached full bundle (the state's version first, else the newest
    valid version directory), or None."""
    base = full_dir()
    try:
        names = [p.name for p in base.iterdir()
                 if p.is_dir() and _VERSION_RE.fullmatch(p.name)]
    except OSError:
        return None
    preferred = (state or {}).get("version")
    ordered = sorted(names, key=_version_key, reverse=True)
    if isinstance(preferred, str) and preferred in names:
        ordered.remove(preferred)
        ordered.insert(0, preferred)
    for name in ordered:
        bundle = _load_version_dir(base / name)
        if bundle is not None:
            return bundle
    return None


def manifest_text(manifest: dict) -> str:
    """How a received manifest is written: as received (key order kept),
    2-space indented, trailing newline (same bytes in both SDKs)."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def save_full(manifest: dict, files: dict, bundle: CatalogBundle) -> CatalogBundle:
    """Write a verified bundle to ``catalog/full/<version>/`` (temp dir +
    rename; each file through the atomic helper), drop every other version
    directory, and return the bundle bound to its directory."""
    version = bundle.version
    base = full_dir()
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".tmp-{uuid.uuid4().hex}"
    target = base / version
    try:
        for name in BUNDLE_DATA_FILES:
            raw = files[name]
            data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
            store.write_atomic(tmp / name, data)
        store.write_atomic(tmp / MANIFEST_FILE, manifest_text(manifest).encode("utf-8"))
        if target.exists():
            shutil.rmtree(target)
        store._replace_with_retry(str(tmp), str(target))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    for entry in base.iterdir():
        if entry.name != version:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except OSError:
                    pass
    with _LOADED_LOCK:
        _LOADED.clear()
    placed = CatalogBundle(
        manifest=bundle.manifest,
        models=bundle.models,
        benchmarks=bundle.benchmarks,
        normalization_ranges=bundle.normalization_ranges,
        centroids=bundle.centroids,
        training_queries=bundle.training_queries,
        directory=target,
    )
    with _LOADED_LOCK:
        _LOADED[str(target)] = placed
    return placed


# ---------------------------------------------------------------- transport
class _Failure(Exception):  # noqa: N818 (internal control-flow signal)
    """Network failure, timeout, unexpected status or body."""


def fetch_live(api_url: str, access_token: str, etag: Optional[str], *,
               opener=None) -> tuple[int, bytes]:
    """``GET /v1/catalog/live``: one attempt, 10 s timeout, no redirects
    (auth-v1 transport). Returns ``(status, body bytes)``; gzip bodies are
    decompressed. Raises :class:`_Failure` on any transport failure."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": f"tryaii/{transport._version()}",
        "Authorization": f"Bearer {access_token}",
    }
    if etag:
        headers["If-None-Match"] = f'"{etag}"'
    request = urllib.request.Request(f"{api_url}{LIVE_PATH}", method="GET", headers=headers)
    open_fn = opener or transport.urlopen
    encoding = None
    try:
        with open_fn(request, timeout=transport.TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            raw = response.read()
            encoding = _header(response, "Content-Encoding")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        encoding = _header(exc, "Content-Encoding")
    except Exception as exc:  # noqa: BLE001 -- one collapsed failure kind
        raise _Failure(str(exc)) from exc
    if encoding and encoding.strip().lower() == "gzip" and raw:
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise _Failure("bad gzip body") from exc
    return int(status), raw


def _header(obj, name: str) -> Optional[str]:
    headers = getattr(obj, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except Exception:  # noqa: BLE001
        return None


def _json_object(raw: bytes) -> Optional[dict]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- the check
# Outcomes of one check.
_OK = "ok"                    # new bundle downloaded and saved
_NOT_MODIFIED = "not_modified"
_SESSION_ENDED = "session_ended"
_FORBIDDEN = "forbidden"
_FAILED = "failed"
_SCHEMA = "schema_too_new"


def end_session() -> None:
    """Session ended: delete the credentials file and the full cache."""
    try:
        store.delete()
    except OSError:
        pass
    clear_cache()


def _run_check(creds: dict, cached: Optional[CatalogBundle], *, opener=None
               ) -> tuple[str, Optional[CatalogBundle]]:
    """Refresh if needed, then ``GET /v1/catalog/live`` with the cached
    version as ``If-None-Match``. Persists the rotated session, the new
    bundle and ``state.json``. Returns ``(outcome, bundle or None)``."""
    api_url = session_api_url(creds.get("api_url"))
    try:
        creds, refreshed = flow.refresh_if_needed(api_url, creds, opener=opener)
    except AuthError as exc:
        if exc.code == "invalid_grant":
            return _SESSION_ENDED, None
        return _FAILED, None
    if refreshed:
        try:
            store.save(creds)
        except OSError:
            pass
    try:
        status, raw = fetch_live(api_url, creds["access_token"],
                                 cached.version if cached else None, opener=opener)
    except _Failure:
        return _FAILED, None
    now = _now()

    if status == 304:
        if cached is None:
            return _FAILED, None
        _try_write_state(cached.version, now)
        return _NOT_MODIFIED, cached
    if status == 401:
        return _SESSION_ENDED, None
    if status == 403:
        body = _json_object(raw) or {}
        if body.get("error") == "insufficient_entitlement":
            # No access: remember it for 24 h and drop any old full cache.
            shutil.rmtree(full_dir(), ignore_errors=True)
            _try_write_state(None, now)
            return _FORBIDDEN, None
        return _FAILED, None
    if status != 200:
        return _FAILED, None

    body = _json_object(raw)
    manifest = (body or {}).get("manifest")
    files = (body or {}).get("files")
    if not isinstance(manifest, dict) or not isinstance(files, dict):
        return _FAILED, None
    # The wire format carries text only (same rule as the Node SDK).
    if any(name in files and not isinstance(files[name], str) for name in BUNDLE_DATA_FILES):
        return _FAILED, None
    try:
        # Signature first (contract section 6), before anything is written to
        # the cache: missing / unknown key / bad signature = a hash mismatch.
        bundle = bundle_from_texts(manifest, files, verify_signature=True)
        if bundle.kind != "full" or not _VERSION_RE.fullmatch(bundle.version):
            return _FAILED, None
    except BundleSchemaError:
        return _SCHEMA, None
    except Exception:  # noqa: BLE001 -- any bad body is a failed download, never a crash
        return _FAILED, None
    try:
        bundle = save_full(manifest, files, bundle)
    except Exception:  # noqa: BLE001
        # Use it for this run; the next use downloads again.
        return _OK, bundle
    _try_write_state(bundle.version, now)
    return _OK, bundle


def _try_write_state(version: Optional[str], now: float) -> None:
    try:
        write_state(version, now)
    except OSError:
        pass


def select_catalog(catalog: str = "auto", *, opener=None,
                   force_check: bool = False) -> CatalogSelection:
    """Pick the catalog per contract section 5 ("Which catalog").

    * ``"starter"`` -- the packaged starter catalog; no network, no nudge.
    * ``"auto"`` -- starter when not logged in (notice ``not_logged_in``);
      otherwise the full catalog, checked against the server at most once
      per 24 h (``force_check`` checks now).
    * ``"full"`` -- like auto, but raises :class:`LoginRequiredError` when not
      logged in.

    Raises :class:`SessionEndedError` (after deleting the credentials file
    and the full cache) when the session is gone. Never prints.
    """
    if catalog not in CATALOG_MODES:
        raise ValueError(f"catalog must be one of {', '.join(CATALOG_MODES)}; got {catalog!r}")
    if catalog == "starter":
        return CatalogSelection(starter_bundle())
    creds = store.load()
    if creds is None:
        if catalog == "full":
            raise LoginRequiredError()
        return CatalogSelection(starter_bundle(), NOTICE_NOT_LOGGED_IN)

    state = read_state()
    cached = cached_full(state)
    now = _now()
    due = force_check or _check_due(state, now)
    if not due:
        if cached is not None:
            return CatalogSelection(cached)
        if state.get("version") is None:
            # Checked within 24 h and the account had no access then.
            return CatalogSelection(starter_bundle(), NOTICE_NO_ENTITLEMENT)
        # state names a version but its directory is gone: check now.

    outcome, bundle = _run_check(creds, cached, opener=opener)
    if outcome in (_OK, _NOT_MODIFIED):
        return CatalogSelection(bundle, checked=True)
    if outcome == _SESSION_ENDED:
        end_session()
        raise SessionEndedError()
    if outcome == _FORBIDDEN:
        return CatalogSelection(starter_bundle(), NOTICE_NO_ENTITLEMENT, checked=True)
    if cached is not None:
        return CatalogSelection(cached, checked=True, failed=True)
    notice = NOTICE_SCHEMA_TOO_NEW if outcome == _SCHEMA else NOTICE_DOWNLOAD_FAILED
    return CatalogSelection(starter_bundle(), notice, checked=True, failed=True)


def download_after_login(*, opener=None) -> Optional[CatalogBundle]:
    """``tryaii login``'s immediate download. The full bundle (new, or the
    cached one confirmed by a 304), or None on any failure. Never raises
    for server/network problems and never deletes the fresh session."""
    creds = store.load()
    if creds is None:
        return None
    state = read_state()
    outcome, bundle = _run_check(creds, cached_full(state), opener=opener)
    _forget_selections()
    return bundle if outcome in (_OK, _NOT_MODIFIED) else None


# ---------------------------------------------------------------- nudge (CLI)
def nudge_text(starter: Optional[CatalogBundle] = None) -> str:
    starter = starter or starter_bundle()
    full = (starter.full_counts or {}).get("models", 0)
    return (
        f"Routing on the starter catalog ({routable_count(starter)} models). "
        f"Log in for free to use the full catalog ({full} models): tryaii login"
    )


def _local_today() -> str:
    return _dt.date.fromtimestamp(_now()).isoformat()


def maybe_nudge(selection: CatalogSelection, write: Optional[Callable[[str], Any]] = None,
                *, env=None) -> bool:
    """CLI only: print the login nudge to stderr when the selection is the
    starter catalog because nobody is logged in -- at most once per local
    calendar day, never when ``TRYAII_NO_BANNER`` is set. True when shown."""
    env = os.environ if env is None else env
    if selection.notice != NOTICE_NOT_LOGGED_IN or env.get("TRYAII_NO_BANNER"):
        return False
    today = _local_today()
    try:
        data = json.loads(nudge_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("last_shown") == today:
            return False
    except (OSError, ValueError):
        pass
    line = nudge_text(selection.bundle) + "\n"
    if write is None:
        sys.stderr.write(line)
        sys.stderr.flush()
    else:
        write(line)
    try:
        store.write_atomic(nudge_path(),
                           json.dumps({"last_shown": today}).encode("utf-8"),
                           prefix=".nudge-")
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- library memo
# resolve_bundle(None, catalog=...) runs the selection once per process (per
# mode and data dir) and re-runs it after 24 h, so a library that builds
# several Routers / registries checks the server at most once a day. A
# selection whose check FAILED (``CatalogSelection.failed``: download_failed,
# schema_too_new, or a stale cached catalog kept after a failure) is kept for
# FAILURE_RETRY_SECONDS only, so a long-running process that started during a
# network blip retries on a later use instead of staying put for a day.
# Not-logged-in and no-access (403) selections are final answers: 24 h.
_SELECTIONS: dict[tuple, tuple[float, CatalogSelection]] = {}
_SELECTIONS_LOCK = threading.Lock()
#: Monotonic clock of the memo (a seam for tests).
_monotonic = time.monotonic


def _memo_ttl(selection: CatalogSelection) -> float:
    return FAILURE_RETRY_SECONDS if selection.failed else CHECK_INTERVAL_SECONDS


def _forget_selections() -> None:
    with _SELECTIONS_LOCK:
        _SELECTIONS.clear()


def reset_cache() -> None:
    """Forget every in-process memo (tests)."""
    _forget_selections()
    with _LOADED_LOCK:
        _LOADED.clear()


def selected_bundle(catalog: str = "auto") -> CatalogBundle:
    """The bundle for ``catalog``, memoized per process (see above)."""
    if catalog not in CATALOG_MODES:
        raise ValueError(f"catalog must be one of {', '.join(CATALOG_MODES)}; got {catalog!r}")
    if catalog == "starter":
        return starter_bundle()
    key = (catalog, str(store.data_dir()))
    now = _monotonic()
    with _SELECTIONS_LOCK:
        hit = _SELECTIONS.get(key)
    if hit is not None and now - hit[0] < _memo_ttl(hit[1]):
        return hit[1].bundle
    selection = select_catalog(catalog)
    with _SELECTIONS_LOCK:
        _SELECTIONS[key] = (now, selection)
    return selection.bundle
