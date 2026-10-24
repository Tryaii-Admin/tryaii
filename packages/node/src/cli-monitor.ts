/**
 * `tryaii monitor` -- verb dispatcher and renderers (shared/monitor/SPEC.md §5).
 *
 * Verb-peeling like `tryaii diagnose`: `monitorMain(argv)` receives everything
 * after the command name. Every message, table and exit code here is
 * byte-identical to the Python CLI (packages/python/tryaii/cli/monitor.py)
 * and frozen by the `cli` fixture suite in shared/monitor/fixtures. Usage
 * errors are thrown as MonitorUsageError (exit 2, mapped by cli.ts); any
 * other error is a runtime failure (exit 1).
 */

import { formatFixed } from './cachelint/util/halfEven.js';
import { ENV_APP, envFromOs, MSG_BAD_MODE, MSG_MANAGED_NEEDS_MODEL, setProcessMode } from './monitor/config.js';
import { formatTs, parseTs } from './monitor/events.js';
import { statusLine as langfuseStatusLine } from './monitor/langfuse.js';
import * as privacy from './monitor/privacy.js';
import { aggregate, windowSeconds } from './monitor/status.js';
import * as store from './monitor/store.js';
import { MODES } from './monitor/winner.js';
import { readState as readModelState } from './embeddings/modelStore.js';
import { catalogInUse } from './catalog/client.js';

export const VERBS = ['status', 'set', 'apps', 'events', 'upload'] as const;
const WINDOWS = ['24h', '7d', '30d'];
const DEFAULT_EVENTS = 20;
const HELP_HINT = 'Run "tryaii help monitor".';

// Flags that take a value; everything else is a switch.
const VALUE_FLAGS: Record<string, string> = {
  '--app': 'app',
  '--since': 'since',
  '-n': 'n',
  '--now': 'now',
  '--model': 'model',
};
const SWITCH_FLAGS: Record<string, string> = { '--json': 'json' };

/** A usage error: message to stderr, exit 2. */
export class MonitorUsageError extends Error {}

type Opts = Record<string, string | boolean | undefined>;
type Env = Record<string, string | undefined>;
type Write = (text: string) => void;

function str(opts: Opts, key: string): string | undefined {
  const value = opts[key];
  return typeof value === 'string' ? value : undefined;
}

/**
 * Split `argv` into positionals and options. `--flag value` and
 * `--flag=value` both work; unknown options are a usage error so a typo
 * never silently changes a window or an app.
 */
function parse(argv: string[]): [string[], Opts] {
  const positionals: string[] = [];
  const opts: Opts = {};
  let i = 0;
  while (i < argv.length) {
    const arg = argv[i];
    if (arg === '--') {
      positionals.push(...argv.slice(i + 1));
      break;
    }
    const eq = arg.indexOf('=');
    const name = eq >= 0 ? arg.slice(0, eq) : arg;
    if (name in VALUE_FLAGS) {
      const key = VALUE_FLAGS[name];
      let value: string;
      if (eq >= 0) {
        value = arg.slice(eq + 1);
      } else {
        if (i + 1 >= argv.length) {
          throw new MonitorUsageError(`${name} needs a value. ${HELP_HINT}`);
        }
        i += 1;
        value = argv[i];
      }
      opts[key] = value;
    } else if (arg in SWITCH_FLAGS) {
      opts[SWITCH_FLAGS[arg]] = true;
    } else if (arg.startsWith('-') && arg !== '-') {
      throw new MonitorUsageError(`unknown option: ${arg}. ${HELP_HINT}`);
    } else {
      positionals.push(arg);
    }
    i += 1;
  }
  return [positionals, opts];
}

function onlySetTakesModel(opts: Opts): void {
  if ('model' in opts) throw new MonitorUsageError(`unknown option: --model. ${HELP_HINT}`);
}

