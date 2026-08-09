#!/usr/bin/env node
/**
 * AST introspection spike (gate) — verifies the V8 mechanics the Node
 * cachelint template tracer depends on BEFORE it is built (precedent:
 * scripts/spike-cachelint-tokenizer-parity.mjs).
 *
 * Run manually per Node major:  node scripts/spike-cachelint-ast-node.mjs
 * Deps resolved from packages/node/node_modules (acorn via
 * `npm install --no-save acorn` until the real dependency lands; typescript
 * is already a devDependency there).
 *
 * PASS/FAIL matrix items (plan §Spike):
 *  1 prepareStackTrace capture works and restores cleanly
 *  2 GATE: user frame visible (file/line/col) for direct `await client.chat()`
 *    AFTER internal awaits, in both ESM and CJS (+ tail-call shape documented)
 *  3 `for await` over the async-generator stream() reaches the user frame
 *  4 Promise.all / .then() chains: no WRONG-user-line misattribution (a frame
 *    outside the prompt-bearing call is harmless — the arg locator discards it)
 *  5 detached async generator attributes the consumption line (documented)
 *  6 V8 (line,col) semantics for multi-line calls, reconciled against acorn
 *  7 tsx / ts-node .ts filename fidelity (best-effort; SKIP if not installed)
 *  8 acorn parse + span reconciliation; typescript parse twin equivalence
 *  9 tsc ES2020 output preserves template literals
 */
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../packages/node/package.json', import.meta.url));

const results = [];
function record(item, name, pass, detail = '') {
  results.push({ item, name, pass, detail });
  const tag = pass === true ? 'PASS' : pass === false ? 'FAIL' : 'SKIP';
  console.log(`[${tag}] ${item}. ${name}${detail ? ' — ' + detail : ''}`);
}

// ---------------------------------------------------------------------------
// Scaffold: a fake "SDK package dir" + user modules in a temp tree, so the
// frame filter has a real package boundary to skip.
// ---------------------------------------------------------------------------
const root = mkdtempSync(join(tmpdir(), 'cachelint-ast-spike-'));
const sdkDir = join(root, 'sdk');
mkdirSync(sdkDir);

// The capture helper the real hook will embed (~the planned _captureRaw).
const CAPTURE = `
export function captureRaw(pkgDir) {
  const saved = Error.prepareStackTrace;
  const savedLimit = Error.stackTraceLimit;
  let sites;
  try {
    Error.stackTraceLimit = 40;
    Error.prepareStackTrace = (_err, callSites) => callSites;
    sites = new Error().stack;
  } finally {
    Error.prepareStackTrace = saved;
    Error.stackTraceLimit = savedLimit;
  }
  const frames = [];
  for (const s of sites) {
    let file = s.getFileName();
    if (typeof file === 'string' && file.startsWith('file://')) {
      try { file = decodeURIComponent(new URL(file).pathname.replace(/^\\/([A-Za-z]:)/, '$1')); } catch {}
    }
    frames.push({ file, line: s.getLineNumber(), col: s.getColumnNumber(),
                  fn: s.getFunctionName(), async: s.isAsync ? s.isAsync() : null });
  }
  const norm = (p) => (p || '').replace(/\\\\/g, '/').toLowerCase();
  const pkg = norm(pkgDir);
  for (const f of frames) {
    const nf = norm(f.file);
    if (!nf || nf.startsWith(pkg)) continue;          // SDK-internal / anonymous
    if (f.file.startsWith('node:')) continue;         // runtime internals (timers, task queues)
    return { user: f, frames };
  }
  return { user: null, frames };
}
`;
writeFileSync(join(sdkDir, 'capture.mjs'), CAPTURE);

