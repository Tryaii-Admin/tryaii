# diagnose — behavior contract

This file freezes the behavior of `tryaii diagnose` across both SDKs
(`packages/python/tryaii/diagnose` — the REFERENCE implementation — and
`packages/node/src/diagnose`). Any behavior change is a three-part edit:
update this SPEC, regenerate `fixtures/` from the Python engine
(`scripts/gen-diagnose-fixtures.py`), and update BOTH engines in one PR.

Product frame (locked): diagnose is an **agent-first codebase diagnostic**.
The user's coding agent interviews the user, discovers LLM call sites in the
codebase (free-form, language-agnostic), and hands the CLI a JSON *inventory*.
The engines below are deterministic; the agent is the only discovery layer.
diagnose is **insight-only** — it never mutates code and never sends payloads.

---

## §1 Cross-language ground rules

1.1 All rules of `shared/cachelint/SPEC.md` §1 apply verbatim — in particular
    the **integral-float rule** (§1.3: any float that is integral after
    rounding is emitted as an integer, which makes
    `json.dumps(indent=2)` ≡ `JSON.stringify(null, 2)`), code-point offsets,
    and canonical JSON key handling. Both engines pass their final findings
    document through the cachelint number normalizer
    (`tryaii.cachelint._jsonutil.normalize_numbers` / the Node
    `stableJson` equivalent).

1.2 Money and score floats are rounded half-even to 4 decimal places before
    emission (`round(x, 4)` / `halfEvenRound(x, 4)`).

1.3 Key ORDER in emitted JSON objects is part of the contract (the Node
    conformance test asserts `JSON.stringify` equality against fixtures).
    Objects are built in the field order given in this SPEC.

1.4 Timestamps are opaque strings: the engine never reads a clock. `run_id`
    and `generated_at` are inputs (CLI flags `--run-id`/`--now` or the
    current UTC time formatted by the CLI layer, not the engine).

## §2 Check semantics

### §2.0 Intake (lenient, honest)

