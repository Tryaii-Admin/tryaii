# monitor (SDK surface)

`tryaii.monitor` (Python) and the `tryaii/monitor` subpath (Node) wrap the
OpenAI SDK client your application already uses. Each call is recorded
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
| `client` | — | An `openai.OpenAI` or `openai.AsyncOpenAI` (anything with `base_url` and `chat.completions.create`). Patched **in place**: `client.chat.completions.create` becomes the monitored function. |
| `app` | `TRYAII_APP`, else the working directory's name | Where records go: `<data dir>/monitor/<app>/` |
| `process` | `None` | Default sub-process name for this client (below the per-call tag and `process()` block) |
| `mode` | `None` | `off`, `stable`, `dynamic` or `managed` for this client, below `TRYAII_MONITOR_MODE` and the dashboard config, above `config.json` (`managed` here has no model, so the model in your code is sent — assign the model in `config.json` or on the dashboard) |
| `cachelint` | `False` | Also run the [runtime cache lint](client/cache-lint.md) on every call (warn-only). Python only; the Node wrapper accepts the option but does not lint yet |
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

## `install(...)`

**Python**

```python
import openai
import tryaii.monitor as monitor

monitor.install(app="shop", mode="off")   # True when the openai classes were patched
```

Sets the module defaults (`app`, `process`, `mode`, `cachelint`) and patches
`openai.resources.chat.completions.Completions.create` and
`AsyncCompletions.create` at class level, so every client created in this
process is monitored. Returns `False` — with the defaults still set — when
`openai` has not been imported yet: import it first, then call `install()`.

**Node**: `install({ app, process, mode })` sets the defaults only and
returns nothing -- ES-module `openai` classes cannot be patched in place, so
wrap each client with `wrap()`.

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

## Upload and the dashboard

Set `TRYAII_API_KEY` to an app key ([`tryaii apps create`](../cli/apps.md))
and the wrapper, on background threads that never touch your calls:

- **uploads** the event lines to `POST {TRYAII_API_URL}/v1/monitor/events`
  — the stored lines verbatim, in batches of at most 500 events / 1 MiB, at
  most one batch every 5 seconds, backing off from 5 seconds to 5 minutes
  while the server or the network fails. `upload.json` next to the events
  records how far it got; local files are never deleted or changed. A batch
  the server rejects as malformed (400) is skipped and noted in
  `upload.json`; a rejected key (401) turns upload off for the process with
  one warning line. At exit it gets one last pass of at most 2 seconds.
- **follows the dashboard**: a long poll of `GET {TRYAII_API_URL}/v1/monitor/config`
  keeps `remote.json` current; the next call uses it (precedence above).
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
| `TRYAII_MONITOR_REMOTE` | `0` disables the dashboard config (upload continues) |

### What is uploaded

Each uploaded event is the local event line (`shared/monitor/SPEC.md`
§3.1): `event_id`, timestamp, app, sub-process, call site (file relative to the working directory, never an
absolute path, line, function), provider class, the model requested / resolved / used, mode, the
shadow-routing winner and whether it was reachable, route status, the
prompt's classification (category, subcategory, confidence), the taxonomy
version, token counts, the estimated costs (at the model used, at the
winner, at the anchor), latency, stream flag, the error status and class,
the sha256 of the routing text, and a boolean saying whether a vector was
stored. Every batch carries `notice_version` — the version of the data
notice you accepted by setting the key, the constant
`tryaii.monitor.NOTICE_VERSION` (currently `"2026-10"`) — and the SDK
language and version.

**Never uploaded:** prompt or response text (no event field holds any, and
the uploader refuses to send any line carrying a field named `prompt`,
`messages`, `response`, `content`, `text` or `raw`; the server rejects such
a batch too), the embedding vectors in `vectors-*.jsonl`, `pins.json`,
`config.json`, your provider API keys, and anything outside
`<data dir>/monitor/<app>/events-*.jsonl`.

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
