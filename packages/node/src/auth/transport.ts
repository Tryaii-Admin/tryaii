/**
 * Auth HTTP transport (docs/auth/CONTRACT-auth-v1.md section 6, "Transport").
 *
 * Native fetch only (Node >= 18), one attempt, 10 s timeout, no retries
 * (polling is the loop). Mirrors designpartner/transport.ts: the fetch
 * function is injectable, and every network failure, timeout, non-JSON
 * body and unexpected status collapses into a single AuthNetworkError so
 * the CLI message stays byte-identical across SDKs. Only a 200 is success;
 * a well-formed RFC 6749 error body (`{"error": "<code>"}`) on a 400/401
 * surfaces as an AuthProtocolError carrying the code, and a 429 as an
 * AuthRateLimitedError (contract section 7, item 15). Redirects are never
 * followed (item 16): a 3xx is just another unexpected status.
 */

export const DEFAULT_API_URL = 'https://api.tryaii.com';
export const CLIENT_ID = 'tryaii-cli';
export const TIMEOUT_MS = 10000;

/**
 * The api base for `login`: TRYAII_API_URL first (an empty value falls
 * through), else the default. Trailing slashes are dropped (same as the
 * Python SDK). A stored session never uses this (see sessionApiUrl).
 */
export function effectiveApiUrl(env: NodeJS.ProcessEnv = process.env): string {
  return (env.TRYAII_API_URL || DEFAULT_API_URL).replace(/\/+$/, '');
}

/**
 * The api base for an existing session: ALWAYS the api_url recorded in its
 * credentials file, never TRYAII_API_URL (contract section 7, item 13 --
 * the refresh token must not be sent to another host). A file without one
 * falls back to the default, not the environment.
 */
export function sessionApiUrl(storedApiUrl: unknown): string {
  if (typeof storedApiUrl === 'string' && storedApiUrl) return storedApiUrl.replace(/\/+$/, '');
  return DEFAULT_API_URL;
}

/** Network failure, timeout, non-JSON body or unexpected status. */
export class AuthNetworkError extends Error {
  constructor() {
    super('network');
    this.name = 'AuthNetworkError';
  }
}

/** HTTP 429 from any endpoint (error kind `rate_limited`). */
export class AuthRateLimitedError extends Error {
  readonly code = 'rate_limited';
  constructor() {
    super('rate_limited');
    this.name = 'AuthRateLimitedError';
  }
}

/** The server answered with an RFC 6749 error code. */
export class AuthProtocolError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, status: number) {
    super(code);
    this.name = 'AuthProtocolError';
    this.code = code;
    this.status = status;
  }
}

/** The caller aborted (Ctrl+C during login). */
export class AuthCancelledError extends Error {
  constructor() {
    super('cancelled');
    this.name = 'AuthCancelledError';
  }
}

export interface TransportOptions {
  fetchFn?: typeof fetch;
  timeoutMs?: number;
  /** User-Agent version: `tryaii/<version>`. */
  version: string;
  /** External cancellation (SIGINT during login). */
  signal?: AbortSignal;
}

export interface RequestSpec {
  method: 'GET' | 'POST';
  path: string;
  body?: unknown;
  bearer?: string;
}

function joinUrl(base: string, path: string): string {
  return base.replace(/\/+$/, '') + path;
}

/**
 * Perform one request and return the parsed JSON object of a 200 response.
 * Throws AuthProtocolError for a 400/401 with an `error` code,
 * AuthRateLimitedError for a 429, AuthCancelledError when `signal` fires,
 * and AuthNetworkError for everything else, 3xx included (same
 * classification as the Python transport).
 */
export async function requestJson(
  baseUrl: string,
  spec: RequestSpec,
  opts: TransportOptions,
): Promise<Record<string, unknown>> {
  const fetchFn = opts.fetchFn ?? fetch;
  const timeoutMs = opts.timeoutMs ?? TIMEOUT_MS;
  if (opts.signal?.aborted) throw new AuthCancelledError();

  // One controller for timeout + external cancel (AbortSignal.any is Node 20+).
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const onAbort = (): void => controller.abort();
  opts.signal?.addEventListener('abort', onAbort, { once: true });

  const headers: Record<string, string> = {
    'Accept': 'application/json',
    'User-Agent': `tryaii/${opts.version}`,
    // One connection per request, as Python's urllib does. A pooled socket
    // reused after a 5 s poll interval races the server's (typically 5 s)
    // keep-alive timeout and fails with ECONNRESET, i.e. "Could not reach".
    'Connection': 'close',
  };
  if (spec.body !== undefined) headers['Content-Type'] = 'application/json';
  if (spec.bearer !== undefined) headers['Authorization'] = `Bearer ${spec.bearer}`;

  let status: number;
  let text: string;
  try {
    const response = await fetchFn(joinUrl(baseUrl, spec.path), {
      method: spec.method,
      headers,
      body: spec.body === undefined ? undefined : JSON.stringify(spec.body),
      signal: controller.signal,
      // Never follow a redirect: it would re-send the Authorization header
      // to whatever host the Location names (item 16).
      redirect: 'manual',
    });
    status = response.status;
    text = await response.text();
  } catch {
    if (opts.signal?.aborted) throw new AuthCancelledError();
    throw new AuthNetworkError();
  } finally {
    clearTimeout(timer);
    opts.signal?.removeEventListener('abort', onAbort);
  }

  let data: unknown = undefined;
  try {
    data = JSON.parse(text);
  } catch {
    data = undefined;
  }
  const isObject = data !== null && typeof data === 'object' && !Array.isArray(data);

  if (status === 200) {
    if (!isObject) throw new AuthNetworkError();
    return data as Record<string, unknown>;
  }
  if (status === 429) throw new AuthRateLimitedError();
  if (status === 400 || status === 401) {
    const code = isObject ? (data as Record<string, unknown>).error : undefined;
    if (typeof code === 'string' && code) throw new AuthProtocolError(code, status);
  }
  throw new AuthNetworkError();
}
