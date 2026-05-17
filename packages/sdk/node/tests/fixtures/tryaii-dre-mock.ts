/**
 * Test fixture that stands in for the real `tryaii-dre` core package.
 *
 * Vitest's resolve.alias (in vitest.config.ts) rewrites
 * `import ... from 'tryaii-dre'` to this file, so `DREClient` talks to
 * `FakeRouter` instead of the real embedding-backed `Router`. Tests can
 * mutate `fakeRouteResult` to control what the next `route()` call returns
 * and inspect `lastRouteCall` to verify what was passed through.
 *
 * This fixture is intentionally self-contained — it has no runtime
 * dependency on the real core package, so SDK tests run without needing
 * `packages/node` to be installed or built.
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type FakeRouteResult = any;

/** Default result — used until a test overrides it. */
function defaultResult(): FakeRouteResult {
  return {
    bestModel: 'gpt-4o',
    scores: [
      {
        modelId: 'gpt-4o',
        finalScore: 0.92,
        qualityScore: 0.88,
        costScore: 0.4,
        speedScore: 0.4,
        qualityContribution: 0.42,
        costContribution: 0.12,
        speedContribution: 0.12,
        topBenchmarks: [['MMLU', 0.88], ['HumanEval', 0.9]],
        reasoning: 'Quality: 0.88 on [MMLU (88%), HumanEval (90%)]',
      },
      {
        modelId: 'gpt-4o-mini',
        finalScore: 0.55,
        qualityScore: 0.7,
        costScore: 0.85,
        speedScore: 0.5,
        qualityContribution: 0.3,
        costContribution: 0.25,
        speedContribution: 0.15,
        topBenchmarks: [['MMLU', 0.7]],
        reasoning: 'Quality: 0.70 on [MMLU (70%)]',
      },
    ],
    classification: null,
    priorities: { quality: 3, cost: 3, speed: 3 },
  };
}

let fakeRouteResult: FakeRouteResult = defaultResult();

export function setFakeRouteResult(r: FakeRouteResult): void {
  fakeRouteResult = r;
}

export function resetFakeRouteResult(): void {
  fakeRouteResult = defaultResult();
}

export interface LastRouteCall {
  prompt: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  opts: any;
}

let lastRouteCall: LastRouteCall | null = null;

export function getLastRouteCall(): LastRouteCall | null {
  return lastRouteCall;
}

export function clearLastRouteCall(): void {
  lastRouteCall = null;
}

/** Stand-in for the real core Router. */
export class Router {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async route(prompt: string, opts: any): Promise<FakeRouteResult> {
    lastRouteCall = { prompt, opts };
    if (fakeRouteResult instanceof Error) {
      throw fakeRouteResult;
    }
    return fakeRouteResult;
  }
}

/** Stand-in for core's Priorities class — only `fromDict` is used by the SDK. */
export class Priorities {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  static fromDict(d: any): Priorities {
    return d as Priorities;
  }
}

/** Minimal mapping used by the SDK for OpenRouter slug resolution. */
export const MODEL_ID_TO_OPENROUTER: Record<string, string> = {
  'gpt-4o': 'openai/gpt-4o',
  'gpt-4o-mini': 'openai/gpt-4o-mini',
  'claude-sonnet-4-5-20250929': 'anthropic/claude-sonnet-4.5',
};
