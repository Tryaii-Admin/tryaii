# `cache_lint="warn"` — SDK cache lint + runtime verification

Lint every outgoing chat request for prompt-cache readiness **at the moment
it leaves** — after routing, message assembly, and truncation — and verify
the prediction against the provider's actual `usage` response. This is the
SDK seat of the [cachelint engine](../../cli/cachelint.md): the CLI sees what
you paste into it; the hook sees the real rendered payload of every call.

```python
# Python — DREClient, OpenRouterIntegration, and AsyncDREClient all accept it
from tryaii import DREClient

client = DREClient(api_key="sk-or-...", cache_lint="warn")
client.chat("...")   # warnings, if any, print to stderr; the call is never blocked
```

```ts
// Node — DREClient and OpenRouterIntegration
import { DREClient } from 'tryaii';

const client = new DREClient({ apiKey: 'sk-or-...', cacheLint: 'warn' });
```

`TRYAII_CACHE_LINT=warn` enables it without code changes; an explicit
constructor value always wins (`cache_lint="off"` beats the env var).
Default is off.

## What you get

**Pre-flight warnings** — before the request is sent, the assembled messages
run through the cachelint engine (18 detectors, per-model token floors,
stable-prefix computation). A problem prints 1–3 stderr lines:

```
[tryaii cachelint] EFFECTIVELY_UNCACHEABLE for anthropic/claude-fable-5: Dynamic content too early: the stable prefix is only ~5 tok, below the 512 minimum — in practice nothing will cache until it moves.
[tryaii cachelint]   first blocker: uuid at messages[0]:system+16 (0% in)
```

**Runtime verification** — the hook remembers what it predicted per prompt
shape. From the **second** call with the same shape onward (the first call is
the expected cache write), a shape predicted cacheable that reports zero
cached tokens in `usage` warns once:

```
[tryaii cachelint] VERIFY_MISS for google/gemini-2.5-pro: predicted ~2244 tok cacheable prefix, but usage reports 0 cached tokens on repeat call #2 of this prompt shape — a silent invalidator or missing provider support may be the cause
```

## Noise policy

- **Problems only.** Warnings fire for `BELOW_THRESHOLD`,
  `EFFECTIVELY_UNCACHEABLE`, and `UNKNOWN_THRESHOLD` *with* blocking
  findings. Healthy prompts are silent; a bare unknown threshold (a
  knowledge-base gap, e.g. a DeepSeek slug) is silent too.
- **Once per prompt shape, per client instance.** The shape key is a hash of
  (model slug, canonical rendered prompt). Agent loops repeating one prompt
  see one warning, not thousands. Memory is capped (1024 shapes, FIFO) and a
  shape idle for over an hour is treated as fresh. A new client instance
  starts clean — there is no cross-run persistence.
- **Fail-open, absolutely.** A lint failure of any kind — engine error,
  missing tokenizer, broken stderr — never raises and never blocks the API
  call. There is deliberately no "strict" mode.

## Template introspection

The deepest insight tier. A rendered `f"...{DAY_OF_WEEK}..."` reaches the
provider as `"...Monday..."` — invisible to every detector. But the SDK runs
*in your process*: in warn mode it walks the stack to your calling frame,
parses that module's source, and traces the prompt argument back to its
template — so the warning names the exact slot and line:

```
[tryaii cachelint] template slot {DAY_OF_WEEK} at app.py:21 renders inside your cacheable prefix — its value changes between calls and breaks the cache there
[tryaii cachelint]   {DAY_OF_WEEK} = datetime.now().strftime('%A') at app.py:9
```

**What it traces**: inline f-strings / template literals, `.format()` calls
(Python), `+`-concatenation, and single-assignment variables (`const` in JS;
a reassigned name is honestly UNKNOWN — the tracer can't know which render
fired). Bare-name slots resolve one extra hop through their own assignment.

**When it speaks** (same problems-only philosophy):
- A structurally dynamic slot (its expression contains a call, like
  `datetime.now()` / `new Date()`) inside your cacheable prefix warns on
  first sight; at most 3 slot lines per warning, once per call site.
- A bare-name slot arms on its first value and warns on the call that
  *proves* it dynamic — when its rendered value actually changes.
- Where the engine already warns, introspection only enriches: the existing
  `first blocker:` line gains a `rendered by template slot {...} at file:line`
  attribution when a slot produced that blocker.
- Where the regex detectors already catch the rendered value (a date, a
  UUID), the engine wins and the slot stays silent — no double reporting.

**What it can't trace** (all silently fall back to today's behavior):
prompts arriving as function parameters or from other modules, attributes/
subscripts, loop-bound names (`for prompt in prompts:`), REPL/`exec` code,
frozen bundles, files over 1 MB, and Python `asyncio.create_task`/`gather`
call sites. Node's V8 async traces reach further (even `Promise.all`);
detached generators and `.then()` chains fail open in both.

> **Privacy — local source parsing.** In warn mode the SDK reads the *source
> file of the module that called `chat()`/`stream()`* from local disk to
> trace how the prompt string was built. The file is parsed, never executed,
> and never leaves the process — no source is transmitted, stored, or added
> to any payload; warnings carry only the file's basename. Where the source
> can't be found or parsed (REPLs, frozen apps, bundles), the hook silently
> behaves exactly as before.

Node specifics: JS files parse via the bundled `acorn`; `.ts`/`.tsx` frames
(tsx / ts-node users) parse via your project's own `typescript` (an optional
peer dependency) and fail open when it isn't importable. Analysis is cached
per call site — a loop pays the parse once, not per call.

## Notes and caveats

- **Python + OpenAI/xAI-routed prompts** need the tokenizer extra for exact
  analysis: `pip install tryaii[cachelint]`. Without it the hook skips those
  prompts and prints a one-time install hint. The Node SDK bundles its
  tokenizer.
- **Streaming** verification is best-effort: OpenRouter only includes a
  `usage` chunk when usage accounting is enabled upstream; the hook reads it
  when present and stays silent otherwise. `chat()` is the first-class path.
- `CACHEABLE_WITH_ACTION` (Anthropic-routed prompts that would need
  `cache_control` breakpoints) is intentionally silent today: the SDK does
  not send cache breakpoints yet, so zero cached tokens is the *correct*
  outcome — warning would be noise. This becomes actionable once the SDK can
  set breakpoints.
- Python validates the option strictly (`ValueError` for anything other than
  `"off"`/`"warn"`); Node relies on the TypeScript union and treats unknown
  runtime values as off. Env values are lenient in both.

## See also

- [`tryaii cachelint` CLI](../../cli/cachelint.md) — the same engine over
  files/stdin, with the full report and `--json`.
- [DREClient](README.md) · [OpenRouterIntegration](openrouter.md) ·
  [AsyncDREClient](async.md)
