# monitor upload and dashboard API — contract v1.8 (2026-10-25)

v1.8 (2026-10-25, Jira TS-307 / TS-308; it follows v1.7, Langfuse): the server owns the stable pin. RemoteConfig (section 3.3) gains the top-level `app_name` (the app's display name, TS-308) and, per process, `pin_version` (a counter that never goes down) and `pin` (the model a `stable` process is pinned to, chosen by the owner or the current winner, TS-307); the summary (section 3.4) gains `processes[].config_pin`. A config PUT that changes a process's mode or model (or removes the process) bumps its `pin_version`, so an SDK drops a local pin that no longer belongs to the dashboard's choice (unpin, a new pinned model), and an SDK of 0.8.3 or newer sends the server's `pin.model` without routing in-path (`SPEC.md` section 2.5). All additive: the schema id stays `tryaii.monitor.remote/1`, and a reader must tolerate the new keys' absence (a v1.7 server); an SDK then keeps the v1.7 behaviour.

v1.7 (2026-10-24, Jira TS-245 / TS-249, epic "Works with Langfuse"; it follows v1.6, Claude Code sessions, TS-297 / TS-290): two optional event fields and one optional batch field (section 3.2.4). `usage.reasoning_tokens` is the reasoning share of `completion_tokens` (included in it, never added on top), read from `usage.completion_tokens_details.reasoning_tokens`. `otel` is the OpenTelemetry span the call belongs to (`{trace_id, span_id}`, plus `origin: "tryaii"` when the SDK made that span itself), written only when there is one, so the SDK's Langfuse export can put its span in the same trace as the developer's own (`SPEC.md` section "Langfuse export"). The upload batch gains `integrations: ["langfuse"]` while the SDK's Langfuse export is on, so the funnel can be measured. All three are optional; the schema ids stay `tryaii.monitor.event/1` and `tryaii.monitor.upload/1`, older events and batches stay valid, and the batch carries the event fields exactly as stored. The server stores `usage.reasoning_tokens` as `events.reasoning_tokens`, keeps the known names of `integrations` per app in `app_integrations` (section 4) and ignores `otel`. Nothing in v1.7 changes a v1.6 field.

v1.6 (Claude Code sessions and live advice, Jira TS-297 / TS-290; released with Data Notice
`2026-10-24`, section 7.1). All changes are additive; a reader must tolerate their absence:
§2 `AppWithStats.sdks` (which SDKs an app's data came from: `live`, `import` or `sdk`); §3.2
the optional upload field `sdk.source` and the optional event field `cache_cold`; §3.4
`since=90d`, per-process `advice`, `GET .../breakdown` and `GET .../claude-code/sessions`
(with each session's and each cold restart's `project`), list-price fallbacks for cache and
cold-restart costs with their `price_source`, and one model id per model (`aliases`); §4 the
columns `events.session_ref`, `events.effort`, `events.cache_cold` and the table
`process_advice`; §7.1 notice version `2026-10-24`; §7.4 retention of session references (7 days) and advice (90 days); §8.3
`effort` and `session.id` (as `session_ref` only) join the allowlist; new §9
`POST /v1/monitor/claude-code/annotations`. This file mirrors the server's
`tryaii-sdk-api/docs/CONTRACT-monitor-v1.md` v1.6 for these sections (SDK side: `SPEC.md`
§10.5, §10.8, §10.9).

v1.5 (Jira TS-228, epic TS-227 "cache intelligence"): the event carries the **write** side of prompt-cache usage next to v1.4's `usage.cached_tokens` — `usage.cache_write_tokens`, `usage.cache_writes[] {tokens, ttl}` and the top-level `cache_ttl_policy` (section 3.2.2, SDK side `SPEC.md` section 3.1b). All three are optional and nullable; the schema id stays `tryaii.monitor.event/1`, older events stay valid, the v1.4 `cache` block is unchanged, and the upload batch carries the fields exactly as stored. The server stores them (TS-231: `events.cache_write_tokens`, `cache_writes`, `cache_ttl_policy` and the daily cache rollups, section 4). The dashboard summary and events read gain the same fields (section 3.4).

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
- The app key also authorizes `GET /v1/catalog/live` (catalog contract v1.4,
  `docs/catalog/CONTRACT-catalog-v1.md` section 4, TS-294): the same signed full
  catalog a logged-in user gets, checked against the app owner's `catalog:full`
  entitlement, rate-limited per `key_id`. An invalid or revoked key answers that
  endpoint's own `401 {"error":"invalid_token"}`, exactly as a missing token. So a
  server needs only `TRYAII_API_KEY`, no personal `tryaii login`.

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
                      processes: int, keys_active: int,
                      sdks: [{lang, version, source: "live"|"import"|"sdk", last_seen}]}   // v1.6
KeySummary   = {key_id, label, prefix: "tryaii_<key_id>_", created_at,
                last_used_at|null, revoked_at|null, expires_at|null}
```

`sdks` (v1.6) lists where the app's data came from, from the upload batches received in the
last 90 days: one entry per (`lang`, `version`, `source`) with `last_seen` = the latest batch's
receive time, latest first (ties by `lang`, `version`, `source`), at most 10; `[]` when there
is none. `source`:

- `live`: Claude Code's own telemetry (section 8; `lang` `"claude-code"`, `version` `"otlp/1"`);
- `import`: the SDK's Claude Code log import (`tryaii claude-code connect`, `SPEC.md` §10.5;
  `lang` and `version` of the SDK that uploaded it): a batch that sent `sdk.source`
  `"claude-code-import"` (section 3.2), or that sent no `sdk.source` and in which every event
  has the importer's shape (`provider` `"anthropic"`, `mode` `"off"`, `latency_ms`,
  `call_site.line` and `call_site.function` null; a wrapped call always has a latency);
- `sdk`: every other upload (an app instrumented with the SDK).

The dashboard shows "live" / "imported" badges from it and treats an app with a `live` or
`import` entry as a Claude Code app. Batches stored before v1.6 have no `source` and count as
`sdk` (or `live` for `lang` `"claude-code"`).

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
  "sdk": {"lang": "python"|"node", "version": "0.7.0",
          "source"?: "claude-code-import"},   // v1.6, optional: sent only with Claude Code import batches
  "notice_version": "2026-10-24",    // notice version built into the SDK (not proof of consent)
  "integrations": ["langfuse"],      // v1.7, optional: present only when the Langfuse export is on
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
included; Anthropic, Bedrock: excluded). The server reads the convention from the
event's `provider`: `anthropic` and `bedrock` exclude the cached and written tokens,
every other value includes them. The cache hit rates of TS-231 (summary `cache_usage`,
`cache_hit_rate`, rollups) divide by `prompt_tokens + cached_tokens +
cache_write_tokens` for those two, else by `max(prompt_tokens, cached_tokens)`. The
SDKs' Anthropic wrapper (Anthropic, Bedrock and Vertex clients of the Anthropic SDK,
all with Messages-API usage) and the Claude Code import send `anthropic`;
OpenAI-shaped clients send `openai`, `openrouter` or `other`. `null` means "not
reported", `0` means "reported as zero" (below the provider's minimum prompt size
caching silently does nothing). A batch with events that lack the keys is valid: a missing key reads as
`null`.

Lifetimes are what the dashboard's hot-cache bar is built on: a `cache_writes`
entry with an exact lifetime is hot until `ts + ttl`; `"explicit"` writes and
providers without a breakdown draw nothing (only provider-promised lifetimes are
shown). The server stores them (TS-231): `events.cache_write_tokens`,
`events.cache_writes jsonb`, `events.cache_ttl_policy` and the cache columns of
`rollups_daily` (section 4), which the bar's rollup reads.

#### 3.2.3 Claude Code import fields (v1.6)

v1.6: `sdk.source` is optional; the only accepted value is `"claude-code-import"`, sent with a
batch of events built by the Claude Code importer (`tryaii claude-code connect`, `SPEC.md`
§10). Any other value is a `400 bad_batch` (`sdk.source must be claude-code-import when
present`). It only labels the batch (`AppWithStats.sdks`, section 2); the events are stored as
any others. An SDK sends it only to a v1.6 server; an older server ignores it. (SDK side:
the Python uploader sends it for the batches of the directory `tryaii claude-code connect`
uploads, `<data_dir>/monitor/claude-code/`, and never for wrapped-call uploads; `SPEC.md`
§3.4, §10.5. Batches of older SDKs carry no `sdk.source`; the server then recognises an
import batch by its events' shape, section 2.)

v1.6 adds one optional event field, sent only by the Claude Code importer:

```
cache_cold           true|false|null    true when a main-thread call was a cold cache restart of its
                                        Claude Code session (rule in section 3.4, "sessions"), false
                                        for any other main-thread call, null for a subagent call or
                                        when unknown; absent = null
```

The SDK groups calls by session on the machine; the session itself is never uploaded. A
value other than a boolean or null is a `400 bad_batch` (`events[i].cache_cold must be a
boolean`). Fields named `session_ref` or `effort` in an uploaded event are ignored: only the
OTLP receiver (section 8) sets them, so an upload cannot forge a session reference. In the
SDK's event line it follows `cache_ttl_policy` (`SPEC.md` §3.1, §10.5); the wrappers leave it
out.

#### 3.2.4 Reasoning tokens, active trace and integrations (v1.7)

v1.7 adds two optional event fields and one optional batch field (`SPEC.md` section
3.1 for `usage.reasoning_tokens`, section "Langfuse export" for `otel` and the export
itself; the SDK sends the event fields **unchanged**):

```
usage.reasoning_tokens   int|null         output tokens the model spent on reasoning, from
                                          usage.completion_tokens_details.reasoning_tokens;
                                          INCLUDED in completion_tokens, never added on top
otel                     object|null      {"trace_id": "<32 hex>", "span_id": "<16 hex>"
                                          [, "origin": "tryaii"]}: the span the call belongs
                                          to, fixed at call time; the key is present only
                                          when there is one; origin only when the SDK made it
integrations (batch)     list             ["langfuse"] while the SDK's Langfuse export is on;
                                          the key is absent otherwise
```

**`usage.reasoning_tokens`** — `integer >= 0 | null`, the last key inside `usage`.
Read from the provider's response for streamed and non-streamed calls alike (for a
stream, the usage of the last chunk). `null` means "not reported" (no
`completion_tokens_details`, or no `reasoning_tokens` in it), `0` means "reported as
zero". It is part of `completion_tokens` as the provider counts it, so
`completion_tokens`, `total_tokens` and every cost figure stay as they were; a reader
must never add it to `completion_tokens`. Langfuse shows output and reasoning tokens
apart (TS-244 pilot); this field lets the dashboard do the same. The server stores it
as `events.reasoning_tokens` (section 4); a value it cannot read (not an integer,
negative, above 2^53) is stored as null, never a `400`.

**`otel`** — `{"trace_id": "<32 hex>", "span_id": "<16 hex>"[, "origin": "tryaii"]}`,
the span the TryAii observation belongs to, fixed by the wrapper at call time.
Ids are lower-case hex, the W3C trace-context sizes (32 and 16 characters).
Without `origin` it is the OpenTelemetry span active around the call (for example
a Langfuse `@observe` span, or Langfuse's own generation in Node); the SDK's
Langfuse export makes the TryAii span its child. `"origin": "tryaii"` is present
only when the wrapper made the parent span itself (Python, Langfuse's OpenAI
integration and no active span, `SPEC.md` §11.2): the ids are then the ones the
export gives the TryAii span, sent as the trace root. Any other `origin` value is
ignored. Absent when no span applies. The server **ignores** `otel`: it is neither
stored nor validated beyond the batch limits.

**`integrations`** — on the batch, after `notice_version`. `["langfuse"]` while the
SDK's Langfuse export is really on: the switch `TRYAII_LANGFUSE=on` (also `1`,
`true`), an app key in `TRYAII_API_KEY` and both `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` (`SPEC.md` §11.1); absent otherwise, and never an empty list.
The server keeps only known names (today `langfuse`; case and surrounding spaces
ignored, duplicates dropped) from the first 16 entries; an unknown name, a
non-string, a value that is not a list, or a missing key is ignored, never a `400`.
For each kept name it upserts `monitor.app_integrations` (section 4: one row per
app and name, with `first_seen` / `last_seen` = the `received_at` of the first and
the latest batch that named it), in the batch transaction and only for a batch not
seen before. A batch without the key counts as no integration. The admin funnel
("apps that upload with the Langfuse export on") reads that table.

A batch with events that lack `usage.reasoning_tokens` or `otel` is valid: a missing
key reads as `null`.

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
  "app_name": "<the app's display name>",               // v1.8 (TS-308); always sent by a v1.8 server
  "default_mode": "off"|"stable"|"dynamic"|"managed"|null,
  "processes": {
    "<process name>": {
      "mode": "off"|"stable"|"dynamic"|"managed",
      "model": "<catalog id>"|null,                     // managed only; null for stable (the pin carries it)
      "pin_version": <int >= 0>,                        // v1.8 (TS-307); always sent by a v1.8 server
      "pin": {"model": "<catalog id>",                  // v1.8 (TS-307); non-null only when mode == "stable"
              "version": <int, == pin_version>,
              "source": "owner"|"winner",
              "pinned_at": "...Z",
              "expires_at": "...Z"|null}|null
    }
  },
  "updated_at": "...Z", "updated_by": "<user id>"|null
}
```

Key order is as shown: `app_name` after `app_id`; `pin_version` and `pin` after
`model`. A reader must tolerate their absence (a v1.7 server).

**Server-owned stable pin (v1.8, TS-307).**

1. `pin_version` is a per-process counter stored with the config. It never goes
   down and survives the process going to `off` or being removed (it stays in
   storage; when the process reappears it continues from the stored value).
2. A PUT that changes a process's `mode` or `model` bumps that process's
   `pin_version` by 1 — including unpin (the dashboard saves `off`) — and so
   does removing the process (set to `null`). A PUT that leaves the mode and
   model unchanged does not; in particular re-saving `stable` without a model
   re-pins the current `pin_candidate` and bumps `pin_version` only when that
   winner is a different model than the stored pin.
3. Saving `stable`: with `model` (validated like `managed`: a non-empty
   string) →
   `pin = {model, version, source: "owner", pinned_at: now, expires_at: null}`,
   held until the owner changes it; without `model` → the current
   `pin_candidate` (section 3.4) with `source: "winner"` and `expires_at = now +
   24h`; no candidate → `pin: null` (the SDK then pins locally). The response's
   `model` stays `null` for `stable`.
4. Refresh of a `winner` pin (lazy, no cron): whenever a RemoteConfig is built
   (long poll and dashboard GET), a `stable` process whose pin is `null` (winner
   pending) or whose `winner` pin has `expires_at <= now` gets `pin_candidate`
   recomputed: a new model → a new pin and `pin_version + 1`; the same model →
   same version, `pinned_at = now`, `expires_at = now + 24h`; still no candidate
   → unchanged. Any stored change bumps the etag. An `owner` pin is never
   refreshed. The recompute is cached per app (about 60 s) or runs only when
   expired.
5. `pin` is `null` for every mode other than `stable`.
6. Only a process entry carries a pin. A process that is `stable` only through
   `default_mode` (no entry in `processes`) gets no server pin and no
   `pin_version`; the SDK pins it locally, as in v1.7.

**SDK side** (`SPEC.md` section 2.5): a `stable` call with a remote `pin` sends
`pin.model` (reachability as for `managed`; unreachable → the anchor with
`route_status: "unreachable_pin"`) and stores it in `pins.json` (`basis.window:
"server"`); without one, a local pin counts only while its `version` equals the
process's `pin_version`; a process whose remote mode is not `stable` loses its
local pin on its next call.

**`app_name` (v1.8, TS-308)** is the app's current display name (the one the
dashboard shows and `tryaii apps create <name>` sets). The key, not `wrap(app=)`,
decides which dashboard app the events reach; an SDK warns once per local app when
the two names differ (`SPEC.md` section 3.3).

