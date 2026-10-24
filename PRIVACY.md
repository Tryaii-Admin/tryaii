# Privacy: what the tryaii CLI and SDK send

This page describes the data the `tryaii` command-line tool and SDK (Python and
Node) send to TryAii. The binding text is the
[TryAii privacy policy, section "Command-line tool and SDK"](https://tryaii.com/privacy-policy#cli-sdk);
this page says the same thing in developer terms and points to the code.

Notice version: **2026-10-24**.

## Nothing is sent unless you sign in and set an app key

Routing runs on your machine. The model catalog is downloaded, but your prompts are
never sent to TryAii to be routed.

`tryaii.monitor.wrap(client)` writes a record of each LLM call under
`~/.tryaii/monitor/<app>/` (or `$TRYAII_DRE_DATA_DIR/monitor/<app>/`). Without
`TRYAII_API_KEY`, those records never leave your machine.

## With an app key: metadata about each call

When `TRYAII_API_KEY` holds an app key from `tryaii apps create`, the SDK uploads
each call record to your tryaii.com account. A record holds:

- the app and sub-process names you chose (for example `shop` and `purchase item`);
- the file (relative to your project; never an absolute path) and function that
  made the call, and the line number;
- the model you asked for, the model that was used, and the model TryAii would
  have recommended;
- the time of the call, how long it took, token counts and estimated cost;
- the kind of error if the call failed (HTTP status and exception class name, not
  the message);
- a keyed hash of the prompt: an HMAC-SHA256 of its sha256 with a secret that
  stays in `~/.tryaii/monitor/upload-hash.key`, so TryAii cannot check a guess
  of a prompt against it;
- a task profile: the benchmark category and subcategory the prompt is closest to,
  with a confidence number.

The exact field list is `shared/monitor/SPEC.md` §3.1. The uploader code is
`packages/python/tryaii/monitor/upload.py` and `packages/node/src/monitor/upload.ts`.

**Never sent:** prompt text, response text, prompt embeddings, your provider API
keys, file contents. The uploader refuses to send any record that has a field
named `prompt`, `messages`, `response`, `content`, `text` or `raw`, and the
server rejects such a batch too.

The first upload from a machine prints a four-line notice to your terminal.
`tryaii apps create` prints it too, and records on your account that you saw it.

## Claude Code (`tryaii claude-code`, Python only)

If you connect Claude Code with `tryaii claude-code connect`, TryAii receives a
record of each Claude Code API call in two ways, each with its own yes/no: the
command imports your past calls (the last 30 days by default) from Claude Code's
logs on your machine, and, if you agree, Claude Code's own telemetry reports each
new call as it happens. Details: `docs/cli/claude-code.md`; contract:
`shared/monitor/SPEC.md` §10.

- **Live telemetry**, per call: the model, the token counts (input, output, cache
  read and cache write), Claude Code's cost estimate, how long the call took, the
  time, whether it came from the main conversation or a subagent (and the
  subagent type), the effort level, and a session reference (below).
- **Import**, per call: the model, the token counts (including cache read and
  write), the estimated cost, the time, the main conversation or subagent type,
  the name of the project folder (for example `my-app`, never its path), and
  whether the call had to write the prompt cache again after a pause (a "cold
  restart", worked out on your machine). No session id or reference.
- **`tryaii claude-code sync`**, for calls reported by live telemetry: the
  project folder name, the subagent type, and, if you ran `analyze`, the task
  type and the recommended model; plus, for the main conversation and each
  subagent type, a summary of advice (share of each task type, which models are
  good enough, the recommended model, the estimated saving). The TryAii plugin
  for Claude Code runs it when a Claude Code session ends; you can also run it
  yourself.

Never sent: prompt or response text, code, file contents, tool inputs or
outputs, paths, Claude Code's session id, your email. Claude Code's request id
is used only to make an id that stops a call being counted twice; it is not
stored. Claude Code's telemetry attaches your email address, account and
organization ids and session id to every record; the server drops them on
arrival, and the settings `connect` adds keep Claude Code from sending the rest.

**Session reference.** It lets TryAii show how long the prompt cache stays warm
in each Claude Code session (the cache timer). The server makes it from Claude
Code's session id with a keyed hash (HMAC-SHA256 with a per-app key derived
from a secret only TryAii holds, cut to 64 bits), so it cannot be turned back
into the session id or matched across apps or with other services. It is used
only for the cache timer and removed after 7 days. To leave it out, run
`tryaii claude-code connect --live-only --no-session` or set
`OTEL_METRICS_INCLUDE_SESSION_ID=false` in Claude Code; the cache timer then
shows no sessions.

Task types and recommendations come from `tryaii claude-code analyze`, which
reads the text of your messages on your machine only, and only after you agree;
the text never leaves your machine. `tryaii claude-code disconnect` removes
exactly the settings `connect` added and what the commands stored locally.

## Turn it off

Each of these stops uploads. Local recording continues.

| How | Scope |
|---|---|
| `tryaii monitor upload off` | this machine (`tryaii monitor upload on` to resume) |
| `TRYAII_TELEMETRY=0`, `DO_NOT_TRACK=1` or `TRYAII_MONITOR_UPLOAD=0` | this process |
| Pause uploads on tryaii.com (dashboard) | every machine using your apps' keys |
| Revoke the app key on tryaii.com | that key, immediately |
| `tryaii logout` | the CLI session; app keys keep working until revoked |

`tryaii monitor upload` shows the notice and what is currently in force.

## Where it is stored and for how long

Stored on Amazon Web Services in the EU (Stockholm, eu-north-1).

| Data | Kept |
|---|---|
| Call records | 90 days, then deleted automatically |
| Claude Code session references | 7 days, then removed automatically |
| Claude Code advice per process | 90 days |
| Daily totals per app (calls, tokens, cost) | while the app exists |
| Upload batch log and dashboard change log | 13 months |
| A deleted app | restorable for 30 days, then every record of it is deleted; only the app id, owner and dates remain as proof of deletion |
| Backups | 7 days (database snapshots) |

TryAii uses aggregate statistics across accounts to improve its recommendations.
Those statistics come only from a separate table that has no user, app, process,
file, function or prompt-hash column. Each row combines at least 3 different apps.

## Your controls and contact

- Delete an app (and with it all its data) from the tryaii.com dashboard, or all
  your monitor data at once from your account settings.
- Ask for a copy of your data, or for anything else about it:
  **support@tryaii.com**. We answer within 30 days.
