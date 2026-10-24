# Changelog

## 0.8.2 (2026-10-24)

- **Monitor: cache writes and lifetimes on every event (TS-229, contract
  `shared/monitor/API.md` v1.5).** The OpenAI wrapper (OpenAI, Azure
  OpenAI, OpenRouter and other OpenAI-compatible hosts), in both SDKs, now
  fills `usage.cache_write_tokens` and `usage.cache_writes` from the
  provider's response, using `normalize_cache_usage` / `normalizeCacheUsage`.
  It also fills `cache_ttl_policy` from the request: `prompt_cache_retention`
  `24h`, or the longest explicit `cache_control.ttl`. Only reported values
  are recorded. A field the provider does not send stays `null`, never `0`.
  `in_memory` retention and a `cache_control` without a `ttl` record `null`.
  OpenRouter's top-level `cached_tokens` is read now. A compatible host that
  answers with Anthropic-shaped usage gets its 5m/1h write breakdown. New
  helpers: `usage_from_provider` / `usageFromProvider` and
  `cache_ttl_policy_from_request` / `cacheTtlPolicyFromRequest`.

- **Monitor: Anthropic SDK wrapper** (TS-169). `tryaii.monitor.wrap(client)`
  (Python) and `wrap(client)` from `tryaii/monitor` (Node) now also take an
  `anthropic` / `@anthropic-ai/sdk` client (`Anthropic`, `AsyncAnthropic`,
  `AnthropicBedrock`, `AnthropicVertex`) and monitor `messages.create`
  (sync, async, `stream=True`) and `messages.stream()` with the same
  call-site capture, sub-process naming, modes, privacy and upload rules as
  the openai wrapper. Python `install()` also patches the anthropic
  `Messages` classes once `anthropic` is imported (`install_anthropic()`
  alone). The event's `usage` follows contract v1.5: `input_tokens` /
  `output_tokens` as `prompt_tokens` / `completion_tokens` (input excludes
  cache), and `cache_read_input_tokens`, `cache_creation_input_tokens` and
  the `cache_creation` 5m / 1h breakdown as `cached_tokens`,
  `cache_write_tokens` and `cache_writes`; only what the response reports.
  Events carry `provider: "anthropic"` (Bedrock and Vertex clients too), so
  the server counts cached and written tokens in the hit rate's input;
  routing treats it like class `other`: monitored and shadow-routed, never
  swapped. `cache_ttl_policy` is the longest explicit `cache_control.ttl`
  of the request. Bedrock model ids resolve to their catalog
  anchor. New optional extra `tryaii[anthropic]`.

- **Monitor: works with Langfuse, TryAii's recommendation as Langfuse scores**
  (TS-245, TS-246, TS-249; contract `shared/monitor/API.md` v1.7). Set
  `TRYAII_LANGFUSE=on` (also `1`, `true`; off by default) next to
  `TRYAII_API_KEY`, `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, and the
  wrapper exports each call to your own Langfuse (`LANGFUSE_BASE_URL`, else
  `LANGFUSE_HOST`, else `https://cloud.langfuse.com`; Langfuse Cloud EU and
  US and self-hosted v3.22+). The export is a second reader of the local
  event lines with its own cursor (`langfuse.json`, which starts at the end
  of the events already stored, so older calls are not exported):
  background, batched, retried, never raising into the app and never in the
  LLM call path. It posts OTLP/HTTP JSON to `/api/public/otel/v1/traces`.
  Each call gives one trace. With an active OpenTelemetry span (for example
  Langfuse's `@observe`) or, in Node, `observeOpenAI`, the TryAii span is a
  child of that span; in Python with `langfuse.openai` and no active span,
  the wrapper makes the TryAii span the trace root and Langfuse's generation
  its child. In both cases it is typed `span` with no model cost, so the
  call is not counted twice. Otherwise the export creates the trace itself
  (ids derived from `event_id`) with a `generation` carrying the model,
  usage and cost estimate. Known limits: in Python, call
  `monitor.install()` after `import langfuse.openai` (or use `wrap()`),
  else a call gives two traces; in Node, the app must register an
  OpenTelemetry tracer provider, as in Langfuse's documented setup. After
  the span it adds up to three scores to the trace: `tryaii.recommended_model` (the winner, or the
  anchor when it should stay), `tryaii.est_savings_usd` (`est_cost_usd -
  est_cost_winner_usd`) and `tryaii.route_status`, each with a fixed id per
  event so a retry never duplicates. Metadata only, never prompt or response
  text; the data goes from your app straight to your Langfuse, never through
  TryAii. A missing app key or Langfuse key logs one warning and leaves the
  export off. When `TRYAII_LANGFUSE` is set, `tryaii monitor status` ends
  with `langfuse: on (<host>)`, `langfuse: off (<reason>)` or
  `langfuse: off`. New in the event (v1.7): `usage.reasoning_tokens`, from
  `usage.completion_tokens_details.reasoning_tokens` (already part of
  `completion_tokens`, never added on top; `null` when not reported), and
  `otel`, the span the call belongs to (`origin: "tryaii"` when the
  wrapper made it; ignored by the server). Upload batches carry
  `"integrations": ["langfuse"]` while the export is on, that is the switch,
  an app key and both Langfuse keys (absent otherwise). Python and Node, with the new fixture
  suite `shared/monitor/fixtures/langfuse`. Docs: "Works with Langfuse" in
  `docs/sdk/monitor.md` and `docs/cli/monitor.md`; PRIVACY.md gains the
  Langfuse line.

## 0.8.1 (2026-10-24)

- **Catalog: AA-LCR, AA-Omniscience and Terminal-bench-Hard removed**
  (Artificial Analysis licensing). The benchmark taxonomy, normalization
  ranges, training queries and both embedding models' centroids drop the three
  (centroid rows removed, no re-embedding); the full build data goes from 33 to
  30 benchmarks and from 362 to 358 models (319 routable): four models scored
  only on these benchmarks and leave the catalog
  (`inception/mercury-2.5-preview`, `x-ai/grok-build-0.1`,
  `google/gemini-3.1-flash-lite`, `apodex/apodex-1.1-mini:free`); no starter
  model loses all its scores. `scripts/build_cache_shared.py` always excludes
  the three (`EXCLUDED_BENCHMARKS`), whatever the allowlist says, and the new
  `scripts/fetch_snapshot.py` fetches the bench snapshot it reads. Parity,
  diagnose and monitor pin fixtures regenerated.
- **Starter catalog 2026.10.23.3: Claude Opus 5 and SWE-bench-Pro.** The
  packaged starter catalog now has 46 models and 15 benchmarks.
  `anthropic/claude-opus-5` joins it (Claude Code users still run it, and
  `tryaii claude-code report` now prices it from the catalog instead of the
  list-price fallback). SWE-bench-Pro is kept as the starter's agentic-coding
  benchmark although fewer than 60% of the starter models report it (a
  documented `coverage_exceptions` entry in `shared/catalog/starter-selection.json`,
  checked by `scripts/build-catalog-bundles.py`), so coding prompts still
  classify as code now that Terminal-bench-Hard is gone. Starter training
  queries: 217.
- **`tryaii claude-code report` shows usage by day** (TS-287). A "By day
  (UTC)" section lists every UTC date in the window with calls, cache hit
  share and estimated cost (days without calls as 0); `--json` adds a `days`
  array with the same rows plus unpriced, main and subagent call counts.
  Counts and costs only, never message text.
- **An app key alone unlocks the full model catalog** (TS-294, catalog
  contract v1.4). On a server, set only `TRYAII_API_KEY`: when no
  `tryaii login` session is stored, both SDKs download and refresh the full
  signed catalog (319 models instead of the 46-model starter) from
  `GET /v1/catalog/live` with the key, against `TRYAII_API_URL`. A stored
  login still wins when present (unchanged behaviour for logged-in
  machines). A rejected key (revoked, expired, deleted app) falls back to the
  starter catalog without raising, and the CLI prints
  `TRYAII_API_KEY was not accepted for the full catalog; using the starter catalog.`
  `catalog/state.json` gains `"auth": "app_key"` after a key's check.
  `tryaii monitor status` ends with `catalog: full (app key)`,
  `catalog: full (login)` or `catalog: starter`. Needs the API change that
  accepts app keys on the catalog endpoint (tryaii-backend-servers, same
  ticket); against an older API a key is rejected and the starter catalog
  is used, as before.
- **Monitor: stable mode pins the same model the dashboard names (TS-295).**
  When two models won a sub-process equally often over the pin window, the
  pin used to go to the model id that sorts first, while the dashboard
  recommended the model with the better benchmark score and price, so
  "Pin X · stable" could pin Y. Both now use one rule: most wins, then the
  higher catalog benchmark score, then the lower blended price, then the
  model id (SPEC §2.5), and the server's summary reports that pick as
  `pin_candidate`. The pin's `reason` says when a tie was broken and by what.
  Python and Node, with shared fixtures for ties.
- **Monitor: dashboard mode changes apply on the next call, and from the
  first call** (TS-296). A `remote.json` the long poll just wrote was only
  noticed on the wrapper's next 5 s re-check, so calls in the first seconds
  after a fetch still used the old mode; the poll's own writes now apply on
  the very next call (an in-process write counter, independent of mtime
  resolution). The first poll of a process asks with `wait=0`, and the first
  call of a process waits for that answer at most 500 ms, once, so a short
  script honours the dashboard from its first call; a slow or unreachable
  server costs at most 500 ms once and falls back to the cached/default
  mode. Both SDKs (SPEC.md §3.3).
- **New (Python only): `tryaii claude-code`, your own Claude Code usage**
  (TS-286; CLI, 30-day log import and report: TS-287).
  `report` reads the session logs Claude Code keeps on the machine
  (`~/.claude/projects`) and shows calls, tokens, the cache hit share and the
  cost split (input, cache write 5m / 1h, cache read, output) per model, main
  conversation vs subagents, per subagent type and per project, plus where a
  cheaper model may fit (subagents with short answers -> `model: haiku`, with
  the upper-bound saving, measured per subagent run). Costs are an estimate at
  API list prices from the TryAii catalog, with Anthropic's cache-read rates
  per model and list prices for Claude models the catalog lacks (marked
  "list price (fallback)") until the catalog carries them; any other model the
  catalog does not price is shown as unpriced.
  `connect` stores each call as a monitor event of the app `claude-code` and
  uploads it with the monitor's uploader (`TRYAII_API_KEY`); re-running adds
  only new calls, `--dry-run` writes nothing. `disconnect` removes what
  `connect` stored. Only model ids, token counts, times, the subagent type and
  the project folder's base name are read: never message text, tool inputs or
  outputs, or file contents. Not in the Node CLI yet (a documented parity
  exception, `shared/monitor/SPEC.md` §10). Docs: `docs/cli/claude-code.md`.
  `analyze` (opt-in, TS-288) classifies your Claude Code tasks on the machine:
  it reads the text of your own messages and of each subagent's instruction
  (after asking; secrets are skipped), embeds it with the local embedding
  model, and says per process (main, each subagent type, weighted by cost)
  which Anthropic model is good enough and what switching would save. Main-
  thread advice is marked low confidence. The text is never stored or sent;
  `connect` uploads only the task profile and the recommendation.
  Live telemetry (TS-291, v1.5, SPEC §10.8): with a separate yes, `connect` adds
  Claude Code's OpenTelemetry settings to `~/.claude/settings.json` (backup
  first, every other setting kept, another tool's telemetry never
  overwritten) so each new API call is reported to
  `/v1/otlp/claude-code/v1/logs`: model, tokens, cost, duration, time (the
  request id only builds the de-duplication id and is not stored);
  never prompts, code or tool inputs. `--no-live` / `--live-only` pick a part;
  `otel-headers` is the headers helper, `status` shows what is connected and
  warns when a Claude Code content switch is on, `disconnect` removes exactly
  the added keys. Event ids now derive from the request id (shared with the
  server), and after live telemetry is on the import stops at its cutoff.
  Live telemetry sends logs only (metrics export stays off), to the OTLP
  endpoint on `api.tryaii.com`, and needs the app key in `TRYAII_API_KEY`.
