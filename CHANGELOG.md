# Changelog

Dates are the measurement or release date. Price-table changes get their own entry, because a stale
price table makes every cost figure wrong without looking wrong.

## Unreleased

Audited by two independent Opus 5.5 agents, one executing every documented command against the
code, one writing tests for the untested surface. ~120 claims checked, 37 defects fixed.

**Behaviour changes worth knowing about:**

- **One data directory.** `$LACUNA_DATA`, default `~/.lacuna`. The previous fallback to
  `CLAUDE_PLUGIN_DATA` meant hooks and the CLI read *different* config files, so a setting made
  where the README said to was silently ignored by the cache guard.
- **`after.*` runs through a shell**, so `~` and `$VAR` expand. The output path is appended as the
  final argument, so a command with no arguments of its own receives it as `$1`. It previously ran
  with no shell at all, and neither README example could execute.
- **`lacuna doctor` exits 0 when only optional PDF parsers are missing.** It used to report a
  blocker and exit 1 on a machine that had everything the README calls a requirement.
- `lit verify` exits 4 instead of filing a model refusal as a fact-check; `synth` and `verify`
  exit 2 on a bad path instead of paying for a model call; `read` no longer mistakes a note left
  by an earlier run for its own output.
- A parse that fails validation is never passed to a reader, whatever its position in the chain.
- `lacuna config` shows the provenance of every value and every path, and reports a config file it
  could not parse instead of ignoring it.
- The cost ledger records `source_tokens`, which the calibration recipe in docs/extending.md needs.

**Corrected numbers.** Study 1 is 15 cells × 2 repeats (which is what makes 30 runs); study 2 is
18 plans at $39.56, not 20 at $52.25, and its 75 findings are two judges' raw count — 2.1 per plan
after averaging. The effort multiplier is 47× from the shipped calibration. Three parsers reached
100% recall, not one. The strong tier is not flat across effort levels.

Tests: 31 → 95.

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
