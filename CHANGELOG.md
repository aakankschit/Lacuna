# Changelog

Dates are the measurement or release date. Price-table changes get their own entry, because a stale
price table makes every cost figure wrong without looking wrong.

## 0.1.0 — 2026-10-13

First public release. Everything in it comes from four benchmarks run 2026-10-12/13; see
`docs/benchmarks/`.

- `lacuna` CLI: `lit read|synth|verify|estimate|split-book`, `spend`, `doctor`, `config`, `pane`,
  `split`. Exit codes 0/2/3/4 are part of the contract.
- Skills: `lit-review`, `dev-cycle`, `results-pane`. Agents: `paper-reader`, `coder`, `test-author`.
- Commands: `/spend`, `/lacuna-doctor`.
- Hooks: prompt-cache guard on `SessionStart` and `UserPromptSubmit`; results-pane protocol
  (silent unless running inside the pane layout).
- Status line as a documented manual install — a plugin cannot ship `statusLine`.
- Price table: `scripts/pricing.json`, retrieved 2026-10-12.
- Shipped calibration is marked `_source: upstream`; `lacuna lit estimate` says so when it uses it.
