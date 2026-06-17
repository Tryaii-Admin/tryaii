# `tryaii help` — global or per-command help

Print the global overview, or detailed help for a single command. Help is local and instant — nothing is downloaded or routed.

```bash
tryaii help            # global overview (same as bare `tryaii` / `tryaii --help`)
tryaii help eval       # detailed help for the eval command
tryaii eval --help     # identical — the flag form of the same page
tryaii route -h        # short flag works too
```

## The four help paths

Following the [git-style convention](https://clig.dev/), all of these work:

| Invocation | Prints |
|---|---|
| `tryaii` (bare) | Global overview |
| `tryaii help` / `tryaii --help` / `tryaii -h` | Global overview |
| `tryaii help <command>` | That command's detailed help |
| `tryaii <command> --help` / `tryaii <command> -h` | That command's detailed help |

The `-h`/`--help` flag is honored anywhere in argv (it's stripped before parsing, like `--no-banner`), so it works before or after the command.

## Topics

`route`, `eval`, `models`, `benchmarks`, `setup`, `regenerate`, `help`.

`tryaii help <topic>` resolves to the matching page above. `tryaii help help` describes this command. A flag-only argument (`tryaii help --help`) has no topic token, so it falls back to the global overview rather than erroring.

## Exit codes

- `0` — help printed (global or per-command).
- `2` — unknown help topic, e.g. `tryaii help bogus` → `error: unknown help topic: bogus. Run "tryaii help" for the list of commands.` on stderr.

## Parity

The global help and every per-command page are kept **byte-identical** between the npm and pip CLIs, guarded by `tests/test_parity.py` (`test_cli_help_text_identical_across_sdks` and `test_command_help_text_identical_across_sdks`).