- **Fix (Node): `wrap()` on a Langfuse `observeOpenAI` client no longer
  produces two Langfuse traces per call.** `wrap(observeOpenAI(client))`
  sent one request but Langfuse traced it twice, because `wrap()` read
  `create` through Langfuse's proxy (getting Langfuse's tracing closure) and
  the proxy then wrapped TryAii's `create` again. `wrap()` now recognises the
  proxy and patches the real `chat.completions` behind it, so both orders
  give one request, one Langfuse generation and one TryAii event per call,
  streams included. Every other client (plain, Azure, OpenAI-compatible) is
  patched exactly as before. Python already handled both orders.
- **Claude Code: cold cache restarts, sessions and live advice (Python only,
  TS-286 / TS-297, API.md v1.6).**
  `report` gains a **cold cache restarts** section: main-conversation calls
  after a pause of more than 5 minutes that wrote the whole context to the
  prompt cache again, with the cost of that rewrite above reading the cache
  (at API list prices), by reason (expired, model switch, other), per session
  and the largest few. Sessions are grouped by their log file on the machine
  and named only "Session N"; the session id is never printed, stored or
  uploaded. Imported events carry the result as the optional field
  `cache_cold` (true / false for main-thread calls, null for subagents). The
  rule is pinned by the shared fixture
  `shared/monitor/fixtures/claude-code/cold-restarts.json`, which the server
  also runs for live calls.
  `connect --no-session` adds `OTEL_METRICS_INCLUDE_SESSION_ID=false`, so
  Claude Code sends no session id and the server keeps no session reference
  (works on an existing setup: `connect --live-only --no-session`;
  `disconnect` removes it); `status` shows whether the session id is sent.
  New verb `sync [--background] [--quiet] [--days N]`: for calls that came
  by live telemetry it sends the project folder name, the subagent type and,
  after `analyze`, the task type and recommended model, plus the per-process
  advice summary (never text) to `POST /v1/monitor/claude-code/annotations`.
  The Claude Code plugin (0.2.0) runs it from a `SessionEnd` hook in the
  background; the hook always exits 0.
- **Data notice 2026-10-24.** `NOTICE_VERSION` is now `2026-10-24` in both
  SDKs (sent as `notice_version` with every batch and by Claude Code's headers
  helper), because Claude Code's live telemetry now also keeps the effort level
  and a session reference: a keyed hash of Claude Code's session id made on
  TryAii's server (per-app key, 64 bits), used only for the cache timer and
  removed after 7 days; the raw session id is never stored. `PRIVACY.md`
  has a new Claude Code section. The first upload after updating prints the
  notice again (it is recorded per version).

## 0.8.0 (2026-10-21)

Breaking: the default embedding model changes. Vectors and classifications
recorded before this release are in the old model's space and are never
mixed with new ones. Setting `TRYAII_DRE_EMBEDDING_MODEL=all-MiniLM-L6-v2`
keeps the old model, but it now needs `pip install "tryaii[centroids]"`.

- **Monitor: cache fields on every event (contract `shared/monitor/API.md`
  v1.4).** `usage.cached_tokens` records the prompt tokens the provider served
  from its cache (`prompt_tokens_details.cached_tokens`, or
  `cache_read_input_tokens` in Anthropic-shaped usage; `null` when not
  reported), in both SDKs, with no option needed. With
  `wrap(client, cachelint=True)` each event also ends with a `cache` block:
  the predicted outcome from the previous call of the same sub-process
  (`hit` / `partial` / `miss` / `null`) and, on a sub-process's first call
  and then when it changed, the lint report (verdict, token counts, up to 10
  findings, one recommendation). A full analysis runs at most once per
  process every 60 s. The lint covers the tool schemas (`tools` /
  `functions`) as well as the messages. Findings never quote the prompt:
  only a strict ASCII mask of the matched text is written, locally and in
  the upload (ASCII digits -> `0`, whitespace -> space, printable ASCII
  punctuation kept, every other code point -> `x`, cut to 40 code points
  with `…`; `{{sku}}` -> `{{xxx}}`). Node now honours `cachelint` for the
  `cache` block (the stderr lint lines stay Python-only). New fixture suite
  `shared/monitor/fixtures/cache`.

- **No more PyTorch: local embeddings run on an int8 ONNX build** (TS-252).
  The default embedding model is now `all-MiniLM-L6-v2-onnx-int8`: the same
  all-MiniLM-L6-v2, exported to ONNX with int8 weights (weight-only: the
  arithmetic stays fp32, so every CPU computes the same vectors) and run on
  `onnxruntime` + `tokenizers`. `sentence-transformers` and `torch` are no
  longer installed with `pip install tryaii`; they moved to the optional extra
  `tryaii[centroids]`, needed only to build fp32 centroids or to run another
  sentence-transformers model. Measured with a fresh venv, CPU only, median
  of 3, model weights included in the size (the 0.7.1 model adds 90 MB to
  the Hugging Face cache on first use):

  | | Windows 11, Python 3.13: 0.7.1 | ONNX int8 | Linux (python:3.12-slim): 0.7.1 | ONNX int8 |
  |---|---|---|---|---|
  | `pip install` size (site-packages) | 1052 MB | 186 MB (-82%) | 6069 MB (CUDA PyTorch) | 228 MB (-96%) |
  | `pip install` time | 2 min 9 s | 20 s | 4 min 8 s | 13 s |
  | first classified call through the daemon (cold start) | 10.6 s | 1.3 s | 6.6 s | 0.9 s |
  | daemon memory (RSS) | 551 MB | 182 MB | 902 MB | 255 MB |

  On Linux a plain `pip install tryaii` 0.7.1 pulled the CUDA build of
  PyTorch (torch, triton and 15 `nvidia-*` packages) even on machines without
  a GPU.
- **The model ships as a package, nothing is downloaded at run time.** The
  23 MB model file is the new package `tryaii-minilm-int8`, which `tryaii`
  depends on (pinned exactly). It comes through your usual index, mirror or
  cache like any other dependency, so locked-down machines need no extra
  network access. The SDK uses the file only when its size and sha256 match
  the values pinned in the SDK. On a machine that cannot install the package,
  set `TRYAII_EMBEDDING_MODEL_PATH` to a copy of the file (or its directory).
