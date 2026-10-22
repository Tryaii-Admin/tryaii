# `tryaii monitor` — watch and steer the LLM calls your app makes

`tryaii.monitor.wrap(client)` wraps the OpenAI SDK client your application
already uses: every call is recorded locally, and every call is
**shadow-routed** — tryaii works out which model would have given at least the
quality of the model you asked for, for at most its price, and records that
winner next to the model that actually answered. The model id in your code
stays where it is; it is the **anchor** the winner is measured against and the
fallback whenever tryaii cannot or should not swap. Per named sub-process you
choose a mode: `off` (monitor only), `stable` (pin one model for 24 hours),
`dynamic` (route every call) or `managed` (always one model you assign).
`tryaii monitor` reads those records. With an app key (`TRYAII_API_KEY`, see
[`tryaii apps`](apps.md)) the wrapper also uploads the call metadata to your
tryaii.com dashboard and follows the modes set there.

```bash
tryaii monitor status                                  # per app/process roll-up
tryaii monitor set "purchase item" stable --app shop   # pin the best model for 24h
tryaii monitor set summarize managed --model openai/gpt-5-mini   # always this model
tryaii monitor events --app shop -n 50 --json          # the raw event lines
```

## Quick start

```bash
pip install tryaii openai        # or: npm install tryaii openai
tryaii login                     # optional, free: the full catalog (322 models)
```

Without login the starter catalog (45 models) is used; after `tryaii login`
the full catalog downloads automatically ([docs](login.md)).

```python
from openai import OpenAI
import tryaii.monitor as monitor

client = OpenAI()                       # your existing client, unchanged
monitor.wrap(client, app="shop")        # patches client.chat.completions.create in place

with monitor.process("purchase item"):  # name the sub-process (optional)
    reply = client.chat.completions.create(
        model="gpt-5.5",                # stays in your code: the anchor and the fallback
        messages=[{"role": "user", "content": "Find the cheapest listing for ..."}],
    )
```

Then, in a terminal:

```
$ tryaii monitor status
tryaii monitor -- app: shop (last 24h)

process        mode     pinned          calls  tokens  est cost  at winner  savings  top winner share
checkout       off      openai/gpt-5.4      0       0    0.0000          -        -  -
purchase item  stable   -                  24   23331    0.1897     0.0750   0.1106  openai/gpt-5.4 68%
    -> /srv/app/cart/flow.py
    -> C:/app/checkout.py
summarize      dynamic  -                   1    2500    0.0011     0.0011   0.0000  openai/gpt-5-mini 100%
    -> /srv/app/summarize.py

totals: 25 calls, 1 errors, est cost 0.1908, at winner 0.0762, savings 0.1106
realised: 1 swapped calls, saved -
source: local
upload: off
```

