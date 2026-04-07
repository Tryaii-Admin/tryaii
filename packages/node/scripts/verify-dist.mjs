import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const packageDir = dirname(scriptDir);

const requiredPaths = [
  'dist/index.js',
  'dist/index.d.ts',
  'dist/integrations/index.js',
  'dist/integrations/index.d.ts',
  'dist/registry/presets/defaultModels.json',
  'dist/centroids/data/centroids_all-MiniLM-L6-v2.json',
];

for (const relativePath of requiredPaths) {
  const absolutePath = join(packageDir, relativePath);
  if (!existsSync(absolutePath)) {
    throw new Error(`Missing dist artifact: ${absolutePath}`);
  }
}

const indexModule = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);
const integrationsModule = await import(
  pathToFileURL(join(packageDir, 'dist/integrations/index.js')).href
);

if (typeof indexModule.Router !== 'function') {
  throw new Error('dist/index.js does not export Router');
}

if (typeof integrationsModule.OpenRouterIntegration !== 'function') {
  throw new Error('dist/integrations/index.js does not export OpenRouterIntegration');
}

const router = new indexModule.Router();
if (router.models.length === 0) {
  throw new Error('Router loaded zero models from dist assets');
}

console.log('dist verification passed');
