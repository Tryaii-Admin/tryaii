"""Data Notice, upload switch and upload hashing (shared/monitor/SPEC.md §9, API.md §7).

Everything here is shared by the uploader and the CLI and mirrored byte for
byte by packages/node/src/monitor/privacy.ts:

- ``NOTICE_TEXT``: the CLI notice (Confluence "CLI and SDK Data Notice", piece D);
- :func:`upload_decision`: whether this machine uploads, and why not
  (env opt-outs > the local ``tryaii monitor upload off`` setting > on);
- ``settings.json`` and ``notice.json`` under ``<data dir>/monitor/``;
- :func:`keyed_hash`: the prompt hash as uploaded, an HMAC-SHA256 keyed with
  a random per-installation secret (``upload-hash.key``) that never leaves the
  machine, so the server cannot test guesses against it.

The remote switch (``"upload": false`` in remote.json, or a 403
``upload_disabled``) is applied by the uploader per app directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Optional

from tryaii.auth.store import write_atomic

#: The notice version: its publication date (API.md §7.1).
NOTICE_VERSION = "2026-10-24"
NOTICE_URL = "https://tryaii.com/privacy-policy#cli-sdk"
NOTICE_TEXT = (
    "[tryaii] monitor records sync to your TryAii account.\n"
    "Sent: model names, token counts, costs, timings, file paths, a task profile.\n"
    "Never sent: your prompts, responses or API keys.\n"
    f"Turn off: tryaii monitor upload off   Details: {NOTICE_URL}\n"
)

SETTINGS_FILE = "settings.json"
SETTINGS_SCHEMA = "tryaii.monitor.settings/1"
NOTICE_FILE = "notice.json"
NOTICE_SCHEMA = "tryaii.monitor.notice/1"
HASH_KEY_FILE = "upload-hash.key"

ENV_UPLOAD = "TRYAII_MONITOR_UPLOAD"
ENV_TELEMETRY = "TRYAII_TELEMETRY"
ENV_DNT = "DO_NOT_TRACK"


def _env_off(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() in ("0", "false", "off", "no")


def _env_on(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() in ("1", "true", "yes", "on")


def _dump(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read(path: Path) -> Optional[dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


# --- the local switch -------------------------------------------------------------

def read_local_upload(root) -> Optional[bool]:
    """The ``tryaii monitor upload on|off`` choice, or None when never set."""
    doc = _read(Path(root) / SETTINGS_FILE)
    if not doc or doc.get("schema") != SETTINGS_SCHEMA or not isinstance(doc.get("upload"), bool):
        return None
    return doc["upload"]


def write_local_upload(root, upload: bool) -> Path:
    path = Path(root) / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, _dump({"schema": SETTINGS_SCHEMA, "upload": bool(upload)}))
    return path


def upload_decision(env=None, root=None) -> tuple[bool, Optional[str]]:
    """``(enabled, reason)``. ``reason`` names what turned upload off:
    ``TRYAII_MONITOR_UPLOAD=0``, ``TRYAII_TELEMETRY=0``, ``DO_NOT_TRACK=1`` or
    ``tryaii monitor upload off``; None while enabled."""
    env = os.environ if env is None else env
    if _env_off(env.get(ENV_UPLOAD)):
        return False, f"{ENV_UPLOAD}=0"
    if _env_off(env.get(ENV_TELEMETRY)):
        return False, f"{ENV_TELEMETRY}=0"
    if _env_on(env.get(ENV_DNT)):
        return False, f"{ENV_DNT}=1"
    if root is not None and read_local_upload(root) is False:
        return False, "tryaii monitor upload off"
    return True, None


# --- the one-time notice -------------------------------------------------------------

def notice_shown(root, version: str = NOTICE_VERSION) -> bool:
    doc = _read(Path(root) / NOTICE_FILE)
    return bool(doc) and doc.get("schema") == NOTICE_SCHEMA and doc.get("version") == version


def mark_notice_shown(root, shown_at: str, version: str = NOTICE_VERSION) -> None:
    path = Path(root) / NOTICE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, _dump({"schema": NOTICE_SCHEMA, "version": version, "shown_at": shown_at}))


def can_show_notice(stream, env=None) -> bool:
    """Only to a person: the stream is a terminal and ``CI`` is not set."""
    env = os.environ if env is None else env
    if env.get("CI"):
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


# --- keyed prompt hash -----------------------------------------------------------------

def hash_key(root) -> bytes:
    """The per-installation secret (32 random bytes, hex in ``upload-hash.key``),
    created on first use. Never uploaded."""
    path = Path(root) / HASH_KEY_FILE
    try:
        text = path.read_text(encoding="ascii").strip()
        if len(text) == 64:
            return bytes.fromhex(text)
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    write_atomic(path, (key.hex() + "\n").encode("ascii"))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def keyed_hash(key: bytes, prompt_hash: str) -> str:
    """The uploaded ``prompt_hash``: lowercase hex HMAC-SHA256(key, local hash)."""
    return hmac.new(key, prompt_hash.encode("ascii", "replace"), hashlib.sha256).hexdigest()
