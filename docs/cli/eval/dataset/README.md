# Eval dataset format

The input file is a **top-level JSON array**. Items can be plain strings, objects, or a mix.

```json
[
  "Explain CAP theorem",
  { "prompt": "Write a SQL migration", "category": "code" },
  { "id": "q42", "prompt": "Draft a polite decline email", "category": "writing" }
]
```

## Item shapes

| Item | Becomes |
|---|---|
| `"some prompt"` | `{ id: "p<n>", prompt: "some prompt", category: "unknown" }` |
| `{ "prompt": "...", "id"?: ..., "category"?: ... }` | `prompt` must be a string; `id` defaults to `p<n>`, `category` to `"unknown"` |

- `<n>` is the 1-based position in the array.
- `id` and `category` are coerced to strings (`42` → `"42"`); empty/null values fall back to the defaults.
- Any other item shape (number, array, object without a string `prompt`) fails the whole run with `Item at index <i> is neither a string nor an object with prompt` (exit 1).

## Field semantics

- **`id`** — carried through to each [`results.jsonl`](../outputs/results-jsonl.md) row; use stable ids to join eval output back to your own dataset.
- **`category`** — a free-form label used only for **grouping**: the `byCategory` section of [`summary.json`](../outputs/summary-json.md) and the per-category cards in the [dashboard](../outputs/dashboard.md). It does not influence routing (the router classifies prompts itself).
- **`prompt`** — routed verbatim. Prompts longer than 100,000 characters are silently truncated by the router.

## Encoding & validation

- Read as UTF-8; a leading BOM is tolerated (common for files written on Windows).
- Not a top-level array → `Expected top-level JSON array in <path>` (exit 1).
- A missing file or malformed JSON also exits 1 with the underlying parse/IO error.
- An empty array `[]` is valid: artifacts are written with zero rows and the run exits 0.

## Practical tips

- Group prompts into meaningful `category` values — the summary and dashboard become far more readable (`code`, `math`, `writing`, …).
- Strings-only files are fine for quick runs; ids/categories pay off as soon as you compare runs.
- The same file works in both [priority mode](../priority-mode/README.md) and [budget mode](../budget-mode/README.md) — the format does not change.
