/**
 * Device-authorization flow and session helpers
 * (docs/auth/CONTRACT-auth-v1.md sections 1, 4 and 6).
 *
 * Every function takes an AuthDeps bag so tests can inject fetch, sleep,
 * the clock and the environment without mocking globals.
 */

import {
  AuthCancelledError,
  AuthProtocolError,
  CLIENT_ID,
  requestJson,
  sessionApiUrl,
} from './transport.js';
import {
  AuthUser,
  Credentials,
  CREDENTIALS_VERSION,
  isoUtc,
  loadCredentials,
  parseIso,
  saveCredentials,
} from './store.js';

export const DEVICE_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:device_code';
/** Refresh when the access token expires within this window. */
export const REFRESH_SKEW_MS = 60_000;
/** Seconds added to the polling interval on each slow_down. */
export const SLOW_DOWN_STEP_S = 5;

export interface AuthDeps {
  /** Package version: User-Agent and client.version. */
  version: string;
  fetchFn?: typeof fetch;
  /** Resolves after `ms`; rejects with AuthCancelledError if `signal` aborts. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  /** Wall clock in epoch ms. */
  now?: () => number;
  env?: NodeJS.ProcessEnv;
  platform?: string;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export interface DeviceCode {
  device_code: string;
  user_code: string;
  verification_uri: string;
  verification_uri_complete: string;
  expires_in: number;
  interval: number;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  refresh_token: string;
  refresh_expires_in: number;
  user: AuthUser;
  entitlements: string[];
}

export function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new AuthCancelledError());
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new AuthCancelledError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function nowOf(deps: AuthDeps): number {
  return deps.now ? deps.now() : Date.now();
}

function envOf(deps: AuthDeps): NodeJS.ProcessEnv {
  return deps.env ?? process.env;
}

function transportOpts(deps: AuthDeps) {
  return {
    fetchFn: deps.fetchFn,
    version: deps.version,
    signal: deps.signal,
    timeoutMs: deps.timeoutMs,
  };
}

/** POST /v1/auth/device/code. */
export async function startDevice(apiUrl: string, deps: AuthDeps): Promise<DeviceCode> {
  const data = await requestJson(
    apiUrl,
    {
      method: 'POST',
      path: '/v1/auth/device/code',
      body: {
        client_id: CLIENT_ID,
        client: {
          sdk: 'node',
          version: deps.version,
          os: deps.platform ?? process.platform,
        },
      },
    },
    transportOpts(deps),
  );
  return data as unknown as DeviceCode;
}

/** `value` when it is a finite number > 0, else `fallback` (same as Python). */
function positiveOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback;
}

/**
 * Poll POST /v1/auth/token until the grant resolves (contract section 7,
 * item 17). `interval` / `expires_in` that are missing, non-numeric or
 * <= 0 default to 5 / 600. Sleeps `interval` seconds before each poll;
 * `authorization_pending` keeps polling, `slow_down` adds 5 s to the
 * interval. The deadline is checked after each answer, so the client polls
 * exactly once more after the local deadline (an approval in the last
 * interval still wins); a pending/slow_down answer -- or an `invalid_grant`
 * -- received after the deadline throws `expired_token`. Every other error
 * code throws AuthProtocolError as is.
 */
export async function pollForToken(
  apiUrl: string,
  device: DeviceCode,
  deps: AuthDeps,
): Promise<TokenResponse> {
  const sleep = deps.sleep ?? defaultSleep;
  let interval = positiveOr(device.interval, 5);
  const deadline = nowOf(deps) + positiveOr(device.expires_in, 600) * 1000;

  for (;;) {
    await sleep(interval * 1000, deps.signal);
    if (deps.signal?.aborted) throw new AuthCancelledError();
    try {
      const data = await requestJson(
        apiUrl,
        {
          method: 'POST',
          path: '/v1/auth/token',
          body: {
            grant_type: DEVICE_GRANT_TYPE,
            device_code: device.device_code,
            client_id: CLIENT_ID,
          },
        },
        transportOpts(deps),
      );
      return data as unknown as TokenResponse;
    } catch (error) {
      if (!(error instanceof AuthProtocolError)) throw error;
      const expired = nowOf(deps) >= deadline;
      const retriable = error.code === 'authorization_pending' || error.code === 'slow_down';
      if (expired && (retriable || error.code === 'invalid_grant')) {
        throw new AuthProtocolError('expired_token', error.status);
      }
      if (!retriable) throw error;
      if (error.code === 'slow_down') interval += SLOW_DOWN_STEP_S;
    }
  }
}

