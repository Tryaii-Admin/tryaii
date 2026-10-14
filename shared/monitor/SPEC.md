# monitor — behavior contract (v2: slice 1 + slice 2)

v2 (slice 2, 2026-10-12) adds: `event_id`, `taxonomy_version` and
`est_cost_anchor_usd` on events (§3.1); the `managed` mode (§2.9);
policy-aware reachability (§2.8, Jira TS-170); `remote.json` (§3.3) and
`upload.json` (§3.4); the HTTP helper and its fixture seam (§6); the
`tryaii apps` CLI (§7); status `source`/`upload`/realised lines (§2.7,
§5.2). The wire contract with the API server and the website is
`shared/monitor/API.md` (v1.1); this SPEC is the SDK side of it.

This file freezes the behavior of `tryaii monitor` and the `tryaii.monitor`
wrapper across both SDKs (`packages/python/tryaii/monitor` — the REFERENCE
implementation — and `packages/node/src/monitor`). Any behavior change is a
three-part edit: update this SPEC, regenerate `fixtures/` from the Python
engine (`scripts/gen-monitor-fixtures.py`), and update BOTH engines in one PR.

Product frame (locked): the wrapper sits **in the developer's process around
the `openai` SDK call**. The model id the developer wrote stays in the code —
it is the **anchor** (the quality/price reference) and the **fallback**. Every
call is recorded locally (metadata plus the prompt's embedding; **never the
prompt or response text**) and **shadow-routed** through the tryaii daemon so
the best-for-less **winner** is known even when nothing is swapped. Per named
**sub-process** a **mode** decides whether the call is actually sent elsewhere:
`off` (monitor only), `stable` (one pinned model for 24 h), `dynamic` (the
winner of this very call) or `managed` (a model assigned on the dashboard or
with `monitor set`). Everything is fail-open: a tryaii failure never
changes or blocks the developer's call.

---

## §1 Cross-language ground rules

1.1 All rules of `shared/cachelint/SPEC.md` §1 apply — in particular the
    **integral-float rule** (a float that is integral after rounding is
    emitted as an integer; both engines pass emitted documents through the
    number normalizer) and canonical JSON key handling.

1.2 **Key order is contract.** Objects are built in the field order given
    in this SPEC; the Node conformance test asserts `JSON.stringify`
    equality against the fixtures. JSONL lines are minified
    (`separators=(",", ":")` / `JSON.stringify`), non-ASCII verbatim,
    `\n`-terminated; documents (`pins.json`, `config.json`) are
    `indent=2` + trailing `\n`.

1.3 **Rounding.** Money (`est_cost_*`, `savings_usd`) is rounded half-even
    to **6** decimals. Classification `confidence` and `top_winner.share`
    to **4**. Vector components to **4**.

1.4 **Clock-free engine.** Timestamps are strings of the form
    `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC, millisecond precision; parsers also
    accept whole seconds and a `+00:00` offset). The engine never reads a
    clock: `now` is an input (the wrapper passes the current UTC time; the
    CLI passes `--now` or the current time). Pin TTLs and windows are
    computed from `now`.

1.5 **Routing-free engine.** Scores arrive in the daemon's wire shape
    (`{"modelId", "qualityScore", ...}` as serialised by `server.py`) or as
    `ModelScore` objects; both are read through the same accessors. No
    fixture needs the embedding model or a daemon.

1.6 **Catalog.** Fixtures use the **starter** catalog bundle only
    (`ModelRegistry.from_bundle(starter_bundle())`), so they never depend on
    a login. Pricing is USD per 1K tokens (`pricing.input_per_1k` /
    `output_per_1k`); the **blended price** of a model is their mean (the
    scoring engine's cost basis, `diagnose/modelfit._blended_price`).

## §2 Semantics

### §2.1 Routing text (`extract`)

Input: the native `messages` list of a chat-completions call.

- `message_text(content)`: a string as is; a list of content parts → the
  `text` of the parts whose `type == "text"`, joined with `\n`; anything
  else (None, image/audio parts, tool payloads) → `""`.
- `system` = the text of the **first** message whose role is `system` or
  `developer`; `user` = the text of the **last** message whose role is
  `user`. Non-object messages are ignored.
- `text` = `system + "\n\n" + user` when both are non-empty; `user` alone
  when there is no system text; `""` when there is no user text (a call
  without a user turn is not routed — `route_status: "skipped"`).
- `text` is truncated to `MAX_PROMPT_LENGTH` code points
  (`classifiers/base.py`, the router's own limit).
- `prompt_hash` = lowercase sha256 hex of the UTF-8 bytes of `text` (`""`
  hashes too).

### §2.2 Provider class, anchor, reachability (`resolve`)

- **Provider class** from the wrapped client's `base_url` host (a value
  without a scheme is read as `https://` + value):
  `openai` — unset/empty, `openai.com` or any `*.openai.com` host;
  `openrouter` — `openrouter.ai` or `*.openrouter.ai`;
  `other` — everything else (Azure OpenAI, local OpenAI-compatible
  servers, proxies).
- **Anchor** = `resolve_model_id(model_requested)` from diagnose §2.1
  (exact → slug → normalized → alias; dated and `-latest` ids resolve), or
  `null`.
- **Reachable** (can this client send a request for catalog id `m`?):
  `openrouter` → every catalog id; `openai` → ids starting `openai/`;
  `other` → only the anchor itself (never a swap).
- **`allowed_models`** (the reachable ids): `openrouter` → `null`
  (unfiltered); otherwise the reachable ids in catalog order (`[anchor]` or
  `[]` for `other`). What is actually sent as the daemon's `allowedModels`
  combines this with the data-save policy (§2.8).
