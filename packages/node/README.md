# tryaii-dre

AI model router for Node.js and TypeScript.

Ranks models using benchmark performance, pricing, latency, and your quality/cost/speed priorities.

## Installation

```bash
npm install tryaii-dre
```

## Quick Start

```typescript
import { Priorities, Router } from 'tryaii-dre';

// Default startup is keyword-based and requires no extra dependencies.
const router = new Router();

const result = router.route('Write a Python function to sort an array');
console.log(result.bestModel);     // e.g., "gpt-5.2"
console.log(result.scores[0]);     // Full scoring breakdown

// Route with custom priorities
const budgetResult = router.route(
  'Explain quantum computing',
  { priorities: Priorities.budget() }  // Favor cheaper models
);
```

## Default Behavior

`Router` defaults to keyword classification so an npm install works immediately with no model downloads or API keys.

If you want full control, you can also call `routeKeywordOnly()` directly:

```typescript
const result = router.routeKeywordOnly('Debug my Python code');
```

## Priorities

Control what matters most in model selection:

```typescript
// Presets
Priorities.balanced()     // quality=3, cost=3, speed=3
Priorities.performance()  // quality=5, cost=1, speed=1
Priorities.budget()       // quality=2, cost=5, speed=3
Priorities.fast()         // quality=2, cost=3, speed=5

// Custom
new Priorities(4, 2, 3)   // quality=4, cost=2, speed=3
```

## Adding Custom Models

```typescript
router.addModel({
  modelId: 'my-custom-model',
  provider: 'custom',
  benchmarks: { 'HumanEval': 85, 'MMLU': 80 },
  pricing: [0.001, 0.002],  // [input, output] per 1k tokens
  latency: 'fast',
});
```

## Adding Custom Benchmarks

```typescript
router.addBenchmark(
  'CustomerSupportQA',
  [
    'How do I reset my password?',
    'I want to cancel my subscription',
    'Where is my order?',
  ],
  'Customer support query handling',
  0,    // min score
  100,  // max score
);
```

## Filtering

```typescript
// Only Anthropic models
router.route('prompt', { filterProvider: 'anthropic' });

// Only models under $0.01/1k input tokens
router.route('prompt', { filterMaxCost: 0.01 });

// Only models with specific capabilities
router.route('prompt', { filterCapability: 'vision' });
```

## OpenRouter Integration

Route prompts and call the selected model through OpenRouter:

```typescript
import { OpenRouterIntegration, Router } from 'tryaii-dre';

const router = new Router();
const openrouter = new OpenRouterIntegration(router, {
  apiKey: process.env.OPENROUTER_API_KEY,
});

const response = await openrouter.chat('Write a quicksort in Python');
console.log(response.modelUsed);   // Which model was selected
console.log(response.content);     // The actual response
```

## Architecture

```
User Prompt
     |
     v
[Classifier] --> benchmark similarity scores
     |              (HumanEval: 0.8, MMLU: 0.3, ...)
     v
[ScoringEngine] --> weighted scores per model
     |              (quality * qW + cost * cW + speed * sW)
     v
[RouteResult] --> best model + reasoning
```

## Requirements

- Node.js >= 18.0.0
- TypeScript >= 5.3 (for development)

## License

Apache 2.0
