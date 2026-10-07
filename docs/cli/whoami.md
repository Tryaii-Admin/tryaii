# `tryaii whoami` — show the signed-in account

Show the account you are signed in with, and whether the full catalog is downloaded.

```bash
tryaii whoami
tryaii whoami --json
```

## Flags

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--json` | boolean | off | Print the account exactly as the server returns it (`user`, `entitlements`, `session`), pretty-printed with 2-space indent |

## Text output

```
Logged in as you@example.com (Your Name)
Entitlements: catalog:full
Catalog: full, release 2026.10.07.1 (322 models)
```

The third line reads `Catalog: not downloaded yet` until the full catalog has been downloaded (see [`login`](login.md)).

`whoami` refreshes the access token when needed, so it also tells you whether the session is still valid.

## Errors

All on stderr, exit 1:

| Situation | Message |
|---|---|
| Not logged in | `Not logged in. Run: tryaii login` |
| The session was revoked or expired (the local credentials are deleted) | `Your session has ended. Run: tryaii login` |
| No connection | `Could not reach <api_url>. Check your connection and try again.` |
| Any other server answer (credentials kept) | `Unexpected response from <api_url>: <error>.` |

## Exit codes

`0` signed in, `1` not signed in, session ended or network failure, `2` bad flag.
