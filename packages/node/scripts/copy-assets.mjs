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

const assetCopies = [
  ['src/registry/presets', 'dist/registry/presets'],
  ['src/centroids/data', 'dist/centroids/data'],
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
    if (!entry.isFile() || !entry.name.endsWith('.json')) {
      continue;
    }

    copyFileSync(join(sourcePath, entry.name), join(targetPath, entry.name));
  }
}
