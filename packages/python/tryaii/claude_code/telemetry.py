"""Claude Code live telemetry -> TryAii (shared/monitor/SPEC.md §10.8).

``connect`` merges a small block into Claude Code's **user** settings file
(``~/.claude/settings.json``, or ``$CLAUDE_CONFIG_DIR/settings.json``): the
OpenTelemetry exporter variables only take effect from user or managed
settings, never from a project's ``.claude/settings.json``. Claude Code then
sends its OTLP/HTTP JSON log events to
``<api url>/v1/otlp/claude-code/v1/logs`` with the headers that
``tryaii claude-code otel-headers`` prints (the app key stored on this
machine, never in the settings file). The server keeps only the
``claude_code.api_request`` events' model, token, cost, duration and time
fields (API.md §8); the request id is used only to build the event id and is
never stored.

Merge rules: every other setting stays as it was (key order kept, the file's
indent and line endings reused); a timestamped backup is written next to the
file first, and the new file is written atomically. Another tool's telemetry
(a different endpoint, headers helper, exporter or static OTLP headers) is
never overwritten: the plan lists it as a conflict and nothing is written.
What was added is recorded in ``<data_dir>/claude-code/telemetry.json`` so
``disconnect`` removes exactly those keys. The state also holds ``live_since``,
the time live telemetry was turned on: from then on the log import stores only
calls before it (the server's events key is ``(event_id, ts)`` and a live
event's time differs from the log line's by milliseconds, so the shared
``event_id`` alone would not deduplicate). ``disconnect`` clears it.

``connect --no-session`` also writes ``OTEL_METRICS_INCLUDE_SESSION_ID=false``
(Claude Code's documented switch, default true): Claude Code then leaves its
session id out of its telemetry. It is recorded like every other key, so
``disconnect`` removes exactly it; a later connect without the flag leaves it
in place.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from tryaii.auth.store import write_atomic

ROUTE = "/v1/otlp/claude-code/v1/logs"
HELPER_KEY = "otelHeadersHelper"
HELPER_COMMAND = "tryaii claude-code otel-headers"
SETTINGS_FILE = "settings.json"
BACKUP_INFIX = ".tryaii-backup-"
STATE_DIR = "claude-code"
STATE_FILE = "telemetry.json"
STATE_SCHEMA = "tryaii.claude-code.telemetry/1"
KEY_FILE = "telemetry-key.json"
KEY_SCHEMA = "tryaii.claude-code.telemetry-key/1"
NOTICE_HEADER = "X-Tryaii-Notice-Version"

ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"
#: Content switches Claude Code documents (monitoring-usage): all default off.
#: ``connect`` writes an explicit ``0`` for the two below that accept it.
CONTENT_OFF = ("OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS")
#: ``OTEL_LOG_RAW_API_BODIES`` documents only ``1`` and ``file:<dir>``; it is off
#: when unset, so ``connect`` leaves it unset and refuses when it is on.
RAW_BODIES_VAR = "OTEL_LOG_RAW_API_BODIES"
#: Every switch ``status`` warns about when on.
CONTENT_VARS = ("OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS", RAW_BODIES_VAR,
                "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_TOOL_CONTENT")
#: Variables of another telemetry destination: when set, connect stops. Static
#: OTLP headers would also be sent to TryAii (merged with the helper's).
FOREIGN_VARS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS",
                "OTEL_EXPORTER_OTLP_LOGS_HEADERS")
#: Claude Code's switch for its ``session.id`` attribute (default true);
#: ``connect --no-session`` writes ``false``.
SESSION_ID_VAR = "OTEL_METRICS_INCLUDE_SESSION_ID"
SESSION_ID_OFF = "false"
#: Off values (env-vars docs: ``0``, ``false``, ``no``, ``off``, any casing).
OFF_VALUES = ("", "0", "false", "no", "off")


class SettingsError(Exception):
    """The settings file cannot be read or merged safely; nothing was written."""


# --- paths ----------------------------------------------------------------------

def default_claude_dir(env=None) -> Path:
    """``$CLAUDE_CONFIG_DIR`` when set, else ``~/.claude``."""
    env = os.environ if env is None else env
    base = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(base) if base else Path.home() / ".claude"


def settings_path(claude_dir) -> Path:
    return Path(claude_dir) / SETTINGS_FILE


def logs_endpoint(api_url: str) -> str:
    return api_url.rstrip("/") + ROUTE


def desired_env(endpoint: str, no_session: bool = False) -> dict:
    """The ``env`` entries connect adds, in the order it writes them."""
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        ENDPOINT_VAR: endpoint,
        "OTEL_LOG_USER_PROMPTS": "0",
        "OTEL_LOG_TOOL_DETAILS": "0",
    }
    if no_session:
        env[SESSION_ID_VAR] = SESSION_ID_OFF
    return env


def is_on(value) -> bool:
    """A content switch is on unless unset or an off value."""
    if value is None:
        return False
    return str(value).strip().lower() not in OFF_VALUES


# --- reading and writing the settings file ---------------------------------------

class Settings:
    """A parsed settings file and how it was formatted."""

    def __init__(self, path: Path, doc: dict, text: Optional[str]):
        self.path = path
        self.doc = doc
        self.text = text  # None: the file does not exist
        self.indent, self.newline, self.final_newline = _format_of(text)

    @property
    def env(self) -> dict:
        env = self.doc.get("env")
        return env if isinstance(env, dict) else {}

    def render(self, doc: dict) -> str:
        text = json.dumps(doc, indent=self.indent, ensure_ascii=False)
        if self.newline != "\n":
            text = text.replace("\n", self.newline)
        return text + (self.newline if self.final_newline else "")


def _format_of(text: Optional[str]) -> tuple:
    if not text or not text.strip():
        return 2, "\n", True
    newline = "\r\n" if "\r\n" in text else "\n"
    indent = None
    for line in text.splitlines()[1:]:
        match = re.match(r"^([ \t]+)\S", line)
        if match:
            ws = match.group(1)
            indent = "\t" if ws.startswith("\t") else len(ws)
            break
    if indent is None and "\n" in text.strip():
        indent = 2
    return indent, newline, text.endswith("\n")


def read_settings(path) -> Settings:
    path = Path(path)
    try:
        # Bytes, not read_text: keep the file's own line endings (CRLF on Windows).
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SettingsError(f"{path.name} is not UTF-8") from None
    except FileNotFoundError:
        return Settings(path, {}, None)
    except OSError as exc:
        raise SettingsError(f"cannot read {path.name}: {exc.strerror or exc}") from None
    if not text.strip():
        return Settings(path, {}, text)
    try:
        doc = json.loads(text)
    except ValueError:
        raise SettingsError(f"{path.name} is not valid JSON; fix it first (Claude Code "
                            "reports it too), then run connect again") from None
    if not isinstance(doc, dict):
        raise SettingsError(f"{path.name} is not a JSON object")
    if "env" in doc and not isinstance(doc["env"], dict):
        raise SettingsError(f'"env" in {path.name} is not an object')
    return Settings(path, doc, text)


def backup_path(path: Path, stamp: str) -> Path:
    """``settings.json.tryaii-backup-<UTC yyyymmddThhmmssZ>`` next to the file."""
    compact = re.sub(r"[^0-9T]", "", stamp.split(".")[0].rstrip("Z")) + "Z"
    return path.with_name(path.name + BACKUP_INFIX + compact)


def write_settings(settings: Settings, doc: dict, stamp: str) -> Optional[Path]:
    """Back the current file up (when it exists), then write ``doc``
    atomically. Returns the backup path."""
    backup = None
    if settings.text is not None:
        first = backup = backup_path(settings.path, stamp)
        n = 1
        while backup.exists():
            n += 1
            backup = first.with_name(f"{first.name}-{n}")
        backup.write_bytes(settings.path.read_bytes())
    write_atomic(settings.path, settings.render(doc).encode("utf-8"), prefix=".settings-")
    return backup


# --- the plan --------------------------------------------------------------------

def plan_connect(settings: Settings, endpoint: str, owned: Optional[dict] = None,
                 environ=None, no_session: bool = False) -> dict:
    """What connect would change: ``add_env`` / ``add_top`` (to write),
    ``present`` (already set to the wanted value; left alone and not recorded),
    ``conflicts`` (one sentence each; non-empty means stop).

    ``owned`` is the state of an earlier connect: a key whose current value is
    the one TryAii wrote may be updated (a new API URL). ``no_session`` adds
    ``OTEL_METRICS_INCLUDE_SESSION_ID=false`` (an off value already there is
    left alone; an on value is a conflict, never overwritten)."""
    environ = os.environ if environ is None else environ
    owned = owned or {}
    owned_env = owned.get("env") or {}
    owned_top = owned.get("top") or {}
    env = settings.env
    add_env, add_top, present, conflicts = {}, {}, [], []
    name = settings.path.name

    for var in FOREIGN_VARS:
        if var in env:
            conflicts.append(f"{name} sets {var}: another tool's telemetry destination or "
                             "headers")
        elif (environ.get(var) or "").strip():
            conflicts.append(f"your environment sets {var}: another tool's telemetry "
                             "destination or headers")
    shell_endpoint = (environ.get(ENDPOINT_VAR) or "").strip()
    if shell_endpoint and shell_endpoint != endpoint and ENDPOINT_VAR not in env:
        conflicts.append(f"your environment sets {ENDPOINT_VAR} to another destination")
    if "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL" in env and \
            str(env["OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"]) != "http/json":
        conflicts.append(f"{name} sets OTEL_EXPORTER_OTLP_LOGS_PROTOCOL="
                         f"{env['OTEL_EXPORTER_OTLP_LOGS_PROTOCOL']}")
    if is_on(env.get(RAW_BODIES_VAR)):
        conflicts.append(f"{name} turns on {RAW_BODIES_VAR} (whole conversations); turn it "
                         "off first")

    for var, wanted in desired_env(endpoint, no_session).items():
        if var not in env:
            add_env[var] = wanted
            continue
        current = env[var]
        if (current == wanted or (var in CONTENT_OFF and not is_on(current))
                or (var == SESSION_ID_VAR and _is_off(current))):
            present.append(var)
        elif var in owned_env and current == owned_env[var]:
            add_env[var] = wanted  # ours, from an earlier connect
        elif var in CONTENT_OFF:
            conflicts.append(f"{name} turns on {var}; TryAii does not overwrite it -- set it "
                             "to 0 or remove it")
        elif var == SESSION_ID_VAR:
            conflicts.append(f"{name} sets {var}={current}; for --no-session set it to false "
                             "yourself, or connect without --no-session")
        else:
            conflicts.append(f"{name} already sets {var}={current} (TryAii needs {wanted})")

    current = settings.doc.get(HELPER_KEY)
    if current is None:
        add_top[HELPER_KEY] = HELPER_COMMAND
    elif current == HELPER_COMMAND:
        present.append(HELPER_KEY)
    elif HELPER_KEY in owned_top and current == owned_top[HELPER_KEY]:
        add_top[HELPER_KEY] = HELPER_COMMAND
    else:
        conflicts.append(f"{name} already has {HELPER_KEY} set to another command")
    return {"add_env": add_env, "add_top": add_top, "present": present,
            "conflicts": conflicts, "endpoint": endpoint}


def _is_off(value) -> bool:
    """Set to an off value (unlike ``is_on``, an empty value is not off)."""
    return value is not None and str(value).strip() != "" and not is_on(value)


def session_id_setting(settings: Optional[Settings], environ=None) -> dict:
    """Whether Claude Code sends its session id: ``{"sent", "where", "value"}``.
    The settings file's ``env`` wins over the environment; unset means sent
    (Claude Code's default)."""
    environ = os.environ if environ is None else environ
    env = settings.env if settings is not None else {}
    if SESSION_ID_VAR in env:
        value, where = env[SESSION_ID_VAR], settings.path.name
    elif SESSION_ID_VAR in environ:
        value, where = environ[SESSION_ID_VAR], "your environment"
    else:
        return {"sent": True, "where": None, "value": None}
    return {"sent": not _is_off(value), "where": where, "value": str(value)}


def block(plan: dict) -> dict:
    """The keys connect writes, shaped like the settings file."""
    doc: dict = {}
    if plan["add_env"]:
        doc["env"] = dict(plan["add_env"])
    doc.update(plan["add_top"])
    return doc


def merged(doc: dict, plan: dict) -> dict:
    """``doc`` with the plan's keys: existing keys keep their place, new ones
    go to the end of ``env`` (created at the end when missing) and of the top
    level."""
    out = dict(doc)
    if plan["add_env"]:
        env = dict(out["env"]) if isinstance(out.get("env"), dict) else {}
        env.update(plan["add_env"])
        out["env"] = env
    out.update(plan["add_top"])
    return out


def removed(doc: dict, state: dict) -> tuple[dict, list, list]:
    """``doc`` without the keys TryAii added (only where they still hold the
    value TryAii wrote). Returns ``(doc, removed_keys, kept_keys)``."""
    out = dict(doc)
    gone, kept = [], []
    env = dict(out["env"]) if isinstance(out.get("env"), dict) else None
    for var, value in (state.get("env") or {}).items():
        if env is not None and var in env:
            if env[var] == value:
                del env[var]
                gone.append(var)
            else:
                kept.append(var)
    if env is not None:
        if not env and state.get("created_env"):
            del out["env"]
        else:
            out["env"] = env
    for key, value in (state.get("top") or {}).items():
        if key in out:
            if out[key] == value:
                del out[key]
                gone.append(key)
            else:
                kept.append(key)
    return out, gone, kept


def content_warnings(settings: Optional[Settings], environ=None) -> list:
    """``(variable, where)`` for every content switch that is on."""
    environ = os.environ if environ is None else environ
    found = []
    env = settings.env if settings is not None else {}
    for var in CONTENT_VARS:
        if is_on(env.get(var)):
            found.append((var, settings.path.name))
        elif is_on(environ.get(var)):
            found.append((var, "your environment"))
    return found


# --- local state ------------------------------------------------------------------

def state_path(data_dir) -> Path:
    return Path(data_dir) / STATE_DIR / STATE_FILE


def key_path(data_dir) -> Path:
    return Path(data_dir) / STATE_DIR / KEY_FILE


def _read_json(path: Path) -> Optional[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def read_state(data_dir) -> Optional[dict]:
    doc = _read_json(state_path(data_dir))
    return doc if doc and doc.get("schema") == STATE_SCHEMA else None


def write_state(data_dir, *, settings_file: Path, plan: dict, created_env: bool, now: str,
                backup: Optional[Path]) -> Path:
    previous = read_state(data_dir) or {}
    env = dict(previous.get("env") or {})
    env.update(plan["add_env"])
    top = dict(previous.get("top") or {})
    top.update(plan["add_top"])
    doc = {"schema": STATE_SCHEMA, "settings_path": str(settings_file),
           "endpoint": plan["endpoint"], "env": env, "top": top,
           "created_env": bool(previous.get("created_env")) or created_env,
           "connected_at": previous.get("connected_at") or now, "updated_at": now,
           "live_since": previous.get("live_since") or now,
           "backup": str(backup) if backup else previous.get("backup")}
    path = state_path(data_dir)
    write_atomic(path, (json.dumps(doc, indent=2) + "\n").encode("utf-8"))
    return path


def write_key(data_dir, key: str) -> Path:
    """The app key the headers helper prints (0600 on POSIX)."""
    path = key_path(data_dir)
    write_atomic(path, (json.dumps({"schema": KEY_SCHEMA, "app_key": key}) + "\n")
                 .encode("utf-8"), prefix=".telemetry-key-")
    return path


def read_key(data_dir) -> Optional[str]:
    doc = _read_json(key_path(data_dir))
    if not doc or doc.get("schema") != KEY_SCHEMA:
        return None
    key = doc.get("app_key")
    return key if isinstance(key, str) and key else None


def headers(key: str) -> dict:
    """The helper's output: the app key and the Data Notice version this SDK
    carries (the same value an upload batch sends as ``notice_version``)."""
    from tryaii.monitor.privacy import NOTICE_VERSION

    return {"Authorization": f"Bearer {key}", NOTICE_HEADER: NOTICE_VERSION}


def live_since(state: Optional[dict]) -> Optional[str]:
    """The cutoff (SPEC.md §10.8): calls from this time on come by live
    telemetry, so the log import stores only calls before it."""
    value = (state or {}).get("live_since")
    return value if isinstance(value, str) and value else None


def clear_state(data_dir) -> list:
    removed_paths = []
    for path in (key_path(data_dir), state_path(data_dir)):
        if path.exists():
            path.unlink()
            removed_paths.append(path)
    return removed_paths
