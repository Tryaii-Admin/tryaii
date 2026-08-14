# `tryaii diagnose` — agent-first codebase LLM diagnostics

Analyze a codebase's LLM call sites: is each call using the right model, will
its prompt hit the provider's cache, what does it cost per month, and is the
prompt structured well. **Agent-first**: your coding agent (Claude Code etc.)
interviews you, finds the call sites in any language, and writes an inventory
JSON; `tryaii` runs deterministic checks over it and renders a local HTML
report. **Insight-only** — it never edits code and never sends anything
anywhere.

```bash
tryaii diagnose init                     # once per repo: install the agent playbook
# ... your agent: interview → discovery → inventory.json ...
tryaii diagnose check inventory.json --quality 3 --cost 4 --speed 2 --calls-per-day 1000
tryaii diagnose report                   # open .tryaii/diagnose/<run-id>/index.html
```

| Verb | Purpose |
|---|---|
| [`init`](init.md) | Install the playbook: `.claude/skills/tryaii-diagnose/SKILL.md`, an AGENTS.md pointer block, an anchored `/.tryaii/` gitignore entry |
| [`plan`](plan.md) | The check catalog + interview questions + inventory shape for the agent (`--json` = machine-readable, with a full example) |
| [`check`](check.md) | Run the checks over an inventory; writes `.tryaii/diagnose/<run-id>/` (inventory, findings.json, meta) + a `latest` pointer |
| [`report`](report.md) | Render a run's findings to a self-contained `index.html`, with deltas vs the previous run |

## The four checks

| Check | Question it answers | Needs per site |
|---|---|---|
| `model_fit` | Where does the current model rank in the full catalog for THIS prompt under your priorities? | prompt (+ model for the comparison) |
| `cache_readiness` | Will this prompt hit the provider's prompt cache — and what blocks it? (the [cachelint](../cachelint.md) engine) | prompt + provider |
| `cost_exposure` | Cost per call and per month, upper-bound cache savings, and a cheaper swap within quality tolerance | prompt + model (+ traffic for monthly) |
| `hygiene` | Dynamic values in the wrong place, dynamic content in the system block, missing structure | prompt |

**Honest degradation**: a site missing data doesn't fail the run — that check
reports `insufficient data` with a reason for that site. Every site appears in
the report; healthy ones get a green check.

## Setup requirement

Live prompt classification (any site without a precomputed `_classification`)
requires `tryaii setup` once per machine — it downloads the local embedding
model and warms centroids, and now records a marker `diagnose check` looks
for. Classification rides the routing daemon, so N sites share one
embedding-model load.

## Storage & privacy

Runs accumulate under `<repo>/.tryaii/diagnose/<run-id>/` (UTC-timestamp ids;
`latest` is a plain pointer file). Raw prompts stay in the local run dir.
`findings.json` also carries a redacted `summary` section (verdict counts,
totals — no code, prompts, or paths), designed as the payload for a future
**opt-in** upload; no upload exists today and nothing is ever sent.

## Exit codes

diagnose warns, it never blocks: findings do not change the exit code.
0 checks completed (findings included) · 1 runtime failure (bad file, no
runs, setup missing) · 2 usage error or invalid JSON.

The behavior contract for everything above is `shared/diagnose/SPEC.md`;
both SDKs' engines conform to frozen golden fixtures and a cross-CLI
byte-parity suite.
