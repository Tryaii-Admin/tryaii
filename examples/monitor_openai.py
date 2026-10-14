"""Monitor an OpenAI client with tryaii, then inspect the records.

    pip install tryaii openai
    export OPENAI_API_KEY=...            # or OPENROUTER_API_KEY, see below
    python examples/monitor_openai.py
    tryaii monitor status --app examples

The model id stays in the code: it is the anchor tryaii measures the
best-for-less winner against, and the fallback. With mode="off" (the
default) nothing is swapped; records land under ~/.tryaii/monitor/examples/.
Switch a sub-process later with `tryaii monitor set summarize dynamic --app
examples` -- a running app picks it up within seconds.

To let tryaii swap across providers, point the client at OpenRouter:
    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])
"""

from openai import OpenAI

import tryaii.monitor as monitor

client = OpenAI()
monitor.wrap(client, app="examples")

with monitor.process("summarize"):
    reply = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": "Summarize in one sentence."},
            {"role": "user", "content": "tryaii routes each prompt to the cheapest model "
                                        "that is good enough for it, using benchmark data."},
        ],
    )
print(reply.choices[0].message.content)

# A per-call tag beats the process() block, and is removed before sending.
reply = client.chat.completions.create(
    model="gpt-5-mini",
    messages=[{"role": "user", "content": "Classify the sentiment of: 'great service, slow delivery'"}],
    tryaii_process="classify",
)
print(reply.choices[0].message.content)
