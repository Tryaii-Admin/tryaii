# monitor — behavior contract (v2: slice 1 + slice 2)

v2 (slice 2, 2026-10-12) adds: `event_id`, `taxonomy_version` and
`est_cost_anchor_usd` on events (§3.1); the `managed` mode (§2.9);
policy-aware reachability (§2.8, Jira TS-170); `remote.json` (§3.3) and
`upload.json` (§3.4); the HTTP helper and its fixture seam (§6); the
`tryaii apps` CLI (§7); status `source`/`upload`/realised lines (§2.7,
§5.2). The wire contract with the API server and the website is
`shared/monitor/API.md` (v1.1); this SPEC is the SDK side of it.

API.md v1.4 (2026-10-19) adds two optional event fields, `usage.cached_tokens`
and `cache` (the runtime cachelint outcome and a redacted report), §3.1.

v2.2 (2026-10-20, Jira TS-228, API.md v1.5) adds the write side of
prompt-cache usage to the event, on top of v1.4's `usage.cached_tokens`:
`usage.cache_write_tokens`, `usage.cache_writes[].ttl` and the top-level
`cache_ttl_policy` (§3.1b), the `normalize_cache_usage` helper and its
`cache_usage` fixture suite, the status `cached_tokens` /
`cache_write_tokens` sums (§2.7) and the optional cache segments of the
human `events` line (§5.5). The wrappers fill the write fields since
TS-229; the cost estimate learns about them in TS-230; the server stores
them in TS-231.

API.md v1.8 (2026-10-25, Jira TS-307 / TS-308) moves the stable pin to the
server: a `stable` call sends the RemoteConfig `pin` first, a local pin
counts only while its `version` equals the remote `pin_version`, and a
process whose remote mode is not `stable` loses its local pin (§2.5,
`reconcile_pin`); `pins.json` gains the optional `version`. The poller warns
once per local app when the key's app (`app_name`) has another name (§3.3),
and `monitor status` shows it (§2.7, §5.2).

API.md v1.7 (2026-10-24, Jira TS-245) adds the optional event field
`usage.reasoning_tokens` (§3.1) and the optional upload-batch field
`integrations` (§3.4); the Langfuse export that sets it is the section
"Langfuse export".

§10 (2026-10-21, Python only) adds `tryaii claude-code`: a developer's own
Claude Code usage read from its local logs, reported, and imported as events
of the app `claude-code` (a documented exception to CLI parity, §10.6).

§10 update (API.md v1.6, notice 2026-10-24, Python only): cold cache restarts
worked out from the local session grouping and sent as the optional event field
`cache_cold` (§10.5, §10.9; the session itself stays on the machine),
`connect --no-session` (§10.8), and `tryaii claude-code sync` with the
plugin's `SessionEnd` hook (§10.9). Node needs only the new notice version.

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
`route_status` (`unreachable_managed`, `unreachable_pin` or `null`).
Inputs: `provider`, `mode`, `anchor`, `scores`, optional `pin_model`,
`allowed`, `managed_model`, `server_pin_model` (v1.8: a `stable` call with
the server's pin, §2.5 `server_pin_target`).

### §2.5 Pins (`pin`)

`pins.json` = `{"schema": "tryaii.monitor.pins/1", "processes": {name: pin}}`
with `pin = {"model", "pinned_at", "expires_at", "version", "basis":
{"window", "events", "counts"}}`; processes sorted by name. `version`
(API.md v1.8) is optional: a file from before v1.8 has none and still
loads (it counts as 0). `expires_at` is `null` only for an owner's pin from
the server, which `pin_valid` treats as valid until it is replaced.

**The server's pin first (API.md v1.8, TS-307).** Before every call the
wrapper runs `reconcile_pin(remote, pins, process, now, mode, mode_source)`
→ `{"action", "pin", "version", "write", "pins", "reason"}` (`pins` = the
document after the change; `write` = pins.json must be written, so there is
no write per call):

1. `mode` is not `stable`: when `mode_source` is `remote` (the remote config
   set the mode — e.g. `off` after an unpin) and the process has an entry →
   `remove` (the entry is deleted, `reason` `"remote mode is <mode>"`); else
   `none`. A mode from env `TRYAII_MONITOR_MODE`, `wrap(mode=)` or
   `config.json` never deletes a pin.
2. The remote entry of the process has `mode: "stable"` and a `pin` with a
   `model` → `server`: the call sends `pin.model` with
   `server_pin_target(provider, anchor, model, registry, allowed)` =
   `managed_target` (§2.9) with `route_status: "unreachable_pin"` in place
   of `unreachable_managed` (the anchor is sent when the model cannot be);
   no in-path routing (the call is still shadow-routed). The entry becomes
   `{"model", "pinned_at", "expires_at" (the server's, or null), "version":
   pin.version, "basis": {"window": "server", "events": 0, "counts": {}}}`,
   written only when it differs. `reason` = `"server pin (<source>)"`.
3. Otherwise the remote `pin_version` of the process (absent: no remote
   config, no process entry, or a v1.7 server → do not compare) is compared
   with the local pin's `version` (missing = 0); a mismatch drops the entry
   (`write: true`, `reason` `"local pin version <a> != remote pin_version
   <b>"`).
4. A remaining valid pin → `keep`. Else `evaluate` with `pin` = the expired
   pin or null: the wrapper runs `evaluate_pin` below (and, for `none`,
   `pin_from_first_call`) and stores the new pin with `version` = the remote
   `pin_version` or 0, placed after `expires_at`.

The `pin` fixture suite freezes `reconcile_pin` (inputs under `reconcile`,
`pins` inline or `"from_store"`) and the §3.3 warning (inputs under
`app_name_warning`).

`evaluate_pin(events, pin, now, process, models)`:

1. `pin_valid`: the pin has a `model` and `now < expires_at` → `action:
   "keep"`.
2. Else count the **reachable winners** (`winner` non-null and
   `winner_reachable` true) of `process` whose `ts` lies in
   `[now − 24h, now]`; when that holds fewer than **20**, use
   `[now − 7d, now]` instead (`basis.window` = `"24h"` / `"7d"`). `counts`
   is ordered by **the winner rule** below; `basis.events` = the sum.
3. Counts non-empty → `action: "pin"`, the first entry wins,
   `pinned_at = now`, `expires_at = now + 24h`. `reason` = `"most frequent
   winner over <window>"`, plus `" (tie broken by benchmark score|price|model
   id)"` when the first two entries have the same count (naming the first
   key below on which they differ).
4. Counts empty → `action: "none"`: the wrapper routes this call in-path and,
   when the winner is reachable, pins it with `pin_from_first_call` (`basis
   = {"window": "first_call", "events": 1, "counts": {winner: 1}}`);
   routing unavailable → no pin, the anchor is sent.

**The winner rule (TS-295)** — "the winner of a sub-process", shared with the
server's summary (API.md §3.4 `pin_candidate`, computed over the uploaded
events with the same windows, so the dashboard's "Today that is X" is the
model the SDK pins). Order the reachable winners by:

1. count desc;
2. catalog benchmark score (`quality`) desc;
3. blended price (`blended_price_usd_per_m`) asc;
4. model id asc (code-unit order).

A null score or price sorts after every known one. Never list, event or
insertion order. `models` = `model_facts(bundle)`: `{id: {"quality",
"blended_price_usd_per_m"}}` for every model of the catalog bundle in use,
defined exactly as `GET /v1/monitor/models`: `quality` = the mean over the
model's raw `benchmark_scores` of `(score − lo) / (hi − lo)` clipped to
0..1 (`lo`/`hi` from `normalization_ranges.json`; benchmarks without a
range, non-numeric or boolean scores and ranges with `hi ≤ lo` skipped),
half-even 4 dp, `null` without any ranged benchmark; price = `(input_per_1k
+ output_per_1k) / 2 × 1000`, 6 dp, `null` when either is missing.
`models` absent/`null` (no catalog) → steps 2–3 compare equal. The `pin`
fixture suite takes `models` inline or as `"starter"` (the starter catalog's
facts) and freezes the facts themselves (`facts_of`).

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
 "key_app": "<app_name>", "key_app_mismatch": true|false,   // only with app_name
 "upload": {"state": "off"|"ok"|"error", "events", "last_ok", "last_error"},
 "processes": [{"process", "mode", "mode_source", "managed_model",
                "pinned": {"model", "expires_at"}|null,
                "calls", "errors", "prompt_tokens", "completion_tokens", "total_tokens",
                "cached_tokens", "cache_write_tokens",
                "est_cost_usd", "est_cost_winner_usd", "savings_usd",
                "swapped_calls", "est_cost_anchor_usd", "realised_savings_usd",
                "top_winner": {"model", "share"}|null, "files": [...]}],
 "totals": {"calls", "errors", "cached_tokens", "cache_write_tokens",
            "est_cost_usd", "est_cost_winner_usd", "savings_usd",
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
- `pinned` only while the pin is valid at `now` (an owner's pin from the
  server, `expires_at: null`, is valid).
- `key_app` / `key_app_mismatch` (API.md v1.8, TS-308; right after
  `source`): when `remote.json` carries a non-empty `app_name`, `key_app` =
  that name and `key_app_mismatch` = it differs from `app` (exact compare);
  both keys are omitted otherwise.
- `errors` = events with a non-null `error`. Token totals sum the numeric
  usage fields (`prompt_tokens`, `completion_tokens`, `total_tokens`; a
  missing or null field counts 0).
- `cached_tokens` / `cache_write_tokens` (v2.2) sum the numeric
  `usage.cached_tokens` / `usage.cache_write_tokens` of the set and are
  `null` when no event in the set carries that field (pre-v2.2 records, or a
  provider that does not report it) — null, not 0, keeps "unknown" apart
  from "nothing cached". Same rule for `totals`.
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
| `remote.json` | §3.3, the dashboard config (written atomically by the long poll); read through the same 5 s mtime cache, plus an in-process write counter so the poll's own writes apply on the next call |
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
 "usage":{"prompt_tokens":812,"completion_tokens":140,"total_tokens":952,
          "cached_tokens":512,"cache_write_tokens":null,"cache_writes":null,
          "reasoning_tokens":null},
 "cache_ttl_policy":null,
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
- `provider` ∈ `openai|openrouter|other` (§2.2), or `anthropic` for a
  Messages-API client: the Anthropic SDK wrapper (TS-169; `Anthropic`,
  `AnthropicBedrock` and `AnthropicVertex` clients alike, since all answer
  with Messages-API usage) and events imported from Claude Code (§10,
  Python only). `anthropic` is not a §2.2 class: every routing rule treats
  it like `other` (only the anchor is reachable, so `stable` / `dynamic`
  never swap and a different `managed` model records
  `unreachable_managed`). It tells the server that `usage.prompt_tokens`
  excludes the cached and written tokens (§3.1b); the server's hit rate
  adds them back for `anthropic` and `bedrock` events only (API.md §3.2.2).
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
  when the provider sent none, e.g. a stream without `include_usage`), in
  the key order `prompt_tokens`, `completion_tokens`, `total_tokens`,
  `cached_tokens`, `cache_write_tokens`, `cache_writes`, `reasoning_tokens`
  (`cached_tokens` since API v1.4, `cache_write_tokens` / `cache_writes`
  since v2.2, §3.1b, `reasoning_tokens` since API v1.7). A writer always
  emits all seven keys; a reader treats a missing key as `null`.
