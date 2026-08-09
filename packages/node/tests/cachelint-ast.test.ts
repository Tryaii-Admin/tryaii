/**
 * Tests for cacheLint AST template introspection — the mirror of
 * packages/python/tests/test_cachelint_ast.py.
 *
 * Fixture source files are AUTHORED AS STRINGS with pinned line layouts,
 * written to a temp dir, and NEVER executed — the tracer only needs
 * (file, line, col), injected via preflight's callSite test seam. Expected
 * warning literals are exact full-line strings; the Python suite pins the
 * same formats for its language-analog fixtures (language-native slot exprs
 * — `new Date()` vs `datetime.now()` — are the documented carve-out).
 */

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterAll, beforeEach, describe, expect, it } from 'vitest';

import { CacheLintHook } from '../src/cachelint/hook.js';
import * as introspect from '../src/cachelint/introspect.js';

const BIG = 'You are the routing assistant for a logistics platform. '.repeat(40);
const PREFIX = BIG.repeat(4); // ~2244 tok @4.0 — comfortably above gemini's 2048 floor
const MODEL = 'google/gemini-2.5-pro';
const NOTE_LINE =
  '[tryaii cachelint] note: reported once per unique prompt shape per client; ' +
  "set cacheLint: 'off' or TRYAII_CACHE_LINT=off to disable";

const root = mkdtempSync(join(tmpdir(), 'cachelint-ast-test-'));
afterAll(() => rmSync(root, { recursive: true, force: true }));

let fixtureCount = 0;
function writeFixture(name: string, source: string): string {
  const path = join(root, `${fixtureCount++}-${name}`);
  writeFileSync(path, source);
  return path;
}

function makeHook(sink: string[]): CacheLintHook {
  return new CacheLintHook({ sink: (l) => sink.push(l) });
}

async function preflight(
  hook: CacheLintHook,
  path: string,
  line: number,
  rendered: string,
  opts?: { model?: string; system?: string; col?: number | null },
): Promise<string | null> {
  const messages: Array<{ role: string; content: string }> = [];
  if (opts?.system !== undefined) messages.push({ role: 'system', content: opts.system });
  messages.push({ role: 'user', content: rendered });
  return hook.preflight(opts?.model ?? MODEL, messages, {
    file: path,
    line,
    col: opts?.col ?? null,
  });
}

beforeEach(() => introspect.clearCaches());

// Fixture sources — line layouts are load-bearing; the call line is marked.
const INLINE_DYNAMIC = `const client = getClient();
const resp = await client.chat(
  \`\${PREFIX_TEXT}Today is \${new Date().toDateString()}, plan the routes.\`
);
`; // call at line 2

const TRACED_VARIABLE = `const BASE = loadBase();
const DAY_OF_WEEK = new Date().toDateString();
const client = getClient();
const resp = await client.chat(
  \`\${PREFIX_TEXT}Today is \${DAY_OF_WEEK}, plan the routes accordingly and reply.\`
);
`; // call at line 4; DAY_OF_WEEK declared at line 2

