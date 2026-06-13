# Eval outputs

Every run writes three artifacts into the output directory (`-o/--output`, default `./tryaii-eval-<YYYYMMDD-HHMMSS>`; created recursively). They are always written — including runs with partial failures and `infeasible` budget runs.

| File | What | Details |
|---|---|---|
| `results.jsonl` | One JSON object per prompt (UTF-8, newline-terminated; empty file for zero rows) | [results-jsonl.md](results-jsonl.md) |
| `summary.json` | Aggregates: counts, timings, model distribution, per-category breakdown (+ `budget` block in budget mode); pretty-printed | [summary-json.md](summary-json.md) |
| `index.html` | Self-contained HTML dashboard rendering the summary | [dashboard.md](dashboard.md) |

The dashboard links to its sibling artifacts by relative path — keep the three files together when archiving or publishing a run.

## Console summary

After the artifacts are written:

```
[eval] === Summary ===
Prompts        : 120
Successes      : 118
Errors         : 2
Distinct models: 7
Avg route time : 41.2 ms
Budget status  : optimal          (budget mode only)
Estimated cost : $0.412345        (budget mode only)
Budget         : $0.500000        (budget mode only)

Top recommended models:
  claude-sonnet-4-5-20250929                  60  (50.85%)
  gemini-2.5-flash                            31  (26.27%)
  ...                                              (top 10 of the distribution)

[eval] per-prompt results -> <output>/results.jsonl
[eval] summary            -> <output>/summary.json
[eval] dashboard          -> <output>/index.html
```

Budget mode also prints `[eval] optimizer status: <optimal|infeasible>` earlier in the run, and `[eval] output fit : <requested> -> <effective> tokens/prompt` when [fit-output](../budget-mode/strict-vs-fit-output.md) shrank the answers.

## Machine consumption

- `results.jsonl` is line-delimited — stream it (`jq -c`, pandas `read_json(lines=True)`).
- All status output goes to stdout and the banner to stderr, but the artifacts are the stable machine interface; the console format is not.
- CI tip: exit code 0 does **not** mean zero failures — assert on `summary.json`'s `errorCount` (and `budget.status` for budget runs).
