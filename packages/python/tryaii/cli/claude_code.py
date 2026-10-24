"""`tryaii claude-code` -- your own Claude Code usage (shared/monitor/SPEC.md §10).

Python only for now: a documented exception to CLI parity (SPEC.md §10.6), so
the command is not in the shared HELP / COMMAND_HELP tables and has its own
help text here.

Verbs:
  report      totals, models, main vs subagents, cache, cost split, projects, by day,
              where a cheaper model may fit (read-only)
  connect     import the calls as monitor events of the app ``claude-code`` and
              upload them with the monitor's uploader (TRYAII_API_KEY), and add
              live telemetry to Claude Code's user settings (§10.8); two consents
  status      what is connected, the endpoint, the last import, and loud warnings
              when a Claude Code content switch is on
  otel-headers  the headers helper Claude Code runs: prints the auth header JSON
  analyze     opt-in: classify your Claude Code tasks on this machine and say
              which model is good enough for each kind (SPEC.md §10.7)
  sync        send the project, subagent type and (with analyze) task profile of
              calls that came by live telemetry, the task profile of imported
              calls uploaded before analyze, and the per-process advice
              (SPEC.md §10.9); the plugin's SessionEnd hook runs it
  disconnect  remove the local state ``connect`` and ``analyze`` created
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

VERBS = ("report", "connect", "status", "analyze", "sync", "disconnect", "otel-headers")
HELP_HINT = 'Run "tryaii claude-code --help".'
DEFAULT_DAYS = 30
UPLOAD_BUDGET_S = 600.0

HELP_CLAUDE_CODE = """tryaii claude-code -- Your own Claude Code usage, reported and sent to TryAii

Usage:
  tryaii claude-code report [--days N] [--json] [--projects-dir PATH]
  tryaii claude-code connect [--dry-run] [--days N] [--yes] [--no-live | --live-only]
                            [--no-session] [--projects-dir PATH] [--claude-dir PATH]
  tryaii claude-code status [--json] [--claude-dir PATH]
  tryaii claude-code analyze [--days N] [--json] [--yes] [--projects-dir PATH]
  tryaii claude-code sync [--background] [--quiet] [--days N] [--projects-dir PATH]
  tryaii claude-code disconnect [--yes]
  tryaii claude-code otel-headers

Reads the session logs Claude Code keeps on this machine
(~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects): only model ids, token
counts, times, the subagent type and the project folder's base name. Message
text, tool inputs and outputs and file contents are never read into a report,
stored or uploaded. The one exception is analyze, which you opt into.

Verbs:
  report        Calls, tokens and cost per model, main vs subagents, cache hit share,
                cost split (input, cache write 5m/1h, cache read, output), top projects,
                calls and cost per UTC day, where a cheaper model may fit, and cold
                cache restarts (a main-thread call after a pause of more than 5
                minutes that wrote the whole context to the cache again; sessions
                are named "Session N" only)
  connect       Two parts, each with its own yes/no:
                1. backfill: store each logged call as a monitor event of the app
                   "claude-code" and upload them (app key in TRYAII_API_KEY); a re-run
                   only adds new calls
                2. live: add OpenTelemetry settings to Claude Code's user settings
                   (~/.claude/settings.json, backup first) so every new API call is
                   reported to TryAii: model, tokens, cost, duration, time, main or
                   subagent type, effort level and a session reference (a keyed hash
                   of the session id, made on TryAii's server, removed after 7 days;
                   --no-session leaves it out). The request id only makes a
                   de-duplication id; it is not stored. Prompts, code, file
                   contents and tool inputs are never sent
                Each imported main-thread call says whether it was a cold cache restart
                (cache_cold); the import sends no session id or reference
  status        Backfill and live telemetry state, the endpoint, the last import and
                whether Claude Code sends its session id;
                warns when Claude Code is set to export prompts, tool details or raw
                API bodies
  analyze       Opt-in: read the text of your own messages and of each subagent's
                instruction ON THIS MACHINE, classify each task with TryAii's local
                embedding model and say which Anthropic model is good enough for each
                kind of task, per process (main, each subagent type). Asks first; the
                text is never printed, stored or uploaded. report shows the result and
                connect sends only the task profile and the recommendation
  sync          For calls that came by live telemetry: send, per call, the project
                folder's name, the subagent type and, if you ran analyze, the task
                type and recommended model; for imported calls uploaded before
                analyze (last 30 days): the task type and recommended model; plus
                the per-process advice of analyze. Never text. Needs the backfill
                connected (and live telemetry, or TRYAII_API_KEY); at most once
                every 5 minutes. The TryAii plugin runs it when a Claude Code
                session ends (--background --quiet)
  disconnect    Remove what connect and analyze stored on this machine, and exactly
                the settings keys connect added to Claude Code's user settings
  otel-headers  Print {"Authorization": "Bearer <app key>", "X-Tryaii-Notice-Version":
                "<version>"} for Claude Code's otelHeadersHelper; exits 1 with
                nothing on stdout when not connected

Options:
  --days N              Window in days (default 30; connect: at most 90; sync: default 7
                        for live calls, never before live telemetry was turned on;
                        imported calls: at least 30)
  --json                report/analyze: machine-readable output
  --projects-dir PATH   Claude Code's projects directory (default ~/.claude/projects)
  --claude-dir PATH     Claude Code's config directory (default ~/.claude, or
                        $CLAUDE_CONFIG_DIR); its settings.json is the file connect changes
  --dry-run             connect: show what would be stored, uploaded and added to the
                        settings file; write nothing
  --no-live             connect: backfill only, no telemetry settings
  --live-only           connect: telemetry settings only, no backfill
  --no-session          connect: also add OTEL_METRICS_INCLUDE_SESSION_ID=false, so Claude
                        Code sends no session id with its telemetry; works on an
                        already connected setup (connect --live-only --no-session);
                        disconnect removes it again
  --yes                 connect/analyze/disconnect: yes to every question
  --background          sync: start in the background and return at once
  --quiet               sync: print nothing (also when there is nothing to do)

