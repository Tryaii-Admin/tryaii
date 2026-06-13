# `tryaii` CLI

Both packages install the same `tryaii` command (npm: `dist/cli.js` bin; pip: `tryaii.cli.main:cli` entry point). Commands, flags, output formats, and exit codes are kept in parity between the two implementations.

```
tryaii <command> [options]
```

| Command | Purpose |
|---|---|
| [`route <prompt>`](route.md) | Route one prompt to the best model and show ranked recommendations |
| [`eval <input.json>`](eval/README.md) | Route a JSON prompt dataset; writes `results.jsonl`, `summary.json`, `index.html` — see [dataset](eval/dataset/README.md), [priority mode](eval/priority-mode/README.md), [budget mode](eval/budget-mode/README.md), [outputs](eval/outputs/README.md) |
| [`models`](models.md) | List the model catalog (`--provider`, `--json`) |
| [`benchmarks`](benchmarks.md) | List registered benchmarks (`--json`) |
| [`setup`](setup.md) | Download the embedding model and warm centroids (one-time) |
| [`regenerate`](regenerate.md) | Force-rebuild benchmark centroids (e.g. after changing the embedding model) |

Running bare `tryaii`, `tryaii help`, or `tryaii -h/--help` prints the global help. There is no per-command help — `tryaii eval --help` prints the same global text.

## Global flags

| Flag | Node | Python | Effect |
|---|---|---|---|
| `--no-banner` | ✓ | ✓ | Suppress the startup banner. Accepted anywhere in argv (stripped before parsing). Also honored via `TRYAII_NO_BANNER`. |
| `-v`, `--verbose` | ✓ | ✓ | Python: enables DEBUG logging. Node: sets `TRYAII_VERBOSE=1` for downstream code (the Node SDK has no logging today). |
| `-V`, `--version` | ✓ | ✗ | Print the package version and exit (Node only — the Python parser does not define it, despite its docstring). |
| `-h`, `--help` | ✓ | ✓ | Print global help. |

Note `-v` is **verbose**, not version. Because `--no-banner`/`--verbose`/`-v` are stripped from argv before parsing (Node strips all three; Python strips `--no-banner`), a positional argument literally equal to one of those strings is silently swallowed.

## Banner

A gradient "TRYAII" wordmark is printed to **stderr** (stdout stays clean for piping). It self-suppresses when stderr is not a TTY, and prints monochrome under `NO_COLOR` or `TERM=dumb`. 24-bit gradient requires `COLORTERM=truecolor|24bit`. A banner failure never breaks the command.

## Environment variables

| Variable | Effect |
|---|---|
| `TRYAII_NO_BANNER` | Any value disables the banner |
| `NO_COLOR`, `TERM`, `COLORTERM` | Banner color/animation detection |
| `TRYAII_DRE_EMBEDDING_MODEL` | (Python only) default embedding model when `--model` is not given |
| `TRYAII_DRE_DATA_DIR` | (Python only) data dir for centroids/caches; default `~/.tryaii` |
| `TRYAII_VERBOSE` | (Node) set to `1` by `--verbose`; not read by the SDK itself |
| `OPENROUTER_API_KEY` | Read by the SDK clients, **not** by any CLI command (the CLI never calls model APIs) |

The Python package also loads a `.env` file from the working directory on import (`python-dotenv`).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success — including `eval` runs with *partial* per-prompt failures |
| 1 | Runtime failure (bad input file, routing error); also `eval` when **all** prompts failed |
| 2 | Usage error: unknown command/option, missing argument, invalid value |

Errors are written to stderr as `error: <message>` (Node, no stack trace) or an argparse message/traceback (Python).
