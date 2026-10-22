"""`tryaii apps` -- tryaii.com apps and app keys (shared/monitor/API.md §5,
shared/monitor/SPEC.md §7).

Verb-peeling like `tryaii monitor`: `cmd_apps(argv)` receives everything
after the command name. `create`, `list` and `keys` act as the signed-in
user (the device-flow JWT from `tryaii login`); `check` validates an app key
(`TRYAII_API_KEY` or `--key`). Every message, table and exit code is
byte-identical to the Node CLI and frozen by the `apps` fixture suite, which
runs against canned HTTP answers (env `TRYAII_HTTP_FIXTURE`, SPEC.md §6.3).
"""

from __future__ import annotations

import json
import sys
from typing import Optional

VERBS = ("create", "list", "keys", "check")
HELP_HINT = 'Run "tryaii help apps".'
KEY_WARNING = ("warning: this is the only time the full key is shown; "
               "store it now (e.g. as TRYAII_API_KEY)")
DEFAULT_KEY_LABEL = "default"
NAME_MAX = 64

_VALUE_FLAGS = {"--label": "label", "--revoke": "revoke", "--key": "key"}
_SWITCH_FLAGS = {"--json": "json", "--new": "new"}
# Which options each verb accepts (anything else is an unknown option).
_VERB_OPTIONS = {
    "create": {"json"},
    "list": {"json"},
    "keys": {"json", "new", "label", "revoke"},
    "check": {"json", "key"},
}


class AppsUsageError(Exception):
    """Usage error: message to stderr, exit 2."""


class AppsRuntimeError(Exception):
    """Server/network/auth failure: message to stderr, exit 1."""


def _parse(argv: list, verb: str) -> tuple[list, dict]:
    positionals: list = []
    opts: dict = {}
    allowed = _VERB_OPTIONS[verb]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1:])
            break
        name, eq, inline = arg.partition("=")
        if name in _VALUE_FLAGS and _VALUE_FLAGS[name] in allowed:
            if eq:
                value = inline
            else:
                if i + 1 >= len(argv):
                    raise AppsUsageError(f"{name} needs a value. {HELP_HINT}")
                i += 1
                value = argv[i]
            opts[_VALUE_FLAGS[name]] = value
        elif arg in _SWITCH_FLAGS and _SWITCH_FLAGS[arg] in allowed:
            opts[_SWITCH_FLAGS[arg]] = True
        elif arg.startswith("-") and arg != "-":
            raise AppsUsageError(f"unknown option: {arg}. {HELP_HINT}")
        else:
            positionals.append(arg)
        i += 1
    return positionals, opts


# --- HTTP ----------------------------------------------------------------------

def _call(method: str, base: str, path: str, bearer: str, body=None):
    from tryaii.monitor import api

    try:
        return api.request(method, f"{base}{path}", body, bearer=bearer)
    except api.ApiNetworkError:
        raise AppsRuntimeError(f"could not reach {base}") from None


def _server_error(resp) -> str:
    """``<error>[: <detail>]`` from an error body, else the HTTP status."""
    if resp.error:
        return f"{resp.error}: {resp.detail}" if resp.detail else resp.error
    return f"unexpected response (HTTP {resp.status})"


def _session() -> tuple[str, str]:
    """``(api_url, access_token)`` of the signed-in user, refreshing the
    access token when it is about to expire (the rotated refresh token is
    saved). Raises AppsRuntimeError when signed out or the session ended."""
    from tryaii.auth import flow, store
    from tryaii.auth.transport import AuthError, session_api_url

    creds = store.load()
    if creds is None:
        raise AppsRuntimeError("not signed in (run tryaii login)")
    api_url = session_api_url(creds.get("api_url"))
    try:
        creds, refreshed = flow.refresh_if_needed(api_url, creds)
    except AuthError as exc:
        if exc.is_network:
            raise AppsRuntimeError(f"could not reach {api_url}") from None
        raise AppsRuntimeError("session expired or revoked (run tryaii login)") from None
    if refreshed:
        store.save(creds)
    return api_url, creds["access_token"]


def _user_call(method: str, path: str, body=None):
    base, token = _session()
    resp = _call(method, base, path, token, body)
    if resp.status == 401:
        raise AppsRuntimeError("session expired or revoked (run tryaii login)")
    return resp


def record_consent(source: str) -> bool:
    """POST /v1/monitor/consent for the signed-in user (API.md §7.2): call it
    only right after the Data Notice was printed to a terminal. True when
    recorded; never raises."""
    from tryaii.monitor.privacy import NOTICE_VERSION

    try:
        resp = _user_call("POST", "/v1/monitor/consent",
                          {"notice_version": NOTICE_VERSION, "source": source})
    except Exception:  # noqa: BLE001 -- best effort
        return False
    return resp.status == 204


