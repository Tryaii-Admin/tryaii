/**
 * `tryaii apps` -- tryaii.com apps and app keys (shared/monitor/API.md §5,
 * shared/monitor/SPEC.md §7).
 *
 * Verb-peeling like `tryaii monitor`: `appsMain(argv, io)` receives
 * everything after the command name and resolves to the exit code.
 * `create`, `list` and `keys` act as the signed-in user (the device-flow JWT
 * from `tryaii login`); `check` validates an app key (`TRYAII_API_KEY` or
 * `--key`). Every message, table and exit code is byte-identical to the
 * Python CLI (packages/python/tryaii/cli/apps.py) and frozen by the `apps`
 * fixture suite, which runs against canned HTTP answers (env
 * `TRYAII_HTTP_FIXTURE`, SPEC.md §6.3).
 */

import { AuthNetworkError } from './auth/transport.js';
import { currentSession, ensureAccessToken } from './auth/flow.js';
import { formatFixed } from './cachelint/util/halfEven.js';
import * as api from './monitor/api.js';
import { formatTs } from './monitor/events.js';
import * as privacy from './monitor/privacy.js';
import * as monitorStore from './monitor/store.js';

export const VERBS = ['create', 'list', 'keys', 'check'] as const;
const HELP_HINT = 'Run "tryaii help apps".';
export const KEY_WARNING =
  'warning: this is the only time the full key is shown; store it now (e.g. as TRYAII_API_KEY)';
const DEFAULT_KEY_LABEL = 'default';
const NAME_MAX = 64;

const VALUE_FLAGS: Record<string, string> = { '--label': 'label', '--revoke': 'revoke', '--key': 'key' };
const SWITCH_FLAGS: Record<string, string> = { '--json': 'json', '--new': 'new' };
// Which options each verb accepts (anything else is an unknown option).
const VERB_OPTIONS: Record<string, ReadonlySet<string>> = {
  create: new Set(['json']),
  list: new Set(['json']),
  keys: new Set(['json', 'new', 'label', 'revoke']),
  check: new Set(['json', 'key']),
};

/** Usage error: message to stderr, exit 2. */
export class AppsUsageError extends Error {}

/** Server/network/auth failure: message to stderr, exit 1. */
export class AppsRuntimeError extends Error {}

type Opts = Record<string, string | boolean>;

export interface AppsIo {
  out: (text: string) => void;
  err: (text: string) => void;
  /** Package version (auth User-Agent). */
  version: string;
  env?: NodeJS.ProcessEnv;
}

function parse(argv: string[], verb: string): [string[], Opts] {
  const positionals: string[] = [];
  const opts: Opts = {};
  const allowed = VERB_OPTIONS[verb];
  let i = 0;
  while (i < argv.length) {
    const arg = argv[i];
    if (arg === '--') {
      positionals.push(...argv.slice(i + 1));
      break;
    }
    const eq = arg.indexOf('=');
    const name = eq >= 0 ? arg.slice(0, eq) : arg;
    if (name in VALUE_FLAGS && allowed.has(VALUE_FLAGS[name])) {
      let value: string;
      if (eq >= 0) {
        value = arg.slice(eq + 1);
      } else {
        if (i + 1 >= argv.length) throw new AppsUsageError(`${name} needs a value. ${HELP_HINT}`);
        i += 1;
        value = argv[i];
      }
      opts[VALUE_FLAGS[name]] = value;
    } else if (arg in SWITCH_FLAGS && allowed.has(SWITCH_FLAGS[arg])) {
      opts[SWITCH_FLAGS[arg]] = true;
    } else if (arg.startsWith('-') && arg !== '-') {
      throw new AppsUsageError(`unknown option: ${arg}. ${HELP_HINT}`);
    } else {
      positionals.push(arg);
    }
    i += 1;
  }
  return [positionals, opts];
}

