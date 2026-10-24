# monitor (SDK surface)

`tryaii.monitor` (Python) and the `tryaii/monitor` subpath (Node) wrap the
OpenAI or Anthropic SDK client your application already uses
([Anthropic clients](#anthropic-clients) below). Each call is recorded
locally and shadow-routed; the model id in your code stays and is the anchor
(and the fallback). The CLI that reads the records is
[`tryaii monitor`](../cli/monitor.md), which also explains what is stored,
the modes, reachability and the daemon. This page is the API.

## `wrap(client, ...)`

**Python**

```python
import tryaii.monitor as monitor

mon = monitor.wrap(client, app="shop", process=None, mode=None)
```

| Argument | Default | Meaning |
|---|---|---|
| `client` | — | An `openai.OpenAI` or `openai.AsyncOpenAI` (anything with `base_url` and `chat.completions.create`), or an `anthropic` client (`Anthropic`, `AsyncAnthropic`, `AnthropicBedrock`, `AnthropicVertex` and their async twins: anything with `messages.create` and no `chat`). Patched **in place**: `client.chat.completions.create` (or `client.messages.create` and `client.messages.stream`) becomes the monitored function. |
| `app` | `TRYAII_APP`, else the working directory's name | Where records go: `<data dir>/monitor/<app>/` |
| `process` | `None` | Default sub-process name for this client (below the per-call tag and `process()` block) |
| `mode` | `None` | `off`, `stable`, `dynamic` or `managed` for this client, below `TRYAII_MONITOR_MODE` and the dashboard config, above `config.json` (`managed` here has no model, so the model in your code is sent — assign the model in `config.json` or on the dashboard) |
| `cachelint` | `False` | Also run the [runtime cache lint](client/cache-lint.md) on every call and add its result to the event (the `cache` block, [below](#cache-fields)). Python prints the warn-only lint lines too; Node records the `cache` block only (no stderr lines yet) |
| `vectors` | `TRYAII_MONITOR_VECTORS` (on) | Write the prompt embedding to `vectors-*.jsonl` |
| `route_fn`, `inline` | `None` | Test seam: a function that replaces the daemon, run synchronously (see below) |

Returns the `Monitor`: `.app`, `.dir` (the app's record directory),
`.provider` (`openai`, `openrouter` or `other`, from `base_url`) and
`.transport`. Wrapping the same client twice replaces the earlier monitor.

The wrapped `create` accepts one extra keyword, `tryaii_process="..."`,
which names the sub-process for that call and is removed before the request
is sent. Sync and async clients are both supported; streams come back as a
monitored stream that behaves like the SDK's (iteration, `with`, `close()`)
and records the call when the stream ends.

**Node**

```ts
import { wrap } from 'tryaii/monitor';

const mon = wrap(client, { app: 'shop' });   // also: process, mode, vectors
```

Same options in camelCase. In mode `off`, non-streaming calls return the
SDK's own `APIPromise` untouched (`.withResponse()` keeps working); in
`stable` and `dynamic`, and for streams, the wrapper returns a plain
`Promise` of the same value. Streams are forwarded chunk by chunk and
recorded on completion (`tee()` is not supported on a monitored stream).

With Langfuse (`observeOpenAI` from `@langfuse/openai`), call order does not
matter: `wrap(client)` then `observeOpenAI(client)`, or
`wrap(observeOpenAI(client))`, each give one Langfuse generation and one
TryAii event per call. On a Langfuse proxy, `wrap()` patches the client behind
the proxy so Langfuse traces each call once. Langfuse's proxy returns a plain
`Promise`, so `.withResponse()` is not available through it in either order.
To see TryAii's recommendation in Langfuse, see
[Works with Langfuse](#works-with-langfuse).

## `install(...)`

**Python**

```python
import openai
import tryaii.monitor as monitor

monitor.install(app="shop", mode="off")   # True when the openai or anthropic classes were patched
```

Sets the module defaults (`app`, `process`, `mode`, `cachelint`) and patches
`openai.resources.chat.completions.Completions.create` and
`AsyncCompletions.create` — and, when `anthropic` is imported,
`anthropic.resources.messages.Messages` / `AsyncMessages` (`create` and
`stream`, which also serve the Bedrock and Vertex clients) — at class level,
so every client created in this process is monitored. Returns `False` — with
the defaults still set — when neither SDK has been imported yet: import it
first, then call `install()`. `install_anthropic()` patches the anthropic
classes alone.

**Node**: `install({ app, process, mode })` sets the defaults only and
returns nothing -- ES-module `openai` classes cannot be patched in place, so
wrap each client with `wrap()`.

## Anthropic clients

`wrap()` recognises an `anthropic` / `@anthropic-ai/sdk` client by its
`messages.create` (and no `chat`) and hands it to `wrap_anthropic` /
`wrapAnthropic` (also exported; it returns an `AnthropicMonitor`). The same
options, call-site capture, sub-process naming, privacy and upload rules
apply. What differs:

- **Wrapped surface**: `client.messages.create` (sync, async,
  `stream=True`) and `client.messages.stream()` (Python: the `with` /
  `async with` manager, recorded on exit from the accumulated message; Node:
  the `MessageStream`, whose internal `create()` call carries the call site
  captured at `stream()`). In Node `create()` always returns the SDK's own
  `APIPromise`, in every mode, so `.withResponse()`, `messages.stream()` and
  `messages.parse()` keep working. `client.beta.messages`, the Batches and
  token-counting endpoints, and `with_raw_response` are not wrapped.
- **Bedrock and Vertex**: `AnthropicBedrock` / `AnthropicVertex` share the
  `Messages` resource and are wrapped the same way. Bedrock ids
  (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) resolve to the catalog
  anchor once the region / vendor prefix and version suffix are stripped;
  Vertex ids (`claude-haiku-4-5@20251001`) resolve as they are. The id in
  your code is what is sent and recorded as `model_used`.
- **Provider `anthropic`**: the event's `provider` is `anthropic` for every
  Messages-API client (Bedrock and Vertex included), which tells the server
  that `prompt_tokens` excludes the cache, so its hit rate counts the cached
  and written tokens as input. Routing treats it like class `other`: the
  model is never swapped. Shadow routing still runs and records the
  winner (`winner_reachable: false`); `stable` and `dynamic` record their
  decision and send the model in your code; `managed` with another model is
  recorded as `unreachable_managed`.
- **Routing text**: the top-level `system` (string or text blocks) is the
  system message, then the last `user` message.
- **Usage** (contract v1.5, SPEC.md §3.1b, `anthropic` row):
  `prompt_tokens` = `input_tokens` as reported (Anthropic's `input_tokens`
  **excludes** cached and written tokens), `completion_tokens` =
  `output_tokens`, `total_tokens` = their sum when both are reported,
  `cached_tokens` = `cache_read_input_tokens`, `cache_write_tokens` =
  `cache_creation_input_tokens`, and `cache_writes` =
  `cache_creation.ephemeral_5m_input_tokens` as `{"ttl": "5m"}` and
  `ephemeral_1h_input_tokens` as `{"ttl": "1h"}` (`null` when the response
  has no `cache_creation`). A field the response does not report is `null`.
  Streams merge the `message_start` usage with the `message_delta` usage.
  The longest explicit `cache_control.ttl` of the request is recorded as
  `cache_ttl_policy` once the TS-229 plumbing has landed.
- **Install**: Python `pip install "tryaii[anthropic]"`; Node: install
  `@anthropic-ai/sdk` yourself (tryaii never imports it).

## `process(name)`

**Python** — a context manager (a `ContextVar`, so it follows `asyncio`
tasks; new threads start without a name):

```python
with monitor.process("purchase item"):
    client.chat.completions.create(model="gpt-5.5", messages=messages)

monitor.current_process()   # the innermost active name, or None
```

**Node** — `process(name, fn)` runs `fn` inside an
`AsyncLocalStorage` context: `await process('purchase item', () => client.chat.completions.create(...))`.

Precedence for the sub-process name: per-call `tryaii_process` > `process()`
block > `wrap(process=)` > call site (`file:function`).

## Mode and app resolution

Per call, the mode is `TRYAII_MONITOR_MODE` > the dashboard config
(`remote.json`: `processes[name].mode`, then `default_mode`) >
`wrap(mode=)` > `config.json` (`processes[name].mode`, then
`default_mode`) > `off`; the app is `TRYAII_APP` > `wrap(app=)` > the
working directory's name > `default`. `config.json`, `remote.json` and
`pins.json` are re-read when they change (checked at most every 5 seconds),
so `tryaii monitor set` and a dashboard change apply to a running app.

A `managed` sub-process sends the model assigned to it in `remote.json` or
`config.json` (`"model"` next to `"mode"`) when the client can reach it and
your data-save policy allows it; otherwise the model in your code is sent and
the event carries `route_status: "unreachable_managed"`.

## Data-save policy

The policy saved with [`tryaii policy set`](../cli/policy.md) applies to the
wrapper too. It is read once per process and evaluated against the catalog
in use: a model the policy excludes is never recommended and never sent.
`dynamic` (and the first call of a `stable` sub-process) routes in-path over
the models the client can reach **and** the policy allows, so when the
overall winner is unreachable the best reachable model is sent; the
recommendation recorded in the event is chosen over every policy-allowed
model (reachable or not). A policy that cannot be applied — the starter
catalog has no origin data, or the policy file is broken — allows nothing:
the model in your code is sent and no winner is recorded. Evaluating the
policy never loads the embedding model or starts the daemon.

## Fail-open and errors

Every monitor step (identity, routing, swap, recording) is wrapped: a failure
inside tryaii is logged at debug level and the call proceeds with the model
in your code. A provider exception is re-raised unchanged after an error
event (`error: {"status": <http status or null>, "class": <exception class>}`)
is written.

## Routing transport

Shadow routing goes through the tryaii daemon ([docs](../daemon.md)) on a
worker thread with a bounded queue (256 jobs); `dynamic` and the first
`stable` call of a sub-process route in-path within
`TRYAII_MONITOR_ROUTE_TIMEOUT_MS` (default 1500 ms), and only when a daemon
is already running — the wrapper never waits for a daemon to start on the
hot path. The daemon's `includeEmbedding` request returns the prompt's
vector for `vectors-*.jsonl`; an older daemon without the feature yields
events with `vector: false`. The embedding model is never loaded inside your
process. Pending shadow jobs are drained for up to 2 seconds at exit; a
call still waiting for the daemon warm-up at that point is recorded as
`route_status: unavailable` rather than lost.

## Cache fields

Every event records `usage.cached_tokens`: the prompt tokens the provider
served from its prompt cache, read from the response
(`usage.prompt_tokens_details.cached_tokens`, or `cache_read_input_tokens` in
Anthropic-shaped usage). `null` means the provider reported nothing; `0` means
it reported no hit. This needs no option.

With `cachelint` on, each event also ends with a `cache` block:

```json
"cache": {"outcome": "miss", "report": {"verdict": "EFFECTIVELY_UNCACHEABLE",
  "prompt_tokens": 1493, "stable_prefix_tokens": 11, "floor_tokens": 1024,
  "tools_tokens": 0, "system_tokens": 1486, "messages_tokens": 7,
  "findings": [{"severity": "high", "detector": "timestamp-datetime",
                "section": "system", "offset": 44, "excerpt": "0000-00-00 00:00"}],
  "recommendation": "at messages[0]:system+44 (1% in): Remove it, round it to a coarse bucket (day/hour), or move it into the final user message (after the cached prefix)."}}
```

- `outcome` — the lint's prediction for this call given the previous linted
  call of the **same sub-process** in this process: `hit`, `partial`, `miss`,
  or `null` (first call, a model switch, or a prediction of `UNKNOWN` /
  `AT_RISK`).
- `report` — the lint result, attached on the first linted call of each
  sub-process and afterwards only when it changed and at least 60 seconds
  have passed since the last one attached; `null` on every other call. The
  full analysis behind a report runs at most once a minute per sub-process,
  even when the prompt changes on every call (a timestamp, say); the
  `outcome` prediction is cheap and runs on every call. At
  most 10 findings, in prompt order. `recommendation` is the lint's first fix
  line (or, for `CACHEABLE_WITH_ACTION`, its enablement line) without its
  `[tag]`.
- Sections: a `system` or `developer` message counts toward `system_tokens`
  and its findings report `section: "system"`; tool definitions (the
  request's `tools`, and legacy `functions`) are linted with the messages and
  count as `tools`, in the prompt's tokens and in the verdict; every other
  message as `messages`. `recommendation` keeps the
  lint's own label (`messages[0]:system+44`).

**Redaction.** A finding never carries prompt text. Instead of the lint's own
excerpt (the match with its surrounding text), the event holds a **mask of
the matched span only**, character by character: an ASCII digit becomes `0`,
whitespace becomes a space, ASCII punctuation and symbols (`{`, `-`, `:`, ...)
stay, and everything else becomes `x` — ASCII letters, letters and digits of
any other script, accents and other combining marks, emoji and non-ASCII
symbols; at most 40 characters (39 and `…` when longer). So
`2026-10-18 14:02` → `0000-00-00 00:00`, `{{sku}}` → `{{xxx}}`,
`7f9c02aa-51b3` → `0x0x00xx-00x0`, `Grüße 東京` → `xxxxx xx`. The mask is applied when the event is
written, so the local file holds no prompt text either.

In Node the lint engine loads in the background on the first call (its
tokenizer data is several MB); a call recorded before it has loaded (an
error recorded at once, a full queue) has no `cache` block.
### The embedding model

The daemon embeds with `all-MiniLM-L6-v2-onnx-int8`: all-MiniLM-L6-v2 as an
int8 ONNX file (23 MB) on `onnxruntime`, with no PyTorch. The file comes in
the `tryaii-minilm-int8` package, which `pip install tryaii` installs; nothing
is downloaded at run time. Before it starts a daemon, the transport's warm-up
finds the file on the worker thread, never on the hot path:

1. `TRYAII_EMBEDDING_MODEL_PATH`, if set: a copy of the file, or a directory
   holding `model.onnx`, for a machine that cannot install the package. When
   it is set, it is the only source.
2. Otherwise, the installed `tryaii-minilm-int8` package.

The file must match the size and sha256 pinned in the SDK. When there is no
verified file (the package is missing or damaged, or the placed file is
wrong), the monitor works exactly as without a daemon. No daemon starts,
events are metadata-only (`route_status: unavailable`, no classification,
`vector: false`), and the model in your code is sent. It is reported without
flooding your logs:

- One warning on the `tryaii.monitor` logger, at most once per machine per
  day for the same reason, with the fix (`pip install --force-reinstall
  tryaii-minilm-int8`, or `TRYAII_EMBEDDING_MODEL_PATH`). Every other process
  start logs it at debug level only. The package logs nothing unless your
  application configures logging.
- `tryaii monitor status` ends with `classification: off since <time>
  (<reason>)` while the model is missing. The state lives in
  `<data dir>/embedding-model.json`, and it is removed as soon as a process
  finds the model again.

**Node** (TS-253) embeds in the application's process, with the same model
file, tokenizer, pooling and centroids as Python, so both SDKs classify a
prompt identically (similarities equal to 4 decimals). No Python and no daemon
are needed. The engine needs two optional peer dependencies:

```bash
npm install onnxruntime-node tryaii-minilm-int8
```

On Linux, set `ONNXRUNTIME_NODE_INSTALL=skip` for that install. Otherwise
`onnxruntime-node` also downloads its CUDA libraries, which the monitor never
uses (574 MB instead of 301 MB).

`TRYAII_EMBEDDING_MODEL_PATH` overrides the model package as in Python. The
warm-up builds the engine off the hot path; routes then take about 10 ms. When
`onnxruntime-node` or the model is missing, the wrapper falls back to the
daemon for one more release, then records metadata only. The once-a-day
warning goes to stderr, and the same `classification: off` status line
applies. `onnxruntime-node` ships native binaries for Windows (x64, arm64),
Linux (x64, arm64) and macOS (arm64).

Vector lines carry the embedding model id (`"model":
"all-MiniLM-L6-v2-onnx-int8"`). Vectors are only comparable with vectors and
centroids of the same model. Lines written by an older SDK with
`"all-MiniLM-L6-v2"` live in a different space and are never scored against
the int8 centroids.

## Upload and the dashboard

Set `TRYAII_API_KEY` to an app key ([`tryaii apps create`](../cli/apps.md))
and the wrapper, on background threads that never touch your calls:

- **uploads** the event lines to `POST {TRYAII_API_URL}/v1/monitor/events`
  — the stored lines, with the prompt hash keyed (below), in batches of at most 500 events / 1 MiB, at
  most one batch every 5 seconds, backing off from 5 seconds to 5 minutes
  while the server or the network fails. `upload.json` next to the events
  records how far it got; local files are never deleted or changed. A batch
  the server rejects as malformed (400) is skipped and noted in
  `upload.json`; a rejected key (401) turns upload off for the process with
  one warning line. At exit it gets one last pass of at most 2 seconds.
- **follows the dashboard**: a long poll of `GET {TRYAII_API_URL}/v1/monitor/config`
  keeps `remote.json` current; the next call uses it (precedence above).
  The first call of a process waits up to 0.5 s, once, for the first
  answer, so even a short script follows the dashboard from its first call.
  A failed poll keeps the last config in force; with no `remote.json` the
  wrapper behaves exactly as without a key.

The key is checked once per process (`GET /v1/apps/keys/validate`), on the
background thread. No key → nothing is uploaded and no config is fetched;
local recording works the same either way. `tryaii monitor status` shows the
state: `source: local | remote etag ... fetched ...` and `upload: off | ok
<n> events at <time> | error <reason>`.

| Variable | Effect |
|---|---|
| `TRYAII_API_KEY` | The app key; enables upload and the dashboard config |
| `TRYAII_API_URL` | API base (default `https://api.tryaii.com`) |
| `TRYAII_MONITOR_UPLOAD` | `0` disables upload (the dashboard config still applies) |
| `TRYAII_TELEMETRY` | `0` disables upload, like `TRYAII_MONITOR_UPLOAD=0` |
| `DO_NOT_TRACK` | `1` disables upload ([consoledonottrack.com](https://consoledonottrack.com)) |
| `TRYAII_MONITOR_REMOTE` | `0` disables the dashboard config (upload continues) |

**Turning upload off.** Any of these stops uploads; records keep being
written locally and nothing already uploaded is affected:
`tryaii monitor upload off` (this machine, `<data dir>/monitor/settings.json`),
the variables above (this process), or the upload switch for your account on
tryaii.com (every machine; the server answers `403 upload_disabled` and the
wrapper waits until it is on again). `tryaii monitor status` then says
`upload: off (<what turned it off>)`. The first upload from a machine prints
the data notice once to the terminal.

### What is uploaded

Each uploaded event is the local event line (`shared/monitor/SPEC.md`
§3.1): `event_id`, timestamp, app, sub-process, call site (file relative to the working directory, never an
absolute path, line, function), provider class, the model requested / resolved / used, mode, the
shadow-routing winner and whether it was reachable, route status, the
prompt's classification (category, subcategory, confidence), the taxonomy
version, token counts (including the prompt-cache usage below), the
estimated costs (at the model used, at the winner, at the anchor), latency,
stream flag, the error status and class,
a keyed hash of the routing text (HMAC-SHA256 of its sha256 with a secret
that never leaves your machine, `<data dir>/monitor/upload-hash.key`, so the
server cannot check a guess against it), a boolean saying whether a vector
was stored, the provider's cached prompt tokens (`usage.cached_tokens`) and,
with `cachelint` on, the `cache` block ([above](#cache-fields): an outcome
and now and then a report whose finding excerpts are masked, never prompt
text). Every batch carries `notice_version` — the version of the data
notice built into this SDK, `tryaii.monitor.NOTICE_VERSION` (currently
`"2026-10-24"`, the date it was published) — and the SDK language and version.
Since contract v1.7 a batch also carries `integrations`, the integrations in
use on this machine (`["langfuse"]` while the
[Langfuse export](#works-with-langfuse) is on; absent otherwise), and the
event's `usage` carries `reasoning_tokens` (below).

**Reasoning tokens** (contract v1.7). `usage.reasoning_tokens` is the part of
`completion_tokens` the model spent on reasoning, read from
`usage.completion_tokens_details.reasoning_tokens`. It is already included in
`completion_tokens`, as the provider counts it, and is never added on top.
`null` (or absent) means the provider reported nothing.

**How long it is kept and how to delete it.** Call records: 90 days. Daily
totals per app: while the app exists. Deleting an app on the dashboard hides
it at once and revokes its keys; it can be restored for 30 days and is then
removed with all its data. Details:
[privacy policy, "Command-line tool and SDK"](https://tryaii.com/privacy-policy#cli-sdk).

**Prompt-cache usage** (contract v1.5, TS-228). The event's `usage` carries
what the provider's prompt cache did for the call, so the dashboard can show
how much of your prompt volume was served from cache and until when a cache
is still hot: `cached_tokens` (input tokens read from the cache),
`cache_write_tokens` (tokens written to it on this call) and `cache_writes`
(the write broken down by lifetime, `[{"tokens", "ttl"}]` with `ttl` one of
`5m`, `1h`, `30m`, `24h` or `explicit`); the event's `cache_ttl_policy` is
the lifetime the request asked for. A field the provider does not report is
`null`, a reported zero is `0`, and `prompt_tokens` stays exactly what the
provider counted (OpenAI, Azure, OpenRouter and Gemini include the cached
tokens in it; Anthropic and Bedrock do not). The fields are plain counts:
no cache content, key or prompt text. The `openai` wrapper fills them from
the response (TS-229): OpenAI and Azure send the read count and, on models
that report it, a write total (`cache_writes` stays `null` because they
give no lifetime breakdown); OpenRouter sends the read and write totals;
an OpenAI-compatible host that answers with Anthropic-shaped usage gets
the 5m/1h breakdown. `cache_ttl_policy` comes from the request: OpenAI
`prompt_cache_retention="24h"`, or the longest explicit
`cache_control.ttl`. `in_memory` retention, and `cache_control` without a
`ttl`, are recorded as `null`. The pure
mapper `normalize_cache_usage(provider, raw_usage)` /
`normalizeCacheUsage` (below) turns a provider's raw usage object into
these three fields, and `build_event` always writes them in contract order.

**Never uploaded:** prompt or response text (no event field holds any, and
the uploader refuses to send any line carrying a field named `prompt`,
`messages`, `response`, `content`, `text` or `raw`; the server rejects such
a batch too), the embedding vectors in `vectors-*.jsonl`, `pins.json`,
`config.json`, your provider API keys, and anything outside
`<data dir>/monitor/<app>/events-*.jsonl`.

## Works with Langfuse

If your app already traces its LLM calls with [Langfuse](https://langfuse.com),
the wrapper can add TryAii's recommendation to each trace: which model to
use, what it would save, and the route status, as Langfuse scores you can
filter and chart. Keep Langfuse's own integration; add `monitor.wrap` to the
same client.

**Python** (Langfuse's drop-in `langfuse.openai`):

```python
from langfuse.openai import OpenAI      # Langfuse's integration
import tryaii.monitor as monitor

client = OpenAI()
monitor.wrap(client, app="shop")
```

With `monitor.install()` instead of `wrap()`, call `install()` **after**
`import langfuse.openai` (or `from langfuse.openai import ...`). If
`install()` runs first, Langfuse's integration wraps TryAii's patch, and each
call gives two traces in Langfuse (Langfuse's and TryAii's) instead of one.
`wrap()` works in either order.

**Node** (`observeOpenAI` from `@langfuse/openai`). The app must register an
OpenTelemetry tracer provider, as in Langfuse's documented setup (for example
`NodeSDK` with `LangfuseSpanProcessor` from `@langfuse/otel`). Without one,
Langfuse's integration records nothing, and the TryAii export makes its own
trace for each call:

```ts
import OpenAI from 'openai';
import { observeOpenAI } from '@langfuse/openai';
import { wrap } from 'tryaii/monitor';

const client = observeOpenAI(new OpenAI());
wrap(client, { app: 'shop' });           // or wrap(client) before observeOpenAI(client)
```

Then turn the export on in the app's environment:

```bash
TRYAII_LANGFUSE=on                       # also 1 or true; off by default
TRYAII_API_KEY=<app key>                 # tryaii apps create
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com   # optional, see below
```

| Variable | Effect |
|---|---|
| `TRYAII_LANGFUSE` | `on`, `1` or `true` (any case) turns the export on (default: off) |
| `TRYAII_API_KEY` | An app key (`tryaii_...`); the export needs it |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Your Langfuse project's keys, the same ones Langfuse's integration reads |
| `LANGFUSE_BASE_URL` | Your Langfuse host; else `LANGFUSE_HOST`; else `https://cloud.langfuse.com` |

It works with Langfuse Cloud (EU: `https://cloud.langfuse.com`, US:
`https://us.cloud.langfuse.com`) and with a self-hosted Langfuse v3.22 or
later (its URL). When the switch is on but the app key or a Langfuse key is
missing, the process logs one warning,
`tryaii monitor: the Langfuse export is off (<reason>)`, and the export stays
off; your calls are never affected.

`tryaii monitor status` prints one line for the machine after the `catalog:`
line, only when `TRYAII_LANGFUSE` is set. It reads the environment of the
shell that runs `status`:

```
langfuse: on (https://cloud.langfuse.com)
langfuse: off (LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are needed)
langfuse: off
```

The second form shows the reason when the switch is on but a key is missing
(`TRYAII_API_KEY is not set`, `TRYAII_API_KEY is not an app key`, or the
Langfuse keys); the third is for any other switch value (for example `off`).

**How it runs.** The export is a second reader of the local event lines, like
the uploader, with its own cursor (`langfuse.json` next to the events). The
cursor starts at the end of the events already stored when the export first
runs (or when the Langfuse host changes): it exports the calls made from then
on, and never older ones. It runs on a background thread, sends in batches, retries when Langfuse is
unreachable, and never raises into your app or sits in the path of an LLM
call. It posts OpenTelemetry (OTLP/HTTP JSON) to
`<host>/api/public/otel/v1/traces` and the scores to
`<host>/api/public/scores`, with your Langfuse keys.

**Where the TryAii span goes.**

Each call gives one trace in Langfuse, and its cost is counted once.

- When an OpenTelemetry span is active around the call (for example a
  Langfuse `@observe` or `startActiveObservation` span), or, in Node, when
  `observeOpenAI` traced the call, the TryAii span is a child of that span in
  the same trace.
- In Python, with `langfuse.openai` and no active span, the wrapper puts a
  TryAii span around the call and Langfuse's generation becomes its child:
  the TryAii span is the trace root.
- In these cases the TryAii observation is a plain span, without a model or
  cost, because Langfuse's own generation already has them.
- Without any of these, the export creates the trace itself (its id derived
  from the event id) with a generation that carries the model, the token
  usage and TryAii's cost estimate.

The span carries the event's metadata and a link to the app's page on your
tryaii.com dashboard.

**Scores.** After the span is accepted, the export adds up to three scores to
its trace, each only when the event has the value:

| Score | Type | Meaning |
|---|---|---|
| `tryaii.recommended_model` | categorical | The model TryAii recommends for this call (the shadow-routing winner), or the model in your code when it should stay |
| `tryaii.est_savings_usd` | numeric | Estimated saving in USD at the recommended model: the call's estimated cost minus its estimated cost at the winner (catalog pricing), rounded to 6 decimals |
| `tryaii.route_status` | categorical | The event's route status (`ok`, `unavailable`, `timeout`, `skipped`, ...) |

Each score has a fixed id per event and name, so a retry never adds a
duplicate.

**What is sent, and where.** Metadata only, the same kind of fields as the
[upload](#what-is-uploaded): never prompt or response text. The data goes
from your app straight to your own Langfuse, never through TryAii. The upload
to TryAii is unchanged, except that while the export is on its batches carry
`"integrations": ["langfuse"]`, which names the integration in use and
nothing else.

## The pure engine

The rules behind the wrapper are plain functions in both SDKs, clock-free
and routing-free, frozen by the fixtures in `shared/monitor/fixtures/` and
specified in `shared/monitor/SPEC.md`:

| Python (`tryaii.monitor`) | Node (`tryaii/monitor`) | What |
|---|---|---|
| `routing_text(messages)` | `routingText` | The text that is routed (system + last user message) and its sha256 |
| `provider_class`, `resolve_anchor`, `reachable`, `allowed_models`, `native_model_id` | `providerClass`, `resolveAnchor`, `reachable`, `allowedModels`, `nativeModelId` | Provider class from `base_url`, anchor resolution, what this client can send |
| `pick_winner(scores, anchor_id, registry, allowed=None)`, `routed_model(...)`, `managed_target(...)` | `pickWinner`, `routedModel`, `managedTarget` | Best-for-less winner (policy-filtered); the model actually sent per mode; the managed decision |
| `policy_allowed`, `route_allowed`, `shadow_allowed`, `filter_ranking` | `policyAllowed`, `routeAllowed`, `shadowAllowed`, `filterRanking` | The policy's allowed ids and the candidate lists of the in-path and shadow route requests |
| `evaluate_pin`, `pin_from_first_call`, `set_pin` | `evaluatePin`, `pinFromFirstCall`, `setPin` | The 24 h pin rule |
| `resolve_mode`, `resolve_app`, `resolve_process`, `set_process_mode` | `resolveMode`, `resolveApp`, `resolveProcess`, `setProcessMode` | Config precedence |
| `aggregate(app, events, pins, config, now, since, env, remote, upload_cursor, uploaded_events)` | `aggregate` | The `status` document |
| `build_event`, `build_vector`, `estimate_cost`, `format_ts`, `parse_ts` | `buildEvent`, `buildVector`, `estimateCost`, `formatTs`, `parseTs` | Event and vector lines, cost from catalog pricing |
| `tryaii.monitor.cache`: `cached_tokens_from_usage`, `mask_excerpt`, `lint_prompt`, `recommendation_from`, `build_report`, `CacheTracker` | `cachedTokensFromUsage`, `maskExcerpt`, `lintPrompt`, `recommendationFrom`, `buildReport`, `CacheTracker` | The cache fields: cached tokens, the excerpt mask, the report and the per-sub-process outcome and report cadence |
| `normalize_cache_usage(provider, raw_usage)`, `normalize_usage` | `normalizeCacheUsage`, `normalizeUsage` | The provider field map for prompt-cache usage (`openai`, `openai_responses`, `openrouter`, `azure`, `anthropic`, `bedrock`, `gemini` shapes → `cached_tokens`, `cache_write_tokens`, `cache_writes`), and the event's `usage` in contract order |
| `tryaii.monitor.store` | `store` namespace | Read/append the daily files, pins, config, `remote.json` and `upload.json` |
| `tryaii.monitor.upload.Uploader`, `tryaii.monitor.remote.RemotePoller` | `Uploader`, `RemotePoller` | The background uploader and the dashboard long poll (`run_once()` / `poll_once()` for tests) |

Event lines are minified JSON with a fixed key order; both SDKs write
byte-identical lines for the same inputs (the Python store prints numbers the
way `JSON.stringify` does).

## Testing a wrapped client

`wrap(client, route_fn=fn)` replaces the daemon with `fn(text) -> (scores,
classification, embedding_or_None)` and runs the whole pipeline
synchronously (`inline=True` is implied), so a duck-typed fake client is
enough to assert the model swap, the written event and the pin. Set
`TRYAII_DRE_DATA_DIR` to a temporary directory to keep test records out of
`~/.tryaii`. Wrappers built with `route_fn` never start the uploader or the
dashboard poll, even with `TRYAII_API_KEY` set. For HTTP, the helper honours
`TRYAII_HTTP_FIXTURE` (a JSON file of canned answers, `shared/monitor/SPEC.md`
§6.3) — test-only; never set it in production.