- **A missing model fails open, quietly.** Without a verified file the
  monitor works as it does without a daemon: metadata-only events, and the
  model in your code is sent. It logs one warning per machine per day (not one
  per process start), and `tryaii monitor status` ends with
  `classification: off since <time> (<reason>)` until it is fixed.
- **Node: the monitor embeds in-process, no Python and no daemon** (TS-253).
  `tryaii/monitor` runs the same int8 model file, tokenizer, pooling and
  centroids as Python on `onnxruntime-node`, so both SDKs classify a prompt
  the same way. On the parity prompts the category is identical and every
  similarity agrees to 4 decimals. Install the optional peer dependencies to
  enable it: `npm install onnxruntime-node tryaii-minilm-int8` (the npm twin
  of the PyPI model package). Without them the wrapper uses the daemon as
  before, for this release; with neither, it records metadata only and warns
  once a day. Measured with a fresh project, Node 22, CPU, median of 3:

  | | Windows 11: 0.7.1 (daemon, @xenova/transformers) | Windows: in-process ONNX int8 | Linux (node:22-slim): 0.7.1 | Linux: in-process ONNX int8 |
  |---|---|---|---|---|
  | node_modules after `npm install` | 336 MB | 639 MB | 304 MB | 606 MB |
  | first classified call (cold start) | 0.64 s | 0.48 s | 0.59 s | 0.54 s |
  | memory of the embedding process | 194 MB (daemon) | 210 MB (your process) | 217 MB (daemon) | 290 MB (your process) |

  A little faster, a little more memory. The gain is no daemon and
  no second engine: Node now classifies exactly like Python, where 0.7.1 ran
  a different quantised MiniLM and scored it against the fp32 centroids.
  The monitor install is larger: `onnxruntime-node` 1.30 ships native
  binaries for every platform (301 MB), on top of the 23 MB model, while
  `@xenova/transformers` is still installed for `Router`. On Linux, install
  with `ONNXRUNTIME_NODE_INSTALL=skip` (as measured above): without it
  `onnxruntime-node` also downloads its CUDA libraries (574 MB instead of
  301 MB). `npm install tryaii` without the two peers is unchanged (337 MB on
  Windows, 304 MB on Linux). macOS has not been measured.

- **Centroids per embedding model.** A catalog bundle may now carry
  `centroids.<model id>.json` next to `centroids.json` (catalog contract
  v1.3); the starter bundle `2026.10.20.2` carries the int8 centroids next to
  the fp32 ones. A vector is only scored with its own model's centroids, and
  monitor vector lines carry the model id (`"model":
  "all-MiniLM-L6-v2-onnx-int8"`). Older SDKs ignore the new file.
- **Classification parity.** Against the fp32 build, the int8 build gives the
  same top benchmark and category on every parity prompt (the monitor fixture
  corpus and the catalog parity set) and routes all 39 to the same model.
  Embeddings agree to cosine 0.999 or better.
- New maintainer scripts: `scripts/export-onnx-embedding.py` (reproducible
  export + quantisation, writes `packages/python-model`),
  `scripts/gen-centroids.py`, `scripts/gen-embedding-parity.py`.
- **Privacy: see and control what the monitor sends.** New
  `tryaii monitor upload [on|off]`: prints the data notice (what is sent to
  your tryaii.com account, what never is) and switches uploads for this
  machine. `TRYAII_TELEMETRY=0` and `DO_NOT_TRACK=1` now turn uploads off too,
  next to `TRYAII_MONITOR_UPLOAD=0`, and so does the new upload switch for your
  account on tryaii.com (`"upload": false` in the dashboard config, or
  `403 upload_disabled`). `tryaii monitor status` says what turned uploads off.
- **The data notice is shown before data is sent.** The first upload from a
  machine prints the notice once to the terminal, and `tryaii apps create`
  prints it after the key and records on your account that it was shown.
  Notice version `2026-10-18` (the publication date) replaces `2026-10`.
- **The uploaded prompt hash is keyed.** It is an HMAC of the local sha256
  with a secret that never leaves your machine
  (`<data dir>/monitor/upload-hash.key`), so the server cannot check a guess
  of a short prompt against it. Local files are unchanged.
- New: `PRIVACY.md`, a "Data and privacy" section in the package READMEs and
  a `Privacy` link in the PyPI metadata. Contract `shared/monitor/API.md`
  v1.3, `SPEC.md` §9.
- Fixtures: `shared/monitor/fixtures` and `shared/cachelint/fixtures` are
  checked out with LF on every OS (`.gitattributes`), so the fixture check
  also passes on Windows.
- **Fix (Python): `monitor.wrap()` on an `AsyncOpenAI` client.** The real
  `AsyncCompletions.create` sits behind a sync decorator, so the wrapper took
  the client for a sync one: `await create(..., stream=True)` raised
  `TypeError: object MonitoredStream can't be used in 'await' expression`, and
  non-streaming calls were recorded with `usage: null` and `latency_ms: 0`.
  The async check now looks through `__wrapped__` (also for `install()`).
- **Fix (Python): tools that patch the `openai` classes after `wrap()` now
  run.** `wrap(client)` looks up the class's `create` on every call instead
  of keeping the method it saw at wrap time, so a later class-level patch
  (e.g. `import langfuse.openai`) is no longer bypassed. A `create` already
  set on the client instance before `wrap()` is kept as before.
- monitor: events record prompt-cache usage (`cached_tokens`,
  `cache_write_tokens`, `cache_writes[].ttl`, `cache_ttl_policy`); contract
  `shared/monitor/API.md` v1.5, `SPEC.md` §3.1b (TS-228). On top of the
  v1.4 `usage.cached_tokens` read count, both SDKs now write the cache
  **write** side in contract order (`cache_write_tokens`, the per-lifetime
  `cache_writes`, and the lifetime the request asked for; `null` until the
  wrappers capture them in TS-229), `tryaii monitor events` shows
  `cached <n>` / `cache write <n>` when present, `status --json` sums them,
  and the new pure helper `normalize_cache_usage` / `normalizeCacheUsage`
  maps the OpenAI (Chat and Responses), OpenRouter, Azure, Anthropic,
  Bedrock Converse and Gemini usage shapes to the three usage cache fields
  (fixture suite `cache_usage`, byte-identical across SDKs).

## 0.7.1 (2026-10-14)

- **Privacy: the call site never names your machine** (TS-226, TS-225):
  `call_site.file` is now recorded relative to the application's working
  directory with `/` separators (base name only for a file outside it), and
  the uploader replaces any absolute path left in an older record with the
  base name before sending. Before, the full local path (including the OS
  user name) was uploaded and shown on the dashboard. Contract
  `shared/monitor/API.md` v1.2.

## 0.7.0 (2026-10-14)

### Highlights

- **Policies: control who builds your models and who sees your prompts.** New
  `tryaii policy` command. Save your company's rules once and every routing decision
  (`route`, `eval`, `models` and the SDK) stays inside them:
  - `--origin no-prc-models`: only models whose maker, and the maker of their base
    model, is not controlled from the People's Republic of China. A fine-tune of a
    Chinese model counts as Chinese.
  - `--origin no-prc-hosting`: any model, but prompts only go to hosts not controlled
    from the PRC.
  - `--origin us-eu`: models and hosts from the US, the EEA, the UK and Switzerland
    only.
  - `--providers anthropic,openai` / `--exclude-providers …`: only (or never) these
    companies, as model maker and as host.
  - `--allow-countries` / `--deny-countries`: by the country that controls the company.
  - `--platform bedrock|azure|vertex`: only models that cloud platform offers.
- **Hosts are enforced, not just filtered.** When a policy limits hosts, each OpenRouter
  request lists exactly the hosts it may use (`provider.only`), so a prompt never reaches
  an excluded host.
- **Unknown means excluded.** A model or host whose origin tryaii cannot vouch for is
  never allowed while a policy is active, and a policy never silently falls back to
  "all models".
- **Monitor: see what your LLM calls cost and what they could cost.** New
  `tryaii monitor` and `tryaii apps` commands. `tryaii.monitor.wrap(client)`
  (Python) / `wrap(client)` from `tryaii/monitor` (Node) patches the `openai`
  client your app already uses. Every call is recorded locally (metadata only,
  never prompt or response text) and shadow-routed to the best-for-less model.
  Per sub-process modes `off`, `stable`, `dynamic` and `managed`; with an app
  key from the dashboard on tryaii.com, call metadata is uploaded and the
  dashboard assigns modes and models to the running app.

### `tryaii monitor` — dashboard upload, remote modes, managed mode (both SDKs)

- **Upload** (opt-in): with `TRYAII_API_KEY` set to an app key, the wrapper
  uploads its event lines (metadata only — never prompt or response text,
  never embeddings) in the background to `api.tryaii.com`: batches of at most
  500 events / 1 MiB, at most every 5 s, exponential backoff (5 s → 5 min),
  a cursor in `upload.json`, local files never touched. Each batch carries
  `notice_version` (`tryaii.monitor.NOTICE_VERSION = "2026-10"`). A line with
  a field named `prompt`, `messages`, `response`, `content`, `text` or `raw`
  is never sent. `TRYAII_MONITOR_UPLOAD=0` disables it.
