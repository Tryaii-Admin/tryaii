# monitor upload and dashboard API — contract v1.5 (2026-10-20)

v1.5 (Jira TS-228, epic TS-227 "cache intelligence"): the event carries the **write** side of prompt-cache usage next to v1.4's `usage.cached_tokens` — `usage.cache_write_tokens`, `usage.cache_writes[] {tokens, ttl}` and the top-level `cache_ttl_policy` (section 3.2.2, SDK side `SPEC.md` section 3.1b). All three are optional and nullable; the schema id stays `tryaii.monitor.event/1`, older events stay valid, the v1.4 `cache` block is unchanged, and the upload batch carries the fields exactly as stored. The server stores them and adds the cache rollups in TS-231; until then it ignores them. The dashboard summary and events read gain the same fields (section 3.4).

v1.4: per-feature fields in the summary (section 3.4): each process gains `prompt_tokens`, `completion_tokens`, `savings_available_usd`, `latency` and `cache`, all optional for readers (a v1.3 server omits them). Events gain two optional fields (section 3.2.1): `usage.cached_tokens` (the provider's cached prompt tokens) and `cache` (the SDK's runtime cachelint result: a per-call outcome and, now and then, a redacted report). The report rides on the event upload. The web dashboard called these fields "proposed v1.2"; v1.2 and v1.3 were already taken, so they land as v1.4. Also in v1.4: the App object gains `emoji` and `color`, set with `PATCH /v1/monitor/apps/{app_id}` (section 2.1), and the dashboard's group layout is stored per app (`GET`/`PUT /v1/monitor/apps/{app_id}/layout`, section 3.4.1).

v1.3: section 7 (privacy). Consent is recorded only where a person is shown the Data Notice; an upload batch never counts as consent. A per-user upload switch (`upload` in RemoteConfig, `403 upload_disabled`). Deleting an app keeps it restorable for 30 days, then purges every row. Batch and config-change logs are kept 13 months. Cross-account statistics come only from an anonymous table. `notice_version` is the notice's publication date (`YYYY-MM-DD`, first `2026-10-18`). `prompt_hash` is an HMAC keyed per installation (SPEC section 3.1), so hashes from different machines are not comparable.

v1.2: `call_site.file` in an uploaded event is relative to the application's working directory (base name only when outside it); the SDK never uploads an absolute path (TS-226). Servers and dashboards must not assume a path on the developer's machine.
v1.1: adds `est_cost_anchor_usd` to events and fixes the realised-savings formula.

The single contract shared by the SDKs (`packages/python`, `packages/node`), the
API server (`tryaii-backend-servers/tryaii-sdk-api`) and the website
(`tryai`, the dashboard pages). Every side builds to this file; a change here is
a change on all three sides. Owner decisions behind it: Confluence page 6881283
("monitor dashboard backend: decisions (slice 2)").

Base URL: `https://api.tryaii.com`. JSON everywhere; timestamps are ISO-8601 with
millisecond `Z`; money is USD as a number with up to 6 decimals.

## 1. Identity and authentication

Three callers, three credentials:

| Caller | Credential | Header |
|---|---|---|
| SDK inside a developer's app | app key `tryaii_<key_id>_<secret>` | `Authorization: Bearer tryaii_...` |
| CLI / signed-in user (device flow) | user JWT (existing, 1 h, Ed25519) | `Authorization: Bearer <jwt>` |
| Website server (dashboard pages, Next.js route handlers) | service token + acting user | `Authorization: Bearer <WEB_SERVICE_TOKEN>` and `X-Tryaii-User-Id: <google sub>` |

The user id is the Google `sub`, identical in the website's `users._id`, the
user JWT `sub` and every `apps.owner_user_id`. The website never exposes the
service token to the browser: dashboard pages call the website's own route
handlers, which proxy to the API server-side.

### App key

- Shape: `tryaii_` + `key_id` (12 chars `[a-z0-9]`) + `_` + `secret` (43 chars
  base64url, 32 random bytes). Total 63 chars. Regex:
  `^tryaii_([a-z0-9]{12})_([A-Za-z0-9_-]{43})$`.
- Stored: `key_id` (unique index), `secret_hash = sha256(secret)` hex, `app_id`,
  `scopes` (`["monitor:write"]`), `label`, `created_at`, `created_by`,
  `last_used_at`, `revoked_at`, `expires_at` (nullable).
- Validation (constant time, O(1)): shape check → lookup by `key_id` → reject
  if missing / revoked / expired → `hmac.compare_digest(sha256(secret), hash)`.
  All failures answer `401 {"error":"invalid_key"}` with no distinction.
  `last_used_at` is updated at most once per 60 s per key. Rate limit is per
  `key_id`.
- At most **2 active keys per app** (rotation). Creating a third answers
  `409 {"error":"key_limit"}`.
- The full key is returned **once**, on creation. Lists show `tryaii_<key_id>_…`.
- The SDK reads `TRYAII_API_KEY`. No key → no upload, local recording continues.

## 2. Management endpoints (user JWT or website service token)