- **Native id** (what goes into the request for a catalog id): `openai`
  → the part after the first `/`; `openrouter` → `MODEL_ID_TO_OPENROUTER`
  lookup (identity for slugs); `other` → unchanged.

### §2.3 Winner — best-for-less (`winner`)

Input: the value-ranked `scores` of a route (daemon order, ties already
deterministic), the anchor id, the registry and (v2) `allowed` — the
policy-allowed ids of §2.8, or `null`. `allowed` first restricts the
ranking (order kept), so a disallowed model — the anchor included — is
never the winner; ranks are 1-based in the restricted ranking.

1. Empty ranking → `{"winner": null, "rank": null, "method": "none"}`.
2. No anchor, or the anchor is not in the ranking → the top model:
   `{"winner": scores[0], "rank": 1, "method": "top"}`.
3. Otherwise `floor = anchor.qualityScore − 0.05`
   (`SWAP_QUALITY_EPSILON`, shared with diagnose) and
   `cap = blended_price(anchor)`. If the anchor is unpriced →
   `method: "anchor"`. Else the **first** ranked model with
   `qualityScore ≥ floor` and a known blended price `≤ cap`:
   the anchor itself → `method: "anchor"` (being the best at your price is
   a positive result); another model → `method: "band"` with its 1-based
   rank; none → the anchor, `method: "anchor"`.

The winner is computed on the **policy-filtered but reachability-
unfiltered** ranking (the shadow route, §2.8) so an unreachable better
model is still *recommended*; `winner_reachable` says whether the client
could have sent it. A model the policy disallows is never recommended.

### §2.4 Model actually sent (`routed_model`)

