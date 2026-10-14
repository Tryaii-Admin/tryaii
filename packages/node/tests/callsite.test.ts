import { describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import * as vm from 'node:vm';
import { captureCallSite } from '../src/callsite.js';
import { afterAwait, anonymous, purchaseItem, viaTimer } from './fixtures-callsite/user-code.js';

const USER_FILE = fileURLToPath(new URL('./fixtures-callsite/user-code.ts', import.meta.url));
const norm = (p: string) => p.replace(/\\/g, '/').toLowerCase();

describe('captureCallSite', () => {
  it('returns the first user frame with its function name', () => {
    const site = purchaseItem();
    expect(site).not.toBeNull();
    expect(norm(site!.file)).toBe(norm(USER_FILE));
    // Source line 5. Under vitest the frame reports esbuild's transformed
    // line (the `type` import collapses one line); source-map resolution
    // for .ts frames is cachelint's job, not this module's.
    expect([4, 5]).toContain(site!.line);
    expect(site!.col).toBeGreaterThan(0);
    expect(site!.function).toBe('purchaseItem');
  });

  it('reports an arrow function by its binding name, or null when anonymous', () => {
    const site = anonymous();
    expect(site).not.toBeNull();
    expect(norm(site!.file)).toBe(norm(USER_FILE));
    // V8 infers the name from the const binding; either is acceptable, never undefined.
    expect(site!.function === 'anonymous' || site!.function === null).toBe(true);
  });

  it('skips frames inside the package (direct call from this test file is user code)', () => {
    const site = captureCallSite();
    expect(site).not.toBeNull();
    expect(norm(site!.file)).toBe(norm(fileURLToPath(import.meta.url)));
    expect(norm(site!.file).includes('/src/')).toBe(false);
  });

  it('carries the user frame through an await and skips node: internals', async () => {
    const site = await afterAwait();
    expect(site).not.toBeNull();
    expect(norm(site!.file)).toBe(norm(USER_FILE));
    expect(site!.function).toBe('afterAwait');
  });

  it('skips timer internals when called from a setTimeout callback', async () => {
    const site = await viaTimer();
    expect(site).not.toBeNull();
    expect(norm(site!.file)).toBe(norm(USER_FILE));
  });

  it('dead-ends (null) when the first foreign frame is inside node_modules', () => {
    // A script whose V8 filename sits under node_modules stands in for a
    // wrapper library calling the SDK: the capture must yield null, never
    // the library's path and never this test file further up the stack.
    const fn = vm.compileFunction('return capture();', ['capture'], {
      filename: '/fake/node_modules/wrapper-lib/index.js',
    }) as (c: () => unknown) => unknown;
    expect(fn(captureCallSite)).toBeNull();
  });
});
