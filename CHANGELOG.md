# Changelog

## Unreleased

### Catalog: bump flagship latency tier (claude-fable-5, claude-opus-4-8)

Moved `claude-fable-5` and `claude-opus-4-8` from the `slow` latency tier to
`medium`, raising their speed score from `0.3` to `0.6`. They were being unduly
penalized on speed-weighted/balanced routing relative to their quality. Data-only
change; preset stays byte-identical across SDKs.

### Routing daemon — fast repeated `route`/`eval`

`tryaii route` and `tryaii eval` previously paid the full embedding-stack import
and model load on every invocation (tens of seconds), since each CLI call is a
fresh process; the routing itself is sub-millisecond. Both SDKs now keep a warm
background daemon so only the first call pays that cost — subsequent calls drop
from ~minute to ~milliseconds.

- **Fully transparent — no new commands:** `route`/`eval` auto-start a daemon on
  first use and reuse it thereafter, falling back to in-process routing if it
  can't start. The daemon self-stops when idle (or on `SIGTERM`); the detached
  server runs as a module (`python -m tryaii.server` / `node .../server.js`).
- **New flag / env:** `--no-daemon` (per-call opt-out), `TRYAII_NO_DAEMON=1`
  (global opt-out), `TRYAII_DAEMON_IDLE=<seconds>` (idle shutdown, default 900).
- Implemented identically in the Python and Node SDKs (separate per-runtime
  daemons; loopback TCP + token auth). Protocol documented in `docs/daemon.md`.

### Catalog: standardized OpenAI latency tiers

Normalized inconsistent latency tiers in the OpenAI line so siblings rank
consistently on speed: all `mini` models are now `fast` (`gpt-4o-mini`,
`gpt-5-mini`, `o4-mini`; `gpt-5.4-mini` already was), and the regular full models
are `medium` (`gpt-5` moved down; the rest already were). `nano` models stay
`very fast`, giving a clean nano > mini > regular ordering. Previously
`gpt-5-mini` was `very fast` while `gpt-5.4-mini` was `fast`, and `gpt-5` was
`fast` while `gpt-5.4`/`gpt-5.5` were `medium`, which let an older sibling
out-rank a newer one on balanced routing purely on a speed-tier gap.

### Scoring: benchmark normalization fit to the catalog (more routing spread)

The benchmark normalization ranges were tightened from loose textbook bounds to
the observed min/max of the shipped catalog, so models spread across most of
0–1 instead of bunching into a narrow high band. Previously several benchmarks
were badly compressed (LiveBench used only 18% of the 0–1 range, ARC 23%, Arena
36%), so the quality dimension couldn't differentiate frontier models and
balanced routing collapsed onto cost/speed — one model (`gpt-5-mini`) won 93% of
the 1000-prompt eval. After the change, balanced routing spreads across several
models (top model ~52%), while quality-max still picks the flagships and budget
still picks the cheapest. Quality-only ranking on a single benchmark is
unchanged (normalization is monotonic).

- Updated `NORMALIZATION_RANGES` (`scoring/benchmarks.{ts,py}`) and the mirrored
  `STANDARD_BENCHMARKS` (`benchmarks/standard.{ts,py}`) in both SDKs, kept in
  sync (guarded by `test_parity.py`). Re-fit these when the catalog changes a lot.

### Scoring: cost/speed priorities are now fully suppressible

