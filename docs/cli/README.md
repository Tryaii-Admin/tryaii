# `tryaii` CLI

Both packages install the same `tryaii` command (npm: `dist/cli.js` bin; pip: `tryaii.cli.main:cli` entry point). Commands, flags, output formats, and exit codes are kept in parity between the two implementations.

```
tryaii <command> [options]
```

| Command | Purpose |
|---|---|
| [`route <prompt>`](route.md) | Route one prompt to the best model and show ranked recommendations |
| [`eval <input.json>`](eval/README.md) | Route a JSON prompt dataset; writes `results.jsonl`, `summary.json`, `index.html` — see [dataset](eval/dataset/README.md), [priority mode](eval/priority-mode/README.md), [budget mode](eval/budget-mode/README.md), [outputs](eval/outputs/README.md) |
| [`cachelint <input.json>`](cachelint.md) | Pre-flight prompt-cache analysis: detectors, stable prefix, per-model floors, predicted HIT/MISS across sequences (`--json`, `--provider` raw-text mode) |
| [`diagnose <verb>`](diagnose/README.md) | Agent-first codebase LLM diagnostics: [`init`](diagnose/init.md) installs the agent playbook, [`plan`](diagnose/plan.md) hands the agent the interview + inventory shape, [`check`](diagnose/check.md) runs model-fit/cache/cost/hygiene over an inventory, [`report`](diagnose/report.md) renders a local HTML report with run-over-run deltas |
| [`designpartner`](designpartner.md) | Enroll as a tryaii design partner: one resumable command (questionnaire → diagnose → tiered consent → submission; nothing sent without `--confirm`, always saved locally first) |
| [`models`](models.md) | List the model catalog in use — the 45-model starter catalog, or the full catalog after `login` (`--provider`, `--json`) |
| [`benchmarks`](benchmarks.md) | List registered benchmarks (`--json`) |
| [`setup`](setup.md) | Download the embedding model and warm centroids (one-time) |
| [`regenerate`](regenerate.md) | Force-rebuild benchmark centroids (e.g. after changing the embedding model) |
| [`login`](login.md) | Sign in with your tryaii.com account (free; unlocks the full model catalog) |
| [`logout`](logout.md) | Sign out and remove the stored credentials and the downloaded full catalog |
| [`whoami`](whoami.md) | Show the signed-in account and the catalog in use (`--json`) |
| [`help [command]`](help.md) | Global help, or detailed help for one command |

Running bare `tryaii`, `tryaii help`, or `tryaii -h/--help` prints the global help. Per-command help is available two ways — `tryaii help <command>` or `tryaii <command> -h/--help` — both printing the same detailed page for that command (see [`help`](help.md)). An unknown topic (`tryaii help bogus`) exits 2.

## Global flags

| Flag | Node | Python | Effect |
|---|---|---|---|
| `--no-banner` | ✓ | ✓ | Suppress the startup banner. Accepted anywhere in argv (stripped before parsing). Also honored via `TRYAII_NO_BANNER`. |
| `-v`, `--verbose` | ✓ | ✓ | Python: enables DEBUG logging. Node: sets `TRYAII_VERBOSE=1` for downstream code (the Node SDK has no logging today). |
| `-V`, `--version` | ✓ | ✓ | Print the package version and exit. |
| `-h`, `--help` | ✓ | ✓ | Print global help. |

Note `-v` is **verbose**, not version. Because `--no-banner`/`--verbose`/`-v` are stripped from argv before parsing, a positional argument literally equal to one of those strings is silently swallowed.

## Banner

A gradient "TRYAII" wordmark is printed to **stderr** (stdout stays clean for piping). It self-suppresses when stderr is not a TTY, and prints monochrome under `NO_COLOR` or `TERM=dumb`. 24-bit gradient requires `COLORTERM=truecolor|24bit`. A banner failure never breaks the command.

## Environment variables

| Variable | Effect |
|---|---|
| `TRYAII_NO_BANNER` | Any value disables the banner and the once-a-day login hint |
| `NO_COLOR`, `TERM`, `COLORTERM` | Banner color/animation detection |
| `TRYAII_DRE_EMBEDDING_MODEL` | (Python only) default embedding model when `--model` is not given |
| `TRYAII_DRE_DATA_DIR` | Data dir; default `~/.tryaii`. Both CLIs keep the login credentials and the downloaded full catalog there; Python also keeps its centroid caches there |
| `TRYAII_API_URL` | API base URL for [`login`](login.md) only (default `https://api.tryaii.com`); a stored session always talks to the server that issued it |
| `TRYAII_VERBOSE` | (Node) set to `1` by `--verbose`; not read by the SDK itself |
| `OPENROUTER_API_KEY` | Read by the SDK clients, **not** by any CLI command (the CLI never calls model APIs) |

The Python package also loads a `.env` file from the working directory on import (`python-dotenv`).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success — including `eval` runs with *partial* per-prompt failures |
| 1 | Runtime failure (bad input file, routing error); also `eval` when **all** prompts failed |
| 2 | Usage error: unknown command/option, missing argument, invalid value |

Runtime errors are written to stderr as `error: <message>` without a stack trace. Parser usage errors use each runtime's standard argument-parser formatting.
