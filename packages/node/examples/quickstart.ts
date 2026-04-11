/**
 * TryAii-DRE Quick Start Example
 *
 * Demonstrates basic usage of the Router to classify prompts
 * and route them to the best AI model.
 *
 * Run with: npx tsx examples/quickstart.ts
 */

import { Router, Priorities } from '../src/index.js';

async function main(): Promise<void> {
  // Create a router with default settings.
  // Uses the embedding classifier backed by @xenova/transformers.
  const router = new Router();

  console.log('=== TryAii-DRE Quick Start ===\n');
  console.log(`Models loaded: ${router.models.length}`);
  console.log(`Benchmarks: ${router.benchmarks.names.join(', ')}\n`);

  // --- Example 1: Basic routing ---
  console.log('--- Example 1: Code task ---');
  const codeResult = await router.route('Write a Python function to implement binary search');
  console.log(`Best model: ${codeResult.bestModel}`);
  console.log(`Score: ${codeResult.scores[0]?.finalScore.toFixed(3)}`);
  console.log(`Category: ${codeResult.classification?.broadCategory} / ${codeResult.classification?.subcategory}`);
  console.log(`Reasoning: ${codeResult.scores[0]?.reasoning}\n`);

  // --- Example 2: Creative writing ---
  console.log('--- Example 2: Creative writing ---');
  const creativeResult = await router.route('Write a poem about the ocean at sunset');
  console.log(`Best model: ${creativeResult.bestModel}`);
  console.log(`Score: ${creativeResult.scores[0]?.finalScore.toFixed(3)}`);
  console.log(`Category: ${creativeResult.classification?.broadCategory}\n`);

  // --- Example 3: With custom priorities ---
  console.log('--- Example 3: Budget-friendly routing ---');
  const budgetResult = await router.route(
    'Explain the theory of relativity in simple terms',
    { priorities: Priorities.budget() },
  );
  console.log(`Best model: ${budgetResult.bestModel}`);
  console.log(`Score: ${budgetResult.scores[0]?.finalScore.toFixed(3)}`);
  console.log(`Priorities: quality=${budgetResult.priorities.quality}, cost=${budgetResult.priorities.cost}, speed=${budgetResult.priorities.speed}\n`);

  // --- Example 4: Performance-focused ---
  console.log('--- Example 4: Performance-focused routing ---');
  const perfResult = await router.route(
    'Debug this complex distributed system architecture',
    { priorities: Priorities.performance() },
  );
  console.log(`Best model: ${perfResult.bestModel}`);
  console.log(`Score: ${perfResult.scores[0]?.finalScore.toFixed(3)}`);
  console.log(`Top 3: ${perfResult.scores.slice(0, 3).map((s) => `${s.modelId}(${s.finalScore.toFixed(2)})`).join(', ')}\n`);

  // --- Example 5: Adding a custom model ---
  console.log('--- Example 5: Custom model ---');
  router.addModel({
    modelId: 'my-custom-model',
    provider: 'custom',
    benchmarks: { 'HumanEval': 85, 'SWE-bench': 70, 'MMLU': 80 },
    pricing: [0.001, 0.002],
    latency: 'fast',
  });

  const customResult = await router.route('Fix this bug in my code');
  const customModelRank = customResult.scores.findIndex((s) => s.modelId === 'my-custom-model');
  console.log(`Custom model rank: ${customModelRank + 1} of ${customResult.scores.length}`);
  console.log();

  // --- Example 6: Filtering by provider ---
  console.log('--- Example 6: Provider filter ---');
  const anthropicResult = await router.route('Write a detailed research paper outline', {
    filterProvider: 'anthropic',
    priorities: Priorities.performance(),
  });
  console.log(`Best Anthropic model: ${anthropicResult.bestModel}`);
  console.log(`Score: ${anthropicResult.scores[0]?.finalScore.toFixed(3)}`);
  console.log();

  console.log('=== Done ===');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
