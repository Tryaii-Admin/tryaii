import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const packageDir = dirname(scriptDir);

// `--release` (npm run verify:release) adds the release guard of catalog
// contract section 6: the packaged trusted_keys.json must list production
// keys only. It fails on purpose while the committed list holds the
// development key; the production key is added at the deploy step
// (scripts/check-release-keys.py explains the procedure).
const releaseMode = process.argv.includes('--release');

// Never read the developer's real ~/.tryaii (credentials, a cached full
// catalog): the checks below must see what a fresh install sees.
const isolatedDataDir = mkdtempSync(join(tmpdir(), 'tryaii-verify-dist-'));
process.env.TRYAII_DRE_DATA_DIR = isolatedDataDir;
process.on('exit', () => rmSync(isolatedDataDir, { recursive: true, force: true }));

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
  'dist/catalog/data/starter/manifest.json',
  'dist/catalog/data/starter/models.json',
  'dist/catalog/data/starter/benchmarks.json',
  'dist/catalog/data/starter/normalization_ranges.json',
  'dist/catalog/data/starter/centroids.json',
  'dist/catalog/data/starter/training_queries.json',
  'dist/catalog/data/trusted_keys.json',
];

for (const relativePath of requiredPaths) {
  const absolutePath = join(packageDir, relativePath);
  if (!existsSync(absolutePath)) {
    throw new Error(`Missing dist artifact: ${absolutePath}`);
  }
}

// The full catalog must never be packaged (it is downloaded after login).
const forbiddenPaths = [
  'dist/registry/presets/defaultModels.json',
  'dist/centroids/data',
  'dist/scoring/data',
  'dist/catalog/data/full',
];
for (const relativePath of forbiddenPaths) {
  if (existsSync(join(packageDir, relativePath))) {
    throw new Error(`Full-catalog data must not be packaged: ${relativePath}`);
  }
}

// Catalog signing (contract section 6): the packaged trusted-keys list must be
// well formed; in --release mode it must hold production keys only.
const trustedKeys = JSON.parse(
  readFileSync(join(packageDir, 'dist/catalog/data/trusted_keys.json'), 'utf-8'),
);
if (!Array.isArray(trustedKeys?.keys) || trustedKeys.keys.length === 0) {
  throw new Error('dist/catalog/data/trusted_keys.json has no keys');
}
for (const entry of trustedKeys.keys) {
  if (!/^[a-z0-9-]{3,64}$/.test(String(entry?.key_id))) {
    throw new Error(`trusted_keys.json: invalid key_id ${JSON.stringify(entry?.key_id)}`);
  }
  if (Buffer.from(String(entry.public_key), 'base64url').length !== 32) {
    throw new Error(`trusted_keys.json: ${entry.key_id}: public_key is not 32 bytes`);
  }
  if (!['production', 'development'].includes(entry.env)) {
    throw new Error(`trusted_keys.json: ${entry.key_id}: invalid env ${JSON.stringify(entry.env)}`);
  }
}
if (releaseMode) {
  const dev = trustedKeys.keys.filter((k) => k.env === 'development').map((k) => k.key_id);
  const prod = trustedKeys.keys.filter((k) => k.env === 'production');
  if (dev.length > 0 || prod.length === 0) {
    throw new Error(
      'RELEASE KEY CHECK FAILED (catalog contract section 6): dist/catalog/data/trusted_keys.json ' +
        (dev.length > 0 ? `contains development key(s) ${dev.join(', ')}` : 'has no production key') +
        '. The production key is added at the deploy step (see scripts/check-release-keys.py).',
    );
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

// The packaged catalog: 'starter' explicitly (the default, 'auto', would use
// a full catalog when logged in).
const router = new indexModule.Router({ catalog: 'starter' });
if (router.models.length === 0) {
  throw new Error('Router loaded zero models from dist assets');
}
if (router.bundle.kind !== 'starter') {
  throw new Error(`dist starter catalog is ${router.bundle.kind}, expected starter`);
}
// Logged out (the isolated data dir), the default catalog is the starter too.
if (new indexModule.Router().bundle.kind !== 'starter') {
  throw new Error('dist default catalog (logged out) is not the starter catalog');
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

console.log(releaseMode ? 'dist verification passed (release keys OK)' : 'dist verification passed');
