"""`tryaii monitor` -- verb dispatcher and renderers (shared/monitor/SPEC.md §5).

Verb-peeling like `tryaii diagnose`: the command is not registered with
argparse; `cmd_monitor(argv)` receives everything after the command name.
Every message, table and exit code here is byte-identical to the Node CLI
(packages/node/src/cli.ts, cmdMonitor) and frozen by the `cli` fixture
suite in shared/monitor/fixtures.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VERBS = ("status", "set", "apps", "events", "upload")
WINDOWS = ("24h", "7d", "30d")
DEFAULT_EVENTS = 20
HELP_HINT = 'Run "tryaii help monitor".'

# Flags that take a value; everything else is a switch.
_VALUE_FLAGS = {"--app": "app", "--since": "since", "-n": "n", "--now": "now",
                "--model": "model"}
_SWITCH_FLAGS = {"--json": "json"}


class MonitorUsageError(Exception):
    """A usage error: message to stderr, exit 2."""


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(2)


def _parse(argv: list) -> tuple[list, dict]:
    """Split ``argv`` into positionals and options.

    ``--flag value`` and ``--flag=value`` both work; unknown options are a
    usage error so a typo never silently changes a window or an app.
    """
    positionals: list = []
    opts: dict = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1:])
            break
        name, eq, inline = arg.partition("=")
        if name in _VALUE_FLAGS:
            key = _VALUE_FLAGS[name]
            if eq:
                value = inline
            else:
                if i + 1 >= len(argv):
                    raise MonitorUsageError(f"{name} needs a value. {HELP_HINT}")
                i += 1
                value = argv[i]
            opts[key] = value
        elif arg in _SWITCH_FLAGS:
            opts[_SWITCH_FLAGS[arg]] = True
        elif arg.startswith("-") and arg != "-":
            raise MonitorUsageError(f"unknown option: {arg}. {HELP_HINT}")
        else:
            positionals.append(arg)
        i += 1
    return positionals, opts


def _only_set_takes_model(opts: dict) -> None:
    if "model" in opts:
        raise MonitorUsageError(f"unknown option: --model. {HELP_HINT}")


def _now_arg(value: Optional[str]) -> str:
    from tryaii.monitor.events import format_ts, parse_ts

    if value is None:
        return format_ts(datetime.now(timezone.utc))
    try:
        return format_ts(parse_ts(value))
    except ValueError:
        raise MonitorUsageError(
            "--now must be an ISO-8601 UTC timestamp (e.g. 2026-10-11T12:00:00Z)") from None


def _since_arg(value: Optional[str]) -> str:
    if value is None:
        return "24h"
    if value not in WINDOWS:
        raise MonitorUsageError("--since must be 24h, 7d or 30d")
    return value


def _count_arg(value: Optional[str]) -> int:
    if value is None:
        return DEFAULT_EVENTS
    try:
        n = int(value, 10)
    except ValueError:
        n = 0
    if n <= 0 or str(n) != value.strip():
        raise MonitorUsageError("-n must be a positive integer")
    return n


def _data_dir() -> Path:
    from tryaii.config import TryaiiDreConfig

    return Path(TryaiiDreConfig().data_dir)


def _display(path) -> str:
    return str(path).replace("\\", "/")


def _no_records(root: Path) -> str:
    return f"no monitor records under {_display(root)}"


def _select_app(apps: list, opts: dict, env: dict) -> str:
    """The one app a `set`/`events` call targets: --app, then TRYAII_APP,
    then the only app with records; otherwise a usage error naming them."""
    from tryaii.monitor.config import ENV_APP

    explicit = opts.get("app") or env.get(ENV_APP)
    if explicit:
        if explicit not in apps:
            raise MonitorUsageError(f"unknown app '{explicit}'")
        return explicit
    if len(apps) == 1:
        return apps[0]
    if not apps:
        raise MonitorUsageError("--app is required (no monitor records yet)")
    raise MonitorUsageError("--app is required (apps: " + ", ".join(apps) + ")")


# --- rendering -------------------------------------------------------------

def _money(value) -> str:
    return "-" if value is None else f"{value:.4f}"


def _int(value) -> str:
    return "-" if value is None else str(int(value))


def _share(top) -> str:
    if not top:
        return "-"
    pct = math.floor(top["share"] * 100 + 0.5)
    return f"{top['model']} {pct}%"


STATUS_COLUMNS = ("process", "mode", "pinned", "calls", "tokens", "est cost",
                  "at winner", "savings", "top winner share")
# Right-aligned numeric columns (by header name).
_RIGHT = {"calls", "tokens", "est cost", "at winner", "savings"}


def _mode_cell(p: dict) -> str:
    if p["mode"] == "managed":
        return f"managed ({p.get('managed_model') or '-'})"
    return p["mode"]


def _status_rows(doc: dict) -> list:
    rows = []
    for p in doc["processes"]:
        rows.append((
            p["process"],
            _mode_cell(p),
            p["pinned"]["model"] if p.get("pinned") else "-",
            _int(p["calls"]),
            _int(p["total_tokens"]),
            _money(p["est_cost_usd"]),
            _money(p["est_cost_winner_usd"]),
            _money(p["savings_usd"]),
            _share(p.get("top_winner")),
        ))
    return rows


def _table(headers: tuple, rows: list) -> list:
    """Space-aligned table: two spaces between columns, text left-aligned,
    numbers right-aligned, no trailing whitespace."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = []
    for row in (headers, *rows):
        cells = []
        for i, cell in enumerate(row):
            if headers[i] in _RIGHT and row is not headers:
                cells.append(cell.rjust(widths[i]))
            else:
                cells.append(cell.ljust(widths[i]))
        out.append("  ".join(cells).rstrip())
    return out