describe('slot warnings', () => {
  it('inline dynamic slot warns with the exact literal shape', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const path = writeFixture('app.js', INLINE_DYNAMIC);
    const rendered = PREFIX + 'Today is Monday, plan the routes.';
    const key = await preflight(hook, path, 2, rendered);
    expect(key).not.toBeNull();
    expect(sink.some((l) => l.includes('template slot {new Date().toDateString()}')
      && l.includes('renders inside your cacheable prefix'))).toBe(true);
    expect(sink[sink.length - 1]).toBe(NOTE_LINE);
  });

  it('traced variable slot emits SLOT + RESOLVED lines', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const path = writeFixture('app.js', TRACED_VARIABLE);
    const base = path.split(/[\\/]/).pop();
    const rendered = PREFIX + 'Today is Monday, plan the routes accordingly and reply.';
    await preflight(hook, path, 4, rendered);
    expect(sink).toContain(
      `[tryaii cachelint] template slot {DAY_OF_WEEK} at ${base}:4 renders ` +
        'inside your cacheable prefix — its value changes between calls and ' +
        'breaks the cache there',
    );
    expect(sink).toContain(
      `[tryaii cachelint]   {DAY_OF_WEEK} = new Date().toDateString() at ${base}:2`,
    );
  });

  it('each dynamic slot warns once; static identifier slot stays silent', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const client = getClient();
const resp = await client.chat(
  \`Session \${sessionId()} opened. \${PREFIX_TEXT}Ref tail \${refId()}.\`
);
`;
    const path = writeFixture('app.js', src);
    const rendered = `Session alpha opened. ${PREFIX}Ref tail omega.`;
    await preflight(hook, path, 2, rendered);
    const slotLines = sink.filter((l) => l.includes('template slot') && !l.includes('more'));
    // Clean verdict -> boundary is the whole prompt, so BOTH call-bearing
    // slots are in-prefix and warn; the unbound PREFIX_TEXT identifier is
    // tier-1 unarmed and stays silent. (True tail classification only exists
    // when a blocker creates a real boundary — covered by the attribution test.)
    expect(slotLines).toHaveLength(2);
    expect(slotLines.some((l) => l.includes('{sessionId()}'))).toBe(true);
    expect(slotLines.some((l) => l.includes('{refId()}'))).toBe(true);
  });

  it('concat template with untraceable base stays silent', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const client = getClient();
const resp = await client.chat(BASE_TEXT + new Date().toISOString() + ' end.');
`;
    const path = writeFixture('app.js', src);
    await preflight(hook, path, 2, PREFIX + '2026-08-09T10:00:00 end.');
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true);
  });

  it('systemMessage option target is traced', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const client = getClient();
const resp = await client.chat('Question about routes?', {
  systemMessage: \`\${PREFIX_TEXT}Now: \${clockPhrase()}.\`,
});
`;
    const path = writeFixture('app.js', src);
    const system = PREFIX + 'Now: half past nine in the morning.';
    await preflight(hook, path, 2, 'Question about routes?', { system });
    expect(sink.some((l) => l.includes('template slot {clockPhrase()}'))).toBe(true);
  });

  it('caps at three slot lines then the MORE literal', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const client = getClient();
const resp = await client.chat(
  \`\${PREFIX_TEXT}A \${a()} B \${b()} C \${c()} D \${d()} E \${e()} tail.\`
);
`;
    const path = writeFixture('app.js', src);
    const rendered = PREFIX + 'A one B two C three D four E five tail.';
    await preflight(hook, path, 2, rendered);
    const slotLines = sink.filter((l) => l.includes('template slot') && !l.includes('more'));
    expect(slotLines).toHaveLength(3);
    expect(sink).toContain(
      '[tryaii cachelint]   ...and 2 more template slot(s) in the cacheable prefix',
    );
  });
});

describe('suppression and dedup', () => {
  it('BELOW_THRESHOLD never emits slot lines', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const client = getClient();
const resp = await client.chat(\`Today is \${new Date().toDateString()}.\`);
`;
    const path = writeFixture('app.js', src);
    await preflight(hook, path, 2, 'Today is Monday.', { model: 'anthropic/claude-fable-5' });
    expect(sink.some((l) => l.includes('BELOW_THRESHOLD'))).toBe(true);
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true);
  });

  it('static const slot never warns', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const APP_NAME = 'tryaii';
const client = getClient();
const resp = await client.chat(\`\${PREFIX_TEXT}Welcome to \${APP_NAME}, ask away.\`);
`;
    const path = writeFixture('app.js', src);
    await preflight(hook, path, 3, PREFIX + 'Welcome to tryaii, ask away.');
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true);
  });

  it('tier-1 bare identifier arms on first value, warns once on change', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const DAY_OF_WEEK = configuredDay;
const client = getClient();
const resp = await client.chat(
  \`\${PREFIX_TEXT}Today is \${DAY_OF_WEEK}, plan the routes accordingly and reply.\`
);
`;
    const path = writeFixture('app2.js', src);
    const r = (day: string) => PREFIX + `Today is ${day}, plan the routes accordingly and reply.`;
    await preflight(hook, path, 3, r('Monday'));
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true); // armed, silent
    await preflight(hook, path, 3, r('Tuesday')); // value changed
    expect(sink.some((l) => l.includes('template slot {DAY_OF_WEEK}'))).toBe(true);
    const n = sink.length;
    await preflight(hook, path, 3, r('Wednesday')); // site-deduped
    expect(sink).toHaveLength(n);
  });

  it('site dedup holds across distinct value shapes', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const path = writeFixture('app.js', INLINE_DYNAMIC);
    for (const day of ['Monday', 'Tuesday', 'Wednesday']) {
      await preflight(hook, path, 2, PREFIX + `Today is ${day}, plan the routes.`);
    }
    const slotLines = sink.filter((l) => l.includes('template slot') && !l.includes('more'));
    expect(new Set(slotLines).size).toBe(slotLines.length);
  });
});

