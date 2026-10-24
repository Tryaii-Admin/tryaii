/**
 * `tryaii monitor` CLI goldens (shared/monitor/SPEC.md §5, fixture suite `cli`).
 *
 * Each case spawns the built Node CLI (`node dist/cli.js`, so run
 * `npm run build` first) in a fresh temp data dir with the named corpus
 * stores staged under <tmp>/monitor/ -- the same staging the Python
 * generator (scripts/gen-monitor-fixtures.py, run_cli_case) uses -- and
 * byte-compares stdout against the frozen golden, plus stderr and the exit
 * code. Byte parity of WRITTEN files between the two CLIs is asserted by
 * packages/python/tests/test_monitor_cli_parity.py.
 */

import { spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_DIR = resolve(HERE, '..');
const CLI = join(PKG_DIR, 'dist', 'cli.js');
const FIXTURES = resolve(PKG_DIR, '..', '..', 'shared', 'monitor', 'fixtures');
const CASES = join(FIXTURES, 'cli', 'cases.json');

interface CliCase {
  name: string;
  input: { argv: string[]; copy_store?: string[]; env?: Record<string, string> };
  expected: { stdout_golden: string; stderr: string | null; exit_code: number } | null;
}

function loadCases(): CliCase[] {
  if (!existsSync(CASES)) return [];
  return (JSON.parse(readFileSync(CASES, 'utf-8')) as { cases: CliCase[] }).cases;
}

function normalize(buf: Buffer): string {
  return buf.toString('utf-8').replace(/\r\n/g, '\n');
}

/** Run one cli case in a fresh temp cwd; returns [exitCode, stdout, stderr]. */
function runCase(input: CliCase['input'], inspect?: (tmp: string) => void): [number, string, string] {
  const tmp = mkdtempSync(join(tmpdir(), 'tryaii-monitor-cli-'));
  try {
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      TRYAII_NO_BANNER: '1',
      TRYAII_DRE_DATA_DIR: tmp,
    };
    // The upload opt-outs change `status` and `upload` output (SPEC.md §9);
    // like the generator, a case never inherits them from the caller.
    for (const name of ['TRYAII_MONITOR_UPLOAD', 'TRYAII_TELEMETRY', 'DO_NOT_TRACK', 'CI']) delete env[name];
    Object.assign(env, input.env ?? {});
    for (const name of input.copy_store ?? []) {
      cpSync(join(FIXTURES, 'corpus', name), join(tmp, 'monitor', name), { recursive: true });
    }
    const proc = spawnSync(process.execPath, [CLI, ...input.argv], { cwd: tmp, env });
    inspect?.(tmp);
    return [proc.status ?? -1, normalize(proc.stdout), normalize(proc.stderr)];
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

describe.skipIf(!existsSync(CLI) || !existsSync(CASES))('tryaii monitor CLI (Node)', () => {
  for (const c of loadCases()) {
    it(c.name, () => {
      if (c.expected === null) return;
      const [code, stdout, stderr] = runCase(c.input);
      const golden = readFileSync(join(FIXTURES, 'cli', c.expected.stdout_golden), 'utf-8').replace(
        /\r\n/g,
        '\n',
      );
      expect(stderr || null, 'stderr').toBe(c.expected.stderr);
      expect(code, 'exit code').toBe(c.expected.exit_code);
      expect(stdout).toBe(golden);
    });
  }

  it('help monitor and <verb> --help print the frozen pages', () => {
    const [code, stdout] = runCase({ argv: ['help', 'monitor'] });
    expect(code).toBe(0);
    expect(stdout.startsWith('tryaii monitor -- Watch and steer the LLM calls your app makes\n')).toBe(true);
    for (const verb of ['status', 'set', 'apps', 'events']) {
      const [vcode, vout] = runCase({ argv: ['monitor', verb, '--help'] });
      expect(vcode, verb).toBe(0);
      expect(vout.startsWith(`tryaii monitor ${verb} -- `), verb).toBe(true);
    }
  });

  it('set managed writes mode, model, providers in contract order (SPEC.md §2.6)', () => {
    let written = '';
    const [code, stdout] = runCase(
      { argv: ['monitor', 'set', 'summarize', 'managed', '--model', ' openai/gpt-5-mini ', '--app', 'shop'], copy_store: ['shop'] },
      (tmp) => {
        written = readFileSync(join(tmp, 'monitor', 'shop', 'config.json'), 'utf-8');
      },
    );
    expect(code).toBe(0);
    expect(stdout).toBe('summarize: managed (openai/gpt-5-mini) (app shop)\n');
    const doc = JSON.parse(written);
    expect(Object.keys(doc.processes.summarize)).toEqual(['mode', 'model', 'providers']);
    expect(doc.processes.summarize.model).toBe('openai/gpt-5-mini');
    expect(written.endsWith('}\n')).toBe(true);
    expect(written.includes('\r')).toBe(false);
  });

  it('status reads remote.json and upload.json (source and upload lines)', () => {
    const [code, stdout] = runCase({
      argv: ['monitor', 'status', '--app', 'managed', '--now', '2026-10-12T09:00:00Z'],
      copy_store: ['managed'],
    });
    expect(code).toBe(0);
    const lines = stdout.trimEnd().split('\n');
    expect(lines.at(-3)).toMatch(/^source: remote etag e-42 fetched /);
    expect(lines.at(-2)).toMatch(/^upload: ok 2 events at /);
    expect(lines.at(-1)).toBe('catalog: starter'); // TS-294, SPEC.md §5.2
    expect(lines.some((l) => l.startsWith('realised: '))).toBe(true);
  });
});
