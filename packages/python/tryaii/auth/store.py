"""Credentials storage (CONTRACT-auth-v1.md section 6, "Configuration").

``<data dir>/credentials.json``, data dir = ``TRYAII_DRE_DATA_DIR`` or
``~/.tryaii``. Written atomically (CONTRACT section 7, item 18): a temp file
in the same directory created exclusively with mode 0600 (``mkstemp`` =
``O_EXCL``), fsynced, then ``os.replace``d over the target -- retried on
Windows while another process briefly holds the file.
"""

from __future__ import annotations

import calendar
import errno
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

CREDENTIALS_FILE = "credentials.json"
SCHEMA_VERSION = 1

# Windows rename retry (item 18): a reader (antivirus, indexer, a parallel
# tryaii) holding the target open makes MoveFileEx fail transiently.
RENAME_RETRIES = 5
RENAME_RETRY_DELAY = 0.05
_RENAME_RETRY_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EBUSY})
# Seams for the retry tests.
_IS_WINDOWS = os.name == "nt"
_sleep = time.sleep

# Key order of the stored document (the contract's schema order).
_KEYS = ("version", "api_url", "user", "entitlements", "refresh_token",
         "refresh_expires_at", "access_token", "access_expires_at", "created_at")


def data_dir() -> Path:
    """``TRYAII_DRE_DATA_DIR`` (empty falls through) or ``~/.tryaii``."""
    env = os.environ.get("TRYAII_DRE_DATA_DIR")
    return Path(env) if env else Path.home() / ".tryaii"


def credentials_path() -> Path:
    return data_dir() / CREDENTIALS_FILE


def iso_z(epoch: float) -> str:
    """ISO 8601 UTC with ``Z``, second precision."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def parse_iso_z(value) -> Optional[float]:
    """Inverse of :func:`iso_z`; None for anything unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def load(path: Optional[Path] = None) -> Optional[dict]:
    """The stored credentials, or None when absent, unreadable, not a JSON
    object, of another schema version, or missing a refresh token."""
    path = path or credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        return None
    if not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]:
        return None
    return data


def save(creds: dict, path: Optional[Path] = None) -> Path:
    """Atomically write ``creds`` (0600 on POSIX). Returns the path."""
    path = path or credentials_path()
    ordered = {k: creds[k] for k in _KEYS if k in creds}
    ordered.update({k: v for k, v in creds.items() if k not in ordered})
    text = json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"
    write_atomic(path, text.encode("utf-8"), prefix=".credentials-")
    return path


def write_atomic(path: Path, data: bytes, *, prefix: str = ".tryaii-") -> None:
    """THE atomic-write helper (item 18), shared by the credentials file and
    the catalog cache (catalog contract section 5): a temp file in the same
    directory created exclusively with mode 0600 (``mkstemp`` = ``O_EXCL``),
    fsynced, then ``os.replace``d over ``path`` (retried on Windows)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(path.parent))
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(src, dst) -> None:
    """``os.replace``; on Windows retried up to 5 times, 50 ms apart, on
    EPERM/EACCES/EBUSY so a rotated refresh token is not lost to a
    transient lock."""
    attempts = 1 + (RENAME_RETRIES if _IS_WINDOWS else 0)
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if attempt == attempts - 1 or exc.errno not in _RENAME_RETRY_ERRNOS:
                raise
            _sleep(RENAME_RETRY_DELAY)


def delete(path: Optional[Path] = None) -> bool:
    """Remove the credentials file. True when a file was removed, False when
    there was none (ENOENT is not an error). Any other failure raises
    ``OSError`` -- ``logout`` reports it (CONTRACT section 7, item 19)."""
    path = path or credentials_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
