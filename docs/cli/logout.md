# `tryaii logout` — sign out

Sign out and remove the stored credentials.

```bash
tryaii logout
```

No flags besides `-h`/`--help`.

## Behavior

- Revokes the session on the server (best effort — a network failure does not stop the logout).
- Deletes the local credentials file (`~/.tryaii/credentials.json`, or under `TRYAII_DRE_DATA_DIR`) and the downloaded full catalog.
- Prints `Logged out.`

After `logout`, every command routes on the starter catalog again.

| Situation | Output | Exit |
|---|---|---|
| Logged in | `Logged out.` | 0 |
| Not logged in | `Not logged in.` | 0 |
| The credentials file cannot be deleted | `Could not remove the credentials file.` (stderr) | 1 |

## Exit codes

`0` success or already signed out, `1` could not remove the credentials file, `2` bad flag.
