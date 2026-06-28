# Examples

## `prompts.json` — 1000-prompt routing eval set

A ready-to-run input for `tryaii eval`: **1000 unique prompts spanning 37 domains**
(academic subjects, math, code, general instructions, and commonsense reasoning).

```bash
tryaii eval examples/prompts.json --output results/run
# or with a budget:
tryaii eval examples/prompts.json --max-price=0.50 --output-tokens=2000
```

Each entry is an object `{"id", "prompt", "category"}`. `category` feeds the
per-category breakdown in the generated `index.html` dashboard. (`tryaii eval`
also accepts a plain JSON array of prompt strings.)

### Provenance & licensing

Every prompt is **unmodified task text** pulled from public, **MIT-licensed**
Hugging Face datasets via the [datasets-server API](https://huggingface.co/docs/datasets-server),
so this file is compatible with the repository's Apache-2.0 license.

| Source dataset | License | Domain(s) | ~count |
|---|---|---|---|
| [Open-Orca/OpenOrca](https://huggingface.co/datasets/Open-Orca/OpenOrca) | MIT | general / diverse instructions | 280 |
| [cais/mmlu](https://huggingface.co/datasets/cais/mmlu) | MIT | 33 academic subjects (each its own category) | 280 |
| [openai/gsm8k](https://huggingface.co/datasets/openai/gsm8k) | MIT | grade-school math | 200 |
| [openai/openai_humaneval](https://huggingface.co/datasets/openai/openai_humaneval) | MIT | code generation | 120 |
| [tau/commonsense_qa](https://huggingface.co/datasets/tau/commonsense_qa) | MIT | commonsense reasoning | 120 |

Notes:
- MMLU and CommonsenseQA items are formatted as self-contained multiple-choice
  prompts (question + lettered options); MMLU keeps its `subject` as the category.
- HumanEval prompts retain their original code formatting (newlines/indentation).
- Category labels are dataset-derived and approximate (a few OpenOrca "general"
  items are really math/code) — they're for grouping, not ground truth.

To rebuild with a different mix, re-fetch from the same datasets-server endpoint
(`/rows?dataset=...&config=...&split=...&offset=...&length=100`) and reassemble.
