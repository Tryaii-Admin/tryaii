#!/usr/bin/env node
/**
 * P1 spike (SPEC.md §3.1): confirm js-tiktoken and Python tiktoken produce
 * identical o200k_base counts before any tokenize/analyze fixture is frozen.
 *
 * Prints one JSON array of counts for scripts/spike-cachelint-probes.json,
 * encoding special tokens as ordinary text — encode(text, [], []) — to mirror
 * Python's encode(text, disallowed_special=()).
 *
 * Compare with the Python side:
 *   python - <<'EOF'
 *   import json, tiktoken
 *   enc = tiktoken.get_encoding("o200k_base")
 *   probes = json.load(open("scripts/spike-cachelint-probes.json", encoding="utf-8"))
 *   print(json.dumps([len(enc.encode(p, disallowed_special=())) for p in probes]))
 *   EOF
 *
 * js-tiktoken is resolved from packages/node/node_modules (install it there,
 * e.g. `npm install --no-save js-tiktoken` before the dependency lands).
 */
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

const require = createRequire(new URL('../packages/node/package.json', import.meta.url));
const { getEncoding } = require('js-tiktoken');

const probes = JSON.parse(
  readFileSync(new URL('./spike-cachelint-probes.json', import.meta.url), 'utf-8'),
);
const enc = getEncoding('o200k_base');
const counts = probes.map((p) => enc.encode(p, [], []).length);
console.log(JSON.stringify(counts));
