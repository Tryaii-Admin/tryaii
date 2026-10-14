"""The local monitor store: ``<data_dir>/monitor/<app>/`` (SPEC.md §3).

Append-only daily JSONL files for events and vectors (one ``O_APPEND``
write per line, so concurrent processes interleave whole lines), plus
``pins.json`` / ``config.json`` written atomically.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from tryaii.auth.store import write_atomic
from tryaii.monitor.config import REMOTE_SCHEMA, empty_config
from tryaii.monitor.events import parse_ts
from tryaii.monitor.pins import empty_pins

MONITOR_DIR = "monitor"
EVENTS_PREFIX = "events-"
VECTORS_PREFIX = "vectors-"
PINS_FILE = "pins.json"
CONFIG_FILE = "config.json"
REMOTE_FILE = "remote.json"
UPLOAD_FILE = "upload.json"
UPLOAD_LOCK_FILE = "upload.lock"
UPLOAD_CURSOR_SCHEMA = "tryaii.monitor.upload-cursor/1"

_append_lock = threading.Lock()
_seen_hashes: dict[str, set] = {}


def monitor_root(data_dir) -> Path:
    return Path(data_dir) / MONITOR_DIR


def app_dir(data_dir, app: str) -> Path:
    return monitor_root(data_dir) / app


def list_apps(data_dir) -> list[str]:
    """App names with a monitor directory, sorted."""
    root = monitor_root(data_dir)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def day_of(ts: str) -> str:
    return parse_ts(ts).strftime("%Y-%m-%d")


def events_path(directory, ts: str) -> Path:
    return Path(directory) / f"{EVENTS_PREFIX}{day_of(ts)}.jsonl"


def vectors_path(directory, ts: str) -> Path:
    return Path(directory) / f"{VECTORS_PREFIX}{day_of(ts)}.jsonl"


def _js_number(token: str) -> str:
    """Rewrite one JSON number token the way JavaScript's ``JSON.stringify``
    prints it, so Python and Node write byte-identical lines: integral
    floats drop the fraction (``1.0`` -> ``1``), fixed notation for
    1e-6 <= |x| < 1e21 (``9e-05`` -> ``0.00009``), otherwise a bare exponent
    (``1e-07`` -> ``1e-7``, ``1e+21``) -- ECMAScript Number::toString."""
    lowered = token.lower()
    if "e" not in lowered:
        if "." in token and token.split(".", 1)[1].strip("0") == "":
            return token.split(".", 1)[0]
        return token
    magnitude = abs(float(token))
    if 1e-6 <= magnitude < 1e21:
        fixed = format(Decimal(token), "f")
        return _js_number(fixed)
    mantissa, exponent = lowered.split("e")
    exp = int(exponent)
    return f"{mantissa}e{'+' if exp > 0 else '-'}{abs(exp)}"


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def js_numbers(text: str) -> str:
    """Apply :func:`_js_number` to every number token outside JSON strings
    (SPEC.md §1: the Node SDK's ``JSON.stringify`` is the byte reference)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        m = _NUMBER.match(text, i)
        if m:
            out.append(_js_number(m.group(0)))
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def dumps_line(obj) -> str:
    """Minified JSON, key order preserved, non-ASCII verbatim, ``\n``,
    numbers printed as Node would."""
    return js_numbers(json.dumps(obj, ensure_ascii=False, separators=(",", ":"))) + "\n"


def append_line(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dumps_line(obj).encode("utf-8")
    with _append_lock:
        fd = os.open(str(path),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
                     0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def append_event(directory, event: dict) -> Path:
    path = events_path(directory, event["ts"])
    append_line(path, event)
    return path


def _hashes_in(path: Path) -> set:
    key = str(path)
    seen = _seen_hashes.get(key)
    if seen is None:
        seen = set()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    seen.add(json.loads(line).get("prompt_hash"))
                except (ValueError, AttributeError):
                    continue
        _seen_hashes[key] = seen
    return seen


def append_vector(directory, record: dict) -> bool:
    """Append a vectors line unless the day's file already has this
    ``prompt_hash``. Returns True when written."""
    path = vectors_path(directory, record["ts"])
    with _append_lock:
        seen = _hashes_in(path)
        if record["prompt_hash"] in seen:
            return False
        seen.add(record["prompt_hash"])
    append_line(path, record)
    return True


def _day_files(directory, prefix: str, start: datetime, end: datetime) -> list[Path]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    first = start.strftime("%Y-%m-%d")
    last = (end + timedelta(days=1)).strftime("%Y-%m-%d")
    out = []
    for p in sorted(directory.iterdir()):
        name = p.name
        if not (name.startswith(prefix) and name.endswith(".jsonl")):
            continue
        day = name[len(prefix):-len(".jsonl")]
        if first <= day <= last:
            out.append(p)
    return out


def read_lines(path: Path) -> list[dict]:
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_events(directory, now: str, since_seconds: float) -> list[dict]:
    """Events with ``start <= ts <= now`` across the daily files, in file
    order (which is append order). Malformed lines are skipped."""
    end = parse_ts(now)
    start = end - timedelta(seconds=since_seconds)
    out = []
    for path in _day_files(directory, EVENTS_PREFIX, start, end):
        for event in read_lines(path):
            try:
                ts = parse_ts(event["ts"])
            except (KeyError, ValueError, TypeError):
                continue
            if start <= ts <= end:
                out.append(event)
    return out


def read_recent_events(directory, limit: int) -> list[dict]:
    """The last ``limit`` events across all daily files (oldest first)."""
    directory = Path(directory)
    if not directory.is_dir() or limit <= 0:
        return []
    files = sorted(p for p in directory.iterdir()
                   if p.name.startswith(EVENTS_PREFIX) and p.name.endswith(".jsonl"))
    out: list[dict] = []
    for path in reversed(files):
        out = read_lines(path) + out
        if len(out) >= limit:
            break
    return out[-limit:]


def _dump_doc(doc) -> bytes:
    return (js_numbers(json.dumps(doc, indent=2, ensure_ascii=False)) + "\n").encode("utf-8")


def _read_doc(path: Path, default: dict) -> dict:
    if not path.is_file():
        return default
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default
    return obj if isinstance(obj, dict) else default


def read_pins(directory) -> dict:
    return _read_doc(Path(directory) / PINS_FILE, empty_pins())


def write_pins(directory, pins: dict) -> Path:
    path = Path(directory) / PINS_FILE
    write_atomic(path, _dump_doc(pins))
    return path


def read_config(directory) -> dict:
    return _read_doc(Path(directory) / CONFIG_FILE, empty_config())


def write_config(directory, config: dict) -> Path:
    path = Path(directory) / CONFIG_FILE
    write_atomic(path, _dump_doc(config))
    return path


def config_mtime(directory) -> Optional[float]:
    try:
        return (Path(directory) / CONFIG_FILE).stat().st_mtime
    except OSError:
        return None


# --- remote.json (SPEC.md §3.3) ------------------------------------------------

def read_remote(directory) -> Optional[dict]:
    """The cached remote config, or None when the file is missing, not
    JSON, or not a ``tryaii.monitor.remote/1`` object ("no remote config":
    exactly the local behaviour)."""
    path = Path(directory) / REMOTE_FILE
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("schema") != REMOTE_SCHEMA:
        return None
    return obj


def write_remote(directory, remote: dict, fetched_at: str) -> Path:
    """Write a server ``RemoteConfig`` plus ``fetched_at`` (last key),
    atomically, indent 2. The server's key order is kept."""
    doc = {k: v for k, v in remote.items() if k != "fetched_at"}
    doc["fetched_at"] = fetched_at
    path = Path(directory) / REMOTE_FILE
    write_atomic(path, _dump_doc(doc))
    return path


# --- upload.json (SPEC.md §3.4) ------------------------------------------------

def empty_upload_cursor() -> dict:
    return {"schema": UPLOAD_CURSOR_SCHEMA, "files": {}, "last_ok": None, "last_error": None}


def read_upload_cursor(directory) -> Optional[dict]:
    """The upload cursor, or None when no upload was ever attempted (or
    the file is unreadable). ``files`` is normalised to name -> int."""
    path = Path(directory) / UPLOAD_FILE
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("schema") != UPLOAD_CURSOR_SCHEMA:
        return None
    files = obj.get("files") if isinstance(obj.get("files"), dict) else {}
    clean = {}
    for name, value in files.items():
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            clean[name] = value
    return {
        "schema": UPLOAD_CURSOR_SCHEMA,
        "files": dict(sorted(clean.items())),
        "last_ok": obj.get("last_ok") if isinstance(obj.get("last_ok"), str) else None,
        "last_error": obj.get("last_error") if isinstance(obj.get("last_error"), str) else None,
    }


def write_upload_cursor(directory, cursor: dict) -> Path:
    """Atomic, indent 2, key order schema, files (sorted), last_ok,
    last_error."""
    doc = {
        "schema": UPLOAD_CURSOR_SCHEMA,
        "files": dict(sorted((cursor.get("files") or {}).items())),
        "last_ok": cursor.get("last_ok"),
        "last_error": cursor.get("last_error"),
    }
    path = Path(directory) / UPLOAD_FILE
    write_atomic(path, _dump_doc(doc))
    return path


def event_files(directory) -> list[Path]:
    """Every ``events-*.jsonl`` of an app directory, sorted by name (= day)."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.name.startswith(EVENTS_PREFIX) and p.name.endswith(".jsonl")
                  and p.is_file())


def uploaded_event_count(directory, cursor: Optional[dict]) -> int:
    """The number of event lines inside the uploaded byte prefixes of the
    cursor's files (every ``\\n`` counts; files that are gone count 0)."""
    if not cursor:
        return 0
    total = 0
    for name, size in (cursor.get("files") or {}).items():
        path = Path(directory) / name
        if not path.is_file() or size <= 0:
            continue
        try:
            with open(path, "rb") as fh:
                total += fh.read(size).count(b"\n")
        except OSError:
            continue
    return total
