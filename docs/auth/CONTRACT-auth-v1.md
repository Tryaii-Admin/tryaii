# tryaii CLI login — contract v1.1

This is the client-side specification of `tryaii login` / `logout` / `whoami`:
the device sign-in flow, the tokens a client sees, the HTTP API as seen by a
client, and the exact CLI behaviour. Both SDKs (Python and Node) implement it and
must agree byte-for-byte where it says so. The full catalog that a login unlocks
is specified separately in [the catalog contract](../catalog/CONTRACT-catalog-v1.md).

Section 7 holds the detailed rules; where it and earlier text disagree, section 7 wins.

## 1. Flow (RFC 8628 device authorization grant)

1. CLI `POST /v1/auth/device/code` -> gets `device_code`, `user_code`, URLs.
2. CLI prints the URL and code, then polls `POST /v1/auth/token` every `interval` s.
3. User opens `https://tryaii.com/device?user_code=XXXX-XXXX` in any browser,
   signs in with Google, sees the code + client info, presses Approve or Deny
   (section 5).
4. Next poll returns tokens. CLI stores them in the credentials file.
5. Later calls: CLI refreshes with the refresh token when the access token is
   expired (or within 60 s of expiry) and calls `GET /v1/auth/me`.

## 2. Sessions and refresh tokens

- One successful login creates one **session**. Its id is returned by `/me` as
  `session.id`.
- The refresh token **rotates**: every successful refresh grant returns a new
  `refresh_token` (and a new `refresh_expires_in`), which the client must store,
  replacing the old one. Refresh-token lifetime is 90 days, sliding: each
  rotation starts a new 90-day window.
- **Reuse detection:** presenting a refresh token that was already exchanged
  (outside a short grace window) revokes the whole session and returns
  `invalid_grant`.
- `POST /v1/auth/revoke` with any refresh token of a session revokes the whole
  session.
- An account that is deactivated, or has no CLI access, behaves as revoked
  (`invalid_grant` on refresh, 401 on `/me`).
- **Entitlements** are a list of strings. Version 1 defines one:
  `catalog:full` (access to the full model catalog). An account without CLI
  access has `[]`.

## 3. Token formats
- `device_code`: 32 random bytes, base64url without padding (43 chars).
- `user_code`: 8 chars from `BCDFGHJKLMNPQRSTVWXZ`, displayed `XXXX-XXXX`. Input
  normalization everywhere: uppercase, drop every char not in the alphabet.
- `refresh_token`: `tair_` + 32 random bytes base64url (no padding).
- `access_token`: a signed JWT (`alg: EdDSA`, audience `tryaii-cli`), valid one
  hour. Clients treat it as opaque; only `expires_in` is used for refresh timing.
- Never log any token.

## 4. HTTP API (base `TRYAII_API_URL`, default `https://api.tryaii.com`)

All bodies JSON (`Content-Type: application/json`). Errors follow RFC 6749
shape `{"error": "<code>", "error_description": "<text>"}`.

### `POST /v1/auth/device/code`
Request: `{"client_id": "tryaii-cli", "client": {"sdk": "python", "version": "0.6.0", "os": "win32"}}`
200:
```json
{"device_code": "...", "user_code": "BCDF-GHJK",
 "verification_uri": "https://tryaii.com/device",
 "verification_uri_complete": "https://tryaii.com/device?user_code=BCDF-GHJK",
 "expires_in": 600, "interval": 5}
```
400 `invalid_client` if client_id != `tryaii-cli`; 400 `invalid_request` on bad body.
429 `slow_down` when the client calls too often.

### `POST /v1/auth/token`
Device grant: `{"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "...", "client_id": "tryaii-cli"}`
Refresh grant: `{"grant_type": "refresh_token", "refresh_token": "tair_...", "client_id": "tryaii-cli"}`

