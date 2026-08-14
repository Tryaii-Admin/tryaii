/**
 * Run persistence (SPEC.md §4): .tryaii/diagnose/<run-id>/ + latest pointer.
 *
 * The `latest` pointer is a plain text file (never a symlink — Windows).
 * Run ids sort lexicographically, so "previous run" is a plain sorted
 * lookup. All files are written with \n newlines for cross-platform byte
 * parity. Mirrors diagnose/store.py.
 */

import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const LATEST_FILE = 'latest';

function dump(obj: unknown): string {
  return JSON.stringify(obj, null, 2) + '\n';
}

/** Persist one run; returns {name: path} of everything written. */
export function writeRun(
  outDir: string,
  inventoryData: unknown,
  findings: Record<string, unknown>,
): Record<string, string> {
  const runId = findings.run_id as string;
  const runDir = join(outDir, runId);
  mkdirSync(runDir, { recursive: true });

  const meta = {
    run_id: runId,
    generated_at: findings.generated_at,
    tool: findings.tool,
  };
  const paths = {
    inventory: join(runDir, 'inventory.json'),
    findings: join(runDir, 'findings.json'),
    meta: join(runDir, 'meta.json'),
    latest: join(outDir, LATEST_FILE),
  };
  writeFileSync(paths.inventory, dump(inventoryData), 'utf-8');
  writeFileSync(paths.findings, dump(findings), 'utf-8');
  writeFileSync(paths.meta, dump(meta), 'utf-8');
  writeFileSync(paths.latest, runId + '\n', 'utf-8');
  return paths;
}

/** Run dirs (containing a findings.json), sorted ascending. */
export function listRunIds(outDir: string): string[] {
  if (!existsSync(outDir) || !statSync(outDir).isDirectory()) return [];
  return readdirSync(outDir)
    .filter((name) => {
      const p = join(outDir, name);
      return statSync(p).isDirectory() && existsSync(join(p, 'findings.json'));
    })
    .sort();
}

/** The pointer file's run id, falling back to the newest run dir. */
export function latestRunId(outDir: string): string | null {
  const pointer = join(outDir, LATEST_FILE);
  if (existsSync(pointer) && statSync(pointer).isFile()) {
    const runId = readFileSync(pointer, 'utf-8').trim();
    if (runId && existsSync(join(outDir, runId, 'findings.json'))) return runId;
  }
  const runs = listRunIds(outDir);
  return runs.length ? runs[runs.length - 1] : null;
}

/** The run immediately before `runId` in lexicographic order. */
export function previousRunId(outDir: string, runId: string): string | null {
  const earlier = listRunIds(outDir).filter((r) => r < runId);
  return earlier.length ? earlier[earlier.length - 1] : null;
}

export function loadRunFindings(outDir: string, runId: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(outDir, runId, 'findings.json'), 'utf-8')) as Record<
    string,
    unknown
  >;
}