`est cost` is what the calls cost at the model that answered (catalog
pricing, your token counts); `at winner` is what the same calls would have cost
at the shadow-routing winners; `savings` is the difference (the saving still
available). `realised` counts the calls tryaii actually swapped and what they
saved against the model in your code (`-` for records written before 0.7,
which lack the anchor price). `top winner share` is the model that won most
often and how often. The `->` lines are the files each sub-process runs in.
`source` says whether a dashboard config is in force, `upload` how far the
upload got (see [status](#status)). The SDK side is documented in
[docs/sdk/monitor.md](../sdk/monitor.md).

## What is stored, and where

Everything lives under `~/.tryaii/monitor/<app>/` (or `$TRYAII_DRE_DATA_DIR/monitor/<app>/`):

| File | Content |
|---|---|
| `events-YYYY-MM-DD.jsonl` | One line per call: app, sub-process, call site (file relative to the working directory, line, function), provider class, model requested / resolved / used, mode, the shadow-routing winner and whether this client could have sent it, route status, classification, token usage (including what the provider's prompt cache read and wrote, and for how long it is kept — see below), estimated cost at the model used and at the winner, latency, stream flag, error class, a hash of the routing text and, when the wrapper runs with `cachelint`, a `cache` block (the predicted cache outcome and, now and then, the lint report — [details](../sdk/monitor.md#cache-fields)). **No prompt or response text.** |
| `vectors-YYYY-MM-DD.jsonl` | The prompt's embedding vector (the one the router computed), keyed by the same hash, one line per distinct prompt per day. `TRYAII_MONITOR_VECTORS=0` turns this off. |
| `pins.json` | The pinned model per sub-process in `stable` mode, with its expiry and the counts it was chosen from. |
| `config.json` | The mode per sub-process (and the model of a `managed` one), written by `tryaii monitor set`. |
| `remote.json` | The modes set on the tryaii.com dashboard, kept current by the wrapper while `TRYAII_API_KEY` is set. |
| `upload.json` | How many bytes of each events file were uploaded, the last success and the last error. |

Next to the app directories, in `<data dir>/monitor/`:

| File | Content |
|---|---|
| `settings.json` | `tryaii monitor upload on/off`: whether this machine uploads. |
| `notice.json` | Which version of the data notice this machine has shown, and when. |
| `upload-hash.key` | The secret that keys the uploaded prompt hash. Never uploaded; delete it to start a new hash series. |

The lint report in the `cache` block never quotes the prompt: each finding
keeps only a mask of the matched text, written that way to the file —
ASCII digits become `0`, whitespace a space, ASCII punctuation stays, and
every other character (any letter, a non-ASCII digit, an accent, an emoji)
becomes `x`; at most 40 characters (`2026-10-18 14:02` →
`0000-00-00 00:00`, `{{sku}}` → `{{xxx}}`).

Without `TRYAII_API_KEY` nothing is uploaded: no event, no vector, no prompt
leaves the machine. With a key, the event lines (metadata only) are uploaded
in the background; vectors and prompt or response text never are — see
[What is uploaded](../sdk/monitor.md#what-is-uploaded). Embeddings are stored
because they let tryaii re-route your past traffic locally when a new model
appears; treat them as pseudonymous data, not anonymous — approximate wording
can be recovered from an embedding.

## Naming sub-processes

A sub-process is a named activity of your app ("purchase item", "summarize"),
possibly spread over several files. The name is resolved per call, first
match wins:

1. the per-call argument `tryaii_process="..."` (removed before the request
   is sent);
2. the enclosing `with monitor.process("..."):` block (propagates into
   asyncio tasks; threads start empty);
3. `wrap(client, process="...")`;
4. the call site: `<file>:<function>` of the code that made the call
   (`checkout.py:purchase_item`), or `<file>:L<line>` when the function is
   unknown.

The app name comes from `TRYAII_APP`, then `wrap(app=)`, then the working
directory's name.

## Modes

| Mode | What is sent | When to use |
|---|---|---|
| `off` (default) | The model in your code. tryaii only records and shadow-routes. | Watch first. |
| `stable` | One pinned model for 24 hours: the model that **won most** of this sub-process's calls in the last 24 h (last 7 days when fewer than 20 calls). On the first call with no history the call is routed in-path and its winner is pinned. The anchor is sent whenever the pin is not reachable. | Predictable behaviour: one model per sub-process per day. |
| `dynamic` | The best-for-less model of each call among the models this client can send. | Lowest cost at the anchor's quality. |
| `managed` | The model assigned to the sub-process (`--model`, or on the dashboard), when this client can reach it and your policy allows it; otherwise the model in your code, recorded with `route_status: unreachable_managed`. The winner is still recorded. | You decided which model a sub-process uses. |

The mode is resolved per call: `TRYAII_MONITOR_MODE` > the dashboard
(`remote.json`) > `wrap(mode=)` > the sub-process entry in `config.json` >
`default_mode` in `config.json` > `off`. `tryaii monitor set <process> <mode>`
writes `config.json`; a running wrapper re-reads it (and `remote.json`)
within a few seconds, so switching needs no restart.

"Best-for-less" means: among the catalog models ranked for this prompt, the
first whose quality is at least the anchor's quality minus 0.05 and whose
blended price is at most the anchor's. When nothing beats the anchor, the
winner is the anchor itself.

## Which models can be sent

The winner (the recommendation) is chosen on the whole catalog — minus the
models your [data-save policy](policy.md) excludes — so a better model at a
provider you have no key for is still *recommended* (it shows as the winner
with `winner_reachable: false`), but it is never *routed*: `dynamic` then
sends the best-for-less model among the models the client can reach. A model
the policy excludes is neither recommended nor sent. The saved policy is read
once per process; if it cannot be applied (for example the starter catalog,
which carries no origin data), tryaii recommends and swaps nothing. What can
be sent depends on the client's `base_url`:

| `base_url` host | Reachable models |
|---|---|
| `api.openai.com` (the default) | `openai/*` only |
| `openrouter.ai` | the whole catalog |
| anything else (Azure OpenAI, a local OpenAI-compatible server, a proxy) | only the model in your code — monitor only, never a swap |

Pointing the client at OpenRouter with an `OPENROUTER_API_KEY` is the way to
let `stable` / `dynamic` swap across providers.

## The daemon

Shadow routing runs through the tryaii routing daemon ([docs](../daemon.md)),
a separate process that keeps the embedding model warm; the embedding model
is never loaded inside your application. The first wrapped call starts the
daemon (about 0.5 GB of RAM, a minute of warm-up on first run); in `off` and
`stable` mode routing happens on a background thread and adds no latency to
your calls. `dynamic` mode and the first `stable` call of a sub-process route
in-path, within `TRYAII_MONITOR_ROUTE_TIMEOUT_MS` (default 1500 ms) and only
when the daemon is already up — otherwise the anchor is sent and the event
says why (`route_status`: `unavailable`, `timeout`, `queue_full`, `disabled`,
`skipped`). `TRYAII_NO_DAEMON=1` disables routing entirely (recording
continues).

Every monitor step is fail-open: a problem inside tryaii never changes or
fails your call, and a provider error is re-raised unchanged after an error
event is written.

## Command reference

```
tryaii monitor -- Watch and steer the LLM calls your app makes

Usage:
  tryaii monitor <verb> [options]

monitor reads the local records written by tryaii.monitor.wrap(client):
every call your app makes, the model it used, and the model tryaii's
shadow routing would have picked (best-for-less: at least the anchor's
quality for at most its price). Records live under
<data dir>/monitor/<app>/; only metadata and prompt embeddings are stored,
never prompt text.

Verbs:
  status                Per app/process: mode, pinned model, calls, tokens, est. cost, savings
  set <process> <mode>  Set a process's mode: off (monitor only), stable (24h pin), dynamic,
                        managed --model <id> (always that model)
  apps                  List the apps with monitor records
  events                Print the most recent events (--json emits JSONL)

Options:
  --app <id>            App to inspect (default: every app for status, the only app otherwise)
  --since <window>      Window for status: 24h (default), 7d, 30d
  -n <count>            events: number of lines (default 20)
  --model <id>          set: the model of a managed process
  --now <iso8601>       Override the current time (window and pin evaluation)
  --json                Machine-readable output

With TRYAII_API_KEY set, the wrapper also uploads these records (metadata
only) to your tryaii.com dashboard and follows the modes set there; see
'tryaii help apps'.

Examples:
  tryaii monitor status
  tryaii monitor set "purchase item" stable --app shop
  tryaii monitor set summarize managed --model openai/gpt-5-mini
  tryaii monitor events --app shop -n 50 --json

Exit codes:
  0 success, 1 runtime failure, 2 usage error or unknown app/mode.

Docs: docs/cli/monitor.md
```

`tryaii help monitor` prints this page; `tryaii monitor <verb> --help` prints
the verb's page. Both CLIs (npm and PyPI) print byte-identical output.

### `status`

One block per app (or only `--app`): the table above, the files of each
sub-process, a totals line and a `realised` line. Sub-processes named in
`config.json`, `remote.json` or `pins.json` show even with zero calls;
`pinned` shows the pinned model while the pin is valid; a managed
sub-process shows `managed (<model>)` in the mode column. Each block ends
with two lines:

- `source: local`, or `source: remote etag <etag> fetched <time>` when a
  dashboard config (`remote.json`) is in force;
- `upload: off` (no upload has run for this app), `upload: ok <n> events at
  <time>` (the event lines uploaded so far and the last success), or
  `upload: error <reason>` (`network`, `http 503`, `rate_limited`,
  `invalid_key`, `bad_batch: ...`, `forbidden_field: ...`).
 `--since` selects the window (`24h`, `7d`, `30d`); `--now`
fixes the reference time (useful in scripts and tests). `--json` prints the
status document (one object with `--app`, otherwise a list). With no records
at all: `no monitor records under <data dir>/monitor` (exit 0; `[]` with
`--json`).

### `set <process> <mode>`

Writes the mode into `<app>/config.json` and prints
`<process>: <mode> (app <app>)`. `--app` may be omitted when exactly one app
has records or `TRYAII_APP` is set. `managed` needs `--model <id>` (a catalog
id such as `openai/gpt-5-mini`, or a native id the catalog resolves) and
prints `<process>: managed (<model>) (app <app>)`. Errors (exit 2):
`mode must be off, stable, dynamic or managed`, `managed mode needs --model
<id>`, `--model only applies to managed mode`. A mode set on the dashboard
takes precedence over `config.json`.

### `apps`

The apps with monitor records, one per line (`--json`: an array).

### `events`

The newest `-n` events (default 20) of one app, oldest first:

```
2026-10-11T11:35:00.000Z  purchase item | gpt-5.5 | off | winner - | - tok | - | ok | error RateLimitError
2026-10-11T11:40:00.000Z  purchase item | gpt-5.5 | off | winner - | 550 tok | 0.0040 | unavailable
2026-10-11T11:50:00.000Z  summarize | gpt-5-mini | dynamic | winner openai/gpt-5-mini | 2500 tok | 0.0011 | ok
```

Timestamp, sub-process, model used, mode, winner, tokens, estimated cost,
route status, and the error class when the call failed. When the provider
reported prompt-cache usage the line also shows it after the token count:
`cached <n>` (input tokens read from the provider's cache) and
`cache write <n>` (tokens written to it on this call):

```
2026-10-13T08:00:00.000Z  assist | gpt-5.6 | off | winner openai/gpt-5-mini | 3380 tok | cached 1024 | cache write 2048 | 0.0071 | ok
```

`--json` prints the stored event lines unchanged (JSONL), one object per
line — see `shared/monitor/SPEC.md` §3.1 for the fields. Since contract
v1.5 (TS-228) the `usage` object carries `cached_tokens`,
`cache_write_tokens` and `cache_writes` (`[{"tokens", "ttl"}]`, the write
broken down by lifetime: `5m`, `1h`, `30m`, `24h` or `explicit`), and the
event carries `cache_ttl_policy`, the lifetime the request asked for. A
field the provider does not report is `null`; `0` means the provider
reported zero (for example a prompt below its minimum cacheable size).
Older lines simply lack the keys. `status --json` sums
`cached_tokens` and `cache_write_tokens` per process and in `totals`
(`null` when no event in the window reported them); the human table is
unchanged.

### `upload [on|off]`

```
$ tryaii monitor upload
[tryaii] monitor records sync to your TryAii account.
Sent: model names, token counts, costs, timings, file paths, a task profile.
Never sent: your prompts, responses or API keys.
Turn off: tryaii monitor upload off   Details: https://tryaii.com/privacy-policy#cli-sdk

upload: on
```

Without an argument: the data notice and the state. `off` stops uploads from
this machine (`upload: off (tryaii monitor upload off)`), `on` starts them
again. `TRYAII_MONITOR_UPLOAD=0`, `TRYAII_TELEMETRY=0`, `DO_NOT_TRACK=1` and the
account switch on tryaii.com win over `on`; the line then names which one is
in force. `--json` prints `{"upload", "reason", "notice_version"}`.

## Environment variables

| Variable | Effect |
|---|---|
| `TRYAII_APP` | The app name (overrides `wrap(app=)` and the working-directory name); also the default `--app` for `set` and `events` |
| `TRYAII_MONITOR_MODE` | Force one mode (`off`, `stable`, `dynamic`, `managed`) for every sub-process of this process, above every other source (`managed` from here has no model, so the model in your code is sent) |
| `TRYAII_API_KEY` | An app key (`tryaii apps create`): turns on upload and the dashboard config ([details](../sdk/monitor.md#upload-and-the-dashboard)) |
| `TRYAII_API_URL` | API base for upload and the dashboard config (default `https://api.tryaii.com`) |
| `TRYAII_MONITOR_UPLOAD` | `0` stops uploading (the dashboard config still applies) |
| `TRYAII_TELEMETRY` | `0` stops uploading, like `TRYAII_MONITOR_UPLOAD=0` |
| `DO_NOT_TRACK` | `1` stops uploading |
| `TRYAII_MONITOR_REMOTE` | `0` stops following the dashboard config (upload continues) |
| `TRYAII_MONITOR_VECTORS` | `0` stops writing `vectors-*.jsonl` (default: on) |
| `TRYAII_MONITOR_ROUTE_TIMEOUT_MS` | Budget for in-path routing (`dynamic`, first `stable` call); default `1500` |
| `TRYAII_NO_DAEMON` | `1` disables routing; calls are still recorded with `route_status: disabled` |
| `TRYAII_DRE_DATA_DIR` | Data dir; default `~/.tryaii`. Records live under `<data dir>/monitor/` |
| `TRYAII_DAEMON_IDLE` | Seconds the daemon stays up after its last request (default 900) |

## Known limits

- Wrapped surface: `client.chat.completions.create` of the official `openai`
  SDK (Python `OpenAI` / `AsyncOpenAI`; Node `OpenAI`). The Responses API and
  the `anthropic` SDK are not wrapped yet.
- Streaming: `stream_options.include_usage` is set on streams to OpenAI and
  OpenRouter so the last chunk carries `usage`; other OpenAI-compatible hosts
  return no `usage` on streams, so those events have null tokens and costs.
  In Node, `stream.tee()` on a monitored stream is not supported.
- Models the catalog does not know (local servers, private deployments) are
  recorded as requested, with no cost estimate and no winner.
- Pins are per sub-process; a sub-process that uses two different anchors
  shares one pin.
- No retention yet: daily files accumulate until you delete them.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success — including the "no records" notices |
| 1 | Runtime failure (`error: <message>`) |
| 2 | Usage error: unknown verb, option, app or mode; missing or invalid argument |
