/**
 * AST template introspection for the cacheLint warn hook.
 *
 * Rules (verbatim from the cachelint design doc, and enforced here):
 *   - parse-never-execute — the caller's source is read as text and parsed
 *     (acorn for JS, the typescript compiler's parser for TS when importable);
 *     nothing is ever evaluated or imported as code;
 *   - local-only — source never leaves the process; the only output is the
 *     warning text (which carries the file BASENAME, never the full path);
 *   - analysis cached per call site — one parse per module, one trace per
 *     (file, line), never per call.
 *
 * V8 position semantics (pinned by scripts/spike-cachelint-ast-node.mjs):
 * async frames report the AWAIT-keyword position, not the call start — so
 * call-node matching checks containment against BOTH CallExpression and
 * AwaitExpression nodes and unwraps the latter to its argument.
 *
 * This module is loaded by the hook via cached dynamic import (it statically
 * imports acorn, a small hard dependency; `typescript` is an optional peer,
 * dynamically imported only for .ts/.tsx/.mts/.cts frames). Every exported
 * function is fail-open: any failure returns null and the hook behaves
 * exactly as if this module did not exist.
 */

import { readFileSync, statSync } from 'node:fs';
import * as acorn from 'acorn';

export interface CallSite {
  file: string;
  line: number; // 1-based (V8)
  col: number | null; // 1-based (V8); null when unknown
}

export interface Segment {
  kind: 'text' | 'slot';
  text?: string;
  expr?: string;
  hasCall?: boolean;
  resolvedFrom?: [string, number] | null; // [expr, line] via single-declaration trace
}

export interface TemplateMap {
  target: 'prompt' | 'system_message';
  segments: Segment[];
  varLine: number | null;
}

export interface OverlaySlot {
  expr: string;
  start: number; // canonical char offsets
  end: number;
  inPrefix: boolean;
  hasCall: boolean;
  resolvedFrom: [string, number] | null;
}

const MAX_DEPTH = 4;
const MAX_SEGMENTS = 64;
const MAX_FILE_BYTES = 1_000_000;
const MODULE_CACHE_MAX = 64;
const TRACE_CACHE_MAX = 256;

const FAILED = Symbol('failed');

interface Parsed {
  source: string;
  calls: CallInfo[]; // normalized call nodes with line/col spans
  scope: Map<string, Binding[]>;
}

interface Loc {
  line: number; // 1-based
  col: number; // 1-based
}

interface CallInfo {
  start: Loc;
  end: Loc;
  awaitStart: Loc | null; // wrapping AwaitExpression start, when present
  args: ExprInfo[]; // positional arguments (null entries for spreads)
  options: Map<string, ExprInfo> | null; // arg[1] object-literal properties
}

/** Language-neutral expression handle the tracer walks. */
interface ExprInfo {
  kind: 'template' | 'string' | 'concat' | 'identifier' | 'other';
  text: string; // source slice (display)
  hasCall: boolean;
  parts?: Array<{ type: 'text'; value: string } | { type: 'expr'; info: ExprInfo }>;
  left?: ExprInfo;
  right?: ExprInfo;
  name?: string;
  line: number;
}

interface Binding {
  kind: 'decl' | 'assign' | 'loop';
  init: ExprInfo | null;
  line: number;
}

const moduleCache = new Map<string, Parsed | typeof FAILED>();
const traceCache = new Map<string, TemplateMap[] | typeof FAILED>();

/** Test seam: clear the module-level caches. */
export function clearCaches(): void {
  moduleCache.clear();
  traceCache.clear();
}

// ---------------------------------------------------------------------------
// Parsing (acorn for JS, typescript for TS) into the neutral representation
// ---------------------------------------------------------------------------

function fileKey(path: string): string | null {
  try {
    const st = statSync(path);
    if (st.size > MAX_FILE_BYTES) return null;
    return `${path.replace(/\\/g, '/').toLowerCase()}|${st.mtimeMs}|${st.size}`;
  } catch {
    return null;
  }
}

type AcornNode = acorn.Node & Record<string, any>;

