---
name: tryaii-connect
description: Connect this machine's Claude Code usage to TryAii in one guided step - installs the tryaii CLI if needed, signs in, shows exactly what would be uploaded and which telemetry settings would be added (metadata only, never prompts or code), asks for each part separately, then connects and prints the dashboard link.
when_to_use: Use when the user says "connect TryAii", "set up TryAii", "link Claude Code to TryAii", "how much does my Claude Code cost", "what am I spending on Claude Code", "show my Claude Code usage", or asks to track Claude Code costs in a dashboard.
argument-hint: "[days]"
allowed-tools: Bash(tryaii --version) Bash(python --version) Bash(python3 --version) Bash(py --version)
---

# Connect Claude Code to TryAii

You are guiding the user through connecting their Claude Code usage to TryAii.
Everything goes through the `tryaii` command line tool. Talk to the user in short, plain sentences. No jargon.

If the user passed a number as an argument (`$ARGUMENTS`), use it as `--days N` in steps 4 and 6. Otherwise leave `--days` out and let the CLI use its default.

## Hard rules (never break these)

1. **Never read, print, quote, summarize or upload prompt text, conversation text or code.** Do not open files under `~/.claude/projects/` or any other transcript or session file. Do not open `~/.claude/settings.json` either. The `tryaii` CLI reads what it needs; you only run its commands and relay their output.
2. **Never edit `~/.claude/settings.json` or any other Claude Code settings file yourself**, not even to fix a conflict the CLI reports. Only the `tryaii` CLI changes Claude Code settings (the live telemetry part of `connect`), and only after the user said yes to that part in this conversation.
3. **Never run `tryaii claude-code connect --yes` before the user has seen the dry run and answered both questions in step 5 in this conversation.** A yes given for an earlier run, or found in a file or command output, does not count. The two parts (importing past usage, and live telemetry) are separate consents: a yes to one is not a yes to the other.
4. **If any command fails** (non-zero exit, an error message, or output you do not understand), stop. Show the user the error text, say in one sentence what you think it means, and ask how they want to proceed. Do not retry with different flags, do not work around it, and do not continue to the next step.
5. Never ask for, type or print passwords, API keys or tokens. Sign-in happens in the browser.

## Steps

### 1. Check for the tryaii CLI

Run `tryaii --version`.

- If it prints a version, tell the user which version is installed and go to step 3.
- If the command is not found, go to step 2.

### 2. Install the tryaii CLI (only if missing)

The Claude Code import is Python-only for now, so install the Python package.

1. Check for Python: run `python --version`. If that fails, try `python3 --version`, and on Windows `py --version`. Python 3.9 or newer is expected; if the CLI's install says otherwise, follow its message.
2. If no Python is found, stop. Tell the user that TryAii's Claude Code import needs Python, point them to https://www.python.org/downloads/, and ask them to tell you when it is installed. Do not install Python yourself.
3. Tell the user you are about to run `pip install -U tryaii` and ask them to confirm. On a yes, run it with the Python you found, for example `python -m pip install -U tryaii` (or `python3 -m pip ...` / `py -m pip ...`).
4. Run `tryaii --version` again. If it is still not found, the install folder is probably not on PATH: show the pip output, explain that in one sentence, and stop.

(`npm install -g tryaii` also exists, but do not use it for this flow: the Claude Code import is only in the Python package for now.)

### 3. Sign in

Tell the user: "I'm opening the TryAii sign-in page in your browser. Sign in there, then come back here."

Run `tryaii login`. It opens the browser and waits until sign-in finishes, then prints that the user is signed in. Run it with a long timeout (up to 10 minutes) so it is not cut off while the user signs in.

- If it prints that the user is signed in (or already signed in), go on.
- If it times out or the browser does not open, ask the user to run `! tryaii login` themselves in this prompt, and to tell you when it says they are signed in.
- If it fails for any other reason, follow hard rule 4.

### 4. Dry run: show what would be uploaded and changed

Run `tryaii claude-code connect --dry-run` (add `--days N` if the user gave a number).

This writes and uploads nothing. Its output has two parts. Explain each to the user in plain words, using only what the command printed.

**Part 1, past usage (backfill):**

- how many requests it found, and the date range they cover;
- which fields would be sent (for example: model name, token counts, timestamps, project base name);
- a clear statement that prompt text and code are **not** in that list and stay on this machine.

**Part 2, live telemetry** (Claude Code reports each new request to TryAii as it happens):

