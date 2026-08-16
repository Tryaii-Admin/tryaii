# designpartner — behavior contract

This file freezes the behavior of `tryaii designpartner` across both SDKs
(`packages/python/tryaii/designpartner` — the REFERENCE implementation —
and `packages/node/src/designpartner`). Any behavior change is a
three-part edit: update this SPEC, regenerate `fixtures/` from the Python
engine (`scripts/gen-designpartner-fixtures.py`), and update BOTH engines
in one PR.

Product frame (locked): `tryaii designpartner` enrolls a user in tryaii's
design-partner program through ONE resumable command — no verbs. The
user's coding agent interviews them from a data-driven questionnaire,
walks them through a diagnose run when their consent tier needs one,
presents the consent tiers VERBATIM, and only after an explicit
`--confirm` does anything leave the machine. The submission is ALWAYS
written locally before any network attempt.

---

## §1 Cross-language ground rules

1.1 All rules of `shared/cachelint/SPEC.md` §1 apply verbatim — the
    integral-float rule (both engines pass emitted documents through the
    cachelint number normalizer), code-point semantics, and contractual
    key ORDER (the Node conformance suite pins it via `JSON.stringify`).

1.2 The engine is clock-free: `now` (ISO-8601 string), `stamp` (submission
    id), and `version` arrive via opts. The CLI supplies real values
    (UTC `%Y-%m-%dT%H:%M:%SZ` / `%Y%m%dT%H%M%SZ`); fixtures pin them via
    the documented `--now` / `--stamp` flags.

1.3 All files are written with `\n` newlines, `indent=2`, trailing
    newline — byte-identical across SDKs (the cross-CLI parity suite
    compares every written file).

## §2 State machine

State lives in `<out_dir>/state.json` (default out_dir
`.tryaii/designpartner`). Stage is DERIVED from state facts plus one
external fact — whether a diagnose run exists — never trusted from a
stored stage label alone (the stored `stage` field is informational):

```
answers is null                                        -> questionnaire
consent is null                                        -> consent
tier.requires_diagnose and no diagnose run exists      -> diagnose
submission is null                                     -> confirm
otherwise                                              -> submitted
```

- "A diagnose run exists" = `tryaii.diagnose.store.latest_run_id`
  (the sibling `.tryaii/diagnose` store, overridable in the engine for
  tests) returns a run id.
