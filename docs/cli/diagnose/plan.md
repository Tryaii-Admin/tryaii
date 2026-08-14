# `tryaii diagnose plan` — the check catalog + interview for the agent

Prints what diagnose can check, the interview questions the agent should ask
the user, and the inventory shape the agent must produce.

```bash
tryaii diagnose plan          # human rendering
tryaii diagnose plan --json   # the machine-readable plan — agents consume this
```

`--json` emits the bundled `shared/diagnose/plan.json` **verbatim** (schema
`tryaii.diagnose.plan/1`) — cross-SDK parity by construction. It contains:

- `checks[]` — id, title, what it does, which inventory fields it `needs`
  and is `better_with`
- `interview[]` — the questions (which checks · scope · quality/cost/speed
  1–5 · traffic · goal), with types and defaults
- `inventory` — required fields per site (`file`, `line`, `prompt`),
  optional ones (`id`, `provider`, `model`, `calls_per_day`,
  `output_tokens`, `notes`), authoring notes, and one complete example
- `commands` — copy-paste `check` / `report` invocations

The human rendering is derived purely from the same JSON, so both CLIs print
identical bytes in both modes.

Exit codes: 0 success · 2 usage error.
