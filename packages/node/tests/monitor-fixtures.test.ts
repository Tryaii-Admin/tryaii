/**
 * Golden-fixture conformance for the TypeScript monitor engine.
 *
 * The fixtures in shared/monitor/fixtures/ were generated from the PYTHON
 * reference engine and frozen (shared/monitor/SPEC.md §4) — this suite proves
 * the TS port reproduces every expected block byte-for-byte: structural
 * equality via toEqual PLUS JSON.stringify equality, which also pins key
 * ORDER (events and status documents are byte-diffed across SDKs).
 *
 * Every suite is routing-free and clock-free: scores arrive in the daemon's
 * wire shape and `now` is an input, so no test here needs the embedding
 * model or a daemon. Each gen* function below mirrors its namesake in
 * scripts/gen-monitor-fixtures.py.
 *
 * The two CLI-driven suites spawn the built CLI instead: `cli` in
 * monitor-cli.test.ts and `apps` (through the TRYAII_HTTP_FIXTURE seam,
 * request log included) in apps-cli.test.ts.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { starterBundle } from '../src/catalog/bundle.js';
import {
  aggregate,
  allowedModels,
  evaluatePin,
  filterRanking,
  managedTarget,
  nativeModelId,
  pickWinner,
  pinFromFirstCall,
  policyAllowed,
  providerClass,
  reachable,
  resolveAnchor,
  resolveApp,
  resolveMode,
  resolveProcess,
  routeAllowed,
  routedModel,
  routingText,
  setProcessMode,
  shadowAllowed,
  store,
  windowSeconds,
} from '../src/monitor/index.js';
import { ModelPolicy } from '../src/policy.js';
import { ModelRegistry } from '../src/registry/models.js';

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'shared', 'monitor', 'fixtures');

const SEVEN_DAYS = 7 * 86400;

interface Case {
  name: string;
  description: string;
  input: Record<string, any>;
  expected: any;
}

function cases(suite: string): Case[] {
  const path = join(FIXTURES, suite, 'cases.json');
  if (!existsSync(path)) return [];
  return JSON.parse(readFileSync(path, 'utf-8')).cases as Case[];
}

/** Structural equality + serialized equality (pins key order). */
function expectFrozen(got: unknown, expected: unknown): void {
  expect(got).toEqual(expected);
  expect(JSON.stringify(got, null, 2)).toBe(JSON.stringify(expected, null, 2));
}

let registryCache: ModelRegistry | null = null;
function registry(): ModelRegistry {
  if (registryCache === null) registryCache = ModelRegistry.fromBundle(starterBundle());
  return registryCache;
}

function storeDir(name: string): string {
  return join(FIXTURES, 'corpus', name);
}

/**
 * The case's policy-allowed ids: `policy_allowed` (a list, or null = no
 * policy) as given, or `policy` (a ModelPolicy dict) evaluated against the
 * starter bundle -- which carries no origins.json, so an active policy
 * fails closed to [].
 */
function casePolicyAllowed(inp: Record<string, any>, reg: ModelRegistry): string[] | null {
  if ('policy' in inp) return policyAllowed(ModelPolicy.fromDict(inp.policy), starterBundle().origins, reg.modelIds);
  return inp.policy_allowed ?? null;
}

function genExtract(inp: Record<string, any>): unknown {
  const [text, promptHash] = routingText(inp.messages);
  return { text, prompt_hash: promptHash };
}

function genResolve(inp: Record<string, any>): unknown {
  const reg = registry();
  const provider = providerClass(inp.base_url);
  const anchor = resolveAnchor(inp.model, reg);
  const policy = casePolicyAllowed(inp, reg);
  const ids = reg.modelIds;
  return {
    provider,
    anchor,
    anchor_reachable: reachable(provider, anchor, anchor),
    allowed_models: allowedModels(provider, ids, anchor),
    native: anchor ? nativeModelId(provider, anchor) : null,
    policy_allowed: policy,
    route_allowed: routeAllowed(provider, ids, anchor, policy),
    shadow_allowed: shadowAllowed(ids, policy),
  };
}