Input: an inventory — either `{"version"?, "defaults"?, "sites": [...]}` or a
bare array of sites. Anything else → `ValueError` ("inventory must be an
object with a 'sites' array, or an array of sites").

`defaults`: `{calls_per_day?, output_tokens?}`. `output_tokens` falls back to
**500** when absent at both levels. `calls_per_day` has no fallback.

Per site (processed in input order, `index` = position in the input array):

| Field | Rule |
|---|---|
| `file` | required non-empty string → else site is **skipped** (recorded in `inventory.skipped` as `{index, reason}`) |
| `line` | required; int ≥ 1; integral floats and integer-strings are accepted and coerced; else skipped |
| `prompt` | optional; string or cachelint prompt object (`{system?, tools?, messages?}`); a present-but-invalid prompt (wrong type) is treated as absent |
| `id` | optional string → `site_id`; absent → `site_id = "<file>:<line>"` |
| `provider`, `model` | optional strings, trimmed; empty → absent |
| `calls_per_day`, `output_tokens` | optional; number > 0 (integral coercion as `line`); invalid → absent |
| `notes` | optional string, echoed through |
| `_classification` | optional injection seam (§2.2); `{benchmark_similarities: {name: float}, broad_category, subcategory, confidence, difficulty?}` |

A non-object site is skipped (`reason: "site is not an object"`). Missing
file/line reasons: `"missing file"` / `"missing or invalid line"`.

Duplicate `site_id`s are disambiguated deterministically: the 2nd occurrence
becomes `<site_id>#2`, the 3rd `<site_id>#3`, …

**Degradation doctrine**: a missing input never fails the run and never
produces a guessed value. Each check reports one of four statuses:

- `ok` — check ran, nothing to act on (rendered as a green check).
- `finding` — check ran, actionable result.
- `insufficient_data` — check could not run for THIS site; `reason` says why.
- `skipped` — check not selected for this run; `reason: "not selected"`.

Every non-skipped site appears in `sites` with all four check objects.

### §2.1 Model resolution (`resolved_model_id`)

Resolution of a site's `model` string against the routing registry
(`ModelRegistry.default()`), tried in order; first hit wins:

1. **exact**: `get_model(model)`.
2. **slug**: the model matches an OpenRouter slug in the SDKs'
   `MODEL_ID_TO_OPENROUTER` mapping (compared after the normalization of
   step 3 on both sides) → the mapped registry id.
3. **normalized**: `norm(model) == norm(registry_id)` for exactly one
   registry id, where `norm` = trim, lowercase, collapse `[ ._/]+` runs to
   `-` (the cachelint `norm_model` rule). If the model contains `/`, the
   text after the LAST `/` is also tried under the same rule. Ambiguous
   (2+ registry hits) resolves to nothing.
4. otherwise unresolved → `resolved_model_id: null`.

No fuzzy matching. Unresolved models degrade honestly (§2.2, §2.4).

### §2.2 model_fit

Needs: a prompt AND a classification. Classification comes from the site's
`_classification` seam if present, else from the engine's injected
`classify_fn(canonical_text)` (the CLI wires this to the router; fixtures
always use the seam). `canonical_text` is the cachelint canonical render of
the prompt. No classification source → `insufficient_data`
(`reason: "no classifier available"`); no prompt → `"no prompt"`.

Scoring: `ScoringEngine.score_models(all_models, benchmark_similarities,
priorities, top_k = len(all_models))` — the full catalog, ranked. `rank` =
1-based position. The renormalized `final_score` is NEVER emitted; only the
absolute raw dimensions (`quality_score`, `cost_score`, `speed_score`) and
rank are, plus `reasoning` and `top_benchmarks` (arrays of
`[name, score]`).

Status (policy, frozen here):

- current model resolved and `current_rank <= 3` → `ok`
- `4 <= current_rank <= 10` → `finding` (summary "consider <best>")
- `current_rank > 10` → `finding` (summary "mismatch")
- current model unresolved, or resolved but absent from the ranking (the
  engine dropped it for lack of benchmark signal) → `insufficient_data`
  with reason `"unknown model '<model>'"` / `"no model declared"` /
  `"model '<id>' has no benchmark signal for this prompt"`; the
  recommendation payload is still emitted in full.

Payload field order: `status`, `reason`, `summary`, `classification`
(`broad_category`, `subcategory`, `confidence` — each null when the
classification source omitted it), `current_rank`, `catalog_size` (the
number of models actually RANKED — normally the whole catalog; a model the
engine dropped for lack of signal is not counted), `current` (null or
`{model_id, quality_score, cost_score, speed_score}`), `recommended`
(`{model_id, quality_score, cost_score, speed_score, top_benchmarks,
reasoning}`), `top` (the first **5** of the full ranking: `{rank, model_id,
quality_score, cost_score, speed_score, reasoning}`).

`summary` one-liners (exact strings):
- ok: `"<model_id> is a strong fit (rank <r> of <n>)"`
- consider: `"consider <best_id> (current <model_id> ranks <r> of <n>)"`
- mismatch: `"<model_id> is a poor fit for this prompt (rank <r> of <n>) — best: <best_id>"`

### §2.3 cache_readiness

Needs: a prompt AND a provider (cachelint `resolve` requires one). Missing →
`insufficient_data` (`"no prompt"` / `"no provider declared"`); an unknown
provider surfaces cachelint's ValueError message as the reason.

Runs cachelint `analyze_full` on the single item
`{prompt, llm: {provider, name: <model or "">}}`. Status: `ok` iff the
verdict code is `CACHEABLE`; every other verdict is `finding`.

Payload field order: `status`, `reason`, `verdict` (`{code, summary}`),
`threshold_min_tokens`, `total_tokens`, `stable_prefix` (`{tokens,
pct_of_prompt, if_first_fixed_tokens}`), `findings_count`, `top_findings`
(first **3** of the item's findings, cachelint dict shape),
`recommendations` (the item's list, unmodified).

### §2.4 cost_exposure

`output_tokens` = site → defaults → 500. `input_tokens`:

- when §2.3 produced a token report → its `total.tokens`
  (`token_method: "tokenizer"`)
- else → `estimate_tokens(canonical_text)` = `max(1, ceil(len/4))`
  (`token_method: "chars/4"`); no prompt → `insufficient_data`
  (`"no prompt"`).

`cost_per_call_usd` = `estimate_generation_cost(resolved model,
input_tokens, output_tokens)` (the budget-module primitive). Unresolved
model → `insufficient_data` (`"unknown model '<model>'"` /
`"no model declared"`); missing pricing → `insufficient_data`
(`"no pricing for '<id>'"`).

`monthly`: needs `calls_per_day` (site → defaults). Present →
`{status: "ok", calls_per_day, cost_usd}` where
`cost_usd = round4(cost_per_call * calls_per_day * 30)`. Absent →
`{status: "insufficient_data", reason: "no traffic estimate"}`.

`cache_savings_usd` (inside `monthly`, only when monthly is ok): an **upper
bound**, emitted only when ALL hold — §2.3 ran with a cacheable-family
verdict (`CACHEABLE`, `CACHEABLE_WITH_ACTION`, `CACHEABLE_PREFIX`), the
provider (or its detected upstream, for `openrouter`) has a factor in
`costmodel.json` `read_discount_factors`, and the model has pricing. Then
`round4(stable_prefix_tokens / 1000 * input_per_1k * factor *
calls_per_day * 30)`. Otherwise the key is absent.

`swap`: needs a §2.2 ranking AND a scored current model. Candidates =
ranking entries with pricing and `quality_score >= current.quality_score -
0.05`; pick the lowest `cost_per_call_usd` (same input/output tokens; ties
break on rank). Emitted only when strictly cheaper than the current model:
`{model_id, cost_per_call_usd, monthly_savings_usd?}` (monthly only with
traffic). Otherwise `swap: null`.

Status: `insufficient_data` per above; else `finding` when a swap exists;
else `ok`. Field order: `status`, `reason`, `input_tokens`, `output_tokens`,
`token_method`, `cost_per_call_usd`, `monthly`, `swap`.

### §2.5 hygiene

Needs only a prompt (provider-independent). Runs the cachelint canonical
render + detector scan with section attribution via the public
`cachelint.analyzer.hygiene_findings(prompt)` helper. Each finding carries
the cachelint finding dict plus `hint` (the cachelint fix-hint for its kind;
default `"Move this value after the static prefix."`).

Payload field order: `status`, `reason`, `findings_count`,
`blocking_count`, `findings_in_system`, `findings` (first **10**, each
`{kind, severity, section, section_offset, pct_into_prompt, excerpt, why,
hint}`), `advisories`. `advisories` (exact strings, in this order, only
when applicable):

- no `system`/`tools` section in a structured prompt with ≥ 2 messages:
  `"No system block: shared static instructions in a system block form the classic cacheable unit."`
- `findings_in_system > 0`:
  `"<n> dynamic value(s) inside the system block — the system block should be fully static."`

Status: `finding` iff `blocking_count > 0`, else `ok`;
`insufficient_data` (`"no prompt"`) without a prompt.

## §3 The findings document

`analyze_inventory(inventory, opts, classify_fn?)` returns (field order):

```
version            1
run_id             opts.run_id (opaque)
generated_at       opts.now (opaque)
tool               {name: "tryaii", version: opts.version}
interview          {priorities: {quality, cost, speed}, goal (string|null),
                    defaults: {calls_per_day (number|null), output_tokens}}
inventory          {site_count, skipped: [{index, reason}]}
sites              [per §2, field order: site_id, file, line, provider,
                    model, resolved_model_id, notes?, checks: {model_fit,
                    cache_readiness, cost_exposure, hygiene}]
summary            §3.1
```

`opts.checks` (default all four, in the canonical order `model_fit`,
`cache_readiness`, `cost_exposure`, `hygiene`) selects checks; unselected
ones emit `{status: "skipped", reason: "not selected"}`. Priorities follow
the routing `Priorities` clamping rules (round-half-up, clamp 1–5).

### §3.1 summary (the redacted layer)

Designed as the future upload payload. MUST NOT contain code, prompts, file
paths, model reasoning text, or the goal text. Field order:

```
schema                    "tryaii.diagnose.summary/1"
run_id, generated_at      as above
site_count                len(sites)
goal_present              bool
priorities                as interview
check_status_counts       {<check>: {ok, finding, insufficient_data, skipped}}
cache_verdict_counts      {<verdict code>: n} — only codes that occur, key
                          order = first occurrence over sites in order
totals                    {est_monthly_cost_usd, est_monthly_cache_savings_usd,
                           est_monthly_swap_savings_usd, sites_with_traffic_data}
                          (sums of the sites' computed values, round4; a sum
                           with no contributing site is null, except
                           sites_with_traffic_data which is an int)
swap_stats                {sites_with_cheaper_swap, median_current_rank}
                          (median over sites with a current_rank; even count
                           → mean of the middle two; none → null)
```

## §4 Report rendering

Run directory: `<out_dir>/<run_id>/` containing `inventory.json` (the
intake, verbatim re-dump with indent 2), `findings.json`, `meta.json`
(`{run_id, generated_at, tool}`), `index.html`. `<out_dir>/latest` is
a plain text file holding `<run_id>\n` (never a symlink). Run ids sort
lexicographically; the CLI default is UTC `YYYYMMDDTHHMMSSZ`.

### §4.1 Template + substitution language

The report HTML comes from ONE shared template
(`shared/diagnose/report/template.html`, packed by sync-shared into both
packages' `diagnose/data/report_template.json` as `{"html": "..."}`).
Both renderers implement the same two-construct substitution language —
the eval dashboard's silent cross-SDK drift is the cautionary tale this
design exists to prevent:

1. `{{key}}` — replaced with the HTML-ESCAPED string value of `key`,
   resolved innermost-scope-first. A missing key is a renderer ERROR in
   both languages (never silently empty), so template and scope builder
   cannot drift apart.
2. `<!--BEGIN name-->inner<!--END name-->` — `name` must resolve to a list
   of scopes; `inner` renders once per item (item scope chained onto the
   parent), concatenated. Conditional content = a 0-or-1-item list.
   Blocks nest; the SAME name never nests inside itself.

Escape table (applied to every `{{key}}` value; the fixed markup is never
escaped): `&`→`&amp;` `<`→`&lt;` `>`→`&gt;` `"`→`&quot;` `'`→`&#39;`.

Every scope value is a pre-formatted STRING (built with the SPEC'd helpers
below) — no numbers ever cross the render boundary, so formatting parity
lives in one place. Helpers: `money2(x)` = `"$" + <half-even .2f>`;
`money4(x)` = `"$" + <half-even .4f>`; signed variants prefix `+`/`-`;
integers/pre-rounded floats stringify natively (the §1.3 normalizer
already made integral floats integers).

The HTML is a pure function of findings.json + the previous run's
findings.json (deltas) — the only timestamp shown is `generated_at` from
the findings document. Written with `\n` newlines.

### §4.2 Deltas (report-time, vs the previous run)

`previous` = the run id immediately before the current one in the store
(§4). When present, the report shows a delta band computed per `site_id`
present in BOTH runs, comparing each of the four checks where both
statuses are in {`ok`, `finding`}: `finding`→`ok` is an improvement event,
`ok`→`finding` a regression event (transitions involving
`insufficient_data`/`skipped` are ignored — data availability changes are
not quality changes). Then:

- `sites_improved` = sites with ≥1 improvement and NO regression
- `sites_regressed` = sites with ≥1 regression (a mixed site counts here)
- `sites_added` / `sites_removed` = site_id set differences
- monthly delta = current − previous `totals.est_monthly_cost_usd`, only
  when both are non-null, formatted signed money2.

### §4.3 The `report` verb

`tryaii diagnose report [--run <id>] [--out-dir <dir>] [--out <file>]` —
loads `--run` (default: the `latest` pointer), renders against the
previous run when one exists, writes `<out_dir>/<run_id>/index.html`
(or `--out`), echoes `-> <path>` with forward slashes. No runs → exit 1.

## §5 Fixtures

`fixtures/` mirrors the cachelint machinery: hand-written `cases.json`
inputs per suite, machine-generated `expected` blocks from the Python
reference via `scripts/gen-diagnose-fixtures.py` (`--suite`, `--check`).
Suites: `intake`, `resolve`, `modelfit`, `cost`, `hygiene`, `check` (full
inventory → findings document), later `report` and `cli`. All suites are
routing-free: model_fit cases always use the `_classification` seam, so no
fixture ever needs the embedding model. The `check` suite pins
`run_id`/`now`/`version` via opts (`"0.0.0"` for version) — fixture output
never depends on the package version or a clock.