function nowArg(value: string | undefined): string {
  if (value === undefined) return formatTs(Date.now());
  try {
    return formatTs(parseTs(value));
  } catch {
    throw new MonitorUsageError(
      '--now must be an ISO-8601 UTC timestamp (e.g. 2026-10-11T12:00:00Z)',
    );
  }
}

function sinceArg(value: string | undefined): string {
  if (value === undefined) return '24h';
  if (!WINDOWS.includes(value)) throw new MonitorUsageError('--since must be 24h, 7d or 30d');
  return value;
}

function countArg(value: string | undefined): number {
  if (value === undefined) return DEFAULT_EVENTS;
  // Python: int(value, 10) must round-trip to value.strip() and be > 0, i.e.
  // ASCII digits with no leading zero, sign, underscore or inner space.
  const trimmed = value.trim();
  if (!/^[1-9][0-9]*$/.test(trimmed)) throw new MonitorUsageError('-n must be a positive integer');
  return Number(trimmed);
}

function display(path: string): string {
  return path.replace(/\\/g, '/');
}

function noRecords(root: string): string {
  return `no monitor records under ${display(root)}`;
}

/**
 * The one app a `set`/`events` call targets: --app, then TRYAII_APP, then
 * the only app with records; otherwise a usage error naming them.
 */
function selectApp(apps: string[], opts: Opts, env: Env): string {
  const explicit = str(opts, 'app') || env[ENV_APP];
  if (explicit) {
    if (!apps.includes(explicit)) throw new MonitorUsageError(`unknown app '${explicit}'`);
    return explicit;
  }
  if (apps.length === 1) return apps[0];
  if (apps.length === 0) throw new MonitorUsageError('--app is required (no monitor records yet)');
  throw new MonitorUsageError('--app is required (apps: ' + apps.join(', ') + ')');
}

// --- rendering -------------------------------------------------------------

function money(value: unknown): string {
  return value === null || value === undefined ? '-' : formatFixed(Number(value), 4);
}

function int(value: unknown): string {
  return value === null || value === undefined ? '-' : String(Math.trunc(Number(value)));
}

function share(top: unknown): string {
  if (!top || typeof top !== 'object') return '-';
  const t = top as { model?: unknown; share?: unknown };
  const pct = Math.floor(Number(t.share) * 100 + 0.5);
  return `${String(t.model)} ${pct}%`;
}

const STATUS_COLUMNS = [
  'process', 'mode', 'pinned', 'calls', 'tokens', 'est cost', 'at winner', 'savings', 'top winner share',
];
// Right-aligned numeric columns (by header name).
const RIGHT = new Set(['calls', 'tokens', 'est cost', 'at winner', 'savings']);

type StatusProcess = Record<string, unknown> & {
  process: string;
  mode: string;
  pinned?: { model?: unknown } | null;
  files: string[];
};

function modeCell(p: StatusProcess): string {
  if (p.mode === 'managed') return `managed (${p.managed_model ? String(p.managed_model) : '-'})`;
  return String(p.mode);
}

function statusRows(doc: Record<string, unknown>): string[][] {
  return (doc.processes as StatusProcess[]).map((p) => [
    String(p.process),
    modeCell(p),
    p.pinned && typeof p.pinned === 'object' ? String(p.pinned.model) : '-',
    int(p.calls),
    int(p.total_tokens),
    money(p.est_cost_usd),
    money(p.est_cost_winner_usd),
    money(p.savings_usd),
    share(p.top_winner),
  ]);
}

/**
 * Space-aligned table: two spaces between columns, text left-aligned,
 * numbers right-aligned, no trailing whitespace.
 */
function table(headers: string[], rows: string[][]): string[] {
  const widths = headers.map((h) => h.length);
  for (const row of rows) {
    row.forEach((cell, i) => {
      widths[i] = Math.max(widths[i], cell.length);
    });
  }
  const out: string[] = [];
  for (const row of [headers, ...rows]) {
    const cells = row.map((cell, i) =>
      RIGHT.has(headers[i]) && row !== headers ? cell.padStart(widths[i]) : cell.padEnd(widths[i]),
    );
    out.push(cells.join('  ').replace(/\s+$/, ''));
  }
  return out;
}