Costs are an estimate at API list prices from the TryAii model catalog
(cache write 1.25x input for 5 minutes, 2x for 1 hour; cache read at the
catalog's cache price, else Anthropic's per-model share of the input price).
Claude models the catalog lacks use Anthropic's list price, marked "list price
(fallback)"; any other model the catalog does not price is reported as
unpriced.

Examples:
  tryaii claude-code report --days 7
  tryaii claude-code connect --dry-run
  tryaii claude-code connect --live-only --no-session
  tryaii claude-code status
  tryaii claude-code analyze --days 30
  tryaii claude-code sync
  TRYAII_API_KEY=tryaii_... tryaii claude-code connect --yes

Exit codes:
  0 success, 1 runtime failure, 2 usage error or declined confirmation.

Docs: docs/cli/claude-code.md
"""

_VALUE_FLAGS = {"--days": "days", "--projects-dir": "projects_dir", "--now": "now",
                "--claude-dir": "claude_dir"}
_SWITCH_FLAGS = {"--json": "json", "--dry-run": "dry_run", "--yes": "yes", "-y": "yes",
                 "--no-live": "no_live", "--live-only": "live_only",
                 "--no-session": "no_session",
                 "--background": "background", "--quiet": "quiet", "-q": "quiet"}
_VERB_OPTIONS = {
    "report": {"days", "json", "projects_dir", "now"},
    "connect": {"days", "dry_run", "yes", "projects_dir", "now", "claude_dir", "no_live",
                "live_only", "no_session"},
    "status": {"json", "claude_dir"},
    "analyze": {"days", "json", "yes", "projects_dir", "now"},
    "sync": {"days", "background", "quiet", "projects_dir", "now"},
    "disconnect": {"yes"},
    "otel-headers": set(),
}


class UsageError(Exception):
    """Message to stderr, exit 2."""


def _parse(argv: list, verb: str) -> dict:
    opts: dict = {}
    allowed = _VERB_OPTIONS[verb]
    i = 0
    while i < len(argv):
        arg = argv[i]
        name, eq, inline = arg.partition("=")
        if name in _VALUE_FLAGS and _VALUE_FLAGS[name] in allowed:
            if eq:
                value = inline
            else:
                if i + 1 >= len(argv):
                    raise UsageError(f"{name} needs a value. {HELP_HINT}")
                i += 1
                value = argv[i]
            opts[_VALUE_FLAGS[name]] = value
        elif arg in _SWITCH_FLAGS and _SWITCH_FLAGS[arg] in allowed:
            opts[_SWITCH_FLAGS[arg]] = True
        elif arg.startswith("-"):
            raise UsageError(f"unknown option: {arg}. {HELP_HINT}")
        else:
            raise UsageError(f"unexpected argument: {arg}. {HELP_HINT}")
        i += 1
    return opts


def _days(value: Optional[str], maximum: Optional[int] = None) -> int:
    if value is None:
        return DEFAULT_DAYS
    try:
        days = int(value.strip(), 10)
    except ValueError:
        days = 0
    if days <= 0:
        raise UsageError("--days must be a positive integer")
    if maximum is not None and days > maximum:
        raise UsageError(f"--days must be at most {maximum} (the server keeps {maximum} days)")
    return days


def _now(value: Optional[str]) -> datetime:
    from tryaii.monitor.events import parse_ts

    if value is None:
        return datetime.now(timezone.utc)
    try:
        return parse_ts(value)
    except ValueError:
        raise UsageError("--now must be an ISO-8601 UTC timestamp") from None


def _projects_dir(opts: dict) -> Path:
    from tryaii.claude_code.reader import default_projects_dir

    if opts.get("projects_dir"):
        return Path(opts["projects_dir"]).expanduser()
    return default_projects_dir()


def _display(path) -> str:
    """A path for output with the home directory shown as ``~`` (the OS user
    name stays out of reports)."""
    text = str(path)
    home = str(Path.home())
    if text.lower().startswith(home.lower()):
        text = "~" + text[len(home):]
    return text.replace("\\", "/")


def _data_dir() -> Path:
    from tryaii.config import TryaiiDreConfig

    return Path(TryaiiDreConfig().data_dir)


def _pricer():
    from tryaii.claude_code.pricing import Pricer
    from tryaii.cli.main import _cli_catalog

    return Pricer(_cli_catalog())


def _read(opts: dict, days: int):
    from tryaii.claude_code.reader import read_calls

    root = _projects_dir(opts)
    if not root.is_dir():
        raise RuntimeError(f"no Claude Code logs at {_display(root)} (use --projects-dir)")
    now = _now(opts.get("now"))
    since = now - timedelta(days=days)
    return root, now, since, read_calls(root, since=since, until=now)


def _confirm(question: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        raise UsageError("confirmation needed: run in a terminal or pass --yes")
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


# --- verbs ------------------------------------------------------------------------

def _verb_report(argv: list) -> None:
    from tryaii.claude_code.report import build_report, render_report

    opts = _parse(argv, "report")
    days = _days(opts.get("days"))
    root, now, since, result = _read(opts, days)
    doc = build_report(result.calls, _pricer(), days=days, now=now, since=since,
                       files=result.files, projects_dir=_display(root))
    stored = _stored_analysis()
    if stored is not None:
        doc["analysis"] = stored
    if opts.get("json"):
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return
    print(render_report(doc), end="")


def _stored_analysis() -> Optional[dict]:
    """The last ``analyze`` result without its per-call map, or None."""
    from tryaii.claude_code import analysis

    doc = analysis.read_analysis(_data_dir())
    return analysis.summary(doc) if doc else None


def _key_hint(key: Optional[str]) -> str:
    from tryaii.monitor import api

    if not key:
        return "-"
    match = api.APP_KEY_RE.match(key)
    return f"tryaii_{match.group(1)}_..." if match else "not an app key"


def _upload_plan(data_dir: Path) -> tuple[str, Optional[str]]:
    """``(state, detail)``: ``ready`` / ``no_key`` / ``bad_key`` / ``off``."""
    from tryaii.monitor import api, privacy, store

    key = api.api_key()
    enabled, reason = privacy.upload_decision(None, store.monitor_root(data_dir))
    if not key:
        return "no_key", None
    if not api.is_app_key(key):
        return "bad_key", None
    if not enabled:
        return "off", reason
    return "ready", _key_hint(key)


def _redacted(event: dict) -> dict:
    example = json.loads(json.dumps(event))
    example["event_id"] = "<uuid5 of the call's message id>"
    if isinstance(example.get("call_site"), dict):
        example["call_site"]["file"] = "<project folder base name>"
    return example


def _pending(directory: Path) -> int:
    from tryaii.monitor import store

    total = sum(len(store.read_lines(p)) for p in store.event_files(directory))
    cursor = store.read_upload_cursor(directory)
    return max(total - store.uploaded_event_count(directory, cursor), 0)


def _run_upload(directory: Path) -> str:
    """Send the app directory's unread events with the monitor uploader, one
    batch per 5 s (SPEC.md §3.4); returns a one-line outcome."""
    from tryaii.monitor import api, store
    from tryaii.monitor.upload import SOURCE_CLAUDE_CODE_IMPORT, Uploader

    uploader = Uploader(api.api_key(), api.api_url())
    uploader.register(directory, source=SOURCE_CLAUDE_CODE_IMPORT)
    deadline = time.monotonic() + UPLOAD_BUDGET_S
    while True:
        outcome = uploader.run_once()
        if outcome in ("sent", "skipped"):
            print(f"uploaded {uploader.sent_events} events...", file=sys.stderr, flush=True)
            continue
        if outcome == "idle":
            cursor = store.read_upload_cursor(directory) or {}
            error = cursor.get("last_error")
            if error and _pending(directory):
                return f"upload paused ({error}); {_pending(directory)} events wait locally"
            return f"uploaded {uploader.sent_events} events"
        if outcome in ("disabled", "rejected"):
            return "upload failed: TRYAII_API_KEY was rejected (invalid_key)"
        if outcome is None:
            return "upload failed (internal error); events wait locally"
        wait = max(uploader.next_at - time.monotonic(), 0.05)
        if time.monotonic() + wait > deadline:
            cursor = store.read_upload_cursor(directory) or {}
            return (f"uploaded {uploader.sent_events} events; stopped "
                    f"({cursor.get('last_error') or outcome}); the rest uploads on the next "
                    "connect")
        time.sleep(wait)


def _verb_connect(argv: list) -> None:
    opts = _parse(argv, "connect")
    if opts.get("no_live") and opts.get("live_only"):
        raise UsageError("--no-live and --live-only cannot be combined")
    if opts.get("no_live") and opts.get("no_session"):
        raise UsageError("--no-session changes the live telemetry settings; it cannot be "
                         "combined with --no-live")
    data_dir = _data_dir()
    live = None if opts.get("no_live") else _live_plan(opts, data_dir)
    backfill = None if opts.get("live_only") else _backfill_plan(
        opts, data_dir, starting_live=live is not None and live["state"] == "ready")
    if live is not None and live["state"] in _LIVE_SKIPPED and opts.get("live_only") \
            and not opts.get("dry_run"):
        raise RuntimeError(live["line"].replace("live telemetry: ", "", 1))
    dry = bool(opts.get("dry_run"))

    if dry:
        print("dry run: nothing is written or uploaded")
    if backfill is not None:
        _print_backfill(backfill, dry)
    if live is not None:
        if backfill is not None:
            print()
        _print_live(live, dry)
    if live is not None and live["state"] == "conflict":
        print("error: live telemetry stopped: TryAii never overwrites another tool's "
              "telemetry. Change those settings yourself, or connect without live telemetry "
              "(--no-live).", file=sys.stderr)
        sys.exit(1)
    if dry:
        return

    asked = declined = 0
    if backfill is not None:
        asked += 1
        declined += not _run_backfill(backfill, opts, data_dir)
    if live is not None and live["state"] == "ready":
        asked += 1
        declined += not _run_live(live, opts, data_dir)
    if asked and declined == asked:
        sys.exit(2)
    from tryaii.claude_code import analysis

    if analysis.read_consent(data_dir) is None:
        print(ANALYZE_HINT)


#: The last line of ``connect`` while ``analyze`` has no consent: without a task
#: profile the dashboard's model advice and savings stay empty.
ANALYZE_HINT = ("next: run 'tryaii claude-code analyze' (task types and model advice, on this "
                "machine), then 'tryaii claude-code sync' to add them to the uploaded calls")


# --- connect: backfill (log import) -----------------------------------------------------

def _backfill_plan(opts: dict, data_dir: Path, starting_live: bool = False) -> dict:
    """``starting_live``: this run may turn live telemetry on (its cutoff is
    then the moment it does, after this read)."""
    from tryaii.claude_code import analysis, importer, telemetry
    from tryaii.claude_code.reader import MAIN_PROCESS
    from tryaii.monitor import api
    from tryaii.monitor.events import format_ts, parse_ts

    days = _days(opts.get("days"), importer.MAX_DAYS)
    root, now, since, result = _read(opts, days)
    directory = importer.app_directory(data_dir)
    pricer = _pricer()
    known = importer.existing_event_ids(data_dir)
    analysed = analysis.read_analysis(data_dir) or {}
    results = analysis.call_results(data_dir)
    cutoff = telemetry.live_since(telemetry.read_state(data_dir))
    cutoff_dt = parse_ts(cutoff) if cutoff else None
    calls = [c for c in result.calls if cutoff_dt is None or c.ts < cutoff_dt]
    after_cutoff = len(result.calls) - len(calls)
    events, new = [], []
    for call in calls:
        event = importer.build_call_event(call, pricer, results.get(call.key),
                                          analysed.get("taxonomy_version"))
        events.append(event)
        # A call stored before the request-id rule keeps its old id: skip it too.
        if event["event_id"] not in known and importer.legacy_event_id(call) not in known:
            new.append(event)
    plan, detail = _upload_plan(data_dir)
    days_touched = sorted({e["ts"][:10] for e in new})
    unpriced = sum(1 for e in new if e["est_cost_usd"] is None)
    subagent = sum(1 for e in new if e["process"] != MAIN_PROCESS)

    lines = [
        f"found {len(events):,} Claude Code calls in the last {days} days "
        f"({format_ts(since)[:10]} .. {format_ts(now)[:10]}) in {_display(root)}",
        f"already stored: {len(events) - len(new):,}; new: {len(new):,} "
        f"({subagent:,} from subagents, {unpriced:,} unpriced)",
    ]
    if cutoff:
        lines.append(f"live telemetry since {cutoff}: the import covers calls before it; "
                     f"{after_cutoff:,} later calls come by live telemetry and are skipped")
    elif starting_live:
        lines.append("cutoff: when live telemetry is turned on below, calls from that moment "
                     "on come by live telemetry; this import covers the calls before it")
    profiled = sum(1 for e in new if e["classification"] is not None)
    if results:
        lines.append(f"task profiles from analyze: {profiled:,} of the new events (category, "
                     "subcategory, confidence and the Anthropic recommendation; never text)")
    if new:
        lines.append(f"events: {new[0]['ts']} .. {new[-1]['ts']} into {len(days_touched)} daily "
                     f"files of {_display(directory)}")
    upload_line = {
        "ready": f"upload: to {api.api_url()} with TRYAII_API_KEY {detail}",
        "no_key": "upload: no TRYAII_API_KEY set -- stored on this machine only (create an app "
                  "key with 'tryaii login' and 'tryaii apps create claude-code')",
        "bad_key": "upload: TRYAII_API_KEY is not an app key -- stored on this machine only",
        "off": f"upload: off ({detail}) -- stored on this machine only",
    }[plan]
    return {"new": new, "lines": lines, "upload_line": upload_line, "plan": plan,
            "directory": directory}


def _print_backfill(backfill: dict, dry: bool) -> None:
    print("backfill (import the calls in Claude Code's logs):")
    print("\n".join(backfill["lines"]))
    if not dry:
        return
    new = backfill["new"]
    print(backfill["upload_line"].replace("upload:", "would upload:", 1))
    if new:
        print("fields per event: " + ", ".join(new[0].keys()))
        print("never included: message text, tool inputs or outputs, file contents, "
              "full paths, session ids (cache_cold is worked out on this machine)")
        print("example (redacted):")
        print(json.dumps(_redacted(new[0]), indent=2, ensure_ascii=False))


def _run_backfill(backfill: dict, opts: dict, data_dir: Path) -> bool:
    """False when the user said no."""
    from tryaii.claude_code import importer
    from tryaii.monitor.events import format_ts

    new, plan, directory = backfill["new"], backfill["plan"], backfill["directory"]
    pending = _pending(directory) if directory.is_dir() else 0
    if not new and not (plan == "ready" and pending):
        print("nothing new to import")
        return True
    print(backfill["upload_line"])
    if not opts.get("yes"):
        verb = "store and upload" if plan == "ready" else "store"
        what = f"{len(new):,} new events" if new else f"{pending:,} stored events"
        if not _confirm(f"{verb.capitalize()} {what} for app {importer.APP}?"):
            print("backfill cancelled; no events were written", file=sys.stderr)
            return False
    stored = importer.store_events(data_dir, new)
    importer.write_state(data_dir, now=format_ts(datetime.now(timezone.utc)), imported=stored)
    print(f"stored {stored:,} events in {_display(directory)}")
    if plan == "ready":
        print(_run_upload(directory))
    print(f"see them with: tryaii monitor status --app {importer.APP} --since 30d")
    return True


# --- connect: live telemetry (SPEC.md §10.8) ----------------------------------------------

#: What the server keeps per Claude Code API call, and what is never sent.
RECEIVED = ("model, input and output tokens, cache read and cache write tokens, cost "
            "estimate, duration, time, main conversation or subagent type, effort level "
            "and a session reference (a keyed hash of the session id, for the cache "
            "timer, removed after 7 days); the request id is used only to make a "
            "de-duplication id and is not stored")
NEVER_SENT = "prompts, code, file contents, tool inputs and outputs"
_LIVE_SKIPPED = ("no_key", "bad_key", "off")


def _claude_dir(opts: dict) -> Path:
    from tryaii.claude_code.telemetry import default_claude_dir

    if opts.get("claude_dir"):
        return Path(opts["claude_dir"]).expanduser()
    return default_claude_dir()


def _live_plan(opts: dict, data_dir: Path) -> dict:
    """``state``: ``ready`` (something to write), ``connected``, ``conflict``
    or one of ``_LIVE_SKIPPED``."""
    from tryaii.claude_code import telemetry
    from tryaii.monitor import api, privacy, store

    endpoint = telemetry.logs_endpoint(api.api_url())
    path = telemetry.settings_path(_claude_dir(opts))
    live = {"endpoint": endpoint, "path": path, "plan": None, "settings": None, "key": None,
            "conflicts": [], "line": "", "no_session": bool(opts.get("no_session"))}
    key = api.api_key()
    enabled, reason = privacy.upload_decision(None, store.monitor_root(data_dir))
    try:
        settings = telemetry.read_settings(path)
    except telemetry.SettingsError as exc:
        return {**live, "state": "conflict", "conflicts": [str(exc)]}
    plan = telemetry.plan_connect(settings, endpoint, telemetry.read_state(data_dir),
                                  no_session=bool(opts.get("no_session")))
    # Shown by --dry-run even when the live part cannot run yet.
    live.update(plan=plan, settings=settings, key=key, conflicts=plan["conflicts"])
    if not key:
        return {**live, "state": "no_key",
                "line": "live telemetry: needs an app key in TRYAII_API_KEY (create one with "
                        "'tryaii login' and 'tryaii apps create claude-code')"}
    if not api.is_app_key(key):
        return {**live, "state": "bad_key",
                "line": "live telemetry: TRYAII_API_KEY is not an app key"}
    if not enabled:
        return {**live, "state": "off",
                "line": f"live telemetry: off because upload is off ({reason})"}
    if plan["conflicts"]:
        state = "conflict"
    elif plan["add_env"] or plan["add_top"] or telemetry.read_key(data_dir) != key:
        state = "ready"
    else:
        state = "connected"
    return {**live, "state": state}


def _print_live(live: dict, dry: bool) -> None:
    from tryaii.claude_code import telemetry

    print("live telemetry (Claude Code reports each new API call to TryAii; asked "
          "separately):")
    if live["state"] in _LIVE_SKIPPED:
        print(live["line"])
        if not dry or live["plan"] is None:
            return
        print("once that is fixed, connect would do this:")
    if live["conflicts"]:
        print(f"cannot add live telemetry to {_display(live['path'])}:")
        for line in live["conflicts"]:
            print(f"  - {line}")
        return
    plan = live["plan"]
    print(f"endpoint: {live['endpoint']} (OTLP/HTTP JSON)")
    if live["state"] == "connected":
        print(f"already connected: {_display(live['path'])} has every setting")
        return
    block = telemetry.block(plan)
    if block:
        print(f"{'would add' if dry else 'adds'} to {_display(live['path'])} (every other "
              "setting stays as it is):")
        print(json.dumps(block, indent=2))
        if live["settings"].text is not None:
            print(f"backup first: {_display(live['path'])}{telemetry.BACKUP_INFIX}<UTC time>")
    if plan["present"]:
        print("already set, left as is: " + ", ".join(plan["present"]))
    hint = _key_hint(live["key"]) if live["key"] else "<app key>"
    print(f"the helper prints {{\"Authorization\": \"Bearer {hint}\", "
          f"\"{telemetry.NOTICE_HEADER}\": \"<notice version>\"}} with the app key stored in "
          f"{_display(telemetry.key_path(_data_dir()))}; the key is never written to Claude "
          "Code's settings")
    print("cutoff: from the moment this is written, calls come by live telemetry and the log "
          "import skips them (no call is counted twice)")
    print(f"TryAii keeps per API call: {RECEIVED}")
    print(f"never sent: {NEVER_SENT} (Claude Code's content switches stay off)")
    if live.get("no_session"):
        print(f"--no-session: {telemetry.SESSION_ID_VAR}={telemetry.SESSION_ID_OFF}, so Claude "
              "Code sends no session id with its telemetry")
    print("Claude Code also sends its standard attributes (such as your account email and "
          "session id) and other event types; TryAii's server drops them before storage, "
          "keeping only a keyed hash of the session id"
          + (" (none with --no-session)" if live.get("no_session") else
             " (leave it out: --no-session)"))
    print("applies to Claude Code sessions started afterwards; undo: tryaii claude-code "
          "disconnect")


def _run_live(live: dict, opts: dict, data_dir: Path) -> bool:
    """False when the user said no."""
    from tryaii.claude_code import telemetry
    from tryaii.monitor.events import format_ts

    plan, settings = live["plan"], live["settings"]
    if not opts.get("yes"):
        if not _confirm(f"Add live telemetry to {_display(live['path'])}?"):
            print("live telemetry cancelled; Claude Code's settings were not changed",
                  file=sys.stderr)
            return False
    now = format_ts(datetime.now(timezone.utc))
    telemetry.write_key(data_dir, live["key"])
    backup, created_env = None, False
    if plan["add_env"] or plan["add_top"]:
        created_env = bool(plan["add_env"]) and not isinstance(settings.doc.get("env"), dict)
        backup = telemetry.write_settings(settings, telemetry.merged(settings.doc, plan), now)
    telemetry.write_state(data_dir, settings_file=live["path"], plan=plan,
                          created_env=created_env, now=now, backup=backup)
    added = len(plan["add_env"]) + len(plan["add_top"])
    if added:
        print(f"live telemetry: added {added} settings to {_display(live['path'])}"
              + (f" (backup: {_display(backup)})" if backup else ""))
    else:
        print("live telemetry: stored the new app key")
    print("Claude Code sessions started from now on report each API call to TryAii; check "
          "with: tryaii claude-code status")
    print(SYNC_HINT)
    return True


# --- status and the headers helper -------------------------------------------------------

_CONTENT_WHAT = {
    "OTEL_LOG_USER_PROMPTS": "the text of your prompts",
    "OTEL_LOG_TOOL_DETAILS": "tool inputs such as Bash commands and file paths",
    "OTEL_LOG_RAW_API_BODIES": "the full API requests and responses (whole conversations)",
    "OTEL_LOG_ASSISTANT_RESPONSES": "Claude's response text",
    "OTEL_LOG_TOOL_CONTENT": "tool output such as file contents",
}


def _verb_status(argv: list) -> None:
    from tryaii.claude_code import importer, telemetry
    from tryaii.monitor import api

    opts = _parse(argv, "status")
    data_dir = _data_dir()
    backfill = importer.read_state(data_dir) or {}
    state = telemetry.read_state(data_dir)
    if state and state.get("settings_path"):
        path = Path(state["settings_path"])
    else:
        path = telemetry.settings_path(_claude_dir(opts))
    settings, settings_error = None, None
    try:
        settings = telemetry.read_settings(path)
    except telemetry.SettingsError as exc:
        settings_error = str(exc)
    changed = []
    if state:
        env = settings.env if settings is not None else {}
        top = settings.doc if settings is not None else {}
        changed = [k for k, v in (state.get("env") or {}).items() if env.get(k) != v]
        changed += [k for k, v in (state.get("top") or {}).items() if top.get(k) != v]
    key = telemetry.read_key(data_dir)
    current = settings.env.get(telemetry.ENDPOINT_VAR) if settings is not None else None
    warnings = [{"variable": var, "where": where}
                for var, where in telemetry.content_warnings(settings)]
    session_id = telemetry.session_id_setting(settings)
    doc = {
        "schema": "tryaii.claude-code.status/1",
        "backfill": {"connected": bool(backfill),
                     "connected_at": backfill.get("connected_at"),
                     "last_import_at": backfill.get("last_import_at"),
                     "imported_events": backfill.get("imported_events")},
        "live": {"connected": bool(state) and not changed and api.is_app_key(key)
                 and settings_error is None,
                 "connected_at": (state or {}).get("connected_at"),
                 "live_since": telemetry.live_since(state),
                 "endpoint": (state or {}).get("endpoint") or current,
                 "settings_file": _display(path), "settings_error": settings_error,
                 "changed_since_connect": changed,
                 "app_key": _key_hint(key) if key else None,
                 "session_id_sent": session_id["sent"],
                 "session_id_setting": ({"variable": telemetry.SESSION_ID_VAR,
                                         "value": session_id["value"],
                                         "where": session_id["where"]}
                                        if session_id["where"] else None),
                 "no_session_by_connect": telemetry.SESSION_ID_VAR in ((state or {}).get("env")
                                                                       or {})},
        "warnings": warnings,
    }
    if opts.get("json"):
        print(json.dumps(doc, indent=2))
        for w in warnings:
            print(f"WARNING: {w['variable']} is on ({w['where']})", file=sys.stderr)
        return
    b, live = doc["backfill"], doc["live"]
    if b["connected"]:
        print(f"backfill: connected {b['connected_at']}; last import {b['last_import_at']} "
              f"({b['imported_events'] or 0:,} events imported)")
    else:
        print("backfill: not connected (tryaii claude-code connect --no-live)")
    if state:
        print(f"live telemetry: {'connected' if live['connected'] else 'NOT WORKING'} "
              f"(since {live['connected_at']})")
        print(f"  endpoint: {live['endpoint']}")
        print(f"  cutoff: calls from {live['live_since']} on come by live telemetry; the log "
              "import covers calls before it")
        print(f"  settings: {live['settings_file']}")
        print(f"  headers helper key: {live['app_key'] or 'missing'}")
        print("  " + _session_line(session_id))
        if changed:
            print("  changed or removed since connect: " + ", ".join(changed)
                  + " (run connect again)")
        if settings_error:
            print(f"  {settings_error}")
    else:
        print("live telemetry: not connected (tryaii claude-code connect --live-only)")
        if current:
            print(f"  {live['settings_file']} sends Claude Code telemetry to {current}")
    print(f"TryAii API: {api.api_url()}")
    for w in warnings:
        _warn_content(w["variable"], w["where"])


def _session_line(session_id: dict) -> str:
    from tryaii.claude_code.telemetry import SESSION_ID_VAR

    if session_id["sent"]:
        where = (f" ({SESSION_ID_VAR}={session_id['value']} in {session_id['where']})"
                 if session_id["where"] else "")
        return (f"sessions: on{where}: Claude Code sends its session id; turn it off with: "
                "tryaii claude-code connect --live-only --no-session")
    return (f"sessions: off ({SESSION_ID_VAR}={session_id['value']} in {session_id['where']}): "
            "Claude Code sends no session id")


def _warn_content(var: str, where: str) -> None:
    print("!" * 78)
    print(f"WARNING: {var} is ON (in {where}).")
    print(f"Claude Code exports {_CONTENT_WHAT.get(var, 'content')} with its telemetry, to "
          "every OTLP destination it has.")
    print("TryAii's server drops such data before storage, but it still leaves this "
          "machine.")
    print("Turn it off: set it to 0 or remove it.")
    print("!" * 78)


def _verb_otel_headers(argv: list) -> None:
    """Claude Code's ``otelHeadersHelper``: stdout carries the headers JSON and
    nothing else; when not connected, nothing on stdout and exit 1. The key is
    never logged."""
    from tryaii.claude_code import telemetry
    from tryaii.monitor import api

    _parse(argv, "otel-headers")
    data_dir = _data_dir()
    key = telemetry.read_key(data_dir) if telemetry.read_state(data_dir) else None
    if not api.is_app_key(key):
        print("error: Claude Code live telemetry is not connected (run: tryaii claude-code "
              "connect)", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(json.dumps(telemetry.headers(key)) + "\n")
    sys.stdout.flush()


ANALYZE_NOTICE = """tryaii claude-code analyze reads, on this machine only:
  - the text of your own messages to Claude Code (main conversation), without
    tool results, system reminders or other injected context;
  - the instruction each subagent was started with.
