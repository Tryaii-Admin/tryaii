---
name: tryaii-report
description: Show a plain-language report of what Claude Code cost on this machine over the last 30 days - biggest costs, cache use, and subagent advice - using the local tryaii CLI. Nothing is uploaded.
when_to_use: Use when the user asks "show my Claude Code usage", "how much does my Claude Code cost", "where are my Claude Code tokens going", "Claude Code cost report", or wants a local usage breakdown without connecting to TryAii.
argument-hint: "[days]"
allowed-tools: Bash(tryaii --version) Bash(tryaii claude-code report *)
---

# Claude Code cost report (local)

Run the local TryAii report and explain it in plain words. This report runs only on this machine and uploads nothing.

## Hard rules

1. **Never read, print or quote prompt text, conversation text or code.** Do not open files under `~/.claude/projects/`, `~/.claude/settings.json` or any transcript file. Use only what the `tryaii` command prints.
2. **Never edit `~/.claude/settings.json` or any Claude Code settings file.** If a recommendation would need a settings change, describe it and let the user decide; do not make it.
3. **If any command fails, stop.** Show the error, say in one sentence what it likely means, and ask the user how to proceed.

## Steps

1. Run `tryaii --version`. If the command is not found, tell the user the TryAii CLI is not installed and offer to set it up with the `tryaii-connect` skill (or `pip install -U tryaii`). Stop here until they answer.
2. Run `tryaii claude-code report --days 30`. If the user gave a number (`$ARGUMENTS`), use it instead of 30.
3. Explain the result in short, plain sentences, using only numbers from the output:
   - **Total**: the total cost (or tokens) for the period, and the date range.
   - **Biggest costs**: the top 3 items the report lists (models, projects or sessions), with their share of the total.
   - **Cache**: how much of the input was served from cache, and what it saved, if the report shows it. If cache use looks low, say in one sentence that long, repeated context is cheaper when it is cached, and what the report suggests.
   - **Subagents**: how much of the cost came from subagents, if shown. If it is a large share, say so plainly and pass on the report's advice (for example, using a smaller model for simple subagent tasks, or fewer parallel subagents).
4. End with at most three concrete suggestions, each one sentence, taken from or clearly supported by the report. Do not invent numbers, prices or savings the report does not show.
5. If the user is not connected yet, mention in one line that `tryaii-connect` can send this usage metadata (never prompts or code) to a TryAii dashboard, and only if they want.