// SDK client that mimics the real hook seat: internal awaits BEFORE capture.
writeFileSync(join(sdkDir, 'client.mjs'), `
import { captureRaw } from './capture.mjs';
const SDK_DIR = new URL('.', import.meta.url).pathname.replace(/^\\/([A-Za-z]:)/, '$1');
async function preflightLike() {
  await Promise.resolve();               // mimic dynamic engine import
  await new Promise(r => setTimeout(r, 1));
  return captureRaw(SDK_DIR);
}
export class Client {
  async chat(prompt) { await Promise.resolve(); return preflightLike(); }
  async *stream(prompt) { const cap = await preflightLike(); yield cap; }
}
`);

// CJS twins.
writeFileSync(join(sdkDir, 'capture.cjs'),
  CAPTURE.replace('export function captureRaw', 'function captureRaw')
    + '\nmodule.exports = { captureRaw };\n');
writeFileSync(join(sdkDir, 'client.cjs'), `
const { captureRaw } = require('./capture.cjs');
const SDK_DIR = __dirname;
async function preflightLike() {
  await Promise.resolve();
  await new Promise(r => setTimeout(r, 1));
  return captureRaw(SDK_DIR);
}
class Client {
  async chat(prompt) { await Promise.resolve(); return preflightLike(); }
}
module.exports = { Client };
`);

// User module (ESM) — line numbers below are asserted, keep layout frozen.
const USER_MJS = `import { Client } from './sdk/client.mjs';
const client = new Client();
const PROMPT = \`Today is \${new Date().toDateString()}, plan my day.\`;
export async function directAwait() {
  return await client.chat(PROMPT);      // line 5: true await
}
export async function tailCall() {
  return client.chat(PROMPT);            // line 8: NO await (tail-call shape)
}
export async function multiLine() {
  const r = await client
    .chat(
      PROMPT,
    );                                    // call spans lines 11-14
  return r;
}
export async function forAwait() {
  for await (const cap of client.stream(PROMPT)) {   // line 18
    return cap;
  }
}
export async function viaPromiseAll() {
  const [cap] = await Promise.all([client.chat(PROMPT)]);   // line 23
  return cap;
}
export function viaThen() {
  return client.chat(PROMPT).then((cap) => cap);            // line 27
}
export async function detachedGen() {
  const gen = client.stream(PROMPT);     // line 30: created here
  for await (const cap of gen) return cap;   // line 31: consumed here
}
`;
writeFileSync(join(root, 'user.mjs'), USER_MJS);

writeFileSync(join(root, 'user.cjs'), `const { Client } = require('./sdk/client.cjs');
const client = new Client();
async function directAwait() {
  return await client.chat('hello');     // line 4: true await
}
module.exports = { directAwait };
`);

const user = await import(pathToFileURL(join(root, 'user.mjs')).href);
const userCjs = require(join(root, 'user.cjs'));
const normPath = (p) => (p || '').replace(/\\/g, '/').toLowerCase();
const userFile = normPath(join(root, 'user.mjs'));
const userCjsFile = normPath(join(root, 'user.cjs'));

// --- 1: capture + restore ---------------------------------------------------
{
  const before = Error.prepareStackTrace;
  const cap = await user.directAwait();
  const restored = Error.prepareStackTrace === before;
  const plainStackOk = typeof new Error().stack === 'string';
  record(1, 'prepareStackTrace capture + clean restore',
    cap.frames.length > 0 && restored && plainStackOk,
    `${cap.frames.length} frames`);
}

// --- 2: THE GATE — direct await, ESM + CJS ----------------------------------
{
  const cap = await user.directAwait();
  const esmOk = cap.user && normPath(cap.user.file) === userFile && cap.user.line === 5;
  record(2, 'GATE ESM: await chat() user frame after internal awaits',
    !!esmOk, cap.user ? `${cap.user.file}:${cap.user.line}:${cap.user.col} async=${cap.user.async}` : 'NO USER FRAME');
  if (!esmOk) console.log('  frames:', JSON.stringify(cap.frames, null, 1));

  const capC = await userCjs.directAwait();
  const cjsOk = capC.user && normPath(capC.user.file) === userCjsFile && capC.user.line === 4;
  record(2, 'GATE CJS: await chat() user frame after internal awaits',
    !!cjsOk, capC.user ? `${capC.user.file}:${capC.user.line}:${capC.user.col}` : 'NO USER FRAME');
  if (!cjsOk) console.log('  frames:', JSON.stringify(capC.frames, null, 1));

  // Tail-call shape: helper returns the promise without awaiting — the helper
  // frame legitimately drops out of the async chain; attribution lands at the
  // helper's CALLER (still user code; the arg locator discards it harmlessly).
  const capTail = await user.tailCall();
  record(2, 'tail-call (return without await): attribution documented',
    true, capTail.user ? `lands at ${capTail.user.file}:${capTail.user.line}` : 'null');
}

