# Manual test on a fresh machine

Use a machine (or a new OS user account) that has Claude Code installed and some Claude Code history, but no `tryaii` CLI and no TryAii plugin.

## 0. Validate the files

```bash
claude plugin validate .
```

Expect `Validation passed` (a warning is OK only if you understand it).

## 1. Install from the marketplace

Before the repo is public, test from a local copy: `claude plugin marketplace add ./path/to/claude-code-plugin`. After it is public:

```bash
claude plugin marketplace add Tryaii-Admin/tryaii-claude-code
claude plugin install tryaii@tryaii
claude plugin list          # tryaii@tryaii, Status: enabled
claude plugin details tryaii  # Skills (2): tryaii-connect, tryaii-report; Hooks: SessionEnd
```

Alternative for quick iteration, without installing: `claude --plugin-dir ./path/to/claude-code-plugin`.

## 2. Triggering

Start `claude` in any folder. In three separate new sessions, type:

- `connect TryAii`
- `how much does my Claude Code cost`
- `show my Claude Code usage`

Each should load `tryaii-connect` or `tryaii-report`. Also check that `/tryaii:tryaii-connect` and `/tryaii:tryaii-report` appear in the `/` menu.

## 3. Connect flow (`/tryaii:tryaii-connect`)

Check each point:

1. Claude runs `tryaii --version`, finds nothing, checks for Python, and **asks before** running `pip install -U tryaii`.
   - Also test once with no Python: Claude must stop and point to python.org.
2. `tryaii login` opens the browser; Claude waits and continues after "signed in".
3. `tryaii claude-code connect --dry-run` runs; Claude explains counts, date range and fields in plain words, and says prompts and code are not included.
4. Claude asks one yes/no question. **Answer "no"**: Claude stops and says nothing was uploaded. Confirm on the TryAii side that no data arrived.
5. Run again and answer "yes": Claude runs `tryaii claude-code connect --yes`, then prints the dashboard link exactly as the CLI printed it. Open it and check the data is there.
6. Claude explains `tryaii claude-code disconnect`.

## 4. Safety checks

- During the whole run, Claude never opens files in `~/.claude/projects/` and never opens or edits `~/.claude/settings.json` itself. Check the tool calls in the transcript, and compare `~/.claude/settings.json` before and after: unchanged when you said no to live telemetry; after a yes, the only difference is the block the dry run showed (made by `tryaii claude-code connect`, with a `settings.json.tryaii-backup-*` copy next to it), and `tryaii claude-code disconnect` restores the file exactly. Test this with a throwaway `CLAUDE_CONFIG_DIR`.
- Force a failure (for example, disconnect from the network before the dry run). Claude must show the error and ask what to do, not retry or skip ahead.
- Check the uploaded records on the TryAii side contain no prompt or code text.

## 5. Report (`/tryaii:tryaii-report`)

Runs `tryaii claude-code report --days 30` and explains total, biggest costs, cache and subagents using only the report's numbers. Try `/tryaii:tryaii-report 7` to check the days argument.

## 6. Session-end hook (`sync`)

Use a throwaway `CLAUDE_CONFIG_DIR` and `TRYAII_DRE_DATA_DIR`.

1. **Not installed:** with `tryaii` not on `PATH`, start and end a session (`/exit`). The session ends at once with no error shown. Repeat on Windows with and without Git Bash.
2. **Not connected:** with `tryaii` installed but not connected, end a session. Nothing is shown; `tryaii claude-code sync` (no `--quiet`) says live telemetry is not connected and exits 0.
3. **Connected:** after both parts of `connect`, work in a project for a few minutes (use one subagent, for example Explore), then end the session. Within a minute `<data dir>/claude-code/sync.json` has `last_ok_at` set and `applied` ids. On the TryAii dashboard the live calls of that session show the project folder name and the subagent type; after `tryaii claude-code analyze`, also the task type and the recommended model.
4. **What is sent:** run `TRYAII_HTTP_FIXTURE_LOG=... tryaii claude-code sync` against a fixture, or watch the request in a proxy. Each call has only: event id, time, project folder name (no path), subagent type, task type, recommended model and its cost estimate, taxonomy version. The per-process advice has counts, shares and the recommended model. Never prompt text, code, file paths or the session id.
5. **Interval:** end two sessions within 5 minutes. The second run does nothing (`tryaii claude-code sync` says when the next run is possible).
6. `tryaii claude-code disconnect` removes `sync.json`; the next session end does nothing.

## 7. Manual copy path

Remove the plugin (`claude plugin uninstall tryaii@tryaii`), copy `skills/tryaii-connect/SKILL.md` to `~/.claude/skills/tryaii-connect/SKILL.md`, start a new session, and check `/tryaii-connect` works the same.

## 8. Undo

```bash
tryaii claude-code disconnect
claude plugin uninstall tryaii@tryaii
claude plugin marketplace remove tryaii
```

Check the TryAii dashboard shows the machine as disconnected and `claude plugin list` no longer lists the plugin.