function acornExprInfo(node: AcornNode, source: string): ExprInfo {
  const text = source.slice(node.start, node.end);
  const hasCall = containsCall(node);
  const line = (node.loc?.start.line ?? 0) as number;
  if (node.type === 'TemplateLiteral') {
    const parts: ExprInfo['parts'] = [];
    const quasis = node.quasis as AcornNode[];
    const exprs = node.expressions as AcornNode[];
    for (let i = 0; i < quasis.length; i++) {
      const cooked = quasis[i].value?.cooked;
      if (typeof cooked !== 'string') return { kind: 'other', text, hasCall, line };
      parts.push({ type: 'text', value: cooked });
      if (i < exprs.length) parts.push({ type: 'expr', info: acornExprInfo(exprs[i], source) });
    }
    return { kind: 'template', text, hasCall, parts, line };
  }
  if (node.type === 'Literal' && typeof node.value === 'string') {
    return { kind: 'string', text: node.value, hasCall: false, line };
  }
  if (node.type === 'BinaryExpression' && node.operator === '+') {
    return {
      kind: 'concat',
      text,
      hasCall,
      left: acornExprInfo(node.left, source),
      right: acornExprInfo(node.right, source),
      line,
    };
  }
  if (node.type === 'Identifier') {
    return { kind: 'identifier', text, hasCall: false, name: node.name, line };
  }
  return { kind: 'other', text, hasCall, line };
}

function containsCall(node: AcornNode): boolean {
  let found = false;
  (function walk(n: any) {
    if (found || !n || typeof n.type !== 'string') return;
    if (n.type === 'CallExpression' || n.type === 'NewExpression') {
      found = true;
      return;
    }
    for (const k of Object.keys(n)) {
      if (k === 'loc') continue;
      const v = n[k];
      if (Array.isArray(v)) v.forEach(walk);
      else if (v && typeof v.type === 'string') walk(v);
    }
  })(node);
  return found;
}

function parseAcornWithSource(source: string): Parsed | null {
  let tree: AcornNode;
  try {
    tree = acorn.parse(source, {
      ecmaVersion: 'latest', sourceType: 'module', locations: true, ranges: true,
    }) as AcornNode;
  } catch {
    try {
      tree = acorn.parse(source, {
        ecmaVersion: 'latest', sourceType: 'script', locations: true, ranges: true,
      }) as AcornNode;
    } catch {
      return null;
    }
  }

  const calls: CallInfo[] = [];
  const scope = new Map<string, Binding[]>();
  const push = (name: string, binding: Binding) => {
    const list = scope.get(name) ?? [];
    list.push(binding);
    scope.set(name, list);
  };
  const expr = (n: AcornNode): ExprInfo => acornExprInfo(n, source);

  (function walk(n: any, awaitStart: Loc | null) {
    if (!n || typeof n.type !== 'string') return;
    const nextAwait: Loc | null = n.type === 'AwaitExpression'
      ? { line: n.loc.start.line, col: n.loc.start.column + 1 }
      : null;
    if (n.type === 'CallExpression') {
      const args: ExprInfo[] = [];
      let options: Map<string, ExprInfo> | null = null;
      const argNodes = n.arguments as AcornNode[];
      if (argNodes[0] && argNodes[0].type !== 'SpreadElement') args.push(expr(argNodes[0]));
      if (argNodes[1] && argNodes[1].type === 'ObjectExpression') {
        options = new Map();
        for (const prop of argNodes[1].properties as AcornNode[]) {
          if (prop.type === 'Property' && !prop.computed) {
            const key = prop.key.type === 'Identifier' ? prop.key.name : prop.key.value;
            if (typeof key === 'string') options.set(key, expr(prop.value));
          }
        }
      }
      calls.push({
        start: { line: n.loc.start.line, col: n.loc.start.column + 1 },
        end: { line: n.loc.end.line, col: n.loc.end.column + 1 },
        awaitStart,
        args,
        options,
      });
    }
    if (n.type === 'VariableDeclarator' && n.id?.type === 'Identifier') {
      push(n.id.name, { kind: 'decl', init: n.init ? expr(n.init) : null, line: n.loc.start.line });
    }
    if (n.type === 'AssignmentExpression' && n.left?.type === 'Identifier') {
      push(n.left.name, { kind: 'assign', init: null, line: n.loc.start.line });
    }
    if ((n.type === 'ForOfStatement' || n.type === 'ForInStatement')
        && n.left?.type === 'VariableDeclaration') {
      for (const d of n.left.declarations as AcornNode[]) {
        if (d.id?.type === 'Identifier') push(d.id.name, { kind: 'loop', init: null, line: d.loc?.start.line ?? 0 });
      }
    }
    for (const k of Object.keys(n)) {
      if (k === 'loc') continue;
      const v = n[k];
      if (Array.isArray(v)) v.forEach((c) => walk(c, nextAwait ?? (n.type === 'CallExpression' ? null : awaitStart)));
      else if (v && typeof v.type === 'string') walk(v, nextAwait ?? (n.type === 'CallExpression' ? null : awaitStart));
    }
  })(tree, null);

  return { source, calls, scope };
}