function sourceLine(source: Record<string, unknown>): string {
  if (source.kind === 'remote') {
    return `source: remote etag ${or(source.etag, '-')} fetched ${or(source.fetched_at, '-')}`;
  }
  return 'source: local';
}

function uploadLine(upload: Record<string, unknown>, offReason: string | null = null): string {
  if (offReason) return `upload: off (${offReason})`;
  const state = upload.state;
  if (state === 'error') return `upload: error ${pyStr(upload.last_error)}`;
  if (state === 'ok') return `upload: ok ${String(upload.events ?? 0)} events at ${or(upload.last_ok, '-')}`;
  return 'upload: off';
}

/** Python's str() for the JSON scalars a status document holds. */
function pyStr(value: unknown): string {
  return value === null || value === undefined ? 'None' : String(value);
}

/**
 * `classification: off since <since> (<reason>)` while the monitor last found
 * the embedding model unavailable on this machine (SPEC.md §3.1a, TS-253),
 * else null. Machine-wide, so printed once after every app block.
 */
export function classificationLine(dataDir: string): string | null {
  const state = readModelState(dataDir);
  if (state === null) return null;
  return `classification: off since ${state.since || '-'} (${state.reason || 'embedding model not available'})`;
}

export function renderStatus(doc: Record<string, unknown>, offReason: string | null = null): string {
  const lines = [`tryaii monitor -- app: ${String(doc.app)} (last ${String(doc.since)})`, ''];
  const processes = doc.processes as StatusProcess[];
  if (processes.length === 0) {
    lines.push('no calls in this window');
  } else {
    const rendered = table(STATUS_COLUMNS, statusRows(doc));
    lines.push(rendered[0]);
    rendered.slice(1).forEach((rowText, i) => {
      lines.push(rowText);
      for (const file of processes[i].files) lines.push(`    -> ${file}`);
    });
    const t = doc.totals as Record<string, unknown>;
    lines.push('');
    lines.push(
      `totals: ${String(t.calls)} calls, ${String(t.errors)} errors, est cost ` +
        `${money(t.est_cost_usd)}, at winner ${money(t.est_cost_winner_usd)}, ` +
        `savings ${money(t.savings_usd)}`,
    );
    lines.push(`realised: ${String(t.swapped_calls)} swapped calls, saved ${money(t.realised_savings_usd)}`);
  }
  lines.push(sourceLine(doc.source as Record<string, unknown>));
  lines.push(uploadLine(doc.upload as Record<string, unknown>, offReason));
  return lines.join('\n') + '\n';
}

function or(value: unknown, fallback: string): string {
  return value ? String(value) : fallback;
}

export function renderEvent(event: Record<string, unknown>): string {
  const usage = (event.usage && typeof event.usage === 'object' ? event.usage : {}) as Record<string, unknown>;
  const error = event.error && typeof event.error === 'object' ? (event.error as Record<string, unknown>) : null;
  const parts = [
    `${event.ts === undefined ? '-' : String(event.ts)}  ${or(event.process, '-')}`,
    or(event.model_used, '-'),
    or(event.mode, '-'),
    `winner ${or(event.winner, '-')}`,
    `${int(usage.total_tokens)} tok`,
  ];
  // v2.2 (SPEC.md §5.5): the cache segments only when the field is a number.
  for (const [key, label] of [['cached_tokens', 'cached'], ['cache_write_tokens', 'cache write']] as const) {
    const value = usage[key];
    if (typeof value === 'number') parts.push(`${label} ${int(value)}`);
  }
  parts.push(money(event.est_cost_usd), or(event.route_status, '-'));
  if (error) {
    const label = or(error.class, '') || or(error.status, '') || 'error';
    parts.push(`error ${label}`);
  }
  return parts.join(' | ');
}