- `cache_ttl_policy` (v2.2, §3.1b) — right after `usage`.
- `cache_cold` (API.md v1.6, optional) — right after `cache_ttl_policy`, only
  in Claude Code import events (§10.5); the wrappers of both SDKs leave it
  out, and a reader treats a missing key as `null`.
- `est_cost_usd` = `estimate_generation_cost(model_used)` and
  `est_cost_winner_usd` the same usage priced at the winner; `null` for
  unknown/unpriced models or missing usage.
- `error` = `{"status": <int|null>, "class": "<exception class name>"}`
  or `null`. Never message text.
- `prompt_hash` per §2.1; `vector` = whether a vectors line was written for
  this call (false when deduplicated, disabled, or no embedding).

v1.4 fields (API.md §3.2.1 is normative for shape, limits and redaction):

- `usage.cached_tokens` — the provider's cached prompt tokens for the call
  (`usage.prompt_tokens_details.cached_tokens`, or `cache_read_input_tokens` in
  Anthropic-shaped usage); `null` when the provider reported nothing. Key order
  inside `usage`: `prompt_tokens`, `completion_tokens`, `total_tokens`,
  `cached_tokens`. Recorded whether or not cachelint is on; a `usage` object
  from an older SDK simply lacks the key.
- `cache` — only when the call ran the runtime cache lint
  (`wrap(client, cachelint=True)`): `{"outcome": "hit"|"partial"|"miss"|null,
  "report": CacheReport|null}`, the **last key** of the line (after `vector`).
  Absent when cachelint is off. `outcome` is the cachelint pair prediction from
  the previous linted call of the same process in this SDK process (null for the
  first, and for `UNKNOWN` / `AT_RISK`). `report` is attached on the first linted
  call of a process and then when it changed and 60 s have passed since the last
  one attached for that process; the full analysis behind it runs at most once per
  60 s per process (and only when the prompt changed), whether or not the report
  changes, while the cheap `outcome` prediction runs on every linted call. The
  linted prompt is the request's `messages` plus its tool schemas (`tools`, then
  legacy `functions`) as the engine's `tools` input. Finding excerpts are **masked
  at record time** (matched span only, per code point: ASCII digit → `0`;
  whitespace → space; printable ASCII punctuation kept; every other code point,
  including non-ASCII letters, digits, combining marks and emoji → `x`; ≤ 40 code
  points, `…` when cut; API.md §3.2.1 lists the exact sets): the local line never
  holds prompt text, so the uploader sends it unchanged (§9.4). Report sections
  follow API.md §3.2.1's section mapping: a `system` or `developer` message
  (`messages[0]:system`, `messages[2]:developer`) counts as `system` (its findings
  and its tokens, in `system_tokens`); `tools` as `tools`; every other message as
  `messages`.

v1.7 field (API.md §3.2.4, TS-245):

- `usage.reasoning_tokens` — `integer >= 0 | null`: the output tokens the
  model spent on reasoning, read from the response's
  `usage.completion_tokens_details.reasoning_tokens` (OpenAI-compatible chat
  completions; for a stream, the usage of the last chunk, which the wrapper
  asks for with `stream_options.include_usage`). Sync and async, streamed and
  not, are read the same way. `null` when the provider does not report it
  (no `completion_tokens_details`, no `reasoning_tokens` in it, or a value
  that is not a count by §3.1b rule 5); a reported `0` stays `0`. It is
  **part of** `completion_tokens`, as the provider counts it, and is never
  added on top: `completion_tokens`, `total_tokens` and the cost estimates
  do not change. An event-shaped usage that already carries
  `reasoning_tokens` keeps it. The last key inside `usage`. The `cache`
  suite's `usage-reasoning-*` cases freeze it.

### §3.1b Prompt-cache usage (v2.2, TS-228; API.md v1.5)

Four optional fields record what the provider's prompt cache did for the
call, so the dashboard can show per developer how much of the prompt volume
was served from cache and until when a cache is still hot. The read count,
`usage.cached_tokens`, is the v1.4 field above; v2.2 adds the write side.
The schema id stays `tryaii.monitor.event/1`: readers tolerate missing
keys, every older line stays valid, and the `cache` block (v1.4, the lint
outcome and report) is untouched and stays the last key.

| Field | Type | Meaning |
|---|---|---|
| `usage.cached_tokens` | int \| null | input tokens the provider **read** from its prompt cache for this call — always the read count, never a write count |
| `usage.cache_write_tokens` | int \| null | input tokens the provider **wrote** to its cache on this call, in total |
| `usage.cache_writes` | list \| null | the write broken down by lifetime: `[{"tokens": int, "ttl": <ttl>}, ...]`, in the provider's order; `null` when the provider gives no breakdown (then `cache_write_tokens` holds the total) |
| `cache_ttl_policy` (top level) | `<ttl>` \| null | the cache lifetime the **request** asked for (e.g. Anthropic `cache_control.ttl`, OpenAI `prompt_cache_retention`, Bedrock `cachePoint.ttl`), `null` when it asked for none |

`<ttl>` ∈ `"5m" | "1h" | "30m" | "24h" | "explicit"`. The first four are
provider-promised lifetimes measured from the write (Claude 5 m and 1 h,
GPT-5.6+ 30 m minimum, OpenAI extended retention 24 h); `"explicit"` is a
cache the developer created and manages (Gemini `cachedContents`), whose
expiry is known to the developer and not to the call.

**Normalisation rule** (applied by `normalize_cache_usage`, below, and by
`build_event`, which re-emits whatever it is handed in contract order):

1. `cached_tokens` is always the **read** count. A provider's
   "cache hit" / "cached" field maps here; nothing else does.
2. `prompt_tokens` is recorded **unchanged**, as the provider reported it.
   Whether it already includes the cached tokens is a per-provider
   convention (table below) that TS-230's cost estimate applies; the event
   never re-derives one from the other.
3. A provider that reports writes only in aggregate →
   `cache_write_tokens` = that total, `cache_writes` = `null`. A provider
   that reports a breakdown → `cache_writes` = the breakdown (entries with
   zero tokens dropped, so an empty breakdown is `[]`, not `null`), and
   `cache_write_tokens` = its aggregate field when it has one, else the sum
   of the entries.
4. A field the provider does not report is `null`, **not 0**. A field the
   provider reports as 0 is `0` (below the provider's minimum prompt size
   caching silently does nothing and the fields come back 0 — that is a
   real observation).
5. Booleans are never token counts; numbers are truncated to integers;
   negative or non-numeric values count as absent.
6. `cache_writes[].ttl` is lower-cased and must be one of `<ttl>`; an entry
   with an unknown ttl is dropped. `cache_ttl_policy` outside `<ttl>` is
   recorded as `null`.

**Provider field map** — `normalize_cache_usage(provider, raw_usage)` →
`{"cached_tokens", "cache_write_tokens", "cache_writes"}` (that key
order; every key present). `provider` is one of the ids below; an unknown
id answers all `null`. `raw_usage` is the provider's usage object as a dict
(an object with attributes is read the same way); Gemini may pass either
the response or its `usageMetadata`. The ids are the *API shapes*, not the
event's `provider` class (§2.2): an Azure or OpenRouter client has class
`other` / `openrouter` but the `openai` **shape**.