200 (both grants):
```json
{"access_token": "eyJ...", "token_type": "Bearer", "expires_in": 3600,
 "refresh_token": "tair_...", "refresh_expires_in": 7776000,
 "user": {"id": "...", "email": "a@b.com", "name": "A B"},
 "entitlements": ["catalog:full"]}
```
400 errors (device grant): `authorization_pending`, `slow_down` (polled faster
than `interval`; the server adds 5 s to the grant's interval), `access_denied`,
`expired_token`, `invalid_grant` (unknown/consumed code).
400 errors (refresh grant): `invalid_grant` (unknown, expired, revoked, reused,
or user inactive).
400 `unsupported_grant_type`, `invalid_client`, `invalid_request` as usual.
An approved grant is consumed by the first poll that receives the tokens;
a second poll gets `invalid_grant`.

### `POST /v1/auth/revoke`
Request: `{"refresh_token": "tair_...", "client_id": "tryaii-cli"}` -> always 200 `{}`
(RFC 7009). Revokes the whole session.

### `GET /v1/auth/me`
Header `Authorization: Bearer <access_token>`.
200: `{"user": {"id","email","name"}, "entitlements": [...], "session": {"id": "<session id>", "created_at": "<ISO8601 Z>"}}`
401 `{"error": "invalid_token"}` for missing/bad/expired token, revoked session or inactive user.

### `GET /healthz` -> 200 `{"ok": true}`

## 5. Approval page (`https://tryaii.com/device`)

What the user sees after opening the URL the CLI printed:
- Not signed in -> Google sign-in, then back to the same page with the code.
- Signed in: a code input (prefilled from `?user_code=`), then a confirm view
  showing "Signing in as <email>", the code, the `client.sdk` / `version` / `os`
  the CLI sent (labelled "reported by the device") and how long ago the sign-in
  was requested, with **Approve** and **Deny** buttons and the warning:
  "Only approve if you started this sign-in from your own terminal."
- Approve -> the CLI's next poll receives the tokens; Deny -> `access_denied`.
- Unknown, expired or already-used codes are reported on the page; nothing is
  sent to the CLI.

## 6. SDK / CLI

### Configuration
- `TRYAII_API_URL` overrides the api base **for `login` only** (empty value falls
  through to default; trailing `/` stripped). A stored session ALWAYS talks to the
  `api_url` recorded in its credentials file; `whoami`/`logout` ignore
  `TRYAII_API_URL` (prevents sending a production refresh token to another host).
- Credentials file: `<data dir>/credentials.json`, data dir = `TRYAII_DRE_DATA_DIR`
  or `~/.tryaii`. Written atomically (temp file + rename); mode 0600 on POSIX.

```json
{"version": 1, "api_url": "https://api.tryaii.com",
 "user": {"id": "...", "email": "...", "name": "..."},
 "entitlements": ["catalog:full"],
 "refresh_token": "tair_...", "refresh_expires_at": "2027-01-01T00:00:00Z",
 "access_token": "eyJ...", "access_expires_at": "2026-10-03T13:00:00Z",
 "created_at": "2026-10-03T12:00:00Z"}
```
Timestamps: ISO 8601 UTC with `Z`, second precision.

### Transport
Stdlib only (Python `urllib.request`, Node global `fetch`). `User-Agent: tryaii/<version>`.
10 s timeout per request, no retries (polling is the loop). Injectable seam for
tests (`opener=` / `fetchFn=`). Network failure, timeout, non-JSON body
and unexpected status all map to one error kind `network`.

### Commands (output byte-identical across SDKs; stdout unless noted)

`tryaii login` (no flags besides -h)
```
To sign in, open this URL in a browser:
  <verification_uri>
and enter the code: <user_code>

Or open directly: <verification_uri_complete>

Waiting for approval (expires in 10 minutes, Ctrl+C to cancel)...
Logged in as <email>.
```
(The catalog contract, section 5, adds one line after `Logged in as <email>.`)
Polling honors `interval` and `slow_down` (+5 s). Already logged in -> first
line `Already logged in as <email>. Signing in again replaces that session.`
then the normal flow (old session revoked best-effort after success).
Failure lines (stderr), exit 1:
- `access_denied` -> `Login denied in the browser.`
- `expired_token` -> `The code expired. Run tryaii login again.`
- network -> `Could not reach <api_url>. Check your connection and try again.`
- any other error code -> `Login failed: <error>.`
Ctrl+C -> stderr `Login cancelled.` exit 130.

`tryaii logout`
- logged in via file -> revoke (best-effort, ignore failure), delete file, print `Logged out.` exit 0
- not logged in -> `Not logged in.` exit 0
- file exists but cannot be deleted -> stderr `Could not remove the credentials file.` exit 1

`tryaii whoami [--json]`
- refresh if needed, then `GET /v1/auth/me`.
- success:
```
Logged in as <email> (<name>)
Entitlements: <comma+space joined, or none>
```
  (The catalog contract, section 5, adds a third `Catalog: ...` line.)
  `--json` -> the /me JSON pretty-printed, 2-space indent, keys in server order.
- not logged in -> stderr `Not logged in. Run: tryaii login` exit 1
- refresh rejected (`invalid_grant`) or /me 401 -> delete file (not env), stderr
  `Your session has ended. Run: tryaii login` exit 1
- network -> stderr `Could not reach <api_url>. Check your connection and try again.` exit 1

Exit code 2 for bad flags, as for every other command.

### Help text (byte-identical in both CLIs; no backticks, no `${`)

Global `HELP` gains, after the `regenerate` line in Commands:
```
  login                 Sign in with your tryaii.com account (free; unlocks the full model catalog)
  logout                Sign out and remove the stored credentials
  whoami                Show the signed-in account
```
(The login line is the one the catalog contract, section 5, specifies.)

`HELP_LOGIN`:
```
tryaii login -- Sign in with your tryaii.com account

Usage:
  tryaii login

Starts a device sign-in: prints a URL and a short code. Open the URL in any
browser (it does not have to be on this machine), sign in with Google and
approve the code. Works over SSH and inside containers.

Credentials are stored in ~/.tryaii/credentials.json (or under
TRYAII_DRE_DATA_DIR).

Environment:
  TRYAII_API_URL        Override the API base URL (default https://api.tryaii.com)

Examples:
  tryaii login

Exit codes:
  0 success, 1 denied, expired or network failure, 2 bad flag, 130 cancelled.
```
(The catalog contract, section 5, inserts one paragraph after the credentials
paragraph.)

`HELP_LOGOUT`:
```
tryaii logout -- Sign out and remove the stored credentials

Usage:
  tryaii logout

Revokes the session on the server (best effort) and deletes the local
credentials file.

Examples:
  tryaii logout

Exit codes:
  0 success or already signed out, 1 could not remove the credentials file, 2 bad flag.
```

`HELP_WHOAMI`:
```
tryaii whoami -- Show the signed-in account

Usage:
  tryaii whoami [options]

Options:
  --json                Print the account as pretty-printed JSON

Examples:
  tryaii whoami
  tryaii whoami --json

Exit codes:
  0 signed in, 1 not signed in, session ended or network failure, 2 bad flag.
```

## 7. Detailed rules (v1.1)

Where this section and earlier text disagree, this section wins.

### Server behaviour a client can observe
1. **slow_down tolerance.** A poll is "too fast" when the time since the previous
   poll of the same grant is **less than `interval - 1` seconds** (1 s jitter
   allowance). Polling at `interval - 1` or later is not slow_down.
2. **client_id.** Missing `client_id` -> `invalid_request`. Present but not exactly
   `tryaii-cli` (including empty string) -> `invalid_client`.
3. **Content-Type.** `/v1/auth/device/code`, `/v1/auth/token`: a media type other
   than `application/json` (parameters such as `charset=utf-8` allowed) -> 400
   `invalid_request`. `/v1/auth/revoke` stays "always 200".
4. **Client fields.** `client.version` must match
   `^[0-9]+\.[0-9]+\.[0-9]+([-+.][0-9A-Za-z.+-]{1,32})?$`; `client.os` must match
   `^[a-z0-9]{2,16}$`; each field at most 64 chars. Otherwise `invalid_request`.
   (Stops attacker-chosen text from being shown on the approval page.)
5. **Session-level revocation.** Once any refresh token of a session is revoked
   (logout, reuse detection), the refresh grant fails with `invalid_grant` for
   every token of that session, including one rotated concurrently. No revoked
   session can mint new tokens.
6. **Approvals survive temporary errors.** If the server answers a device-grant
   poll with 503 `{"error": "temporarily_unavailable"}`, the approval is not
   consumed and a later poll can still succeed.
7. **Temporary failures.** Any temporary server-side failure answers 503
   `{"error": "temporarily_unavailable"}` (clients treat it as `network`).
8. **Late polls.** A poll shortly after the code expired still gets
   `expired_token`, not `invalid_grant`.
9. **Rate limits.** Any endpoint may answer 429 when a client calls it too often
   (client handling: item 15).

### Approval page
10. Signing in on the approval page with an account that cannot use the CLI is
    refused on the page; the code stays pending until it expires.
11. Repeatedly entering wrong codes on the approval page is rate-limited.
12. The confirm view shows "Signing in as <session email>" and labels sdk/version/os as
    "reported by the device".

### SDKs
13. `TRYAII_API_URL` applies to `login` only (see section 6). There is no
    token-from-environment variable.
14. `whoami` deletes the credentials file ONLY when the refresh grant returns
    `invalid_grant` or `/me` returns 401. Any other OAuth error code from refresh or
    /me -> stderr `Unexpected response from <api_url>: <error>.` exit 1, file kept.
15. HTTP 429 from any endpoint -> error kind `rate_limited`; `login` prints stderr
    `Too many sign-in attempts. Wait a minute and try again.` exit 1. HTTP 503 and
    every other non-400/401 status stay `network`.
    `whoami` on 429 (refresh or /me) -> stderr
    `Unexpected response from <api_url>: rate_limited.` exit 1, file kept.
16. Redirects are never followed (3xx -> `network`), in both SDKs.
17. Polling: `interval <= 0` -> 5, `expires_in <= 0` -> 600. The client polls once
    more after the local deadline before reporting `expired_token`; an
    `invalid_grant` received after the local deadline is also reported as expired.
18. Credentials write: temp file created exclusively (O_EXCL / `wx`), mode 0600 at
    creation, fsync, then rename. On Windows, retry the rename up to 5 times
    (50 ms apart) on EPERM/EACCES/EBUSY.
19. `logout`: if the file exists but cannot be deleted -> stderr
    `Could not remove the credentials file.` exit 1 (both SDKs). A missing file
    (ENOENT) during delete is not an error.
20. Python writes stdout as UTF-8 for `login`, `logout` and `whoami`.
21. `tryaii help` topic lists include login, logout and whoami (both SDKs).
