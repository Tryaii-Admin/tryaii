# `tryaii claude-code` (Python only)

See what your own Claude Code use costs, and send it to your TryAii dashboard as the app `claude-code`.

```
tryaii claude-code report [--days N] [--json] [--projects-dir PATH]
tryaii claude-code connect [--dry-run] [--days N] [--yes] [--no-live | --live-only] [--no-session] [--projects-dir PATH] [--claude-dir PATH]
tryaii claude-code status [--json] [--claude-dir PATH]
tryaii claude-code analyze [--days N] [--json] [--yes] [--projects-dir PATH]
tryaii claude-code sync [--background] [--quiet] [--days N] [--projects-dir PATH]
tryaii claude-code disconnect [--yes]
tryaii claude-code otel-headers
```

Only in the Python package for now (`pip install tryaii`); the Node CLI does not have it. Contract: `shared/monitor/SPEC.md` §10.

## What is read

Claude Code keeps a log per session under `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR/projects`). The command reads only the model id, the token counts, the time, whether the call came from a subagent (and the subagent type, such as `Explore`) and the base name of the project folder (`tryaii-dev`, never the full path). It never prints, stores or uploads message text, tool inputs or outputs, or file contents, and it never writes into Claude Code's folder. The one exception is `analyze`, which you opt into (below).

One API call appears on several log lines (and again in a resumed session), so calls are counted once by their message id.

## `report`

Shows the last 30 days (`--days N` to change it):

- calls and tokens: input, cache writes (5 minutes / 1 hour), cache reads, output (with thinking);
- the cache hit share: the part of all input tokens read from the prompt cache;
- the cost split: input, cache write 5m, cache write 1h, cache read, output;
- per model, main conversation vs subagents, per process (`main`, each subagent type), top projects;
- **by day**: one line per UTC date in the window (calls, cache hit share, est cost). Every date is listed, days without calls as 0; the first and last dates are partial (the window is the last N x 24 hours, not whole days). In `--json` this is the `days` array (`date`, `calls`, `unpriced_calls`, `main_calls`, `subagent_calls`, `cache_hit_share`, `est_cost_usd`);
- subagent runs: per subagent type, how many runs (one run = one subagent invocation) and the median and p90 output and cost per run;
- **where a cheaper model may fit**: subagent types whose runs are small (median output under 2,000 tokens per run and p90 under 8,000), with the most `model: haiku` in that subagent's definition could save. That figure is an upper bound: it assumes the same tokens and that Haiku does the task as well;
- **cold cache restarts**: main-conversation calls that came after a pause and had to write the whole context to the prompt cache again, with what that rewrite cost above reading the cache (at API list prices), by reason (cache expired, model switch, other), per session and the largest few. The rule: within one session's main-thread calls in time order, a call is a cold restart when more than 5 minutes passed since the previous one and it wrote at least max(1,024, its cache read) tokens; the first call of a session and subagent calls never count. A write without a 5m/1h split is priced at the lifetime the session used last (else 5 minutes). Sessions are grouped by the log file they are in and named only "Session 1", "Session 2", ... (by their first call in the window); the session id is never printed, stored or uploaded. The rule is pinned by `shared/monitor/fixtures/claude-code/cold-restarts.json`; the server applies the same rule to live calls.

Costs are an **estimate at API list prices** from the TryAii model catalog. A Claude subscription does not bill per token, so read the figure as "what this use would cost on the API". The rule: input and output at the catalog prices; cache writes at 1.25 times the input price (5 minutes) or 2 times (1 hour); cache reads at the catalog's cache price, else at Anthropic's published share of the input price (0.05 for claude-opus-5-5, 0.025 for claude-fable-5-1 and claude-mythos-5-1, 0.1 otherwise). A Claude model the catalog does not have yet (today claude-haiku-5-5; claude-opus-5 is in the catalog, starter included) uses Anthropic's list price and is marked "list price (fallback)". These two tables are temporary until the catalog carries cache prices. Any other model the catalog does not price is listed as unpriced and left out of the total. After `tryaii login` the full catalog is used, which knows more models.

`--json` prints the same data as a JSON document (`tryaii.claude-code.report/1`).

## `connect`

```
tryaii claude-code connect --dry-run            # what would be stored and sent; writes nothing
TRYAII_API_KEY=tryaii_... tryaii claude-code connect
```