| `provider` id | Read (→ `cached_tokens`) | Write (→ `cache_write_tokens`) | Breakdown (→ `cache_writes`) | `prompt_tokens` convention |
|---|---|---|---|---|
| `openai` (Chat Completions; also Azure OpenAI and any OpenAI-compatible host) | `prompt_tokens_details.cached_tokens` | `prompt_tokens_details.cache_write_tokens`, else top-level `cache_write_tokens` (GPT-5.6+; absent → null) | null | includes cached tokens |
| `openai_responses` (Responses API) | `input_tokens_details.cached_tokens` | `input_tokens_details.cache_write_tokens`, else top-level `cache_write_tokens` | null | `input_tokens` includes cached tokens |
| `openrouter` | `prompt_tokens_details.cached_tokens`, else top-level `cached_tokens` | `prompt_tokens_details.cache_write_tokens`, else top-level `cache_write_tokens` | null | includes cached tokens; `cache_discount` (money) is not recorded |
| `azure` | same as `openai` | same as `openai` (no write field before GPT-5.6) | null | includes cached tokens |
| `anthropic` (Messages API) | `cache_read_input_tokens` | `cache_creation_input_tokens` | `cache_creation.ephemeral_5m_input_tokens` → `"5m"`, `cache_creation.ephemeral_1h_input_tokens` → `"1h"` (that order); no `cache_creation` object → null | `input_tokens` **excludes** cached and written tokens |
| `bedrock` (Converse) | `cacheReadInputTokens` | `cacheWriteInputTokens` | `cacheDetails[]` → `{"tokens": inputTokens, "ttl": ttl}` in the given order | `inputTokens` **excludes** cached tokens |
| `gemini` (generateContent) | `usageMetadata.cachedContentTokenCount` (also `cached_content_token_count`) | null (explicit caches are written by `cachedContents.create`, outside the call) | null | `promptTokenCount` includes cached tokens |

**Wrappers (TS-229).** The `openai` wrapper fills the cache columns with
`usage_from_provider(shape, raw_usage)`. It takes the token counts as
`usage_from_response` reads them and adds each column that
`normalize_cache_usage` finds. The shape is `openai` for class `openai`
and `openrouter` for class `openrouter`. For class `other` it tries
`openai` first, then `anthropic` for any column still null, which covers
OpenAI-compatible proxies in front of Claude. The wrapper fills
`cache_ttl_policy` with `cache_ttl_policy_from_request(kwargs)`, which
takes `prompt_cache_retention`, then the same key inside `extra_body`
(`24h` gives `"24h"`; `in_memory` and any other value give null). Without
a retention it takes the longest explicit `cache_control.ttl` on the
messages, their content parts or the tools. A `cache_control` without a
`ttl` gives null: a default is never assumed. The Anthropic wrapper
(TS-169, event `provider` `anthropic`) reads the `anthropic` shape only:
`prompt_tokens` = `input_tokens` as reported (excluding the cache),
`completion_tokens` = `output_tokens`, `total_tokens` = their sum, and the
cache columns from `normalize_cache_usage("anthropic", …)`; its
`cache_ttl_policy` is the longest explicit `cache_control.ttl` on the
request, its `tools`, `system` blocks and message content blocks.

TS-229 maps `prompt_tokens` / `completion_tokens` / `total_tokens` of the
non-OpenAI shapes in the wrappers; this helper only covers the cache
columns. The `cache_usage` fixture suite freezes the helper's output per
provider on representative raw usage objects (every row above, plus the
absent / zero / malformed cases).

**How the dashboard will use the lifetimes** (no SDK implementation; TS-231
stores, the website renders): a cache write with an exact lifetime is hot
until `event.ts + lifetime` (`5m`, `1h`, `30m`, `24h`), so the per-developer
hot-cache bar is the union, over recent events, of the intervals `[ts, ts +
ttl]` of each `cache_writes` entry, weighted by its `tokens`; a read on a
later call (`cached_tokens > 0`) shows that the cache paid off but
does not extend the bar (only providers that document refresh-on-read may
be modelled so later, per provider). Writes with `"explicit"` lifetime and
providers with best-effort caches (no `cache_writes`) draw nothing in the
bar: only provider-promised lifetimes are shown. `cache_ttl_policy` tells
which lifetime the developer asked for, so the bar can flag a request that
asked for `1h` and was answered with a `5m` write.

### §3.1a Embedding model state (`<data_dir>/embedding-model.json`)

Python since TS-252, Node since TS-253. Written by the transport's warm-up when
the embedding model file is not available (no daemon is started; calls are
recorded as `unavailable`), deleted when it is found again:
`{"model", "reason", "since" (ISO Z, first seen), "last_warned" (local
YYYY-MM-DD)}`. The warning is logged at most once per machine per day for
the same reason. While the file exists, `tryaii monitor status` (text, not
`--json`) prints one last line after every app block: `classification: off
since <since> (<reason>)`. The fixtures never create it.

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
  first etag is the cached file's (`""` without one). The **first** poll of
  the process sends `wait=0`, so the server answers at once (`200` when the
  cached etag is stale or missing, else `304`); every later poll sends
  `wait=25`.
- `200` with a valid body → `remote.json` is written into **every** app
  directory this process records to, the etag is kept, poll again at once;
  `304` → poll again at once; a `401` → polling stops for the process (one
  log line shared with the uploader, §3.4); anything else (other status,
  invalid body, network) → wait 30 s. Polls are at least 1 s apart.
- The wrapper reads `remote.json` through the same mtime cache as
  `config.json` (checked at most every 5 s) **plus** a per-process write
  counter bumped by every `remote.json` write: an answer the poll of this
  process wrote applies on the **next call**, with no 5 s delay and no
  reliance on mtime resolution (TS-296). A `remote.json` written by another
  process still applies within ~5 s.
- **App name** (API.md v1.8, TS-308): when an answer names an `app_name`
  that differs from the name of an app directory it applies to (exact
  string compare), the SDK logs ONE warning per local app per OS process,
  on the monitor's warning channel (Python: logger `tryaii.monitor`,
  `WARNING`; Node: stderr, like the rejected-key line):
  `tryaii monitor: app="<local>" uploads to app "<app_name>" (the app of
  TRYAII_API_KEY). To use a separate app, run: tryaii apps create <local>
  and use its key.` It is checked on every `200` and, from the cached
  `remote.json`, on every `304` (a mismatch that never changes is otherwise
  never answered with a `200` again). The key decides the dashboard app;
  `app=` is only the local name.
- **First call** (TS-296): while the poller's first answer has not come,
  the first call(s) of the process wait for it — `200`, `304`, error or
  `401`, whichever — but at most **500 ms**, once per process: the bound
  starts with the first call that waits, concurrent first calls share it,
  and after the first answer or the bound no call ever waits again (a flag
  check). The mode is then resolved again with the fresh `remote.json`. No
  wait without a poller (no key, `TRYAII_MONITOR_REMOTE=0`, a malformed key)
  or when env `TRYAII_MONITOR_MODE` sets the mode. A slow or unreachable
  server costs the first call at most 500 ms and it runs with the cached /
  default mode. Python: the sync path blocks on a `threading.Event`; the
  async path waits in the default executor (never on the event loop). Node:
  the first call's `create` returns a plain `Promise` (as calls that route
  in-path already do) and the bound timer is ref'd, so a script awaiting it
  does not exit early.

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
  `TRYAII_API_KEY` is set and no opt-out is set (§9.1); no key →
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
  "notice_version":"2026-10-24","events":[` + the outgoing lines (§9.4),
  comma-joined + `]}` (minified, that key order; `notice_version` =
  the constant `NOTICE_VERSION`); Python only, API.md v1.6: a directory
  registered with a source (only the Claude Code importer's,
  `register(dir, source="claude-code-import")`, §10.5) gets
  `"sdk":{"lang":"python","version":"…","source":"claude-code-import"}`; every
  other upload leaves `source` out; API.md v1.7, both SDKs: while the
  Langfuse export is on (§11.1 `settings`: the switch `TRYAII_LANGFUSE` is `on`, also `1`,
  `true`, trimmed, any case, read by `config.langfuse_enabled()` /
  `langfuseEnabled()`; an app key in `TRYAII_API_KEY`; both Langfuse keys),
  `"integrations":["langfuse"]` follows `notice_version`; otherwise the key
  is absent. It is read at batch time (Python `upload.batch_integrations`,
  Node `batchIntegrations`);
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
`pin` (§2.5), `config` (§2.6), `status` (§2.7), `cache_usage` (§3.1b,
v2.2: `normalize_cache_usage(provider, raw_usage)` per case), `cli`
(stdout/stderr/exit goldens of `tryaii monitor`) and `apps` (goldens plus
the request log of `tryaii apps`, §7.4). `corpus/<store>/` holds hand-frozen store snapshots
(`events-*.jsonl`, `pins.json`, `config.json`, `vectors-*.jsonl`, v2:
`remote.json`, `upload.json`) that the `pin`, `status` and `cli` suites
read through the engine's own store reader; the generator never rewrites
them. v2 stores: `managed` (v2 event lines with `event_id` /
`taxonomy_version` / `est_cost_anchor_usd`, a managed process in
config.json, remote.json with etag `e-42`, an upload cursor covering the
first two lines) and `flaky` (only an upload cursor with `last_error`).
v1.8 store: `keyapp` (a `remote.json` whose `app_name` is `shop` and whose
`summarize` is pinned by the owner, and the matching `pins.json` entry with
`expires_at: null` and `version: 3`; read by the `status` and `cli`
suites).
v2.2 store: `cached` (event lines with the §3.1b cache fields in every
combination — an OpenAI 30 m write with policy, an Azure read without a
write field, a 5 m + 1 h breakdown, an OpenRouter zero read, plus one v2
line without any cache key — read by the `status` and `cli` suites).
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

v1.8 (TS-308): when the status document has `key_app_mismatch: true`, the
line `  key app: "<key_app>" (differs from the local name)` (two leading
spaces) follows the source line. `--json` carries `key_app` /
`key_app_mismatch` as in §2.7.

TS-294 (catalog contract v1.4): after every block (and after the `classification: off ...`
line when there is one) comes one machine-wide line naming the catalog this
machine routes on and why: `catalog: full (login)`, `catalog: full (app key)`
or `catalog: starter`. It is read from local files only (no network, no
environment): `full (...)` when `<data dir>/catalog/state.json` names a
`version` whose cached full bundle loads and verifies, `(app key)` when the
state's `auth` is `app_key` and `(login)` otherwise; anything else is `starter`
(docs/catalog/CONTRACT-catalog-v1.md v1.4, section 5). Not printed with
`--json` or when there are no apps on disk.

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
<ts>  <process> | <model_used> | <mode> | winner <winner|-> | <total_tokens|-> tok[ | cached <n>][ | cache write <n>] | <est_cost|-> | <route_status>[ | error <class|status>]
```

