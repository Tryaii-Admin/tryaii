# `tryaii login` — sign in and unlock the full catalog

Sign in with your tryaii.com account (Google sign-in, free). Signing in unlocks the **full model catalog** (322 routable models); without it, every command routes on the 45-model **starter catalog** that ships in the package.

```bash
tryaii login
```

No flags besides `-h`/`--help`.

## How it works

`login` is a device sign-in: it prints a URL and a short code. Open the URL in any browser — it does not have to be on this machine, so it works over SSH and inside containers — sign in with Google and approve the code. The CLI waits for the approval:

```
To sign in, open this URL in a browser:
  https://tryaii.com/device
and enter the code: BCDF-GHJK

Or open directly: https://tryaii.com/device?user_code=BCDF-GHJK

Waiting for approval (expires in 10 minutes, Ctrl+C to cancel)...
Logged in as you@example.com.
Downloaded the full catalog (322 models).
```

Only approve a code you started from your own terminal. If you were already logged in, the first line is `Already logged in as <email>. Signing in again replaces that session.` and the old session is revoked after the new one succeeds.

## The full catalog

Right after signing in, `login` downloads the full catalog. If the download fails, it prints `Could not download the full catalog now; it will be fetched on next use.` and still exits 0.

From then on the catalog is kept up to date automatically: `route`, `eval`, `models`, `benchmarks`, `setup`, `regenerate` and the SDK's `Router` check for a new release at most once a day and otherwise use the local copy. Every download is verified before use — each file against the manifest's sha256, and the manifest against an Ed25519 signature from a key built into the package. A catalog that fails verification is never used; the CLI keeps the previous full catalog, or falls back to the starter catalog with a one-line notice on stderr.

## Files

- Credentials: `~/.tryaii/credentials.json` (or under `TRYAII_DRE_DATA_DIR`), written atomically, mode 0600 on POSIX. It holds a refresh token — treat it like a password.
- Full catalog cache: `~/.tryaii/catalog/` (same data dir).

[`logout`](logout.md) removes both.

## Environment

| Variable | Effect |
|---|---|
| `TRYAII_API_URL` | Override the API base URL (default `https://api.tryaii.com`). Applies to `login` only; a stored session always talks to the server that issued it. |
| `TRYAII_DRE_DATA_DIR` | Where credentials and the catalog cache are stored (default `~/.tryaii`). |

## Errors

All on stderr, exit 1 unless noted:

| Situation | Message |
|---|---|
| You pressed Deny in the browser | `Login denied in the browser.` |
| The code expired (10 minutes) | `The code expired. Run tryaii login again.` |
| No connection | `Could not reach <api_url>. Check your connection and try again.` |
| Too many attempts | `Too many sign-in attempts. Wait a minute and try again.` |
| Any other server error | `Login failed: <error>.` |
| Ctrl+C | `Login cancelled.` (exit 130) |

## Exit codes

`0` success, `1` denied, expired or network failure, `2` bad flag, `130` cancelled.

The wire protocol (endpoints, token formats, error codes) is specified in [the auth contract](../auth/CONTRACT-auth-v1.md); catalog download and verification in [the catalog contract](../catalog/CONTRACT-catalog-v1.md).