Each text is embedded with TryAii's local embedding model and classified
against the benchmark categories; texts that look like secrets are skipped.
The text is never printed, stored, hashed or uploaded. Stored locally: the task
category and model recommendation per call (in {path}). connect uploads only
those, never text, embeddings or hashes of it. Undo: tryaii claude-code disconnect
"""


def _make_route_fn(bundle, config):
    """The task router: in this process, with the local embedding model
    (test seam)."""
    from tryaii.claude_code.analysis import inprocess_route_fn

    return inprocess_route_fn(bundle, config)


def _policy_ids(pricer) -> Optional[list]:
    """The saved data-save policy's allowed ids (fails closed), or None."""
    from tryaii import policy as pol
    from tryaii.monitor.winner import policy_allowed

    try:
        saved = pol.load_saved_policy()
    except pol.PolicyError:
        return []
    return policy_allowed(saved, getattr(pricer.bundle, "origins", None),
                          pricer.registry.model_ids)


def _analyze_and_store(root: Path, calls: list, data_dir: Path, *, days: int, now: datetime,
                       since: datetime, consent: dict, progress=None) -> dict:
    """Classify the tasks of ``calls`` on this machine and store the analysis
    (analyze, and sync when the stored one is older than a day)."""
    from tryaii.claude_code import analysis
    from tryaii.claude_code.tasks import read_tasks
    from tryaii.config import TryaiiDreConfig
    from tryaii.monitor.events import taxonomy_version

    started = time.monotonic()
    pricer = _pricer()
    config = TryaiiDreConfig()
    read = read_tasks(root)
    route_fn = _make_route_fn(pricer.bundle, config)
    doc = analysis.build_analysis(
        calls, read, pricer, route_fn, days=days, now=now, since=since,
        consent=consent, embedding_model=config.embedding_model,
        taxonomy_version=taxonomy_version(pricer.bundle), policy_allowed=_policy_ids(pricer),
        progress=progress)
    doc["duration_s"] = round(time.monotonic() - started, 1)
    analysis.write_analysis(data_dir, doc)
    return doc


