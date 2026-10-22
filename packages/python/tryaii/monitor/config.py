"""Mode, app and process resolution (SPEC.md §2.6). Pure functions."""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from tryaii.monitor.winner import MODE_MANAGED, MODES

CONFIG_SCHEMA = "tryaii.monitor.config/1"
REMOTE_SCHEMA = "tryaii.monitor.remote/1"
ENV_MODE = "TRYAII_MONITOR_MODE"
ENV_APP = "TRYAII_APP"
DEFAULT_APP = "default"
DEFAULT_PROCESS = "default"
MSG_BAD_MODE = "mode must be off, stable, dynamic or managed"
MSG_MANAGED_NEEDS_MODEL = "managed mode needs --model <id>"
_APP_RE = re.compile(r"[^a-z0-9._-]+")


def empty_config() -> dict:
    return {"schema": CONFIG_SCHEMA, "source": "local", "default_mode": "off", "processes": {}}


def _valid_mode(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip().lower() in MODES:
        return value.strip().lower()
    return None


def _clean_model(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _config_mode(config: Optional[dict], process: str) -> Optional[tuple]:
    """``(mode, model)`` from a config-shaped document (config.json or
    remote.json): the process entry first, then ``default_mode``. ``model``
    is the entry's managed model (None at the default level and for the
    other modes)."""
    if not isinstance(config, dict):
        return None
    processes = config.get("processes") or {}
    entry = processes.get(process) if isinstance(processes, dict) else None
    if isinstance(entry, dict):
        mode = _valid_mode(entry.get("mode"))
        if mode:
            model = _clean_model(entry.get("model")) if mode == MODE_MANAGED else None
            return mode, model
    mode = _valid_mode(config.get("default_mode"))
    if mode:
        return mode, None
    return None


def resolve_mode(process: str, *, env: Optional[dict] = None,
                 remote: Optional[dict] = None, args: Optional[str] = None,
                 config: Optional[dict] = None) -> dict:
    """``{"mode", "source", "model"}`` — precedence env > remote > args >
    config > default (``off``). Invalid values at a level are ignored.
    ``model`` is the managed model of the level that set the mode (only a
    per-process entry of remote.json / config.json carries one), else None.
    """
    env = env or {}
    mode = _valid_mode(env.get(ENV_MODE))
    if mode:
        return {"mode": mode, "source": "env", "model": None}
    found = _config_mode(remote, process)
    if found:
        return {"mode": found[0], "source": "remote", "model": found[1]}
    mode = _valid_mode(args)
    if mode:
        return {"mode": mode, "source": "args", "model": None}
    found = _config_mode(config, process)
    if found:
        return {"mode": found[0], "source": "config", "model": found[1]}
    return {"mode": "off", "source": "default", "model": None}


def sanitize_app(value: Any) -> Optional[str]:
    """Lower-case, ``[a-z0-9._-]`` only, runs collapsed to ``-``; None when
    nothing is left."""
    if not isinstance(value, str):
        return None
    text = _APP_RE.sub("-", value.strip().lower()).strip("-.")
    return text or None


def resolve_app(*, env: Optional[dict] = None, args: Optional[str] = None,
                cwd_name: Optional[str] = None) -> dict:
    """``{"app", "source"}`` — env ``TRYAII_APP`` > ``wrap(app=)`` > the
    working directory's name > ``default``."""
    env = env or {}
    for source, value in (("env", env.get(ENV_APP)), ("args", args), ("cwd", cwd_name)):
        app = sanitize_app(value)
        if app:
            return {"app": app, "source": source}
    return {"app": DEFAULT_APP, "source": "default"}


def call_site_process(call_site: Optional[dict]) -> Optional[str]:
    """``basename(file):function`` (``:L<line>`` when the function is
    unknown) for a call site; forward and back slashes both separate."""
    if not isinstance(call_site, dict) or not call_site.get("file"):
        return None
    base = re.split(r"[\\/]", str(call_site["file"]))[-1]
    function = call_site.get("function")
    if function:
        return f"{base}:{function}"
    line = call_site.get("line")
    return f"{base}:L{line}" if line is not None else base


def _clean_name(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_process(*, per_call: Any = None, context: Any = None,
                    default: Any = None, call_site: Optional[dict] = None) -> dict:
    """``{"process", "source"}`` — per-call ``tryaii_process`` > the
    ``process(name)`` context > ``wrap(process=)`` > the call site >
    ``default``."""
    for source, value in (("call", per_call), ("context", context), ("args", default)):
        name = _clean_name(value)
        if name:
            return {"process": name, "source": source}
    name = call_site_process(call_site)
    if name:
        return {"process": name, "source": "call_site"}
    return {"process": DEFAULT_PROCESS, "source": "default"}


def set_process_mode(config: Optional[dict], process: str, mode: str,
                     model: Optional[str] = None) -> dict:
    """A new config document with ``process`` set to ``mode``.

    The entry is rebuilt in contract key order ``mode``, ``model`` (only for
    ``managed``), ``providers`` (kept, or null), then any other keys it
    already had. ``managed`` needs a non-empty ``model``; a ``model`` with
    another mode is ignored and a stale one is dropped.
    """
    if mode not in MODES:
        raise ValueError(MSG_BAD_MODE)
    model = _clean_model(model)
    if mode == MODE_MANAGED and model is None:
        raise ValueError(MSG_MANAGED_NEEDS_MODEL)
    base = config if isinstance(config, dict) else {}
    processes = dict(base.get("processes") or {})
    old = processes.get(process)
    old = dict(old) if isinstance(old, dict) else {}
    entry: dict = {"mode": mode}
    if mode == MODE_MANAGED:
        entry["model"] = model
    entry["providers"] = old.get("providers")
    for key, value in old.items():
        if key not in ("mode", "model", "providers"):
            entry[key] = value
    processes[process] = entry
    return {
        "schema": CONFIG_SCHEMA,
        "source": base.get("source") or "local",
        "default_mode": _valid_mode(base.get("default_mode")) or "off",
        "processes": dict(sorted(processes.items())),
    }


def env_from_os() -> dict:
    """The monitor-relevant environment, for callers that are not pure: the
    mode and app overrides plus the upload opt-outs (SPEC.md §9)."""
    keys = (ENV_MODE, ENV_APP, "TRYAII_MONITOR_UPLOAD", "TRYAII_TELEMETRY", "DO_NOT_TRACK")
    return {k: v for k, v in os.environ.items() if k in keys}