- The SDK keeps the last answer in `<data_dir>/monitor/<app>/remote.json`
  (same shape plus `"fetched_at"`), re-polls immediately after a 200/304, waits
  30 s after an error, and applies the config on the **next call** (the config
  layer reads the file through the existing mtime cache plus an in-process
  write counter; the first poll of a process sends `wait=0` and the first
  call waits for its answer at most 500 ms, SPEC.md §3.3; precedence stays
  env > remote > `wrap(mode=)` > `config.json` > default).
- `managed` requires `model`; the SDK sends that model if it is reachable from
  the app's provider class, else the anchor (recorded as `mode: "managed"`,
  `model_used: <anchor>`, `route_status: "unreachable_managed"`).
- A `304` or an error leaves the cached config in force; a missing file means
  "no remote config" (local behaviour, exactly slice 1).

### 3.4 Dashboard reads and writes (user JWT or service token)

```
GET /v1/monitor/apps/{app_id}/summary?since=24h|7d|30d|90d       (90d: v1.6)
-> 200 {
  "app_id", "since", "generated_at",
  "totals": {calls, errors, prompt_tokens, completion_tokens, cost_usd,
             cost_at_winner_usd, savings_usd, savings_available_usd,
             cached_tokens|null, cache_write_tokens|null},        // v1.5: sums; null when no event reported them
  "processes": [{
     "process", "mode", "managed_model"|null,
     "pin": {model, expires_at|null, observed_at}|null,   // TS-295, see below
     "calls", "errors", "tokens", "cost_usd", "cost_at_winner_usd", "savings_usd",
     "savings_available_usd",             // v1.4, see below
     "cached_tokens"|null, "cache_write_tokens"|null,   // v1.5: sums over the window; null when no event reported them
     "top_winner": "<model id>"|null, "top_winner_share": 0..1,
     "pin_candidate": {model, window, events, counts}|null,   // TS-295, see below
     "config_pin": RemoteConfig pin|null,  // v1.8 (TS-307): processes.<p>.pin of section 3.3
     "anchor": "<model id>"|null, "traffic_light": "green"|"yellow"|"red"|"grey",
     "models": [{model, calls, share, cost_usd, wins, win_share, reachable,
                 aliases}],             // aliases: v1.6, see "Model ids" below
     "files": [{file, function|null, calls}],
     "classes": [{class, share}],         // from classification.category, until task profiles land
     // v1.4, optional for readers (absent from a v1.3 server):
     "prompt_tokens", "completion_tokens",
     "latency": ProcessLatency|null,
     "cache": ProcessCache|null,
     "cache_usage": ProcessCacheUsage|null,   // v1.5 (TS-231), see below
     "advice": ProcessAdvice|null         // v1.6, see below
  }],
  "daily": [{date, calls, cost_usd, cost_at_winner_usd}]
}
GET /v1/monitor/apps/{app_id}/events?limit=100&before=<ts>&process=<name>
-> 200 {"events":[Event...], "next_before": "<ts>"|null}
   // v1.5: each Event carries usage.cached_tokens / cache_write_tokens / cache_writes
   // and cache_ttl_policy as uploaded (null for events stored before the server kept them, TS-231)
   // v1.7: usage.reasoning_tokens from events.reasoning_tokens (null when not reported
   // or stored before v1.7); never otel
GET /v1/monitor/apps/{app_id}/config               -> 200 RemoteConfig
PUT /v1/monitor/apps/{app_id}/config
    {"default_mode"?: ..., "processes": {"<name>": {"mode": ..., "model"?: ...}}}
-> 200 RemoteConfig   (merges per process; a process set to null is removed; writes an audit row;
                       bumps etag; wakes long-polls; v1.8: a changed mode/model bumps the process's
                       pin_version, and stable sets the pin, section 3.3)
-> 400 {"error":"bad_config","detail":"..."}  (managed without model, unknown mode; v1.8: stable
                                               with a model that is not a non-empty string)
GET /v1/monitor/apps/{app_id}/audit?limit=50    -> 200 {"entries":[{at, user_id, change}]}
GET /v1/monitor/models?app_id=...              -> 200 {"models":[{id, provider, quality, blended_price_usd_per_m, reachable}]}
GET /v1/monitor/apps/{app_id}/layout           -> 200 Layout                  (v1.4, section 3.4.1)
PUT /v1/monitor/apps/{app_id}/layout  {groups, assignments}  -> 200 Layout   (v1.4, section 3.4.1)
GET /v1/monitor/apps/{app_id}/breakdown?since=7d|30d|90d                  -> 200 Breakdown        (v1.6)
GET /v1/monitor/apps/{app_id}/claude-code/sessions?since=24h|7d|30d|90d   -> 200 ClaudeCodeSessions (v1.6)
```