// ---------------------------------------------------------------------------
// TypeScript parsing (optional peer, dynamic import)
// ---------------------------------------------------------------------------

let tsModule: Promise<any> | null = null;
let tsBroken = false;

async function loadTs(): Promise<any | null> {
  if (tsBroken) return null;
  tsModule ??= import('typescript');
  try {
    const mod = await tsModule;
    return mod.default ?? mod;
  } catch {
    tsBroken = true;
    return null;
  }
}

async function parseWithTs(source: string): Promise<Parsed | null> {
  const ts = await loadTs();
  if (!ts) return null;
  let sf: any;
  try {
    sf = ts.createSourceFile('user.ts', source, ts.ScriptTarget.Latest, true);
  } catch {
    return null;
  }

  const calls: CallInfo[] = [];
  const scope = new Map<string, Binding[]>();
  const push = (name: string, binding: Binding) => {
    const list = scope.get(name) ?? [];
    list.push(binding);
    scope.set(name, list);
  };
  const locOf = (pos: number): Loc => {
    const lc = sf.getLineAndCharacterOfPosition(pos);
    return { line: lc.line + 1, col: lc.character + 1 };
  };
  const lineOf = (node: any): number => locOf(node.getStart(sf)).line;

  function tsExprInfo(node: any): ExprInfo {
    const text = node.getText(sf);
    const hasCall = tsContainsCall(node);
    const line = lineOf(node);
    if (ts.isTemplateExpression(node)) {
      const parts: ExprInfo['parts'] = [{ type: 'text', value: node.head.text }];
      for (const span of node.templateSpans) {
        parts.push({ type: 'expr', info: tsExprInfo(span.expression) });
        parts.push({ type: 'text', value: span.literal.text });
      }
      return { kind: 'template', text, hasCall, parts, line };
    }
    if (ts.isNoSubstitutionTemplateLiteral(node) || ts.isStringLiteral(node)) {
      return { kind: 'string', text: node.text, hasCall: false, line };
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      return { kind: 'concat', text, hasCall, left: tsExprInfo(node.left), right: tsExprInfo(node.right), line };
    }
    if (ts.isIdentifier(node)) {
      return { kind: 'identifier', text, hasCall: false, name: node.text, line };
    }
    return { kind: 'other', text, hasCall, line };
  }

  function tsContainsCall(node: any): boolean {
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) return true;
    let found = false;
    ts.forEachChild(node, function visit(child: any) {
      if (found) return;
      if (ts.isCallExpression(child) || ts.isNewExpression(child)) {
        found = true;
        return;
      }
      ts.forEachChild(child, visit);
    });
    return found;
  }

  (function walk(node: any, awaitStart: Loc | null) {
    const nextAwait = ts.isAwaitExpression(node) ? locOf(node.getStart(sf)) : awaitStart;
    if (ts.isCallExpression(node)) {
      const args: ExprInfo[] = [];
      let options: Map<string, ExprInfo> | null = null;
      const argNodes = node.arguments;
      if (argNodes[0] && !ts.isSpreadElement(argNodes[0])) args.push(tsExprInfo(argNodes[0]));
      if (argNodes[1] && ts.isObjectLiteralExpression(argNodes[1])) {
        options = new Map();
        for (const prop of argNodes[1].properties) {
          if (ts.isPropertyAssignment(prop) && (ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name))) {
            options.set(prop.name.text, tsExprInfo(prop.initializer));
          }
        }
      }
      calls.push({
        start: locOf(node.getStart(sf)),
        end: locOf(node.getEnd()),
        awaitStart: ts.isAwaitExpression(node.parent) ? locOf(node.parent.getStart(sf)) : nextAwait,
        args,
        options,
      });
    }
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      push(node.name.text, {
        kind: 'decl',
        init: node.initializer ? tsExprInfo(node.initializer) : null,
        line: lineOf(node),
      });
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left)) {
      push(node.left.text, { kind: 'assign', init: null, line: lineOf(node) });
    }
    ts.forEachChild(node, (child: any) => walk(child, nextAwait));
  })(sf, null);

  return { source, calls, scope };
}

