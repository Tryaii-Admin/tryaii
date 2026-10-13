# `tryaii apps` — apps and app keys for monitor upload

An **app** groups the monitor records of one program on your tryaii.com
dashboard. Each app has up to two active **app keys**
(`tryaii_<key_id>_<secret>`). Put the key into `TRYAII_API_KEY` where the
program runs and the [monitor wrapper](monitor.md) starts, in the
background:

- uploading its call metadata (the event lines; never prompt or response
  text, never embeddings — see [What is uploaded](../sdk/monitor.md#what-is-uploaded));
- following the modes you set per sub-process on the dashboard
  (`off`, `stable`, `dynamic`, `managed`).

```bash
tryaii login                                   # once: your tryaii.com account
tryaii apps create shop                        # prints the app id and the full key ONCE
export TRYAII_API_KEY=tryaii_...               # where the program runs
tryaii apps check                              # validate the key
```

`create`, `list` and `keys` act as your signed-in account (the credentials
of [`tryaii login`](login.md); they talk to the server you signed in to).
`check` needs only the app key and talks to `TRYAII_API_URL` (default
`https://api.tryaii.com`).

## Verbs

### `create <name>`

Creates the app (names are 1–64 characters, unique per account) and its
first key, labelled `default`:

```
$ tryaii apps create shop
created app app_newapp00001 (shop)
key: tryaii_k1a2b3c4d5e6_...
warning: this is the only time the full key is shown; store it now (e.g. as TRYAII_API_KEY)
```

The warning goes to stderr. The full key is never shown again — lists show
the prefix only. `--json` prints `{"app": ..., "key": ...}`. A name you
already use fails with `error: name_taken: you already have an app named
'<name>'`.

### `list`

```
$ tryaii apps list
app id            name            keys  calls 24h  cost 24h  last event
app_abcdefghijkl  shop               2       1234    1.2346  2026-10-12T08:59:00.000Z
app_mnopqrstuvwx  internal tools     1          0    0.0000  -
```

Active keys, calls and estimated cost (USD) of the last 24 hours, and the
time of the last uploaded event. `--json` prints the server's app objects.

### `keys <app_id>`

```
$ tryaii apps keys app_abcdefghijkl
key                      label    created                   last used                 status
tryaii_k1a2b3c4d5e6_...  default  2026-10-12T08:00:00.000Z  2026-10-12T08:59:00.000Z  active
tryaii_q9w8e7r6t5y4_...  -        2026-10-11T08:00:00.000Z  -                         revoked
```

- `--new [--label <label>]` creates a key and prints it once
  (`new key <key_id> for app <app_id>` + `key: ...` + the warning). An app
  has at most **2 active keys**; a third fails with `error: key_limit`.
- `--revoke <key_id>` revokes a key (`revoked key <key_id> (app <app_id>)`).

Rotation: `--new`, deploy the new key, then `--revoke` the old one.

### `check [--key <key>]`

```
$ tryaii apps check
app: shop (app_abcdefghijkl)
key: k1a2b3c4d5e6
scopes: monitor:write
expires: never
```

Validates `TRYAII_API_KEY` (or `--key`). A rejected key prints `error:
invalid key` (exit 1); a value that is not an app key at all is refused
before any request (exit 2).

## Command reference

```
tryaii apps -- tryaii.com apps and app keys for monitor upload

Usage:
  tryaii apps <verb> [options]

An app groups the monitor records of one program on your tryaii.com
dashboard. Its app key (tryaii_<key_id>_<secret>) goes into TRYAII_API_KEY
where the program runs: the monitor wrapper then uploads its call metadata
(never prompt or response text) and follows the modes set on the dashboard.

Verbs:
  create <name>         Create an app and its first key (the full key is shown once)
  list                  Your apps with keys, calls and cost of the last 24 hours
  keys <app_id>         List an app's keys (prefix only); --new adds one, --revoke removes one
  check                 Validate TRYAII_API_KEY (or --key) and show its app and scopes

Options:
  --new                 keys: create a key (at most 2 active keys per app)
  --label <label>       keys --new: a label for the new key
  --revoke <key_id>     keys: revoke a key
  --key <key>           check: the key to validate instead of TRYAII_API_KEY
  --json                Machine-readable output

create, list and keys act as your account (run 'tryaii login' first);
check only needs the app key.

Examples:
  tryaii apps create shop
  tryaii apps keys app_x1y2z3a4b5c6 --new --label ci
  TRYAII_API_KEY=tryaii_... tryaii apps check

Exit codes:
  0 success, 1 server, network or sign-in failure, 2 usage error.

Docs: docs/cli/apps.md
```

`tryaii apps <verb> --help` prints the verb's page. Both CLIs (npm and PyPI)
print byte-identical output; the behaviour is frozen by
`shared/monitor/fixtures/apps/` (`shared/monitor/SPEC.md` §7).

## Errors and exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Server, network or sign-in failure: `error: not signed in (run tryaii login)`, `error: session expired or revoked (run tryaii login)`, `error: could not reach <api url>`, `error: invalid key`, `error: <error code>[: <detail>]` |
| 2 | Usage: unknown verb or option, missing or extra argument, `--new` with `--revoke`, `--label` without `--new`, no or malformed key for `check` |