v1.6 (additive; a reader must tolerate their absence):

- `since=90d`: the last 90 UTC calendar days including today, like `7d` / `30d`.
- `processes[].advice` (`ProcessAdvice`): the latest advice document `tryaii claude-code sync`
  sent for the process (section 9), generated at most 90 days ago, else `null`:

```
ProcessAdvice = {process, thread: "main"|"subagent", window_days: 1..90, generated_at,
                 tasks, calls, est_cost_usd|null,
                 classes: [{class, share}], good_enough_shares: [{model, share}],
                 recommendation: {model, alias|null, good_enough_share, saving_usd|null,
                                  saving_usd_per_month|null}|null,
                 confidence: "low"|"medium"|"high"}
```

- **Model ids** (`processes[].models[].model`, `Breakdown.rows[].model`): one id per model.
  Claude Code live calls record Claude's native id (`claude-opus-5-5`, no `model_resolved`),
  imported calls the same native id with the catalog id resolved (`anthropic/claude-opus-5.5`).
  A native Claude id (`claude-...`) is shown as the catalog id whose native form it is (prefix
  `anthropic/` dropped, dots as hyphens, a `-YYYYMMDD` suffix ignored) when exactly one id of the
  live catalog, or of the window's resolved anchors and winners (summary only), has it; the
  ids merged into it are listed in `aliases` (sorted, `[]` when none) and their figures summed.
  Other ids are shown as recorded. Without the catalog a breakdown shows ids as recorded.