def _verb_analyze(argv: list) -> None:
    from tryaii.claude_code import analysis
    from tryaii.monitor.events import format_ts

    opts = _parse(argv, "analyze")
    days = _days(opts.get("days"))
    root, now, since, result = _read(opts, days)
    data_dir = _data_dir()
    info = sys.stderr if opts.get("json") else sys.stdout
    print(ANALYZE_NOTICE.format(path=_display(analysis.analysis_path(data_dir))), file=info,
          flush=True)
    consent = analysis.read_consent(data_dir)
    stamp = format_ts(datetime.now(timezone.utc))
    if opts.get("yes"):
        consent = analysis.write_consent(data_dir, now=stamp, method="--yes")
    elif consent is None:
        if not _confirm("Read your Claude Code task texts on this machine and classify them?"):
            print("cancelled; no task text was read", file=sys.stderr)
            sys.exit(2)
        consent = analysis.write_consent(data_dir, now=stamp, method="prompt")
    else:
        print(f"consent given {consent.get('granted_at')} (undo: tryaii claude-code "
              "disconnect)", file=info)

    last = [0.0]

    def progress(done: int, total: int) -> None:
        if total and (done == total or time.monotonic() - last[0] > 5):
            last[0] = time.monotonic()
            print(f"classified {done:,} of {total:,} tasks...", file=sys.stderr, flush=True)

    doc = _analyze_and_store(root, result.calls, data_dir, days=days, now=now, since=since,
                             consent=consent, progress=progress)
    path = analysis.analysis_path(data_dir)
    if opts.get("json"):
        print(json.dumps(analysis.summary(doc), indent=2, ensure_ascii=False))
        return
    print(analysis.render_analysis(analysis.summary(doc)), end="")
    print(f"took {doc['duration_s']:.1f} s; saved to {_display(path)} (no text); "
          "see it again with: tryaii claude-code report")