```
POST   /v1/apps                      {name}                -> 201 App
GET    /v1/apps                                            -> 200 {apps:[AppWithStats]}
GET    /v1/apps/{app_id}                                   -> 200 AppWithStats
DELETE /v1/apps/{app_id}                                   -> 204 (soft delete; keys revoked; purged after 30 days, section 7.4)
GET    /v1/apps/deleted                                    -> 200 {apps:[DeletedApp]}
POST   /v1/apps/{app_id}/restore                           -> 200 App | 404 | 409 name_taken
POST   /v1/apps/{app_id}/keys        {label?}             -> 201 {key_id, key, label, created_at}
GET    /v1/apps/{app_id}/keys                              -> 200 {keys:[KeySummary]}
DELETE /v1/apps/{app_id}/keys/{key_id}                     -> 204 (revoke)
PATCH  /v1/monitor/apps/{app_id}     {emoji?, color?}      -> 200 App (v1.4, section 2.1)
```

```
App          = {app_id, name, owner_user_id, created_at, deleted_at|null,
                emoji: string|null, color: string|null}            // emoji, color: v1.4
AppWithStats = App + {calls_24h, cost_24h_usd, savings_24h_usd, last_event_at|null,
                      processes: int, keys_active: int}
KeySummary   = {key_id, label, prefix: "tryaii_<key_id>_", created_at,
                last_used_at|null, revoked_at|null, expires_at|null}
```

`app_id` is `app_` + 12 chars `[a-z0-9]`. `name` is 1–64 chars; unique per owner
(409 `{"error":"name_taken"}`). An app is only visible to its owner (org support
comes later; the schema already carries `owner_user_id` and a nullable `org_id`).

### 2.1 App display identity (v1.4)

Every App (create, list, get, deleted list, restore, PATCH) carries `emoji` and `color`,
both `null` until the owner sets them; the dashboard picks its own default for `null`.

```
PATCH /v1/monitor/apps/{app_id}
    {"emoji"?: string|null, "color"?: string|null}
-> 200 App
-> 400 {"error":"bad_request","detail":"..."}   (unknown key, invalid emoji or color, not a JSON object)
-> 404 {"error":"not_found"}                    (not the caller's app, deleted, unknown)
```

- Same credentials, ownership and rate limit as the other management endpoints. The
  same handler also answers `PATCH /v1/apps/{app_id}`.
- A key that is absent leaves the field unchanged; `null` clears it. Any other key is
  a 400. `{}` answers 200 with the unchanged App and writes no audit row.
- `color` is one of `blue`, `violet`, `green`, `orange`, `pink`, `teal`, `amber`, `slate`.
- `emoji` is meant to be one emoji grapheme (ZWJ sequences, variation selectors, skin
  tones and flags included). The server checks only: a non-empty string of at most
  **16 UTF-8 bytes** that contains no ASCII letter or digit, no whitespace and no
  control character. It does not prove the value is a single grapheme. Note that some
  long ZWJ sequences exceed 16 bytes (the four-person family is 25) and are rejected,
  and digit keycaps such as `1️⃣` are rejected because they contain an ASCII digit;
  `#️⃣` and `*️⃣` are accepted.
- Each PATCH that changes something writes a `config_audit` row whose `change` is
  `{"app": <the fields given>}`, e.g. `{"app": {"emoji": "🛒", "color": null}}`
  (section 3.4, `GET .../audit`).

## 3. App-key endpoints (SDK)

### 3.1 Validate

```
GET /v1/apps/keys/validate -> 200 {app_id, app_name, key_id, scopes, expires_at|null}
                              401 {"error":"invalid_key"}
```

Used by `tryaii apps check` and by the SDK once at start-up (result cached for
the process; a 401 disables upload for the process and logs one line).

### 3.2 Upload events

```
POST /v1/monitor/events
{
  "schema": "tryaii.monitor.upload/1",
  "batch_id": "<uuid4>",              // idempotency key, SDK-generated
  "sdk": {"lang": "python"|"node", "version": "0.7.0"},
  "notice_version": "2026-10-18",    // notice version built into the SDK (not proof of consent)
  "events": [ Event, ... ]           // 1..500 events, body <= 1 MiB
}
-> 202 {"accepted": n, "batch_id": "..."}      (queued; duplicates by batch_id are accepted and ignored)
-> 400 {"error":"bad_batch", "detail": "..."}  (shape; nothing queued)
-> 401 {"error":"invalid_key"}
-> 413 {"error":"too_large"}
-> 429 {"error":"rate_limited", "retry_after": seconds}
```

`Event` is the local event line (schema `tryaii.monitor.event/1`, see
`SPEC.md`) **plus** these fields that slice 2 adds to the local line too:

```
event_id             "<uuid4>"          unique per call, generated at record time
taxonomy_version     "<string>"|null    from the catalog bundle, null if unknown
est_cost_anchor_usd  number|null        the call's usage priced at the anchor (model_requested,
                                        resolved); equals est_cost_usd when no swap happened;
                                        null when the anchor is unpriced
```

