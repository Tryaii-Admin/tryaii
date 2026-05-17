/**
 * Smoke tests for dreMiddleware.
 *
 * Uses the same tryaii-dre fixture as client.test.ts — the middleware's only
 * work is to call DREClient.route() and set response headers, so we can
 * exercise its full surface without a real Express server.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { dreMiddleware } from '../src/middleware.js';
import {
  clearLastRouteCall,
  getLastRouteCall,
  resetFakeRouteResult,
  setFakeRouteResult,
} from './fixtures/tryaii-dre-mock.js';

// Minimal stand-in for express.Request/Response/NextFunction so tests don't
// need a running server. Only the fields the middleware actually touches.
interface FakeRes {
  headers: Record<string, string>;
  setHeader: (name: string, value: string | number) => void;
}

function makeReq(body: unknown): unknown {
  return { body };
}

function makeRes(): FakeRes {
  const res: FakeRes = {
    headers: {},
    setHeader(name, value) {
      this.headers[name] = String(value);
    },
  };
  return res;
}

beforeEach(() => {
  resetFakeRouteResult();
  clearLastRouteCall();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('dreMiddleware', () => {
  it('sets X-DRE-Model and X-DRE-Score headers when the body has a prompt', async () => {
    const mw = dreMiddleware();
    const req = makeReq({ prompt: 'Write a sort' });
    const res = makeRes();
    const next = vi.fn();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (mw as any)(req, res, next);

    expect(res.headers['X-DRE-Model']).toBe('gpt-4o');
    expect(Number(res.headers['X-DRE-Score'])).toBeCloseTo(0.92);
    expect(next).toHaveBeenCalledTimes(1);
  });

  it('honors a custom header prefix and prompt field', async () => {
    const mw = dreMiddleware({ headerPrefix: 'X-My', promptField: 'question' });
    const req = makeReq({ question: 'hello' });
    const res = makeRes();
    const next = vi.fn();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (mw as any)(req, res, next);

    expect(res.headers['X-My-Model']).toBe('gpt-4o');
    expect(res.headers['X-My-Score']).toBeDefined();
    expect(next).toHaveBeenCalledTimes(1);
  });

  it('calls next() and sets no headers when the body has no prompt', async () => {
    const mw = dreMiddleware();
    const req = makeReq({ other: 'field' });
    const res = makeRes();
    const next = vi.fn();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (mw as any)(req, res, next);

    expect(res.headers).toEqual({});
    expect(next).toHaveBeenCalledTimes(1);
    expect(getLastRouteCall()).toBeNull();
  });

  it('invokes onError and still calls next() when the router throws', async () => {
    setFakeRouteResult(new Error('boom'));
    const onError = vi.fn();
    const mw = dreMiddleware({ onError });
    const req = makeReq({ prompt: 'hello' });
    const res = makeRes();
    const next = vi.fn();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (mw as any)(req, res, next);

    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0] as Error;
    expect(err.message).toBe('boom');
    // Headers were NOT set because route() threw before setHeader.
    expect(res.headers['X-DRE-Model']).toBeUndefined();
    // But the request pipeline is never blocked.
    expect(next).toHaveBeenCalledTimes(1);
  });

  it('silently swallows errors when no onError hook is provided (default)', async () => {
    setFakeRouteResult(new Error('boom'));
    const mw = dreMiddleware(); // no onError
    const req = makeReq({ prompt: 'hello' });
    const res = makeRes();
    const next = vi.fn();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await expect((mw as any)(req, res, next)).resolves.toBeUndefined();
    expect(res.headers['X-DRE-Model']).toBeUndefined();
    expect(next).toHaveBeenCalledTimes(1);
  });

  it('does not let a broken onError hook block the pipeline', async () => {
    setFakeRouteResult(new Error('boom'));
    const mw = dreMiddleware({
      onError: () => {
        throw new Error('hook exploded');
      },
    });
    const req = makeReq({ prompt: 'hello' });
    const res = makeRes();
    const next = vi.fn();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await expect((mw as any)(req, res, next)).resolves.toBeUndefined();
    expect(next).toHaveBeenCalledTimes(1);
  });
});
