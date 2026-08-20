/**
 * Submission transport (SPEC.md §5). Mirrors designpartner/transport.py.
 *
 * Native fetch only (Node >= 18). One attempt, 10s timeout, and EVERY
 * failure (network error, timeout, non-2xx) collapses into a single false
 * outcome so the CLI message stays byte-identical across platforms and
 * SDKs.
 */

export const DEFAULT_URL = 'https://api.tryaii.com/v1/design-partners';
export const TIMEOUT_MS = 10000;

/**
 * Env override first (the TRYAII_DESIGNPARTNER_URL convention), else the
 * default endpoint. An empty env value falls through.
 */
export function effectiveUrl(): string {
  return process.env.TRYAII_DESIGNPARTNER_URL || DEFAULT_URL;
}

export interface SendOptions {
  fetchFn?: typeof fetch;
  timeoutMs?: number;
}

/** POST the exact saved submission bytes. True only on a 2xx response. */
export async function sendSubmission(
  url: string,
  body: string,
  version: string,
  opts: SendOptions = {},
): Promise<boolean> {
  const fetchFn = opts.fetchFn ?? fetch;
  const timeoutMs = opts.timeoutMs ?? TIMEOUT_MS;
  try {
    const response = await fetchFn(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'User-Agent': `tryaii/${version}`,
      },
      body,
      signal: AbortSignal.timeout(timeoutMs),
    });
    return response.ok;
  } catch {
    // SPEC §5: one collapsed failure branch
    return false;
  }
}