- **Dashboard config**: a background long poll keeps `remote.json` current;
  mode precedence is now env > dashboard > `wrap(mode=)` > `config.json` >
  `off`. `TRYAII_MONITOR_REMOTE=0` disables it.
- **`managed` mode**: a sub-process always sends an assigned model (from the
  dashboard or `tryaii monitor set <process> managed --model <id>`) when the
  client can reach it and the policy allows it; otherwise the model in your
  code, recorded as `route_status: "unreachable_managed"`.
- **No lost records at exit** (TS-224): a process that exits while the
  daemon is still warming up now records its pending calls as
  `route_status: "unavailable"` (and uploads them) instead of dropping them.
- **Rejected key is visible in `status`**: a key rejected at start-up now
  writes `upload: error invalid_key` for the app, instead of leaving the last
  `upload: ok` line in place.
- **Data-save policy in the wrapper** (TS-170): the saved policy is applied
  once per process; a disallowed model is never recommended or sent.
  `dynamic` now sends the best-for-less model among the models the client can
  reach, so an unreachable recommendation no longer means "keep the anchor".
- **Events** gain `event_id` (uuid4), `taxonomy_version` (null until the
  catalog carries one) and `est_cost_anchor_usd` (the call priced at the
  model in your code, so realised savings are computable).
- **`tryaii monitor status`** shows `managed (<model>)`, a `realised:` line
  (swapped calls and what they saved), `source: local | remote ...` and
  `upload: off | ok ... | error ...`; the JSON document gains `source`,
  `upload`, `managed_model`, `swapped_calls`, `est_cost_anchor_usd` and
  `realised_savings_usd`.
- **New command `tryaii apps`**: `create <name>` (prints the full key once),
  `list`, `keys <app_id>` (`--new [--label L]`, `--revoke <key_id>`),
  `check [--key K]` ([docs](docs/cli/apps.md)).
- Contract: `shared/monitor/API.md` (v1.1) and `shared/monitor/SPEC.md` v2;
  fixtures regenerated, new `apps` suite.

### `tryaii monitor` — wrap the LLM calls your app makes (both SDKs)

- **`tryaii.monitor.wrap(client)`** (Python) / **`wrap(client)` from
  `tryaii/monitor`** (Node) patches an `openai` client in place. Every
  `chat.completions.create` call (sync, async, streaming) is recorded locally
  and **shadow-routed**: the model that would give at least the quality of the
  model in your code for at most its price is recorded next to the model that
  answered. The model id in your code stays — it is the anchor and the
  fallback.
- **Modes per sub-process**: `off` (record only, the default), `stable` (pin,
  for 24 hours, the model that won most of that sub-process's calls) and
  `dynamic` (route every call). Sub-processes are named with
  `with monitor.process("...")`, a per-call `tryaii_process=` argument, or fall
  back to the calling `file:function`. Modes are read from env
  (`TRYAII_MONITOR_MODE`), `wrap(mode=)` or `config.json`, which a running app
  re-reads — `tryaii monitor set` applies without a restart.
- **What can be swapped** depends on the client's `base_url`: OpenRouter →
  the whole catalog, `api.openai.com` → OpenAI models, other hosts → monitor
  only. A better model the client cannot reach is recommended, never routed.
- **Records** live under `~/.tryaii/monitor/<app>/`: daily `events-*.jsonl`
  (metadata only — model, tokens, estimated cost at the model used and at the
  winner, latency, call site, route status; no prompt or response text), daily
  `vectors-*.jsonl` (the prompt's embedding; `TRYAII_MONITOR_VECTORS=0`
  disables), `pins.json`, `config.json`. Nothing is uploaded without an app
  key (slice 2 above).
- **New CLI command** `tryaii monitor status | set <process> <mode> | apps |
  events` (`--app`, `--since 24h|7d|30d`, `-n`, `--now`, `--json`),
  byte-identical in both CLIs and frozen by `shared/monitor/fixtures/`.
- **Daemon**: a `route` request may ask for `includeEmbedding`; the daemon
  returns the prompt's vector (`{model, dim, v}`) and advertises
  `features: ["routeEmbedding"]` in its state file. Routing for the wrapper
  goes through the daemon on a background thread (`dynamic` routes in-path
  within `TRYAII_MONITOR_ROUTE_TIMEOUT_MS`, default 1500 ms); the embedding
  model is never loaded inside your application. Every monitor step is
  fail-open.
- Call-site capture now records the enclosing function name (Python
  `CallSite.function`; Node `captureCallSite()` moved to a shared module).
- Contract: `shared/monitor/SPEC.md`; docs: `docs/cli/monitor.md`,
  `docs/sdk/monitor.md`; examples: `examples/monitor_openai.py` / `.mjs`.

### Policies — details

- Policies need the full catalog, which now carries origin data (`origins.json`,
  catalog contract v1.2). Run `tryaii login` (free) first. With no policy set, routing
  is unchanged.
- `tryaii policy set` saves to `~/.tryaii/policy.json`. `--origin`, `--providers` and
  `--platform` on `route`, `eval` and `models` override the saved value for one call.
- `tryaii models` ends each line with the model's origin (`| origin: US`, `US+CN` for a
  US fine-tune of a Chinese base model), and `route` prints a `Policy:` line.
- SDK: `Router(policy=…)`, `DREClient(policy=…)` (Python) and `new Router({ policy })`
  (Node) take a `ModelPolicy`, a dict, `None` (no policy) or `"auto"` (the saved one, the
  default). `router.policy_result` (Node: `router.policyResult`) lists allowed and
  excluded models with the reason.
- Python and Node apply policies identically: both SDKs' tests reproduce the shared
  fixtures in `shared/datasave/fixtures/`. See `docs/cli/policy.md` and
  `shared/datasave/SPEC.md`.

## 0.6.1 (2026-10-07)

First npm release of the 0.6 line: 0.6.0 was published to PyPI only. Features are
listed under 0.6.0 below.

- Package metadata (npm `repository`, `homepage`, `bugs`; PyPI project URLs) now
  points to https://github.com/Tryaii-Admin/tryaii.

## 0.6.0 (2026-10-07)

### Highlights

- **A new built-in catalog.** The package now ships a **starter catalog**: 45
  well-known models from OpenAI, Anthropic, Google, DeepSeek, xAI and Mistral,
  scored on 16 current benchmarks. It works offline, with no account.
- **Log in (free) for the full catalog.** New `tryaii login`, `tryaii logout`
  and `tryaii whoami` commands. After signing in with your tryaii.com Google
  account, the **full catalog of 322 models** downloads automatically, is
  refreshed at most once a day, cached locally, and used only after its
  Ed25519 signature checks out.
- **A new routing score.** Quality now decides *which models may win*, and
  cost and speed decide *which of them does*; cost is judged on a log scale and
  speed as the wait for a typical answer.

### `tryaii login` / `logout` / `whoami` (both SDKs)

- `tryaii login` is a device sign-in: it prints a URL and a short code; open
  the URL in any browser (it does not have to be on the same machine, so it
  works over SSH and in containers), sign in with Google and approve the code.
  Credentials are stored in `~/.tryaii/credentials.json` (or under
  `TRYAII_DRE_DATA_DIR`), written atomically with mode 0600 on POSIX. Right
  after signing in, the full catalog is downloaded
  (`Downloaded the full catalog (<n> models).`); if that fails, it is fetched
  on next use and `login` still succeeds.
- `tryaii logout` revokes the session (best effort) and deletes the
  credentials and the downloaded catalog.
- `tryaii whoami [--json]` shows the account, its entitlements and the catalog
  release in use (`Catalog: full, release <version> (<n> models)` or
  `Catalog: not downloaded yet`).
- `TRYAII_API_URL` overrides the API base URL for `login` (default
  `https://api.tryaii.com`); a stored session always talks to the server that
  issued it.
- Output, exit codes and help text are byte-identical in both CLIs. The
  protocol is specified in `docs/auth/CONTRACT-auth-v1.md`; new docs pages
  `docs/cli/login.md`, `logout.md`, `whoami.md`.

### Starter catalog and full catalog (both SDKs)

- `Router(catalog="auto" | "starter" | "full")` / `new Router({ catalog })`.
  `auto` (the default) routes on the full catalog when you are logged in and on
  the starter catalog otherwise; `starter` always uses the packaged catalog
  with no network access; `full` raises `LoginRequiredError` when not logged
  in. A session the server rejected raises `SessionEndedError` (the local
  credentials and cached catalog are deleted). `ModelRegistry.default()` and
  `BenchmarkRegistry.default()` take the same option. In Node the constructor
  starts from the local cache, and the first `route()` (or
  `await router.ready()`) completes the daily check.
