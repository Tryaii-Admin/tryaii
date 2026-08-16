# `tryaii diagnose init` — install the agent playbook

Writes the pieces a coding agent needs to run diagnose end to end:

| File | What |
|---|---|
| `.claude/skills/tryaii-diagnose/SKILL.md` | The playbook: interview → discovery → inventory → check → report. A tryaii-owned file — always safe to overwrite (upgrades replace it). |
| `AGENTS.md` | A short pointer block between `<!-- tryaii-diagnose:begin/end -->` markers, so non-Claude agents find the skill too. Replaced in place on upgrades; created if the file is missing; **everything outside the markers is never touched**. |
| `.gitignore` | An **anchored** `/.tryaii/` entry so run data stays untracked (skipped with `--no-gitignore`). |

```bash
tryaii diagnose init
tryaii diagnose init --dir ../my-app --no-gitignore
```

| Flag | Default | Effect |
|---|---|---|
| `--dir <path>` | `.` | Target repo root |
| `--no-gitignore` | off | Do not touch `.gitignore` |

Idempotent: unchanged targets print `ok <path> (up to date)`; written ones
print `-> <path>`. All writes are LF-normalized. The masters live in
`shared/diagnose/skill/` and ship packed inside both packages
(`diagnose/data/skill.json`).

Exit codes: 0 success · 1 runtime failure · 2 usage error.