- **List-price fallbacks** (cache figures and cold-restart costs; the SDK's Claude Code pricing,
  `tryaii/claude_code/pricing.py`, uses the same tables). Prices come from the live catalog
  first. For an Anthropic model whose catalog entry has an input price but no cache read price,
  cache read = input × the model's multiplier (`claude-opus-5-5` 0.05; `claude-fable-5-1` and
  `claude-mythos-5-1` 0.025; any other Claude model 0.1); without a cache write price, cache
  write = 1.25 × input (5-minute; 2 × input for a 1-hour write where the lifetime is known). A
  Claude model the catalog does not price at all takes Anthropic's list price from a fallback
  table (`claude-opus-5` $5 / $25 per MTok; `claude-haiku-5-5` $0.10 / $0.50, above a
  100K-token prompt $0.50 / $2.50), its cache prices derived as above. A model of another
  provider gets nothing derived. `price_source` says where a figure's prices came from, the
  least direct part winning: `"catalog"` (all from the catalog), `"list_multipliers"` (a cache
  price derived from a catalog input price), `"list_fallback"` (the input price from the
  fallback table); `null` when nothing was priced. The summary's
  `processes[].cache_usage.price_source` (below) is the weakest over the process's cache
  figures (its savings ignore the long-context tier: they are sums, not per call).
- `processes[].cache_usage` (`ProcessCacheUsage`; v1.5, TS-231; `price_source` v1.6): the
  process's prompt-cache reads, writes and savings over the window; `null` when no event of the
  process in the window reports a cache read or write and no cache write of it is stored from
  the last 90 days:

```
ProcessCacheUsage = {hit_rate: 0..1|null, input_tokens: int,
                     saved_usd|null, read_saved_usd|null, write_cost_usd|null,
                     saved_includes_writes: bool,
                     price_source: "catalog"|"list_multipliers"|"list_fallback"|null,   // v1.6
                     last_write: {at, ttl|null, tokens}|null,
                     last_writes: [{at, ttl|null, tokens}],
                     daily: [{date, cached_tokens|null, cache_write_tokens|null,
                              hit_rate: 0..1|null, saved_usd|null}]}
```

  - `hit_rate` = Σ cached tokens / Σ input tokens over the window's events that report both,
    the input counting the cache (prompt + cached + written for a provider whose prompt count
    excludes the cache, else max(prompt, cached)); capped at 1, 4 dp; `null` when the input sum
    is 0. `input_tokens` is that input sum. Unlike `cache.hit_rate` it needs no cachelint report.
  - `read_saved_usd` = Σ over models of cached × (input price − cache read price);
    `write_cost_usd` = Σ over models of written × (cache write price − input price), the extra
    paid to fill the cache; each `null` when a model that needs it has no price (list-price
    fallbacks above; writes are priced as 5-minute writes, the summary does not split them by
    lifetime). `saved_usd` = `read_saved_usd` − `write_cost_usd`, the write cost subtracted only
    when known (`saved_includes_writes` says whether it was); `null` when `read_saved_usd` is.
    USD, 6 dp, at the prices of the model the tokens were sent to.
  - `last_write`: the latest stored cache write of the process; `last_writes`: the latest one per
    lifetime (`ttl` as uploaded in `cache_writes`, `null` = the provider gave no per-lifetime
    breakdown); at most 90 days old, so a reader can tell until when the cache is warm
    (`at` + ttl).
  - `daily`: one entry per day of the window, from the daily rollups; a field is `null` on a day
    without that cache data.
- `Breakdown` (rollups by day; `24h` is not offered because rollups are daily):

```
Breakdown = {
  "app_id", "since", "generated_at",
  "rows": [{date, process, model|null, calls, errors, prompt_tokens, completion_tokens,
            cached_tokens, cache_write_tokens, cost_usd, aliases}], // rollups_daily summed over winner
                                                                  // (and over aliases, "Model ids"),
                                                                  // ordered by date, process, model (code points)
  "projects": [{project, calls, cost_usd}],   // events.call_file over the window, at most 100, by calls desc:
                                              // a Claude Code app's project (working-directory base name,
                                              // section 9), an SDK app's call-site file; "\" shown as "/"
  "effort": [{process, effort, calls}]        // Claude Code live calls that carry effort (section 8.3)
}
```

- `ClaudeCodeSessions` (the cache timer; only Claude Code live calls carry a session; the
  session reference itself is never returned):

