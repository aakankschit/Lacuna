# VERDICT on Lacuna at HEAD 5d94f97

## NOT READY

I would not tell a colleague to install this today. The reason is narrow and fixable: the four money guarantees the README leans on do not all hold on the exact command sequence the README puts in front of a new user, and the 66-test suite cannot see any of the failures. Everything else I checked is in better shape than the author's own account suggests: the install works from GitHub, the suite is green on three Pythons in a scrubbed environment, the battery and the 126-mutant list are honest, the real pipeline reads two papers for two cents and writes faithful notes, and the cache guard holds a real idle session with the right dollar figure.

What makes it NOT READY rather than READY WITH CAVEATS is that two of the defects below silently report success with real models on the primary workflow (`synth` → `verify`), one poisons the note cache permanently, and one deletes a user's source document. None of them needs more than a few lines to fix, and each has a reproduction below. After those fixes, and the tests that go with them, I would expect to say READY WITH CAVEATS.

Verified means I ran it and show the output. Inferred is marked as such.

---

## 1. Defects that ship undetected by `tools/check.sh`

The suite is green at HEAD with every one of these present. All reproductions use a fake `claude` (`probes/sandbox.py`, derived from `tests/support.py`) unless marked REAL. Probe scripts: `probes/probe_a.py`, `probe_b.py`, `probe_c.py`; agent probes under `runs/agents/fresh/probes/`.

### 1.1 `synth` reports a stale plan as this run's output when the model refuses or crashes (wrong output, exit 0, hook fires)

`cmd_synth` never removes an earlier `--out` before the model call (`scripts/lit/lit_pipeline.py:419-420`). `read` had exactly this defect and fixed it ("Remove any earlier note first, so 'the file exists' can only mean this call wrote it", line 260); `synth` was not fixed.

```
$ lacuna lit synth ... --out plan.md --objective speed           # run 1, succeeds
$ lacuna lit synth ... --out plan.md --objective accuracy        # run 2, model returns is_error:true, writes nothing
plan (claude-opus-5): $0.002, 0s -> .../plan.md
  after_synth: ok
exit 0 | plan.md unchanged from run 1: True | after.synth fired: 2 times
```
(`probes/probe_c.py` D3; found by the fresh-eyes agent, reproduced by me. The agent also showed the same with non-JSON output from `claude`.) The ledger row says `wrote: true`. Suite gap: `test_an_apology_or_refusal_is_not_filed_as_a_plan_or_a_fact_check` runs synth with no earlier plan on disk; the read-stage "subtle one" test (test_pipeline.py:195-200) has no synth twin.

### 1.2 `verify` files a reply that checked nothing as a fact-check (REAL; wrong output, exit 0, hook fires)

The only success test is `is_error or not report.strip()` (`lit_pipeline.py:456`). A model's text reply always arrives with `is_error: false`:

```
$ printf 'Reply with exactly the sentence: I cannot help with that request.' | claude -p --model haiku --output-format json
{'is_error': False, 'result': 'I cannot help with that request.', 'total_cost_usd': 0.0027}
```

REAL, on the README workflow with the target given as a user types it:

```
$ lacuna lit verify --target runs/real/plan.md --repo <abs>/examples/demo-repo --notes runs/real/notes
verify: $0.0015, 31s -> runs/real/plan.md.unverified.md

I could not check any claims because the document is not in the codebase.
- `runs/real/plan.md` -> not found. ...
verify exit=0
```

The plan was not fact-checked; the stage exited 0, wrote `.unverified.md` and would have run `after.verify`. The same command with absolute paths worked and found two over-claims for $0.0100 (`runs/real/log-verify-abs.txt`). Two defects combine here: `cmd_verify` puts the relative `--target`/`--notes` in the prompt unchanged (only `--add-dir` is made absolute, line 449), and nothing judges the report's content the way `usable_output` judges a note. Fake reproduction of the pure refusal case: `probe_a.py` §2 → exit 0, `.unverified.md` contains "I can't help with that request.", `after.verify` fired.

Suite gap: the fake's `refuse` mode sets `is_error: true`; `test_relative_paths_and_txt_input_work_from_any_directory` covers synth and read, not verify.

