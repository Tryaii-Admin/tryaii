<div align="center">

![pip install tryaii-dre](https://img.shields.io/badge/pip-tryaii--dre-2563eb?style=flat-square&logo=pypi&logoColor=white)
![npm install tryaii-dre](https://img.shields.io/badge/npm-tryaii--dre-ef4444?style=flat-square&logo=npm&logoColor=white)
![python 3.9+](https://img.shields.io/badge/python-3.9%2B-2563eb?style=flat-square&logo=python&logoColor=white)
![node 18+](https://img.shields.io/badge/node-18%2B-ef4444?style=flat-square&logo=node.js&logoColor=white)
![license Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-555?style=flat-square)

</div>

```text
████████╗██████╗ ██╗   ██╗ █████╗ ██╗██╗
╚══██╔══╝██╔══██╗╚██╗ ██╔╝██╔══██╗██║██║   ▸ Diff Routing Engine
   ██║   ██████╔╝ ╚████╔╝ ███████║██║██║   ▸ semantic, prompt-aware LLM routing
   ██║   ██╔══██╗  ╚██╔╝  ██╔══██║██║██║   ▸ ranks 33 models by benchmark × cost × speed
   ██║   ██║  ██║   ██║   ██║  ██║██║██║   ▸ local embeddings — zero API keys to route
   ╚═╝   ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝
```

> **TryAii-DRE** reads your prompt, figures out *what kind of task it is* using local
> embeddings, and routes it to the best LLM for the job — balancing benchmark quality,
> price, and latency the way **you** tell it to. The same engine ships as a Python
> package and a Node/TypeScript package, with one matching `tryaii-dre` CLI.
>
> _(The wordmark above animates in a blue→red gradient when you run the CLI in a real terminal.)_

---

## Install

```bash
pip install tryaii-dre        # Python 3.9+
npm install tryaii-dre        # Node 18+
```

Both install a `tryaii-dre` command on your `PATH`. Routing runs **fully locally** —
embeddings are computed on-device (`sentence-transformers` on Python, ONNX MiniLM via
`@xenova/transformers` on Node), so no API key is needed just to rank models. An
OpenRouter key is only required if you want the SDK to *call* the chosen model for you.

## 30-second quickstart

<table>
<tr><th>Python</th><th>Node / TypeScript</th></tr>
<tr valign="top"><td>

```python
from tryaii_dre import Router, Priorities

router = Router()
result = router.route(
    "Write a Python function to merge sorted arrays",
    priorities=Priorities(quality=5, cost=1, speed=2),
)

print(result.best_model)      # e.g. "gpt-5-nano"
print(result.best_reasoning)  # "Quality: 0.94 on [HumanEval ...]"
```

</td><td>

```ts
import { Router, Priorities } from "tryaii-dre";

const router = new Router();
const result = await router.route(
  "Write a Python function to merge sorted arrays",
  { priorities: Priorities.performance() },
);

console.log(result.bestModel);            // e.g. "gpt-5-nano"
console.log(result.scores[0].reasoning);  // "Quality: 0.94 ..."
```

</td></tr>
</table>

Want it to actually answer? Pass an OpenRouter key and let the client route **and** call:

```python
from tryaii_dre import DREClient
client = DREClient(api_key="sk-or-...")
reply = client.chat("Write a quicksort implementation")
print(reply.model_used, reply.content)
```

```ts
import { DREClient } from "tryaii-dre";
const client = new DREClient({ apiKey: process.env.OPENROUTER_API_KEY });
const reply = await client.chat("Write a quicksort implementation");
console.log(reply.content);
```

---

## Command-line interface

Both the pip and npm packages expose the **same** `tryaii-dre` command. It opens with an
animated blue→red banner, then runs your command.

```bash
tryaii-dre <command> [options]
```

### Commands

| Command | What it does |
|---------|--------------|
| `route "<prompt>"`   | Classify one prompt and print ranked model recommendations with reasoning. |
| `eval <input.json>`  | Route a whole dataset and write `results.jsonl`, `summary.json`, and an `index.html` dashboard. |
| `models`             | List the built-in models (provider, latency, pricing). |
| `benchmarks`         | List the 12 benchmarks and their score-normalization ranges. |
| `setup`              | Download the embedding model and warm the centroids (one-time). |
| `regenerate`         | Rebuild benchmark centroids, e.g. after switching the embedding model. |

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
| `-o, --output <dir>`    | `./tryaii-dre-eval-<timestamp>` | Where to write the run artifacts. |
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

### Global flags & environment

| Flag / env | Effect |
|------------|--------|
| `--no-banner` | Skip the startup banner (works before or after the command). |
| `TRYAII_NO_BANNER=1` | Same as `--no-banner`, via the environment. |
| `NO_COLOR=1` | Render the banner monochrome (color convention). |
| `--version` | Print the version (Node). |
| `-v, --verbose` | Verbose logging (Python). |

The banner prints to **stderr** and auto-suppresses when output is piped or redirected, so
`tryaii-dre models --json > models.json` stays clean.

### Examples

```bash
# Quality-first routing for a single prompt
tryaii-dre route "Debug this memory leak in my Node.js app" --quality=5 --cost=1 --speed=2

# Route a dataset, balanced priorities, into a named folder
tryaii-dre eval prompts.json --output results/run --quality=5 --cost=1 --speed=1

# Spend at most $0.10 across the whole dataset, ~2000 tokens/answer, shrink answers to fit
tryaii-dre eval prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output

# Complexity-aware budget: invest more in the harder prompts (intrinsic difficulty, the default)
tryaii-dre eval prompts.json --max-price=0.50 --output-tokens=2000 --difficulty-source=intrinsic

# Gauge difficulty from model disagreement instead, and push budget harder toward complex prompts
tryaii-dre eval prompts.json --max-price=0.50 --difficulty-source=capability --difficulty-gamma=3

# Inspect what's available
tryaii-dre models --provider anthropic
tryaii-dre benchmarks --json
```

Open the generated `index.html` to see a self-contained dashboard of which models were
recommended, broken down by category — identical across the Python and Node SDKs.

---

## Use TryAii-DRE from an AI agent

Copy–paste the block below into an agent (Claude Code, Cursor, a custom tool, etc.) to
teach it how to use this project. The package name is **`tryaii-dre`** on both PyPI and npm.

````text
You can use TryAii-DRE to pick the best LLM for a prompt before you call it. It classifies
the prompt with local embeddings (no API key needed) and ranks models by benchmark quality,
price, and latency according to priorities you choose.

INSTALL
  Python:  pip install tryaii-dre
  Node:    npm install tryaii-dre

PRIORITIES (1 = ignore, 3 = balanced, 5 = critical) for quality, cost, speed.
  Presets — Python: Priorities.balanced()/performance()/budget()/fast()
            Node:   Priorities.balanced()/performance()/budget()/fast()

PYTHON
  from tryaii_dre import Router, Priorities
  router = Router()
  r = router.route("<prompt>", priorities=Priorities(quality=5, cost=1, speed=2), top_k=5)
  r.best_model        # str  -> the model id to call
  r.best_reasoning    # str  -> why it was chosen
  r.scores            # list -> each has .model_id .final_score .quality_score
                      #         .cost_score .speed_score .reasoning
  r.classification    # .broad_category .subcategory .confidence
  # Optional: route AND call via OpenRouter
  from tryaii_dre import DREClient
  reply = DREClient(api_key="<OPENROUTER_KEY>").chat("<prompt>")
  reply.model_used, reply.content

NODE / TYPESCRIPT
  import { Router, Priorities, DREClient } from "tryaii-dre";
  const router = new Router();
  const r = await router.route("<prompt>", { priorities: Priorities.performance(), topK: 5 });
  r.bestModel;                 // string
  r.scores[0].reasoning;       // string
  r.classification?.confidence;
  const reply = await new DREClient({ apiKey: "<OPENROUTER_KEY>" }).chat("<prompt>");
  reply.content;

CLI (same command for both packages)
  tryaii-dre route "<prompt>" --quality=5 --cost=1 --speed=2
  tryaii-dre eval prompts.json --output results/run            # writes results.jsonl + summary.json + index.html
  tryaii-dre eval prompts.json --max-price=0.10 --output-tokens=2000
  tryaii-dre eval prompts.json --max-price=0.50 --difficulty-source=intrinsic   # spend more on harder prompts
  tryaii-dre models --json        # machine-readable model catalog (stdout)
  tryaii-dre benchmarks --json    # machine-readable benchmark catalog
  # Add --no-banner (or set TRYAII_NO_BANNER=1) for clean, scriptable output.

NOTES
  - route() is async in Node, sync in Python.
  - Routing is local and free; only DREClient/OpenRouter calls hit the network.
  - In budget eval, --max-price is a hard cap and quality/cost/speed flags are ignored.
````

---

## How it works

```
User Prompt
    |
    v
[Embed locally]  -->  Cosine similarity vs 12 benchmark centroids
    |                  (HumanEval, MMLU, GSM8K, SWE-bench, ...)
    v
[Classify task]  -->  "This is a CODE_TECHNICAL task"
    |
    v
[Score models]   -->  (quality·qW + cost·cW + speed·sW) / (qW+cW+sW)
    |
    v
Top-K ranked models, each with human-readable reasoning
```

## Architecture

```
tryaii-dre/
  shared/                  Single source of truth for model data
    models/                33 models with benchmarks and pricing
    benchmarks/            12 standard benchmark definitions
    centroids/             Pre-computed embedding centroids
  packages/
    python/                pip install tryaii-dre
    node/                  npm install tryaii-dre
  scripts/                 Build and sync tooling
```

## Models & benchmarks

**33 models across 6 providers**, pre-loaded with benchmark scores and pricing — and fully
extensible via `router.addModel(...)`:

- **OpenAI** (12): GPT-5.2, GPT-5.1, GPT-5, GPT-5-nano, O3, O4-mini, GPT-4o, GPT-4.1, and more
- **Google** (6): Gemini 3 Pro, Gemini 3 Flash, Gemini 2.5 Pro/Flash, and more
- **xAI** (6): Grok 4, Grok 4 Fast, Grok Code Fast, Grok 3, and more
- **Anthropic** (5): Claude Opus 4.5, Claude Sonnet 4.5, Claude Sonnet 4, Claude Haiku 4.5
- **DeepSeek** (2): Reasoner, Chat
- **Mistral** (2): Large, Small

**12 benchmarks** drive classification: ARC, Chatbot Arena (LMSys), DROP, GSM8K, HellaSwag,
HumanEval, LiveBench, MMLU, MT-Bench, SWE-bench, SuperGLUE, TruthfulQA.

## Packages

| Package | Install | Docs |
|---------|---------|------|
| Python | `pip install tryaii-dre` | [packages/python](packages/python/) |
| Node   | `npm install tryaii-dre` | [packages/node](packages/node/) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

Apache 2.0 — see [LICENSE](LICENSE).
