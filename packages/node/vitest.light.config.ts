import { configDefaults, defineConfig, mergeConfig } from 'vitest/config';

import base from './vitest.config';

/**
 * The light suite (`npm run test:light`): everything in vitest.config.ts
 * except the slow files below. The tryaii-dev pull-request CI runs it; the
 * full suite (`npm test`) runs on the full CI (production repo, or dev with
 * the `full-ci` label / workflow_dispatch `full: true`).
 *
 * Opt-out list, so a new test file runs in the light suite by default. Add a
 * file here only when it is slow (several seconds) AND what it checks is
 * also covered elsewhere on the full run: today these all spawn the built
 * CLI many times (black-box contract goldens, end-to-end CLI flows). The
 * shared-fixture parity suites (`*-fixtures.test.ts`, embedding-parity) are
 * the cross-SDK contract and must stay in the light suite.
 *
 * Local timings (2026-10-21, full suite 71 s wall): auth-contract-blackbox
 * 69 s, apps-cli 26 s, monitor-cli 12 s, policy 11 s,
 * catalog-contract-blackbox 9 s.
 */
export const SLOW_TEST_FILES = [
  'tests/auth-contract-blackbox.test.ts',
  'tests/catalog-contract-blackbox.test.ts',
  'tests/apps-cli.test.ts',
  'tests/monitor-cli.test.ts',
  'tests/policy.test.ts',
];

export default mergeConfig(
  base,
  defineConfig({
    test: {
      exclude: [...configDefaults.exclude, ...SLOW_TEST_FILES],
    },
  }),
);
