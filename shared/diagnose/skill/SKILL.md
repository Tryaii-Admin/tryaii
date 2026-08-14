---
name: tryaii-diagnose
description: Diagnose this codebase's LLM call sites with tryaii — model fit, cache readiness, cost exposure, prompt hygiene. Use when the user asks to diagnose/audit/review their LLM usage, prompts, model choices, or LLM costs, or mentions tryaii diagnose.
---

# tryaii diagnose — codebase LLM diagnostics

You are the discovery and interview layer; the `tryaii` CLI is the
measurement layer. You find the LLM call sites and write an inventory
JSON; `tryaii diagnose check` runs deterministic checks over it and
`tryaii diagnose report` renders a local HTML report. The tool is
insight-only: it never edits code and never sends anything anywhere.

## Step 1 — get the plan and interview the user

Run:

    tryaii diagnose plan --json

It returns the check catalog, the interview questions, and the exact
inventory shape (with a complete example). Ask the user the interview
questions conversationally — which checks (recommend all four), scope
(whole repo / paths / current diff), quality/cost/speed priorities (1–5),
rough traffic (calls per day), and their main goal (cheaper? faster?
better?). Do not skip the interview; the answers become CLI flags.

## Step 2 — discover the LLM call sites

Find every place in scope where the code calls an LLM. Any language
counts. Useful starting points (adapt freely):

- imports/requires of provider SDKs: `openai`, `anthropic`, `google
  genai`, `groq`, `mistral`, `cohere`, `litellm`, `langchain`, Vercel
  `ai`, `openrouter`, `tryaii`
- call shapes: `chat.completions`, `messages.create`,
  `generateContent`, `responses.create`, raw HTTP to provider endpoints
- model-name string literals (`gpt-`, `claude-`, `gemini-`, `grok-`,
  `deepseek-`, `mistral-`)

For a large repo, consider fanning out subagents (for example one per
top-level directory, or one for discovery and one for prompt extraction)
— your call; a small repo needs no fan-out.

For each call site capture: `file`, `line`, the prompt (the rendered
string, or `{system, messages: [{role, content}]}` — if you can only see
the template, include it with its `{slots}` intact; the detectors flag
unrendered slots, which is signal, not failure), and where visible the
`provider`, `model`, and per-site traffic.

## Step 3 — write inventory.json

Match the shape from `plan --json`. Honesty rules:

- Give each site a stable `id` (e.g. `summarize-ticket`) so future runs
  can show deltas even when line numbers move.
- Omit what you do not know. NEVER invent traffic numbers — ask the user
  or leave them out (the checks degrade honestly per site).
- Do not paraphrase prompts; copy them.

## Step 4 — run the checks and the report

    tryaii diagnose check inventory.json --quality <q> --cost <c> --speed <s> [--calls-per-day <n>] [--goal "<goal>"]
    tryaii diagnose report

`check` writes `.tryaii/diagnose/<run-id>/` (inventory, findings.json,
meta) and prints a summary; `report` renders `index.html` next to it.
Open the HTML for the user (or tell them the path). Everything is local.

Note: if `check` says setup is required, run `tryaii setup` once (it
downloads the local embedding model — may take a minute) and retry.

## Step 5 — walk the results

Present the summary honestly: healthy sites are green checks — say so;
findings get the detail. For each finding offer the concrete next step
(swap the model, move the dynamic value out of the prompt prefix, add a
system block...), and offer to apply fixes — that is you editing code
with the user's approval, never the tool. After changes, re-run
`check` + `report`: the new report shows deltas against the previous run.
