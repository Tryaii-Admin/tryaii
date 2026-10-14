/** Mode, app and process resolution (SPEC.md §2.6). Pure functions. Mirrors monitor/config.py. */

import { sortedByKey } from './pins.js';
import { MODE_MANAGED, MODES, type Mode } from './winner.js';

export const CONFIG_SCHEMA = 'tryaii.monitor.config/1';
export const REMOTE_SCHEMA = 'tryaii.monitor.remote/1';
export const ENV_MODE = 'TRYAII_MONITOR_MODE';
export const ENV_APP = 'TRYAII_APP';
export const DEFAULT_APP = 'default';
export const DEFAULT_PROCESS = 'default';
export const MSG_BAD_MODE = 'mode must be off, stable, dynamic or managed';
export const MSG_MANAGED_NEEDS_MODEL = 'managed mode needs --model <id>';
const APP_RE = /[^a-z0-9._-]+/g;
// Both separators split a call-site path (Python: re.split(r"[\\/]", ...)).
const PATH_SEPARATORS = /[\\/]/;

export interface ProcessConfig {
  mode?: string | null;
  model?: string | null;
  providers?: string[] | null;
  [key: string]: unknown;
}

export interface ConfigDoc {
  schema: string;
  source: string;
  default_mode: string;
  processes: Record<string, ProcessConfig>;
}

export type ModeResolution = { mode: Mode; source: string; model: string | null };
export type AppResolution = { app: string; source: string };
export type ProcessResolution = { process: string; source: string };

export function emptyConfig(): ConfigDoc {
  return { schema: CONFIG_SCHEMA, source: 'local', default_mode: 'off', processes: {} };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function validMode(value: unknown): Mode | null {
  if (typeof value !== 'string') return null;
  const text = value.trim().toLowerCase();
  return (MODES as readonly string[]).includes(text) ? (text as Mode) : null;
}

function cleanModel(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value.trim();
  return null;
}

/**
 * `[mode, model]` from a config-shaped document (config.json or
 * remote.json): the process entry first, then `default_mode`. `model` is the
 * entry's managed model (null at the default level and for the other modes).
 */
function configMode(config: unknown, process: string): [Mode, string | null] | null {
  if (!isRecord(config)) return null;
  const processes = config.processes;
  const entry = isRecord(processes) ? processes[process] : undefined;
  if (isRecord(entry)) {
    const mode = validMode(entry.mode);
    if (mode) return [mode, mode === MODE_MANAGED ? cleanModel(entry.model) : null];
  }
  const mode = validMode(config.default_mode);
  return mode ? [mode, null] : null;
}

export interface ResolveModeOptions {
  env?: Record<string, string | undefined> | null;
  remote?: unknown;
  args?: string | null;
  config?: unknown;
}

/**
 * `{mode, source, model}` — precedence env > remote > args > config >
 * default (`off`). Invalid values at a level are ignored. `model` is the
 * managed model of the level that set the mode (only a per-process entry of
 * remote.json / config.json carries one), else null.
 */
export function resolveMode(process: string, opts: ResolveModeOptions = {}): ModeResolution {
  const env = opts.env ?? {};
  let mode = validMode(env[ENV_MODE]);
  if (mode) return { mode, source: 'env', model: null };
  let found = configMode(opts.remote, process);
  if (found) return { mode: found[0], source: 'remote', model: found[1] };
  mode = validMode(opts.args);
  if (mode) return { mode, source: 'args', model: null };
  found = configMode(opts.config, process);
  if (found) return { mode: found[0], source: 'config', model: found[1] };
  return { mode: 'off', source: 'default', model: null };
}

/** Python's str.strip(chars) for the "-." set. */
function stripChars(text: string, chars: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && chars.includes(text[start])) start++;
  while (end > start && chars.includes(text[end - 1])) end--;
  return text.slice(start, end);
}

/** Lower-case, `[a-z0-9._-]` only, runs collapsed to `-`; null when nothing is left. */
export function sanitizeApp(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const text = stripChars(value.trim().toLowerCase().replace(APP_RE, '-'), '-.');
  return text || null;
}