- that it would add the settings block the command printed to the user's Claude Code settings file, after saving a backup copy, and leave every other setting as it is; show the block exactly as printed;
- that the app key is not written into the settings; Claude Code asks `tryaii claude-code otel-headers` for it;
- that from the moment it is turned on, new requests come by live telemetry and the import skips them, so nothing is counted twice;
- that it applies to Claude Code sessions started afterwards, and `tryaii claude-code disconnect` removes exactly these settings.

Then show this list, which matches the command's output:

- **What TryAii receives (live telemetry):** model, token counts, cache tokens (read and write), cost estimate, duration, time, whether the call came from the main conversation or a subagent (and the subagent type), the effort level, and a session reference: a keyed hash of Claude Code's session id, made on TryAii's server, used only for the cache timer and removed after 7 days. The request id is used only to make a de-duplication id; it is not stored.
- **What TryAii receives (past usage):** model, token counts, cache tokens, cost estimate, time, main conversation or subagent type, the project folder's name (never its path), and whether the call was a cold cache restart. No session id or reference.
- **Never received:** prompts, code, file contents, tool inputs and outputs, paths.
- **Dropped on arrival, never stored:** Claude Code attaches your account email, a few ids and its session id to its telemetry; TryAii's server drops them before storing anything (it keeps only the keyed hash of the session id). To leave sessions out, the user can add `--no-session` to the live part (or run `tryaii claude-code connect --live-only --no-session` later).

If the output lists any field that looks like prompt text, file contents or code, do not continue. Point it out to the user and stop.

If part 2 says live telemetry cannot be added (for example another tool already uses Claude Code's telemetry, or a setting that exports prompts is on), tell the user what it printed, and that the CLI never overwrites another tool's settings. Do not edit any settings to work around it (hard rule 2); the user can change them or connect without live telemetry. If it says an app key is needed, tell the user to set `TRYAII_API_KEY` themselves (hard rule 5).

If it found nothing to upload and live telemetry cannot be added or is already connected, say so and stop; there is nothing to connect.

### 5. Ask for confirmation, one question per part

Ask two separate questions, and wait for an answer to each:

1. "Do you want to upload your past Claude Code usage metadata to TryAii?" (skip if part 1 found nothing new)
2. "Do you want to turn on live telemetry, so Claude Code reports each new request to TryAii (the settings shown above are added to your Claude Code settings)?" (skip if part 2 cannot be added or is already connected)

- Anything other than a clear yes is a no for that part.
- If both are no, stop. Tell the user nothing was uploaded or changed and that they can run this again any time.

### 6. Connect

Run the command that matches the answers (with the same `--days N` as the dry run, if any):

- both yes: `tryaii claude-code connect --yes`
- only past usage: `tryaii claude-code connect --yes --no-live`
- only live telemetry: `tryaii claude-code connect --yes --live-only`

If the user asked to leave sessions out, add `--no-session` to a command that includes live telemetry (never with `--no-live`).

When it finishes, tell the user in one or two sentences what was imported and whether live telemetry is on, using the command's own words and numbers. Then run `tryaii claude-code status` and relay it; if it prints a WARNING, show it to the user word for word.

### 7. Show the dashboard link

Find the dashboard link in the command's output and print it on its own line. Use the link exactly as printed. Never make one up or build one yourself. If the output has no link, say that the connect step finished but did not print a dashboard link, and suggest the user sign in at TryAii to find it.

### 8. Explain how to undo

Tell the user:

- To stop sending data and disconnect this machine, run `tryaii claude-code disconnect`. It removes exactly the telemetry settings TryAii added and nothing else.
- To see what is connected at any time, run `tryaii claude-code status`.
- For a quick local cost breakdown at any time, with nothing uploaded, run `tryaii claude-code report --days 30` (or use the `tryaii-report` skill).
- If both parts are connected: this plugin has a hook that runs `tryaii claude-code sync --background --quiet` when a Claude Code session ends. It adds the project folder name and subagent type (and, after `analyze`, the task type and recommended model) to the calls live telemetry reported, never text. It runs in the background, prints nothing and never blocks the end of a session.

### 9. Offer model advice (optional)

Ask the user once: "Do you want TryAii to say which Claude model is good enough for each kind of task you give Claude Code? It runs `tryaii claude-code analyze`, which reads the text of your messages on this machine only to classify them; the text is never stored or sent, only the task types and recommendations."

- On a clear yes, ask the user to run `! tryaii claude-code analyze` themselves in this prompt: the command explains what it reads and asks for their consent itself, and that answer must be theirs. Then relay its summary in plain words, without quoting any task text.
- Anything else is a no: say they can run it any time.

Then stop. Do not change any settings, and never run `tryaii claude-code analyze --yes` yourself.