```
ClaudeCodeSessions = {
  "app_id", "since", "generated_at", "cost_basis": "list_price",
  "price_source": "catalog"|"list_multipliers"|"list_fallback"|null,   // over every cold restart
  "ttl": {"main": {ttl: "5m"|"1h", inferred_from: "default"|"observed"},
          "subagent": {ttl, inferred_from}},
  "sessions": [{label: "Session N", started_at, last_call_at, last_model|null, last_effort|null,
                last_thread: "main"|"subagent", project|null, calls, expires_at, state: "warm"|"cold",
                cold_restarts, cold_restart_cost_usd|null}],
  "cold_restarts": [{ts, model|null, process, reason: "expired"|"model_switch"|"other"|null,
                     write_tokens, cost_usd|null, price_source|null, session_label|null,
                     project|null}],                    // newest first, at most 200
  "cold_restarts_total": n,                             // every cold restart in the window
  "daily": [{date, cold_restarts, cost_usd|null}]      // one entry per UTC day of the window
}
```

  Rules (`app/monitor/sessions.py`; the SDK's import uses the same rule, `SPEC.md` §10.9,
  fixture `shared/monitor/fixtures/claude-code/cold-restarts.json`):
  - **Cold restart.** Within one session's main-thread calls (`process` `main`; every other
    process is a subagent and is excluded), in time order: the first main call is never cold;
    a later one is cold when the gap since the previous main call of the same session is
    **more than 5 minutes** and `cache_write_tokens >= max(1024, cached_tokens)`.
  - **ttl** of a cold call: its own `cache_writes` split (`1h` when the 1h part is larger,
    else `5m`); without one, the session's last split; else the app's main-thread TTL. (The
    SDK's import, which has no app TTL, defaults to `5m`; the fixture passes its
    `default_ttl` explicitly.)
  - **reason**: `expired` when the gap ≥ ttl; else `model_switch` when the model differs from
    the previous main call's; else `other`.
  - **cost_usd**, at list price: per lifetime part, tokens × (write price − cache read price),
    write price = 1.25 × input (5m) or 2 × input (1h), with the input and cache read prices of
    the live catalog, else the list-price fallbacks above (a fallback model's long-context tier
    applies when the call's prompt, input + cache read + cache write, exceeds its threshold);
    a write without a split is one part at the call's ttl. `null` when neither prices the
    model; a sum is `null` when any part is. `price_source` as above (`null` when unpriced).
  - **ttl inference**: main thread `1h`, subagents `5m` by default (`inferred_from: "default"`);
    the latest 5m / 1h cache write the app's imported events reported for the thread overrides
    it (`"observed"`).
  - **sessions**: those whose last call is in the window and that have a main-thread call,
    labelled `Session 1, 2, ...` by their first call; `expires_at` = last main call + the
    session's ttl; `state` = `warm` while now < `expires_at`. Session references live 7 days
    (section 7.4), so `sessions` cover at most the last 7–9 days whatever `since` says.
  - **cold_restarts**: every cold restart of the window, newest first (by `ts`), at most 200;
    `cold_restarts_total` counts them all and equals the sum of `daily[].cold_restarts`. They
    are the calls that still carry a session (rule above, at read time; `session_label` is
    their session's) plus the calls stored with `cache_cold` true and no session (imported
    calls, and live calls whose reference was cleared), each call listed once. A stored mark
    has `session_label` `null` and `reason` `null` (its gap is not known), its own `project`,
    and its cost priced as above at its own split's ttl, else the app's main-thread TTL.
  - **project**: a call's `events.call_file`, which a live call gets only from an annotation
    (section 9: the working-directory base name). A session's `project` is the latest non-null
    one among its calls (its most recent project), `null` when none has one; a cold restart's
    is its own call's, `null` when it has none.
  - **daily**: cold restarts per UTC day of the window: the same calls as `cold_restarts`
    (before the cap; the worker marks live calls every 5 minutes, those between 26 h and 1 h
    old), so the history outlives the 7-day references.
  - Auth, limits and errors as the other dashboard reads (user JWT or service token,
    `404 not_found` for an app of another user, `400 bad_request` for an unknown `since`).

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

Stable pins (TS-295, v1.8 TS-307; additive, a reader must tolerate their absence). Since
v1.8 the pin comes from the server first: `config_pin` is the configured pin and `pin`
is what the events show, so a dashboard can tell when an app still sends another model
(an SDK older than 0.8.3, or one that has not made its next call yet):

- `config_pin` (v1.8) — the same object as RemoteConfig `processes.<p>.pin` (section
  3.3) or `null`: the model the server pinned (`source: "owner"` or `"winner"`, with
  `expires_at`). Present for every process; `null` unless the mode is `stable` and a
  pin is set.

- `pin_candidate` — `{"model", "window": "24h"|"7d", "events", "counts": {id: n}}` or
  `null`: the model a stable pin made now would hold, by the SDK's own rule (SDK
  SPEC.md §2.5, "the winner rule") over the uploaded events, **independent of
  `since`**: the reachable winners (`winner` non-null, `winner_reachable` true) of
  the process in `[now − 24h, now]`, or `[now − 7d, now]` when that holds fewer
  than 20; `counts` ordered by count desc, then the catalog `quality` (as in
  `GET /v1/monitor/models`) desc, then `blended_price_usd_per_m` asc (unknown after
  known), then the model id asc; `model` = the first entry. `null` without
  reachable winners. The dashboard names this model for "Pin X · stable" / "Today
  that is X". It equals the SDK's pin when the SDK sees the same events (one
  instance with uploads on) and the same catalog release.
- `pin` — the observed pin. `pins.json` is never uploaded, so the server reports
  what the events show: when the process's mode is `stable` and its latest
  event of the last 24 h was a `stable` call sent to another model than the one in
  the code (`model_used != model_requested`), `{"model": <catalog id where it can be
  told, else model_used>, "expires_at": null, "observed_at": <that call's ts>}`;
  `null` otherwise (no pin seen yet, or the pin is the anchor itself).
  `expires_at` stays in the shape for a future upload of pins; it is `null` today.

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
       -- v1.8: each process's pin_version and pin are stored with the config; a removed
       --       process keeps its pin_version (section 3.3); the columns are the server's choice
config_audit(id, app_id, at, user_id, change jsonb)
batches(batch_id pk, app_id, key_id, received_at, event_count, sdk jsonb, notice_version)
       -- sdk = {lang, version} (+ "source": "import" for a Claude Code import batch, v1.6)
app_integrations(app_id, integration, first_seen, last_seen,
                 pk(app_id, integration))                -- v1.7 (TS-249): batch integrations
events(event_id pk, app_id, ts, process, call_file, call_line, call_function, provider,
       model_requested, model_resolved, model_used, mode, winner, winner_reachable,
       route_status, class, subclass, confidence, prompt_tokens, completion_tokens,
       total_tokens, est_cost_usd, est_cost_winner_usd, est_cost_anchor_usd null, latency_ms, stream, error_status,
       error_class, prompt_hash, vector bool, taxonomy_version, batch_id,
       cached_tokens null, cache_outcome null)           -- the last two: v1.4
       -- v1.5 (added by TS-231, migration 0008): cache_write_tokens null, cache_writes jsonb null,
       --                         cache_ttl_policy null
       -- v1.6 (migration 0009): session_ref null, effort null, cache_cold bool null
       -- v1.7: reasoning_tokens bigint null (usage.reasoning_tokens; not in the rollups)
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
process_advice(app_id, process, thread, generated_at, doc jsonb, received_at,
               pk(app_id, process))                                 -- v1.6 (migration 0010)
