/**
 * Catalog bundles for the tests.
 *
 * The package ships only the *starter* catalog bundle. Tests that pin
 * full-catalog behaviour load a full bundle from `build/catalog/full` (built
 * from the catalog build data; never committed, never packaged) and are
 * skipped when it is absent -- their
 * describe titles say so. Mirrors packages/python/tests/conftest.py.
 */

import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { CatalogBundle, loadBundle, starterBundle } from '../src/catalog/bundle.js';

export const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
export const FULL_BUNDLE_DIR = join(REPO_ROOT, 'build', 'catalog', 'full');

/** True when build/catalog/full has been built. */
export const HAS_FULL_BUNDLE = existsSync(join(FULL_BUNDLE_DIR, 'manifest.json'));

/** Appended to describe titles that need the full bundle. */
export const FULL_ONLY =
  '[full catalog: skipped unless build/catalog/full exists]';

let full: CatalogBundle | null = null;

/** The full catalog bundle (call only inside a HAS_FULL_BUNDLE-guarded suite). */
export function fullBundle(): CatalogBundle {
  if (full === null) full = loadBundle(FULL_BUNDLE_DIR);
  return full;
}

export { starterBundle };
