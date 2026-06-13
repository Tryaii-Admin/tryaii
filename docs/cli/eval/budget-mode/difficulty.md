# `--difficulty-source` and `--difficulty-gamma`

The lever that makes budget mode *complexity-aware*: harder prompts get a larger share of the budget, so the strong (expensive) models land where they matter.

## How difficulty enters the optimization

1. Each prompt gets a **difficulty score in [0, 1]** from the chosen source (below).
2. Raw difficulties are compressed in practice, so they're converted to **percentile ranks across the dataset** (easiest → 0, hardest → 1; ties share the average rank; a 1-prompt dataset ranks 0). Relative order — not tiny absolute differences — drives allocation.
3. Every candidate of a prompt has its utility (quality score) multiplied by **`1 + gamma × rank`** before the knapsack runs. A maximally hard prompt counts up to `(1 + gamma)×` as much as the easiest one.

Each row's `difficulty` field in [`results.jsonl`](../outputs/results-jsonl.md#budget-mode-rows) records the raw (pre-ranking) score.

## `--difficulty-source`

### `intrinsic` (default) — *what the prompt says*

Content-based: the classifier embeds the prompt and compares it to centroids built from 24 "easy" and 24 "hard" exemplar prompts, then maps the gap through a logistic: `difficulty = 1 / (1 + e^(−10·(simHard − simEasy)))`. Identical exemplars and scale ship in both SDKs. Falls back to `capability` for a prompt when the classifier couldn't produce the signal. See [classification](../../../sdk/routing/classification.md#intrinsic-difficulty).

Best default: independent of your model catalog, and works even when all models score similarly.

### `capability` — *what the models say*

Model-spread based: `difficulty = (q_top − q_cheap) / q_top`, clamped to [0, 1], where `q_top` is the best quality among the prompt's candidates and `q_cheap` is the best quality within the **cheapest third** of candidates (≥ 1). Intuition:

- ≈ 0 — a cheap model is about as good as the frontier → easy, don't spend here.
- ≈ 1 — only expensive models reach top quality → hard, worth the spend.

Using *best-of-cheap-tier* (not the single worst model) stops one junk model from inflating difficulty, and correctly reports "easy" when a cheap-but-strong model exists. Sensitive to your registry: with a custom catalog, this measures *your* models' disagreement.

### `blend`

The arithmetic mean of the two: `0.5 × (capability + intrinsic)`. A hedge when neither signal alone is trustworthy for your workload.

Unknown values are rejected by the CLI (exit 2); the SDK silently falls back to `intrinsic`.

## `--difficulty-gamma`

| Value | Effect |
|---|---|
| `0` | Difficulty ignored — pure quality-per-dollar knapsack |
| `1` (default) | The hardest prompt's quality counts double the easiest's |
| `2–5` | Aggressively concentrates budget on the hard tail |

Must be ≥ 0 (exit 2 otherwise). Raising gamma doesn't change *which* prompts are hard — only how lopsided the allocation gets.

```bash
# Trust the prompt text, strong tilt toward hard prompts
tryaii eval prompts.json --max-price=0.50 --difficulty-source=intrinsic --difficulty-gamma=2

# Let model disagreement decide, maximum tilt
tryaii eval prompts.json --max-price=0.50 --difficulty-source=capability --difficulty-gamma=3

# Plain quality-per-dollar, no difficulty shaping
tryaii eval prompts.json --max-price=0.50 --difficulty-gamma=0
```

To audit the effect, compare `difficulty` against `bestModel`/`estimatedCost` per row, and check `budgetConstrained` — with a well-tuned gamma, easy prompts should be the budget-constrained ones.
