/**
 * The `process(name, fn)` context: which sub-process the current code runs
 * in. An AsyncLocalStorage, so the name follows every await / promise /
 * timer started inside `fn`. Mirrors monitor/context.py (a ContextVar).
 */

import { AsyncLocalStorage } from 'node:async_hooks';

const storage = new AsyncLocalStorage<string>();

/** The sub-process name set by the innermost `process(name, fn)` call. */
export function currentProcess(): string | null {
  return storage.getStore() ?? null;
}

/**
 * Name the sub-process for every monitored call made while `fn` runs
 * (including everything it awaits). Per-call `tryaii_process` still wins
 * (SPEC.md §2.6). Returns whatever `fn` returns.
 */
export function runProcess<T>(name: string, fn: () => T): T {
  return storage.run(name, fn);
}

export { runProcess as process };
