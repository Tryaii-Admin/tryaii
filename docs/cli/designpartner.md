# `tryaii designpartner` — enroll as a tryaii design partner

**One resumable command — no verbs.** Every run reads the enrollment state
(`.tryaii/designpartner/`), ingests whatever you pass, advances, and prints
the current stage plus exactly what to do next (`--json` for agents). Stop
anywhere; the next run resumes where you left off. The
`tryaii-designpartner` skill (installed automatically on the first run,
plus an AGENTS.md pointer block and the anchored `/.tryaii/` gitignore
entry) teaches your coding agent to drive the whole flow.

```
questionnaire ──▶ diagnose (required for insight tiers) ──▶ consent ──▶ confirm ──▶ submitted
```

```bash
tryaii designpartner                          # first run: installs the skill, prints the questionnaire
tryaii designpartner --answers answers.json   # agent-collected answers; validated all-at-once
tryaii designpartner --consent summary_insights
tryaii designpartner --confirm                # the ONLY invocation that sends anything
```

## Flags

| Flag | Effect |
|---|---|
| `--answers <file \| ->` | Validate + save the questionnaire answers (a flat JSON object of `{question_id: answer}`). Problems are reported ALL at once, exit stays 0 — the agent fixes and resubmits. |
| `--consent <tier>` | Choose a data-sharing tier; regenerates `preview.json` (exactly what will be sent). |
| `--confirm` | Send the previewed submission. Guarded: a stale preview is rejected. |
| `--reset` | Clear `state.json` + `preview.json`; `submission-*.json` records are kept. |
| `--json` | Print the machine-readable status report (`tryaii.designpartner.status/1`) — agents consume this. |
| `--out-dir <dir>` | State directory (default `.tryaii/designpartner`) |
| `--no-gitignore` | First run: do not touch `.gitignore` |
| `--now <iso8601>` / `--stamp <id>` | Determinism seams (fixtures/CI) |

At most one of `--answers`/`--consent`/`--confirm`/`--reset` per invocation.

## The questionnaire

Data-driven (`shared/designpartner/questions.json`): three sections —
About you · How you use AI · Where tryaii fits — with typed questions and
explicit `ask_if` conditions (e.g. per-provider model questions appear only
for the providers you selected). The agent interviews conversationally from
the `--json` catalog; every answer comes from the user, never invented.

## Consent tiers (shown to the user verbatim)

| Tier | Sends | Needs a diagnose run |
|---|---|---|
| `contact_only` | questionnaire answers + contact details | no |
| `summary_insights` | + the redacted diagnose summary (`tryaii.diagnose.summary/1`) — **no code, no paths, no prompts** | yes |
| `full_partnership` | + the FULL findings **and** the prompt inventory of the latest diagnose run — file paths, line numbers, model names, **your actual prompts, verbatim** | yes |

The skill instructs the agent to present each tier's consent copy **word
for word** — the full_partnership disclosure of raw prompts and paths is
never paraphrased. The `--consent` step writes `preview.json` (the exact
submission document, timestamps nulled); `--confirm` refuses if it has
gone stale.

## Submission & privacy

- The submission (`tryaii.designpartner.submission/1`) is ALWAYS written to
  `.tryaii/designpartner/submission-<stamp>.json` **before** any network
  attempt.
- POST to `https://designpartners.tryaii.com/api` (override:
  `TRYAII_DESIGNPARTNER_URL`), stdlib HTTP on Python / native fetch on
  Node, 10s timeout, one attempt. If the endpoint cannot be reached, the
  command still succeeds: `could not reach <url> — submission saved
  locally at <path>`.
- Nothing is ever sent without an explicit `--confirm`.

Exit codes: 0 stage reported (including rejected answers — the flow never
blocks) · 1 runtime failure · 2 usage error (unknown tier, conflicting
flags, malformed answers JSON, stale preview).

The behavior contract is `shared/designpartner/SPEC.md`; both SDKs conform
to frozen fixtures and a cross-CLI byte-parity suite that compares every
written file.
