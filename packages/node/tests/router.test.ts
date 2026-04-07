import { describe, expect, it } from 'vitest';

import { Router } from '../src/index.js';

describe('Router', () => {
  it('loads the bundled default registry', () => {
    const router = new Router();

    expect(router.models.length).toBeGreaterThan(0);
  });

  it('uses keyword routing by default', () => {
    const router = new Router();
    const result = router.route('Write a Python function to sort an array');

    expect(result.bestModel).toBeTruthy();
    expect(result.classification?.classifierUsed).toBe('keyword');
  });
});