```

v1.6: `events.session_ref` (16 hex characters, section 8.3) is set only by the OTLP receiver,
indexed `(app_id, session_ref, ts) WHERE session_ref IS NOT NULL`, and cleared after 7 days;
`events.effort` is `low|medium|high|xhigh|max` or null; `events.cache_cold` comes from an
imported event's `cache_cold` or the worker's 5-minute marking of live calls (section 3.4),
indexed `(app_id, ts) WHERE cache_cold`. None of the three is in `rollups_daily` or
`stats_model_daily`. `process_advice` keeps the newest advice document per (app, process).

Ingest: `POST /v1/monitor/events` validates and enqueues the batch on SQS
(standard queue, DLQ after 3 receives); the worker inserts events (`ON CONFLICT
DO NOTHING` on `event_id`), records the batch (with its `notice_version`),
upserts `app_integrations` for the batch's known `integrations` (v1.7), upserts `rollups_daily` and, for each newly inserted event that carries a cache report,
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
                                      v1.8: a "key app" line when remote.json's app_name differs from the local app
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
version: its publication date, `YYYY-MM-DD`. The first is **`2026-10-18`**; the current one is
**`2026-10-24`** (v1.6: Claude Code session references, effort, project labels and process
advice; sections 8.3, 9 and 7.4). The SDKs, the API and the website carry the same constant,
and a new version asks the person for the notice again (section 7.2, `seen`). A text change that changes what is
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
  layout, config changes, batches, integrations (`app_integrations`, v1.7), keys and the app row. A `deletions` row (app id, owner, deleted_at,
  purged_at) is kept as the record that the deletion was done.
- Retention: events 90 days; `cache_reports` 90 days after their `ts`; `batches` and `config_audit` 13 months (395 days);
  `rollups_daily` and `app_integrations` while the app exists; `deletions` and `consent` while the account
  exists.
- v1.6: `events.session_ref` **7 days** (the worker clears it daily on calls 7 to 9 days old;
  the calls themselves stay 90 days without it); `process_advice` **90 days** from its
  `generated_at`. Both go with the app at the purge. Rotating the server's session-ref secret
  unlinks every stored reference at once; together with app deletion that is the erasure
  path for session references.

### 7.5 Anonymous statistics

Statistics across accounts (which models win, at what cost) are read **only** from
`stats_model_daily`. The worker rebuilds yesterday's rows once a day from the events of
live apps. The table has no user, app, process, file, function or prompt-hash column,
and a row is written only when at least **3 different apps** contributed to it.

## 8. Claude Code live telemetry (v1.5)

Contract v1.5, Jira TS-291. Claude Code can export its own telemetry over OpenTelemetry
(code.claude.com/docs/en/monitoring-usage). The server accepts that log export and turns
each Claude Code API call into an ordinary monitor event of the user's `claude-code` app,
metadata only, so the existing dashboard shows Claude Code usage with no website change.

The SDK never calls this endpoint itself: Claude Code's own OpenTelemetry exporter sends
the log events, and the SDK only writes the settings and runs the headers helper
(`SPEC.md` §10.8, `tryaii/claude_code/telemetry.py`).

**Source of the user-facing privacy statement.** Section 8.3 is what TryAii promises about
Claude Code telemetry; the privacy policy, the CLI and the dashboard quote it. A change to
the allowlist is a change to that promise (and to `KEPT_ATTRIBUTES` in the API server's
`app/monitor/otlp.py`, pinned by a test).

### 8.1 Route and client settings

```
POST /v1/otlp/claude-code/v1/logs
Content-Type: application/json             (OTLP/HTTP, JSON encoding: ExportLogsServiceRequest; parameters such as charset allowed)
Content-Encoding: gzip | identity          (optional; default identity)
Authorization: Bearer tryaii_<key_id>_<secret>
X-Tryaii-Notice-Version: YYYY-MM-DD        (optional)
```

The body is at most 4 MiB as sent, and a gzip body at most 4 MiB inflated (section 8.6).
The cap is larger than an SDK upload's 1 MiB because the export also carries every event
that is dropped here.

`tryaii claude-code connect` (SDK `SPEC.md` §10.8) merges this block into Claude Code's
**user** settings file (`~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json`;
the exporter variables have no effect in a project's `.claude/settings.json`). `<api url>`
is `TRYAII_API_URL`, default `https://api.tryaii.com`.

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "<api url>/v1/otlp/claude-code/v1/logs",
    "OTEL_LOG_USER_PROMPTS": "0",
    "OTEL_LOG_TOOL_DETAILS": "0"
  },
  "otelHeadersHelper": "tryaii claude-code otel-headers"
}
```

The per-signal `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` takes the full URL. The OTLP base is
`<api url>/v1/otlp/claude-code`; the exporter appends `/v1/logs` only when the base is set
as the generic `OTEL_EXPORTER_OTLP_ENDPOINT`. `connect` uses the per-signal variable and
writes nothing when `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` or
`OTEL_EXPORTER_OTLP_LOGS_HEADERS` is already set (another destination, or headers that
would be sent to TryAii as well). The app key is never written to the settings file:
Claude Code runs `otelHeadersHelper`, which prints
`{"Authorization": "Bearer <app key>", "X-Tryaii-Notice-Version": "<notice version>"}`
(the key `connect` stored on the machine and the SDK's Data Notice version, section 7.1).
Metrics and traces are not received (`connect` sets `OTEL_METRICS_EXPORTER` to `none`).
The content switches `OTEL_LOG_USER_PROMPTS` and `OTEL_LOG_TOOL_DETAILS` are written as
`0`; `OTEL_LOG_RAW_API_BODIES` stays unset (`connect` refuses while it is on).

Client detail: the helper reads the key from `<data_dir>/claude-code/telemetry-key.json`;
when live telemetry is not connected it prints nothing on stdout, an error on stderr and
exits 1, so Claude Code's exports carry no key and get `401`. Claude Code runs the helper
at startup and every 29 minutes, and the settings apply to sessions started after
`connect`.

### 8.2 Authentication and the app

The app key of section 1, validated exactly as for `POST /v1/monitor/events`: the same
`401 {"error":"invalid_key"}` for every key failure (missing, malformed, unknown, revoked,
expired, app deleted, or no `monitor:write` scope), the owner's upload switch
(`403 upload_disabled`, section 7.3) and the per-key upload rate limit of section 6
(60 requests per minute per key, shared with `POST /v1/monitor/events`). The events belong
to the key's app; the SDK tells the user to create and use an app named `claude-code`
(`tryaii apps create claude-code`).

### 8.3 What is kept (allowlist)

**Metadata only.** Per Claude Code API call TryAii stores the model, the token counts,
Claude Code's cost estimate, the duration and the time (v1.6: also the process, the effort
level and a session reference, below); nothing that was typed, read or written in the session.

Only log records whose event name is **`claude_code.api_request`** are used. The name is the
record's `event.name` attribute (documented value `api_request`; `claude_code.api_request` is
accepted too) or, when that attribute is absent, a string body. Every other record is counted
and dropped without reading anything else from it: `claude_code.user_prompt`,
`claude_code.assistant_response`, `claude_code.tool_result`, `claude_code.tool_decision`,
`claude_code.api_error`, `claude_code.api_refusal`, `claude_code.api_request_body`,
`claude_code.api_response_body`, and any other or future event.

From an accepted record the server copies **only** these attributes into a new object:

| Attribute | Used for | Stored as |
|---|---|---|
| `model` | the model of the call | `model_requested`, `model_used` |
| `input_tokens` | input tokens (excludes cache reads and writes) | `usage.prompt_tokens` |
| `output_tokens` | output tokens | `usage.completion_tokens` |
| `cache_read_tokens` | tokens read from the prompt cache | `usage.cached_tokens` |
| `cache_creation_tokens` | tokens written to the prompt cache | `usage.cache_write_tokens` (v1.5; stored in `events.cache_write_tokens` and the daily cache rollups), and in `total_tokens` |
| `cost_usd` | Claude Code's own cost estimate | `est_cost_usd`, `est_cost_anchor_usd` |
| `duration_ms` | request duration | `latency_ms` |
| `request_id` | the API request id (`req_...`, at most 256 printable characters) | only through `event_id` (8.5); the id itself is not stored |
| `event.timestamp` | when the call happened (else the record's `timeUnixNano`, else its `observedTimeUnixNano`) | `ts` (milliseconds, UTC) |
| `query_source` | main thread or subagent | only through `process` (8.4); the value is not stored |
| `agent.name` | the subagent type (Claude Code reports user-defined types as `custom` unless tool details are enabled; more than 64 or non-printable characters -> `subagent`) | `process` |
| `effort` | effort level of the request (v1.6) | `effort` (one of low/medium/high/xhigh/max, else null) |
| `session.id` | grouping calls by Claude Code session (cache timer, v1.6) | only as `session_ref` = first 64 bits of HMAC-SHA256(per-app key derived from a server secret, session.id); the raw value is never stored, queued or logged; non-UUID values -> null |

**Everything else is dropped on arrival**, before storage, queueing or logging: prompt text,
the assistant's response, tool names, parameters and results, MCP / skill / plugin names,
`speed`, `client_request_id`, `event.sequence`, `prompt.id`,
`workspace.host_paths`. Claude Code also puts its standard attributes on every record as
record attributes (`user.email`, `user.account_uuid`, `user.account_id`, `user.id`,
`organization.id`, `ccr.session.id`, `app.version`, `terminal.type`): the
allowlist does not copy them, so they are dropped like any other record attribute.
Resource attributes (`service.*`, `host.*`, `vcs.*`, any `OTEL_RESOURCE_ATTRIBUTES` key,
and the same identity keys when sent there) are never read. The allowlist is implemented
as named-key copies, not as a denylist, so a field Claude Code adds later is dropped by
default. Server logs carry counts only (records, accepted, dropped per reason), never a
value.

**Session reference (v1.6).** `session.id` is read by name and only when it is a canonical
UUID (36 characters, 8-4-4-4-12 hex). The receiver computes, before anything is queued,
`K_app = HKDF-SHA256(server secret, info = "session-ref:v1:" + app_id)` and
`session_ref = HMAC-SHA256(K_app, "claude-code-session:v1:" + lower-case session.id)`, keeping
the first 16 hex characters (64 bits). The secret is held only by TryAii's server, and the key
differs per app, so a reference cannot be turned back into the session id or matched with
another app or service. The raw session id is never stored, queued, logged or echoed in an
error. `ccr.session.id` is never read. A user leaves sessions out by setting
`OTEL_METRICS_INCLUDE_SESSION_ID=false` in Claude Code (`tryaii claude-code connect
--no-session`); the cache timer then has no per-session view. Session references are used only
for the cache timer and cold-restart cost (section 3.4) and kept 7 days (section 7.4).
`effort` and `session_ref` are read only while the server's v1.6 switch is on; until then
both are null and the attributes are dropped like every other.

### 8.4 Mapping to `tryaii.monitor.event/1`

```
app                  the key's app            provider       "anthropic"
model_requested      model                    model_used     model
model_resolved       null                     mode           "off"
route_status         "skipped"                winner         null (winner_reachable, est_cost_winner_usd null)
process              agent.name if present; else query_source "repl_main_thread*", "compact*", "sdk*" -> "main",
                     any other query_source -> "subagent"; neither present -> "claude-code"
