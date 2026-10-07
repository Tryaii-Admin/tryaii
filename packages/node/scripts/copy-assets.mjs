import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  rmSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const packageDir = dirname(scriptDir);

// The routing data is the starter catalog bundle only (the full catalog is
// downloaded after login and must never be packaged). src/catalog/data itself
// holds trusted_keys.json (catalog contract section 6); it is copied first
// because each entry replaces its target directory.
const assetCopies = [
  ['src/catalog/data', 'dist/catalog/data'],
  ['src/catalog/data/starter', 'dist/catalog/data/starter'],
  ['src/cachelint/data', 'dist/cachelint/data'],
  ['src/diagnose/data', 'dist/diagnose/data'],
  ['src/designpartner/data', 'dist/designpartner/data'],
];

for (const [sourceRelativePath, targetRelativePath] of assetCopies) {
  const sourcePath = join(packageDir, sourceRelativePath);
  const targetPath = join(packageDir, targetRelativePath);

  if (!existsSync(sourcePath)) {
    throw new Error(`Missing asset source directory: ${sourcePath}`);
  }

  rmSync(targetPath, { force: true, recursive: true });
  mkdirSync(targetPath, { recursive: true });

  for (const entry of readdirSync(sourcePath, { withFileTypes: true })) {
    if (!entry.isFile() || (!entry.name.endsWith('.json') && !entry.name.endsWith('.html'))) {
      continue;
    }

    copyFileSync(join(sourcePath, entry.name), join(targetPath, entry.name));
  }
}

// Remove the pre-bundle full-catalog data dirs a developer may still have in
// dist/ from an earlier build: TypeScript does not clean dist/, and npm pack
// must never ship the full catalog.
for (const staleRelativePath of [
  'dist/registry/presets/defaultModels.json',
  'dist/centroids/data',
  'dist/scoring/data',
]) {
  rmSync(join(packageDir, staleRelativePath), { force: true, recursive: true });
}
