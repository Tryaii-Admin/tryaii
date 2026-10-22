# Privacy: what the tryaii CLI and SDK send

This page describes the data the `tryaii` command-line tool and SDK (Python and
Node) send to TryAii. The binding text is the
[TryAii privacy policy, section "Command-line tool and SDK"](https://tryaii.com/privacy-policy#cli-sdk);
this page says the same thing in developer terms and points to the code.

Notice version: **2026-10-18**.

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
