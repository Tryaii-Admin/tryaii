import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const packageDir = dirname(scriptDir);

const requiredPaths = [
  'dist/index.js',
  'dist/index.d.ts',
  'dist/cli.js',
  'dist/integrations/index.js',
  'dist/integrations/index.d.ts',
  'dist/cachelint/index.js',
  'dist/cachelint/index.d.ts',
  'dist/cachelint/data/providers.json',
  'dist/diagnose/index.js',
  'dist/diagnose/index.d.ts',
  'dist/diagnose/data/plan.json',
  'dist/diagnose/data/costmodel.json',
  'dist/diagnose/data/report_template.json',
  'dist/diagnose/data/skill.json',
  'dist/designpartner/index.js',
  'dist/designpartner/index.d.ts',
  'dist/designpartner/data/questions.json',
  'dist/designpartner/data/skill.json',
  'dist/registry/presets/defaultModels.json',
  'dist/centroids/data/centroids_all-MiniLM-L6-v2.json',
];

for (const relativePath of requiredPaths) {
  const absolutePath = join(packageDir, relativePath);
  if (!existsSync(absolutePath)) {
    throw new Error(`Missing dist artifact: ${absolutePath}`);
  }
}

// The CLI must keep its shebang so `tryaii` is directly executable.
const cliSource = readFileSync(join(packageDir, 'dist/cli.js'), 'utf-8');
if (!cliSource.startsWith('#!/usr/bin/env node')) {
  throw new Error('dist/cli.js is missing its "#!/usr/bin/env node" shebang');
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

const cachelintModule = await import(
  pathToFileURL(join(packageDir, 'dist/cachelint/index.js')).href
);
if (typeof cachelintModule.analyze !== 'function') {
  throw new Error('dist/cachelint/index.js does not export analyze');
}
const lint = cachelintModule.analyze({
  prompt: 'Smoke-test prompt.',
  llm: { provider: 'anthropic', name: 'claude-fable-5' },
});
if (!lint.items?.[0]?.verdict?.code) {
  throw new Error('cachelint analyze() returned no verdict from dist assets');
}

const diagnoseModule = await import(
  pathToFileURL(join(packageDir, 'dist/diagnose/index.js')).href
);
if (typeof diagnoseModule.analyzeInventory !== 'function') {
  throw new Error('dist/diagnose/index.js does not export analyzeInventory');
}
const findings = await diagnoseModule.analyzeInventory(
  {
    sites: [
      {
        file: 'smoke.py',
        line: 1,
        provider: 'anthropic',
        model: 'claude-fable-5',
        prompt: 'Smoke-test prompt.',
      },
    ],
  },
  { run_id: 'verify-dist' },
);
if (findings.summary?.schema !== 'tryaii.diagnose.summary/1') {
  throw new Error('diagnose analyzeInventory() returned no summary from dist assets');
}

console.log('dist verification passed');