- The full catalog is checked at most once a day (`ETag` / `If-None-Match`,
  one request with a 10 s timeout) and cached under `~/.tryaii/catalog/`.
  Every file is verified against the manifest's sha256 and the manifest
  against an **Ed25519 signature** from a key built into the package before
  anything is written or used; a catalog that fails verification is never used
  (the previous full catalog, or the starter catalog, is used instead, with a
  one-line notice on stderr). `TRYAII_CATALOG_TRUSTED_KEYS` replaces the
  built-in key list for development and tests only.
- CLI: when nobody is logged in, `route`, `eval` and `models` print a one-line
  hint to **stderr** at most once a day (`Routing on the starter catalog (45
  models). Log in for free to use the full catalog (322 models): tryaii
  login`); `TRYAII_NO_BANNER` silences it. `models`, `benchmarks`, `setup` and
  `regenerate` work on the catalog in use.
- Routing data is now a **catalog bundle** (`manifest.json`, `models.json`,
  `benchmarks.json`, `normalization_ranges.json`, `centroids.json`,
  `training_queries.json`; format in `docs/catalog/CONTRACT-catalog-v1.md`).
  `load_bundle(dir)` / `loadBundle(dir)`, `Router(bundle=...)` /
  `new Router({ bundle })`, and `from_bundle` / `fromBundle` on
  `ModelRegistry`, `BenchmarkRegistry` and `BenchmarkNormalizer` route on any
  bundle. The benchmark taxonomy, importance weights, random-chance floors and
  the classifier's category labels moved from code into `benchmarks.json`, so
  one engine routes both catalogs.
- The routing daemon routes on the catalog the CLI selected and is replaced
  when it changes. User centroid caches are now one file per catalog:
  `centroids_<model>__<kind>-<version>.json`.
- Ephemeral OpenRouter `:free` variants are never listed, routed, evaluated or
  picked by budget optimization; `ModelRegistry.default(include_free=True)` /
  `load_preset(..., include_free=True)` (Node: `{ includeFree: true }`,
  `isFreeTier()`) opt back in. User-facing model counts never include them.
- Models carry two measured speed fields, `tokens_per_second` and `ttft_ms`
  (median time to first token), in `ModelInfo`, `to_dict()` and
  `tryaii models --json`. Partially known pricing is treated as unknown.
- `tryaii diagnose` resolves bare model names (`gpt-5.5`) against the
  provider-prefixed catalog ids.
- Fixed: `tryaii diagnose` again recognizes provider-native API model ids
  such as `claude-sonnet-4-5-20250929`, `claude-haiku-4-5@20251001`,
  `grok-4-latest` and `mistral-medium-2508` (dated and `-latest` aliases),
  so the `cost_exposure` and `model_fit` checks no longer report
  `insufficient_data` for them. They map to the matching catalog model only
  when that model is in the catalog you are using; an unknown id still stays
  unresolved rather than being matched to a similar model.

### Routing score: `satisficing-v1` (both SDKs)

- **Quality band.** Quality opens a tolerance band below the best candidate,
  `eps = EPS_UNIT * ((cost - 1) + (speed - 1)) / quality` (`EPS_UNIT = 0.102`,
  the one tuning dial); every model inside the band is then ranked on cost and
  speed only. The quality priority sets the band width rather than acting as a
  weight. At 5/1/1 there is no band: the quality leader wins.
- **Cost on a log scale.** `U_c` maps $0.05/M -> 1 and $50/M -> 0 on the
  average price per million tokens; every 10x cheaper is worth the same. A
  missing price scores 0, not neutral, and is flagged `cost: unknown`.
- **Speed as perceived wait.** `U_s` is log-scaled on the seconds to a
  300-token answer, `T300 = ttft_ms/1000 + 300/tokens_per_second` (0.3 s -> 1,
  30 s -> 0). A missing TTFT falls back to the catalog median
  (`ttft: estimated`); a missing throughput falls back to a deliberately
  pessimistic catalog p25 (`speed: unknown`). `latency` remains a display tier.
- **Evidence-aware quality.** Each benchmark term is weighted by how much of
  the catalog reports that benchmark (`COVERAGE_EXPONENT`), and imputed terms
  count half (`IMPUTED_TERM_WEIGHT`), so picks rest on measured scores.
  `ModelRegistry.benchmark_coverage()` / `benchmarkCoverage()` and
  `ModelScore.benchmark_weights` / `benchmarkWeights` expose this.
- **Catalog-fitted normalization.** `NORMALIZATION_RANGES` is fitted to the
  catalog (`lo` = p25 of real scores, `hi` = max) instead of hand-set floors,
  spreading the frontier out so the band is meaningful.
- **New reasoning string**, byte-identical in both SDKs and explicit about the
  trade it made, e.g. `q'=0.98 (4 real of 5) | imputed: 1/5 | cost 0.2066
  ($12.00/M) | speed 0.4154 (4.43 s to 300 tok) | within 0.020 of the best
  (0.98) | at 5/1/2 you accept up to 0.020 less quality for a cheaper or faster
  model; this pick gave up 0.00 vs anthropic/claude-opus-5.5`.
- **`ModelScore` gains** `band_base`, `in_band`, `quality_tolerance`,
  `quality_best`, `signal_flags`, `benchmark_weights`, plus the unrounded
  `q_prime`, `u_cost`, `u_speed` and `t300`. New exports:
  `satisficing_combine` / `satisficingCombine`, `quality_tolerance` /
  `qualityTolerance`, `cost_utility` / `costUtility`, `speed_utility` /
  `speedUtility`, `EPS_UNIT`,
  `IMPUTED_TERM_WEIGHT`, `COVERAGE_EXPONENT`.
- Ties break deterministically on `q'`, then on the number of real
  (non-imputed) benchmark scores, then on model id.
- Docs: `sdk/routing/scoring.md` and `sdk/routing/priorities.md` rewritten.

### Design partners

- Added a privacy-first [design-partner guide](docs/design-partners.md) and a
  GitHub issue template for applications. Eval prompts stay local; sharing is
  explicit and optional.

### Breaking changes

- **The built-in catalog changed.** The 39-model built-in list is replaced by
  the 45-model starter catalog. Model ids are now OpenRouter-native
  `provider/model` slugs (`openai/gpt-5.5`, `anthropic/claude-sonnet-4.5`)
  instead of bare ids (`gpt-5.5`, `claude-sonnet-4-5-20250929`);
  `MODEL_ID_TO_OPENROUTER` still maps the old ids.
- **The benchmark set changed.** The 12 previous benchmarks (MMLU, HellaSwag,
  HumanEval, SWE-bench, TruthfulQA, ARC, GSM8K, DROP, SuperGLUE, Chatbot Arena
  (LMSys), MT-Bench, LiveBench) are replaced by the starter catalog's 16:
  AIME-2024, AIME-2025, GPQA, HLE, AA-LCR, MMLU-Pro, MMMU, LegalBench,
  LiveCodeBench, SciCode, Tau2-bench, Terminal-bench-Hard, IFBench and Chatbot
  Arena Elo (+ Code / Vision). Custom models scored only on the old names get
  no quality signal from them until you add scores on the new ones (or
  register the old names as custom benchmarks). `STANDARD_BENCHMARKS`,
  `NORMALIZATION_RANGES`, `BENCHMARK_WEIGHTS` and `RANDOM_CHANCE_FLOORS` now
  describe the starter catalog.
- **`final_score` is no longer rescaled into 0.1–0.95.** Contenders (inside the
  quality band) score in [0.5, 1.0] and everyone else in [0.0, 0.5]; it is
  comparable within one call only. Use `quality_score`, `cost_score` and
  `speed_score` to compare across calls.
- **`SPEED_SCORES` is removed**; speed comes from measured `tokens_per_second`
  and `ttft_ms`, not from the `latency` tier.
- **Reasoning strings have a new format** (see above); read the `ModelScore`
  fields instead of parsing the string.
- The old package data files (`registry/presets/default_models.json`,
  `centroids/data/*`) are gone; `ModelRegistry.default()` and
  `load_preset("default")` keep working and return the default catalog.

## 0.5.1 (2026-09-17)

### designpartner — questionnaire v4 (both SDKs)

The design-partner questionnaire (`shared/designpartner/questions.json`)
was rewritten after a review of what sales and product actually need
from an intake. Schema and `ask_if` grammar are unchanged; engines are
untouched; fixtures and goldens regenerated from the Python reference.

- Asks about the application, not the person: section "How you use AI"
  is now "Your application"; calls/spend/stage prompts reworded.