// --- 3: for await over async generator ---------------------------------------
{
  const cap = await user.forAwait();
  const ok = cap.user && normPath(cap.user.file) === userFile;
  record(3, 'for-await over stream() reaches user frame',
    !!ok, cap.user ? `line=${cap.user.line} (loop is line 18)` : 'NO USER FRAME');
  if (!ok) console.log('  frames:', JSON.stringify(cap.frames, null, 1));
}

// --- 4: Promise.all / .then — no wrong-user-line misattribution ---------------
{
  const capAll = await user.viaPromiseAll();
  const capThen = await user.viaThen();
  const describe = (cap) => cap.user ? `USER ${cap.user.file}:${cap.user.line}` : 'null (dead-end)';
  // A frame outside user.mjs (e.g. this spike's own awaiting frame) is harmless
  // by design — the arg locator finds no prompt argument there and discards.
  // Only a WRONG line inside user.mjs would be a real misattribution.
  const allOk = !capAll.user || normPath(capAll.user.file) !== userFile || capAll.user.line === 23;
  const thenOk = !capThen.user || normPath(capThen.user.file) !== userFile || capThen.user.line === 27;
  record(4, 'Promise.all: no wrong-line misattribution', allOk, describe(capAll));
  record(4, '.then(): no wrong-line misattribution', thenOk, describe(capThen));
}

// --- 5: detached generator attribution ---------------------------------------
{
  const cap = await user.detachedGen();
  const detail = cap.user ? `attributed line ${cap.user.line} (created 30, consumed 31)` : 'null';
  record(5, 'detached generator: attribution documented', true, detail);
}