v2.2: `cached <n>` follows the token segment when
`usage.cached_tokens` is a number (0 included), and `cache write <n>`
when `usage.cache_write_tokens` is; a line without the fields (or with
`null`) prints neither segment, so pre-v2.2 output is unchanged.

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

## §9 Privacy: notice, upload switch, keyed hash (contract v1.3)

Module `privacy` (`tryaii/monitor/privacy.py`, `src/monitor/privacy.ts`),
byte-identical in both SDKs. API side: API.md §7.

### §9.1 The upload decision

`upload_decision(env, root)` → `(enabled, reason)`, first match wins:

| Condition | `reason` |
|---|---|
| `TRYAII_MONITOR_UPLOAD` is `0`, `false`, `off` or `no` (any case) | `TRYAII_MONITOR_UPLOAD=0` |
| `TRYAII_TELEMETRY` is `0`, `false`, `off` or `no` | `TRYAII_TELEMETRY=0` |
| `DO_NOT_TRACK` is `1`, `true`, `yes` or `on` | `DO_NOT_TRACK=1` |
| `<root>/settings.json` says `"upload": false` | `tryaii monitor upload off` |
| otherwise | enabled, `null` |

`root` is `<data dir>/monitor`. The environment rows decide whether the
transport starts the uploader at all; the settings file is read on every
pass. The remote switch comes on top, per app directory: while `remote.json`
has `"upload": false` a pass skips the directory and sets `last_error =
"upload_disabled"` (`"upload_off"` for the local setting); a `403
{"error":"upload_disabled"}` answer sets the same error, keeps the cursor and
retries after `BACKOFF_MAX` (5 min). Nothing is lost while paused: the
cursor resumes where it stopped.

`settings.json`: `{"schema": "tryaii.monitor.settings/1", "upload": bool}`,
indent 2 + `\n`, written atomically.

### §9.2 The notice

`NOTICE_VERSION = "2026-10-18"` and `NOTICE_TEXT` (four lines, each ending
`\n`):

```
[tryaii] monitor records sync to your TryAii account.
Sent: model names, token counts, costs, timings, file paths, a task profile.
Never sent: your prompts, responses or API keys.
Turn off: tryaii monitor upload off   Details: https://tryaii.com/privacy-policy#cli-sdk
```

It is shown only to a person: the stream is a TTY and `CI` is unset. Where:

- the uploader, before its first POST from a machine, on stderr, once per
  version (`notice.json` = `{"schema": "tryaii.monitor.notice/1", "version",
  "shown_at"}`); no consent record (an app key is not a person);
- `tryaii apps create` after the key, on stderr, then
  `POST /v1/monitor/consent {"source": "cli"}` (best effort);
- `tryaii monitor upload` and `upload on` (§9.3), on stdout; when stdout is a
  TTY also `notice.json` and the consent POST.

### §9.3 `tryaii monitor upload [status|on|off] [--json]`

`on` / `off` write `settings.json` first. Then: with `--json` the document
`{"upload": bool, "reason": str|null, "notice_version": "..."}` (indent 2);
otherwise `NOTICE_TEXT` + an empty line (not for `off`), then `upload: on` or
`upload: off (<reason>)`. Any other argument: usage error
`usage: tryaii monitor upload [on|off]. Run "tryaii help monitor".`, exit 2.
`tryaii monitor status` ends each block with `upload: off (<reason>)` instead
of the cursor line while the decision says off (the `--json` document is
unchanged). The `cli` fixture suite never inherits `TRYAII_MONITOR_UPLOAD`,
`TRYAII_TELEMETRY`, `DO_NOT_TRACK` or `CI` from the caller.

### §9.4 Outgoing lines and the keyed hash

The uploader sends each kept line as stored except for two fields, then
re-serialises it with the store's own line format (§1.2): an absolute
`call_site.file` becomes its base name (TS-226), and `prompt_hash` becomes
`HMAC-SHA256(key, <stored prompt_hash as ASCII>)`, lowercase hex. `key` is 32
random bytes kept as 64 hex characters + `\n` in
`<data dir>/monitor/upload-hash.key` (mode 0600 where the OS has modes),
created on the first upload and never sent. Local files keep the plain
sha256 (§2.1), so local tools and fixtures are unchanged; the server sees a
hash it cannot test guesses against, stable for one installation.

## §10 Claude Code import (Python)

`tryaii claude-code` reads the developer's **own** Claude Code usage from the
session logs Claude Code keeps on the machine, reports it, and can import it
as monitor events of the app `claude-code`, which the uploader of §3.4 sends
like any other app's. Python only for now (§10.6). Code:
`packages/python/tryaii/claude_code/` and `tryaii/cli/claude_code.py`; docs:
`docs/cli/claude-code.md`.

### §10.1 Source and privacy (hard rule)

- Logs: every `*.jsonl` under `~/.claude/projects` (`$CLAUDE_CONFIG_DIR/projects`
  when set; `--projects-dir` overrides): `<project>/<session>.jsonl` and
  subagents under `<project>/<session>/subagents/agent-<id>.jsonl`, each with an
  optional sibling `agent-<id>.meta.json`.
- From a log line only `type`, `timestamp`, `isSidechain`, `agentId`, `cwd`, `requestId`,
  `message.id`, `message.model` and `message.usage` are read (a line is parsed
  only when it can be an assistant line with usage, and dropped right after);
  from a meta file only `agentType`. Message text, tool inputs and outputs, file
  contents and the meta `description` are **never** printed, stored or uploaded.
  The command never writes into Claude Code's directory. The only verb that
  reads message text is the opt-in `analyze` (§10.7), on this machine only.
- The **project label** is the base name of the call's `cwd` (`tryaii-dev`;
  `unknown` without one), never a full path and never the encoded project folder
  name, which contains the OS user name (TS-226). For a git worktree (a `cwd`
  whose `.git` is a **file** with `gitdir: <main>/.git/worktrees/<name>`, or
  `<main>.git/worktrees/<name>` for a bare repository; Claude app worktree
  folders included) it is the main repository's base name, so all worktrees of
  a repo are one project. Only `<cwd>/.git` is read, no git process; a `cwd`
  that no longer exists, a normal checkout or a submodule keeps its base name.
  A name that looks like an opaque id (an optional short word prefix such as
  `local_`, then a UUID, 16 or more hex digits or a ULID, with at least one
  digit) is `unknown`. `report`, `analyze`, `connect` and `sync` use the same
  label; what leaves the machine is the label or `null` for `unknown`
  (`reader.project_value`; the dashboard shows `null` as "Other"). Paths in human output show the
  home directory as `~`.
- The **session** of a call is internal only: the stem of the main file
  (`<session>.jsonl`; a subagent file under `<session>/subagents/` belongs to
  it), read from the file path, never from a log line (`Call.session`). It is
  a grouping key for cold cache restarts (§10.9) and nothing else: it is never
  printed, stored, hashed or uploaded, and no event, report, `--json` document,
  `sync` request or local state file carries it. Output names a session only by
  its ordinal (`Session N`, numbered by its first main call in the window).

### §10.2 Calls

- A call = the `type: "assistant"` lines with one `message.id` (fallback
  `requestId`), **across all files** (a resumed session copies earlier lines
  into a new file); every usage field is the **maximum** over those lines
  (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `cache_creation.ephemeral_5m_input_tokens`,
  `cache_creation.ephemeral_1h_input_tokens`,
  `output_tokens_details.thinking_tokens`); its time is the earliest line's
  `timestamp`. Model `<synthetic>` lines are skipped (no API call).