# --- sync: live advice (SPEC.md §10.9) ---------------------------------------------------

SYNC_HINT = ("live advice: 'tryaii claude-code sync' adds the project folder's name, the "
             "subagent type and (after analyze) the task type to these calls; the TryAii "
             "plugin runs it when a session ends, without the plugin run it now and then")


class _SyncSkipError(Exception):
    """sync has nothing to do: a short line (none with --quiet), exit 0."""


def _sync_ready(data_dir: Path) -> dict:
    """The connection sync needs, or _SyncSkipError with the reason."""
    from tryaii.claude_code import importer, telemetry
    from tryaii.monitor import api, privacy, store

    state = telemetry.read_state(data_dir)
    key = telemetry.read_key(data_dir) if state else None
    live = bool(state) and api.is_app_key(key)
    imported = importer.read_state(data_dir) is not None
    if not live:
        # Without live telemetry only the imported calls are annotated, with
        # the app key the import uploads with.
        key = api.api_key()
        if not imported:
            raise _SyncSkipError("sync: live telemetry is not connected (tryaii claude-code "
                                 "connect)")
        if not api.is_app_key(key):
            raise _SyncSkipError("sync: live telemetry is not connected and TRYAII_API_KEY "
                                 "holds no app key")
    elif not imported:
        raise _SyncSkipError("sync: the log import is not connected (tryaii claude-code connect "
                        "--no-live)")
    enabled, reason = privacy.upload_decision(None, store.monitor_root(data_dir))
    if not enabled:
        raise _SyncSkipError(f"sync: upload is off ({reason})")
    return {"state": state if live else {}, "key": key, "live": live,
            "live_since": telemetry.live_since(state) if live else None}