function genWinner(inp: Record<string, any>): unknown {
  const reg = registry();
  const anchor = inp.anchor ?? null;
  const provider = inp.provider ?? 'openai';
  const mode = inp.mode ?? 'off';
  const allowed = inp.allowed ?? null;
  const scores = inp.scores ?? [];
  const picked = pickWinner(scores, anchor, reg, allowed);
  const winner = picked.winner;
  const inPath = filterRanking(scores, routeAllowed(provider, reg.modelIds, anchor, allowed));
  const routedWinner = pickWinner(inPath, anchor, reg);
  let status: string | null = null;
  let routed: string | null;
  if (mode === 'managed') {
    const decision = managedTarget(provider, anchor, inp.managed_model ?? null, reg, allowed);
    routed = decision.target;
    status = decision.route_status;
  } else {
    routed = routedModel(mode, provider, anchor, routedWinner.winner, inp.pin_model ?? null, allowed);
  }
  return {
    winner: picked,
    winner_reachable: reachable(provider, winner, anchor),
    routed_winner: routedWinner,
    routed,
    model_to_send: routed ? nativeModelId(provider, routed) : null,
    route_status: status,
  };
}

function genPin(inp: Record<string, any>): unknown {
  if ('first_call_winner' in inp) return pinFromFirstCall(inp.first_call_winner, inp.now);
  let events = inp.events;
  if (events === undefined || events === null) events = store.readEvents(storeDir(inp.store), inp.now, SEVEN_DAYS);
  let pin = inp.pin;
  if (pin === 'from_store') pin = store.readPins(storeDir(inp.store)).processes[inp.process] ?? null;
  return evaluatePin(events, pin, inp.now, inp.process);
}

function genConfig(inp: Record<string, any>): unknown {
  const out: Record<string, unknown> = {};
  if ('mode' in inp) {
    const m = inp.mode;
    out.mode = resolveMode(m.process, { env: m.env, remote: m.remote, args: m.args, config: m.config });
  }
  if ('app' in inp) {
    const a = inp.app;
    out.app = resolveApp({ env: a.env, args: a.args, cwdName: a.cwd_name });
  }
  if ('process' in inp) {
    const p = inp.process;
    out.process = resolveProcess({ perCall: p.per_call, context: p.context, default: p.default, callSite: p.call_site });
  }
  if ('set' in inp) {
    const s = inp.set;
    try {
      out.set = setProcessMode(s.config, s.process, s.mode, s.model ?? null);
    } catch (error) {
      out.set = { error: (error as Error).message };
    }
  }
  return out;
}

function genStatus(inp: Record<string, any>): unknown {
  const since = inp.since ?? '24h';
  const dir = storeDir(inp.store);
  const events = store.readEvents(dir, inp.now, windowSeconds(since));
  const cursor = store.readUploadCursor(dir);
  return aggregate(
    inp.app ?? inp.store,
    events,
    store.readPins(dir),
    store.readConfig(dir),
    inp.now,
    since,
    inp.env ?? null,
    store.readRemote(dir),
    cursor,
    store.uploadedEventCount(dir, cursor),
  );
}

const SUITES: Record<string, (inp: Record<string, any>) => unknown> = {
  extract: genExtract,
  resolve: genResolve,
  winner: genWinner,
  pin: genPin,
  config: genConfig,
  status: genStatus,
};

describe.skipIf(!existsSync(FIXTURES))('monitor fixtures', () => {
  for (const [suite, gen] of Object.entries(SUITES)) {
    describe(suite, () => {
      const list = cases(suite);
      it('has cases', () => {
        expect(list.length).toBeGreaterThan(0);
      });
      for (const c of list) {
        it(c.name, () => {
          if (c.expected === null) return;
          expectFrozen(gen(c.input), c.expected);
        });
      }
    });
  }
});