`off` → `null` (the developer's model as written). `dynamic` → the
**routed winner**: §2.3 applied to the in-path ranking, i.e. the ranking
restricted to reachable ∩ policy (§2.8) — so when the recommendation is
unreachable, the best-for-less model the client *can* reach is sent (v2,
TS-170; slice 1 sent the anchor). `stable` → the pinned model. `managed`
→ §2.9. A target that is `null`, equals the anchor, is not reachable, or
is outside the policy → `null`. The event records `model_used` = the native
id that was sent (anchor native id when `null`).

The `winner` fixture suite freezes, per case: `winner` (§2.3 on the
policy-filtered ranking), `winner_reachable`, `routed_winner` (§2.3 on the
ranking filtered to `route_allowed`), `routed`, `model_to_send` and
`route_status` (`unreachable_managed` or `null`). Inputs: `provider`,
`mode`, `anchor`, `scores`, optional `pin_model`, `allowed`,
`managed_model`.

### §2.5 Pins (`pin`)

`pins.json` = `{"schema": "tryaii.monitor.pins/1", "processes": {name: pin}}`
with `pin = {"model", "pinned_at", "expires_at", "basis": {"window",
"events", "counts"}}`; processes sorted by name.

`evaluate_pin(events, pin, now, process)`:

1. `pin_valid`: the pin has a `model` and `now < expires_at` → `action:
   "keep"`.
2. Else count the **reachable winners** (`winner` non-null and
   `winner_reachable` true) of `process` whose `ts` lies in
   `[now − 24h, now]`; when that holds fewer than **20**, use
   `[now − 7d, now]` instead (`basis.window` = `"24h"` / `"7d"`). `counts`
   is ordered by count desc, then model id asc; `basis.events` = the sum.
3. Counts non-empty → `action: "pin"`, the first entry wins,
   `pinned_at = now`, `expires_at = now + 24h`.
4. Counts empty → `action: "none"`: the wrapper routes this call in-path and,
   when the winner is reachable, pins it with `pin_from_first_call` (`basis
   = {"window": "first_call", "events": 1, "counts": {winner: 1}}`);
   routing unavailable → no pin, the anchor is sent.

### §2.6 Config, app and process names (`config`)

`config.json` = `{"schema": "tryaii.monitor.config/1", "source": "local",
"default_mode": "off", "processes": {name: {"mode", "model"?, "providers":
null}}}`, processes sorted by name. A process entry carries `"model"` (its
managed model, between `mode` and `providers`) only when `mode` is
`managed`. The remote (dashboard) override is `remote.json` (§3.3), read
between env and args below.

- **Mode** (`resolve_mode` → `{"mode", "source", "model"}`), first valid
  value wins — precedence **env > remote > `wrap(mode=)` > `config.json` >
  default**: env `TRYAII_MONITOR_MODE` (`source: "env"`) > `remote.json`
  (`"remote"`: its per-process mode, then its `default_mode`) >
  `wrap(mode=)` (`"args"`) > `config.processes[name].mode`, then
  `config.default_mode` (`"config"`) > `off` (`"default"`). Values are
  trimmed and lower-cased; anything other than `off|stable|dynamic|managed`
  at a level is ignored. `model` = the managed model of the level that set
  the mode: only a per-process entry of `remote.json` / `config.json` whose
  mode is `managed` carries one (trimmed; empty → `null`); env, args and
  `default_mode` levels give `null` (a managed call without a model sends
  the anchor, §2.9).
- **App** (`resolve_app` → `{"app", "source"}`): env `TRYAII_APP` (`"env"`)
  > `wrap(app=)` (`"args"`) > the working directory's name (`"cwd"`) >
  `default`. Values are sanitized: lower-cased, every run of characters
  outside `[a-z0-9._-]` becomes `-`, leading/trailing `-`/`.` stripped; an
  empty result is skipped.
- **Process** (`resolve_process` → `{"process", "source"}`): the per-call
  `tryaii_process` argument (`"call"`) > the `process(name)` context
  (`"context"`) > `wrap(process=)` (`"args"`) > the call site
  (`"call_site"`: `basename(file):function`, or `basename(file):L<line>`
  when the function is unknown; `/` and `\` both separate) > `default`.
  Names are trimmed; empty strings are skipped.
- `set_process_mode(config, process, mode, model=null)` returns a new
  document with the process entry rebuilt as `mode`, `model` (managed
  only), `providers` (kept or `null`), then any other keys it already had;
  leaving `managed` drops the stale model, a `model` with another mode is
  ignored. An invalid mode raises `mode must be off, stable, dynamic or
  managed`; `managed` without a model raises `managed mode needs --model
  <id>`.

### §2.7 Status (`status`)

`aggregate(app, events, pins, config, now, since, env, remote,
upload_cursor, uploaded_events)` over the events with `now − since ≤ ts ≤
now` (`since` ∈ `24h|7d|30d`):

```json
{"schema": "tryaii.monitor.status/1", "app": "shop", "now": "...", "since": "24h",
 "source": {"kind": "local"|"remote", "etag", "fetched_at"},
 "upload": {"state": "off"|"ok"|"error", "events", "last_ok", "last_error"},
 "processes": [{"process", "mode", "mode_source", "managed_model",
                "pinned": {"model", "expires_at"}|null,
                "calls", "errors", "prompt_tokens", "completion_tokens", "total_tokens",
                "est_cost_usd", "est_cost_winner_usd", "savings_usd",
                "swapped_calls", "est_cost_anchor_usd", "realised_savings_usd",
                "top_winner": {"model", "share"}|null, "files": [...]}],
 "totals": {"calls", "errors", "est_cost_usd", "est_cost_winner_usd", "savings_usd",
            "swapped_calls", "est_cost_anchor_usd", "realised_savings_usd"}}
```

- Processes = those with events in the window ∪ `config.processes` ∪
  `remote.processes` ∪ pinned processes, sorted by name (a process set with
  `monitor set` or on the dashboard shows with zero calls).
- `mode`/`mode_source`/`managed_model` via §2.6 with `env`, `remote` and
  `config` (no args level: the CLI cannot see `wrap(mode=)`).
- `source`: `remote.json` present and valid (§3.3) → `{"kind": "remote",
  "etag", "fetched_at"}`; else `{"kind": "local", "etag": null,
  "fetched_at": null}`.
- `upload` from `upload.json` (§3.4): no cursor → `{"state": "off",
  "events": 0, "last_ok": null, "last_error": null}`; `last_error` set →
  `"error"`; else `"ok"`. `events` = the number of `\n` inside the uploaded
  byte prefix of each cursor file (files that are gone count 0) — every
  line the cursor covers, skipped ones included.
- `swapped_calls` = events with `model_used != model_requested` (both
  non-empty). `est_cost_anchor_usd` sums the events' `est_cost_anchor_usd`;
  `realised_savings_usd` = Σ max(0, `est_cost_anchor_usd` − `est_cost_usd`)
  over swapped events carrying both; both are `null` when no event in the
  set carries `est_cost_anchor_usd` (slice-1 records).
- `pinned` only while the pin is valid at `now`.
- `errors` = events with a non-null `error`. Token totals sum the numeric
  usage fields.
- `est_cost_usd` sums every event's `est_cost_usd`; `est_cost_winner_usd`
  and `savings_usd` (= Σ est_cost − Σ est_cost_winner) only over events that
  carry **both** estimates, and are `null` when none do (same rule for
  `totals`).
- `top_winner` = the most frequent non-null `winner` (ties by id asc),
  `share` = its count / events with a winner, 4 dp.
- `files` = sorted distinct `call_site.file` values with `\` shown as `/`.

### §2.8 Policy-aware reachability (TS-170)

The wrapper resolves the saved data-save policy **once per process**
(`resolve_policy("auto")`, `shared/datasave/SPEC.md`) and evaluates it
against the catalog bundle in use, once per bundle (the starter bundle
until warm-up selects the user's catalog, then that one):

- `policy_allowed(policy, origins, model_ids)`: no policy or an inactive
  one → `null` (unconstrained); otherwise `evaluate(...).allowed`. An
  active policy that cannot be evaluated (no `origins.json` in the bundle,
  e.g. the starter bundle; an unknown provider) and an unreadable saved
  policy file **fail closed** → `[]`: tryaii then recommends and swaps
  nothing, the developer's model is sent. Policy evaluation is pure — it
  never loads the embedding model and never touches the daemon.
- `route_allowed(provider, model_ids, anchor, allowed)` — the candidate
  list of the **in-path** request (whose ranking picks what is sent):
  reachable (§2.2) ∩ `allowed`, in catalog order; `null` when both are
  unconstrained (an `openrouter` client without a policy).
- `shadow_allowed(model_ids, allowed)` — the candidate list of the
  **shadow** request (the recommendation): the policy's ids in catalog
  order, `null` without a policy.

Both lists are sent as the daemon's `allowedModels` when the daemon
advertises the `allowedModels` feature, and the returned ranking is
**always** filtered by the list client-side too (older daemons and the
test `route_fn` return everything). A call routes in-path only in
`dynamic` and on a `stable` call without a usable pin; when its
`route_allowed` holds nothing but the anchor (an `other` client) the
in-path route is skipped (nothing could be swapped). When `route_allowed
== shadow_allowed` the in-path outcome doubles as the shadow outcome (one
request); otherwise the shadow request runs on the worker after the call.
The embedding is asked for once (in-path when it ran, else shadow).

The `resolve` suite freezes `policy_allowed`, `route_allowed` and
`shadow_allowed` per case (inputs `policy_allowed` as a list/null, or
`policy` as a `ModelPolicy` dict evaluated against the starter bundle —
which fails closed).

### §2.9 Managed mode

`managed` sends the model assigned to the process (`remote.json` or
`config.json` per-process `model`, §2.6) instead of the anchor:
`managed_target(provider, anchor, model, registry, allowed)` →
`{"model", "target", "route_status"}`:

1. `model` is resolved like the anchor (diagnose §2.1: catalog id, native
   id, alias); `null`/empty/unknown → no target.
2. Resolved model == anchor → sent as written (`target: null`,
   `route_status: null`).
3. Otherwise the target must be reachable from the client (§2.2) and in
   `allowed` (§2.8); then `target` = that id and `route_status: null`.
4. Anything else → the anchor is sent, `route_status:
   "unreachable_managed"` (recorded as `mode: "managed"`, `model_used:
   <anchor native id>`).

A managed call is still shadow-routed and records the winner exactly like
`off` (no in-path routing). It is decided even when the call has no user
text (the shadow route is then `skipped`).

## §3 The local store

`<data_dir>/monitor/<app>/` where `data_dir` = `TRYAII_DRE_DATA_DIR` or
`~/.tryaii` (the SDK's data dir, next to credentials, catalog cache and
daemon state).

| File | Content |
|---|---|
| `events-YYYY-MM-DD.jsonl` | one event per line (§3.1); the day is the UTC date of `ts`; appended with one `O_APPEND` write per line |
| `vectors-YYYY-MM-DD.jsonl` | one vector line per **distinct** `prompt_hash` per day (§3.2); on by default, `TRYAII_MONITOR_VECTORS=0` disables; never uploaded in slice 1 |
| `pins.json` | §2.5, written atomically |
| `config.json` | §2.6, written atomically; re-read by the wrapper when its mtime changes (checked at most every 5 s) |
| `remote.json` | §3.3, the dashboard config (written atomically by the long poll); read through the same 5 s mtime cache |
| `upload.json` | §3.4, the upload cursor (written atomically by the uploader) |
| `upload.lock` | §3.4, exists only while one process uploads this app directory |
| `raw/` | reserved (a future local opt-in for raw prompts); never written |

### §3.1 Event (`tryaii.monitor.event/1`), key order

```json
{"schema":"tryaii.monitor.event/1","event_id":"<uuid4>","ts":"2026-10-11T12:00:00.123Z","app":"shop",
 "process":"purchase item","call_site":{"file":"checkout.py","line":42,"function":"purchase_item"},
 "provider":"openai","model_requested":"gpt-5.5","model_resolved":"openai/gpt-5.5",
 "model_used":"gpt-5.5","mode":"stable","winner":"openai/gpt-5-mini","winner_reachable":true,
 "route_status":"ok","classification":{"category":"coding","subcategory":"debugging","confidence":0.81},
 "taxonomy_version":null,
 "usage":{"prompt_tokens":812,"completion_tokens":140,"total_tokens":952},
 "est_cost_usd":0.004213,"est_cost_winner_usd":0.000731,"est_cost_anchor_usd":0.004213,
 "latency_ms":1432,"stream":false,"error":null,"prompt_hash":"<sha256>","vector":true}
```

v2 fields (the schema id stays `tryaii.monitor.event/1`; readers ignore
unknown and tolerate missing keys, so slice-1 lines stay readable):

- `event_id` — a fresh uuid4 string generated when the event is recorded
  (the upstream idempotency key, API.md §3.2). Second key.
- `taxonomy_version` — the benchmark-taxonomy version of the catalog
  bundle in use: `benchmarks.json`'s top-level `taxonomy_version`, else the
  manifest's, as a non-empty string; `null` when neither has one. **No
  bundle released so far carries either field** (the bundle manifest's
  `version` is the catalog release, not the taxonomy), so every event
  records `null` until the bench server publishes one (TS-179). Right after
  `classification`.
- `est_cost_anchor_usd` (API.md v1.1) — the call's usage priced at the
  anchor (`model_resolved`), 6 dp; equal to `est_cost_usd` when nothing was
  swapped; `null` for an unknown/unpriced anchor or missing usage. After a
  swap `est_cost_usd` is priced at the model sent, so the realised saving
  of the call is `est_cost_anchor_usd − est_cost_usd`. Right after
  `est_cost_winner_usd`.

- `call_site` is `null` when no user-code frame was found; `function` may be
  `null`. `call_site.file` is the path **relative to the application's
  working directory** at the time of the call, with `/` separators; a file
  outside that directory (or on another drive) is its base name only. An
  absolute path names the developer's machine, not the app, and never leaves
  it: the uploader replaces an absolute `call_site.file` (drive letter, root
  slash or `~`) with the base name before the batch is built (TS-226).
- `provider` ∈ `openai|openrouter|other` (§2.2).
- `model_requested` = the id as written in the call; `model_resolved` = the
  anchor catalog id or `null`; `model_used` = the native id actually sent.
- `route_status` ∈ `ok|unavailable|timeout|disabled|queue_full|skipped|
  unreachable_managed` — explains every `winner: null`, except
  `unreachable_managed` (§2.9), which **overrides** the shadow route's
  status whenever a managed call had to send the anchor (the winner is
  still recorded when the shadow route succeeded).
- `classification` = `{"category", "subcategory", "confidence"}` from the
  daemon (`broadCategory` → `category`), or `null`.
- `usage` = the provider's token counts (`null` fields when absent; `null`
  when the provider sent none, e.g. a stream without `include_usage`).
- `est_cost_usd` = `estimate_generation_cost(model_used)` and
  `est_cost_winner_usd` the same usage priced at the winner; `null` for
  unknown/unpriced models or missing usage.
- `error` = `{"status": <int|null>, "class": "<exception class name>"}`
  or `null`. Never message text.
- `prompt_hash` per §2.1; `vector` = whether a vectors line was written for
  this call (false when deduplicated, disabled, or no embedding).

### §3.2 Vector line (`tryaii.monitor.vector/1`), key order

```json
{"schema":"tryaii.monitor.vector/1","prompt_hash":"<sha256>","ts":"...","process":"purchase item",
 "model":"all-MiniLM-L6-v2","dim":384,"v":[0.0123,-0.2,...]}
```

`model` = the daemon's embedding model name; `dim` = `len(v)`; components
rounded to 4 decimals (integral values emitted as integers).

### §3.3 `remote.json` (`tryaii.monitor.remote/1`) and the long poll

The server's `RemoteConfig` (API.md §3.3) **verbatim, in the server's key
order**, plus `"fetched_at"` (the local fetch time, §1.4 format) as the last
key; `indent=2` + `\n`, written atomically. A missing file, invalid JSON or
a `schema` other than `tryaii.monitor.remote/1` means "no remote config"
(exactly the local behaviour).

The long poll runs on its own daemon thread, started by the transport only
when `TRYAII_API_KEY` is set and `TRYAII_MONITOR_REMOTE` is not `"0"`:

- `GET {TRYAII_API_URL}/v1/monitor/config?etag=<url-encoded etag>&wait=25`
  with `Authorization: Bearer <TRYAII_API_KEY>`, HTTP timeout 35 s. The
  first etag is the cached file's (`""` without one).
- `200` with a valid body → `remote.json` is written into **every** app
  directory this process records to, the etag is kept, poll again at once;
  `304` → poll again at once; a `401` → polling stops for the process (one
  log line shared with the uploader, §3.4); anything else (other status,
  invalid body, network) → wait 30 s. Polls are at least 1 s apart.
- The wrapper reads `remote.json` through the same mtime cache as
  `config.json` (checked at most every 5 s), so a change applies on a call
  within ~5 s.

### §3.4 Upload (`upload.json`, `tryaii.monitor.upload-cursor/1`)

```json
{"schema": "tryaii.monitor.upload-cursor/1",
 "files": {"events-2026-10-12.jsonl": 1675},
 "last_ok": "2026-10-12T08:25:00.000Z", "last_error": null}
```

`files` = bytes uploaded per events file (sorted by name); `indent=2` +
`\n`, written atomically after every attempt. The uploader:

- runs on its own daemon thread (not the routing worker, so a slow upload
  never delays recording), started by the transport only when
  `TRYAII_API_KEY` is set and `TRYAII_MONITOR_UPLOAD` is not `"0"`; no key →
  no upload, silently; local recording is unaffected either way;
- first validates the key once per process (`GET /v1/apps/keys/validate`):
  `200` → cached; `401` → upload (and remote) off for the process, one
  warning line `tryaii monitor: TRYAII_API_KEY was rejected (invalid_key);
  upload and remote config are off for this process`; network/other →
  back off and retry. A value that is not an app key (API.md §1 regex) is
  rejected the same way without any request;
- per pass, for each app directory (registration order) takes the
  directory lock `upload.lock` (`O_CREAT|O_EXCL`, the pid inside; a lock
  older than 60 s is taken over), reads the cursor and builds **one** batch
  from the unread bytes of the oldest events file that has any: whole
  `\n`-terminated lines only (a trailing partial line waits); a file
  shorter than its cursor restarts at 0;
- **skips** (never sends, but advances past) lines that are not JSON
  objects of schema `tryaii.monitor.event/1`, lack a uuid `event_id`
  (slice-1 lines), or carry a **forbidden field** — a key named `prompt`,
  `messages`, `response`, `content`, `text` or `raw` at any depth — the
  latter also sets `last_error = "forbidden_field: <name>"`. This is a hard
  rule: prompt text never leaves the machine;
- caps a batch at **500 events** and a **1 MiB** body; the body is the
  envelope `{"schema":"tryaii.monitor.upload/1","batch_id":"<uuid4>",
  "sdk":{"lang":"python"|"node","version":"<sdk version>"},
  "notice_version":"2026-10","events":[` + the stored lines **verbatim**,
  comma-joined + `]}` (minified, that key order; `notice_version` =
  the constant `NOTICE_VERSION`);
- POSTs `/v1/monitor/events` with `Authorization: Bearer <key>`, at most
  one batch every **5 s** per process:
  - 2xx (202) → cursor advanced, `last_ok` = now, `last_error` = null
    (unless the batch skipped a forbidden line), backoff reset;
  - 400 / 413 → the batch is **skipped** (cursor advanced), `last_error` =
    `<error>[: <detail>]` (default `bad_batch` / `too_large`);
  - 401 → upload off for the process (`last_error = "invalid_key"`, one log
    line);
  - 429 → backoff, at least `retry_after` seconds when given
    (`last_error = "rate_limited"`); 5xx / any other status → backoff
    (`last_error = "http <status>"`); network failure → backoff
    (`last_error = "network"`). Backoff: 5 s, doubling, capped at 5 min;
- never deletes or rewrites local files;
- at process exit gets one last pass bounded by **2 s** (after the routing
  worker's own ≤ 2 s drain).

Every failure is swallowed: upload and remote config never affect the
application's calls.

## §4 Fixtures

`fixtures/` mirrors the diagnose machinery: hand-written `cases.json` inputs
per suite, machine-generated `expected` blocks from the Python reference via
`scripts/gen-monitor-fixtures.py` (`--suite`, `--check`). Suites:
`extract` (§2.1), `resolve` (§2.2, §2.8), `winner` (§2.3–§2.4, §2.8–§2.9),
`pin` (§2.5), `config` (§2.6), `status` (§2.7), `cli` (stdout/stderr/exit
goldens of `tryaii monitor`) and `apps` (goldens plus the request log of
`tryaii apps`, §7.4). `corpus/<store>/` holds hand-frozen store snapshots
(`events-*.jsonl`, `pins.json`, `config.json`, `vectors-*.jsonl`, v2:
`remote.json`, `upload.json`) that the `pin`, `status` and `cli` suites
read through the engine's own store reader; the generator never rewrites
them. v2 stores: `managed` (v2 event lines with `event_id` /
`taxonomy_version` / `est_cost_anchor_usd`, a managed process in
config.json, remote.json with etag `e-42`, an upload cursor covering the
first two lines) and `flaky` (only an upload cursor with `last_error`).
Every suite is routing-free, clock-free (`now` is an input) and
network-free.

## §5 The CLI: `tryaii monitor`

Verb-peeling command (not an argparse subparser), byte-identical in both
CLIs; `tryaii help monitor` and `tryaii monitor <verb> --help` print the
frozen `HELP_MONITOR` / `HELP_MONITOR_<VERB>` constants. The `cli` fixture
suite freezes stdout, stderr and the exit code of every case below; the
cross-CLI parity test additionally byte-compares every written file.

### §5.1 Options

`--app <id>`, `--since 24h|7d|30d` (default `24h`), `-n <count>` (default
20), `--now <iso8601>` (default: the current UTC time; normalised with
§1's `ts` format, so `--now 2026-10-11T12:00:00Z` reports
`2026-10-11T12:00:00.000Z`), `--json`. `--flag value` and `--flag=value`
are both accepted; `--` ends option parsing. Any other option →
`error: unknown option: <arg>. Run "tryaii help monitor".` exit 2; a
positional where none is expected →
`error: unexpected argument: <arg>. Run "tryaii help monitor".` exit 2.

`--model <id>` (v2) is accepted by `set` only; any other verb answers
`error: unknown option: --model. Run "tryaii help monitor".` exit 2.

Validation messages (stderr, exit 2): `--since must be 24h, 7d or 30d`;
`--now must be an ISO-8601 UTC timestamp (e.g. 2026-10-11T12:00:00Z)`;
`-n must be a positive integer` (digits only, > 0);
`<flag> needs a value. Run "tryaii help monitor".`.

The data dir is `TRYAII_DRE_DATA_DIR` or `~/.tryaii`; apps are the
subdirectories of `<data dir>/monitor/`, sorted. Paths are printed with
`/` separators.

**App selection** for `set` and `events`: `--app` > `TRYAII_APP` > the only
app on disk. An explicit app without a directory →
`error: unknown app '<app>'`; several apps and none chosen →
`error: --app is required (apps: a, b)`; no apps at all →
`error: --app is required (no monitor records yet)`. All exit 2.

### §5.2 `status`

For every app (or only `--app`, which must exist), one block:

```
tryaii monitor -- app: <app> (last <since>)
<blank>
<table>
<blank>
totals: <calls> calls, <errors> errors, est cost <m>, at winner <m>, savings <m>
```

v2: after the totals line comes `realised: <swapped_calls> swapped calls,
saved <realised_savings_usd>` (money format, `-` for null); every block
(including the `no calls in this window` one) then ends with the source
line — `source: local` or `source: remote etag <etag> fetched
<fetched_at>` — and the upload line — `upload: off`, `upload: ok <events>
events at <last_ok|->` or `upload: error <last_error>` (§2.7).

Blocks are separated by one blank line. The table has the columns
`process  mode  pinned  calls  tokens  est cost  at winner  savings  top winner share`
(two spaces between columns, each column as wide as its widest cell, text
left-aligned, the numeric columns calls/tokens/est cost/at winner/savings
right-aligned, no trailing spaces). `mode` shows `managed (<model>)` for a
managed process (`managed (-)` without a model). `pinned` is the pinned model while the
pin is valid, else `-`; money is `<value>.4f` or `-` for null; `top winner
share` is `<model> <pct>%` with `pct = floor(share × 100 + 0.5)`, or `-`.
After each process row its `files` follow as `    -> <file>` lines. A
window with no events and no configured/pinned processes prints
`no calls in this window` instead of the table and totals. The totals line
uses the §2.7 totals with the same money formatting.

`--json`: with `--app` the §2.7 status document, otherwise a JSON list of
documents (one per app, sorted), `indent=2`. With no apps on disk:
`no monitor records under <data dir>/monitor` on stdout (exit 0), or `[]`
with `--json`.

### §5.3 `set <process> <mode> [--model <id>]`

Writes `set_process_mode` (§2.6) into `<app>/config.json` (indent 2, `\n`)
and prints `<process>: <mode> (app <app>)` — `<process>: managed (<model>)
(app <app>)` for managed; `--json` prints `{"app", "process", "mode"}`
(plus `"model"` for managed) instead. The model is stored as given
(trimmed), resolved only when a call is made (§2.9). Errors (exit 2), in
this order: `usage: tryaii monitor set <process> <mode> [--model <id>].
Run "tryaii help monitor".` when the positionals are not exactly two;
`mode must be off, stable, dynamic or managed`; `managed mode needs --model
<id>`; `--model only applies to managed mode`; `process name must not be
empty`.

### §5.4 `apps`

App names one per line; `--json` a JSON array (indent 2). No apps →
`no monitor records under <data dir>/monitor` (exit 0).

### §5.5 `events`

The newest `-n` events of the selected app across its daily files, oldest
first. Human line:

```
<ts>  <process> | <model_used> | <mode> | winner <winner|-> | <total_tokens|-> tok | <est_cost|-> | <route_status>[ | error <class|status>]
```

`--json` prints each event exactly as stored (one minified line each).
No events → `no events for app <app>`.

### §5.6 Exit codes

`0` success (including the "no records" notices), `1` runtime failure
(`error: <message>`), `2` usage error, unknown app, verb or mode.
Missing verb → `error: missing monitor verb. Run "tryaii help monitor".`;
unknown verb → `error: unknown monitor verb: <verb>. Run "tryaii help monitor".`.

## §6 HTTP: the API helper and its fixture seam

### §6.1 Requests

One helper (`tryaii.monitor.api.request`; Node: the same module name)
serves upload, the long poll and `tryaii apps`: JSON bodies minified
(`separators=(",", ":")`, non-ASCII verbatim) or exact raw bytes (upload),
headers `Accept: application/json`, `User-Agent: tryaii/<version>`,
`Content-Type: application/json` with a body, `Authorization: Bearer <...>`
when authenticated. **Redirects are never followed** (the bearer must not
reach another host; a 3xx is returned as a status). Timeout 10 s (35 s for
the long poll). It returns `{status, body (decoded JSON or null),
headers}` for every HTTP status and raises one network error for every
transport failure.

### §6.2 Environment

| Variable | Meaning |
|---|---|
| `TRYAII_API_KEY` | the app key; enables upload and remote config in the wrapper, and is `apps check`'s default key |
| `TRYAII_API_URL` | API base for upload, remote config and `apps check` (default `https://api.tryaii.com`, trailing `/` dropped, empty = default). User verbs of `tryaii apps` use the signed-in session's stored `api_url` instead (auth contract item 13) |
| `TRYAII_MONITOR_UPLOAD` | `0` disables upload |
| `TRYAII_MONITOR_REMOTE` | `0` disables the remote-config long poll |

### §6.3 The fixture seam (`TRYAII_HTTP_FIXTURE`)

**Both SDKs implement this seam identically** — the `apps` suite and every
cross-CLI parity driver depend on it, and no test may touch the network.
When `TRYAII_HTTP_FIXTURE` names a file (and no in-process opener was
injected), the helper answers from it instead of the network:

```json
{"routes": {
  "GET /v1/apps": [{"status": 200, "body": {"apps": []}}],
  "POST /v1/apps/app_x/keys": [{"status": 409, "body": {"error": "key_limit"}}],
  "GET /v1/monitor/config": [{"status": 200, "body": {...}}, {"status": 304}],
  "POST /v1/monitor/events": [{"network": true}]
}}
```

- The route key is `<METHOD> <path>` — the URL path **without** the query
  string or host.
- Each route's answers are served in order; the **last one repeats**. The
  file is read once per process (the queue position lives in memory).
- An answer is `{"status": <int>, "body": <JSON>|absent}` or
  `{"network": true}` (a transport failure). An unrouted request is a
  transport failure too.
- With `TRYAII_HTTP_FIXTURE_LOG` also set, every request — routed or not —
  is appended to that file **before** it is answered, as one minified JSON
  line `{"method", "path", "query", "auth", "body"}`: `query` = the query
  parameters as an object (blank values kept, `null` when there are none);
  `auth` = `"app_key"` when the bearer matches the app-key regex,
  `"bearer"` for any other bearer, `null` without one — **the credential
  itself is never logged**; `body` = the decoded JSON body or `null`.

## §7 The CLI: `tryaii apps`

Verb-peeling command, byte-identical in both CLIs; `tryaii help apps` and
`tryaii apps <verb> --help` print the frozen `HELP_APPS` /
`HELP_APPS_<VERB>` constants (`APPS_VERB_HELP`), guarded by
`tests/test_parity.py`. Exit codes: `0` ok, `1` server, network or sign-in
failure, `2` usage.

### §7.1 Options and dispatch

`--json`, `--new`, `--label <L>`, `--revoke <key_id>`, `--key <K>`;
`--flag value` and `--flag=value`; `--` ends options. Each verb accepts
only its own options (create, list: `--json`; keys: `--json --new --label
--revoke`; check: `--json --key`); anything else →
`error: unknown option: <arg>. Run "tryaii help apps".` exit 2. A value
flag at the end → `<flag> needs a value. Run "tryaii help apps".`.
Missing verb → `error: missing apps verb. Run "tryaii help apps".`;
unknown verb → `error: unknown apps verb: <verb>. Run "tryaii help apps".`
(both exit 2). Usage errors are detected before any credential is read or
request made.

### §7.2 User verbs (`create`, `list`, `keys`)

They act as the signed-in user: the credentials file of the auth contract
(`<data dir>/credentials.json`); no file → `error: not signed in (run
tryaii login)` exit 1 (no request). The access token is refreshed first
when it expires within 60 s (auth contract; a refresh network failure →
`error: could not reach <api_url>`, a rejected refresh → `error: session
expired or revoked (run tryaii login)`, both exit 1). Requests go to the
session's stored `api_url` with `Authorization: Bearer <access_token>`.
Common outcomes: a transport failure → `error: could not reach
<api_url>`; `401` → `error: session expired or revoked (run tryaii
login)`; any other unexpected status → `error: <error>[: <detail>]` from
the error body, or `error: unexpected response (HTTP <status>)`; all exit 1.

- `create <name>` — exactly one positional (`usage: tryaii apps create
  <name>. Run "tryaii help apps".`), trimmed, 1–64 chars (`app name must be
  1-64 characters`), both exit 2. `POST /v1/apps {"name"}`; `409
  name_taken` → `error: name_taken: you already have an app named
  '<name>'`; on `201` prints `created app <app_id> (<name>)`, then `POST
  /v1/apps/<app_id>/keys {"label": "default"}`; on `201` prints `key:
  <full key>` and on **stderr** `warning: this is the only time the full
  key is shown; store it now (e.g. as TRYAII_API_KEY)`. A failed key call →
  `error: app created but no key: <reason> (retry with tryaii apps keys
  <app_id> --new)` exit 1. `--json`: `{"app": <App>, "key": <key
  response>}` (indent 2) instead of the two stdout lines; the warning stays
  on stderr.
- `list` — no positionals (`unexpected argument: <arg>. Run "tryaii help
  apps".`). `GET /v1/apps`; `--json` prints the `apps` array; none →
  `no apps yet; create one with: tryaii apps create <name>`; else the table
  `app id  name  keys  calls 24h  cost 24h  last event` (monitor table
  rules: two spaces, keys / calls 24h / cost 24h right-aligned, no trailing
  spaces) with `keys_active`, `calls_24h`, `cost_24h_usd` as `.4f`,
  `last_event_at` or `-`.
- `keys <app_id>` — exactly one positional (`usage: tryaii apps keys
  <app_id> [--new [--label <label>] | --revoke <key_id>]. Run "tryaii help
  apps".`); `--new` with `--revoke` → `--new and --revoke cannot be
  combined`; `--label` without `--new` → `--label needs --new` (exit 2).
  - list: `GET /v1/apps/<app_id>/keys`; `--json` prints the `keys` array;
    none → `no keys for app <app_id>`; else the table `key  label  created
    last used  status` (all left-aligned): `<prefix>...` (the summary's
    `prefix`, else `tryaii_<key_id>_`, then three ASCII dots), label or
    `-`, `created_at`, `last_used_at` or `-`, `revoked` when `revoked_at`
    is set else `active`.
  - `--new [--label L]`: `POST /v1/apps/<app_id>/keys` with `{"label": L}`
    (or `{}`); `409 key_limit` → `error: key_limit: an app has at most 2
    active keys; revoke one with tryaii apps keys <app_id> --revoke
    <key_id>` exit 1; on `201` prints `new key <key_id> for app <app_id>`,
    `key: <full key>` and the stderr warning; `--json` prints the response
    (the warning stays on stderr).
  - `--revoke <key_id>`: `DELETE /v1/apps/<app_id>/keys/<key_id>`; on
    `204` prints `revoked key <key_id> (app <app_id>)`; `--json`
    `{"app_id", "key_id", "revoked": true}`.

### §7.3 `check [--key K]`

Key = `--key` (trimmed) else `TRYAII_API_KEY`; none → `no app key: set
TRYAII_API_KEY or pass --key`; not matching the app-key regex → `not an app
key (expected tryaii_<key_id>_<secret>)` (both exit 2, no request). `GET
{TRYAII_API_URL}/v1/apps/keys/validate` with the key as bearer; `401` →
`error: invalid key` exit 1; transport failure → `error: could not reach
<api_url>` exit 1. On `200`:

```
app: <app_name> (<app_id>)
key: <key_id>
scopes: <scopes, comma-space joined | ->
expires: <expires_at | never>
```

`--json` prints the validation body (indent 2).

### §7.4 The `apps` fixture suite

Each case runs the CLI in a fresh temp data dir (`TRYAII_DRE_DATA_DIR`) and
cwd, with `TRYAII_API_KEY`, `TRYAII_API_URL`, `TRYAII_HTTP_FIXTURE` and
`TRYAII_HTTP_FIXTURE_LOG` scrubbed from the inherited environment, then:
`TRYAII_API_URL=https://api.tryaii.test`, `TRYAII_HTTP_FIXTURE` = a file
`{"routes": <input.http or {}>}` and `TRYAII_HTTP_FIXTURE_LOG` = a log file,
both in a **separate** temp dir (never under the data dir), the case's
`env` merged last. `"signed_in": true` writes this `credentials.json` into
the data dir (indent 2 + `\n`; the access token is valid until 2099, so no
refresh request is ever made):

```json
{"version": 1, "api_url": "https://api.tryaii.test",
 "user": {"id": "google-sub-1", "email": "dev@example.com", "name": "Dev Example"},
 "entitlements": ["catalog:full"], "refresh_token": "tair_fixture_refresh",
 "refresh_expires_at": "2099-01-01T00:00:00Z", "access_token": "eyJ.fixture.access",
 "access_expires_at": "2099-01-01T00:00:00Z", "created_at": "2026-10-12T00:00:00Z"}
```

`expected` = `{"stdout_golden", "stderr", "exit_code", "requests"}` where
`requests` is the parsed request log (§6.3), in order. Fixture app keys
have the real shape (`tryaii_k1a2b3c4d5e6_fixtureSecret…`) and are fake.