def _source_line(source: dict) -> str:
    if source.get("kind") == "remote":
        return (f"source: remote etag {source.get('etag') or '-'} "
                f"fetched {source.get('fetched_at') or '-'}")
    return "source: local"


def _upload_line(upload: dict, off_reason: Optional[str] = None) -> str:
    if off_reason:
        return f"upload: off ({off_reason})"
    state = upload.get("state")
    if state == "error":
        return f"upload: error {upload.get('last_error')}"
    if state == "ok":
        return f"upload: ok {upload.get('events', 0)} events at {upload.get('last_ok') or '-'}"
    return "upload: off"


def _classification_line(data_dir) -> Optional[str]:
    """``classification: off since <ts> (<reason>)`` when the monitor last
    found the embedding model unavailable on this machine (TS-252), else None.
    Machine-wide, so printed once after every app's block."""
    from tryaii.embeddings.model_store import read_state

    state = read_state(Path(data_dir))
    if not state:
        return None
    return (f"classification: off since {state.get('since') or '-'} "
            f"({state.get('reason') or 'embedding model not available'})")


def render_status(doc: dict, off_reason: Optional[str] = None) -> str:
    lines = [f"tryaii monitor -- app: {doc['app']} (last {doc['since']})", ""]
    if not doc["processes"]:
        lines.append("no calls in this window")
    else:
        table = _table(STATUS_COLUMNS, _status_rows(doc))
        lines.append(table[0])
        for row_text, p in zip(table[1:], doc["processes"]):
            lines.append(row_text)
            for file in p["files"]:
                lines.append(f"    -> {file}")
        t = doc["totals"]
        lines.append("")
        lines.append(
            f"totals: {t['calls']} calls, {t['errors']} errors, est cost "
            f"{_money(t['est_cost_usd'])}, at winner {_money(t['est_cost_winner_usd'])}, "
            f"savings {_money(t['savings_usd'])}")
        lines.append(f"realised: {t['swapped_calls']} swapped calls, "
                     f"saved {_money(t['realised_savings_usd'])}")
    lines.append(_source_line(doc["source"]))
    lines.append(_upload_line(doc["upload"], off_reason))
    return "\n".join(lines) + "\n"