- Entering `confirm` REGENERATES `preview.json` from the current answers
  and the latest diagnose run on EVERY invocation — the preview can never
  be stale when shown. `--confirm` still guards: the preview's recorded
  `consent.diagnose_run_id` and its `answers` must match the current
  state, else usage error ("preview is stale — run 'tryaii designpartner'
  to regenerate it").
- Inputs are ingested before derivation: `--answers` validates and (only
  when problem-free) stores canonical answers; `--consent <tier>` stores
  the tier + `chosen_at` + the latest diagnose run id (null if none);
  `--confirm` builds + saves + sends the submission; `--reset` deletes
  `state.json` and `preview.json` (submission records are kept).
- At most ONE action input per invocation (CLI usage error otherwise).
- Choosing a new `--consent` tier after a submission exists starts a new
  consent cycle: the stored `submission` block is cleared from state (the
  submission FILES remain), returning the flow to confirm.

## §3 Questionnaire: condition grammar + validation

The catalog is DATA (`questions.json`, schema
`tryaii.designpartner.questions/1`): ordered `sections[]`, each with
ordered `questions[]` of `type` `text` | `select` | `multi_select` |
`boolean`; `required` bool; optional `format: "email"` (text only);
`select`/`multi_select` carry `options[]` of `{id, label}`.

### §3.1 ask_if

```
"ask_if": {"question": "<qid>", "op": "answered" | "equals" | "contains", "value"?: ...}
```

- At most ONE condition per question; no and/or.
- The referenced question must appear EARLIER in catalog order (asserted
  by a unit test over the shipped catalog; evaluation is a single forward
  pass).
- `answered`: the referenced question has an answer present (no `value`).
- `equals`: answer == value (string for text/select, JSON bool for
  boolean).
- `contains`: value (an option id string) is an element of the referenced
  multi_select answer.
- A missing/false condition makes the question NOT APPLICABLE: it is
  never required, and a supplied answer for it is DROPPED from the
  canonical answers with a `not_applicable` warning.

### §3.2 validate_answers

Input: a flat `{qid: answer}` object. Output (field order):
`{"answers": <canonical>, "problems": [...], "warnings": [...]}`.

- Canonical answers: applicable answered questions only, in CATALOG
  order.
- ALL problems are reported at once, each
  `{question, code, message}`, in catalog order with `unknown_question`
  entries last (input order). Codes:
  - `missing` — required + applicable + no answer
  - `invalid_type` — text/select answer not a string; multi_select not an
    array of strings; boolean not a JSON bool
  - `unknown_option` — select answer (or a multi_select element) not an
    option id
  - `invalid_email` — `format: "email"` value without the shape
    `<non-empty>@<non-empty>` (exactly one `@` NOT required; rule:
    contains at least one `@` with non-empty text on both sides)
  - `unknown_question` — answer key not in the catalog (the answer is
    dropped)
- Warnings: `{question, code: "not_applicable", message}` for dropped
  condition-skipped answers.
- Answers are stored in state ONLY when `problems` is empty.

## §4 Documents (field order contractual)

### state.json — `tryaii.designpartner.state/1`

```
schema, created_at, updated_at, tool {name, version}, stage,
answers      null | {qid: answer}                (canonical)
consent      null | {tier, chosen_at, diagnose_run_id (null|str)}
submission   null | {stamp, submitted_at, delivered, url, path}
```

`path` and every printed path use forward slashes.

### submission — `tryaii.designpartner.submission/1`

```
schema         "tryaii.designpartner.submission/1"
submitted_at   iso8601 | null (preview)
tool           {name: "tryaii", version}
consent        {tier, chosen_at, confirmed_at (null in preview)}
answers        canonical {qid: answer}
diagnose       null                                   (contact_only)
               | {run_id, summary}                    (summary_insights)
               | {run_id, summary, findings, inventory} (full_partnership)
```

`preview.json` is the submission document with `submitted_at: null` and
`consent.confirmed_at: null` — exactly what will be sent modulo those two
timestamps. `submission-<stamp>.json` is written BEFORE any network
attempt, always.

### status report — `tryaii.designpartner.status/1`

The single source for both the human rendering and `--json`
(field order): `schema, stage, updated_at, tool, action, next`, then the
stage block(s): `questionnaire` (questionnaire stage: the full catalog
sections + `applicable` + `required` id lists + `answers` so far),
`consent` (consent/diagnose stages: `tiers` verbatim from the catalog +
`chosen` + `requires_diagnose_unmet` bool), `preview` (confirm stage:
`{path, tier, includes, answers_count, diagnose_run_id}`), `submission`
(submitted stage: `{path, delivered, url, submitted_at}`).

`action` = what THIS invocation did:
`{type, ...}` with `type` one of `enrolled` (first run; carries
`installed: [{path, action: written|up_to_date}]`), `answers_saved`,
`answers_rejected` (carries `problems` + `warnings`), `consent_chosen`,
`submitted` (carries `delivered`), `reset`, `status` (no action input).
`next` = `{description, command}` — exactly what the agent should do
next.

## §5 Transport

- Default URL `https://designpartners.tryaii.com/api`; effective URL =
  env `TRYAII_DESIGNPARTNER_URL` (non-empty) else the default. Read
  identically in both SDKs.
- POST, body = the exact bytes of the saved `submission-<stamp>.json`,
  headers `Content-Type: application/json` and
  `User-Agent: tryaii/<version>`. Timeout 10s. ONE attempt, no retry.
- Python: stdlib `urllib.request` (httpx is NOT a dependency of the base
  install and must not become one). Node: native `fetch` with
  `AbortSignal.timeout`.
- ANY exception and any non-2xx status collapse into ONE outcome:
  `delivered: false`. No OS/HTTP detail is ever printed — the failure
  line is byte-identical across CLIs and platforms:
  `could not reach <url> — submission saved locally at <path>`.
  Success line: `delivered to <url>`. Both exit 0.

## §6 CLI surface

```
tryaii designpartner [--answers <file|->] [--consent <tier>] [--confirm]
                     [--reset] [--json] [--out-dir <dir>] [--no-gitignore]
                     [--now <iso8601>] [--stamp <id>]
```

- First run (no state.json) installs
  `.claude/skills/tryaii-designpartner/SKILL.md`, the AGENTS.md marker
  block (`<!-- tryaii-designpartner:begin/end -->`, independent of the
  diagnose block), and the anchored `/.tryaii/` gitignore line (unless
  `--no-gitignore`), then reports the `enrolled` action.
- Exit codes: 0 = stage reported (INCLUDING rejected answers — the
  program never blocks; agents loop on `action.problems`) · 1 = runtime
  failure · 2 = usage error (unknown tier, more than one action flag,
  malformed `--answers` JSON, `--confirm` with no preview, stale
  preview).
- Human output is a rendering of the status report (paced); `--json`
  prints the report itself. Nothing else goes to stdout.

## §7 Fixtures

`fixtures/` mirrors the diagnose machinery: hand-written `cases.json`
inputs per suite, machine-generated `expected` from the Python reference
via `scripts/gen-designpartner-fixtures.py` (`--suite`, `--check`).
Suites: `catalog` (applicable/required resolution), `validate`,
`payload` (all three tiers), `state` (stage derivation), `cli`
(argv-level in fresh temp cwds; supports `copy_from_corpus`, `pre_argv`,
`stdin_file`, and — new with this area — `env`, an object merged into the
subprocess environment in the generator AND both parity drivers; the
save-local network case uses `TRYAII_DESIGNPARTNER_URL=
http://127.0.0.1:1/api`, an instantly-refused address on every platform).
All cli cases pin `--now`/`--stamp`. The cross-CLI parity suite
byte-compares stdout/stderr/exit codes and EVERY written file (state,
preview, submissions, installed skill files).