/** POST /v1/auth/token with the refresh grant. */
export async function refreshTokens(
  apiUrl: string,
  refreshToken: string,
  deps: AuthDeps,
): Promise<TokenResponse> {
  const data = await requestJson(
    apiUrl,
    {
      method: 'POST',
      path: '/v1/auth/token',
      body: { grant_type: 'refresh_token', refresh_token: refreshToken, client_id: CLIENT_ID },
    },
    transportOpts(deps),
  );
  return data as unknown as TokenResponse;
}

/** POST /v1/auth/revoke, best effort: never throws. */
export async function revoke(apiUrl: string, refreshToken: string, deps: AuthDeps): Promise<void> {
  try {
    await requestJson(
      apiUrl,
      {
        method: 'POST',
        path: '/v1/auth/revoke',
        body: { refresh_token: refreshToken, client_id: CLIENT_ID },
      },
      transportOpts(deps),
    );
  } catch {
    // RFC 7009 + contract: logout ignores revoke failures
  }
}

/** GET /v1/auth/me. Returns the parsed body (key order preserved). */
export async function me(
  apiUrl: string,
  accessToken: string,
  deps: AuthDeps,
): Promise<Record<string, unknown>> {
  return requestJson(
    apiUrl,
    { method: 'GET', path: '/v1/auth/me', bearer: accessToken },
    transportOpts(deps),
  );
}

/** Build the credentials record from a token response. */
export function credentialsFromToken(
  apiUrl: string,
  token: TokenResponse,
  nowMs: number,
  createdAt?: string,
): Credentials {
  const user = (token.user ?? {}) as Partial<AuthUser>;
  return {
    version: CREDENTIALS_VERSION,
    api_url: apiUrl,
    user: {
      id: String(user.id ?? ''),
      email: String(user.email ?? ''),
      name: String(user.name ?? ''),
    },
    entitlements: Array.isArray(token.entitlements) ? token.entitlements.map(String) : [],
    refresh_token: token.refresh_token,
    refresh_expires_at: isoUtc(nowMs + Number(token.refresh_expires_in ?? 0) * 1000),
    access_token: token.access_token,
    access_expires_at: isoUtc(nowMs + Number(token.expires_in ?? 0) * 1000),
    created_at: createdAt ?? isoUtc(nowMs),
  };
}

export interface Session {
  apiUrl: string;
  refreshToken: string;
  accessToken: string | null;
  accessExpiresAtMs: number;
  credentials: Credentials;
}

/**
 * The stored session, or null when not logged in. It always talks to the
 * api_url it was issued by -- TRYAII_API_URL is for login only and
 * TRYAII_TOKEN is not supported (contract section 7, item 13).
 */
export function currentSession(deps: AuthDeps): Session | null {
  const creds = loadCredentials(envOf(deps));
  if (creds === null) return null;
  return {
    apiUrl: sessionApiUrl(creds.api_url),
    refreshToken: creds.refresh_token,
    accessToken: creds.access_token || null,
    accessExpiresAtMs: creds.access_expires_at ? parseIso(creds.access_expires_at) : 0,
    credentials: creds,
  };
}

/** True when the access token is missing or expires within 60 s. */
export function needsRefresh(session: Session, deps: AuthDeps): boolean {
  return !(session.accessToken && session.accessExpiresAtMs - nowOf(deps) > REFRESH_SKEW_MS);
}

/**
 * Return a usable access token, refreshing when it is missing or expires
 * within 60 s. The rotated session is persisted (keeping created_at).
 */
export async function ensureAccessToken(session: Session, deps: AuthDeps): Promise<string> {
  if (!needsRefresh(session, deps)) return session.accessToken as string;
  const token = await refreshTokens(session.apiUrl, session.refreshToken, deps);
  const issuedAt = nowOf(deps);
  const creds = credentialsFromToken(session.apiUrl, token, issuedAt, session.credentials.created_at);
  if (!token.refresh_token) creds.refresh_token = session.refreshToken;
  saveCredentials(creds, envOf(deps));
  session.credentials = creds;
  session.refreshToken = creds.refresh_token;
  session.accessToken = token.access_token;
  session.accessExpiresAtMs = issuedAt + Number(token.expires_in ?? 0) * 1000;
  return token.access_token;
}