export interface ResolveAppOptions {
  env?: Record<string, string | undefined> | null;
  args?: string | null;
  cwdName?: string | null;
}

/** `{app, source}` — env TRYAII_APP > wrap(app=) > the working directory's name > `default`. */
export function resolveApp(opts: ResolveAppOptions = {}): AppResolution {
  const env = opts.env ?? {};
  const levels: [string, unknown][] = [
    ['env', env[ENV_APP]],
    ['args', opts.args],
    ['cwd', opts.cwdName],
  ];
  for (const [source, value] of levels) {
    const app = sanitizeApp(value);
    if (app) return { app, source };
  }
  return { app: DEFAULT_APP, source: 'default' };
}

/**
 * `basename(file):function` (`:L<line>` when the function is unknown) for a
 * call site.
 */
export function callSiteProcess(callSite: unknown): string | null {
  if (!isRecord(callSite) || !callSite.file) return null;
  const parts = String(callSite.file).split(PATH_SEPARATORS);
  const base = parts[parts.length - 1];
  const fn = callSite.function;
  if (fn) return `${base}:${String(fn)}`;
  const line = callSite.line;
  return line !== null && line !== undefined ? `${base}:L${String(line)}` : base;
}

function cleanName(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value.trim();
  return null;
}

export interface ResolveProcessOptions {
  perCall?: unknown;
  context?: unknown;
  default?: unknown;
  callSite?: unknown;
}

/**
 * `{process, source}` — per-call `tryaii_process` > the `process(name)`
 * context > `wrap(process=)` > the call site > `default`.
 */
export function resolveProcess(opts: ResolveProcessOptions = {}): ProcessResolution {
  const levels: [string, unknown][] = [
    ['call', opts.perCall],
    ['context', opts.context],
    ['args', opts.default],
  ];
  for (const [source, value] of levels) {
    const name = cleanName(value);
    if (name) return { process: name, source };
  }
  const name = callSiteProcess(opts.callSite);
  if (name) return { process: name, source: 'call_site' };
  return { process: DEFAULT_PROCESS, source: 'default' };
}

/**
 * A new config document with `process` set to `mode`.
 *
 * The entry is rebuilt in contract key order `mode`, `model` (only for
 * `managed`), `providers` (kept, or null), then any other keys it already
 * had. `managed` needs a non-empty `model`; a `model` with another mode is
 * ignored and a stale one is dropped.
 */
export function setProcessMode(config: unknown, process: string, mode: string, model: unknown = null): ConfigDoc {
  if (!(MODES as readonly string[]).includes(mode)) throw new Error(MSG_BAD_MODE);
  const cleaned = cleanModel(model);
  if (mode === MODE_MANAGED && cleaned === null) throw new Error(MSG_MANAGED_NEEDS_MODEL);
  const base = isRecord(config) ? config : {};
  const processes: Record<string, ProcessConfig> = {
    ...((isRecord(base.processes) ? base.processes : {}) as Record<string, ProcessConfig>),
  };
  const old: Record<string, unknown> = isRecord(processes[process]) ? { ...processes[process] } : {};
  const entry: ProcessConfig = { mode };
  if (mode === MODE_MANAGED) entry.model = cleaned;
  entry.providers = (old.providers ?? null) as string[] | null;
  for (const [key, value] of Object.entries(old)) {
    if (key !== 'mode' && key !== 'model' && key !== 'providers') entry[key] = value;
  }
  processes[process] = entry;
  return {
    schema: CONFIG_SCHEMA,
    source: typeof base.source === 'string' && base.source ? base.source : 'local',
    default_mode: validMode(base.default_mode) ?? 'off',
    processes: sortedByKey(processes),
  };
}

/** The monitor-relevant environment, for callers that are not pure. */
export function envFromOs(env: NodeJS.ProcessEnv = process.env): Record<string, string | undefined> {
  const out: Record<string, string | undefined> = {};
  for (const key of [ENV_MODE, ENV_APP]) if (key in env) out[key] = env[key];
  return out;
}
