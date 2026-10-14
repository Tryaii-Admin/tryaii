// Monitor an OpenAI client with tryaii, then inspect the records.
//
//     npm install tryaii openai
//     export OPENAI_API_KEY=...            // or OPENROUTER_API_KEY, see below
//     node examples/monitor_openai.mjs
//     tryaii monitor status --app examples
//
// The model id stays in the code: it is the anchor tryaii measures the
// best-for-less winner against, and the fallback. With mode 'off' (the
// default) nothing is swapped; records land under ~/.tryaii/monitor/examples/.
// Switch a sub-process later with `tryaii monitor set summarize dynamic --app
// examples` -- a running app picks it up within seconds.
//
// To let tryaii swap across providers, point the client at OpenRouter:
//     new OpenAI({ baseURL: 'https://openrouter.ai/api/v1', apiKey: process.env.OPENROUTER_API_KEY })

import OpenAI from 'openai';
import { wrap, process as subprocess } from 'tryaii/monitor';

const client = new OpenAI();
wrap(client, { app: 'examples' });

const reply = await subprocess('summarize', () =>
  client.chat.completions.create({
    model: 'gpt-5-mini',
    messages: [
      { role: 'system', content: 'Summarize in one sentence.' },
      {
        role: 'user',
        content:
          'tryaii routes each prompt to the cheapest model that is good enough for it, using benchmark data.',
      },
    ],
  }),
);
console.log(reply.choices[0].message.content);

// A per-call tag beats the process() block, and is removed before sending.
const classified = await client.chat.completions.create({
  model: 'gpt-5-mini',
  messages: [{ role: 'user', content: "Classify the sentiment of: 'great service, slow delivery'" }],
  tryaii_process: 'classify',
});
console.log(classified.choices[0].message.content);