- **Process**: `main` for the main conversation; for a subagent (a file under
  `subagents/`, or a line with `isSidechain: true`) the meta file's
  `agentType` (`Explore`, `general-purpose`, a custom agent's name), else
  `subagent`.

### §10.3 Cost (estimate at API list prices)

The model id is resolved to a catalog id with the anchor resolver (§2.2,
`claude-opus-5-5` → `anthropic/claude-opus-5.5`); the catalog is the one the CLI
selects: the signed full catalog when it is cached after `tryaii login`, else the
starter. Input and output prices come from the catalog. A Claude model the
catalog does not price falls back to Anthropic's list price from a small table
in `tryaii/claude_code/pricing.py` (`FALLBACK_LIST_PRICES`, USD per MTok:
`claude-haiku-5-5` 0.10 / 0.50, and 0.50 / 2.50 when the
call's prompt, input + cache write + cache read, exceeds 100K tokens), shown as
"list price (fallback)". A model in neither is **unpriced**: cost `null`, listed
as unpriced, never guessed.

In USD: `input_tokens` × input + 5-minute cache writes × 1.25 × input + 1-hour
cache writes × 2 × input + cache reads × the cache-read price + `output_tokens` ×
output. The cache-read price is the catalog entry's `pricing.cache_read_per_1k`
when present, else input × the model's multiplier in `CACHE_READ_MULTIPLIERS`
(Anthropic list prices, 2026-10-06): `claude-opus-5-5` 0.05,
`claude-fable-5-1` and `claude-mythos-5-1` 0.025, every other model 0.1. Both
tables are a stopgap and go away once the catalog carries cache prices and these
models (the cache-price-sync work under TS-227 in Tryaii-BT). A write without a
lifetime breakdown (or the part of the total the breakdown does not cover) is
priced as 5-minute. `output_tokens` already includes thinking. Labelled
"estimate at API list prices" everywhere: a subscription does not bill per token.

### §10.4 `report [--days N] [--json] [--projects-dir PATH]`

Calls with `now − N days ≤ time ≤ now` (default 30). Totals (calls, unpriced
calls, tokens: input, cache write 5m / 1h, cache read, output, thinking), the
**cache hit share** = cache read / (input + cache write + cache read), the cost
split (input, cache write 5m, cache write 1h, cache read, output), per model,
main vs subagents, per process (with the median output per call), the top 10
projects, `days` (one row per UTC date from the start date to the end date,
including dates with no calls as zero rows; the edge dates are partial: calls,
unpriced calls, main / subagent calls, cache hit share, est cost), the unpriced models, the subagent runs and "where a cheaper model may
fit". A **run** is one subagent invocation: the calls of one subagent file, or of
one `agentId` for sidechain lines elsewhere. Per subagent type the report gives
runs, calls and the median and p90 (nearest rank) across runs of each run's total
output and total cost. A type is suggested for Haiku when it has at least 3 runs,
its median run output is below 2,000 tokens and its p90 below 8,000: the usage
of its priced non-Haiku calls priced at the newest priced Haiku (catalog or
fallback table) is the **upper bound** of what `model: haiku` in that subagent's
definition saves (top 5 by saving). **Cold cache restarts** (`cold_restarts`,
§10.9): the rule, main sessions, sessions with a restart, count, unpriced
count, cost, by reason, the top 10 sessions by cost (as `Session N`) and the 5
largest restarts (time, session ordinal, model, reason, ttl, pause, tokens
written, cost). `--json` prints the document
`tryaii.claude-code.report/1` (indent 2). Human output is not frozen by
fixtures.

### §10.5 `connect` and `disconnect`

`connect [--dry-run] [--days N] [--yes] [--no-live | --live-only] [--no-session]
[--projects-dir PATH] [--claude-dir PATH]` has two parts, each with its own `[y/N]` (`--yes` answers
both): the backfill below, and live telemetry (§10.8). `--no-live` skips live
telemetry, `--live-only` the backfill; `--no-session` (live part only, not with
`--no-live`) is in §10.8. The backfill (N ≤ 90: the
server skips events older than its 90-day retention, API.md §4) builds one
event per call in §3.1 key order:

| Field | Value |
|---|---|
| `event_id` | uuid5(`NAMESPACE_URL`, `"tryaii:claude-code:" + requestId`), the rule the server applies to live telemetry (§10.8); a call without `requestId` falls back to uuid5(`5b0f6a55-3c1e-4b8e-9d6c-2a7f1c0de001`, call key). Deterministic, so a re-run never duplicates (locally it is skipped, also under its pre-v1.5 fallback id; the server ignores a known `event_id`) |
| `ts` | the call's time (§1.4 format) |
| `app` / `process` | `claude-code` / §10.2 |
| `call_site` | `{"file": <project label>, "line": null, "function": null}`; `file` is `null` for `unknown` or an id-like name (§10.1) |
| `provider` | `anthropic` (outside the §2.2 classes; routed like `other`, and marks `prompt_tokens` as excluding the cache, §3.1) |
| `model_requested`, `model_used` | the log's model id; `model_resolved` = the catalog id or `null` |
| `mode`, `winner`, `winner_reachable`, `route_status`, `classification`, `taxonomy_version` | `off`, `null`, `false`, `skipped`, `null`, `null`; for a call with an `analyze` result (§10.7): `off`, the Anthropic answer (`null` when the task's model is not in the catalog), `true` when there is one, `ok`, the task profile `{category, subcategory, confidence}`, the catalog's taxonomy version |
| `usage` | `prompt_tokens` = `input_tokens` (Anthropic: excludes cached and written tokens, §3.1b), `completion_tokens` = `output_tokens`, `total_tokens` = input + cache write + cache read + output; the cache columns from `normalize_cache_usage("anthropic", …)` |
| `cache_ttl_policy` | `null` (the logs do not carry the request's `cache_control`) |
| `cache_cold` | (API.md v1.6) right after `cache_ttl_policy`: `true` for a main-thread call that was a cold cache restart (§10.9), `false` for any other main-thread call, `null` for a subagent call. The session it was worked out from is not sent. Other SDK events leave the key out (a missing key reads as `null`) |
| `est_cost_usd` = `est_cost_anchor_usd` | §10.3, 6 dp, `null` when unpriced; `est_cost_winner_usd` `null`, or with an `analyze` result the same usage priced at the Anthropic answer |
| `latency_ms`, `stream`, `error`, `prompt_hash`, `vector` | `null`, `false`, `null`, `null` (no prompt text is ever seen), `false` |

While live telemetry is on, only calls before its cutoff `live_since` (§10.8)
are imported. New events (an `event_id` not yet in `<data_dir>/monitor/claude-code/`) are
appended to the app's daily files (§3), `<data_dir>/claude-code/connect.json`
(`tryaii.claude-code.connect/1`: `app`, `connected_at`, `last_import_at`,
`imported_events`) is written, and, when `TRYAII_API_KEY` holds an app key and
the upload decision (§9.1) is on, the §3.4 uploader sends the directory's
unread lines (batches labelled `sdk.source` `"claude-code-import"`, API.md v1.6) in the foreground (one batch per 5 s, at most 10 minutes; the rest
goes on the next run). Without a key the events stay local and the command
says how to get one (`tryaii login`, `tryaii apps create claude-code`).
`--dry-run` prints the counts, the date range, the field list and one event
with `event_id` and `call_site.file` redacted, and writes nothing. Without
`--yes` the command asks; with no terminal it refuses (exit 2).

`disconnect [--yes]` removes the settings keys live telemetry added (§10.8),
then `<data_dir>/monitor/claude-code/` and `<data_dir>/claude-code/` (nothing
else; never Claude Code's logs). Uploaded
events stay on the account until the app is deleted there. There is no
background import: nothing imports between two `connect` runs. (`sync`,
§10.9, which the plugin's `SessionEnd` hook starts, only annotates calls that
came by live telemetry.)

### §10.6 Parity exception

`claude-code` exists in the Python CLI only, a documented exception to the
byte-identical CLI rule: it is not in the shared `HELP` / `COMMAND_HELP`
tables (`tests/test_parity.py`), its help text lives in
`tryaii/cli/claude_code.py`, and `tryaii help claude-code` and
`tryaii claude-code --help` print it. Its events are ordinary §3.1 lines, so
the Node SDK's `tryaii monitor status --app claude-code` reads them as well.
Tests: `packages/python/tests/test_claude_code.py` (synthetic logs; asserts no
message text reaches any output, event, store file or upload body, `analyze`
included).

### §10.7 `analyze [--days N] [--json] [--yes] [--projects-dir PATH]` (opt-in, TS-288)

Classifies the developer's Claude Code tasks **on this machine** and says which
model is good enough for each kind. Code: `tryaii/claude_code/tasks.py`
(text), `analysis.py` (classification and advice), `secret_scan.py`.

**Consent.** The command first prints what it reads (to stderr with `--json`)
and asks `[y/N]` unless `--yes` or a stored consent of the current
`CONSENT_VERSION` exists; with no terminal it refuses (exit 2), and on a "no"
it reads no text (exit 2). The consent is stored in
`<data_dir>/claude-code/consent.json` (`tryaii.claude-code.consent/1`:
`scope`, `version`, `granted_at`, `method` = `prompt` | `--yes`, `reads`) and
copied into the result. `disconnect` removes it with the rest of
`<data_dir>/claude-code/`.

**What text is read** (and only for the call window of §10.4):

- a **main-thread turn**: the text parts of a `type: "user"` line of a main
  session file that is not a sidechain line. Not a turn (counted): a line with
  any `tool_result` part; `isMeta` or `isCompactSummary`; an `origin.kind`
  other than `human` (task notifications, agent messages); a line that is
  empty or only injected context once `<system-reminder>` blocks are cut out
  (it starts with a command / local-command / bash / task-notification wrapper
  or `[Request interrupted`). `<system-reminder>` blocks are cut from the kept
  text. The turn's calls are the main-thread assistant calls after it in the
  same file up to the next turn (a call belongs to the first turn that claims
  it);
- a **subagent run**: the text of the run's first `type: "user"` line (the
  instruction it was started with); its calls are the run's calls (§10.4).

Never tool inputs or results, file contents or the full call context. The
text is cut to `MAX_PROMPT_LENGTH` code points (§2.1). A text that matches a
secret pattern (`secret_scan.SECRET_PATTERNS`: the cache linter's
`secret-like` rule, provider key shapes, private-key blocks, password and
token assignments, JWTs, credentials in URLs) is not embedded; the turn or run
is skipped and counted (`skipped_secret`).

**Classification.** Each text is routed in-process like a monitor route
(`Router.route`, the CLI's catalog, every model, default priorities) with the
local embedding model (`config.embedding_model`; no network beyond the SDK's
own catalog selection). The task's model (the anchor) is the model its calls
spent most on. Two answers per task:

1. the **Anthropic answer**: the Anthropic models of the ranking (∩ the
   data-save policy, §2.8) with `qualityScore ≥ anchor − 0.05` and a blended
   price ≤ the anchor's (the §2.3 band) are the task's *good-enough set*; the
   cheapest is the answer (`method: band`, or `anchor` when it is the task's
   own model). Catalog variants (an id with a `:` suffix: `:batch`, `:free`,
   `:thinking`, `:extended`, ...) are left out of the ranking first: Claude
   Code cannot be set to one, and a variant shares its model's display name
   (`pricing.claude_code_model`). An `analysis.json` stored before 0.8.3 is
   read without them (a process advice that named one is withdrawn until the
   next run). An anchor that is not ranked or unpriced gives no answer
   (`unranked`);
2. the **overall winner**: §2.3 `pick_winner` over the policy-filtered
   ranking, any provider. Informational only; it stays on the machine.

The result is attached to every call of the task.

**Advice** per process (`main`, each subagent type), every share weighted by
the tasks' estimated cost (§10.3; by count when nothing is priced): the
class shares (`category/subcategory`), for each Anthropic model the share of
cost it is good enough for, and the advice: the cheapest model good enough for
≥ 80 % of the cost (`ADVICE_SHARE`, at least 3 tasks), with the saving of
pricing the analysed calls at it (per call `max(0, cost − cost at it)`; per
month = × 30 / N days). Subagents get `model: <alias>` for the frontmatter,
main gets `/model <alias>`. **Confidence**: `low` for `main` always and for a
process with fewer than 9 tasks, else `medium`. Every output says that
benchmarks fit subagent and simple tasks better than long agentic coding on
the main thread.

**Stored** in `<data_dir>/claude-code/analysis.json`
(`tryaii.claude-code.analysis/1`): window, consent, catalog and embedding
model, counts, the per-process advice, `duration_s`, and per call key
(message id) the result (`task`, `category`, `subcategory`, `confidence`,
`anchor`, `recommended`, `method`, `good_enough`, `overall_winner`,
`overall_method`). Never task text, embeddings or hashes of the text.
`--json` prints the document without the per-call map; `report` adds it as
`analysis` and as a text section.

**Uploaded** (by `connect`, §10.5, and by `sync`, §10.9): only
the task profile and the Anthropic answer, in the event's existing fields, and
(`sync` only) the structured per-process advice, never its sentence. Text, embeddings and hashes of the
text never leave the machine (`prompt_hash` stays `null`, `vector` `false`);
the vectors opt-in (TS-168) does not cover this command. Events already
stored before `analyze` ran keep `classification: null` locally (event ids
are deterministic and never rewritten); once uploaded, `sync` sends their task
profile as annotations (§10.9). `connect` ends with one line suggesting
`analyze`, then `sync`, while there is no current consent.

### §10.8 Live telemetry (v1.5, API.md §8)

`connect` has a second part with its own consent: it sets up Claude Code's
OpenTelemetry export so every **new** Claude Code API call is reported to
TryAii, metadata only. Code: `tryaii/claude_code/telemetry.py`.

**Settings.** The OpenTelemetry exporter variables take effect only from the
**user** settings file (`~/.claude/settings.json`, `$CLAUDE_CONFIG_DIR/settings.json`;
`--claude-dir` overrides) or managed settings; Claude Code ignores them in a
project's `.claude/settings.json` and `.claude/settings.local.json` (except
values that turn telemetry off). `connect` merges into the user file:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "<TRYAII_API_URL>/v1/otlp/claude-code/v1/logs",
    "OTEL_LOG_USER_PROMPTS": "0",
    "OTEL_LOG_TOOL_DETAILS": "0"
  },
  "otelHeadersHelper": "tryaii claude-code otel-headers"
}
```

The per-signal endpoint takes the full URL (the exporter appends `/v1/logs`
only to the generic `OTEL_EXPORTER_OTLP_ENDPOINT`). `OTEL_LOG_RAW_API_BODIES`
is documented only with the values `1` and `file:<dir>` and is off when unset,
so `connect` leaves it unset (and refuses while it is on).

**`--no-session`** adds `"OTEL_METRICS_INCLUDE_SESSION_ID": "false"` to the same
`env` block: Claude Code's documented switch (default true) that leaves
`session.id` (and `ccr.session.id`) out of its telemetry, so the server keeps
no `session_ref` (API.md §8.3) and the cache timer shows no sessions. An off
value already there is left alone; any other value is a conflict, never
overwritten. It is recorded like every added key, so `disconnect` removes it;
it works on an already connected setup (`connect --live-only --no-session`),
and a later `connect` without the flag leaves it in place. It cannot be combined
with `--no-live` (exit 2).

**Merge rules.** Every other key stays as it was, in its order; the file's
indent and line endings are reused. A timestamped copy
(`settings.json.tryaii-backup-<yyyymmddThhmmssZ>`) is written next to the file
first, then the new file atomically. A key that already holds the wanted value
(or an off value for a content switch) is left alone and not recorded. `connect`
stops (exit 1, nothing written, the backfill included) and lists the reason when:
the file is not a JSON object; `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_LOGS_HEADERS` is set in the
file or the environment (another destination, or headers that would be sent to
TryAii as well); `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`, `otelHeadersHelper` or an
exporter variable holds another value; a content switch or
`OTEL_LOG_RAW_API_BODIES` is on. It never overwrites another tool's telemetry.
It needs an app key in `TRYAII_API_KEY` and the upload decision (§9.1) on;
otherwise the live part is skipped (`--live-only`: exit 1).

**Local state.** `<data_dir>/claude-code/telemetry.json`
(`tryaii.claude-code.telemetry/1`: `settings_path`, `endpoint`, `env` and `top`
= exactly the keys and values added, `created_env`, `connected_at`,
`updated_at`, `live_since`, `backup`) and `telemetry-key.json` (the app key,
0600 on POSIX; never in Claude Code's settings). A later `connect` may update a
key whose value is still the one recorded (a new API URL or key).

**Cutoff.** The server stores events under `(event_id, ts)`, and a live
event's `ts` (Claude Code's event time) differs from the log line's by
milliseconds, so the shared `event_id` alone does not deduplicate. `live_since`
(UTC, the moment `connect` turned live telemetry on) is the cutoff: while it is
recorded, the log import stores and uploads only calls whose time is **before**
it; live telemetry covers everything from then on. Without live telemetry
(`--no-live` on a machine where it was never turned on) there is no cutoff.
`disconnect` clears it, so a later import covers the gap again. The event id
rule of §10.5 still applies, so re-imports stay idempotent.

**`otel-headers`.** Claude Code runs the helper at startup and every 29 minutes
(`CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS`) and expects a JSON object of
string header values on stdout. It prints
`{"Authorization": "Bearer <stored app key>", "X-Tryaii-Notice-Version": "<NOTICE_VERSION>"}`
and nothing else; when live telemetry is not connected it prints nothing on
stdout, an error on stderr, and exits 1 (Claude Code's exports then fail). The
key is never logged.

**`status [--json] [--claude-dir PATH]`.** Backfill (connected, last import,
events imported) and live telemetry (connected or not working, endpoint,
cutoff, settings file, the keys changed since connect, the key as
`tryaii_<key_id>_...`, and whether Claude Code sends its session id:
`session_id_sent`, the `OTEL_METRICS_INCLUDE_SESSION_ID` setting and where it is
set, the settings file's `env` before the environment, unset meaning sent;
`no_session_by_connect`). It warns loudly for each of `OTEL_LOG_USER_PROMPTS`,
`OTEL_LOG_TOOL_DETAILS`, `OTEL_LOG_RAW_API_BODIES`,
`OTEL_LOG_ASSISTANT_RESPONSES` and `OTEL_LOG_TOOL_CONTENT` that is on in the
user settings or the environment, and says that the server drops such data
before storage but that it still leaves the machine.

**`disconnect`** removes exactly the recorded keys whose value is still the one
`connect` wrote (a key the user changed stays and is named), the `env` object
when `connect` created it and it is empty again, after a backup; then the local
state as in §10.5. It restores nothing else.

**What TryAii keeps.** Per API call: model, input and output tokens, cache read
and cache write tokens (`events.cache_write_tokens` and the daily cache
rollups), cost estimate, duration, time, the process (`main`, or the subagent
type; a custom subagent arrives as `custom`), the effort level (`low` |
`medium` | `high` | `xhigh` | `max`, else null) and a session reference
(`session_ref`: the first 64 bits of an HMAC-SHA256 of `session.id` with a
per-app key derived from a server secret; a value that is not a UUID gives
null; API.md §8.3). The `session_ref` is used only for the cache timer and cold
restarts (§10.9) and is cleared after 7 days; it is never returned by any API,
and the dashboard names sessions `Session N`. The request id is used only to
build the `event_id` and is not stored. Never sent with these settings:
prompts, code, file contents, tool inputs and outputs. Claude Code also sends
its standard attributes (`user.email`, `user.account_uuid`, `organization.id`,
`session.id`, ...) and other event types; the server drops them before storage,
and the raw `session.id` is hashed on arrival and never stored, queued or
logged. With `--no-session` (or `OTEL_METRICS_INCLUDE_SESSION_ID=false`) there
is no session id to hash. The project and the task profile of a live call come
only from `sync` (§10.9).

### §10.9 `sync`, the plugin hook and the cold-restart rule (v1.6, API.md §9)

`sync [--background] [--quiet] [--days N] [--projects-dir PATH]` completes the
live events of §10.8 with what only the local logs know. Code:
`tryaii/claude_code/sync.py`.

**Preconditions.** The backfill connected (`connect.json`), live telemetry
connected with an app key (without it: only imported calls are annotated,
with the app key in `TRYAII_API_KEY`), and the upload decision (§9.1) on;
otherwise it prints the reason and exits 0. At most one run per 5 minutes
(`sync.json` `last_run_at`) and one at a time (`sync.lock`; a lock older than
30 minutes is taken over).

**What it reads and sends.** The calls of the local logs (§10.2) from
max(`live_since`, now − N days) (N default 7, at most 90) to now, older than 2
minutes and with a `requestId`. One **annotation** per call: `event_id` =
uuid5 of the request id (the live event's id, §10.5), `ts` the log time,
`project` the base name (§10.1; `null` for `unknown`, anything with `/`, `\`
or `:`, a non-printable value or more than 256 characters), `process` (`main`
or the real subagent type), and, only with a current `analyze` consent and a
result for the call: `classification` (category, subcategory, confidence),
`winner` (the Anthropic answer), `est_cost_winner_usd` and `taxonomy_version`;
else those four are `null`.

**Imported calls** (v1.6, API.md §9). `connect` uploads its events before
the user may have run `analyze`, so they carry no task profile. `sync` also
reads the calls from now − max(N, 30) days and sends one annotation (the same
fields) per call whose imported event the uploader already sent
(`upload.json` byte prefixes) with `classification: null` and that `analyze`
has a result for: `event_id` is the importer's id (§10.5; the pre-v1.5
fallback id when the event was stored under it), `ts` the stored event's.
They share the batches (at most 500 calls) and the state with live calls; a
server that does not annotate imported rows answers them `unmatched`, which is
retried and dropped like any other (never an error). Per process of the analysis, one **advice**
document: `process`, `thread`, `window_days`, `generated_at`, `tasks`,
`calls`, `est_cost_usd`, `classes[]` (`class`, `share`),
`good_enough_shares[]` (`model`, `share`), `recommendation` (`model`, `alias`,
`good_enough_share`, `saving_usd`, `saving_usd_per_month`) or `null`, and
`confidence`. Never text, the advice sentence, paths, a session id or an
email. Body: `{"schema": "tryaii.claude-code.annotations/1", "sdk",
"notice_version", "calls", "advice"}`, at most 500 calls and 50 advice
documents per request, `POST /v1/monitor/claude-code/annotations` with the app
key (API.md §9). Before sending, the payload is scanned for the uploader's
forbidden field names (any case) and path-like projects; a hit refuses the
request.

**Analysis.** A stored `analysis.json` younger than a day under the current
consent is reused; an older one is re-run first (on this machine, §10.7) when
the consent is current; without a consent nothing of an analysis is used.

**State.** `<data_dir>/claude-code/sync.json` (`tryaii.claude-code.sync/1`:
`cursor`, `last_run_at`, `last_ok_at`, `last_error` (one short line),
`applied`, `unmatched` (retried for 24 hours, then `dropped`), `dropped`,
`advice_generated_at`). Ids before the read window are forgotten.
`disconnect` removes it with the rest of `<data_dir>/claude-code/`.

**Output.** One line (counts of calls annotated, matched, not found yet,
advice documents, and where the task profiles came from); `--quiet` prints
nothing and always exits 0; `--background` starts a detached `sync --quiet`
(Windows `DETACHED_PROCESS`, POSIX a new session) and returns at once.

**Plugin hook.** The Claude Code plugin (`integrations/claude-code-plugin`,
0.2.0) has `hooks/hooks.json` with one `SessionEnd` command hook, `async`:
`tryaii claude-code sync --background --quiet; exit 0`. It never blocks the
end of a session and always exits 0, also when `tryaii` is missing or not
connected.

**Cold-restart rule** (`tryaii/claude_code/sessions.py`; pinned by
`shared/monitor/fixtures/claude-code/cold-restarts.json`, which the server's
`mark_cold_restarts` also runs for live events):

- only the main thread (`process` `main`) of one session (§10.1), in time
  order; subagent calls are excluded and are not predecessors;
- the first main call of a session is never cold;
- a call is cold when the gap since the previous main call of the same session
  is **more than 5 minutes** and its cache write is at least
  max(1024, its cache read tokens);
- `ttl`: the call's own 5m/1h split (`1h` when the 1-hour part is larger, `5m`
  when only the 5-minute part is set); without a split the session's last
  split, else the default: `5m` for imported calls (the API's default), while
  the server defaults live main-thread calls to `1h`, so the fixture passes its
  `default_ttl` explicitly;
- reason: `expired` when the gap ≥ ttl, else `model_switch` when the model
  differs from the previous main call's, else `other`;
- cost (at API list prices, unrounded, 6 dp in output): 5m part × (1.25 ×
  input − cache read price) + 1h part × (2 × input − cache read price); a write
  without a split is priced whole at the call's resolved ttl; `null` when
  unpriced.

Imported events carry the result as `cache_cold` (§10.5); the report shows it
(§10.4). Nothing about the session itself leaves the machine.

## §11 Langfuse export

(2026-10-24, Jira TS-245 contract, TS-246 exporter and scores; API.md v1.7.)
The SDK can send each monitor event to the customer's own Langfuse (Cloud EU
or US, or self-hosted v3.22+) as one OpenTelemetry span, and TryAii's
recommendation for the call as three Langfuse scores. Everything is sent from
the SDK, with the customer's Langfuse keys: api.tryaii.com never sees a
Langfuse key and never talks to Langfuse. Code:
`tryaii/monitor/langfuse.py` (the reference), `src/monitor/langfuse.ts`; the
call-time capture in `tryaii/monitor/otel.py`, `src/monitor/otel.ts`.

### §11.1 Settings

| Variable | Meaning |
|---|---|
| `TRYAII_LANGFUSE` | the switch: `on`, `1` or `true` (trimmed, any case) turn the export on; anything else, or unset, is off (the default) |
| `LANGFUSE_BASE_URL`, else `LANGFUSE_HOST` | the Langfuse host (trimmed, trailing `/` dropped, a blank value is skipped); neither → `https://cloud.langfuse.com` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | the project keys, sent as HTTP Basic auth `public:secret` |
| `TRYAII_API_KEY` | must hold a TryAii app key (API.md §1 regex; no request is made to check it) |

`settings(env)` → `{"enabled", "host", "reason"}` (that key order; never a
key). `reason` is `null` when on, else the first that applies:
`TRYAII_LANGFUSE is not on`, `TRYAII_API_KEY is not set`, `TRYAII_API_KEY is
not an app key`, `LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are needed`.
When the switch is on but the export is off, the transport logs one warning
per process, `tryaii monitor: the Langfuse export is off (<reason>)`, and the
app is never affected. The export is independent of the upload switches of
§9.1 (it sends to the customer's own system, not to TryAii); the upload batch
says `"integrations": ["langfuse"]` while `settings` says on (the switch, an
app key and both Langfuse keys; API.md v1.7). The switch itself is read by
the one config helper, `config.langfuse_enabled` / `langfuseEnabled`; every
other reader goes through `settings`.

`tryaii monitor status` (text, not `--json`) prints one machine-wide line
after the `catalog:` line, only when `TRYAII_LANGFUSE` is set to a non-blank
value: `langfuse: on (<host>)`; `langfuse: off (<reason>)` while the switch is
on but a key is missing; `langfuse: off` for any other switch value. It
reads the environment of the `status` process. The `cli` fixture suite
never inherits `TRYAII_LANGFUSE` (the `langfuse` suite freezes the line).

### §11.2 The `otel` event field (API.md v1.7)

`otel` = `{"trace_id": "<32 lowercase hex>", "span_id": "<16 lowercase
hex>"[, "origin": "tryaii"]}`: the span the TryAii observation belongs to,
fixed at call time (in `before()`, synchronously, in the caller's context).
Key position: right after `vector`, before `cache` (which stays the last
key); absent when none applies. Optional: readers and the server ignore it.
The OpenTelemetry API and Langfuse are used only when they are loaded and are
never a dependency; any failure records no `otel` and leaves the call as is.
First rule that applies:

1. **The TryAii parent (Python, `origin: "tryaii"`).** The Langfuse export is
   on (§11.1), no span is active, and the class `create` the wrapper calls is
   wrapped by Langfuse's OpenAI integration (`import langfuse.openai`: a wrapt
   wrapper whose function is defined in a `langfuse` module; read without
   importing langfuse). With `wrap()` both import orders work, since the
   wrapper looks the class method up per call and stays outermost (TS-257).
   With `install()` only `import langfuse.openai` first works: `install()`
   run earlier is itself wrapped by Langfuse's patch, does not see it, and
   the call gives two traces (a known gap, documented in
   `docs/sdk/monitor.md`). The wrapper fixes the
   event id at call time, sets `otel` = `{"trace_id": h[0:32], "span_id":
   h[32:48], "origin": "tryaii"}` (`h` = sha256(event_id), §11.5) and, around
   the provider call only, makes a non-recording span with those ids the
   active span (`trace.set_span_in_context(NonRecordingSpan(SpanContext(...,
   is_remote=True, trace_flags=SAMPLED)))` with `context.attach` /
   `detach`). Langfuse's generation, started inside the call, becomes its
   child; the exporter then sends the TryAii observation with exactly these
   ids as the trace root. One trace: TryAii span → Langfuse generation. When
   the context cannot be attached, no `otel` is recorded.
2. **Langfuse's generation (Node).** `observeOpenAI` starts its generation
   before it calls the `create` TryAii patched behind its proxy (TS-301),
   reads no per-call parent and never activates the generation, so no parent
   can be put around it. While the export is on, the wrapper watches, read
   only, the tracer Langfuse starts its spans with (`getTracer("langfuse-sdk")`
   on the OpenTelemetry global tracer provider, `Symbol.for("opentelemetry.js.api.1")`,
   and on Langfuse's isolated provider, `Symbol.for("langfuse")`): every span
   it starts is noted until the end of the synchronous tick. A call whose
   `create` runs in that tick (observeOpenAI calls it right after
   `startObservation`) records `otel` = that generation's ids (no `origin`):
   the TryAii span becomes its child. One trace: Langfuse generation → TryAii
   span; both wrap orders. Without a registered tracer provider (no
   OpenTelemetry SDK such as NodeSDK with `LangfuseSpanProcessor`) Langfuse
   records nothing anyway and the call falls through to the rules below.
3. **The active span.** The OpenTelemetry span active at call time (for
   example a Langfuse `@observe` / `start_as_current_observation` /
   `startActiveObservation`, or any other instrumentation): Python
   `opentelemetry.trace.get_current_span()`, Node `@opentelemetry/api`
   `trace.getActiveSpan()` when it resolves, else the API's global context
   manager (the span under `Symbol.for("OpenTelemetry Context Key SPAN")`).
4. Otherwise no `otel`: the exporter creates the trace itself.

An `origin` other than `"tryaii"` is ignored (rule 3 applies to the ids).
In every case Langfuse shows one trace per call, and the call's cost once:
on Langfuse's generation when its integration records the call, else on the
TryAii generation (§11.5).

### §11.3 The cursor (`langfuse.json`, `tryaii.monitor.langfuse-cursor/1`)

```json
{"schema": "tryaii.monitor.langfuse-cursor/1",
 "host": "https://cloud.langfuse.com",
 "files": {"events-2026-10-24.jsonl": 1675},
 "last_ok": "2026-10-24T08:25:00.000Z", "last_error": null}
```

In the app directory (§3), next to `upload.json`; `indent=2` + `\n`, written
atomically, that key order, `files` sorted by name. `files` = the bytes of
each events file already exported. A missing cursor, or one whose `host` is
not the current host, is created when the app directory is registered with
the exporter, with **every existing events file at its current size**: the
export covers the calls made from then on, never a backfill of older
records. `langfuse.lock` (`O_CREAT|O_EXCL`, pid inside, taken over after 60 s)
keeps one process per app directory exporting at a time. Local files are
never deleted or rewritten. A file shorter than its cursor restarts at 0.

### §11.4 Reading and batching

The exporter is a second reader of the store, on its own daemon thread
(Python) or async loop with unref'd timers and sockets (Node), started by the
transport where the uploader starts (§3.4) when `settings` says on, and fed
the same app directories. It is never in the LLM call path. One pass takes,
per app directory, the unread whole `\n`-terminated lines of the oldest
events file that has any (a trailing partial line waits), at most **100
events**, and **skips** (never sends, but advances past) every line that is
not a JSON object of schema `tryaii.monitor.event/1` with a uuid `event_id`
and a parseable `ts`, or that carries a forbidden field (§3.4's hard rule).
A line is written after the shadow route has finished (§2.4), so the
recommendation of every line read is already known. After a sent batch the
next one waits at least 1 s; an idle exporter looks again every 2 s. At
process exit it gets passes until nothing is left, bounded by 2 s in all,
after the uploader's; a batch cut short there is resent by the next process
(§11.7).

### §11.5 Event → OTLP span

`POST <host>/api/public/otel/v1/traces`, `Content-Type: application/json`,
`Authorization: Basic base64(public:secret)`; redirects are never followed.
Body (minified, the store's line format, §1.2):

```json
{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"tryaii-monitor"}}]},
 "scopeSpans":[{"scope":{"name":"tryaii.monitor","version":"<sdk version>"},"spans":[<span>, ...]}]}]}
```

**Ids.** `h = sha256(event_id as UTF-8)`, lowercase hex. Without a valid
`otel` (lowercase hex of the right length, not all zeros): `traceId` =
`h[0:32]`, `spanId` = `h[32:48]`, no parent: the exporter creates the trace.
With `otel.origin == "tryaii"`: `traceId` = `otel.trace_id`, `spanId` =
`otel.span_id` (the same values, fixed at call time), no parent: the trace
root that Langfuse's generation already points at. With any other `otel`:
`traceId` = `otel.trace_id`, `parentSpanId` = `otel.span_id`, `spanId` =
`h[32:48]`: the TryAii span is a child of that span. Ids are hex strings
(OTLP/JSON).

**Span**, key order: `traceId`, `spanId`, `parentSpanId` (only for a child),
`name` = `"tryaii " + process`, `kind` = `3` (client), `startTimeUnixNano` =
`ts` in ns (string), `endTimeUnixNano` = start + `latency_ms` × 10⁶ (start
when `latency_ms` is null), `attributes`, `status` = `{"code": 2}` with an
`error`, else `{"code": 1}`. Every attribute value is a `stringValue`, in
this order, each only when it applies:

| Attribute | Value |
|---|---|
| `langfuse.observation.type` | `generation` without `otel`; `span` with any `otel` (no model, usage or cost, so Langfuse never counts the call twice next to its own generation) |
| `langfuse.trace.name` | when the span has no parent (no `otel`, or `origin: "tryaii"`): the span name |
| `langfuse.trace.metadata.tryaii_url` | when the span has no parent: `tryaii_url` |
| `langfuse.observation.model.name` | without `otel`: `model_used` (the native id sent) |
| `langfuse.observation.usage_details` | without `otel`: JSON (as Langfuse's own OpenAI integration splits it): `input` = `prompt_tokens − cached_tokens` (≥ 0; `prompt_tokens` when `cached_tokens` is not a number), `output` = `completion_tokens − reasoning_tokens` (same rule), `total` = `total_tokens`, `input_cached_tokens` = `cached_tokens`, `output_reasoning_tokens` = `usage.reasoning_tokens` — each only when it is an integer; absent when none is |
| `langfuse.observation.cost_details` | without `otel`: JSON `{"total": est_cost_usd}` when it is a number: Langfuse shows TryAii's estimate, the base of the savings score |
| `langfuse.observation.level` | `ERROR` with an `error` |
| `langfuse.observation.status_message` | with an `error`: its `class` (`error` when empty), plus ` (HTTP <status>)` when `status` is an integer |
| `langfuse.observation.metadata.<key>` | for each key below whose value is a scalar (null, objects and lists skipped): a string as is, any other value as JSON (`true`, `812`, `0.004213`) |

Metadata keys, in order: `tryaii_url`, `event_id`, `app`, `process`, `mode`,
`provider`, `model_requested`, `model_resolved`, `model_used`, `winner`,
`winner_reachable`, `route_status`, `category` and `subcategory` (from
`classification`), `prompt_tokens`, `completion_tokens`, `total_tokens`,
`cached_tokens`, `reasoning_tokens` (from `usage`), `est_cost_usd`,
`est_cost_winner_usd`, `est_cost_anchor_usd`, `latency_ms`, `stream`,
`call_site` (`<file>:<line>`, or `<file>` without a line; an absolute path
reduced to its base name, TS-226), `error_class`, `error_status`.

**`tryaii_url`** = `https://tryaii.com/dashboard/monitor/<app_id>/features/<process>`
(the web dashboard's sub-process deep link; both parts percent-encoded like
`encodeURIComponent`). `app_id` is the server's app id from the app
directory's `remote.json` (§3.3); without it (no remote config fetched yet)
the URL is `https://tryaii.com/dashboard/monitor`, and with an empty process
`.../monitor/<app_id>`.

### §11.6 Scores

After the batch's spans were accepted, one `POST <host>/api/public/scores`
per score (same auth), at most 8 at a time; the scores of an event come in
this order, each only when the event has its value:

| Name (frozen) | `dataType` | `value` | Sent when |
|---|---|---|---|
| `tryaii.recommended_model` | `CATEGORICAL` | `winner`: the best-for-less model (§2.3), which is the anchor itself when the developer's model should stay | `winner` is a non-empty string |
| `tryaii.est_savings_usd` | `NUMERIC` | `est_cost_usd − est_cost_winner_usd`, rounded to 6 dp (§1.3; integral values as integers) | both are numbers |
| `tryaii.route_status` | `CATEGORICAL` | `route_status` (§3.1) | a non-empty string |

Body, key order: `{"id", "traceId", "observationId", "name", "value",
"dataType"}`; `id` = `<event_id>-<name without "tryaii.">` (for example
`<event_id>-est_savings_usd`), `traceId` / `observationId` = the span's
`traceId` / `spanId` (§11.5).

### §11.7 Retries and failures

Every id is derived from the event id, and Langfuse upserts observations by
span id and scores by score id, so sending a batch again never duplicates a
span or a score. The cursor advances only when the batch is done:

- traces 2xx → the scores; every score 2xx (or 4xx, below) → the cursor
  advances, `last_ok` = now, `last_error` = null (or the note of a refused
  score), backoff reset;
- traces `401` / `403`, or the same on a score → the export is off for the
  process: `last_error` = `<traces|scores>: http <status> (check
  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)`, one warning line `tryaii
  monitor: Langfuse rejected the keys (HTTP <status>); the Langfuse export is
  off for this process`;
- traces any other 4xx except 408 / 429 → the batch is passed over (cursor
  advances, no scores), `last_error` = `traces: http <status>`; a score 4xx
  → that score is passed over, `last_error` = `scores: http <status>`;
- 408, 429, 5xx, any other status or a network failure → the cursor stays,
  `last_error` = `<traces|scores>: http <status>` or `<...>: network`,
  backoff 5 s, doubling, capped at 5 min; the next attempt resends the whole
  batch (spans and scores) with the same ids.

Every failure is swallowed: the export never raises into the app, never
blocks or slows a call, and never changes what is recorded locally or
uploaded to TryAii.

### §11.8 What is never sent

The prompt or response text (the store has none), `prompt_hash` (plain or
keyed), the vectors, the `cache` report, any API key (only the customer's
own Langfuse keys, in the Authorization header to their own host), the
TryAii app key, and absolute file paths. A line with a forbidden field
(§3.4) is never exported.

### §11.9 Fixtures

`shared/monitor/fixtures/langfuse/cases.json`, generated from the Python
reference by `scripts/gen-langfuse-fixtures.py` (`--check`: no writes, exit 1
on drift). Case kinds: `export` (`app_id` + events → per-event ids,
`tryaii_url`, the OTLP request body, the score bodies and the sha256 of each
exact request body, which the Node SDK must reproduce byte for byte),
`settings` (env → `settings` and the status line) and `exportable` (a store
line → whether it is sent). Run by `tests/test_monitor_langfuse.py` and
`tests/monitor-langfuse.test.ts`, which also cover the exporter against a
fake HTTP layer. Clock-free and network-free.
