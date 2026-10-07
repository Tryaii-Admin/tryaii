# TryAii design partners

TryAii is looking for developers and teams who want to evaluate prompt-aware
model routing on a real workload. The initial program is suitable for AI SaaS
products, coding agents, AI agencies, and developers building multi-model
applications.

## Start locally

Install TryAii and route a representative sample of your prompts:

```bash
pip install tryaii       # Python 3.9+
# or: npm install tryaii # Node 18+

tryaii eval prompts.json --output ./routing-baseline
tryaii eval prompts.json --output ./routing-budget --max-price=1.00
```

Routing and reports stay on your machine. No account is required (`tryaii login`
is optional and only unlocks the full model catalog), no model APIs are called
by `eval`, and TryAii does not collect prompt telemetry.

## A useful design-partner workload

The most informative partners generally have:

- recurring prompts rather than a single experiment;
- a current model choice or routing rule to compare against;
- a meaningful cost, quality, latency, or maintenance problem;
- an outcome signal such as human preference, task success, retries, or
  escalation; and
- an environment where the router can first run on historical or shadow
  traffic.

Raw prompts are not required in a public application. Never post customer data,
credentials, private source code, or other sensitive material to GitHub.

## Pilot progression

1. **Historical evaluation** — route a representative prompt set locally and
   compare it with the current model policy.
2. **Disagreement review** — inspect cases where TryAii recommends a different
   model and label the decision as better, equivalent, worse, or unclear.
3. **Shadow mode** — log TryAii recommendations locally while production keeps
   using the existing model.
4. **Controlled adoption** — optionally enable routing for selected low-risk
   categories with explicit fallbacks and overrides.

Each pilot chooses one primary objective—lower cost at a quality guardrail,
higher quality at a fixed spend, lower latency, or simpler model operations—and
records the guardrails before traffic changes.

## Apply

Open a [design-partner application](https://github.com/Tryaii-Admin/tryaii/issues/new?template=design_partner.md)
with high-level, non-sensitive information about the product and workload. A
local `eval` report is useful context, but do not attach it if it contains
private prompts.