describe('attribution', () => {
  it('blocker intersecting a slot appends the attribution line', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const SESSION = crypto.randomUUID();
const client = getClient();
const resp = await client.chat(
  \`Session \${SESSION} live. \${PREFIX_TEXT}Question about the routes?\`
);
`;
    const path = writeFixture('app.js', src);
    const base = path.split(/[\\/]/).pop();
    const rendered =
      'Session 7f9c02aa-51b3-4c2e-9f10-8a4d55e01b27 live. ' + PREFIX + 'Question about the routes?';
    await preflight(hook, path, 3, rendered);
    expect(sink.some((l) => l.includes('EFFECTIVELY_UNCACHEABLE'))).toBe(true);
    expect(sink.some((l) => l.startsWith('[tryaii cachelint]   first blocker: uuid'))).toBe(true);
    expect(sink).toContain(
      `[tryaii cachelint]   rendered by template slot {SESSION} at ${base}:3`,
    );
  });
});

describe('fail-open', () => {
  it('untraceable shapes are silent (param, reassigned let)', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const cases: Array<[string, string, number]> = [
      ['param.js', 'async function run(client, prompt) {\n  return client.chat(prompt);\n}\n', 2],
      ['reassign.js',
        'let P = `${PREFIX_TEXT}one ${A}.`;\nP = `${PREFIX_TEXT}two ${B}.`;\n'
        + 'const resp = client.chat(P);\n', 3],
    ];
    for (const [name, src, line] of cases) {
      const path = writeFixture(name, src);
      await preflight(hook, path, line, PREFIX + 'whatever text.');
    }
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true);
  });

  it('missing files and synthetic names are silent', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    await preflight(hook, join(root, 'nope.js'), 1, PREFIX + 'hello.');
    await preflight(hook, 'node:internal/fake', 1, PREFIX + 'hello two.');
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true);
  });

  it('misaligned template is discarded by the validation guard', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const path = writeFixture('app.js', INLINE_DYNAMIC);
    await preflight(hook, path, 2, PREFIX + 'completely different rendered text');
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true);
  });

  it('a rejected introspect load never breaks preflight', async () => {
    const sink: string[] = [];
    const hook = new CacheLintHook({
      sink: (l) => sink.push(l),
      loadIntrospect: () => Promise.reject(new Error('no introspect')),
    });
    const path = writeFixture('app.js', INLINE_DYNAMIC);
    const key = await preflight(hook, path, 2, PREFIX + 'Today is Monday, plan the routes.');
    expect(key).not.toBeNull();
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true);
  });

  it('a .ts frame with typescript unavailable is silent (fail-open)', async () => {
    const sink: string[] = [];
    const hook = new CacheLintHook({
      sink: (l) => sink.push(l),
      loadIntrospect: async () => {
        const real = await import('../src/cachelint/introspect.js');
        return {
          ...real,
          analyzeCallSite: async () => null, // stand-in for ts-import rejection
        } as typeof real;
      },
    });
    const path = writeFixture('app.ts', 'const x = 1;\n');
    const key = await preflight(hook, path, 1, PREFIX + 'hello.');
    expect(key).not.toBeNull();
    expect(sink.every((l) => !l.includes('template slot'))).toBe(true);
  });
});

