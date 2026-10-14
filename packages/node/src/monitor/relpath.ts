/**
 * The call-site file as the record stores it (TS-226): relative to the
 * application's working directory, `/` separators; a file outside that
 * directory (or on another drive) is its base name only. An absolute path
 * names the developer's machine, not the app, and never leaves it.
 */

import { homedir } from 'node:os';
import { basename, isAbsolute, relative, resolve, sep } from 'node:path';

/** A drive letter, a root slash or `~`: a path that names the machine. */
const ABSOLUTE_RE = /^(?:[A-Za-z]:[\\/]|[\\/]|~)/;

export function isAbsoluteFile(value: unknown): value is string {
  return typeof value === 'string' && ABSOLUTE_RE.test(value);
}

export function relativeFile(file: string | null | undefined, cwd: string = process.cwd()): string | null | undefined {
  if (!file) return file;
  try {
    let rel: string;
    if (isAbsoluteFile(file)) {
      const expanded = file.startsWith('~') ? homedir() + file.slice(1) : file;
      const full = resolve(expanded);
      rel = relative(resolve(cwd), full);
      if (rel === '' || rel === '..' || rel.startsWith('..' + sep) || rel.startsWith('../') || isAbsolute(rel)) {
        rel = basename(full);
      }
    } else {
      rel = file;
    }
    return rel.replace(/\\/g, '/');
  } catch {
    return basename(String(file).replace(/\\/g, '/'));
  }
}
