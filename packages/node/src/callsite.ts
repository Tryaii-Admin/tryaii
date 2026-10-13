/**
 * Shared synchronous call-site capture: the first stack frame outside this
 * package, i.e. the user code that invoked the SDK.
 *
 * Used by the cachelint warn hook (AST introspection of the prompt template)
 * and by the monitor wrapper (which file/function a sub-process runs in).
 * No value-imports: safe to load from the lightest entry points.
 *
 * Spike-verified behaviour: V8 zero-cost async CallSites carry the awaiting
 * user frame through internal awaits; `node:` internal frames (timers, task
 * queues) interleave and are skipped; a first foreign frame inside another
 * node_modules tree is a wrapper library, not user code — dead-end (null).
 */

import { fileURLToPath } from 'node:url';

export interface CallSite {
  file: string;
  line: number; // 1-based (V8)
  col: number | null; // 1-based (V8); null when unknown
  // Enclosing function name (V8 getFunctionName, then getMethodName); null
  // when anonymous or at module level. Optional so literal sites built by
  // callers that predate the field stay valid.
  function?: string | null;
}

// The package root for the stack-walk frame filter: '..' from this module is
// src/ under vitest and dist/ when installed — both are exactly what must be
// skipped as SDK-internal.
const PKG_ROOT = fileURLToPath(new URL('.', import.meta.url))
  .replace(/\\/g, '/')
  .toLowerCase();

export function captureCallSite(): CallSite | null {
  const saved = Error.prepareStackTrace;
  const savedLimit = Error.stackTraceLimit;
  let sites: NodeJS.CallSite[];
  try {
    Error.stackTraceLimit = 40;
    Error.prepareStackTrace = (_err, callSites) => callSites;
    sites = new Error().stack as unknown as NodeJS.CallSite[];
  } finally {
    Error.prepareStackTrace = saved;
    Error.stackTraceLimit = savedLimit;
  }
  if (!Array.isArray(sites)) return null;
  for (const site of sites) {
    let file = site.getFileName();
    if (typeof file === 'string' && file.startsWith('file://')) {
      try {
        file = fileURLToPath(file);
      } catch {
        continue;
      }
    }
    if (!file) continue; // anonymous / eval frames
    if (file.startsWith('node:')) continue; // runtime internals interleave
    const norm = file.replace(/\\/g, '/').toLowerCase();
    if (norm.startsWith(PKG_ROOT)) continue; // SDK-internal
    if (norm.includes('/node_modules/')) return null; // wrapper library: dead-end
    const line = site.getLineNumber();
    if (line === null || line === undefined) continue;
    const col = site.getColumnNumber();
    let fn: string | null = null;
    try {
      fn = site.getFunctionName() ?? site.getMethodName() ?? null;
    } catch {
      fn = null;
    }
    return { file, line, col: col ?? null, function: fn || null };
  }
  return null;
}
