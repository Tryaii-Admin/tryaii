// Node half of the cross-SDK engine parity test (tests/test_catalog_bundle.py).
//
// Usage: node node_engine_parity.mjs <dist/index.js> <prompts.json> <bundleDir|starter>
//
// Scores every prompt's recorded similarities on the given catalog bundle at
// every priority triple -- exactly the Router's post-classification path
// (bundle registry, bundle normalizer, registry-wide coverage) -- and prints
// the full-precision results as JSON on stdout.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [, , distIndex, promptsPath, bundleArg] = process.argv;
const m = await import(pathToFileURL(distIndex).href);
const fixture = JSON.parse(readFileSync(promptsPath, 'utf-8'));

const bundle = bundleArg === 'starter' ? m.starterBundle() : m.loadBundle(bundleArg);
const registry = m.ModelRegistry.fromBundle(bundle);
const engine = new m.ScoringEngine(m.BenchmarkRegistry.fromBundle(bundle).getNormalizer());
const coverage = registry.benchmarkCoverage();

const rows = [];
for (const [index, item] of fixture.prompts.entries()) {
  for (const [q, c, s] of fixture.priorities) {
    const scores = engine.scoreModels(
      registry.allModels,
      item.similarities,
      new m.Priorities(q, c, s),
      10,
      coverage,
    );
    rows.push({
      prompt: index,
      priorities: [q, c, s],
      scores: scores.map((sc) => ({
        model_id: sc.modelId,
        final_score: sc.finalScore,
        quality_score: sc.qualityScore,
        cost_score: sc.costScore,
        speed_score: sc.speedScore,
        q_prime: sc.qPrime,
        u_cost: sc.uCost,
        u_speed: sc.uSpeed,
        in_band: sc.inBand,
        reasoning: sc.reasoning,
      })),
    });
  }
}
process.stdout.write(JSON.stringify({ version: bundle.version, rows }));