def _show_notice_after_create() -> None:
    """`apps create` starts uploads: show the Data Notice to the person (a
    terminal only) and record that it was shown."""
    from tryaii.monitor import privacy, store

    if not privacy.can_show_notice(sys.stderr):
        return
    sys.stderr.write("\n" + privacy.NOTICE_TEXT)
    try:
        from datetime import datetime, timezone

        from tryaii.config import TryaiiDreConfig
        from tryaii.monitor.events import format_ts

        privacy.mark_notice_shown(store.monitor_root(TryaiiDreConfig().data_dir),
                                  format_ts(datetime.now(timezone.utc)))
    except Exception:  # noqa: BLE001 -- best effort
        pass
    record_consent("cli")


# --- rendering -----------------------------------------------------------------

def _table(headers: tuple, rows: list, right: set) -> list:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = []
    for row in (headers, *rows):
        cells = []
        for i, cell in enumerate(row):
            if headers[i] in right and row is not headers:
                cells.append(cell.rjust(widths[i]))
            else:
                cells.append(cell.ljust(widths[i]))
        out.append("  ".join(cells).rstrip())
    return out


def _money(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{value:.4f}"


def _int(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return str(int(value))


def _text(value) -> str:
    return value if isinstance(value, str) and value else "-"


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _print_new_key(key: str) -> None:
    print(f"key: {key}")
    print(KEY_WARNING, file=sys.stderr)


# --- verbs ---------------------------------------------------------------------

def _verb_create(argv: list) -> None:
    positionals, opts = _parse(argv, "create")
    if len(positionals) != 1:
        raise AppsUsageError(f"usage: tryaii apps create <name>. {HELP_HINT}")
    name = positionals[0].strip()
    if not name or len(name) > NAME_MAX:
        raise AppsUsageError(f"app name must be 1-{NAME_MAX} characters")
    resp = _user_call("POST", "/v1/apps", {"name": name})
    if resp.status == 409 and resp.error == "name_taken":
        raise AppsRuntimeError(f"name_taken: you already have an app named '{name}'")
    if resp.status != 201 or not isinstance(resp.body, dict):
        raise AppsRuntimeError(_server_error(resp))
    app = resp.body
    app_id = app.get("app_id")
    if not opts.get("json"):
        print(f"created app {app_id} ({app.get('name', name)})")
    key_resp = _user_call("POST", f"/v1/apps/{app_id}/keys", {"label": DEFAULT_KEY_LABEL})
    if key_resp.status != 201 or not isinstance(key_resp.body, dict) \
            or not isinstance(key_resp.body.get("key"), str):
        if opts.get("json"):
            _dump({"app": app, "key": None})
        raise AppsRuntimeError(
            f"app created but no key: {_server_error(key_resp)} "
            f"(retry with tryaii apps keys {app_id} --new)")
    if opts.get("json"):
        _dump({"app": app, "key": key_resp.body})
        print(KEY_WARNING, file=sys.stderr)
        return
    _print_new_key(key_resp.body["key"])
    _show_notice_after_create()


APP_COLUMNS = ("app id", "name", "keys", "calls 24h", "cost 24h", "last event")
_APP_RIGHT = {"keys", "calls 24h", "cost 24h"}


def _verb_list(argv: list) -> None:
    positionals, opts = _parse(argv, "list")
    if positionals:
        raise AppsUsageError(f"unexpected argument: {positionals[0]}. {HELP_HINT}")
    resp = _user_call("GET", "/v1/apps")
    apps = resp.body.get("apps") if isinstance(resp.body, dict) else None
    if resp.status != 200 or not isinstance(apps, list):
        raise AppsRuntimeError(_server_error(resp))
    if opts.get("json"):
        _dump(apps)
        return
    if not apps:
        print("no apps yet; create one with: tryaii apps create <name>")
        return
    rows = []
    for app in apps:
        app = app if isinstance(app, dict) else {}
        rows.append((_text(app.get("app_id")), _text(app.get("name")),
                     _int(app.get("keys_active")), _int(app.get("calls_24h")),
                     _money(app.get("cost_24h_usd")), _text(app.get("last_event_at"))))
    print("\n".join(_table(APP_COLUMNS, rows, _APP_RIGHT)))


KEY_COLUMNS = ("key", "label", "created", "last used", "status")


def _key_prefix(key: dict) -> str:
    prefix = key.get("prefix")
    if not isinstance(prefix, str) or not prefix:
        prefix = f"tryaii_{key.get('key_id', '')}_"
    return prefix + "..."


def _verb_keys(argv: list) -> None:
    positionals, opts = _parse(argv, "keys")
    if len(positionals) != 1:
        raise AppsUsageError(
            "usage: tryaii apps keys <app_id> [--new [--label <label>] | --revoke <key_id>]. "
            + HELP_HINT)
    app_id = positionals[0].strip()
    if not app_id:
        raise AppsUsageError("app id must not be empty")
    if opts.get("new") and "revoke" in opts:
        raise AppsUsageError("--new and --revoke cannot be combined")
    if "label" in opts and not opts.get("new"):
        raise AppsUsageError("--label needs --new")
    if opts.get("new"):
        body = {"label": opts["label"]} if opts.get("label") else {}
        resp = _user_call("POST", f"/v1/apps/{app_id}/keys", body)
        if resp.status == 409 and resp.error == "key_limit":
            raise AppsRuntimeError(
                "key_limit: an app has at most 2 active keys; revoke one with "
                f"tryaii apps keys {app_id} --revoke <key_id>")
        if resp.status != 201 or not isinstance(resp.body, dict) \
                or not isinstance(resp.body.get("key"), str):
            raise AppsRuntimeError(_server_error(resp))
        if opts.get("json"):
            _dump(resp.body)
            print(KEY_WARNING, file=sys.stderr)
            return
        print(f"new key {resp.body.get('key_id')} for app {app_id}")
        _print_new_key(resp.body["key"])
        return
    if "revoke" in opts:
        key_id = opts["revoke"].strip()
        if not key_id:
            raise AppsUsageError(f"--revoke needs a value. {HELP_HINT}")
        resp = _user_call("DELETE", f"/v1/apps/{app_id}/keys/{key_id}")
        if resp.status != 204:
            raise AppsRuntimeError(_server_error(resp))
        if opts.get("json"):
            _dump({"app_id": app_id, "key_id": key_id, "revoked": True})
            return
        print(f"revoked key {key_id} (app {app_id})")
        return
    resp = _user_call("GET", f"/v1/apps/{app_id}/keys")
    keys = resp.body.get("keys") if isinstance(resp.body, dict) else None
    if resp.status != 200 or not isinstance(keys, list):
        raise AppsRuntimeError(_server_error(resp))
    if opts.get("json"):
        _dump(keys)
        return
    if not keys:
        print(f"no keys for app {app_id}")
        return
    rows = []
    for key in keys:
        key = key if isinstance(key, dict) else {}
        status = "revoked" if key.get("revoked_at") else "active"
        rows.append((_key_prefix(key), _text(key.get("label")), _text(key.get("created_at")),
                     _text(key.get("last_used_at")), status))
    print("\n".join(_table(KEY_COLUMNS, rows, set())))


def _verb_check(argv: list) -> None:
    from tryaii.monitor import api

    positionals, opts = _parse(argv, "check")
    if positionals:
        raise AppsUsageError(f"unexpected argument: {positionals[0]}. {HELP_HINT}")
    key: Optional[str] = opts.get("key")
    key = key.strip() if isinstance(key, str) else api.api_key()
    if not key:
        raise AppsUsageError("no app key: set TRYAII_API_KEY or pass --key")
    if not api.is_app_key(key):
        raise AppsUsageError("not an app key (expected tryaii_<key_id>_<secret>)")
    base = api.api_url()
    resp = _call("GET", base, "/v1/apps/keys/validate", key)
    if resp.status == 401:
        raise AppsRuntimeError("invalid key")
    if resp.status != 200 or not isinstance(resp.body, dict):
        raise AppsRuntimeError(_server_error(resp))
    info = resp.body
    if opts.get("json"):
        _dump(info)
        return
    scopes = info.get("scopes") if isinstance(info.get("scopes"), list) else []
    print(f"app: {_text(info.get('app_name'))} ({_text(info.get('app_id'))})")
    print(f"key: {_text(info.get('key_id'))}")
    print(f"scopes: {', '.join(str(s) for s in scopes) or '-'}")
    expires = info.get("expires_at")
    print(f"expires: {expires if isinstance(expires, str) and expires else 'never'}")


_VERB_HANDLERS = {
    "create": _verb_create,
    "list": _verb_list,
    "keys": _verb_keys,
    "check": _verb_check,
}


def cmd_apps(argv: list) -> None:
    """Verb dispatcher for `tryaii apps` (mirrors cmdApps in cli.ts)."""
    verb = argv[0] if argv else None
    if verb is None:
        print(f"error: missing apps verb. {HELP_HINT}", file=sys.stderr)
        sys.exit(2)
    handler = _VERB_HANDLERS.get(verb)
    if handler is None:
        print(f"error: unknown apps verb: {verb}. {HELP_HINT}", file=sys.stderr)
        sys.exit(2)
    try:
        handler(argv[1:])
    except AppsUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except AppsRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- same one-line contract as main()
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