call_site            null                     classification null
prompt_hash          null                     vector         false
usage.prompt_tokens  input_tokens             usage.completion_tokens  output_tokens
usage.cached_tokens  cache_read_tokens        usage.cache_write_tokens cache_creation_tokens
usage.total_tokens   input + output + cache read + cache write (of those present)
est_cost_usd         cost_usd                 est_cost_anchor_usd      cost_usd (no swap)
latency_ms           duration_ms              stream, error, taxonomy_version  null
effort               effort (enum, v1.6)      session_ref    from session.id (v1.6, section 8.3)
cache_cold           set later by the worker (main-thread calls of a session, section 3.4)
```

Numbers may arrive as `intValue`, `doubleValue` or numeric `stringValue`; a token count must
be a non-negative whole number, a cost or duration a non-negative finite number. The stored
batch's `notice_version` is the `X-Tryaii-Notice-Version` header when it is a `YYYY-MM-DD`
date, else the server's current notice version (section 7.1). As for SDK uploads, a batch is
never consent.

Compared with an imported call (`SPEC.md` §10.5), a live event has `call_site` and
`model_resolved` null, `latency_ms` set, `stream` null, and Claude Code's `cost_usd`
instead of the SDK's own price.

### 8.5 Event id and deduplication

`event_id = uuid5(NAMESPACE_URL, "tryaii:claude-code:" + request_id)` (RFC 4122 URL
namespace `6ba7b811-9dad-11d1-80b4-00c04fd430c8`). The SDK's log import uses the same rule
for a call whose log line carries `requestId` (`SPEC.md` §10.5). The server stores events
under `(event_id, ts)` with `ON CONFLICT DO NOTHING`, so two copies of a call are one event
only when they also carry the same millisecond `ts`.

- **Live path.** Records repeated within one export are kept once; an OTLP retry of the
  same export gets the same batch id and is not queued twice; a record sent again later has
  the same `event_id` and `ts` and adds nothing.
- **Live and import.** A live event's `ts` (Claude Code's `event.timestamp`) differs from
  the log line's time by milliseconds, so the server would keep both copies of the same
  call. The SDK prevents the overlap with a cutoff: `connect` records `live_since` (UTC,
  the moment it turned live telemetry on), and while it is recorded the log import stores
  and uploads only calls whose time is **before** it; live telemetry covers everything from
  then on. `disconnect` clears it (`SPEC.md` §10.8).

### 8.6 Responses and errors

```
200 {"partialSuccess": {}}                                           no record rejected
200 {"partialSuccess": {"rejectedLogRecords": n, "errorMessage": "n malformed, m outside the 90-day window"}}
400 {"error":"bad_request", "detail"}      body is not JSON / not an object / not valid gzip /
                                           gzip that inflates past 4 MiB