def render_event(event: dict) -> str:
    usage = event.get("usage") or {}
    error = event.get("error")
    parts = [
        f"{event.get('ts', '-')}  {event.get('process') or '-'}",
        str(event.get("model_used") or "-"),
        str(event.get("mode") or "-"),
        f"winner {event.get('winner') or '-'}",
        f"{_int(usage.get('total_tokens'))} tok",
    ]
    # v2.2 (SPEC.md §5.5): the cache segments only when the field is a number.
    for key, label in (("cached_tokens", "cached"), ("cache_write_tokens", "cache write")):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{label} {_int(value)}")
    parts += [
        _money(event.get("est_cost_usd")),
        str(event.get("route_status") or "-"),
    ]
    if error:
        label = error.get("class") or error.get("status") or "error"
        parts.append(f"error {label}")
    return " | ".join(parts)


# --- verbs -----------------------------------------------------------------

def _verb_status(argv: list, env: dict) -> None:
    from tryaii.monitor import store
    from tryaii.monitor.status import aggregate, window_seconds

    positionals, opts = _parse(argv)
    _only_set_takes_model(opts)
    if positionals:
        raise MonitorUsageError(f"unexpected argument: {positionals[0]}. {HELP_HINT}")
    now = _now_arg(opts.get("now"))
    since = _since_arg(opts.get("since"))
    data_dir = _data_dir()
    apps = store.list_apps(data_dir)
    if opts.get("app"):
        if opts["app"] not in apps:
            raise MonitorUsageError(f"unknown app '{opts['app']}'")
        apps = [opts["app"]]
    docs = []
    for app in apps:
        directory = store.app_dir(data_dir, app)
        events = store.read_events(directory, now, window_seconds(since))
        cursor = store.read_upload_cursor(directory)
        docs.append(aggregate(app, events, store.read_pins(directory),
                              store.read_config(directory), now, since, env=env,
                              remote=store.read_remote(directory), upload_cursor=cursor,
                              uploaded_events=store.uploaded_event_count(directory, cursor)))
    if opts.get("json"):
        payload = docs[0] if opts.get("app") else docs
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if not docs:
        print(_no_records(store.monitor_root(data_dir)))
        return
    from tryaii.monitor.privacy import upload_decision

    _enabled, off_reason = upload_decision(env, store.monitor_root(data_dir))
    print("\n".join(render_status(doc, off_reason) for doc in docs), end="")
    line = _classification_line(data_dir)
    if line:
        print(line)


def _verb_set(argv: list, env: dict) -> None:
    from tryaii.monitor import store
    from tryaii.monitor.config import (
        MODES,
        MSG_BAD_MODE,
        MSG_MANAGED_NEEDS_MODEL,
        set_process_mode,
    )

    positionals, opts = _parse(argv)
    if len(positionals) != 2:
        raise MonitorUsageError(
            f"usage: tryaii monitor set <process> <mode> [--model <id>]. {HELP_HINT}")
    process, mode = positionals
    if mode not in MODES:
        raise MonitorUsageError(MSG_BAD_MODE)
    model = opts.get("model")
    model = model.strip() if isinstance(model, str) else None
    if mode == "managed" and not model:
        raise MonitorUsageError(MSG_MANAGED_NEEDS_MODEL)
    if mode != "managed" and model is not None:
        raise MonitorUsageError("--model only applies to managed mode")
    if not process.strip():
        raise MonitorUsageError("process name must not be empty")
    data_dir = _data_dir()
    app = _select_app(store.list_apps(data_dir), opts, env)
    directory = store.app_dir(data_dir, app)
    config = set_process_mode(store.read_config(directory), process, mode, model)
    store.write_config(directory, config)
    if opts.get("json"):
        payload = {"app": app, "process": process, "mode": mode}
        if mode == "managed":
            payload["model"] = model
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    shown = f"managed ({model})" if mode == "managed" else mode
    print(f"{process}: {shown} (app {app})")