// --- 6 + 8: V8 col semantics vs acorn + typescript twin -----------------------
{
  const acorn = require('acorn');
  const src = USER_MJS;
  const tree = acorn.parse(src, { ecmaVersion: 'latest', sourceType: 'module', locations: true, ranges: true });

  // findCall rule under test: V8 reports the CALL position for sync frames but
  // the AWAIT-expression position for async frames — so containment must be
  // checked against BOTH node kinds, unwrapping AwaitExpression -> argument.
  const nodes = [];
  (function walk(n) {
    if (n && typeof n.type === 'string') {
      if (n.type === 'CallExpression' || n.type === 'AwaitExpression') nodes.push(n);
      for (const k of Object.keys(n)) {
        const v = n[k];
        if (Array.isArray(v)) v.forEach(walk);
        else if (v && typeof v.type === 'string') walk(v);
      }
    }
  })(tree);

  function findCall(line, col) {   // col 1-based from V8
    let best = null;
    for (const n of nodes) {
      const s = n.loc.start, e = n.loc.end;
      const contains = (line > s.line || (line === s.line && col >= s.column + 1))
                    && (line < e.line || (line === e.line && col <= e.column + 1));
      if (!contains) continue;
      let call = n;
      if (n.type === 'AwaitExpression') {
        call = n.argument && n.argument.type === 'CallExpression' ? n.argument : null;
      }
      if (!call) continue;
      // innermost = latest start position
      if (!best || call.loc.start.line > best.loc.start.line
          || (call.loc.start.line === best.loc.start.line
              && call.loc.start.column >= best.loc.start.column)) best = call;
    }
    return best;
  }

  const cap = await user.multiLine();
  let semantics = 'NO USER FRAME';
  let matched = false;
  if (cap.user) {
    const { line, col } = cap.user;
    const call = findCall(line, col);
    // the multi-line .chat( call spans lines 11-14
    matched = !!call && call.loc.start.line === 11 && call.loc.end.line === 14;
    semantics = call
      ? `V8 ${line}:${col} -> call ${call.loc.start.line}:${call.loc.start.column + 1}-${call.loc.end.line}:${call.loc.end.column + 1} (await-unwrap rule)`
      : `V8 ${line}:${col} matched NO Call/Await node`;
  }
  record(6, 'multi-line call: V8 (line,col) resolves the Call via await-unwrap rule', matched, semantics);
  if (!matched) console.log('  frames:', JSON.stringify(cap.frames, null, 1));

  // 8a: acorn segments of the template literal
  let tmpl = null;
  (function find(n) {
    if (tmpl || !n || typeof n.type !== 'string') return;
    if (n.type === 'TemplateLiteral') { tmpl = n; return; }
    for (const k of Object.keys(n)) {
      const v = n[k];
      if (Array.isArray(v)) v.forEach(find);
      else if (v && typeof v.type === 'string') find(v);
    }
  })(tree);
  const quasis = tmpl ? tmpl.quasis.map((q) => q.value.cooked) : [];
  const exprs = tmpl ? tmpl.expressions.map((e) => src.slice(e.range[0], e.range[1])) : [];
  const acornOk = quasis.length === 2 && quasis[0] === 'Today is '
    && exprs.length === 1 && exprs[0] === 'new Date().toDateString()';
  record(8, 'acorn: template-literal segments extracted',
    acornOk, JSON.stringify({ quasis, exprs }));

  // 8b: typescript parse twin over the same source
  let tsOk = null; let tsDetail = 'typescript not importable';
  try {
    const ts = require('typescript');
    const sf = ts.createSourceFile('user.ts', src, ts.ScriptTarget.Latest, true);
    let tq = null;
    (function tfind(node) {
      if (tq) return;
      if (ts.isTemplateExpression(node)) { tq = node; return; }
      ts.forEachChild(node, tfind);
    })(sf);
    if (tq) {
      const head = tq.head.text;
      const spanExprs = tq.templateSpans.map((s2) => s2.expression.getText(sf));
      tsOk = head === 'Today is ' && spanExprs.length === 1 && spanExprs[0] === 'new Date().toDateString()';
      tsDetail = JSON.stringify({ head, spanExprs });
    } else { tsOk = false; tsDetail = 'no TemplateExpression found'; }
  } catch { tsOk = null; }
  record(8, 'typescript: identical segment extraction', tsOk, tsDetail);
}

// --- 7: tsx / ts-node filename fidelity (best-effort) -------------------------
{
  let status = null; let detail = 'tsx/ts-node not installed — SKIP (fail-open path covers it)';
  try {
    require.resolve('tsx');
    status = null; detail = 'tsx resolvable but exec check not implemented in spike';
  } catch { /* keep SKIP */ }
  record(7, '.ts filename fidelity under tsx/ts-node', status, detail);
}

// --- 9: tsc ES2020 preserves template literals --------------------------------
{
  let ok = null; let detail = '';
  try {
    const ts = require('typescript');
    const out = ts.transpileModule('const p = `Today is ${new Date().toDateString()}.`;',
      { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext } });
    ok = out.outputText.includes('`Today is ${');
    detail = JSON.stringify(out.outputText.trim());
  } catch (e) { detail = String(e); }
  record(9, 'tsc ES2020 output preserves template literals', ok, detail);
}

// -----------------------------------------------------------------------------
rmSync(root, { recursive: true, force: true });
const fails = results.filter((r) => r.pass === false);
console.log(`\n=== ${process.version} matrix: ${results.filter(r => r.pass === true).length} PASS / ${fails.length} FAIL / ${results.filter(r => r.pass === null).length} SKIP ===`);
process.exit(fails.length ? 1 : 0);