// ---------------------------------------------------------------------------
// Module cache + call finding + tracing
// ---------------------------------------------------------------------------

const TS_EXTENSIONS = /\.(ts|tsx|mts|cts)$/i;

async function parsedModule(path: string, key: string): Promise<Parsed | null> {
  const cached = moduleCache.get(key);
  if (cached !== undefined) return cached === FAILED ? null : cached;
  if (moduleCache.size >= MODULE_CACHE_MAX) {
    const oldest = moduleCache.keys().next().value as string | undefined;
    if (oldest !== undefined) moduleCache.delete(oldest);
  }
  let source: string;
  try {
    source = readFileSync(path, 'utf-8');
  } catch {
    moduleCache.set(key, FAILED);
    return null;
  }
  const parsed = TS_EXTENSIONS.test(path)
    ? await parseWithTs(source)
    : parseAcornWithSource(source);
  moduleCache.set(key, parsed ?? FAILED);
  return parsed;
}

/**
 * V8-position call matching with the spike-pinned await-unwrap rule: async
 * frames report the await position, which sits BEFORE the call span — so a
 * call matches when the position falls inside the call itself OR inside its
 * wrapping AwaitExpression. Innermost (latest-starting) match wins; when the
 * column is unknown (or nothing contains it) fall back to line containment.
 */
function findCall(parsed: Parsed, site: CallSite): CallInfo | null {
  const { line, col } = site;
  const cmp = (a: Loc, b: Loc) => (a.line - b.line) || (a.col - b.col);
  let best: CallInfo | null = null;
  for (const call of parsed.calls) {
    const from = call.awaitStart ?? call.start;
    let contains: boolean;
    if (col !== null) {
      const pos: Loc = { line, col };
      contains = cmp(pos, from) >= 0 && cmp(pos, call.end) <= 0;
    } else {
      contains = line >= from.line && line <= call.end.line;
    }
    if (!contains) continue;
    if (best === null || cmp(call.start, best.start) >= 0) best = call;
  }
  if (best === null && col !== null) {
    // column mismatch (source maps, transpilation): line-containment fallback,
    // guarded downstream by the rendered-alignment validation.
    return findCall(parsed, { ...site, col: null });
  }
  return best;
}

function slotMeta(info: ExprInfo, scope: Map<string, Binding[]>):
  { expr: string; hasCall: boolean; resolvedFrom: [string, number] | null } {
  let hasCall = info.hasCall;
  let resolvedFrom: [string, number] | null = null;
  if (info.kind === 'identifier' && info.name) {
    const bindings = scope.get(info.name) ?? [];
    const decls = bindings.filter((b) => b.kind === 'decl' && b.init !== null);
    if (bindings.length === 1 && decls.length === 1) {
      const init = decls[0].init as ExprInfo;
      resolvedFrom = [init.text, decls[0].line];
      hasCall = hasCall || init.hasCall;
    }
  }
  return { expr: info.text, hasCall, resolvedFrom };
}

