# `tryaii diagnose report` — render a run to a self-contained HTML page

```bash
tryaii diagnose report                          # the latest run
tryaii diagnose report --run 20260814T101530Z   # a specific run
```

Renders `<out-dir>/<run-id>/findings.json` into `index.html` next to it:

- header (run id · generated at · tool version), the user's **goal** callout,
  priority chips
- five stat tiles: sites · findings · est. monthly cost · cache savings
  (max) · swap savings
- a **delta band** when a previous run exists — per-site `finding↔ok`
  transitions (`since <run>: N improved, M regressed, ...`) and the monthly
  estimate delta
- per-check status table + intake-skip details
- per-file **site cards**: four status chips each (green ok / amber finding /
  gray insufficient with its reason / dashed skipped) and expandable details —
  the full model ranking (your model always shown, even below the top 5),
  cache verdict + stable-prefix bar + recommendations, itemized cost
  arithmetic, hygiene findings with fix hints

The page is a **pure function of the stored findings** (the only timestamp is
the run's own `generated_at`), fully self-contained, and rendered from one
shared template (`shared/diagnose/report/template.html`) by both SDKs —
byte-identical output is enforced by fixtures.

| Flag | Default | Effect |
|---|---|---|
| `--run <id>` | the `latest` pointer | Which run to render |
| `--out-dir <dir>` | `.tryaii/diagnose` | Run store directory |
| `--out <file>` | `<out-dir>/<run-id>/index.html` | Write the HTML elsewhere |

Exit codes: 0 success · 1 no runs found / runtime failure · 2 usage error.