// --- verbs -----------------------------------------------------------------

function verbStatus(argv: string[], env: Env, write: Write): void {
  const [positionals, opts] = parse(argv);
  onlySetTakesModel(opts);
  if (positionals.length) {
    throw new MonitorUsageError(`unexpected argument: ${positionals[0]}. ${HELP_HINT}`);
  }
  const now = nowArg(str(opts, 'now'));
  const since = sinceArg(str(opts, 'since'));
  const dataDir = store.dataDirFromEnv();
  let apps = store.listApps(dataDir);
  const only = str(opts, 'app');
  if (only) {
    if (!apps.includes(only)) throw new MonitorUsageError(`unknown app '${only}'`);
    apps = [only];
  }
  const docs = apps.map((app) => {
    const directory = store.appDir(dataDir, app);
    const events = store.readEvents(directory, now, windowSeconds(since));
    const cursor = store.readUploadCursor(directory);
    return aggregate(
      app,
      events,
      store.readPins(directory),
      store.readConfig(directory),
      now,
      since,
      env,
      store.readRemote(directory),
      cursor,
      store.uploadedEventCount(directory, cursor),
    );
  });
  if (opts.json) {
    const payload = only ? docs[0] : docs;
    write(JSON.stringify(payload, null, 2) + '\n');
    return;
  }
  if (docs.length === 0) {
    write(noRecords(store.monitorRoot(dataDir)) + '\n');
    return;
  }
  const [, offReason] = privacy.uploadDecision(env, store.monitorRoot(dataDir));
  write(docs.map((doc) => renderStatus(doc, offReason)).join('\n'));
  const classification = classificationLine(dataDir);
  if (classification !== null) write(`${classification}\n`);
  write(`${catalogLine()}\n`);
  const langfuse = langfuseStatusLine();
  if (langfuse !== null) write(`${langfuse}\n`);
}

/**
 * `catalog: full (login)` / `full (app key)` / `starter` (SPEC.md §5.2,
 * TS-294): the catalog this machine routes on and which credential fetched
 * it. Machine-wide, so printed once after every app block.
 */
export function catalogLine(): string {
  return `catalog: ${catalogInUse()}`;
}

function verbSet(argv: string[], env: Env, write: Write): void {
  const [positionals, opts] = parse(argv);
  if (positionals.length !== 2) {
    throw new MonitorUsageError(`usage: tryaii monitor set <process> <mode> [--model <id>]. ${HELP_HINT}`);
  }
  const [process, mode] = positionals;
  if (!(MODES as readonly string[]).includes(mode)) throw new MonitorUsageError(MSG_BAD_MODE);
  const rawModel = str(opts, 'model');
  const model = rawModel === undefined ? null : rawModel.trim();
  if (mode === 'managed' && !model) throw new MonitorUsageError(MSG_MANAGED_NEEDS_MODEL);
  if (mode !== 'managed' && model !== null) throw new MonitorUsageError('--model only applies to managed mode');
  if (!process.trim()) throw new MonitorUsageError('process name must not be empty');
  const dataDir = store.dataDirFromEnv();
  const app = selectApp(store.listApps(dataDir), opts, env);
  const directory = store.appDir(dataDir, app);
  const config = setProcessMode(store.readConfig(directory), process, mode, model);
  store.writeConfig(directory, config);
  if (opts.json) {
    const payload: Record<string, unknown> = { app, process, mode };
    if (mode === 'managed') payload.model = model;
    write(JSON.stringify(payload, null, 2) + '\n');
    return;
  }
  const shown = mode === 'managed' ? `managed (${String(model)})` : mode;
  write(`${process}: ${shown} (app ${app})\n`);
}

