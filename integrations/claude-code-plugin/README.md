# TryAii for Claude Code

Connect your Claude Code usage to [TryAii](https://github.com/Tryaii-Admin) in one step, and get a plain-language report of what Claude Code costs you.

This repository is both a Claude Code **plugin** and a one-plugin **marketplace**, so you can install it straight from GitHub.

## What it does

The plugin adds two skills and one hook. Claude can pick the skills up on its own when you ask in plain words, or you can run them as slash commands.

| Skill | Slash command (plugin install) | Triggers on | What it does |
| --- | --- | --- | --- |
| `tryaii-connect` | `/tryaii:tryaii-connect [days]` | "connect TryAii", "how much does my Claude Code cost", "show my Claude Code usage" | Checks for the `tryaii` CLI (installs it with pip if you agree), signs you in through the browser, shows a dry run of exactly what would be uploaded, asks you to confirm, connects, and prints your dashboard link. |
| `tryaii-report` | `/tryaii:tryaii-report [days]` | "Claude Code cost report", "where are my tokens going" | Runs `tryaii claude-code report --days 30` locally and explains the biggest costs, cache use and subagent spend in plain words. Uploads nothing. |

Both skills only run `tryaii` commands:

```text
tryaii --version
tryaii login
tryaii claude-code report [--days N]               # local only
tryaii claude-code connect --dry-run [--days N]    # shows what would be uploaded, uploads nothing
tryaii claude-code connect [--days N] --yes        # imports and uploads metadata
tryaii claude-code disconnect
```

### Session-end hook (live advice)

When a Claude Code session ends, the plugin's `SessionEnd` hook (`hooks/hooks.json`) runs:

```text
tryaii claude-code sync --background --quiet
```

Live telemetry tells TryAii the model, tokens and cost of each call, but not which project or which subagent it belongs to. `sync` reads the session logs on your machine and sends, for each call made since live telemetry was turned on, the project folder name and the subagent type, plus the task type and recommended model if you ran `tryaii claude-code analyze`. It starts in the background, prints nothing, runs at most once every 5 minutes, and does nothing until both parts of `connect` are done. The hook always exits 0: it never blocks or slows the end of a session, also when `tryaii` is not installed. Without the plugin you can run `tryaii claude-code sync` yourself now and then.

## What is sent, and what is not

- **Sent (only after you confirm the dry run):** usage metadata: model names, token counts, cache counts, cost estimates, timestamps, the project folder name (its base name only, never a path), the subagent type, and, if you ran `analyze`, the task type and the recommended model. The dry run lists the exact fields and counts before anything leaves your machine. No session id is stored.
- **Never sent:** your prompts, Claude's replies, conversation text, file contents, code or full paths. They never leave your machine. `sync` checks every request for text fields before sending it.
- The skills tell Claude never to open your transcript files or `~/.claude/settings.json`, never to edit Claude Code settings themselves (only the `tryaii` CLI adds the live telemetry settings, after you say yes to that part), and to stop and ask you if any command fails.
- `tryaii-report` is fully local.

## Requirements

- Claude Code with plugin support.
- Python 3 with pip. The Claude Code import is in the Python package only for now (`pip install -U tryaii`). There is also an npm package (`npm install -g tryaii`), but it does not do the Claude Code import yet.

## Install

### Option A: plugin from the marketplace (recommended)

Inside a Claude Code session:

```text
/plugin marketplace add Tryaii-Admin/tryaii-claude-code
/plugin install tryaii@tryaii
```

Or from your shell, without opening a session:

```bash
claude plugin marketplace add Tryaii-Admin/tryaii-claude-code
claude plugin install tryaii@tryaii
```

Then start a new session (or run `/reload-plugins`) and say "connect TryAii", or run `/tryaii:tryaii-connect`.

### Option B: copy the skill by hand

If you only want the connect skill and no plugin, copy `SKILL.md` into your personal skills folder:

macOS / Linux:

```bash
mkdir -p ~/.claude/skills/tryaii-connect
curl -fsSL https://raw.githubusercontent.com/Tryaii-Admin/tryaii-claude-code/main/skills/tryaii-connect/SKILL.md \
  -o ~/.claude/skills/tryaii-connect/SKILL.md
```

Windows (PowerShell):

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\skills\tryaii-connect" | Out-Null
Invoke-WebRequest https://raw.githubusercontent.com/Tryaii-Admin/tryaii-claude-code/main/skills/tryaii-connect/SKILL.md `
  -OutFile "$HOME\.claude\skills\tryaii-connect\SKILL.md"
```

Do the same with `skills/tryaii-report/SKILL.md` into `~/.claude/skills/tryaii-report/` if you want the report skill too. Copied skills run as `/tryaii-connect` and `/tryaii-report` (no `tryaii:` prefix).

## Undo

- Disconnect this machine from TryAii: `tryaii claude-code disconnect`
- Remove the plugin: `/plugin uninstall tryaii@tryaii` in a session, or `claude plugin uninstall tryaii@tryaii` in your shell
- Remove the marketplace (also uninstalls its plugins): `claude plugin marketplace remove tryaii`
- Remove hand-copied skills: delete `~/.claude/skills/tryaii-connect/` (and `~/.claude/skills/tryaii-report/`)
- Remove the CLI: `pip uninstall tryaii`

## Repository layout

```text
.claude-plugin/
  plugin.json         # plugin manifest (name "tryaii")
  marketplace.json    # marketplace manifest (name "tryaii"); its one entry points at "." (this repo root)
hooks/
  hooks.json          # SessionEnd: tryaii claude-code sync --background --quiet
skills/
  tryaii-connect/SKILL.md
  tryaii-report/SKILL.md
README.md
TESTING.md
```

The repo root is both the marketplace root and the plugin root. The marketplace entry uses `"source": "."`, which the marketplace reference documents as "the root itself".

## Validate

```bash
claude plugin validate .
```

## Docs used

Checked on 2026-10-21 against Claude Code 2.1.304:

- Skills (SKILL.md frontmatter, personal skills folder, commands merged into skills): https://code.claude.com/docs/en/skills
- Plugins overview (manifest location, components, namespacing): https://code.claude.com/docs/en/plugins
- Plugin manifest reference (`plugin.json` fields, standard layout): https://code.claude.com/docs/en/plugins/manifest-reference
- Create a marketplace (served at the old URL https://code.claude.com/docs/en/plugin-marketplaces): https://code.claude.com/docs/en/plugins/create-marketplace
- Marketplace reference (`marketplace.json` fields, `"."` source, reserved names): https://code.claude.com/docs/en/plugins/marketplace-reference
- Install and manage plugins (add, install, uninstall commands): https://code.claude.com/docs/en/plugins/install
- Hooks (plugin `hooks/hooks.json` format, `SessionEnd`, shell on Windows, `async`): https://code.claude.com/docs/en/hooks

Notes on the docs:

- The skills page does not give a length limit or a required character set for a skill `name`. We use lowercase with hyphens to be safe.
- `SessionEnd` hooks cannot block the end of a session and get a short time budget (1.5 s by default; a plugin's own `timeout` does not raise it). The hook is `async`, so Claude Code does not wait for it, and `sync --background` detaches its work at once. The command is `tryaii ...; exit 0`, which runs the same in Git Bash, `sh` and PowerShell (Claude Code's shell on Windows without Git Bash).
- The docs say slash commands have been merged into skills. A file in `commands/` still works, but new plugins should use `skills/`, so this plugin ships skills only. Each skill is also a slash command.
