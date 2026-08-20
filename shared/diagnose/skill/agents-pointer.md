<!-- tryaii-diagnose:begin -->
## LLM diagnostics (tryaii diagnose)

This repo has `tryaii diagnose` set up: an agent-driven audit of the
codebase's LLM call sites (model fit, cache readiness, cost exposure,
prompt hygiene). The playbook lives in
`.claude/skills/tryaii-diagnose/SKILL.md` — follow it when asked to
diagnose/audit LLM usage. Quick path: `tryaii diagnose plan --json`,
write an inventory of call sites, `tryaii diagnose check inventory.json`,
`tryaii diagnose report`. Insight-only; everything stays local.
<!-- tryaii-diagnose:end -->