function str(opts: Opts, key: string): string | undefined {
  const value = opts[key];
  return typeof value === 'string' ? value : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// --- HTTP ---------------------------------------------------------------------

class Ctx {
  constructor(readonly io: AppsIo) {}

  get env(): NodeJS.ProcessEnv {
    return this.io.env ?? process.env;
  }

  async call(method: string, base: string, path: string, bearer: string, body: unknown = null): Promise<api.ApiResponse> {
    try {
      return await api.request(method, `${base}${path}`, body, { bearer, env: this.env });
    } catch (err) {
      if (err instanceof api.ApiNetworkError) throw new AppsRuntimeError(`could not reach ${base}`);
      throw err;
    }
  }

  /**
   * `[apiUrl, accessToken]` of the signed-in user, refreshing the access
   * token when it is about to expire (the rotated session is saved). Throws
   * AppsRuntimeError when signed out or the session ended.
   */
  async session(): Promise<[string, string]> {
    const deps = { version: this.io.version, env: this.env };
    const session = currentSession(deps);
    if (session === null) throw new AppsRuntimeError('not signed in (run tryaii login)');
    let token: string;
    try {
      token = await ensureAccessToken(session, deps);
    } catch (err) {
      if (err instanceof AuthNetworkError) throw new AppsRuntimeError(`could not reach ${session.apiUrl}`);
      throw new AppsRuntimeError('session expired or revoked (run tryaii login)');
    }
    return [session.apiUrl, token];
  }

  async userCall(method: string, path: string, body: unknown = null): Promise<api.ApiResponse> {
    const [base, token] = await this.session();
    const resp = await this.call(method, base, path, token, body);
    if (resp.status === 401) throw new AppsRuntimeError('session expired or revoked (run tryaii login)');
    return resp;
  }

  dump(obj: unknown): void {
    this.io.out(JSON.stringify(obj, null, 2) + '\n');
  }

  printNewKey(key: string): void {
    this.io.out(`key: ${key}\n`);
    this.io.err(KEY_WARNING + '\n');
  }
}

/** `<error>[: <detail>]` from an error body, else the HTTP status. */
function serverError(resp: api.ApiResponse): string {
  if (resp.error) return resp.detail ? `${resp.error}: ${resp.detail}` : resp.error;
  return `unexpected response (HTTP ${resp.status})`;
}

// --- rendering ----------------------------------------------------------------

function table(headers: readonly string[], rows: string[][], right: ReadonlySet<string>): string[] {
  const widths = headers.map((h) => h.length);
  for (const row of rows) {
    row.forEach((cell, i) => {
      widths[i] = Math.max(widths[i], cell.length);
    });
  }
  const out: string[] = [];
  for (const row of [headers as string[], ...rows]) {
    const cells = row.map((cell, i) =>
      right.has(headers[i]) && row !== headers ? cell.padStart(widths[i]) : cell.padEnd(widths[i]),
    );
    out.push(cells.join('  ').replace(/\s+$/, ''));
  }
  return out;
}

function money(value: unknown): string {
  return typeof value === 'number' ? formatFixed(value, 4) : '-';
}

function int(value: unknown): string {
  return typeof value === 'number' ? String(Math.trunc(value)) : '-';
}

function text(value: unknown): string {
  return typeof value === 'string' && value ? value : '-';
}

// --- verbs --------------------------------------------------------------------

async function verbCreate(ctx: Ctx, argv: string[]): Promise<void> {
  const [positionals, opts] = parse(argv, 'create');
  if (positionals.length !== 1) throw new AppsUsageError(`usage: tryaii apps create <name>. ${HELP_HINT}`);
  const name = positionals[0].trim();
  if (!name || name.length > NAME_MAX) throw new AppsUsageError(`app name must be 1-${NAME_MAX} characters`);
  const resp = await ctx.userCall('POST', '/v1/apps', { name });
  if (resp.status === 409 && resp.error === 'name_taken') {
    throw new AppsRuntimeError(`name_taken: you already have an app named '${name}'`);
  }
  if (resp.status !== 201 || !isRecord(resp.body)) throw new AppsRuntimeError(serverError(resp));
  const app = resp.body;
  const appId = app.app_id;
  if (!opts.json) ctx.io.out(`created app ${pyStr(appId)} (${pyStr('name' in app ? app.name : name)})\n`);
  const keyResp = await ctx.userCall('POST', `/v1/apps/${pyStr(appId)}/keys`, { label: DEFAULT_KEY_LABEL });
  if (keyResp.status !== 201 || !isRecord(keyResp.body) || typeof keyResp.body.key !== 'string') {
    if (opts.json) ctx.dump({ app, key: null });
    throw new AppsRuntimeError(
      `app created but no key: ${serverError(keyResp)} (retry with tryaii apps keys ${pyStr(appId)} --new)`,
    );
  }
  if (opts.json) {
    ctx.dump({ app, key: keyResp.body });
    ctx.io.err(KEY_WARNING + '\n');
    return;
  }
  ctx.printNewKey(keyResp.body.key);
  await showNoticeAfterCreate(ctx);
}

/**
 * POST /v1/monitor/consent for the signed-in user (API.md §7.2): call it only
 * right after the Data Notice was printed to a terminal. Resolves to true when
 * recorded; never rejects.
 */
async function recordConsentWith(ctx: Ctx, source: string): Promise<boolean> {
  try {
    const resp = await ctx.userCall('POST', '/v1/monitor/consent', {
      notice_version: privacy.NOTICE_VERSION,
      source,
    });
    return resp.status === 204;
  } catch {
    return false;
  }
}

/** `recordConsentWith` for callers outside `tryaii apps` (the monitor CLI). */
export function recordConsent(source: string): Promise<boolean> {
  const silent = (): void => undefined;
  return recordConsentWith(new Ctx({ out: silent, err: silent, version: api.sdkVersion() }), source);
}

/**
 * `apps create` starts uploads: show the Data Notice to the person (a
 * terminal only) and record that it was shown.
 */
async function showNoticeAfterCreate(ctx: Ctx): Promise<void> {
  if (!privacy.canShowNotice(process.stderr, ctx.env)) return;
  ctx.io.err('\n' + privacy.NOTICE_TEXT);
  try {
    privacy.markNoticeShown(monitorStore.monitorRoot(monitorStore.dataDirFromEnv(ctx.env)), formatTs(Date.now()));
  } catch {
    // best effort
  }
  await recordConsentWith(ctx, 'cli');
}

/** Python's str() for the JSON values an API body holds. */
function pyStr(value: unknown): string {
  if (value === null || value === undefined) return 'None';
  if (value === true) return 'True';
  if (value === false) return 'False';
  return String(value);
}

const APP_COLUMNS = ['app id', 'name', 'keys', 'calls 24h', 'cost 24h', 'last event'];
const APP_RIGHT = new Set(['keys', 'calls 24h', 'cost 24h']);

async function verbList(ctx: Ctx, argv: string[]): Promise<void> {
  const [positionals, opts] = parse(argv, 'list');
  if (positionals.length) throw new AppsUsageError(`unexpected argument: ${positionals[0]}. ${HELP_HINT}`);
  const resp = await ctx.userCall('GET', '/v1/apps');
  const apps = isRecord(resp.body) ? resp.body.apps : null;
  if (resp.status !== 200 || !Array.isArray(apps)) throw new AppsRuntimeError(serverError(resp));
  if (opts.json) {
    ctx.dump(apps);
    return;
  }
  if (!apps.length) {
    ctx.io.out('no apps yet; create one with: tryaii apps create <name>\n');
    return;
  }
  const rows = apps.map((raw) => {
    const app = isRecord(raw) ? raw : {};
    return [
      text(app.app_id),
      text(app.name),
      int(app.keys_active),
      int(app.calls_24h),
      money(app.cost_24h_usd),
      text(app.last_event_at),
    ];
  });
  ctx.io.out(table(APP_COLUMNS, rows, APP_RIGHT).join('\n') + '\n');
}

const KEY_COLUMNS = ['key', 'label', 'created', 'last used', 'status'];

function keyPrefix(key: Record<string, unknown>): string {
  let prefix = key.prefix;
  if (typeof prefix !== 'string' || !prefix) prefix = `tryaii_${'key_id' in key ? pyStr(key.key_id) : ''}_`;
  return `${String(prefix)}...`;
}

async function verbKeys(ctx: Ctx, argv: string[]): Promise<void> {
  const [positionals, opts] = parse(argv, 'keys');
  if (positionals.length !== 1) {
    throw new AppsUsageError(
      `usage: tryaii apps keys <app_id> [--new [--label <label>] | --revoke <key_id>]. ${HELP_HINT}`,
    );
  }
  const appId = positionals[0].trim();
  if (!appId) throw new AppsUsageError('app id must not be empty');
  if (opts.new && 'revoke' in opts) throw new AppsUsageError('--new and --revoke cannot be combined');
  if ('label' in opts && !opts.new) throw new AppsUsageError('--label needs --new');
  if (opts.new) {
    const label = str(opts, 'label');
    const body = label ? { label } : {};
    const resp = await ctx.userCall('POST', `/v1/apps/${appId}/keys`, body);
    if (resp.status === 409 && resp.error === 'key_limit') {
      throw new AppsRuntimeError(
        `key_limit: an app has at most 2 active keys; revoke one with tryaii apps keys ${appId} --revoke <key_id>`,
      );
    }
    if (resp.status !== 201 || !isRecord(resp.body) || typeof resp.body.key !== 'string') {
      throw new AppsRuntimeError(serverError(resp));
    }
    if (opts.json) {
      ctx.dump(resp.body);
      ctx.io.err(KEY_WARNING + '\n');
      return;
    }
    ctx.io.out(`new key ${pyStr(resp.body.key_id)} for app ${appId}\n`);
    ctx.printNewKey(resp.body.key);
    return;
  }
  if ('revoke' in opts) {
    const keyId = (str(opts, 'revoke') ?? '').trim();
    if (!keyId) throw new AppsUsageError(`--revoke needs a value. ${HELP_HINT}`);
    const resp = await ctx.userCall('DELETE', `/v1/apps/${appId}/keys/${keyId}`);
    if (resp.status !== 204) throw new AppsRuntimeError(serverError(resp));
    if (opts.json) {
      ctx.dump({ app_id: appId, key_id: keyId, revoked: true });
      return;
    }
    ctx.io.out(`revoked key ${keyId} (app ${appId})\n`);
    return;
  }
  const resp = await ctx.userCall('GET', `/v1/apps/${appId}/keys`);
  const keys = isRecord(resp.body) ? resp.body.keys : null;
  if (resp.status !== 200 || !Array.isArray(keys)) throw new AppsRuntimeError(serverError(resp));
  if (opts.json) {
    ctx.dump(keys);
    return;
  }
  if (!keys.length) {
    ctx.io.out(`no keys for app ${appId}\n`);
    return;
  }
  const rows = keys.map((raw) => {
    const key = isRecord(raw) ? raw : {};
    const status = key.revoked_at ? 'revoked' : 'active';
    return [keyPrefix(key), text(key.label), text(key.created_at), text(key.last_used_at), status];
  });
  ctx.io.out(table(KEY_COLUMNS, rows, new Set()).join('\n') + '\n');
}

async function verbCheck(ctx: Ctx, argv: string[]): Promise<void> {
  const [positionals, opts] = parse(argv, 'check');
  if (positionals.length) throw new AppsUsageError(`unexpected argument: ${positionals[0]}. ${HELP_HINT}`);
  const flag = str(opts, 'key');
  const key = flag !== undefined ? flag.trim() : api.apiKey(ctx.env);
  if (!key) throw new AppsUsageError('no app key: set TRYAII_API_KEY or pass --key');
  if (!api.isAppKey(key)) throw new AppsUsageError('not an app key (expected tryaii_<key_id>_<secret>)');
  const base = api.apiUrl(ctx.env);
  const resp = await ctx.call('GET', base, '/v1/apps/keys/validate', key);
  if (resp.status === 401) throw new AppsRuntimeError('invalid key');
  if (resp.status !== 200 || !isRecord(resp.body)) throw new AppsRuntimeError(serverError(resp));
  const info = resp.body;
  if (opts.json) {
    ctx.dump(info);
    return;
  }
  const scopes = Array.isArray(info.scopes) ? info.scopes : [];
  const expires = info.expires_at;
  ctx.io.out(
    `app: ${text(info.app_name)} (${text(info.app_id)})\n` +
      `key: ${text(info.key_id)}\n` +
      `scopes: ${scopes.map((s) => pyStr(s)).join(', ') || '-'}\n` +
      `expires: ${typeof expires === 'string' && expires ? expires : 'never'}\n`,
  );
}

const VERB_HANDLERS: Record<string, (ctx: Ctx, argv: string[]) => Promise<void>> = {
  create: verbCreate,
  list: verbList,
  keys: verbKeys,
  check: verbCheck,
};

/** Verb dispatcher for `tryaii apps` (mirrors cmd_apps in apps.py); resolves to the exit code. */
export async function appsMain(argv: string[], io: AppsIo): Promise<number> {
  const verb = argv[0];
  if (verb === undefined) {
    io.err(`error: missing apps verb. ${HELP_HINT}\n`);
    return 2;
  }
  const handler = Object.prototype.hasOwnProperty.call(VERB_HANDLERS, verb) ? VERB_HANDLERS[verb] : undefined;
  if (!handler) {
    io.err(`error: unknown apps verb: ${verb}. ${HELP_HINT}\n`);
    return 2;
  }
  try {
    await handler(new Ctx(io), argv.slice(1));
    return 0;
  } catch (err) {
    io.err(`error: ${err instanceof Error ? err.message : String(err)}\n`);
    return err instanceof AppsUsageError ? 2 : 1;
  }
}
