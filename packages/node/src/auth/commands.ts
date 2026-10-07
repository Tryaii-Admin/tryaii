/**
 * login / logout / whoami command bodies (docs/auth/CONTRACT-auth-v1.md
 * sections 6-7). Output strings are byte-identical to the Python CLI. Each
 * runner writes through injected stdout/stderr writers and returns the
 * exit code, so tests drive them without spawning a process.
 */

import {
  cachedFull,
  clearCache,
  downloadAfterLogin,
  endSession,
  MSG_LOGIN_DOWNLOAD_FAILED,
  readState,
  routableCount,
} from '../catalog/client.js';
import {
  AuthDeps,
  credentialsFromToken,
  currentSession,
  ensureAccessToken,
  me,
  pollForToken,
  revoke,
  startDevice,
} from './flow.js';
import {
  credentialsFileExists,
  deleteCredentials,
  loadCredentials,
  saveCredentials,
} from './store.js';
import {
  AuthCancelledError,
  AuthNetworkError,
  AuthProtocolError,
  AuthRateLimitedError,
  effectiveApiUrl,
  sessionApiUrl,
} from './transport.js';

export interface CommandIO {
  stdout: (text: string) => void;
  stderr: (text: string) => void;
}

const SESSION_ENDED = 'Your session has ended. Run: tryaii login\n';
const RATE_LIMITED = 'Too many sign-in attempts. Wait a minute and try again.\n';
const CANNOT_DELETE = 'Could not remove the credentials file.\n';

function unreachable(apiUrl: string): string {
  return `Could not reach ${apiUrl}. Check your connection and try again.\n`;
}

function unexpected(apiUrl: string, code: string): string {
  return `Unexpected response from ${apiUrl}: ${code}.\n`;
}

export async function runLogin(io: CommandIO, deps: AuthDeps): Promise<number> {
  const env = deps.env ?? process.env;
  // TRYAII_API_URL applies to login only (contract section 7, item 13).
  const apiUrl = effectiveApiUrl(env);
  const previous = loadCredentials(env);
  if (previous !== null) {
    io.stdout(
      `Already logged in as ${previous.user?.email ?? ''}. ` +
        'Signing in again replaces that session.\n',
    );
  }

  try {
    const device = await startDevice(apiUrl, deps);
    io.stdout(
      'To sign in, open this URL in a browser:\n' +
        `  ${device.verification_uri}\n` +
        `and enter the code: ${device.user_code}\n` +
        '\n' +
        `Or open directly: ${device.verification_uri_complete}\n` +
        '\n' +
        'Waiting for approval (expires in 10 minutes, Ctrl+C to cancel)...\n',
    );
    const token = await pollForToken(apiUrl, device, deps);
    const now = deps.now ? deps.now() : Date.now();
    saveCredentials(credentialsFromToken(apiUrl, token, now), env);
    if (previous !== null && previous.refresh_token !== token.refresh_token) {
      // Old session revoked at the host that issued it, best-effort, after
      // success (never fails login).
      await revoke(sessionApiUrl(previous.api_url), previous.refresh_token, {
        ...deps,
        signal: undefined,
      });
    }
    io.stdout(`Logged in as ${token.user?.email ?? ''}.\n`);
    // Catalog contract section 5: download the full catalog right away
    // (stdout either way; login still exits 0).
    let bundle = null;
    try {
      bundle = await downloadAfterLogin({ ...deps, signal: undefined });
    } catch {
      bundle = null;
    }
    io.stdout(
      bundle
        ? `Downloaded the full catalog (${routableCount(bundle)} models).\n`
        : `${MSG_LOGIN_DOWNLOAD_FAILED}\n`,
    );
    return 0;
  } catch (error) {
    if (error instanceof AuthCancelledError) {
      io.stderr('Login cancelled.\n');
      return 130;
    }
    if (error instanceof AuthRateLimitedError) {
      io.stderr(RATE_LIMITED);
      return 1;
    }
    if (error instanceof AuthProtocolError) {
      if (error.code === 'access_denied') io.stderr('Login denied in the browser.\n');
      else if (error.code === 'expired_token') {
        io.stderr('The code expired. Run tryaii login again.\n');
      } else io.stderr(`Login failed: ${error.code}.\n`);
      return 1;
    }
    if (error instanceof AuthNetworkError) {
      io.stderr(unreachable(apiUrl));
      return 1;
    }
    throw error;
  }
}

export async function runLogout(io: CommandIO, deps: AuthDeps): Promise<number> {
  const env = deps.env ?? process.env;
  if (!credentialsFileExists(env)) {
    clearCache(env);
    io.stdout('Not logged in.\n');
    return 0;
  }
  // An unreadable or foreign-schema file is still removed (nothing to revoke).
  const creds = loadCredentials(env);
  if (creds !== null) {
    await revoke(sessionApiUrl(creds.api_url), creds.refresh_token, deps);
  }
  // Catalog contract section 5: the full catalog and its state go too, also
  // when the revoke failed.
  clearCache(env);
  try {
    deleteCredentials(env);
  } catch {
    // Item 19: never claim "Logged out." while the token is still on disk.
    io.stderr(CANNOT_DELETE);
    return 1;
  }
  io.stdout('Logged out.\n');
  return 0;
}

export async function runWhoami(
  io: CommandIO,
  deps: AuthDeps,
  opts: { json: boolean },
): Promise<number> {
  const env = deps.env ?? process.env;
  const session = currentSession(deps);
  if (session === null) {
    io.stderr('Not logged in. Run: tryaii login\n');
    return 1;
  }
  // A stored session always talks to the host that issued it (item 13).
  const apiUrl = session.apiUrl;

  const fail = (error: unknown, sessionEnded: boolean): number => {
    if (error instanceof AuthNetworkError) {
      io.stderr(unreachable(apiUrl));
    } else if (sessionEnded) {
      // Refresh rejected (invalid_grant) or /me 401: the session is gone --
      // and with it the full catalog cache (catalog contract section 5).
      endSession(env);
      io.stderr(SESSION_ENDED);
    } else if (error instanceof AuthProtocolError || error instanceof AuthRateLimitedError) {
      // Item 14: any other error keeps the file.
      io.stderr(unexpected(apiUrl, error.code));
    } else {
      throw error;
    }
    return 1;
  };

  let accessToken: string;
  try {
    accessToken = await ensureAccessToken(session, deps);
  } catch (error) {
    return fail(error, error instanceof AuthProtocolError && error.code === 'invalid_grant');
  }

  let body: Record<string, unknown>;
  try {
    body = await me(apiUrl, accessToken, deps);
  } catch (error) {
    return fail(error, error instanceof AuthProtocolError && error.status === 401);
  }

  if (opts.json) {
    io.stdout(JSON.stringify(body, null, 2) + '\n');
  } else {
    const user = (body.user ?? {}) as Record<string, unknown>;
    const ents = Array.isArray(body.entitlements) ? body.entitlements.map(String) : [];
    // Catalog contract section 5: third line, from the local cache only.
    const cached = cachedFull(readState(env), env);
    io.stdout(
      `Logged in as ${String(user.email ?? '')} (${String(user.name ?? '')})\n` +
        `Entitlements: ${ents.length ? ents.join(', ') : 'none'}\n` +
        (cached
          ? `Catalog: full, release ${cached.version} (${routableCount(cached)} models)\n`
          : 'Catalog: not downloaded yet\n'),
    );
  }
  return 0;
}