def _verb_apps(argv: list, env: dict) -> None:
    from tryaii.monitor import store

    positionals, opts = _parse(argv)
    _only_set_takes_model(opts)
    if positionals:
        raise MonitorUsageError(f"unexpected argument: {positionals[0]}. {HELP_HINT}")
    data_dir = _data_dir()
    apps = store.list_apps(data_dir)
    if opts.get("json"):
        print(json.dumps(apps, indent=2, ensure_ascii=False))
        return
    if not apps:
        print(_no_records(store.monitor_root(data_dir)))
        return
    print("\n".join(apps))


def _verb_events(argv: list, env: dict) -> None:
    from tryaii.monitor import store

    positionals, opts = _parse(argv)
    _only_set_takes_model(opts)
    if positionals:
        raise MonitorUsageError(f"unexpected argument: {positionals[0]}. {HELP_HINT}")
    limit = _count_arg(opts.get("n"))
    data_dir = _data_dir()
    app = _select_app(store.list_apps(data_dir), opts, env)
    events = store.read_recent_events(store.app_dir(data_dir, app), limit)
    if opts.get("json"):
        for event in events:
            print(store.dumps_line(event), end="")
        return
    if not events:
        print(f"no events for app {app}")
        return
    print("\n".join(render_event(e) for e in events))


UPLOAD_ACTIONS = ("status", "on", "off")


def _record_cli_notice(root: Path) -> None:
    """The person just saw the notice in a terminal: remember it locally and,
    when signed in, record it on the account (API.md §7.2). Best effort."""
    from tryaii.monitor import privacy

    if not privacy.can_show_notice(sys.stdout):
        return
    privacy.mark_notice_shown(root, _now_arg(None))
    try:
        from tryaii.cli.apps import record_consent

        record_consent("cli")
    except Exception:  # noqa: BLE001 -- never fails the command
        pass


def _verb_upload(argv: list, env: dict) -> None:
    """`tryaii monitor upload [status|on|off]` (SPEC.md §9.3)."""
    from tryaii.monitor import privacy, store

    positionals, opts = _parse(argv)
    _only_set_takes_model(opts)
    if len(positionals) > 1 or (positionals and positionals[0] not in UPLOAD_ACTIONS):
        raise MonitorUsageError(f"usage: tryaii monitor upload [on|off]. {HELP_HINT}")
    action = positionals[0] if positionals else "status"
    root = store.monitor_root(_data_dir())
    if action in ("on", "off"):
        privacy.write_local_upload(root, action == "on")
    enabled, reason = privacy.upload_decision(env, root)
    if opts.get("json"):
        print(json.dumps({"upload": enabled, "reason": reason,
                          "notice_version": privacy.NOTICE_VERSION}, indent=2, ensure_ascii=False))
        return
    if action != "off":
        print(privacy.NOTICE_TEXT)
    print(f"upload: off ({reason})" if not enabled else "upload: on")
    if action != "off":
        _record_cli_notice(root)


_VERB_HANDLERS = {
    "status": _verb_status,
    "set": _verb_set,
    "apps": _verb_apps,
    "events": _verb_events,
    "upload": _verb_upload,
}


def cmd_monitor(argv: list) -> None:
    """Verb dispatcher for `tryaii monitor` (mirrors cmdMonitor in cli.ts)."""
    from tryaii.monitor.config import env_from_os

    verb = argv[0] if argv else None
    if verb is None:
        _fail(f"missing monitor verb. {HELP_HINT}")
    handler = _VERB_HANDLERS.get(verb)
    if handler is None:
        _fail(f"unknown monitor verb: {verb}. {HELP_HINT}")
    try:
        handler(argv[1:], env_from_os())
    except MonitorUsageError as exc:
        _fail(str(exc))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- same one-line contract as main()
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
