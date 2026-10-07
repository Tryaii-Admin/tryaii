/**
 * Preset model registry data paths (kept for backwards compatibility).
 *
 * The default models are the packaged starter catalog bundle's models.json;
 * see `catalog/bundle.ts`.
 */

import { join } from 'node:path';

import { STARTER_BUNDLE_DIR } from '../../catalog/bundle.js';

/** Path to the default (starter catalog) models JSON file. */
export const DEFAULT_MODELS_PATH = join(STARTER_BUNDLE_DIR, 'models.json');
