# monitor upload and dashboard API — contract v1.1 (2026-10-12)

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
DELETE /v1/apps/{app_id}                                   -> 204 (soft delete; keys revoked)
POST   /v1/apps/{app_id}/keys        {label?}             -> 201 {key_id, key, label, created_at}
GET    /v1/apps/{app_id}/keys                              -> 200 {keys:[KeySummary]}
DELETE /v1/apps/{app_id}/keys/{key_id}                     -> 204 (revoke)
```

```
App          = {app_id, name, owner_user_id, created_at, deleted_at|null}
AppWithStats = App + {calls_24h, cost_24h_usd, savings_24h_usd, last_event_at|null,
                      processes: int, keys_active: int}
KeySummary   = {key_id, label, prefix: "tryaii_<key_id>_", created_at,
                last_used_at|null, revoked_at|null, expires_at|null}
```

`app_id` is `app_` + 12 chars `[a-z0-9]`. `name` is 1–64 chars; unique per owner
(409 `{"error":"name_taken"}`). An app is only visible to its owner (org support
comes later; the schema already carries `owner_user_id` and a nullable `org_id`).

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
  "notice_version": "2026-10",       // consent text version the user saw
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
             cost_at_winner_usd, savings_usd, savings_available_usd},
  "processes": [{
     "process", "mode", "managed_model"|null, "pin": {model, expires_at}|null,
     "calls", "errors", "tokens", "cost_usd", "cost_at_winner_usd", "savings_usd",
     "top_winner": "<model id>"|null, "top_winner_share": 0..1,
     "anchor": "<model id>"|null, "traffic_light": "green"|"yellow"|"red"|"grey",
     "models": [{model, calls, share, cost_usd, wins, win_share, reachable}],
     "files": [{file, function|null, calls}],
     "classes": [{class, share}]          // from classification.category, until task profiles land
  }],
  "daily": [{date, calls, cost_usd, cost_at_winner_usd}]
}
GET /v1/monitor/apps/{app_id}/events?limit=100&before=<ts>&process=<name>
-> 200 {"events":[Event...], "next_before": "<ts>"|null}
GET /v1/monitor/apps/{app_id}/config               -> 200 RemoteConfig
PUT /v1/monitor/apps/{app_id}/config
    {"default_mode"?: ..., "processes": {"<name>": {"mode": ..., "model"?: ...}}}
-> 200 RemoteConfig   (merges per process; a process set to null is removed; writes an audit row;
                       bumps etag; wakes long-polls)
-> 400 {"error":"bad_config","detail":"..."}  (managed without model, unknown mode)
GET /v1/monitor/apps/{app_id}/audit?limit=50    -> 200 {"entries":[{at, user_id, change}]}
GET /v1/monitor/models?app_id=...              -> 200 {"models":[{id, provider, quality, blended_price_usd_per_m, reachable}]}
```

Traffic light rule (server-side, per process, over `since`): `grey` = fewer
than 5 routed events; `green` = the anchor (or the pin / managed model when
set) wins ≥ 70 % of routed events; `yellow` = another model wins 30–70 %;
`red` = the anchor wins < 30 % or `savings_available_usd / cost_usd ≥ 0.4`.
`savings_available_usd = Σ max(0, est_cost_usd − est_cost_winner_usd)` over
events whose winner is reachable and that were **not** swapped
(`model_used == model_requested`); `savings_usd` (realised) =
`Σ max(0, est_cost_anchor_usd − est_cost_usd)` over swapped events
(`model_used != model_requested`), 0 for events without `est_cost_anchor_usd`.

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
       error_class, prompt_hash, vector bool, taxonomy_version, batch_id)
       PARTITION BY RANGE (ts), one partition per day, dropped after 90 days
rollups_daily(app_id, day, process, model_used, winner, calls, errors, prompt_tokens,
              completion_tokens, cost_usd, cost_at_winner_usd, cost_at_anchor_usd, savings_usd,
              savings_available_usd, wins, pk(app_id, day, process, model_used, winner))
              -- model_used / winner store '' when absent (pk columns cannot be null);
              -- events pk is (event_id, ts) because the table is partitioned by ts
consent(user_id, notice_version, seen_at, pk(user_id, notice_version))
```

Ingest: `POST /v1/monitor/events` validates and enqueues the batch on SQS
(standard queue, DLQ after 3 receives); the worker inserts events (`ON CONFLICT
DO NOTHING` on `event_id`), records the batch, upserts `rollups_daily`, and
updates `consent`. The dashboard reads rollups for totals and the events table
only for the per-process detail windows.

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
