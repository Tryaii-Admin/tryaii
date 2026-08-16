# diagnose engine (SDK surface)

The `tryaii diagnose` CLI is the primary surface (see
[docs/cli/diagnose/](../cli/diagnose/README.md)), but the engine underneath
is public API in both SDKs, useful for embedding the checks in your own
tooling:

- **Python**: `tryaii.diagnose` — `analyze_inventory(data, opts,
  classify_fn=None, registry=None)`, `normalize_inventory`,
  `resolve_model_id`, `render_report_html(findings, previous=None)`, plus
  the run store (`write_run`, `latest_run_id`, `previous_run_id`,
  `list_run_ids`, `load_run_findings`).
- **Node**: the `tryaii/diagnose` subpath export (like `tryaii/cachelint`,
  deliberately not in the root barrel — it transitively loads the multi-MB
  tokenizer data) — `analyzeInventory` (async), `normalizeInventory`,
  `resolveModelId`, `renderReportHtml`, and the same store functions in
  camelCase.

`analyze_inventory` is deterministic and clock-free: `run_id` /
`generated_at` / `version` come in via `opts`, and classification comes from
each site's `_classification` seam or the injected `classify_fn(canonical)`
— the engine itself never touches a model, the network, or a clock. The full
behavior contract (inventory shape, degradation matrix, findings/summary
schema, report rendering) is `shared/diagnose/SPEC.md`; both engines conform
to the frozen fixtures in `shared/diagnose/fixtures/`.
