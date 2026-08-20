---
name: tryaii-designpartner
description: Enroll the user in tryaii's design-partner program — questionnaire interview, optional diagnose run, tiered data-sharing consent, submission. Use when the user asks to become a tryaii design partner, join the partner program, or mentions tryaii designpartner.
---

# tryaii designpartner — enrollment playbook

One resumable command drives everything: `tryaii designpartner`. You are
the interviewer and presenter; the CLI owns the state, the validation,
the consent record, and the submission. Nothing is ever sent without an
explicit `--confirm`, and every submission is saved locally first.

## Rule zero — always start from state

Run:

    tryaii designpartner --json

and obey `stage` and `next`. The flow is resumable: never assume where
the process is, never skip ahead. Stages: questionnaire → (diagnose) →
consent → confirm → submitted.

## Stage: questionnaire

The `questionnaire` block in the JSON is the catalog: sections of typed
questions, some gated by `ask_if` conditions (only ask a question whose
condition is met — e.g. per-provider model questions appear only for
providers the user selected). Interview conversationally, one topic at a
time; keep it light.

Honesty rules:
- EVERY answer comes from the user. Never invent, infer, or autofill —
  especially not name, email, company, spend, or call volume.
- Use option ids (not labels) for select/multi_select answers; JSON
  true/false for booleans.
- The user may skip any non-required question — omit it.

Write the answers as a flat `{question_id: answer}` object to
`answers.json` and run:

    tryaii designpartner --answers answers.json

If `action.problems` comes back, ask the user ONLY about the flagged
questions and resubmit. Exit code stays 0 — loop until clean.

## Stage: diagnose

Recommend a diagnose run to every participant (it makes the partnership
far more useful); it is REQUIRED for the summary_insights and
full_partnership tiers. Follow the tryaii-diagnose skill
(`.claude/skills/tryaii-diagnose/SKILL.md`; run `tryaii diagnose init`
if it is not installed). When the run exists, run
`tryaii designpartner` again — the stage advances by itself.

## Stage: consent

Present ALL THREE tiers to the user using each tier's `copy` field
VERBATIM — no paraphrasing, no summarizing, no softening. In particular,
full_partnership's disclosure that their raw prompts and file paths will
be sent must reach the user word for word. The user chooses explicitly;
you never choose for them. Then:

    tryaii designpartner --consent <tier_id>

## Stage: confirm

The CLI writes `preview.json` — exactly what will be sent. Tell the user
where it is, summarize the `includes` list, and ask whether to send.
Never edit preview.json. Only after the user explicitly says to send:

    tryaii designpartner --confirm

## Stage: submitted

Report the outcome honestly: `delivered` means it reached the endpoint;
otherwise it was saved locally (tell the user the path — the tryaii team
can collect it later). Never re-POST a saved submission yourself.
`--reset` starts the flow over (submission records are kept).