The priority→weight mapping changed so that a priority of `1` (don't care) on
cost or speed now yields a weight of **0** instead of a `0.28` floor. Previously
even `Priorities(quality=5, cost=1, speed=1)` kept ~32% of the decision on
cost/speed, so a cheaper/faster model could out-rank a strictly higher-quality
one — e.g. quality-max routing picked `gpt-5` over `gpt-5.5`. Now quality-max is
a true quality-only route (the 1000-prompt eval flips from gpt-5/gpt-5.4 to
`gpt-5.5` + `claude-fable-5`). Quality keeps a `0.3` baseline so it never drops
to zero (no divide-by-zero) and a prompt is never scored on cost/speed alone.

- Weight formula: `base + ((priority - 1) / 4) * span` — quality `0.3..1.2`,
  cost/speed `0..1.0`. Balanced (3/3/3) routing is essentially unchanged.
- The no-signal fallback (prompt matches no benchmark) still floors cost/speed
  to `0.1` so it stays "routable on cost/speed" as designed, even when the user
  suppressed them.
- Applied identically to both SDKs (`scoring/priorities.{ts,py}`,
  `scoring/engine.{ts,py}`).

### Model catalog resync (June 2026)

Refreshed the bundled model catalog (`shared/models/default_models.json`, synced
to both SDKs) to match the tryai web app's 2026-06 lineup. **33 → 39 active
models** (`"updated": "2026-06"`).

- **Added (14):** `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`,
  `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-fable-5`, `gemini-3.5-flash`,
  `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite`, `deepseek-v4-pro`,
  `deepseek-v4-flash`, `grok-4.3`, `mistral-medium-2508`.
- **Removed (8):** `o1`, `claude-3-7-sonnet-20250219`, `gemini-2.0-flash`,
  `gemini-3-pro-preview`, `grok-3-mini-latest` (retired upstream) and
  `grok-4-fast`, `gpt-4.1-nano`, `grok-3-latest` (dropped from the active set).
- **Re-priced/re-latency'd** several existing models from the upstream catalog
  (notably `claude-opus-4-5` output $0.075→$0.25/1k, `o3`, `gemini-2.5-pro`,
  `gpt-5.2`). This changes cost/latency-based routing for those models.
- **Corrected two upstream price typos** (input ≥ output): `gemini-2.5-flash`
  output $0.00025→$0.0025 and `gemini-3-flash-preview` input $0.003→$0.0003.
- Added OpenRouter slug mappings for the 14 new models (both SDKs).
- `ARC` scores stay DRE-owned (tryai's ARC column drifted to a different scale);
  new models use peer-consistent ARC estimates. Scoring algorithm unchanged.

See `docs/model-catalog-sync-2026-06.md` for the full transform, decisions, and
flagged upstream pricing anomalies.

## 0.3.0 (2026-06-08)

**Package renamed `tryaii-dre` → `tryaii`** on both PyPI and npm. The old
`tryaii-dre` packages are deprecated and will receive no further updates; install
`tryaii` going forward. This is a breaking change to the install name, import
path, and CLI command — pin to `tryaii-dre` 0.2.x if you can't migrate yet.

### Migration

- **Install:** `pip install tryaii` / `npm install tryaii` (was `tryaii-dre`).
- **Python import:** `from tryaii import Router, Priorities` (was
  `from tryaii_dre import ...`). The import module is now `tryaii` (no underscore).
- **Node import:** `import { Router } from "tryaii"` (was `"tryaii-dre"`).
- **CLI:** the command is now `tryaii` (e.g. `tryaii route "..."`,
  `tryaii eval prompts.json`) — previously `tryaii-dre`.
- The public API (classes, methods, scoring, CLI subcommands and flags) is
  otherwise unchanged; only the names moved. `DREClient` keeps its name.

### CLI surface parity (npm vs PyPI)

The two CLIs accepted slightly different global flags and failed differently;
they now behave identically:

- `-V/--version` now works on the Python CLI too (was Node-only).
- `-v/--verbose` now accepted by the Node CLI too (was Python-only), and works
  in any position on both (Python previously required it before the
  subcommand). Sets `TRYAII_VERBOSE=1` in Node; enables debug logging in
  Python.
- `-h/--help` works in any position on both (e.g. `tryaii eval --help`;
  previously a parse error in Node), bare `tryaii help` works on both (was
  Node-only), and both print byte-identical help text (guarded by
  `test_parity.py`).
- **Per-command help.** `tryaii help <command>` now prints detailed,
  per-command help (usage, arguments, flags, examples, exit codes), and
  `tryaii <command> -h/--help` prints that same page instead of the global
  overview — a behavior change from earlier 0.3.0 builds, where every
  `--help` printed the global text. The `help` command is self-documenting
  (`tryaii help help` / `tryaii help --help`), and an unknown topic
  (`tryaii help bogus`) exits `2`. Every per-command page is kept
  byte-identical across the npm and PyPI CLIs (guarded by `test_parity.py`).
- Exit codes unified: `0` success, `1` runtime failure, `2` usage error
  (unknown command/option, missing argument, invalid value). Node previously
  exited `1` for usage errors; it also no longer silently falls back to
  defaults on non-numeric `--quality/--cost/--speed/--top-k` values.
- Python runtime failures now print a clean one-line `error: ...` message
  instead of a traceback, matching Node.
- Python now rejects a negative `--difficulty-gamma` up front like Node
  (previously it silently skewed budget allocation).
- Python CLI default log level is now WARNING (use `--verbose` for more), so
  `route`/`eval` output is as quiet as the Node CLI.
- `setup` prints the same completion message on both SDKs.

## 0.2.1 (2026-05-31)

Bugfix release. **The 0.2.0 PyPI wheel was broken and has been yanked** — please
use 0.2.1. (The npm 0.2.0 package was unaffected; 0.2.1 is published for parity.)

### Fixed

- **Missing `tryaii_dre.cache` submodule in the published wheel.** The root
  `.gitignore` had an unanchored `cache/` pattern (intended for the repo-root
  benchmark-snapshot cache). Hatchling honors `.gitignore` at build time, so the
  pattern also matched `packages/python/tryaii_dre/cache/` and silently dropped
  it from the sdist and wheel — a clean `pip install tryaii-dre` followed by
  `from tryaii_dre import Router` raised
  `ModuleNotFoundError: No module named 'tryaii_dre.cache'`. The patterns are now
  anchored (`/cache/`, `/cache-shared/`) so only the repo-root directories are
  ignored, and the wheel ships all 33 modules. npm was unaffected (it ships
  `dist/` via the `files` field, not `.gitignore`).

### Changed

- Python CI lint is green again. `UP045` (`Optional[X]` → `X | None`) is ignored
  in ruff config because the package targets Python 3.9 + pydantic, where that
  union syntax raises `TypeError` at annotation-evaluation time; the remaining
  `E402`/`E501`/`F841` findings were cleaned up.

## 0.2.0 (2026-05-30)

First public release on PyPI and npm (the 0.1.0 monorepo below was never
published to a public registry). Highlights: full Node/Python routing parity, a
matching `tryaii-dre` CLI on both packages, and scoring v2 in both SDKs.

### Node/Python SDK parity reconciled

A cross-SDK audit found the Node and Python routers had silently drifted despite
being meant to produce identical routing/scoring/budget decisions. Reconciled to
a single set of canonical rules:

- **Preset data** is now byte-identical across both packages (Python
  `default_models.json` is the source of truth; the Node copy was corrected).
- **Speed scores, cost/speed gating, priorities clamping (round-half-up), and
  tie-breaks** (utility → lowest cost → smallest model id) now match.
- **Budget feasibility** is decided in float; the integer DP only optimizes and
  falls back to the cheapest assignment rather than declaring a feasible dataset
  infeasible.
- **No-benchmark-signal fallback**: prompts whose embedding is orthogonal to
  every centroid stay routable on cost/speed instead of crashing `route()`.
- A `tests/test_parity.py` guard asserts the preset JSONs and normalization
  ranges stay in sync.

### CLI parity — one `tryaii-dre` command on both packages

Both SDKs now ship a matching `tryaii-dre` CLI (`route`, `eval`, `models`,
`benchmarks`, `setup`, `regenerate`) with identical `eval` artifacts
(`results.jsonl` + `summary.json` + `index.html`). Each opens with an animated
blue→red banner printed to stderr that self-suppresses on non-TTY / `NO_COLOR` /
`TRYAII_NO_BANNER` / `--no-banner`.

### Scoring v2 — top-5 benchmarks with median imputation

`packages/node/src/scoring/engine.ts`

Routing now considers the prompt's top-5 most-similar benchmarks (was 3)
and fills missing benchmark data with the **registry-wide median** for
each benchmark instead of silently dropping it.

**Why.** The old behaviour produced an unintuitive failure mode: a model
with sparse benchmark data could outrank a fully-covered model because
missing benchmarks were erased from both the numerator and denominator of
the weighted-quality average. A model with `{HumanEval: 95}` only would
score 100% on the one benchmark it had, while a model with
`{HumanEval: 95, LiveBench: 60}` got dragged down by including LiveBench —
so the broader-coverage model lost.

Observed in the eval harness at `quality=5/cost=1/speed=1`: `grok-4-latest`
(no LiveBench score) was picked for ~84% of coding prompts, beating
`gpt-5.2` (HumanEval 95, LiveBench 78) purely because grok's matched
LiveBench similarity was being dropped from its quality average. After the
fix, gpt-5.2 wins coding under quality-first priorities.

**What changed.**
- `TOP_BENCHMARKS_FOR_SCORING` raised from 3 to 5.
- Missing-benchmark data is imputed from the registry median instead of
  being silently skipped. Median is computed per `scoreModels` call from
  the same `models[]` argument the engine is about to score, so model
  filters flow through correctly.
- Imputation is neutral — sparse data is treated as "average", not zero.
  The previous free-pass effect goes away because the imputed value
  participates in the score instead of vanishing.
- The `bestReasoning` string appends `imputed: N/5` whenever any
  benchmark in the top-5 was imputed for that model, so eval output
  surfaces which decisions involved an estimate vs. real data.
- If **no** model in the registry has data on a benchmark, the existing
  "skip the model entirely if it intersects nothing" path is preserved
  (imputation needs data to estimate from). The test pinning that
  behaviour at `tests/scoring/engine.test.ts:137` still passes.

**API surface.** Unchanged. `RouteResult`, `ModelScore`, and
`ClassificationResult` shapes are the same; no call-site needs to change.

**Behavioural impact.** Routing decisions for prompts whose top-5
benchmarks include sparse entries (LiveBench, SWE-bench, MT-Bench, etc.)
will shift toward models with broader benchmark coverage. The shift is
most visible at quality-heavy priorities; balanced priorities are less
affected because cost and speed dampen swings.

**Python parity.** Mirrored in `packages/python/tryaii_dre/scoring/engine.py` —
both SDKs now use top-5 + median imputation with matching tie-breaks.

## 0.1.0 (2026-03-29)

- Initial monorepo release (ported from diffrential); never published to a public registry
- Python and Node core packages with routing engine support
- 35+ models from 6 providers (OpenAI, Anthropic, Google, xAI, DeepSeek, Mistral)
- 12 standard benchmarks (MMLU, HumanEval, SWE-bench, GSM8K, MT-Bench, etc.)
- Embedding-based semantic classification with keyword fallback
- 3-factor scoring engine (quality, cost, speed) with user priorities
- OpenRouter active routing integration
- CLI tool (tryaii-dre route, models, benchmarks, setup, regenerate)
- LRU cache with TTL for embeddings and classifications
- Pre-computed centroids for zero first-run delay