- Qualification data for sales: `ai_tools_in_use` (0 / 1-2 / 3-5 / 6+ /
  Other) followed by `decision_authority` ("who normally decides on a new
  tool"), `existing_solution` (+ `existing_product`), `cost_target`
  (asked of everyone, incl. "cost isn't what would decide it"),
  `checkins_ok` (one firm bi-weekly 15-minute ask, `checkins_alt` on a
  no) and `reference_ok` (public reference / case study).
- Product data: `use_cases` (ranked by builder-survey prevalence),
  `eval_methods` (multi-select, optional = none), `latency_sensitivity`
  (customer-facing production only), `model_choice_process` as a
  multi-select with an automatic-router option (+ `router_name`).
- Every "Other" option has a companion free-text question gated on it
  (`models_other`, `frameworks_other`, `use_case_other`, `eval_other`,
  `model_choice_other`, `biggest_pain_other`, `ai_tools_other`,
  `calls_per_day_other`, `monthly_spend_other`); the skill asks it in
  the same breath as its parent.
- Removed: `role`, `models_openai/anthropic/google`, `company_size`
  (now `team_size`), `follow_up_call`/`time_commitment` (merged into
  `checkins_ok`), `timeline`/`priority`, `unsure` buckets (now `other`
  + free text); providers drop Cohere and Meta (Llama), add
  open-source/self-hosted and Hugging Face.

## 0.5.0 (2026-08-21)

### designpartner — design-partner enrollment (both SDKs)

New `tryaii designpartner` command — ONE resumable command (no verbs)
that enrolls a user in tryaii's design-partner program, driven by their
coding agent via a new `tryaii-designpartner` skill (installed
automatically on the first run). Every invocation reads the state
(`.tryaii/designpartner/`), ingests any flags, advances, and prints the
stage + exactly what to do next (`--json` for agents); the flow resumes
anywhere.

- **Questionnaire as data** (`shared/designpartner/questions.json`): three
  sections (About you / How you use AI / Where tryaii fits) of typed
  questions with explicit `ask_if` conditional gating (per-provider model
  questions appear only for selected providers). Validation reports ALL
  problems at once and never blocks (exit 0; the agent loops).
- **Three consent tiers, shown verbatim**: `contact_only` (answers only),
  `summary_insights` (+ the redacted diagnose summary — no code, paths,
  or prompts), `full_partnership` (+ the FULL findings and the raw-prompt
  inventory of the latest diagnose run — its consent copy states the
  prompt/path disclosure in plain words). Insight tiers require a
  diagnose run; `--consent` writes `preview.json` (exactly what will be
  sent) and `--confirm` — the only sending invocation — rejects a stale
  preview.
- **Submission**: versioned payload
  (`tryaii.designpartner.submission/1`), ALWAYS saved to
  `.tryaii/designpartner/submission-<stamp>.json` before any network
  attempt; POST to `https://api.tryaii.com/v1/design-partners` (override
  `TRYAII_DESIGNPARTNER_URL` — a new env convention) via stdlib urllib
  (Python; no new dependency) / native fetch (Node), 10s timeout, one
  attempt; any failure collapses to a deterministic "saved locally"
  notice with exit 0.
- **Cross-SDK parity, enforced**: `shared/designpartner/SPEC.md`
  contract, five fixture suites frozen from the Python reference
  (`scripts/gen-designpartner-fixtures.py --check`), Node conformance
  with key-order pinning, and a cross-CLI suite that byte-compares
  stdout/stderr/exit AND every written file (state, preview, submission,
  installed skill) — the network save-local branch runs deterministically
  in fixtures via an instantly-refused URL injected through a new per-case
  `env` seam. `tryaii diagnose`'s skill-install helpers were generalized
  in place and are shared by both commands.

### diagnose — agent-first codebase LLM diagnostics (both SDKs)

New `tryaii diagnose` command (verbs: `init`, `plan`, `check`, `report`) and
`tryaii.diagnose` / `tryaii/diagnose` engine modules. The user's coding
agent interviews the user, discovers the codebase's LLM call sites
free-form (any language), and writes an inventory JSON; tryaii runs
deterministic checks over it and renders a local HTML report.
**Insight-only**: never edits code, never sends anything anywhere.

- **Four checks per call site** — `model_fit` (full-catalog ranking under
  the user's quality/cost/speed priorities; rank + absolute dimension
  scores, never renormalized cross-set deltas; also recommends the best
  model within ±20% of the current model's price —
  `recommended_same_price`, a quality upgrade at the price already being
  paid), `cache_readiness` (the
  cachelint engine per site), `cost_exposure` (per-call/monthly cost,
  upper-bound cache savings via conservative read-discount factors in
  `shared/diagnose/costmodel.json`, quality-tolerant cheaper-swap
  suggestion), `hygiene` (canonical prompt scan + fix hints via a new
  public `cachelint.analyzer.hygiene_findings` seam).
- **Lenient intake, honest degradation**: loosely-shaped inventories are
  accepted; anything missing marks that check `insufficient data` with a
  reason for that site — nothing is guessed. Every site appears in the
  report; healthy ones as green checks.
- **Agent playbook**: `diagnose init` installs
  `.claude/skills/tryaii-diagnose/SKILL.md`, an AGENTS.md marker block,
  and an anchored `/.tryaii/` gitignore entry (idempotent; masters live in
  `shared/diagnose/skill/`). `diagnose plan --json` hands the agent the
  check catalog, interview questions, and the exact inventory shape.
- **Run store + deltas**: each `check` writes
  `.tryaii/diagnose/<run-id>/` (inventory, findings.json, meta) plus a
  `latest` pointer; `report` renders a self-contained `index.html` from
  ONE shared template (`shared/diagnose/report/template.html`) and shows a
  delta band vs the previous run. `findings.json` carries a redacted
  `summary` layer (`tryaii.diagnose.summary/1`) — no code, prompts, or
  paths — designed as the payload for a future opt-in upload (not built).
- **Live classification** rides the routing daemon (one embedding-model
  load for N sites) and is gated on `tryaii setup`, which now writes a
  `setup.json` marker; inventories with precomputed `_classification`
  need neither.
- **Cross-SDK parity, enforced**: `shared/diagnose/SPEC.md` contract, 8
  fixture suites frozen from the Python reference
  (`scripts/gen-diagnose-fixtures.py --check`), a Node conformance suite
  (including byte-identical HTML against the Python-rendered goldens), and
  a cross-CLI test asserting byte-identical stdout/stderr AND written
  files (findings, meta, report HTML, installed skill) for every CLI
  fixture. En route, the Node scoring engine switched to half-even
  rounding/formatting (`halfEvenRound`/`formatFixed`), fixing a real
  cross-SDK divergence on rounding-boundary scores.

### cachelint — pre-flight prompt-cache analysis (both SDKs)

New `tryaii.cachelint` (Python) / `tryaii/cachelint` (Node subpath export)
module and a `tryaii cachelint` CLI subcommand: analyze prompts BEFORE they
are sent and predict whether they will hit the provider's prompt cache.

- **18 dynamic-content detectors** (timestamps, UUIDs, session IDs, secrets,
  unrendered template slots, date literals; three severity tiers) with exact
  code-point offsets, plus **stable-prefix computation** — caching is a prefix
  match, so *where* a dynamic value sits decides how much can cache.
- **7-provider knowledge base** (OpenAI, Anthropic, Gemini, xAI, OpenRouter,
  Bedrock, Vertex): per-model token floors, enablement modes (automatic vs
  `cache_control`/`cachePoint` opt-in), TTLs, verify fields, and gateway
  upstream inheritance. Ships as shared data
  (`shared/cachelint/providers.json`), synced into both packages and
  byte-compared by the parity suite.
- **Six verdicts** (`CACHEABLE` ... `EFFECTIVELY_UNCACHEABLE`) with ordered,
  actionable recommendations ending in the exact `usage` field to assert
  after deploying.
- **Sequence analysis** for request lists: per-transition `HIT` / `PARTIAL` /
  `MISS` / `AT_RISK` / `UNKNOWN` predictions, divergence forensics (offset,
  section, likely cause), and TTL-gap checks via optional `sent_at`.
- **CLI**: `tryaii cachelint <input.json | -> [--json]`, plus a raw-text mode
  (`--provider`/`--model`) that treats the entire input as one pasted prompt.
  Warn-only by design: findings never change the exit code.
- **Cross-SDK parity, enforced**: both engines conform to 148 frozen golden
  fixtures generated from the Python reference
  (`scripts/gen-cachelint-fixtures.py`), and a cross-CLI test asserts
  **byte-identical stdout** (including `--json`) for every CLI fixture. The
  behavior contract lives in `shared/cachelint/SPEC.md`.
- **Dependencies**: exact OpenAI/xAI token counts use the o200k tokenizer —
  a new `cachelint` pip extra (`pip install tryaii[cachelint]`) on Python;
  bundled via `js-tiktoken` (the package's first runtime dependency) on Node,
  loaded only by the cachelint module so router users never pay for it.

Origin: the `cache_providers` prompt-caching research (2026-07) and its
cachelint prototype, ported with 16 deliberate fixes (crash-free timestamp
parsing, canonical JSON, Claude-on-Vertex verdicts, and more — see SPEC.md
§2). Thresholds/prices are time-sensitive; refresh policy in SPEC.md §5.

### cachelint — SDK warn hook + runtime cache verification (both SDKs)

The engine now runs inside the clients. `cache_lint="warn"` (Python:
`DREClient`, `AsyncDREClient`, `OpenRouterIntegration`) / `cacheLint: 'warn'`
(Node: `DREClient`, `OpenRouterIntegration`) — or `TRYAII_CACHE_LINT=warn` —
enables two things on every `chat`/`stream` (and async `route_and_chat`):

- **Pre-flight lint of the actual outgoing prompt** — post routing, message
  assembly, and truncation, the exact payload is analyzed and problems print
  1–3 stderr lines (verdict + first blocker). Problems only (`BELOW_THRESHOLD`,
  `EFFECTIVELY_UNCACHEABLE`, `UNKNOWN_THRESHOLD` with blocking findings), and
  only **once per unique prompt shape per client instance** (hash-deduped,
  FIFO-capped, 1h stale reset) — quiet in agent loops.
- **Runtime verification** — from the second same-shape call onward, a prompt
  predicted cacheable that reports 0 cached tokens in the response `usage`
  (`prompt_tokens_details.cached_tokens` / `cached_tokens` / `cache_discount`,
  read defensively) warns `VERIFY_MISS` once. First calls are the expected
  cache write; streams verify best-effort when a usage chunk is present.

Warn-only and **fail-open by design**: no lint failure — engine error, missing
tiktoken (one-time install hint instead), broken stderr — can ever raise into
or block an API call. Warning strings are byte-identical across both SDKs,
pinned by mirrored unit tests. Docs: `docs/sdk/client/cache-lint.md`.

### cachelint — AST template introspection (both SDKs)

The warn hook now closes its founding blind spot. A rendered
`f"...{DAY_OF_WEEK}..."` reaches the provider as `"...Monday..."` — invisible
to every regex detector — but the SDK runs in-process, so at the wrapped call
it traces the prompt back to its template and names the exact slot:

```
[tryaii cachelint] template slot {DAY_OF_WEEK} at app.py:21 renders inside your cacheable prefix — its value changes between calls and breaks the cache there
[tryaii cachelint]   {DAY_OF_WEEK} = datetime.now().strftime('%A') at app.py:9
```

- **Automatic within warn mode** — no new API surface. Stack-walk capture to
  the user's frame (no hardcoded depths; exact call spans via `co_positions`
  on Python 3.11+, V8 structured CallSites with the spike-pinned await-unwrap
  rule on Node), then parse-never-execute AST tracing of f-strings / template
  literals, `.format()`, concatenation, and single-assignment variables.
- **Validation guard**: the traced template's static text is re-aligned
  against the ACTUAL rendered prompt; any mismatch (stale source, wrong node,
  conditionals, bundler rewrites, truncation) discards the analysis — wrong
  insights are structurally impossible, only missing ones.
- **Noise policy**: structurally dynamic slots (call-bearing) warn on first
  sight; bare-name slots arm on first value and warn when it changes; once
  per (call site, slot); max 3 lines + a summary; where the engine's
  detectors already catch the rendered value the slot stays silent, and
  existing blocker warns gain a `rendered by template slot ...` attribution.
- **Privacy**: the calling module's source is read locally, parsed, never
  executed, never transmitted; warnings carry file basenames only.
- **Dependencies**: `acorn` joins the Node package (tiny, zero-dep) for JS
  parsing; `typescript` becomes an *optional* peer for `.ts` frames (tsx/
  ts-node users already have it) — unimportable → fail open. Python needs
  nothing new (stdlib `ast`/`inspect`).
- Fail-open remains absolute: REPLs, frozen bundles, exec'd code, reassigned
  variables, function-parameter prompts, `create_task`/`gather` sites, files
  over 1 MB — all silently produce exactly the previous behavior.

## 0.4.0 (2026-06-29)

### Catalog: bump flagship latency tier (claude-fable-5, claude-opus-4-8)

Moved `claude-fable-5` and `claude-opus-4-8` from the `slow` latency tier to
`medium`, raising their speed score from `0.3` to `0.6`. They were being unduly
penalized on speed-weighted/balanced routing relative to their quality. Data-only
change; preset stays byte-identical across SDKs.

### Routing daemon — fast repeated `route`/`eval`

`tryaii route` and `tryaii eval` previously paid the full embedding-stack import
and model load on every invocation (tens of seconds), since each CLI call is a
fresh process; the routing itself is sub-millisecond. Both SDKs now keep a warm
background daemon so only the first call pays that cost — subsequent calls drop
from ~minute to ~milliseconds.

- **Fully transparent — no new commands:** `route`/`eval` auto-start a daemon on
  first use and reuse it thereafter, falling back to in-process routing if it
  can't start. The daemon self-stops when idle (or on `SIGTERM`); the detached
  server runs as a module (`python -m tryaii.server` / `node .../server.js`).
- **New flag / env:** `--no-daemon` (per-call opt-out), `TRYAII_NO_DAEMON=1`
  (global opt-out), `TRYAII_DAEMON_IDLE=<seconds>` (idle shutdown, default 900).
- Implemented identically in the Python and Node SDKs (separate per-runtime
  daemons; loopback TCP + token auth). Protocol documented in `docs/daemon.md`.

### Catalog: standardized OpenAI latency tiers

Normalized inconsistent latency tiers in the OpenAI line so siblings rank
consistently on speed: all `mini` models are now `fast` (`gpt-4o-mini`,
`gpt-5-mini`, `o4-mini`; `gpt-5.4-mini` already was), and the regular full models
are `medium` (`gpt-5` moved down; the rest already were). `nano` models stay
`very fast`, giving a clean nano > mini > regular ordering. Previously
`gpt-5-mini` was `very fast` while `gpt-5.4-mini` was `fast`, and `gpt-5` was
`fast` while `gpt-5.4`/`gpt-5.5` were `medium`, which let an older sibling
out-rank a newer one on balanced routing purely on a speed-tier gap.

### Scoring: benchmark normalization fit to the catalog (more routing spread)

The benchmark normalization ranges were tightened from loose textbook bounds to
the observed min/max of the shipped catalog, so models spread across most of
0–1 instead of bunching into a narrow high band. Previously several benchmarks
were badly compressed (LiveBench used only 18% of the 0–1 range, ARC 23%, Arena
36%), so the quality dimension couldn't differentiate frontier models and
balanced routing collapsed onto cost/speed — one model (`gpt-5-mini`) won 93% of
the 1000-prompt eval. After the change, balanced routing spreads across several
models (top model ~52%), while quality-max still picks the flagships and budget
still picks the cheapest. Quality-only ranking on a single benchmark is
unchanged (normalization is monotonic).

- Updated `NORMALIZATION_RANGES` (`scoring/benchmarks.{ts,py}`) and the mirrored
  `STANDARD_BENCHMARKS` (`benchmarks/standard.{ts,py}`) in both SDKs, kept in
  sync (guarded by `test_parity.py`). Re-fit these when the catalog changes a lot.

### Scoring: cost/speed priorities are now fully suppressible

The priority→weight mapping changed so that a priority of `1` (don't care) on
cost or speed now yields a weight of **0** instead of a `0.28` floor. Previously
even `Priorities(quality=5, cost=1, speed=1)` kept ~32% of the decision on
cost/speed, so a cheaper/faster model could out-rank a strictly higher-quality
one — e.g. quality-max routing picked `gpt-5` over `gpt-5.5`. Now quality-max is
a true quality-only route (the 1000-prompt eval flips from gpt-5/gpt-5.4 to
`gpt-5.5` + `claude-fable-5`). Quality keeps a `0.3` baseline so it never drops
to zero (no divide-by-zero) and a prompt is never scored on cost/speed alone.

- Weight formula: `base + ((priority - 1) / 4) * span` — quality `0.3..1.2`,
  cost/speed `0..1.0`. Balanced (3/3/3) routing is essentially unchanged.
- The no-signal fallback (prompt matches no benchmark) still floors cost/speed
  to `0.1` so it stays "routable on cost/speed" as designed, even when the user
  suppressed them.
- Applied identically to both SDKs (`scoring/priorities.{ts,py}`,
  `scoring/engine.{ts,py}`).

### Model catalog resync (June 2026)

Refreshed the bundled model catalog (`shared/models/default_models.json`, synced
to both SDKs) to match the tryai web app's 2026-06 lineup. **33 → 39 active
models** (`"updated": "2026-06"`).

- **Added (14):** `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`,
  `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-fable-5`, `gemini-3.5-flash`,
  `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite`, `deepseek-v4-pro`,
  `deepseek-v4-flash`, `grok-4.3`, `mistral-medium-2508`.
- **Removed (8):** `o1`, `claude-3-7-sonnet-20250219`, `gemini-2.0-flash`,
  `gemini-3-pro-preview`, `grok-3-mini-latest` (retired upstream) and
  `grok-4-fast`, `gpt-4.1-nano`, `grok-3-latest` (dropped from the active set).
- **Re-priced/re-latency'd** several existing models from the upstream catalog
  (notably `claude-opus-4-5` output $0.075→$0.25/1k, `o3`, `gemini-2.5-pro`,
  `gpt-5.2`). This changes cost/latency-based routing for those models.
- **Corrected two upstream price typos** (input ≥ output): `gemini-2.5-flash`
  output $0.00025→$0.0025 and `gemini-3-flash-preview` input $0.003→$0.0003.
- Added OpenRouter slug mappings for the 14 new models (both SDKs).
- `ARC` scores stay DRE-owned (tryai's ARC column drifted to a different scale);
  new models use peer-consistent ARC estimates. Scoring algorithm unchanged.

See `docs/model-catalog-sync-2026-06.md` for the full transform, decisions, and
flagged upstream pricing anomalies.

## 0.3.0 (2026-06-08)

**Package renamed `tryaii-dre` → `tryaii`** on both PyPI and npm. The old
`tryaii-dre` packages are deprecated and will receive no further updates; install
`tryaii` going forward. This is a breaking change to the install name, import
path, and CLI command — pin to `tryaii-dre` 0.2.x if you can't migrate yet.

### Migration

- **Install:** `pip install tryaii` / `npm install tryaii` (was `tryaii-dre`).
- **Python import:** `from tryaii import Router, Priorities` (was
  `from tryaii_dre import ...`). The import module is now `tryaii` (no underscore).
- **Node import:** `import { Router } from "tryaii"` (was `"tryaii-dre"`).
- **CLI:** the command is now `tryaii` (e.g. `tryaii route "..."`,
  `tryaii eval prompts.json`) — previously `tryaii-dre`.
- The public API (classes, methods, scoring, CLI subcommands and flags) is
  otherwise unchanged; only the names moved. `DREClient` keeps its name.

### CLI surface parity (npm vs PyPI)

The two CLIs accepted slightly different global flags and failed differently;
they now behave identically:

- `-V/--version` now works on the Python CLI too (was Node-only).
- `-v/--verbose` now accepted by the Node CLI too (was Python-only), and works
  in any position on both (Python previously required it before the
  subcommand). Sets `TRYAII_VERBOSE=1` in Node; enables debug logging in
  Python.
- `-h/--help` works in any position on both (e.g. `tryaii eval --help`;
  previously a parse error in Node), bare `tryaii help` works on both (was
  Node-only), and both print byte-identical help text (guarded by
  `test_parity.py`).
- **Per-command help.** `tryaii help <command>` now prints detailed,
  per-command help (usage, arguments, flags, examples, exit codes), and
  `tryaii <command> -h/--help` prints that same page instead of the global
  overview — a behavior change from earlier 0.3.0 builds, where every
  `--help` printed the global text. The `help` command is self-documenting
  (`tryaii help help` / `tryaii help --help`), and an unknown topic
  (`tryaii help bogus`) exits `2`. Every per-command page is kept
  byte-identical across the npm and PyPI CLIs (guarded by `test_parity.py`).
- Exit codes unified: `0` success, `1` runtime failure, `2` usage error
  (unknown command/option, missing argument, invalid value). Node previously
  exited `1` for usage errors; it also no longer silently falls back to
  defaults on non-numeric `--quality/--cost/--speed/--top-k` values.
- Python runtime failures now print a clean one-line `error: ...` message
  instead of a traceback, matching Node.
- Python now rejects a negative `--difficulty-gamma` up front like Node
  (previously it silently skewed budget allocation).
- Python CLI default log level is now WARNING (use `--verbose` for more), so
  `route`/`eval` output is as quiet as the Node CLI.
- `setup` prints the same completion message on both SDKs.

## 0.2.1 (2026-05-31)

Bugfix release. **The 0.2.0 PyPI wheel was broken and has been yanked** — please
use 0.2.1. (The npm 0.2.0 package was unaffected; 0.2.1 is published for parity.)

### Fixed

- **Missing `tryaii_dre.cache` submodule in the published wheel.** The root
  `.gitignore` had an unanchored `cache/` pattern (intended for the repo-root
  benchmark-snapshot cache). Hatchling honors `.gitignore` at build time, so the
  pattern also matched `packages/python/tryaii_dre/cache/` and silently dropped
  it from the sdist and wheel — a clean `pip install tryaii-dre` followed by
  `from tryaii_dre import Router` raised
  `ModuleNotFoundError: No module named 'tryaii_dre.cache'`. The patterns are now
  anchored (`/cache/`, `/cache-shared/`) so only the repo-root directories are
  ignored, and the wheel ships all 33 modules. npm was unaffected (it ships
  `dist/` via the `files` field, not `.gitignore`).

### Changed

- Python CI lint is green again. `UP045` (`Optional[X]` → `X | None`) is ignored
  in ruff config because the package targets Python 3.9 + pydantic, where that
  union syntax raises `TypeError` at annotation-evaluation time; the remaining
  `E402`/`E501`/`F841` findings were cleaned up.

## 0.2.0 (2026-05-30)

First public release on PyPI and npm (the 0.1.0 monorepo below was never
published to a public registry). Highlights: full Node/Python routing parity, a
matching `tryaii-dre` CLI on both packages, and scoring v2 in both SDKs.

### Node/Python SDK parity reconciled

A cross-SDK audit found the Node and Python routers had silently drifted despite
being meant to produce identical routing/scoring/budget decisions. Reconciled to
a single set of canonical rules:

- **Preset data** is now byte-identical across both packages (Python
  `default_models.json` is the source of truth; the Node copy was corrected).
- **Speed scores, cost/speed gating, priorities clamping (round-half-up), and
  tie-breaks** (utility → lowest cost → smallest model id) now match.
- **Budget feasibility** is decided in float; the integer DP only optimizes and
  falls back to the cheapest assignment rather than declaring a feasible dataset
  infeasible.
- **No-benchmark-signal fallback**: prompts whose embedding is orthogonal to
  every centroid stay routable on cost/speed instead of crashing `route()`.
- A `tests/test_parity.py` guard asserts the preset JSONs and normalization
  ranges stay in sync.

### CLI parity — one `tryaii-dre` command on both packages

Both SDKs now ship a matching `tryaii-dre` CLI (`route`, `eval`, `models`,
`benchmarks`, `setup`, `regenerate`) with identical `eval` artifacts
(`results.jsonl` + `summary.json` + `index.html`). Each opens with an animated
blue→red banner printed to stderr that self-suppresses on non-TTY / `NO_COLOR` /
`TRYAII_NO_BANNER` / `--no-banner`.

### Scoring v2 — top-5 benchmarks with median imputation

`packages/node/src/scoring/engine.ts`

Routing now considers the prompt's top-5 most-similar benchmarks (was 3)
and fills missing benchmark data with the **registry-wide median** for
each benchmark instead of silently dropping it.

**Why.** The old behaviour produced an unintuitive failure mode: a model
with sparse benchmark data could outrank a fully-covered model because
missing benchmarks were erased from both the numerator and denominator of
the weighted-quality average. A model with `{HumanEval: 95}` only would
score 100% on the one benchmark it had, while a model with
`{HumanEval: 95, LiveBench: 60}` got dragged down by including LiveBench —
so the broader-coverage model lost.

Observed in the eval harness at `quality=5/cost=1/speed=1`: `grok-4-latest`
(no LiveBench score) was picked for ~84% of coding prompts, beating
`gpt-5.2` (HumanEval 95, LiveBench 78) purely because grok's matched
LiveBench similarity was being dropped from its quality average. After the
fix, gpt-5.2 wins coding under quality-first priorities.

**What changed.**
- `TOP_BENCHMARKS_FOR_SCORING` raised from 3 to 5.
- Missing-benchmark data is imputed from the registry median instead of
  being silently skipped. Median is computed per `scoreModels` call from
  the same `models[]` argument the engine is about to score, so model
  filters flow through correctly.
- Imputation is neutral — sparse data is treated as "average", not zero.
  The previous free-pass effect goes away because the imputed value
  participates in the score instead of vanishing.
- The `bestReasoning` string appends `imputed: N/5` whenever any
  benchmark in the top-5 was imputed for that model, so eval output
  surfaces which decisions involved an estimate vs. real data.
- If **no** model in the registry has data on a benchmark, the existing
  "skip the model entirely if it intersects nothing" path is preserved
  (imputation needs data to estimate from). The test pinning that
  behaviour at `tests/scoring/engine.test.ts:137` still passes.

**API surface.** Unchanged. `RouteResult`, `ModelScore`, and
`ClassificationResult` shapes are the same; no call-site needs to change.

**Behavioural impact.** Routing decisions for prompts whose top-5
benchmarks include sparse entries (LiveBench, SWE-bench, MT-Bench, etc.)
will shift toward models with broader benchmark coverage. The shift is
most visible at quality-heavy priorities; balanced priorities are less
affected because cost and speed dampen swings.

**Python parity.** Mirrored in `packages/python/tryaii_dre/scoring/engine.py` —
both SDKs now use top-5 + median imputation with matching tie-breaks.

## 0.1.0 (2026-03-29)

- Initial monorepo release (ported from diffrential); never published to a public registry
- Python and Node core packages with routing engine support
- 35+ models from 6 providers (OpenAI, Anthropic, Google, xAI, DeepSeek, Mistral)
- 12 standard benchmarks (MMLU, HumanEval, SWE-bench, GSM8K, MT-Bench, etc.)
- Embedding-based semantic classification with keyword fallback
- 3-factor scoring engine (quality, cost, speed) with user priorities
- OpenRouter active routing integration
- CLI tool (tryaii-dre route, models, benchmarks, setup, regenerate)
- LRU cache with TTL for embeddings and classifications
- Pre-computed centroids for zero first-run delay