`est_cost_anchor_usd` is what makes realised savings computable: after a swap
`est_cost_usd` is priced at the model actually sent, so the saving of that call
is `est_cost_anchor_usd - est_cost_usd`.

v1.4 adds two optional fields (section 3.2.1): `usage.cached_tokens` and `cache`.
Events without them (older SDKs, cachelint off) are valid and simply do not count
toward the cache figures of section 3.4.

#### 3.2.1 Cache fields on the event (v1.4)

**`usage.cached_tokens`** — `integer >= 0 | null`. The prompt tokens the provider
reports as served from its prompt cache for this call, read from the response:
`usage.prompt_tokens_details.cached_tokens` (OpenAI, OpenRouter, xAI and other
OpenAI-compatible APIs) or `usage.cache_read_input_tokens` (Anthropic-shaped usage).
`null` or absent when the provider reported nothing (not `0`: `0` means "reported, no
hit"). Recorded whenever the provider reports it, whether or not cachelint is on.

**`cache`** — `object | null`, present only when the call ran through the SDK's
runtime cache lint (`wrap(client, cachelint=True)`, docs/sdk/client/cache-lint.md).
Absent or `null` otherwise.

```
"cache": {
  "outcome": "hit"|"partial"|"miss"|null,
  "report": CacheReport|null
}
CacheReport = {
  "verdict": "CACHEABLE"|"CACHEABLE_WITH_ACTION"|"CACHEABLE_PREFIX"|
             "EFFECTIVELY_UNCACHEABLE"|"BELOW_THRESHOLD"|"UNKNOWN_THRESHOLD",
  "prompt_tokens": int,                // token_report.total.tokens of the analysed prompt
  "stable_prefix_tokens": int,         // stable_prefix.tokens
  "floor_tokens": int|null,            // threshold.min_tokens; null when the provider floor is unknown
  "tools_tokens": int,                 // token_report.sections summed by report section (see below)
  "system_tokens": int,
  "messages_tokens": int,
  "findings": [{
    "severity": "high"|"medium"|"low",
    "detector": "<cachelint detector kind>",   // e.g. timestamp-datetime, uuid, template-braces
    "section": "tools"|"system"|"messages",    // the engine's section label mapped as below
    "offset": int,                             // finding.start: code-point offset in the canonical render
    "excerpt": "<masked, see below>"
  }],
  "recommendation": "<string>"|null
}
```

Example (one finding; the date and time of the prompt became `0000-00-00 00:00`):

```json
"usage": {"prompt_tokens": 1240, "completion_tokens": 151, "total_tokens": 1391, "cached_tokens": 0},
"cache": {"outcome": "miss", "report": {
  "verdict": "CACHEABLE_PREFIX", "prompt_tokens": 1240, "stable_prefix_tokens": 212,
  "floor_tokens": 1024, "tools_tokens": 0, "system_tokens": 612, "messages_tokens": 628,
  "findings": [{"severity": "high", "detector": "timestamp-datetime", "section": "system",
                "offset": 847, "excerpt": "0000-00-00 00:00"}],
  "recommendation": "at messages[0]:system+847 (17% in): Remove it, round it to a coarse bucket (day/hour), or move it into the final user message (after the cached prefix)."}}
```

**Section mapping.** The engine labels each part of the prompt it lints: `tools`,
`system` (a top-level system prompt) and `messages[<i>]:<role>` for each message. The
SDK maps a label to a report section as follows: a message whose role is `system` or
`developer` (`messages[0]:system`, `messages[2]:developer`; exact, lower-case role)
→ `system`; otherwise the label cut at the first `[` or `:`, which gives `tools` →
`tools`, `system` → `system`, and every other message (`messages[1]:user`, assistant,
tool) and any unexpected label → `messages`. A finding's `section` is the mapped label
of the part it falls in, and each part's token count is added to the matching
`tools_tokens` / `system_tokens` / `messages_tokens`. The wrapper lints OpenAI-shaped
`messages`, so a system prompt sent as a `system` or `developer` message is reported
under `system`. It also lints the request's tool schemas: the `tools` list followed by
the legacy `functions` list (when present) are passed to the engine as its `tools`
input, which renders first (shared/cachelint/SPEC.md §1.4); their tokens count in
`tools_tokens`, `prompt_tokens`, the stable prefix and the verdict, and a change to a
tool schema is a divergence in the `tools` section. `recommendation` keeps the engine's own label (`at
messages[0]:system+847`, as in the example).

The section token counts are flat keys (`messages_tokens`, not `sections.messages`)
because the server rejects any key named `messages`, `text`, ... at any depth
(section 3.2); the summary regroups them as `sections`.

- **`outcome`** — the cachelint sequence prediction (shared/cachelint/SPEC.md, per-pair
  `expected`) for the transition from the previous linted call **of the same process in
  the same SDK process** to this call: `HIT` → `"hit"`, `PARTIAL` → `"partial"`,
  `MISS` → `"miss"`; `UNKNOWN`, `AT_RISK` and the first call of a process → `null`.
- **`report`** — the lint result of this call, attached (a) on the first linted call
  of each process in an SDK process and (b) afterwards when it differs from the last
  report attached for that process and at least 60 s have passed since that one;
  `null` on every other call. So a busy process sends about one report a minute at
  most, a steady one sends one per SDK start. The full analysis that produces a report
  runs on the first linted call of a process and then **at most once per 60 s per
  process** (only when the prompt changed since the last analysis), whether or not the
  report it produces differs, so a prompt that changes on every call (a per-call
  timestamp) costs one analysis a minute, not one per call. The `outcome` prediction
  runs on every linted call; it is the cheap path (a common-prefix scan and one token
  count of that prefix, no detectors).
- **Redaction (no prompt text leaves the machine).** The engine's own `excerpt`
  (the match with 28 code points of context) is **never** recorded or uploaded. The
  SDK records a **mask of the matched span only** (`finding.start .. finding.end`),
  mapping each code point as follows (no Unicode table is consulted, so both SDKs and
  the server agree whatever their Unicode version):
  - an ASCII digit `0`–`9` → `0`;
  - whitespace → one space: ASCII space, tab, newline, vertical tab, form feed,
    carriage return, and the other Unicode White_Space code points (U+0085, U+00A0,
    U+1680, U+2000–U+200A, U+2028, U+2029, U+202F, U+205F, U+3000);
  - printable ASCII punctuation and symbols (U+0021–U+007E minus letters and digits:
    ``!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~``) → kept;
  - **every other code point** → `x`: ASCII letters, any non-ASCII letter or digit
    (Arabic-Indic `٣` → `x`), combining marks (Devanagari virama and vowel signs, Hebrew
    niqqud, Thai vowel and tone marks, combining accents: one `x` per code point),
    emoji, non-ASCII punctuation and symbols, and control characters.

  At most 40 code points: a longer match keeps its first 39 and ends with `…`, the only
  non-ASCII character an excerpt may hold. `2026-10-18 14:02` → `0000-00-00 00:00`,
  `{{sku}}` → `{{xxx}}`, `7f9c02aa-51b3` → `0x0x00xx-00x0`, `Grüße 東京` → `xxxxx xx`.
  The mask is applied when the event is recorded, so the local line never holds the
  text either. shared/monitor/fixtures/cache pins it for both SDKs.
  `recommendation` is one line of the engine's `recommendations` with its leading
  `[tag] ` removed (the first `[fix:*]` line; else the `[enablement]` line when the
  verdict is `CACHEABLE_WITH_ACTION`; else `null`). Those lines are built from the
  provider knowledge base, detector kinds, section labels and numbers only, never from
  prompt text; the SDK must not add template-slot names, file names or excerpts to it.
- **Limits** (the server answers `400 bad_batch` naming the field when broken, like
  any other field): `findings` at most 10 (the SDK keeps the first 10 in prompt order);
  `detector` at most 64 characters; `excerpt` at most 40 code points, checked against an
  **allowlist**: every character is `x`, `0`, a space or printable ASCII punctuation
  (as above), except one trailing `…` (anything else, such as another letter or digit,
  a combining mark, an emoji or a tab, is treated as unredacted text and the batch is
  rejected); `recommendation` at most 500 characters;
  every count an integer `>= 0`. A report is about 2 KiB at most.

The server keeps the **latest report per (app, process)** (section 4,
`cache_reports`): an inserted event carrying a report replaces the stored one when its
`ts` is not older than the stored report's `ts`. A redelivered or duplicate event
changes nothing. `outcome` and `cached_tokens` are stored on the event row and
aggregated over the summary window.

#### 3.2.2 Prompt-cache write fields (v1.5)

v1.5 adds the write side of prompt-cache usage next to `usage.cached_tokens`
(`SPEC.md` section 3.1b has the per-provider map and the normalisation rule; the SDK
sends them **unchanged**):

```
usage.cached_tokens        int|null         (v1.4) input tokens read from the provider's prompt cache
usage.cache_write_tokens   int|null         input tokens written to the cache on this call (total)
usage.cache_writes         list|null        [{"tokens": int, "ttl": "5m"|"1h"|"30m"|"24h"|"explicit"}, ...]
                                            per-lifetime breakdown of the write; null when the provider
                                            gives none (then cache_write_tokens holds the total)
cache_ttl_policy           ttl|null         the lifetime the request asked for, same enum, or null
```

`usage.prompt_tokens` stays what the provider reported; whether it includes the
cached tokens is a per-provider convention (OpenAI, Azure, OpenRouter, Gemini:
included; Anthropic, Bedrock: excluded). `null` means "not reported", `0` means
"reported as zero" (below the provider's minimum prompt size caching silently does
nothing). A batch with events that lack the keys is valid: a missing key reads as
`null`.

Lifetimes are what the dashboard's hot-cache bar is built on: a `cache_writes`
entry with an exact lifetime is hot until `ts + ttl`; `"explicit"` writes and
providers without a breakdown draw nothing (only provider-promised lifetimes are
shown). Storage (`events.cache_write_tokens`,
`events.cache_writes jsonb`, `events.cache_ttl_policy`, the cache columns of
`rollups_daily`) and the bar's rollup are TS-231.

The server stores the metadata fields only. It **rejects the whole batch** with
400 if any event carries a field named `prompt`, `messages`, `response`,
`content`, `text` or `raw` (defence in depth: prompt text never enters the
queue). `vector` stays a boolean flag; vectors themselves are a separate,
opt-in endpoint in a later slice and are never part of this body.

The SDK uploads in the background: a cursor file
`<data_dir>/monitor/<app>/upload.json` = `{"schema":"tryaii.monitor.upload-cursor/1",
"files":{"events-2026-10-12.jsonl": <bytes uploaded>}, "last_ok":"...Z",
"last_error":"...|null"}`. Batches are built from unread bytes, at most 500
events or 1 MiB, sent at most every 5 s, with exponential backoff (5 s → 5 min)
on 5xx/429/network; a 400 skips the batch and records `last_error`. Events are
never deleted locally by the uploader (local retention is a separate concern).
`TRYAII_MONITOR_UPLOAD=0` disables upload; no key disables it silently.

### 3.3 Remote config (long poll)

```
GET /v1/monitor/config?etag=<etag>&wait=25
-> 200 RemoteConfig           (changed, or first call)
-> 304                        (unchanged after `wait` seconds; max wait 30)
-> 401 {"error":"invalid_key"}
```

```
RemoteConfig = {
  "schema": "tryaii.monitor.remote/1",
  "etag": "<opaque string>",
  "app_id": "app_...",
  "default_mode": "off"|"stable"|"dynamic"|"managed"|null,
  "processes": {
    "<process name>": {"mode": "off"|"stable"|"dynamic"|"managed", "model": "<catalog id>"|null}
  },
  "updated_at": "...Z", "updated_by": "<user id>"|null
}
```

- The SDK keeps the last answer in `<data_dir>/monitor/<app>/remote.json`
  (same shape plus `"fetched_at"`), re-polls immediately after a 200/304, waits
  30 s after an error, and applies the config on the **next call** (the config
  layer reads the file through the existing mtime cache; precedence stays
  env > remote > `wrap(mode=)` > `config.json` > default).
- `managed` requires `model`; the SDK sends that model if it is reachable from
  the app's provider class, else the anchor (recorded as `mode: "managed"`,
  `model_used: <anchor>`, `route_status: "unreachable_managed"`).
- A `304` or an error leaves the cached config in force; a missing file means
  "no remote config" (local behaviour, exactly slice 1).

### 3.4 Dashboard reads and writes (user JWT or service token)

```
GET /v1/monitor/apps/{app_id}/summary?since=24h|7d|30d
-> 200 {
  "app_id", "since", "generated_at",
  "totals": {calls, errors, prompt_tokens, completion_tokens, cost_usd,
             cost_at_winner_usd, savings_usd, savings_available_usd,
             cached_tokens|null, cache_write_tokens|null},        // v1.5: sums; null when no event reported them
  "processes": [{
     "process", "mode", "managed_model"|null, "pin": {model, expires_at}|null,
     "calls", "errors", "tokens", "cost_usd", "cost_at_winner_usd", "savings_usd",
     "savings_available_usd",             // v1.4, see below
     "cached_tokens"|null, "cache_write_tokens"|null,   // v1.5: sums over the window; null when no event reported them
     "top_winner": "<model id>"|null, "top_winner_share": 0..1,
     "anchor": "<model id>"|null, "traffic_light": "green"|"yellow"|"red"|"grey",
     "models": [{model, calls, share, cost_usd, wins, win_share, reachable}],
     "files": [{file, function|null, calls}],
     "classes": [{class, share}],         // from classification.category, until task profiles land
     // v1.4, optional for readers (absent from a v1.3 server):
     "prompt_tokens", "completion_tokens",
     "latency": ProcessLatency|null,
     "cache": ProcessCache|null
  }],
  "daily": [{date, calls, cost_usd, cost_at_winner_usd}]
}
GET /v1/monitor/apps/{app_id}/events?limit=100&before=<ts>&process=<name>
-> 200 {"events":[Event...], "next_before": "<ts>"|null}
   // v1.5: each Event carries usage.cached_tokens / cache_write_tokens / cache_writes
   // and cache_ttl_policy as uploaded (null for events stored before TS-231)
GET /v1/monitor/apps/{app_id}/config               -> 200 RemoteConfig
PUT /v1/monitor/apps/{app_id}/config
    {"default_mode"?: ..., "processes": {"<name>": {"mode": ..., "model"?: ...}}}
-> 200 RemoteConfig   (merges per process; a process set to null is removed; writes an audit row;
                       bumps etag; wakes long-polls)
-> 400 {"error":"bad_config","detail":"..."}  (managed without model, unknown mode)
GET /v1/monitor/apps/{app_id}/audit?limit=50    -> 200 {"entries":[{at, user_id, change}]}
GET /v1/monitor/models?app_id=...              -> 200 {"models":[{id, provider, quality, blended_price_usd_per_m, reachable}]}
GET /v1/monitor/apps/{app_id}/layout           -> 200 Layout                  (v1.4, section 3.4.1)
PUT /v1/monitor/apps/{app_id}/layout  {groups, assignments}  -> 200 Layout   (v1.4, section 3.4.1)
```

Audit `change` shapes: a config PUT records its normalized body (`{"default_mode"?,
"processes"}`); v1.4 adds `{"app": {...}}` (App PATCH, section 2.1) and
`{"layout": {"groups": n, "assignments": n}}` (layout PUT, counts only). Readers must
tolerate shapes they do not know.

Traffic light rule (server-side, per process, over `since`): `grey` = fewer
than 5 routed events; `green` = the anchor (or the pin / managed model when
set) wins ≥ 70 % of routed events; `yellow` = another model wins 30–70 %;
`red` = the anchor wins < 30 % or `savings_available_usd / cost_usd ≥ 0.4`.
`savings_available_usd = Σ max(0, est_cost_usd − est_cost_winner_usd)` over
events whose winner is reachable and that were **not** swapped
(`model_used == model_requested`); `savings_usd` (realised) =
`Σ max(0, est_cost_anchor_usd − est_cost_usd)` over swapped events
(`model_used != model_requested`), 0 for events without `est_cost_anchor_usd`.

Per-process fields added in v1.4 (all over the same `since` window as the rest of
the process entry; a v1.4 server always sends them, a reader must tolerate their
absence):

```
ProcessLatency = {
  "p50_ms": int, "p95_ms": int,
  "winner_p50_ms": int|null
}
ProcessCache = {
  "verdict": "CACHEABLE"|"CACHEABLE_WITH_ACTION"|"CACHEABLE_PREFIX"|
             "EFFECTIVELY_UNCACHEABLE"|"BELOW_THRESHOLD"|"UNKNOWN_THRESHOLD",
  "hit_rate": 0..1|null,
  "prompt_tokens": int, "stable_prefix_tokens": int, "floor_tokens": int|null,
  "sections": {"tools": int, "system": int, "messages": int},
  "findings": [{"severity": "high"|"medium"|"low", "detector", "section": "tools"|"system"|"messages",
                "offset": int, "excerpt"}],
  "recommendation": "<string>"|null,
  "outcomes": {"hit": int, "partial": int, "miss": int}|null
}
```

- `savings_available_usd` — number, USD, rounded to 6 dp like the other money fields:
  the process's share of `totals.savings_available_usd`. Over the summary window,
  `Σ max(0, est_cost_usd − est_cost_winner_usd)` over the process's events that have a
  winner, were **not** swapped (`model_used == model_requested`), have
  `winner_reachable === true` and have both cost estimates known; every other event
  contributes 0. This is the same per-event rule as `totals.savings_available_usd`, so
  for `24h` the processes sum to the total; for `7d` / `30d` the total comes from daily
  rollups and the per-process values from events over the same window, so they agree
  whenever those two sources agree (as `cost_usd` does). A process that is configured
  but has no events in the window reports `0`. Swapped calls never contribute (their
  saving is in `savings_usd`), nor do calls whose winner is unreachable. Clients must
  not derive it from `cost_usd − cost_at_winner_usd − savings_usd`.
- `prompt_tokens` / `completion_tokens` — sums of `usage.prompt_tokens` /
  `usage.completion_tokens` over the process's events (missing counts as 0); `0`
  when there are none. Same sums as `totals`, per process.
- `latency` — over the process's events that have a `latency_ms` and **no error**:
  `p50_ms` and `p95_ms` are nearest-rank percentiles (the first value, in ascending
  order, at whose position `i` of `n` values `i / n >= p`; Postgres
  `percentile_disc`). `winner_p50_ms` is the same p50 over those events whose
  `model_used` is the process's `top_winner` (equal to the catalog id, or to its part
  after the first `/`, since `model_used` is the native id); `null` when there is no
  top winner or no such event. `latency` is `null` when no event qualifies.
- `cache` — `null` when no cache report was ever stored for (app, process) (section
  3.2.1) or the stored one is older than 90 days. Otherwise `verdict`, `prompt_tokens`,
  `stable_prefix_tokens`, `floor_tokens`, `findings` (masked excerpts, as uploaded) and
  `recommendation` come from the **latest stored report**, `sections` regroups its
  `tools_tokens` / `system_tokens` / `messages_tokens`, and two fields come from the
  window's events:
  - `hit_rate` = Σ `usage.cached_tokens` / Σ `usage.prompt_tokens` over the window's
    events of the process that carry a non-null `cached_tokens` (and a prompt count),
    capped at 1, 4 dp; `null` when no such event or the prompt sum is 0;
  - `outcomes` = counts of `cache.outcome` = `hit` / `partial` / `miss` over the
    window's events; `null` when no event in the window has an outcome.

`GET .../events` returns `usage.cached_tokens` and, for an event with an outcome,
`"cache": {"outcome": ..., "report": null}` (`cache` is `null` otherwise); reports are
served only through the summary.

#### 3.4.1 Group layout (v1.4)

How the dashboard groups an app's processes. One document per app, stored and returned
as given (after validation); the server does not check that the processes exist.

```
Layout = {
  "schema": "tryaii.monitor.layout/1",
  "groups": [{"id": "<1-40 chars>", "name": "<1-40 chars>", "folder": "<string, ≤200 chars>"|null}],
  "assignments": {"<process name>": "<group id>"},
  "updated_at": "<ts>"|null
}
GET /v1/monitor/apps/{app_id}/layout
-> 200 Layout     (nothing stored: groups [], assignments {}, updated_at null)
PUT /v1/monitor/apps/{app_id}/layout
    {"groups": [...], "assignments": {...}}
-> 200 Layout     (replaces the whole document; updated_at = now; writes an audit row)
-> 400 {"error":"bad_layout","detail":"..."}
-> 404 {"error":"not_found"}   (not the caller's app, deleted, unknown)
```

- Same credentials, ownership and rate limit as the other dashboard endpoints.
- Both `groups` and `assignments` are required on PUT (`[]` / `{}` clear them). `schema`
  (must equal `tryaii.monitor.layout/1`) and `updated_at` (ignored) may be sent too, so a
  GET response can be PUT back as is; any other top-level key is a 400.
- `bad_layout` rules: at most **50** groups and **500** assignments; each group is an
  object with only `id`, `name` and `folder`; `id` 1–40 characters, unique; `name`
  trimmed, then 1–40 characters (stored trimmed); `folder` absent/`null` or a string of
  at most 200 characters, unique among the non-null folders; assignment keys (process
  names) 1–256 characters (the event `process` limit); every assignment value is the
  `id` of a group in `groups`.
- The PUT's audit row is `{"layout": {"groups": n, "assignments": n}}` (counts only).

## 4. Storage (API side)

Postgres (RDS, eu-north-1, same VPC as the API box; decided 2026-10-13), schema `monitor`:

```
apps(app_id pk, owner_user_id, org_id null, name, created_at, deleted_at)
api_keys(key_id pk, app_id fk, secret_hash, scopes text[], label, created_at, created_by,
         last_used_at, revoked_at, expires_at)
app_config(app_id pk, etag, default_mode, processes jsonb, updated_at, updated_by)
config_audit(id, app_id, at, user_id, change jsonb)
batches(batch_id pk, app_id, key_id, received_at, event_count, sdk jsonb, notice_version)
events(event_id pk, app_id, ts, process, call_file, call_line, call_function, provider,
       model_requested, model_resolved, model_used, mode, winner, winner_reachable,
       route_status, class, subclass, confidence, prompt_tokens, completion_tokens,
       total_tokens, est_cost_usd, est_cost_winner_usd, est_cost_anchor_usd null, latency_ms, stream, error_status,
       error_class, prompt_hash, vector bool, taxonomy_version, batch_id,
       cached_tokens null, cache_outcome null)           -- the last two: v1.4
       -- v1.5 (added by TS-231): cache_write_tokens null, cache_writes jsonb null,
       --                         cache_ttl_policy null
       PARTITION BY RANGE (ts), one partition per day, dropped after 90 days
cache_reports(app_id, process, ts, event_id, report jsonb, received_at,
              pk(app_id, process))                       -- v1.4: latest CacheReport per process
apps.emoji text null, apps.color text null               -- v1.4 (migration 0007), section 2.1
layouts(app_id pk fk, doc jsonb {groups, assignments}, updated_at, updated_by)
                                                         -- v1.4 (migration 0007), section 3.4.1
rollups_daily(app_id, day, process, model_used, winner, calls, errors, prompt_tokens,
              completion_tokens, cost_usd, cost_at_winner_usd, cost_at_anchor_usd, savings_usd,
              savings_available_usd, wins, pk(app_id, day, process, model_used, winner))
              -- model_used / winner store '' when absent (pk columns cannot be null);
              -- events pk is (event_id, ts) because the table is partitioned by ts
consent(user_id, notice_version, source, seen_at, pk(user_id, notice_version, source))
user_settings(user_id pk, upload_enabled, updated_at, updated_by)
deletions(app_id pk, owner_user_id, deleted_at, purged_at)
stats_model_daily(day, provider, model_used, winner, class, subclass, calls, errors,
                  prompt_tokens, completion_tokens, cost_usd, cost_at_winner_usd, apps)
```

Ingest: `POST /v1/monitor/events` validates and enqueues the batch on SQS
(standard queue, DLQ after 3 receives); the worker inserts events (`ON CONFLICT
DO NOTHING` on `event_id`), records the batch (with its `notice_version`),
upserts `rollups_daily` and, for each newly inserted event that carries a cache report,
upserts `cache_reports` (`ON CONFLICT (app_id, process) DO UPDATE ... WHERE
cache_reports.ts <= EXCLUDED.ts`; of two reports in one batch the later `ts` wins).
Reports older than 90 days are deleted with the expired events partitions. It never writes `consent`: a batch shows which notice the
SDK carries, not that a person saw it (section 7.2). The dashboard reads rollups for
totals and the events table only for the per-process detail windows.

## 5. CLI additions (both SDKs, byte-identical)

```
tryaii apps create <name>          -> prints the app id and the full key once
tryaii apps list
tryaii apps keys <app_id>          -> list keys (prefix only)
tryaii apps keys <app_id> --new [--label L]
tryaii apps keys <app_id> --revoke <key_id>
tryaii apps check                  -> validates TRYAII_API_KEY (or --key) and prints app + scopes
tryaii monitor set <process> managed --model <id>   (local config; the dashboard is the usual way)
tryaii monitor status              -> gains a "source" line: local | remote (etag, fetched_at) and the upload cursor state
```

`apps create|list|keys` need the user JWT (`tryaii login`); `apps check` needs
the app key. Exit codes: 0 ok, 1 server/network error, 2 usage.

## 6. Errors and limits

- Every error body is `{"error": "<snake_case>", "detail"?: "..."}`.
- Limits: 500 events / 1 MiB per batch; 60 batches per minute per key; 30
  config polls per minute per key (long polls count once); 120 management
  requests per minute per user.
- All endpoints set `Cache-Control: no-store`.

## 7. Privacy (v1.3)

### 7.1 Notice version

The CLI and SDK Data Notice (Confluence page 6782977, published on tryaii.com as the
"Command-line tool and SDK" section of the privacy policy, anchor `#cli-sdk`) has a
version: its publication date, `YYYY-MM-DD`. The first is **`2026-10-18`**. The SDKs,
the API and the website carry the same constant. A text change that changes what is
collected, why, how long or the controls gets a new version; a wording fix does not.

### 7.2 Consent records (user JWT or website service token)

```
POST /v1/monitor/consent   {"notice_version": "YYYY-MM-DD", "source": "device"|"cli"|"dashboard"}
-> 204
-> 400 {"error":"bad_request"}      (version not YYYY-MM-DD, unknown source)
GET  /v1/monitor/consent
-> 200 {"notice_version": "<current>", "seen": bool,
        "records": [{"notice_version", "source", "seen_at"}]}
```

A client posts **only after the person was shown the notice text**:

| `source` | Where | When |
|---|---|---|
| `device` | tryaii.com/device | the person presses Approve under the two-line summary |
| `cli` | `tryaii login`, `tryaii apps create`, `tryaii monitor upload on` | the CLI printed the notice to a terminal (stdout is a TTY) |
| `dashboard` | tryaii.com create-app dialog | the person confirms the dialog that shows the summary |

One row per (user, version, source); the first time wins. `seen` is true when any
record has the current version. Never posted from an app process with an app key.

### 7.3 Upload switch

```
GET /v1/monitor/settings             -> 200 {"upload": bool}
PUT /v1/monitor/settings  {"upload": bool} -> 200 {"upload": bool}
```

Per user (owner of the apps); default on. TryAii operations can set it as a kill
switch. While off:

- `POST /v1/monitor/events` with a key of that user's app answers
  `403 {"error":"upload_disabled"}` and queues nothing;
- `RemoteConfig` carries `"upload": false` (always present: `true` or `false`), and the
  etag changes with the switch; a PUT wakes the long polls of every app of the user.

The SDK stops uploading for the process on `upload: false` or on a 403
`upload_disabled`, keeps recording locally, and resumes when a later RemoteConfig
says `upload: true`. Precedence (SPEC section 9): remote `upload: false` >
`TRYAII_TELEMETRY=0` / `DO_NOT_TRACK=1` / `TRYAII_MONITOR_UPLOAD=0` > local
`tryaii monitor upload off` > default on.

### 7.4 Deletion, restore and retention

```
DeletedApp = App + {purge_at}       (deleted_at + 30 days)
DELETE /v1/monitor/data             -> 200 {"apps_deleted": n, "purge_at"}  (every app of the user)
```

- Deleting an app hides it at once, revokes its keys and stops uploads (its keys no
  longer validate). For **30 days** `POST /v1/apps/{id}/restore` brings it back with its
  data; the keys stay revoked (create a new one). Restore answers 409 `name_taken` when
  a live app of the same owner has the name.
- After 30 days the worker purges the app: events, rollups, cache reports, config,
  layout, config changes, batches, keys and the app row. A `deletions` row (app id, owner, deleted_at,
  purged_at) is kept as the record that the deletion was done.
- Retention: events 90 days; `cache_reports` 90 days after their `ts`; `batches` and `config_audit` 13 months (395 days);
  `rollups_daily` while the app exists; `deletions` and `consent` while the account
  exists.

### 7.5 Anonymous statistics

Statistics across accounts (which models win, at what cost) are read **only** from
`stats_model_daily`. The worker rebuilds yesterday's rows once a day from the events of
live apps. The table has no user, app, process, file, function or prompt-hash column,
and a row is written only when at least **3 different apps** contributed to it.
