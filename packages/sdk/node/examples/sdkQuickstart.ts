/**
 * TryAii-DRE SDK Quickstart
 *
 * Demonstrates basic usage of the DREClient for routing and chat.
 * Set the OPENROUTER_API_KEY environment variable before running.
 */

import { DREClient } from 'tryaii-dre-sdk';

async function main() {
  const client = new DREClient({
    apiKey: process.env.OPENROUTER_API_KEY,
  });

  // --- Route only (no API call) ---
  const route = await client.route('Write a sorting algorithm');
  console.log('Best model:', route.bestModel);
  console.log('Score:', route.bestScore);
  console.log('Reasoning:', route.bestReasoning);
  console.log();

  // --- Chat (routes + calls the API) ---
  const response = await client.chat('Write a sorting algorithm');
  console.log('Model used:', response.modelUsed);
  console.log('Response:', response.content);
  console.log();

  // --- Chat with custom priorities ---
  const fast = await client.chat('Summarize this paragraph in one sentence.', {
    priorities: { quality: 2, cost: 4, speed: 5 },
    temperature: 0.3,
  });
  console.log('Fast model:', fast.modelUsed);
  console.log('Response:', fast.content);
  console.log();

  // --- Streaming ---
  console.log('Streaming response:');
  for await (const chunk of client.stream('Explain quicksort in simple terms')) {
    process.stdout.write(chunk);
  }
  console.log('\n');
}

main().catch(console.error);
