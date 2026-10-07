<div align="center">

![pip install tryaii](https://img.shields.io/badge/pip-tryaii-2563eb?style=flat-square&logo=pypi&logoColor=white)
![npm install tryaii](https://img.shields.io/badge/npm-tryaii-ef4444?style=flat-square&logo=npm&logoColor=white)
![python 3.9+](https://img.shields.io/badge/python-3.9%2B-2563eb?style=flat-square&logo=python&logoColor=white)
![node 18+](https://img.shields.io/badge/node-18%2B-ef4444?style=flat-square&logo=node.js&logoColor=white)
![license Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-555?style=flat-square)

</div>

```text
████████╗██████╗ ██╗   ██╗ █████╗ ██╗██╗
╚══██╔══╝██╔══██╗╚██╗ ██╔╝██╔══██╗██║██║   ▸ Diff Routing Engine
   ██║   ██████╔╝ ╚████╔╝ ███████║██║██║   ▸ semantic, prompt-aware LLM routing
   ██║   ██╔══██╗  ╚██╔╝  ██╔══██║██║██║   ▸ ranks LLMs by benchmark × cost × speed
   ██║   ██║  ██║   ██║   ██║  ██║██║██║   ▸ local embeddings — zero API keys to route
   ╚═╝   ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝
```

> **TryAii** reads your prompt, figures out *what kind of task it is* using local
> embeddings, and routes it to the best LLM for the job — balancing benchmark quality,
> price, and latency the way **you** tell it to. The same engine ships as a Python
> package and a Node/TypeScript package, with one matching `tryaii` CLI.
>
> _(The wordmark above animates in a blue→red gradient when you run the CLI in a real terminal.)_

**Rather watch the race than predict it?** The same model catalog powers
[**tryaii.com**](https://tryaii.com) — a side-by-side playground where one prompt
fans out to OpenAI, Anthropic, Google, xAI, DeepSeek, and Mistral at once and
streams back with tokens, latency, and per-call cost inline. `tryaii` picks the
winner before you spend; tryaii.com lets you watch them run.

## Demo

https://github.com/user-attachments/assets/b9f4d361-5c63-4138-ab13-f589e959798d

---

## Install

```bash
pip install tryaii        # Python 3.9+
npm install tryaii        # Node 18+
```

Both install a `tryaii` command on your `PATH`. Routing runs **fully locally** —
embeddings are computed on-device (`sentence-transformers` on Python, ONNX MiniLM via
`@xenova/transformers` on Node), so no API key is needed just to rank models. An
OpenRouter key is only required if you want the SDK to *call* the chosen model for you.

Out of the box, `tryaii` routes on a **starter catalog** of 45 well-known models and
16 benchmarks that ships inside the package — no account, no network. Log in (free,
with your tryaii.com Google account) to route on the **full catalog of 322 models**:

```bash
tryaii login     # device sign-in: open the printed URL in any browser, approve the code
tryaii whoami    # shows the account and the downloaded catalog release
tryaii logout    # removes the credentials and the downloaded catalog
```

After login the full catalog downloads automatically, is refreshed at most once a day,
cached locally, and checked against an Ed25519 signature before it is used
([docs](docs/cli/login.md)).

Building a real multi-model product? See the local-first
[design-partner program](docs/design-partners.md).

## Examples

The most common use is `tryaii eval` — route a whole dataset under a budget:

```bash
# Route a dataset -> writes results.jsonl + summary.json + an index.html dashboard
# (a ready-made 1000-prompt dataset ships at examples/prompts.json)
tryaii eval examples/prompts.json --output results/run

# Spend at most $0.50 total; invest more in the harder prompts (default: intrinsic difficulty)
tryaii eval examples/prompts.json --max-price=0.50 --output-tokens=2000

# Gauge difficulty from model disagreement instead, and push budget harder toward hard prompts
tryaii eval examples/prompts.json --max-price=0.50 --difficulty-source=capability --difficulty-gamma=3

# Shrink answers to fit a tight budget instead of failing
tryaii eval examples/prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
```

Or rank models for a single prompt:

```bash
tryaii route "Debug this memory leak in my Node.js app" --quality=5 --cost=1 --speed=2
tryaii models --provider anthropic     # inspect the model catalog
```

Or lint a request for prompt-cache readiness before sending it — 18 dynamic-content
detectors, per-model token floors for 7 providers, and predicted HIT/MISS across
request sequences ([docs](docs/cli/cachelint.md)):

```bash
tryaii cachelint request.json                  # verdict-first report; warns, never blocks
cat prompt.txt | tryaii cachelint - --provider anthropic --model claude-fable-5
```

The same engine runs inside the SDK clients: `cache_lint="warn"` (Python) /
`cacheLint: 'warn'` (Node) lints every outgoing chat request at the moment it
leaves and verifies predictions against the provider's `usage` response —
warn-only, fail-open, once per prompt shape ([docs](docs/sdk/client/cache-lint.md)).

Or diagnose a whole codebase's LLM call sites — your coding agent finds the calls
and writes an inventory; `tryaii` runs four deterministic checks over it (model
fit, cache readiness, cost exposure, prompt hygiene) and renders a local HTML
report. Insight-only: it never edits code and never sends anything anywhere
([docs](docs/cli/diagnose/README.md)):

```bash
tryaii diagnose init                     # once per repo: install the agent playbook
tryaii diagnose check inventory.json --quality 3 --cost 4 --speed 2
tryaii diagnose report                   # open .tryaii/diagnose/<run-id>/index.html
```

Want to shape where tryaii goes next? `tryaii designpartner` enrolls you as a
design partner: a short questionnaire, explicit consent tiers shown verbatim,
and nothing leaves your machine until you run `--confirm`
([docs](docs/cli/designpartner.md)).

Full flag reference is in the [command-line interface](#command-line-interface) section below.

## Fast repeated routing (daemon)

Most of a `tryaii route` call is fixed startup: importing the embedding stack and
loading the model takes seconds, while the routing itself takes under a
millisecond. Because each CLI call is a fresh process, that cost can't be
amortized on its own — so `route` and `eval` automatically start a small
background **daemon** that keeps the model warm. The first call pays the load
once; every call after it is near-instant.

```bash
tryaii route "what's greater, 5 or 5.5?"   # first call: loads the model, starts the daemon
tryaii route "write a haiku about winter"  # subsequent calls: ~milliseconds of routing
```

It's fully transparent and safe to ignore — there are no daemon commands to
learn. The daemon self-stops after 15 minutes idle (or on `SIGTERM`). To opt out
for a single call use `--no-daemon`; to disable it everywhere set
`TRYAII_NO_DAEMON=1`. Tune the idle shutdown with `TRYAII_DAEMON_IDLE=<seconds>`.
The Python and Node SDKs run separate daemons (their embedding backends differ);
see [`docs/daemon.md`](docs/daemon.md) for the protocol and state-file details.

## 30-second quickstart (SDK)

<table>
<tr><th>Python</th><th>Node / TypeScript</th></tr>
<tr valign="top"><td>

```python
from tryaii import Router, Priorities

router = Router()
result = router.route(
    "Write a Python function to merge sorted arrays",
    priorities=Priorities(quality=5, cost=1, speed=2),
)

print(result.best_model)      # e.g. "anthropic/claude-opus-5.5"
print(result.best_reasoning)  # "q'=0.98 (4 real of 5) | imputed: 1/5 | cost 0.2066 ($12.00/M) | ..."
```

</td><td>

```ts
import { Router, Priorities } from "tryaii";

const router = new Router();
const result = await router.route(
  "Write a Python function to merge sorted arrays",
  { priorities: Priorities.performance() },
);

console.log(result.bestModel);            // e.g. "anthropic/claude-opus-5.5"
console.log(result.scores[0].reasoning);  // "q'=0.98 (4 real of 5) | ..."
```

</td></tr>
</table>

Want it to actually answer? Pass an OpenRouter key and let the client route **and** call:

```python
from tryaii import DREClient
client = DREClient(api_key="sk-or-...")
reply = client.chat("Write a quicksort implementation")
print(reply.model_used, reply.content)
```

```ts
import { DREClient } from "tryaii";
const client = new DREClient({ apiKey: process.env.OPENROUTER_API_KEY });
const reply = await client.chat("Write a quicksort implementation");
console.log(reply.content);
```

---

## Command-line interface

Both the pip and npm packages expose the **same** `tryaii` command. It opens with an
animated blue→red banner, then runs your command.

```bash
tryaii <command> [options]
```

### Commands

| Command | What it does |
|---------|--------------|
| `route "<prompt>"`   | Classify one prompt and print ranked model recommendations with reasoning. |
| `eval <input.json>`  | Route a whole dataset and write `results.jsonl`, `summary.json`, and an `index.html` dashboard. |
| `models`             | List the models of the catalog in use (provider, latency, pricing): the 45-model starter catalog, or the full catalog after `login`. |
| `benchmarks`         | List the catalog's benchmarks and their score-normalization ranges (16 in the starter catalog). |
| `setup`              | Download the embedding model and warm the centroids (one-time). |
| `regenerate`         | Rebuild benchmark centroids, e.g. after switching the embedding model. |
| `cachelint <request.json>` | Lint a chat request for prompt-cache readiness — verdict-first report; warns, never blocks. |
| `diagnose <verb>`    | Agent-first codebase LLM diagnostics — model fit, cache readiness, cost exposure, prompt hygiene; insight-only, rendered to a local HTML report. |
| `designpartner`      | Enroll as a tryaii design partner — one resumable command; nothing is sent without an explicit `--confirm`. |
| `login`              | Sign in with your tryaii.com account (free; unlocks the full model catalog of 322 models). |
| `logout`             | Sign out and remove the stored credentials and the downloaded full catalog. |
| `whoami`             | Show the signed-in account and the catalog release in use (`--json`). |

### Options

**`route`** — rank models for a single prompt

| Option | Default | Description |
|--------|---------|-------------|
| `--quality <1-5>` | `3` | How much benchmark quality matters. |
| `--cost <1-5>`    | `3` | How much cheap pricing matters. |
| `--speed <1-5>`   | `3` | How much low latency matters. |
| `--top-k <n>`     | `5` | Number of recommendations to print. |

**`eval`** — route a JSON dataset of prompts (array of strings, or objects with `prompt` + optional `id`/`category`)

| Option | Default | Description |
|--------|---------|-------------|
| `-o, --output <dir>`    | `./tryaii-eval-<timestamp>` | Where to write the run artifacts. |
| `--quality / --cost / --speed <1-5>` | `3` | Priorities (ignored in budget mode). |
| `--top-k <n>`           | `5`      | Models recorded per prompt. |
| `--max-price <usd>`     | _off_    | Total budget for the **whole dataset**. Switches eval into budget-optimized mode: price becomes a hard constraint and the optimizer maximizes quality under it. |
| `--output-tokens <n>`   | `1000`   | Expected response length per prompt, used for cost estimation. |
| `--budget-mode <mode>`  | `strict` | `strict` fails if the requested output length can't fit the budget; `fit-output` lowers the per-prompt output length until everything fits. |
| `--difficulty-source <s>` | `intrinsic` | How task complexity is gauged so the optimizer invests more in harder prompts (budget mode only). `intrinsic` = how complex the prompt *looks* (embedding distance to easy/hard exemplars); `capability` = how much model choice changes quality; `blend` = mean of both. |
| `--difficulty-gamma <n>` | `1` | How aggressively budget shifts toward harder prompts. `0` disables complexity-aware allocation (utility = raw quality); higher concentrates more spend on hard prompts. |

> **Complexity-aware routing.** In budget mode the optimizer spends *more* on harder prompts and *less* on easy ones, at the same total budget. Difficulty is rank-normalized across the dataset, so it adapts to whatever mix of prompts you pass. Choose how difficulty is measured with `--difficulty-source`:
> - **`intrinsic`** (default) — content-based: open-ended / multi-step prompts score high, short / factual ones low. Independent of your model catalog.
> - **`capability`** — catalog-based: high only when expensive models clearly beat cheap ones on the task; near-flat when your cheap models are already strong.
> - **`blend`** — the mean of the two.

**`models`** — `--provider <name>` filters by provider; `--json` prints machine-readable output.
**`benchmarks`** — `--json` prints machine-readable output.
**`setup` / `regenerate`** — `--model <name>` selects a non-default embedding model.
**`cachelint`** — `-` reads the request from stdin; `--provider` / `--model` select the cache rules ([docs](docs/cli/cachelint.md)).
**`diagnose`** — verbs `init` / `plan` / `check` / `report` ([docs](docs/cli/diagnose/README.md)).
**`designpartner`** — at most one action flag per run: `--answers` / `--consent` / `--confirm` / `--reset`; `--json` prints the machine-readable status agents consume ([docs](docs/cli/designpartner.md)).
**`login` / `logout`** — no options ([login](docs/cli/login.md), [logout](docs/cli/logout.md)).
**`whoami`** — `--json` prints the account as the server returns it ([docs](docs/cli/whoami.md)).

### Global flags & environment

| Flag / env | Effect |
|------------|--------|
| `--no-banner` | Skip the startup banner (works before or after the command). |
| `TRYAII_NO_BANNER=1` | Same as `--no-banner`, via the environment; also silences the once-a-day login hint. |
| `TRYAII_API_URL` | API base URL for `login` (default `https://api.tryaii.com`). |
| `NO_COLOR=1` | Render the banner monochrome (color convention). |
| `-V, --version` | Print the version and exit. |
| `-v, --verbose` | Verbose logging. |
| `-h, --help` | Show help (works before or after the command, plus bare `tryaii help`). |

All global flags work identically on the npm and PyPI CLIs and are accepted in any position.
Exit codes also match: `0` success, `1` runtime failure, `2` usage error (`130` when `login` is cancelled).

When nobody is logged in, `route`, `eval` and `models` print a one-line hint on **stderr**
at most once a day: `Routing on the starter catalog (45 models). Log in for free to use the
full catalog (322 models): tryaii login`.

The banner prints to **stderr** and auto-suppresses when output is piped or redirected, so
`tryaii models --json > models.json` stays clean. See [Examples](#examples) (top of this
README) for runnable commands; open the generated `index.html` for a self-contained dashboard
of which models were recommended, broken down by category.

---

## Use TryAii from an AI agent

Copy–paste the block below into an agent (Claude Code, Cursor, a custom tool, etc.) to
teach it how to use this project. The package name is **`tryaii`** on both PyPI and npm.
Expand it and use the copy button in its top-right corner. (The `diagnose` and
`designpartner` commands need no copy-paste — each installs its own agent skill
into `.claude/skills/` on first run.)

<details>
<summary><b>📋 Full agent instructions</b></summary>

````text
You can use TryAii to pick the best LLM for a prompt before you call it. It classifies
the prompt with local embeddings (no API key needed) and ranks models by benchmark quality,
price, and latency according to priorities you choose.

INSTALL
  Python:  pip install tryaii
  Node:    npm install tryaii

PRIORITIES (1 = ignore, 3 = balanced, 5 = critical) for quality, cost, speed.
  Presets — Python: Priorities.balanced()/performance()/budget()/fast()
            Node:   Priorities.balanced()/performance()/budget()/fast()

PYTHON
  from tryaii import Router, Priorities
  router = Router()
  r = router.route("<prompt>", priorities=Priorities(quality=5, cost=1, speed=2), top_k=5)
  r.best_model        # str  -> the model id to call
  r.best_reasoning    # str  -> why it was chosen
  r.scores            # list -> each has .model_id .final_score .quality_score
                      #         .cost_score .speed_score .reasoning
  r.classification    # .broad_category .subcategory .confidence
  # Optional: route AND call via OpenRouter
  from tryaii import DREClient
  reply = DREClient(api_key="<OPENROUTER_KEY>").chat("<prompt>")
  reply.model_used, reply.content

NODE / TYPESCRIPT
  import { Router, Priorities, DREClient } from "tryaii";
  const router = new Router();
  const r = await router.route("<prompt>", { priorities: Priorities.performance(), topK: 5 });
  r.bestModel;                 // string
  r.scores[0].reasoning;       // string
  r.classification?.confidence;
  const reply = await new DREClient({ apiKey: "<OPENROUTER_KEY>" }).chat("<prompt>");
  reply.content;

CLI (same command for both packages)
  tryaii route "<prompt>" --quality=5 --cost=1 --speed=2
  tryaii eval examples/prompts.json --output results/run   # writes results.jsonl + summary.json + index.html
  tryaii eval examples/prompts.json --max-price=0.10 --output-tokens=2000
  tryaii eval examples/prompts.json --max-price=0.50 --difficulty-source=intrinsic   # spend more on harder prompts
  tryaii models --json        # machine-readable model catalog (stdout)
  tryaii benchmarks --json    # machine-readable benchmark catalog
  tryaii login                # optional, free: unlocks the full 322-model catalog
  # Add --no-banner (or set TRYAII_NO_BANNER=1) for clean, scriptable output.

NOTES
  - route() is async in Node, sync in Python.
  - Routing is local and free; only DREClient/OpenRouter calls hit the network
    (plus, once logged in, an at-most-daily check for a new full-catalog release).
  - Without login, routing uses the 45-model starter catalog; after `tryaii login`
    it uses the full catalog of 322 models.
  - In budget eval, --max-price is a hard cap and quality/cost/speed flags are ignored.
````

</details>

### Quick copy-paste prompts (evaluate LLMs per price)

Expand a block and use the copy button in its top-right corner.

<details>
<summary><b>📋 pip / Python</b></summary>

```text
Install the `tryaii` PyPI package (`pip install tryaii`) — a local, no-API-key LLM
router that ranks LLMs by quality, price, and latency. Demo evaluating LLMs per price:
create prompts.json (an array of 5 example prompt strings), then run a budget eval where
--max-price is the total $ cap for the whole dataset and the optimizer maximizes quality
under it:
  tryaii eval prompts.json --output results/budget --max-price=0.10 --output-tokens=2000 --no-banner
Open results/budget/index.html and read summary.json, then report which models win on
quality-per-dollar. Also show the per-prompt tradeoff in Python:
  from tryaii import Router, Priorities
  r = Router().route("Refactor this module", priorities=Priorities.budget(), top_k=5)
  for s in r.scores: print(s.model_id, s.final_score, s.quality_score, s.cost_score)
```

</details>

<details>
<summary><b>📋 npm / Node</b></summary>

```text
Install the `tryaii` npm package (`npm install tryaii`) — a local, no-API-key LLM
router that ranks LLMs by quality, price, and latency. Demo evaluating LLMs per price:
create prompts.json (an array of 5 example prompt strings), then run a budget eval where
--max-price is the total $ cap for the whole dataset and the optimizer maximizes quality
under it:
  tryaii eval prompts.json --output results/budget --max-price=0.10 --output-tokens=2000 --no-banner
Open results/budget/index.html and read summary.json, then report which models win on
quality-per-dollar. Also show the per-prompt tradeoff in Node (route() is async):
  import { Router, Priorities } from "tryaii";
  const r = await new Router().route("Refactor this module", { priorities: Priorities.budget(), topK: 5 });
  for (const s of r.scores) console.log(s.modelId, s.finalScore, s.qualityScore, s.costScore);
```

</details>

---

## How it works

```
User Prompt
    |
    v
[Embed locally]  -->  Cosine similarity vs the catalog's benchmark centroids
    |                  (GPQA, LiveCodeBench, MMLU-Pro, Tau2-bench, ...)
    v
[Classify task]  -->  "This is a CODE_TECHNICAL task"
    |
    v
[Score models]   -->  quality sets a tolerance band below the best model;
    |                  cost and speed pick the winner inside the band
    |
    v
Top-K ranked models, each with human-readable reasoning
```

## Architecture

```
tryaii/
  shared/                  Single source of truth, synced into both packages
    catalog/starter/       Starter catalog bundle: 45 models, 16 benchmarks, centroids,
                           training queries, normalization ranges
    cachelint/ diagnose/ designpartner/   Cross-SDK specs and fixtures
  packages/
    python/                pip install tryaii
    node/                  npm install tryaii
  scripts/                 Build and sync tooling
```

## Models & benchmarks

The package ships a **starter catalog: 45 models from 6 providers, scored on 16
benchmarks** — enough to route real workloads with no account and no network. Model ids
are OpenRouter-native slugs (`provider/model`, e.g. `openai/gpt-5.5`,
`anthropic/claude-opus-5.5`), so the chosen id can be passed straight through to
OpenRouter, and the catalog is fully extensible via `router.addModel(...)` /
`router.add_model(...)`.

- **openai** (16): GPT-5.5, GPT-5.4 (+ mini / nano), GPT-5.x, o3 / o4-mini, GPT-4o, gpt-oss-120b, and more
- **anthropic** (10): Claude Opus 5.5, Fable 5.1 / 5, Sonnet 5.5 / 4.x, Opus 4.8 / 4.5, Haiku 4.5
- **google** (8): Gemini 3.8 Flash, 3.5 Flash, 3.1 Pro / Flash-Lite, 2.5 family
- **deepseek** (4), **x-ai** (4), **mistralai** (3)

**16 benchmarks** drive classification in the starter catalog: AIME-2024/2025, GPQA,
HLE, AA-LCR, MMLU-Pro, MMMU, LegalBench, LiveCodeBench, SciCode, Tau2-bench,
Terminal-bench-Hard, IFBench, and Chatbot Arena Elo (+ Code / Vision).

**Log in for the full catalog.** `tryaii login` (free) unlocks the full catalog of
**322 routable models** across many more providers and benchmarks. It downloads right
after login, refreshes at most once a day, is cached under `~/.tryaii/catalog/`, and is
used only after its Ed25519 signature checks out. In code, `Router(catalog="auto")`
(the default) picks the full catalog when you are logged in; `catalog="starter"` keeps
routing offline on the packaged catalog. Ephemeral OpenRouter `:free` variants are never
routed in either catalog.

## Packages

| Package | Install | Docs |
|---------|---------|------|
| Python | `pip install tryaii` | [packages/python](packages/python/) |
| Node   | `npm install tryaii` | [packages/node](packages/node/) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

Maintainers: the release process (version bump, tag, Trusted Publishing to PyPI and npm) is in [RELEASING.md](RELEASING.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