401 {"error":"invalid_key"}
403 {"error":"upload_disabled"}
413 {"error":"too_large"}                  body over 4 MiB as sent
415 {"error":"unsupported_media_type", "detail"}   a Content-Type other than application/json
                                           (protobuf, none), or a Content-Encoding other than gzip / identity
429 {"error":"rate_limited", "retry_after"} (with Retry-After)
503 {"error":"monitor_unavailable"} | {"error":"queue_unavailable"}   retryable
```

- Checks run in this order: `503 monitor_unavailable`, `401`, `415` (Content-Type), `403`,
  `429`, `413`, `415` (Content-Encoding) or `400` (gzip), `400` (JSON), then
  `503 queue_unavailable`.
- A record that is not allowlisted is dropped and **not** counted as rejected (it is
  filtered by design, not an error); nor is a record repeated within the export.
- A malformed `api_request` record (no `request_id` or `model`, a `request_id` over 256 or
  with non-printable characters, a negative / non-integer token count, a negative or
  non-finite cost or duration, no usable timestamp) is skipped and counted as `malformed`
  in `rejectedLogRecords`; it never fails the request.
- A record outside `[now − 90 days (UTC day), now + 1 day]` is skipped and counted (it
  would land in a dropped partition, section 7.4). `errorMessage` names only the reasons
  that occurred.

## 9. Claude Code annotations (v1.6)

`tryaii claude-code sync` (SDK `SPEC.md` §10.9) enriches the calls TryAii already received
live (section 8) or by the Claude Code import (`tryaii claude-code connect`, section 3.2) with
what only the developer's machine knows, and sends the per-process advice of
`tryaii claude-code analyze`. Live and imported calls never overlap (section 8.5),
and a live event's `ts` differs from the log line's by milliseconds, so this cannot go
through `POST /v1/monitor/events`.

```
POST /v1/monitor/claude-code/annotations
Authorization: Bearer tryaii_<key_id>_<secret>
{
  "schema": "tryaii.claude-code.annotations/1",
  "sdk": {"lang": "python"|"node", "version": "0.9.0"},
  "notice_version": "YYYY-MM-DD",
  "calls": [{                                       // 0..500
    "event_id": "<uuid>",                           // uuid5(NAMESPACE_URL, "tryaii:claude-code:" + request id)
    "ts": "...Z",                                   // the log line's time
    "project": "<working directory base name>"|null,
    "process": "<subagent type>"|null,              // the real agentType of the log
    "classification": {"category", "subcategory"|null, "confidence": 0..1|null}|null,
    "winner": "<catalog id>"|null, "est_cost_winner_usd": number|null, "taxonomy_version": "..."|null
  }],
  "advice": [ ProcessAdvice ]                       // 0..50 (section 3.4)
}
-> 200 {"matched": n, "unmatched": ["<event_id>", ...], "advice_stored": k}
-> 400 {"error":"bad_batch", "detail"}   401 {"error":"invalid_key"}   403 {"error":"upload_disabled"}
-> 413 {"error":"too_large"}   429 {"error":"rate_limited", "retry_after"}   503 {"error":"monitor_unavailable"}
```

- **Gate**: the app key of section 1 with `monitor:write`, the owner's upload switch
  (section 7.3), the per-key upload rate limit shared with `POST /v1/monitor/events` (60 per
  minute), a 1 MiB body, and the same forbidden-field scan (`prompt`, `messages`, `response`,
  `content`, `text`, `raw` at any depth reject the whole request). Unknown fields are dropped;
  advice documents carry structured fields only, never a sentence or task text.
- **Synchronous**, one database transaction: each call is matched with
  `SELECT … FOR UPDATE` to the key's app's row with that `event_id` and a `ts` within
  **±10 minutes**, and only to a Claude Code row: a live row (the OTLP receiver's, batch
  `sdk.lang` = `claude-code`) or an imported row (a batch stored with source `import`, or a
  row with the importer's shape of section 2 — `provider` `"anthropic"`, `mode` `"off"`,
  `latency_ms`, `call_line` and `call_function` null — which also covers imports stored
  before the importer sent `sdk.source`). A wrapped SDK call is never matched. A call with no such row is listed in `unmatched` (the SDK retries it for
  24 h); a repeated `event_id` counts once.
- **Update** of a matched row: `call_file` = `project` (when given; on an imported row only
  when its `call_file` is null); `process` = the annotation's `process` only when the live
  value is `custom` or `subagent` (an imported row keeps its own process); with a
  `classification`: `class`, `subclass`, `confidence`, `winner`, `winner_reachable`
  (true when a winner is named), `est_cost_winner_usd`, `route_status` `"ok"` and
  `taxonomy_version`; without one those columns stay as they are. `rollups_daily` is moved in
  the same transaction (the row's old contribution subtracted, the new one added; emptied
  rollup rows are deleted), so totals equal those of the same events ingested already
  annotated. `stats_model_daily` rows already built are not rebuilt. Re-sending the same
  request changes nothing (idempotent).
- **Validation**: `project` at most 256 printable characters; a value containing `/`, `\` or
  `:` (a path) or otherwise invalid is stored as null, never rejected (a non-string is a 400).
  `process` follows the `agent.name` rule (section 8.3: 1–64 printable characters, else
  ignored). Every `confidence` and `share` is a number in 0..1; money and counts are ≥ 0;
  `window_days` 1..90; `thread` `main`|`subagent`; advice `confidence` `low`|`medium`|`high`.
- **Advice**: kept per (app, process) in `process_advice`; a document replaces the stored one
  only when its `generated_at` is not older; documents older than 90 days are deleted and the
  purge removes them with the app (section 7.4). Served as summary `processes[].advice`.
