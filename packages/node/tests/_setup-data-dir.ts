/**
 * Catalog contract section 5: new Router() / ModelRegistry.default() pick the
 * catalog from the data dir (credentials + catalog cache). Never let the
 * developer's real ~/.tryaii (a logged-in session, a downloaded full catalog)
 * leak into the suite: unless already set, every test and every CLI
 * subprocess sees an empty temp data dir -> the starter catalog.
 */
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { writeTrustedKeys } from './_signing.js';

if (!process.env.TRYAII_DRE_DATA_DIR) {
  process.env.TRYAII_DRE_DATA_DIR = mkdtempSync(join(tmpdir(), 'tryaii-tests-'));
}

// Catalog contract section 6: a full catalog is accepted only when signed by a
// trusted key. The tests' synthetic full bundles are signed with the TEST key
// of tests/_signing.ts; trust exactly that key unless already set. Tests of
// the packaged list remove the variable.
if (!process.env.TRYAII_CATALOG_TRUSTED_KEYS) {
  process.env.TRYAII_CATALOG_TRUSTED_KEYS = writeTrustedKeys(
    join(mkdtempSync(join(tmpdir(), 'tryaii-test-keys-')), 'trusted_keys.json'),
  );
}