### 1.3 Two documents with one file stem collide; the parallel reader then poisons the cache (free, fast, wrong answer, permanent)

Note path = `{out_dir}/{stem}.md` (line 253-254); eight readers run at once and each judges success by, and copies to its own cache key from, that shared path.

```
$ lacuna lit read papers/2023/review.md papers/2024/review.md --out notes
  review.md  $0.0120 1s
  review.md  $0.0120 1s
2/2 notes written, $0.024
notes/: ['review.md']                                   <- one note for two papers
cache/notes/review_4701050da4d5.md: source=.../2023/review.md
cache/notes/review_c06a125c2ed1.md: source=.../2023/review.md   <- the 2024 paper's key holds the 2023 note
$ lacuna lit read papers/2023/review.md papers/2024/review.md --out notes2
  review.md  cached (free)
  review.md  cached (free)
```
(`probe_a.py` §3; the fresh-eyes agent reproduced it 20/20 runs with no injected delay, and showed a refusal from one paper being filed as the other paper's note.) The same collision happens for `paper.txt` + `paper.pdf` (§11), `notes_3f2a9c1b7d4e.md` + `notes.md` (§10: a 12-hex suffix is stripped as if it were a cache hash), and the same file passed twice (§8: two paid calls, one note). Publisher downloads named `fulltext.pdf` or `main.pdf` in per-paper folders are the ordinary trigger.

Suite gap: every multi-document test uses distinct stems. No mutant concerns duplicate outputs.

### 1.4 A markdown document inside `--out` is deleted, then overwritten by its own note (data loss)

```
$ lacuna lit read vault/paper.md --out vault
exit 0 | source still exists: True | unchanged: False
source now begins: '# Note on paper.md ...'
```
(`probe_a.py` §4.) `os.remove(out)` at line 262 runs before the agent is spawned, so the agent is told to read a file that no longer exists; with a real model that is an apology, exit 4, and the paper is gone. On a cache hit the source is overwritten by the note. The agent also showed the same lines deleting any unrelated `<out>/<stem>.md` and writing through a symlink. The README recommends pointing `--out` at an Obsidian folder.

### 1.5 `synth` leaves the apology in `--out` after exit 4, and the next README command consumes it

```
$ lacuna lit synth ... --out out/plan.md      # model writes "BLOCKED - not completed ..." into plan.md
exit 4 | files left in out/: ['plan.md', 'plan.md.filelist.txt']
$ lacuna lit verify --target out/plan.md ...  # exit 0, paid model call, writes plan.md.unverified.md
$ lacuna lit synth --digest out/plan.md ...   # exit 0, accepts the apology as a digest
```
(`probe_a.py` §1.) CHANGELOG records the identical defect fixed for `read` ("A rejected note stayed in --out, where the next synth read it as literature"). Suite gap: the apology test asserts exit 4 and no hook, then overwrites plan.md without asserting it was removed.

### 1.6 `--status` and a relative `LACUNA_DATA` reach the agent as relative paths

```
$ cd proj && lacuna lit synth --notes lit/notes --repo . --out lit/plan.md --kind plan --status research/STATUS.md
prompt: "... the project status file research/STATUS.md in full ..."
agent cwd: .../proj/lit
does that path resolve from the agent's cwd? False
```
(`probe_b.py` §6.) The dead-ends file is silently ignored; auto-detection without `--status` gives an absolute path. With `LACUNA_DATA=data` the parsed-paper and book paths in the reader prompt are relative too (`probe_c.py` D7). ARCHITECTURE.md lists relative paths as defect class 3 and the suite tests them for `--repo`, `--notes`, `--out` only.

### 1.7 `lacuna spend` crashes on a real transcript shape (REAL)

```
$ lacuna spend --session ~/.claude/projects/.../adf98282-....jsonl
  File ".../scripts/cost/spend.py", line 65, in session_report
ZeroDivisionError: float division by zero
exit 1
```
13 of the 309 transcripts on this machine have that shape (one assistant row, all-zero usage: a session that died on its first API call). `/spend` reports the newest transcript for the cwd, so a user whose last session failed at its first prompt gets a traceback from `/spend`.

### 1.8 Ordinary phrases in a correct note discard it, and every retry pays again

A fully templated note whose Claim reads "Of 412 patients, 37 had not completed follow-up (Table 2)" is rejected: `! the note opens with 'not completed'`, exit 4, nothing cached, one paid call per attempt (`probe_c.py` D5; the agent showed "cannot complete" and "unable to complete" likewise). CHANGELOG records this class fixed for the bare word "blocked"; the phrase list still contains phrases that occur in clinical and complexity-theory papers. The FAILED line also misreports "agent produced no note file".

### 1.9 Smaller confirmed defects

- A 0-byte or partial file in `cache/md` (an interrupted parse) is served forever: no re-parse, the reader is handed an empty file, ledger `source_tokens: 0` (`probe_b` §12). prep_md writes the cache path non-atomically.
- A space anywhere in the books path crashes every `lit read` while a book exists (`re.findall(r"- index: (\S+)")`, line 240) → `FileNotFoundError: '.../Application'`, exit 1 (`probe_c` D8). macOS `~/Library/Application Support` is the obvious trigger.
- Valid JSON that is not an object from `claude` → `TypeError` after the paid call; the note stays in `--out`, uncached, no ledger row (`probe_c` D11). Whether the real CLI ever emits that is unconfirmed.
- Outside the 0/2/3/4 exit-code contract (all exit 1 with a traceback): `claude` not on PATH (read/synth/verify), `git` not installed (synth), `after.read: true` in config.json (after a paid stage; the docstring says "nothing here may raise"), a top-level-list `pricing.json` (`lacuna config`). `Paper.MD`, `paper.markdown`, `paper.rst` exit 3 "no PDF parser interpreter" for a text file.
- Ledger records `"books": true` whenever `--no-books` is absent, even with no book present (real ledger). Failed synth leaves `plan.md.filelist.txt`. A failed verify leaves the previous `.unverified.md` beside a changed target.

---

## 2. The five claims

| # | Claim | Verdict | Settled by |
|---|---|---|---|
| 1 | Five independent audits; 57+ defects found and fixed | UNTESTABLE as stated; two "fixed" classes have a live twin | No audit artefacts in the repo beyond CHANGELOG prose. The battery's 16 shipped defects do stay fixed. But §1.5 is the synth twin of the fixed read defect, and §1.2 is the verify twin of "lit verify exits 4 instead of filing a refusal" |
| 2 | 66 tests pass on 3.9, 3.12, 3.14 in a scrubbed environment | HOLDS | `tools/check.sh` → `Python 3.9.6 OK (Ran 66 tests)`, `3.14.2 OK`, `3.12.9 OK`, 45 s (`runs/check.log`). CHANGELOG says 65 tests: off by one |
| 3 | Battery replays 18 shipped defects; suite catches 16/16 that apply | HOLDS literally | `python3 tools/mutation_battery.py .` → `caught 16/16 applied mutations`, 2 skipped (anchor moved) (`runs/battery.log`). `tools/mutate.py` → `126/126 mutants killed`, 4m26s (`runs/mutate.log`, matrix in `runs/mutate-matrix.json`). Only one mutant is killed solely by the source-fingerprint test, and it is the one that test exists for. The 4 tests that kill no mutant are the shipped-state and doc checks TESTING.md exempts. The caveat the author conceded is real: the battery sees none of §1.1–1.8 |
| 4a | Bad input never reaches a model | HOLDS for missing paths (exit 2, zero calls, suite + my runs); FAILS by omission | An input inside `--out` (§1.4), colliding stems (§1.3), an unwritable `--out`, an empty `.md` all reach a paid reader |
| 4b | A refusal is never filed as output | FAILS for verify (§1.2, REAL) and for synth with a stale plan (§1.1) | HOLDS for read: `refuse`/`apology`/`prose` all exit 4 and cache nothing (suite), and a real reader run behaved |
| 4c | Caches miss when they should | HOLDS for every keyed input (suite; real re-read: `cached (free)`, 0 calls, 0.1 s); FAILS by collision (§1.3) and by a stale 0-byte parse (§1.9) | |
| 4d | A failed stage writes nothing | FAILS for synth (§1.5: apology plan + filelist stay) and verify (§1.2 writes the non-check) | HOLDS for read |
| 5 | Installs from GitHub and works for a stranger | HOLDS | Below |

---

## 3. Install and first run as a stranger (REAL)

GitHub HEAD is 5d94f97 (`git ls-remote`). I followed README from the top, with `--scope local` instead of `--scope user` as the rules required.

```
$ claude plugin marketplace add https://github.com/aakankschit/Lacuna.git     ✔ (clones over HTTPS)
$ claude plugin install lacuna@lacuna --scope local                           ✔ installPath .../plugins/cache/lacuna/lacuna/0.1.0
$ diff -rq <installPath> checkout -x .git                                      identical (one .in_use marker)
```

Note for the author: `marketplace add` wrote the marketplace into `~/.claude/settings.json` (`extraKnownMarketplaces`). That is the CLI's behaviour, and the README does not say a settings file is touched. I removed it at the end (see §8).

| README step | Result |
|---|---|
| `claude plugin list --json` one-liner and the `lacuna()` shell function | work; field names `id`, `installPath` are correct |
| `lacuna doctor` | exit 0, creates data dir, config.json (empty `models`), copies calibration; three parsers reported as optional notes with exact fix commands |
| `lacuna spend --days 7` | works; reads 303 transcripts, no install needed |
| `lacuna config` | works, shows provenance |
| `/lacuna-doctor`, `/spend` in Claude Code (haiku, headless) | both work. `/spend` is not reproduced verbatim by the model (project labels shortened), though the command text asks for verbatim |
| first-run example: `synth` on `examples/` then `verify` | synth $0.278, 84 s; verify $0.0062, 33 s; total $0.284. README says $0.302 + $0.0099 ≈ $0.31. HOLDS. My verify found no over-claims where the README's run found four; that is run-to-run variance, and the README states it as a past result |
| `doctor --install-pdf` | works (pymupdf4llm into `$LACUNA_DATA/venv`) |
| `doctor --install-fallback` (docling) | 1m05s, 1.4 GB |
| `doctor --install-equations` (marker) | 1m26s, 1.3 GB venv. README says "~3 GB of venv and models"; where marker put its models I did not establish (`~/.cache/datalab` holds 232 KB) |
| plugin hooks | verified executing through the plugin loader (`--debug` log: `SessionStart:startup [python3 .../cache_guard.py] (plugin lacuna@lacuna) finished with status 0`, same for `UserPromptSubmit`) |

## 4. The pipeline against real models (REAL; data dir `runs/data-user`, ledger `runs/data-user/cost-ledger.jsonl`)

| Stage | Command | Cost | Wall | Outcome |
|---|---|---|---|---|
| estimate | `lit estimate papers/*.pdf --per-agent 1` (+ `--json`) | free | 0.7 s | 2 docs, ~38.7k tokens, haiku/medium $0.02 predicted; budget warning names sonnet/max |
| read | `lit read papers/*.pdf --out notes --brief ...` | $0.020 | 49 s | 2/2 notes, 1060 and 910 words, structured, numbers spot-checked against the PDFs. README "~$0.015/doc" HOLDS |
| read again | same | $0 | 0.1 s | `cached (free)`, 0 model calls |
| synth plan | `--kind plan` vs `examples/demo-repo` (claude-opus-5) | $0.347 | 114 s | 8 proposals grounded in grepped files; 2316 words against the prompt's "under 2,000" |
| verify (relative target) | | $0.0015 | 31 s | **checked nothing, exit 0** (§1.2) |
| verify (absolute) | | $0.0100 | 54 s | two real over-claims found. README "$0.02–0.09" is an over-estimate here |
| digest | `--kind digest` (sonnet) | $0.112 | 44 s | 2007 words, organised by objective |
| read, paths with spaces | `"runs/real/my papers/zhao 2025 copy.pdf" --out "runs/real/my notes"` | $0.0066 | 34 s | works |
| read, image-only PDF | 4 rendered pages, no text layer | $0.006 | 1m55 total | `pymupdf4llm failed validation (only 0 chars for 4 pages ...) -> trying docling`; docling OCR parse accepted; note written. `lit estimate` on it: exit 2 "no extractable text" |
| read `--equations` | marker on zhao2025 | $0.0104 | 2m54 total | cached as `_p3_marker.md`, 36 lines with `$` LaTeX against 2 in the pymupdf4llm parse |
| cache_guard | real idle transcript, 233k ctx, 2.7 h idle, opus-5-5 | | | `decision: block ... ≈$1.87` (233,311 × $8/MTok = $1.866); resend passes |
| split-book | `papers/klarich2024.pdf` | | | correct refusal: no table of contents, exit 2 |
| pane | `lacuna pane plan.md --once` | | | renders |

Reference stripping on both real parses left no bibliography entries (checked with the project's own ENTRY regex over the cache files).

Total real-model pipeline spend: see §8.

---

## 5. Did cutting 193 tests to 66 lose anything?

Yes. The coverage agent (`runs/agents/cov/REPORT.md`) read all 195 old tests against HEAD (the old suite had 195, not 193), judged 166 fully covered, 27 partly, 2 not at all, and wrote 35 one-line mutants aimed at the assertions that were cut. 34 leave the HEAD suite green; the 35th is killed only by the source-fingerprint test. The agent also ran the old tests on each mutated copy and reports every one caught (after one fixture change: the old fake note needed a `## Claim` heading), and it re-ran `tools/mutate.py` as a control (126/126 killed). I re-ran its nine most harmful mutants through `tools/mutate.py`'s own runner (`probes/verify_cov_mutants.py`); all nine survive, `Ran 66 … OK`:

| Mutant (one-line change) | What a user would see | Old test that caught it |
|---|---|---|
| `lit_cost.py:169` directory scan drops `.txt` | pre-spend estimate hundreds of times too low, `any_over_budget: false` | `TestLitCostCLI.test_counts_tokens_of_a_directory` |
| `cmd_synth` stops making `--notes` absolute | the planner (claude-opus-5) runs on notes it cannot open; paid | `Flow.test_relative_paths_work_from_any_directory` |
| `lacuna_config._merge` uses `base[k]` | every command crashes with `KeyError: '_comment'` once `doctor` has written config.json | `TestConfigSections.test_unknown_keys_are_harmless` |
| reader prompt uses `books_block()` instead of `books` | `--no-books` ignored in the prompt while the note is cached under the no-books key | `TestBooksReachTheReader.test_no_books_removes_the_block` |
| `cmd_verify` writes the header without the report | `.unverified.md` is empty: a clean bill of health | `TestVerifyOutcome.test_clean_report_is_saved_and_handed_to_the_after_hook` |
| `spend.py:127` divides by `hit['all']` | `/spend` crashes on a fresh install with no transcripts | `Flow.test_the_cost_tools_need_nothing_installed` |
| `bin/lacuna` routes `estimate` to a wrong path | the skill's approval step is broken | `Flow.test_estimate_is_free_and_refuses_what_it_cannot_price` |
| `SKIP_DIRS` loses `venv` | the planner's "authoritative file list" includes a virtualenv | `TestRepoDiscovery.test_walk_files_skips_…` |
| `statusline.py:117` `elif` → `if` | "cold · re-caches ≈$1.20" shown on a warm cache | `TestStatusline.test_warm_cache_and_plan_usage_segments` |

Two of these are not hypothetical: the `--notes`-relative mutant is the same class as the shipped §1.6, and the verify-body mutant sits next to the shipped §1.2. The HEAD suite's `test_relative_paths_and_txt_input_work_from_any_directory` passes a relative `--notes` but asserts only on `Codebase:` in the prompt, and nothing at HEAD runs `bin/lacuna spend|doctor|lit estimate` through the dispatcher.

The agent found almost nothing in the deleted tests that tested behaviour the code no longer has: all 195 old assertions still hold on HEAD source. The deletion that was correct was the old fake's note format; the tests built on it were not themselves obsolete.

My own reading agrees, and adds one thing the mutant count does not show: what was lost is also the *habit* the old integration suite had of re-running stages over existing outputs. §1.1 and §1.5 are precisely re-run cases.

## 6. Novel mutation hunt

[AGENT-MUT]

## 7. Documentation against the code, and TESTING.md

The documentation agent (`runs/agents/docs/REPORT.md`, 175 numbered claims, every one with the command run) found 124 HOLD, 27 FAIL, 14 MISLEADING, 10 UNTESTABLE: one claim in four wrong, against the author's "one in three". It also found §1.1, §1.2 and §1.8 independently by executing the docs. The ones that matter to a user, each reproduced by me:

- **README's equations recipe fails on its own.** `doctor --install-equations` then `lit read x.pdf --equations` → exit 3 "no PDF parser interpreter": `to_markdown` requires the pymupdf4llm venv for every PDF (`need_venv()`, and `pdf_stats` runs inside it). The error's advice, "Run `lacuna doctor`", installs nothing. Needs `--install-pdf` too; the README does not say so.
- **extending.md's config schema, copied verbatim, crashes a paid read.** The `"..."` placeholders are valid JSON and accepted as relative paths; `lit read` creates a directory named `...` in the cwd, calls the model, then dies with `IsADirectoryError` on the ledger (exit 1, 1 paid call).
- **README's "permanently" example** (`{"models": {"planner": "claude-opus-5", "reader": "haiku"}}`) sets the two values to their defaults, which doctor's own comment says pins them against future improvements.
- **extending.md says `after.*` runs "with the stage's output appended as `$1`"** beside an example with arguments; for `import --kind plan` the path is `$3`. README and CHANGELOG state it correctly.
- **`lacuna config` prints `idle_minutes` from the file and ignores `LACUNA_IDLE_MIN`**, which the guard honours; it also marks no provenance for `after`, budget, idle or the data dir.
- **`models.coder` / `models.tester` affect nothing** but the SessionStart banner; the dev-cycle skill's normal path uses agent-frontmatter models, as the skill itself admits.
- **`lacuna lit split-book` on a corrupt PDF: exit 1 with a `pymupdf.FileDataError` traceback** (real venv), outside the contract.
- **Running the suite the way every test file's docstring says** (`python3 -m unittest discover -s tests`, not through `check.sh`) goes red when the user has a `pricing.json` in `LACUNA_DATA`: `test_dedupes_and_prices_the_split` hard-codes haiku list prices and `ccost.PRICE` reads the user override at import.
- **Counts:** CHANGELOG "193 tests, 2,978 lines" → 195 and 3,006 (`git show f94c831`); "65 tests" → 66; the "31 → 119 … test_integration.py" paragraph describes a file that no longer exists; the "24 of 117" figure cannot be reproduced from anything in the repo. `docs/statusline.md`'s sample shows `↻83h19m`; the code prints `↻3d11h` above 48 h.
- `PROMPT_VERSION` has no guard like the `PARSE_VERSION` fingerprint: editing `NOTE_TEMPLATE` serves the old cached note free and the suite stays green, while extending.md says the edit "would change the prompt version and correctly invalidate every cached note".

My own checks of documentation claims:

- CHANGELOG "65 tests": 66. CHANGELOG "126 defects, every one killed": true. TESTING.md "about five minutes" for mutate.py: 4m26s.
- TESTING.md says tests of "the text of the source code" belong at no level; `test_parse_version_is_bumped_when_parsing_changes` fingerprints `inspect.getsource` of `validate`, `clean`, `body`. It is a justified exception and should be listed as one.
- ARCHITECTURE.md says the data dir "deliberately does not fall back to `CLAUDE_PLUGIN_DATA`"; `statusline/statusline.py:24` still reads `CLAUDE_PLUGIN_DATA`. Harmless (the status line runs from user settings), but the two disagree.
- `agents/paper-reader.md` carries a different note template (seven sections, `model: haiku`, `effort: low`) from `NOTE_TEMPLATE` in the pipeline (six sections, effort from config); the pipeline never uses the agent file, so the shipped agent describes a note the pipeline does not write.
- The lit-review skill: "verify ~$0.02-0.09" — my real runs were $0.0015–$0.0100.
- README "Read a document with equations faithfully" HOLDS; "docling ~1 min/document" HOLDS (4 pages OCR inside 1m55 end to end).

**TESTING.md as a strategy.** A contributor can apply rules 1–3 and 5 mechanically: add a mutant, confirm the kill, run `tools/mutate.py --only`. I did that in reverse for every defect above (the matrix shows which test would have to change) and the process is clear. What the document does not give a contributor is the thing that produced §1: a rule for *scenarios*, not mutants. Every pipeline test drives one document per name, one run per output path, a fake that can only refuse with `is_error: true`, and absolute paths from a clean cwd. "Put a test at the lowest level that can observe the defect" is good advice once you know the defect; nothing in TESTING.md tells you to re-run a stage over its own output, to pass two inputs with one stem, or to give a stage a relative path. So: it is a real strategy for *guarding fixes* (post-hoc by design, and honest about it), and not yet a strategy for *finding* the next defect. That is the gap the author named, and it is real.

The documentation agent followed TESTING.md literally for one hypothetical fix (the §1.1 defect) and recorded where it broke down; I checked each point against `runs/mutate-matrix.json`:

- **Rule 4 ("a test whose every kill is also made by a test at its own level or below is redundant") would delete the three behavioural parser tests.** Every prep_md mutant they kill is also killed by the source-fingerprint test at the same level (my matrix: 14 of 15 prep_md mutants have exactly `[fingerprint, one TestParse test]` as killers). The exception swallows the rule.
- The fingerprint fires on a comment edit inside `validate()` and stays silent when the parser *invocation* changes (the agent changed `to_markdown(..., pages=[0..4])` on a real PDF: 10% of the text gone, suite green).
- "When clean() or validate() changes, also run prep_md.py by hand on real papers" is impossible for a newcomer: no papers ship and the benchmark corpus is stated as unpublished.
- "One-line change" per mutant: 26 of the 126 entries are multi-line. "A new test names its mutant": `test_usage_and_skill_name_the_json_flag` has none and is not in the rule-4 exemption list.
- TESTING.md arrived in the same unverified WIP commit (3cef568) as the suite it describes, and its exemptions name existing tests by content. That is the shape of a description written after the fact.

---

## 8. What I spent, what I touched, what I could not test

**Spend** (list price, from `claude -p` JSON and the Lacuna ledger):

[SPEND-TABLE]

**Touched outside this directory, and undone:** the plugin install (`claude plugin uninstall lacuna@lacuna`, `claude plugin marketplace remove lacuna`), which removes the `extraKnownMarketplaces` entry the CLI wrote into `~/.claude/settings.json`. Read-only use of `~/.claude/projects` transcripts for `spend` and the cache guard. `~/.lacuna`, `~/Desktop/Lacuna` untouched. No pushes. The marker venv wrote its model cache wherever marker puts it (not under `LACUNA_DATA`); I did not locate or remove it.

**Agents:** four `claude -p --model opus` agents with narrow briefs and their own `LACUNA_DATA` (`runs/agents/{cov,mut,docs,fresh}/`), reports in each `REPORT.md`. Every claim of theirs that appears above I reproduced myself; the ones I did not are marked as the agent's.

**Not tested, and why:** the dev-cycle skill end to end (it is an instruction document for an interactive session; its headless recipe I did not run); `lacuna split` (needs an interactive tmux); the Windows/Linux claims; the benchmark numbers in `docs/benchmarks/` (they are measurements, not code, and LIMITS.md already scopes them); whether a real `claude -p` ever returns a non-object JSON (§1.9); a real reader refusal on a document (could not be induced); concurrent `lit read` processes on one PDF.

---

## 9. What must be fixed first (all small)

1. `cmd_synth`: remove `a.out` (and don't write the filelist) before the model call; remove `a.out` on failure. Add the synth twin of the read tests: stale plan + refusal → exit 4, plan gone; apology → plan gone.
2. `cmd_verify`: abspath `--target`/`--notes` in the prompt; judge the report with the same content check as notes (refusal markers, "could not check"); extend the fake with an `is_error: false` refusal mode and a relative-path verify test.
3. `cmd_read`: before any model call, exit 2 if two jobs share an `out` or if `realpath(md) == realpath(out)`; drop duplicate `(md, key)` jobs. Test with `a/x.md` + `b/x.md`.
4. `--status` → abspath; `DATA` → abspath. Test `--status research/STATUS.md` from inside the repo.
5. `spend.py:65`: guard `usd == 0`. Test with a zero-usage transcript (13 exist on the author's own machine).
6. Apply `REFUSAL_MARKERS` only when the template headings are absent, or only to text before the first heading.
7. prep_md: write `out + ".tmp"` then `os.replace`; pipeline: treat a 0-byte cache file as absent. Quote-safe book index parsing (line 240).