Stores each call as one monitor event of the app `claude-code` in `~/.tryaii/monitor/claude-code/` and, when `TRYAII_API_KEY` holds an app key, uploads them with the monitor's own uploader (one batch every 5 seconds). Get a key with `tryaii login` and `tryaii apps create claude-code`. Without a key the events stay on this machine; run `connect` again once the key is set.

- Asks before writing; `--yes` skips the question (needed when there is no terminal).
- Re-running only adds calls that are new: each event's id is derived from the call's request id (the same id the live telemetry uses).
- `--days` is at most 90: the server keeps 90 days of events.
- `tryaii monitor upload off` (and `TRYAII_MONITOR_UPLOAD=0`) stops the upload, as for any monitored app.
- Check what is stored with `tryaii monitor status --app claude-code --since 30d`.

Each event has the process (`main`, `Explore`, `general-purpose`, ...), the project base name as its file, provider `anthropic`, the model, the token counts including the cache fields, the estimated cost and, for a main-conversation call, whether it was a cold cache restart (`cache_cold`, see `report`). It has no prompt hash, no latency, no effort level and no session reference: the import groups calls by session on this machine only, to work out `cache_cold`, and sends no session id. Without `analyze` it has no classification; after `analyze`, new events carry the task profile (category, subcategory, confidence) and the Anthropic model recommended for the task, never the text.

## Live telemetry

`connect` can also set up Claude Code's own OpenTelemetry export, so every new Claude Code API call is reported to TryAii as it happens. It is a separate question from the import of past calls: `--no-live` skips it, `--live-only` skips the import. It needs the app key in `TRYAII_API_KEY`.

