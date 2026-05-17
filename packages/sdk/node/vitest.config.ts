// Plain-object vitest config (no `defineConfig` import) so this file does
// not itself require `vitest` to be resolvable from this package's
// node_modules — useful when running the suite via a sibling package's
// installed vitest binary.
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

export default {
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
  },
  resolve: {
    alias: {
      // Redirect `import ... from 'tryaii-dre'` to a local test fixture so
      // the SDK's smoke tests run without requiring the real core package
      // to be installed or built. See tests/fixtures/tryaii-dre-mock.ts.
      'tryaii-dre': resolve(here, 'tests/fixtures/tryaii-dre-mock.ts'),
    },
  },
};
