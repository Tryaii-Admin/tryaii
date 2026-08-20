import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
    // cachelint workers load multi-MB tokenizer ranks + the typescript
    // parser; under parallel CPU contention 5s is too tight.
    testTimeout: 20000,
  },
});
