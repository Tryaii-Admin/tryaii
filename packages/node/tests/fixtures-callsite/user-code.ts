// Stand-in for user code: NOT under src/, NOT under node_modules.
import { captureCallSite, type CallSite } from '../../src/callsite.js';

export function purchaseItem(): CallSite | null {
  return captureCallSite();
}

export const anonymous = (): CallSite | null => captureCallSite();

export async function afterAwait(): Promise<CallSite | null> {
  await new Promise((resolve) => setTimeout(resolve, 1));
  return captureCallSite();
}

export function viaTimer(): Promise<CallSite | null> {
  return new Promise((resolve) => setTimeout(() => resolve(captureCallSite()), 1));
}