function verbApps(argv: string[], _env: Env, write: Write): void {
  const [positionals, opts] = parse(argv);
  onlySetTakesModel(opts);
  if (positionals.length) {
    throw new MonitorUsageError(`unexpected argument: ${positionals[0]}. ${HELP_HINT}`);
  }
  const dataDir = store.dataDirFromEnv();
  const apps = store.listApps(dataDir);
  if (opts.json) {
    write(JSON.stringify(apps, null, 2) + '\n');
    return;
  }
  if (apps.length === 0) {
    write(noRecords(store.monitorRoot(dataDir)) + '\n');
    return;
  }
  write(apps.join('\n') + '\n');
}

function verbEvents(argv: string[], env: Env, write: Write): void {
  const [positionals, opts] = parse(argv);
  onlySetTakesModel(opts);
  if (positionals.length) {
    throw new MonitorUsageError(`unexpected argument: ${positionals[0]}. ${HELP_HINT}`);
  }
  const limit = countArg(str(opts, 'n'));
  const dataDir = store.dataDirFromEnv();
  const app = selectApp(store.listApps(dataDir), opts, env);
  const events = store.readRecentEvents(store.appDir(dataDir, app), limit);
  if (opts.json) {
    for (const event of events) write(store.dumpsLine(event));
    return;
  }
  if (events.length === 0) {
    write(`no events for app ${app}\n`);
    return;
  }
  write(events.map(renderEvent).join('\n') + '\n');
}

const UPLOAD_ACTIONS = ['status', 'on', 'off'];

/**
 * The person just saw the notice in a terminal: remember it locally and,
 * when signed in, record it on the account (API.md §7.2). Best effort.
 */
async function recordCliNotice(root: string): Promise<void> {
  if (!privacy.canShowNotice(process.stdout)) return;
  privacy.markNoticeShown(root, nowArg(undefined));
  try {
    const { recordConsent } = await import('./cli-apps.js');
    await recordConsent('cli');
  } catch {
    // never fails the command
  }
}

/** `tryaii monitor upload [status|on|off]` (SPEC.md §9.3). */
async function verbUpload(argv: string[], env: Env, write: Write): Promise<void> {
  const [positionals, opts] = parse(argv);
  onlySetTakesModel(opts);
  if (positionals.length > 1 || (positionals.length && !UPLOAD_ACTIONS.includes(positionals[0]))) {
    throw new MonitorUsageError(`usage: tryaii monitor upload [on|off]. ${HELP_HINT}`);
  }
  const action = positionals.length ? positionals[0] : 'status';
  const root = store.monitorRoot(store.dataDirFromEnv());
  if (action === 'on' || action === 'off') privacy.writeLocalUpload(root, action === 'on');
  const [enabled, reason] = privacy.uploadDecision(env, root);
  if (opts.json) {
    write(JSON.stringify({ upload: enabled, reason, notice_version: privacy.NOTICE_VERSION }, null, 2) + '\n');
    return;
  }
  if (action !== 'off') write(privacy.NOTICE_TEXT + '\n');
  write((enabled ? 'upload: on' : `upload: off (${String(reason)})`) + '\n');
  if (action !== 'off') await recordCliNotice(root);
}

const VERB_HANDLERS: Record<string, (argv: string[], env: Env, write: Write) => void | Promise<void>> = {
  status: verbStatus,
  set: verbSet,
  apps: verbApps,
  events: verbEvents,
  upload: verbUpload,
};

/** Verb dispatcher for `tryaii monitor` (mirrors cmd_monitor in monitor.py). */
export function monitorMain(argv: string[], write: Write): void | Promise<void> {
  const verb = argv[0];
  if (verb === undefined) throw new MonitorUsageError(`missing monitor verb. ${HELP_HINT}`);
  const handler = VERB_HANDLERS[verb];
  if (!handler) throw new MonitorUsageError(`unknown monitor verb: ${verb}. ${HELP_HINT}`);
  return handler(argv.slice(1), envFromOs(), write);
}