def _sync_base_url(state: dict) -> str:
    """``TRYAII_API_URL`` when set, else the API connect pointed Claude Code at
    (a SessionEnd hook runs without the user's shell variables)."""
    import os

    from tryaii.claude_code import telemetry
    from tryaii.monitor import api

    if (os.environ.get(api.ENV_API_URL) or "").strip():
        return api.api_url()
    endpoint = state.get("endpoint")
    if isinstance(endpoint, str) and endpoint.endswith(telemetry.ROUTE):
        return endpoint[:-len(telemetry.ROUTE)].rstrip("/")
    return api.api_url()


def _verb_sync(argv: list) -> None:
    opts = _parse(argv, "sync")
    if not opts.get("quiet"):
        _sync(opts)
        return
    # --quiet (the SessionEnd hook): nothing on stdout or stderr, exit 0.
    import contextlib
    import io

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            _sync(opts)
        except (Exception, SystemExit):  # noqa: BLE001 -- recorded in sync.json
            pass


def _sync(opts: dict) -> None:
    from tryaii.claude_code import sync
    from tryaii.monitor.events import format_ts

    days = _days(opts.get("days") or str(sync.DEFAULT_DAYS), maximum=90)
    quiet = bool(opts.get("quiet"))

    def say(line: str) -> None:
        if not quiet:
            print(line)

    data_dir = _data_dir()
    now = _now(opts.get("now"))
    try:
        ready = _sync_ready(data_dir)
        state = sync.read_state(data_dir)
        wait = sync.too_soon(state, now)
        if wait is not None:
            raise _SyncSkipError(f"sync: ran {state['last_run_at']}; next run possible in "
                            f"{int(wait) + 1} s (at most once every 5 minutes)")
        if opts.get("background"):
            args = ["--quiet"]
            for name in ("days", "projects_dir", "now"):
                if opts.get(name):
                    args += ["--" + name.replace("_", "-"), str(opts[name])]
            sync.spawn_background(args)
            say("sync: started in the background")
            return
    except _SyncSkipError as skip:
        say(str(skip))
        return

    lock = sync.Lock(data_dir)
    if not lock.acquire():
        say("sync: another sync is running")
        return
    try:
        state = sync.read_state(data_dir)
        if sync.too_soon(state, now) is not None:
            say("sync: another sync ran just now")
            return
        state["last_run_at"] = format_ts(now)
        sync.write_state(data_dir, state)
        try:
            counts, how = _sync_run(opts, data_dir, ready, state, now, days)
        except Exception as exc:  # noqa: BLE001 -- recorded, then the usual contract
            state["last_error"] = str(exc)[:200]
            sync.write_state(data_dir, state)
            if quiet:
                return
            raise
        state["last_error"] = None
        state["last_ok_at"] = format_ts(now)
        sync.write_state(data_dir, state)
    finally:
        lock.release()
    profile = {"reused": "task profiles from analyze",
               "analyzed": "task profiles from a new analyze run",
               "stale": "task profiles from an analyze run older than a day",
               "no_consent": "no task profiles (run tryaii claude-code analyze for model "
                             "advice)",
               "none": "no task profiles (analyze has no result yet)"}[how]
    imported = (f"; {counts['imported']:,} of them imported calls" if counts["imported"]
                else "")
    say(f"sync: {counts['annotations']:,} calls annotated ({counts['matched']:,} matched, "
        f"{counts['unmatched']:,} not found yet, retried for 24 h{imported}); "
        f"{counts['advice']:,} process advice documents; {profile}")