function segments(info: ExprInfo, scope: Map<string, Binding[]>, useLine: number,
                  depth = 0): Segment[] | null {
  if (depth > MAX_DEPTH) return null;

  if (info.kind === 'template' && info.parts) {
    const segs: Segment[] = [];
    for (const part of info.parts) {
      if (part.type === 'text') {
        segs.push({ kind: 'text', text: part.value });
      } else {
        const meta = slotMeta(part.info, scope);
        segs.push({ kind: 'slot', expr: meta.expr, hasCall: meta.hasCall, resolvedFrom: meta.resolvedFrom });
      }
    }
    return segs.length <= MAX_SEGMENTS ? segs : null;
  }
  if (info.kind === 'string') {
    return [{ kind: 'text', text: info.text }];
  }
  if (info.kind === 'concat' && info.left && info.right) {
    const left = segments(info.left, scope, useLine, depth + 1);
    const right = segments(info.right, scope, useLine, depth + 1);
    if (left === null || right === null) return null;
    const combined = left.concat(right);
    return combined.length <= MAX_SEGMENTS ? combined : null;
  }
  if (info.kind === 'identifier' && info.name) {
    const bindings = scope.get(info.name) ?? [];
    const decls = bindings.filter((b) => b.kind === 'decl' && b.init !== null);
    if (bindings.length !== 1 || decls.length !== 1) return null; // reassigned / loop / unbound
    if (decls[0].line >= useLine) return null;
    return segments(decls[0].init as ExprInfo, scope, useLine, depth + 1);
  }
  return null; // params, member expressions, everything else: UNKNOWN
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function analyzeCallSite(
  site: CallSite,
  _messages: Array<{ role: string; content: string }>,
): Promise<TemplateMap[] | null> {
  try {
    const key = fileKey(site.file);
    if (key === null) return null;
    const traceKey = `${key}|${site.line}`;
    const cached = traceCache.get(traceKey);
    if (cached !== undefined) return cached === FAILED ? null : cached;
    if (traceCache.size >= TRACE_CACHE_MAX) {
      const oldest = traceCache.keys().next().value as string | undefined;
      if (oldest !== undefined) traceCache.delete(oldest);
    }

    const parsed = await parsedModule(site.file, key);
    if (parsed === null) {
      traceCache.set(traceKey, FAILED);
      return null;
    }
    const call = findCall(parsed, site);
    if (call === null) {
      traceCache.set(traceKey, FAILED);
      return null;
    }

    const candidates: Array<['prompt' | 'system_message', ExprInfo]> = [];
    if (call.args[0]) candidates.push(['prompt', call.args[0]]);
    if (call.options) {
      const sys = call.options.get('systemMessage') ?? call.options.get('system_message');
      if (sys) candidates.push(['system_message', sys]);
      const prompt = call.options.get('prompt');
      if (prompt) candidates.push(['prompt', prompt]);
    }

    const maps: TemplateMap[] = [];
    for (const [target, info] of candidates) {
      let varLine: number | null = null;
      if (info.kind === 'identifier' && info.name) {
        const decls = (parsed.scope.get(info.name) ?? []).filter((b) => b.kind === 'decl');
        if (decls.length === 1) varLine = decls[0].line;
      }
      const segs = segments(info, parsed.scope, call.start.line);
      if (segs === null) continue;
      if (segs.some((s) => s.kind === 'slot')) {
        maps.push({ target, segments: segs, varLine });
      }
    }
    const result = maps.length ? maps : null;
    traceCache.set(traceKey, result ?? FAILED);
    return result;
  } catch {
    return null; // fail-open, always
  }
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Match static text segments against the rendered string; slot spans out.
 * The validation guard: requires a FULL match with at least one non-empty
 * text segment — truncation, stale source, wrong node, conditional templates
 * and bundler rewrites all fail here and discard the analysis.
 */
export function align(segs: Segment[], rendered: string): Array<[number, number]> | null {
  const parts = segs.filter((s) => !(s.kind === 'text' && s.text === ''));
  if (!parts.some((s) => s.kind === 'text')) return null;
  const slotCount = parts.filter((s) => s.kind === 'slot').length;
  let pattern = '^';
  let slotSeen = 0;
  for (const seg of parts) {
    if (seg.kind === 'text') {
      pattern += escapeRegExp(seg.text as string);
    } else {
      slotSeen++;
      pattern += slotSeen === slotCount ? '([\\s\\S]*)' : '([\\s\\S]*?)';
    }
  }
  pattern += '$';
  const m = new RegExp(pattern).exec(rendered);
  if (m === null) return null;
  const spans: Array<[number, number]> = [];
  let cursor = 0;
  let group = 1;
  for (const seg of parts) {
    if (seg.kind === 'text') {
      cursor += (seg.text as string).length;
    } else {
      const value = m[group] ?? '';
      spans.push([cursor, cursor + value.length]);
      cursor += value.length;
      group++;
    }
  }
  return spans;
}

/** Map slot spans (message-relative) to canonical offsets + prefix flags. */
export function overlay(
  segs: Segment[],
  spans: Array<[number, number]>,
  contentStart: number,
  boundary: number,
): OverlaySlot[] {
  const slots: OverlaySlot[] = [];
  let i = 0;
  for (const seg of segs) {
    if (seg.kind !== 'slot') continue;
    const [start, end] = spans[i++];
    slots.push({
      expr: seg.expr as string,
      start: contentStart + start,
      end: contentStart + end,
      inPrefix: contentStart + start < boundary,
      hasCall: seg.hasCall ?? false,
      resolvedFrom: seg.resolvedFrom ?? null,
    });
  }
  return slots;
}
