# Changelog

Dates are the measurement or release date. Price-table changes get their own entry, because a stale
price table makes every cost figure wrong without looking wrong.

## Unreleased

**Review of the seven unreviewed commits** (2026-10-13), against the real papers and with two
independent reviewers. Found and fixed, in order of harm:

- **Reference stripping did not work on the default parser.** The ACS heading the commit claimed
  to cover is rendered by pymupdf4llm as `# ■<sup>**REFERENCES**</sup>`, which the regex missed, so
  one paper's whole bibliography (9% of its tokens) went to every reader; and "cut to the next
  heading" stopped after two entries of another paper because the parser puts a "Publisher's Note"
  box inside the list. Citations are now stripped entry by entry, from the first References heading
  on, and everything that is not a numbered entry with a year stays. Measured on three papers:
  every entry gone, every legend and acknowledgement kept. `PARSE_VERSION` is 3, and a test now
  fails if the parser changes without a bump (it was changed once without one).
- **A note was discarded over an ordinary word.** The refusal check matched "blocked" and "cannot
  access" anywhere in a note; a paid note about blocked sampling was thrown away and re-paid on
  every retry. A note is now judged by its structure (the template's `## Claim` heading), the
  phrase list is only what an apology says, and the one bare word kept is `BLOCKED` in capitals.
- **A rejected note stayed in `--out`**, where the next `synth` read it as literature. It is removed.
- **A mostly scanned PDF passed validation** because the per-page floor was dropped whenever the
  PDF had any text at all, so the OCR fallback never ran. Both checks apply now.
- **`split-book` chose level 1 by counting entries**, so an e-book with Cover, Copyright, Contents
  and Preface bookmarks still got 140-page chapters. The level is chosen by how coarse its largest
  chapter is.
- **The cache guard raised at import** on a hand-edited `idle_minutes`, breaking every prompt; and
  its compaction check treated any undated line as a recent compaction, switching itself off.
- **`lacuna config` crashed** on the string `budget_warn_usd` that `lit estimate` had been taught
  to tolerate. A null `effort` crashed `read`. A `.txt` document got a note named `paper..md`.
- The status line called a `prompt_cache` with no expiry time "cold"; `lit estimate` told a user
  with a corrupt PDF to install the parser they had; the parse-error filter could never match
  what it meant to drop; table rows wider than 300 characters were split in two.
- The note cache key now covers the contents of the book indexes offered, not only their paths,
  so a re-split book is a different book.
- The default `tester` is `sonnet`: the skill and the benchmark both say the strong tier's edge as
  test author did not replicate, and the default disagreed with both.
- Documentation corrected where it still asserted what the commits retracted: the marker
  concurrency rule and its cause, what validation checks, llama.cpp's role, docling's timing, the
  dev-cycle headless recipe (it named a file that does not ship), the second task's sample size.

**The suite.** Four files and 193 tests, 2,978 lines, became three files by level and 65 tests,
about 1,700 lines, with one fake `claude` in `tests/support.py`. The old suite let 24 of 117
injected defects through, among them twelve audit fixes with no test; the new one is held to
`tools/mutate.py`, 126 defects, every one killed. `TESTING.md` is the strategy.

**Earlier in this cycle.** Audited by two independent Opus 5.5 agents, one executing every
documented command against the code, one writing tests for the untested surface. ~120 claims
checked, 37 defects fixed.

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

Tests: 31 → 119, now in three suites — unit, per-module, and a new
`tests/test_integration.py` covering whole journeys against a fake `claude`. All mutation-tested.

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