def _sync_run(opts: dict, data_dir: Path, ready: dict, state: dict, now: datetime,
              days: int) -> tuple[dict, str]:
    from tryaii import __version__
    from tryaii.claude_code import importer, sync
    from tryaii.claude_code.reader import read_calls
    from tryaii.monitor.events import parse_ts

    root = _projects_dir(opts)
    if not root.is_dir():
        raise RuntimeError(f"no Claude Code logs at {_display(root)} (use --projects-dir)")
    since = now - timedelta(days=days)
    if ready["live_since"]:
        since = max(since, parse_ts(ready["live_since"]))
    # Imported calls (before live_since) are annotated back to IMPORT_DAYS.
    import_since = now - timedelta(days=max(days, sync.IMPORT_DAYS))
    read = read_calls(root, since=import_since, until=now).calls
    calls = [c for c in read if c.ts >= since] if ready["live"] else []

    def reanalyze(consent: dict, previous: Optional[dict]) -> dict:
        window = ((previous or {}).get("window") or {}).get("days") or DEFAULT_DAYS
        start = now - timedelta(days=int(window))
        return _analyze_and_store(root, read_calls(root, since=start, until=now).calls,
                                  data_dir, days=int(window), now=now, since=start,
                                  consent=consent)

    doc, how = sync.choose_analysis(data_dir, now, reanalyze)
    counts = sync.run(data_dir, calls, pricer=_pricer(), now=now, since=import_since,
                      key=ready["key"], base_url=_sync_base_url(ready["state"]),
                      version=__version__, analysis_doc=doc, state=state,
                      imported_calls=read, uploaded=importer.uploaded_events(data_dir))
    return counts, how