With your yes, it adds this to your **user** settings file, `~/.claude/settings.json` (Claude Code ignores these variables in a project's settings), after saving a copy as `settings.json.tryaii-backup-<time>`:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://api.tryaii.com/v1/otlp/claude-code/v1/logs",
    "OTEL_LOG_USER_PROMPTS": "0",
    "OTEL_LOG_TOOL_DETAILS": "0"
  },
  "otelHeadersHelper": "tryaii claude-code otel-headers"
}
```

- Every other setting stays as it is. `--dry-run` shows exactly what would be added.
- If Claude Code already sends telemetry somewhere else (another endpoint, headers helper or OTLP headers), or a setting that exports prompts, tool details or raw API bodies is on, `connect` stops and tells you why. It never overwrites another tool's settings.
- Your app key is not written into Claude Code's settings. Claude Code runs `tryaii claude-code otel-headers` to get it (at startup and every 29 minutes); the key stays in `~/.tryaii/claude-code/`.
- It applies to Claude Code sessions you start afterwards. From that moment on, calls come by live telemetry and the log import skips them, so no call is counted twice.
- `tryaii claude-code status` shows what is connected, the endpoint, the last import and whether Claude Code sends its session id, and warns loudly if Claude Code is set to export prompts, tool details or raw API bodies.
- `--no-session` also adds `"OTEL_METRICS_INCLUDE_SESSION_ID": "false"` (Claude Code's own switch, on by default), so Claude Code sends no session id and TryAii keeps no session reference; the cache timer then shows no sessions. It works on a setup that is already connected: `tryaii claude-code connect --live-only --no-session`. If your settings already set that variable to another value, `connect` stops and tells you; it never overwrites it.
- `disconnect` removes exactly the settings it added (including the `--no-session` one).

### What TryAii receives

| | |
|---|---|
| **Received** | model, token counts (input, output), cache tokens (read, write), cost estimate, duration, time, whether the call came from the main conversation or a subagent (and the subagent type), the effort level, and a session reference (a keyed hash of Claude Code's session id, for the cache timer). The request id is used only to make a de-duplication id; it is not stored |
| **Never received** | prompts, code, file contents, tool inputs and outputs |
| **Dropped on arrival, never stored** | your email and account and organization ids, the raw session id, other event types |

Prompts, code, file contents and tool inputs are not sent at all: the settings above keep Claude Code's content switches off. Claude Code does attach a few standard attributes to every telemetry event, including your account email when you are signed in, and there is no setting to turn that off; TryAii's server drops them before anything is stored.

To show the cache timer (how long the prompt cache stays warm in each session, and which calls were cold restarts), TryAii's server turns Claude Code's session id into a session reference on arrival: a keyed hash made with a secret only TryAii holds, different for each app, so it cannot be turned back into the session id or matched across apps or with any other service. The raw session id is never stored, queued or logged. The session reference is used only for the cache timer and is removed after 7 days; the dashboard names sessions "Session 1", "Session 2", ..., never by the reference. To leave sessions out, run `tryaii claude-code connect --live-only --no-session` or set `OTEL_METRICS_INCLUDE_SESSION_ID=false` in Claude Code's settings; the cache timer then shows no sessions.

Live telemetry carries no project and no task profile; `sync` (below) adds them from your local logs.

## `sync` (live advice)

```
tryaii claude-code sync
```

For the calls that came by live telemetry, `sync` reads Claude Code's logs on this machine (calls since live telemetry was turned on, at most the last 7 days; `--days N` to change it) and sends TryAii, per call: the project folder's base name (never a path; a value that looks like a path is left out), the subagent type (live telemetry reports a custom subagent only as `custom`) and, if you ran `analyze` and your consent is current, the task type (category, subcategory, confidence) and the Anthropic model recommended for it. It also sends, for the main conversation and each subagent type, a summary of `analyze`'s advice: the share of each task type, which models are good enough, the recommended model, the estimated saving and the confidence (not the advice sentence). It never sends text, paths, the session id or your email; the request is checked for text fields before it leaves the machine.

- It needs both parts of `connect` (the log import and live telemetry), the app key, and upload on (`tryaii monitor upload`). Otherwise it says what is missing and exits 0.
- Each call is matched to the live event TryAii already holds by the id the live telemetry uses (made from the request id). Calls younger than 2 minutes wait for the next run; a call the server has not stored yet is retried for 24 hours.
- If the stored `analyze` result is more than a day old and your consent is current, `sync` runs `analyze` again first (on this machine, as `analyze` does). Without that consent it sends only the project and the subagent type.
- It runs at most once every 5 minutes; `--background` starts it detached and returns at once; `--quiet` prints nothing. Its state is in `~/.tryaii/claude-code/sync.json`.
- `tryaii monitor upload off` stops it, as it stops every upload.

**Plugin hook.** The TryAii plugin for Claude Code (`integrations/claude-code-plugin`) has a `SessionEnd` hook that runs `tryaii claude-code sync --background --quiet` when a Claude Code session ends. It never blocks or slows the end of a session and always exits 0, also when `tryaii` is not installed or not connected. Without the plugin, run `tryaii claude-code sync` yourself now and then.

## `analyze` (opt-in)

```
tryaii claude-code analyze --days 30
```

Says which model is good enough for each kind of task you give Claude Code. It reads, **on this machine only**, the text of your own messages in the main conversation (not tool results, system reminders or other injected context) and the instruction each subagent was started with. Each text is embedded with TryAii's local embedding model (the one the monitor uses), classified against the benchmark categories and ranked like a monitor route. Texts that look like secrets (API keys, tokens, private keys, passwords) are skipped and counted. The text is never printed, stored, hashed or uploaded.

It explains this and asks first (`--yes` skips the question; it is needed without a terminal). Your consent is stored in `~/.tryaii/claude-code/consent.json` and recorded with the result.

Per task it gives two answers: the cheapest **Anthropic** model whose benchmark quality is within 0.05 of your model's at a price no higher (what you can act on in Claude Code), and the overall winner across providers (for information). Per process (`main`, each subagent type), weighted by cost, it shows the kinds of task, how much of the cost each Anthropic model is good enough for, and advice: the cheapest model good enough for at least 80% of the cost, with what moving those calls would have saved per month. For a subagent that means `model: <alias>` in its frontmatter; for the main conversation `/model <alias>`.

Benchmarks describe short, self-contained tasks better than long agentic coding sessions, so subagent advice fits better; main-thread advice is always marked low confidence.

The result (categories and model ids per call, no text) is stored in `~/.tryaii/claude-code/analysis.json`; `report` shows it as an extra section, `connect` sends the task profile and the Anthropic recommendation with new imported events, and `sync` sends them for calls that came by live telemetry, together with the per-process advice summary (task shares, good-enough models, recommended model, estimated saving, confidence; never text). Imported events stored before you ran `analyze` keep no classification.

## `disconnect`

Removes the settings live telemetry added to `~/.claude/settings.json` (exactly those; a backup is saved first) and what `connect`, `analyze` and `sync` stored on this machine (`~/.tryaii/monitor/claude-code/` and `~/.tryaii/claude-code/`). Claude Code's own logs are not touched. Events already uploaded stay on your tryaii.com account until you delete the app there.

## Exit codes

`0` success, `1` runtime failure (for example no Claude Code logs found), `2` usage error or a declined confirmation.