describe('position paths', () => {
  it('multi-line call resolves via the await-unwrap rule with a V8 column', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const DAY_OF_WEEK = new Date().toDateString();
const client = getClient();
const resp = await client
  .chat(
    \`\${PREFIX_TEXT}Today is \${DAY_OF_WEEK}, plan the routes accordingly and reply.\`
  );
`;
    const path = writeFixture('app.js', src);
    const rendered = PREFIX + 'Today is Monday, plan the routes accordingly and reply.';
    // V8 async frames report the await-keyword position: line 3, col 14
    await preflight(hook, path, 3, rendered, { col: 14 });
    expect(sink.some((l) => l.includes('template slot {DAY_OF_WEEK}'))).toBe(true);
  });

  it('column mismatch falls back to line containment (guarded)', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const path = writeFixture('app.js', TRACED_VARIABLE);
    const rendered = PREFIX + 'Today is Monday, plan the routes accordingly and reply.';
    await preflight(hook, path, 4, rendered, { col: 999 }); // drifted source-map column
    expect(sink.some((l) => l.includes('template slot {DAY_OF_WEEK}'))).toBe(true);
  });
});

describe('live wiring', () => {
  const realFetch = globalThis.fetch;
  afterAll(() => {
    globalThis.fetch = realFetch;
  });

  it('a real chat() call from an untransformed user module traces live', async () => {
    // vitest transforms THIS file (import hoisting shifts line numbers), so
    // the user call must live in a plain .mjs module imported from tmpdir —
    // executed untransformed, exactly like real user code under plain node.
    // Bonus: this exercises the acorn (JS) parse path in a live flow.
    const { pathToFileURL } = await import('node:url');
    const { OpenRouterIntegration } = await import('../src/integrations/openrouter.js');

    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({ choices: [{ message: { content: 'OK.' } }], usage: { total_tokens: 42 } }),
        { status: 200 },
      )) as typeof fetch;

    const userSource = `export async function run(integ, prefixText) {
  return await integ.chat(
    \`\${prefixText}Today is day \${new Date().getDay()}, plan the routes.\`,
    { overrideModel: 'google/gemini-2.5-pro' },
  );
}
`; // chat call spans lines 2-5 of the user module
    const userPath = writeFixture('live-user.mjs', userSource);
    const user = await import(pathToFileURL(userPath).href);

    const sink: string[] = [];
    const integ = new OpenRouterIntegration(null, { apiKey: 'k', cacheLint: 'warn' });
    (integ as unknown as { _cacheLint: CacheLintHook })._cacheLint = makeHook(sink);

    const resp = await user.run(integ, PREFIX);
    expect(resp.content).toBe('OK.');
    // getDay() renders a bare digit (not a detector pattern), so the engine is
    // silent and the slot line is the standalone blind-spot insight; the call
    // starts at line 2 of the user module.
    const base = userPath.split(/[\\/]/).pop();
    const slotLine = sink.find((l) => l.includes('template slot {new Date().getDay()}'));
    expect(slotLine).toBeDefined();
    expect(slotLine).toContain(`at ${base}:2`);
  });
});

describe('typescript fixture parsing', () => {
  it('traces a .ts fixture identically when typescript is importable', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const src = `const DAY_OF_WEEK: string = new Date().toDateString();
const client = getClient();
const resp = await client.chat(
  \`\${PREFIX_TEXT}Today is \${DAY_OF_WEEK}, plan the routes accordingly and reply.\`
);
`;
    const path = writeFixture('app.ts', src);
    const base = path.split(/[\\/]/).pop();
    const rendered = PREFIX + 'Today is Monday, plan the routes accordingly and reply.';
    await preflight(hook, path, 3, rendered);
    expect(sink).toContain(
      `[tryaii cachelint] template slot {DAY_OF_WEEK} at ${base}:3 renders ` +
        'inside your cacheable prefix — its value changes between calls and ' +
        'breaks the cache there',
    );
    expect(sink).toContain(
      `[tryaii cachelint]   {DAY_OF_WEEK} = new Date().toDateString() at ${base}:1`,
    );
  });
});