def _verb_disconnect(argv: list) -> None:
    from tryaii.claude_code import importer, telemetry
    from tryaii.monitor.events import format_ts

    opts = _parse(argv, "disconnect")
    data_dir = _data_dir()
    targets = [p for p in (importer.app_directory(data_dir),
                           importer.state_path(data_dir).parent) if p.is_dir()]
    if not targets:
        print("not connected: nothing to remove")
        return
    state = telemetry.read_state(data_dir)
    settings, after, gone, kept = None, None, [], []
    if state and state.get("settings_path"):
        settings = telemetry.read_settings(Path(state["settings_path"]))
        after, gone, kept = telemetry.removed(settings.doc, state)
    if not opts.get("yes"):
        names = ", ".join(_display(p) for p in targets)
        extra = (f" and the {len(gone)} settings TryAii added to {_display(settings.path)}"
                 if gone else "")
        if not _confirm(f"Remove {names}{extra} (nothing else of Claude Code's)?"):
            print("cancelled; nothing was removed", file=sys.stderr)
            sys.exit(2)
    if gone:
        backup = telemetry.write_settings(settings, after,
                                          format_ts(datetime.now(timezone.utc)))
        print(f"removed from {_display(settings.path)}: {', '.join(gone)}"
              + (f" (backup: {_display(backup)})" if backup else ""))
    for name in kept:
        print(f"left {name} in {_display(settings.path)}: it changed since connect")
    for path in importer.disconnect(data_dir):
        print(f"removed {_display(path)}")
    print("Claude Code's logs were not touched. Events already uploaded stay on your "
          "tryaii.com account until you delete the app there.")


_HANDLERS = {"report": _verb_report, "connect": _verb_connect, "status": _verb_status,
             "analyze": _verb_analyze, "sync": _verb_sync, "disconnect": _verb_disconnect,
             "otel-headers": _verb_otel_headers}


def cmd_claude_code(argv: list) -> None:
    verb = argv[0] if argv else None
    if verb is None:
        print(f"error: missing claude-code verb. {HELP_HINT}", file=sys.stderr)
        sys.exit(2)
    handler = _HANDLERS.get(verb)
    if handler is None:
        print(f"error: unknown claude-code verb: {verb}. {HELP_HINT}", file=sys.stderr)
        sys.exit(2)
    try:
        handler(argv[1:])
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- same one-line contract as main()
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
