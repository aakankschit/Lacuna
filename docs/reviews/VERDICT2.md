# VERDICT2: Lacuna at HEAD `783d6ef`

## READY WITH CAVEATS

I would tell a colleague to install it, with the six caveats below. I ran it as a new user against
real models: install from GitHub, `doctor`, the README first run, both papers read, the cache, a plan
against a repo with a dead-ends file, a relative-path `verify`, a digest and an equations read. Every
stage did what the README says, at the README's prices. All nine NOT READY findings and the
adversary's three are fixed **at the place they were reported**. That includes the two the last judge
reproduced with real models: the relative-target `verify`, and the cache guard staying silent after a
session limit. I re-ran both with real models and both now behave correctly.

What I will not repeat is the author's claim that **"each fix was applied as a class."** It is false
in ten of the twelve rows below. The fix for §1.8 also re-opened §1.5 by a new route. The suite cannot
see four of the fixes: revert any one of them and it stays green. `tools/mutate.py` is red at HEAD.
And because the plugin version is still `0.1.0`, anyone who installed before these fixes is told
`claude plugin update` has nothing to do.

Why it is not NOT READY: the previous verdict's blockers were silent failures that ordinary input
triggered on the README path. Those are gone. What I found instead is mostly on failure paths, in
secondary tools, or in release hygiene. I tried to make each failure-path defect happen with real
models (§5.3). None of the four real model failures I induced took the shape the new regression lets through, a
marker phrase below a heading. The real planner wrote `BLOCKED` and was caught. Two others were
honest partial outputs that both the old and the new check accept. Why it is not
READY: one silent-wrong-output path has an ordinary trigger (caveat 1). The content check is a phrase
list that real partial outputs pass (caveat 2). And `/spend` under-reports in ways that matter for a
cost tool (caveat 3).

**Caveats for a colleague installing today**

1. **Do not read two documents with the same file name into one `--out` in separate runs.** On the
   second run the first paper's note is replaced without a word, and `synth` then plans from one paper
   fewer. (`main.pdf` and `fulltext.pdf` in per-paper folders are the usual trigger.) The fix catches
   this only within one invocation. See N1.
2. **Exit 0 means a file was written that does not *look* like an apology. It does not mean the stage
   did its job.** A real `synth --kind digest` that could read none of the notes was filed as a
   successful digest, exit 0. Read what you get before an `after.*` hook files it anywhere. See N2.
3. **Treat `/spend` and `lacuna spend` as a lower bound.** They miss 9 idle re-caches worth $39.66 (of
   $86.89 avoidable) on this machine. They also price refused-then-fallback attempts at $0, which
   Anthropic bills for some refusal categories (`bio` among them). See N5 and §2.
4. **Editing a note inside `--out`, deleting the cache, or moving to a new machine makes every later
   `read` into that folder exit 2** until the notes are moved. Nothing is lost or spent. The error
   message wrongly says Lacuna did not write them. See N6.
5. **The status line is behind the CLI.** After a session-limit or API error it shows `last $0.000`.
   It ignores a `pricing.json` under a `~/…` data directory, and goes blank on a malformed one. See N7.
6. **`claude plugin update` will not deliver fixes**, because the version never changes. Uninstall and
   reinstall to upgrade; I tested that this works. See N3.

Verified means I ran it, and the command and output are cited. Agent findings I reproduced are
marked as reproduced. Those I did not reproduce are marked as the agent's.

---

## 1. The twelve prior findings

Reproductions: `judge/probes/twelve.py <repo>` re-runs each report's own reproduction against a repo,
using the fake `claude` from `tests/support.py`. I ran it on HEAD (`judge/runs/twelve-head.log`) and on
`5d94f97` (`judge/old`, `judge/runs/twelve-old.log`). REAL rows used real models (§5).

| # | Finding | Status | Settled by | What is left of the class |
|---|---|---|---|---|
| V1.1 | `synth` reports a stale plan as this run's after a refusal | **FIXED** | `twelve.py` V1_1, HEAD: `run2 exit=4 plan unchanged=True after.synth fired 1x total ledger wrote=[True, False]`, plus "…was left as it was, and holds an earlier run's output". Old: `exit=0 … fired 2x … wrote=[True, True]`. Same for non-JSON output | When the model writes `--out` itself instead of `.partial`, the same message is false (N10a) |
| V1.2 | `verify` files a reply that checked nothing | **FIXED ONLY WHERE REPORTED** | HEAD: both reported replies → `exit=4 .unverified.md exists=False hook fired=False`, prompt `Fact-check this document: /private/var/…` (absolute). Old: `exit=0 … exists=True hook fired=True`, prompt `plan.md`. **REAL**: `cd repo && lacuna lit verify --target lit/plan.md --repo . --notes ../notes` → `$0.0095, 50s`, three real findings, exit 0 | Only the exact phrasings in `REFUSAL_MARKERS` are caught. `I can’t help` (curly apostrophe), `I wasn't able to…`, a refusal as the first list item, and one under a heading are all filed with exit 0 (`judge/runs/verify-agent-sv.log`). New false positives in N9 |
| V1.3 | Two documents with one stem poison the note cache | **FIXED ONLY WHERE REPORTED** | HEAD: `a/review.md + b/review.md: exit=2 model calls=0`, same for `paper.txt + paper.pdf`. `notes_3f2a9c1b7d4e.md + notes.md` → 2 notes, 2 correct cache keys. Same file twice → 1 call. Old: one note, a poisoned key | Across runs the guard is blind: N1. NFC/NFD names on APFS: `a/café.md` + `b/café.md` → `exit 0 … notes ['café.md']`, both cache keys hold paper b's note, same as old (`judge/runs/nfd.log`). Removing the `casefold` survives the suite |
| V1.4 | A document inside `--out` is deleted/overwritten | **FIXED ONLY WHERE REPORTED** | HEAD: `source inside --out: exit=2 calls=0 source unchanged=True`. An unrelated `<out>/<stem>.md` and a symlink at the note path are also refused with 0 calls. Old: `exit=0 calls=1 source unchanged=False` | A Lacuna note given as its own document (`read notes/*.md --out notes`) is still deleted before a paid call, then a `FileNotFoundError` traceback, exit 1, same as old (`judge/probes/read_verify.py`; read agent F2). New fail-closed behaviour: N6. Removing the `islink` check survives the suite |
| V1.5 | `synth` leaves an apology in `--out` for the next command | **REGRESSED** | The reported case is fixed: `synth exit=4 files left in out/=[]; next verify exit=2`. But `judge/probes/regress.py`: an apology one heading down (`# Development plan\n\n## Status\n\nNot completed. I could not read any of the notes…`), or as a list item, is now **`exit=0 filed as plan.md=True`**, and `after.synth` fires. At `5d94f97` the same files gave `exit=4` | Caused by the §1.8 fix (N2). The same change lets a template-shaped apology note be cached and served free on every later run (reproduced: `F2 templated apology note: exit=0 cached=1; re-run … new calls=0 ['cached', '(free)']`; old `exit=4`). This breaks ARCHITECTURE.md's "A refusal must never become a note" |
| V1.6 | `--status` and a relative `LACUNA_DATA` reach the agent relative | **FIXED ONLY WHERE REPORTED** | HEAD: `--status relative: … absolute=True`; `LACUNA_DATA=reldata: … add-dirs absolute=True`. Old: `'research/STATUS.md' absolute=False`. **REAL**: `cd repo && lacuna lit synth --notes ../notes --repo . --out lit/plan.md --kind plan --status research/STATUS.md` → the plan lists roulette-wheel selection under "rejected", citing the dead end in STATUS.md (`judge/runs/real/proj/repo/lit/plan.md:193`) | The `verify` prompt still tells the model to grep `research/STATUS.md`, relative to the caller's cwd (synth/verify agent F8, docs agent F6). `DATA` itself is not made absolute, so with a relative `LACUNA_DATA` a hook and a shell read different `config.json` files (hooks agent D8; pre-existing) |
| V1.7 | `spend` divides by zero on a zero-usage transcript | **FIXED** | HEAD: `exit=0 '■ This session … 1 API calls'`. Old: `exit=1 … ZeroDivisionError`. The hooks agent checked every other division (`fleet_report` uses `max(…,1)`, `bar()` uses `or 1`, the status line has none) | — |
| V1.8 | Ordinary phrases in a correct note discard it, and every retry pays again | **FIXED ONLY WHERE REPORTED** | HEAD: "Of 412 patients, 37 had not completed follow-up", "cannot complete in polynomial time" and "unable to complete 12% of instances" → `exit=0 note kept=True`. Old: `exit=4 note kept=False` | The same defect is re-created in `verify` by the new markers: `I'm unable to confirm the following 2 claims:\n- …` and `I could not verify any of the following numbers:` → `exit=4 filed=False`, and the report is lost (old: filed). An upper-case `BLOCKED` anywhere in the first 400 characters still discards a note or report (`- JobState.BLOCKED -> not found` → exit 4). The fix caused the V1.5 regression |
| V1.9 | Smaller items | **FIXED ONLY WHERE REPORTED** | HEAD, every item: 0-byte parse cache `parses=2` (re-parsed); space in books path `exit=0`; `books` false with no book; non-object JSON `exit=0`; `claude` missing → `exit=4`, no traceback (read/synth/verify); `git` missing → `exit=0`; `after.read: true` → exit 0 with "could not run True"; `pricing.json = []` → exit 0; `Paper.MD`/`.markdown`/`.rst` → exit 0; failed verify on a changed target → old report removed; failed synth leaves no filelist. Old: crashes, `exit=3`, stale | Two twins of the fixes: `lit estimate DIR` still scans `.md/.txt/.pdf` only, so in a mixed directory it prices 1 of 4 documents that `read` then pays for (N8). The status line crashes on the malformed `pricing.json` shapes the fix now tolerates elsewhere (N7). The note cache is still written with a non-atomic `shutil.copy`, the twin of the parse-cache fix (read agent F7, low realism) |
| F2 | After an API error, `cache_guard` sees 0 tokens of context | **FIXED ONLY WHERE REPORTED** | **REAL**: 21 transcripts on this machine end in a `<synthetic>` line. HEAD holds on the 5 with large context (`…re-caches all 549k tokens… ≈$10.98`, also $7.84, $1.55, $1.25, $0.87), where old was silent on all 21 (`judge/runs/guard/`) | FINDINGS §2 named `statusline.py:73` too. It is byte-identical to `5d94f97`: same transcript → `status line … 'last $0.000'` (the real call cost $0.030). The skip is a raw substring test: 35 real assistant lines in 5 transcripts contain the text (all Lacuna review sessions, this one included). The hooks agent built cases that go wrong both ways and found 0 of 687 real prompts decided differently. **Reverting the fix leaves the suite green** |
| F3 | A `~` in `LACUNA_DATA` splits the data directory | **FIXED ONLY WHERE REPORTED** | HEAD: `LACUNA_DATA='~/lac' lacuna config --json` → `reader='from-file'`, and `doctor` creates no `./~`. Old: `reader='haiku'`, and `./~` is created | `statusline.py:23` reads the variable unexpanded: with a 100× opus price in `~/lac/pricing.json`, the status line says `last $0.020` while `spend` says `$2.00`. The `cache_guard` fallback branch (line 28) is unexpanded too. **Reverting the fix leaves the suite green** |
| F4 | Docs say an `after.*` hook gets the output as `$1` | **FIXED ONLY WHERE REPORTED** | The prose in `docs/extending.md:76-86` and `skills/lit-review/SKILL.md:133` now says "appended as the final argument". The docs agent ran the README example: `$1=--kind $2=plan $3=<plan.md>`, as documented | The table FINDINGS cited is still headed "\| Hook \| `$1` is \|" (`extending.md:90`). Two comments still say `$1`: the `after()` docstring (`lit_pipeline.py:123`) and the DEFAULTS comment (`lacuna_config.py:50`). Cosmetic |

**Tally:** 2 FIXED, 9 FIXED ONLY WHERE REPORTED, 1 REGRESSED, 0 NOT FIXED. Every reported instance
now behaves correctly. The class claim holds for two rows of twelve.

### Is each fix guarded?

TESTING.md rule 3 says "the revert of your fix is the mutant". I reverted each fix on a scratch copy
and ran the suite (`judge/probes/reanchor.py`, `reanchor2.py`; logs `judge/runs/reanchor*.log`):

- **Killed (12):** refusal opening, verify content check, synth judged at `.partial`,
  stem-collision guard, `lacuna_wrote`, spend zero-guard, `--status` abspath, 0-byte parse cache,
  stale verify report, filelist cleanup, stale `.partial` removal, non-dict JSON.
- **Survive (4; suite green with the fix undone):** the `<synthetic>` skip, the `LACUNA_DATA`
  expansion, `casefold` in the collision guard, and `islink` in `lacuna_wrote`. The adversary's tests
  for the first two ship in `docs/reviews/adversary-tests.py`, outside `tests/`, so nothing runs
  them. The docs agent separately ran all 35 one-line reverts through `mutate.py`'s own runner and
  got the same four survivors.

---

## 2. The refuted claim: `spend` undercounts by 78% via `usage.iterations`

**The refutation is not sound. The original claim is not right either. The truth lies between them,
and for the author's own chemistry project it may be closer to the claim.**

What I measured (all read-only, `judge/probes/iter_*.py`, logs `judge/runs/iter-*.log`):

- Of 5,959 assistant calls carrying `usage.iterations`, **81 unique calls have more than one
  iteration** (116 if a forked session's copies are counted twice): 79 `(message, message)` and 2
  `(message, fallback_message)`. In every one, the top-level usage equals the **last** iteration
  only. They sit in **4 transcripts**, all interactive sessions in the author's TACTICS projects.
  `cb60aaa1` is a fork of `8c332e5b` and shares 82 of its 83 calls. **None is a headless `claude -p`
  run.**
- **Why the refutation proves nothing.** It "checked against the authoritative `total_cost_usd` of
  eight headless runs". No headless run on this machine has a multi-iteration call, so those eight
  could only agree. And `total_cost_usd` is not a bill: it is Claude Code's own client-side
  arithmetic over the same top-level usage. Claude Code's per-model totals in that session's
  `cost-state` line match the transcript's top-level usage **exactly** and leave out the fallback
  iteration (`claude-fable-5-1: input 34, output 592, cache read 92,927, cache write 32,638` against
  the transcript's identical top-level sums). The second argument, "output_tokens sums to 1.000×",
  cannot detect the gap: 76 of the 79 `(message, message)` first iterations have 0 output tokens. The money is in
  cache writes.
- **Why the claim is not established.** Its "certain" fallback case and its "independent
  corroboration" are both wrong. The `$0.706` that Claude Code charged `claude-fable-5-1` in session
  `e076f39c` is exactly the session's *other* top-level fable calls. That it matched the fallback
  iteration's $0.709 is a coincidence. Anthropic's billing rules
  ([Refusals and fallback](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback#how-refusals-are-billed))
  say a refusal **before any output is not billed** unless its `stop_details.category` is `bio`,
  `frontier_llm` or `reasoning_extraction`. A refusal **mid-stream** bills its input and the output
  already streamed. So the 78% figure is the upper bound, true only if every earlier iteration was
  billed.
- **Why it may still matter here.** The affected sessions are all in the author's drug-discovery
  project, the one the README says needs "a model that does not refuse its source material". That
  project records a `"category": "bio"` refusal (`2c91b98d`), and `bio` pre-stream refusals **are**
  billed. The categories of the 79 `(message, message)` first iterations are not recorded, so no
  one can settle it from the transcripts.

Priced with Lacuna's own `ccost` (`judge/runs/iter-dollars.log`):

```
8c332e5b  calls 126 multi 79   spend $62.65   all iterations $285.28
cb60aaa1  calls  83 multi 35   spend $23.44   all iterations $121.95
2c91b98d  calls 165 multi  1   spend $78.09   all iterations $78.84
e076f39c  calls 244 multi  1   spend $55.49   all iterations $56.20
TOTAL                          spend $219.67  all iterations $542.28
deduplicated by message id     spend $196.43  all iterations $420.52   (cb60aaa1 is a fork of 8c332e5b)
```

Deduplicated, the exposure is **up to $224.09** on this machine, confined to sessions that hit
classifier refusals. `spend` agrees with Claude Code's own `/cost` here. It is wrong only where Claude
Code is wrong as well. I count this as a caveat, not a blocker. The honest fix is cheap: report
refused attempts on their own line ("N refused attempts, $X if billed"). Do not drop them, and do not
add them in silently.

---

## 3. New defects, most harmful first

Every item has a reproduction under `judge/probes/` or an agent's `repro/` directory, and the output
is in `judge/runs/` or the agent's `logs/`. NEW means the behaviour differs from `5d94f97` because of a
fix. PRE means it is the same at `5d94f97`.

**N1. PRE, silent, ordinary trigger. Two same-named documents read on different days into one
`--out`: the second replaces the first's note.**
`judge/probes/read_verify3.py`: day 1 reads `downloads/smith2023/main.md`, day 2 reads
`downloads/jones2024/main.md`, both with `--out notes` →
`day1 exit=0 day2 exit=0 … notes/ = ['main.md']; notes/main.md is: '# Jones 2024'`. There is no
warning. The cache is correct, but `--out` (what `synth` reads) holds one of the two papers.
`lacuna_wrote()` asks whether Lacuna wrote the file, not whether it wrote it *for this document*, so
the new ownership check waves this through. Found by the read agent (F4); I reproduced it.

**N2. NEW plus PRE. The content check is a phrase list. Moving it to the "opening" let structured
apologies through, and real partial outputs pass it on both versions.**
- NEW (fake): a synth apology under `## Status` or `## Summary`, or as the first list item, is filed
  with exit 0, `wrote: true`, and `after.synth` fires (`judge/runs/regress.log`). The synth/verify
  agent's table r3 finds the same for five shapes, all `exit=4` at `5d94f97`. A note template filled
  with an apology is cached and served free from then on (reproduced; read and docs agents F1).
- PRE (**REAL**): `synth --kind digest` with the notes unreadable (`chmod 000`), using the default
  sonnet digester, cost $0.044 and gave **exit 0**. The digest it filed is titled "PARTIAL:
  literature notes unreadable" and says "This digest contains no literature claims". Both the HEAD
  and the `5d94f97` `usable_output()` accept that file (`judge/runs/real/failures.log`). The real
  haiku reader given garbled text wrote an honest "Not determinable" note, which both versions accept
  and cache.
- Weighed: the real opus-5 planner in the same situation wrote `BLOCKED` and was correctly rejected
  (`exit=4`, nothing left, $0.40). So the new regression needs a failure phrased with a marker phrase
  below a heading. I did not see one in four induced real failures, which is why this is a caveat and
  not a blocker.

**N3. NEW (release). Existing installs cannot receive these fixes.** `plugin.json` has been `0.1.0`
since the first commit. I installed in an isolated HOME and set the install state (marketplace
clone, plugin cache, `gitCommitSha`) to exactly what a `5d94f97` install leaves
(`judge/install/upgrade-gh`). Then I ran the documented update:
`claude plugin marketplace update lacuna` → `✔ Successfully updated marketplace` (clone now
`783d6ef`), then `claude plugin update lacuna@lacuna` →
`✔ lacuna is already at the latest version (0.1.0).` The installed copy still lacks `lacuna_wrote`.
`claude plugin uninstall lacuna@lacuna` followed by `claude plugin install lacuna@lacuna` does deliver
the fixes (`after reinstall, has fixes: True`).
The previous judge's real install on this machine (`~/.claude/plugins/cache/lacuna/lacuna/0.1.0`) is
in that state. A fresh install from GitHub is byte-identical to HEAD (`diff -rq` exit 0).

**N4. NEW (process). The guard rails the project set itself are not met at HEAD.**
- `python3 tools/mutate.py` → `121/126 mutants killed; NOT caught: plan-judged-by-size,
  refusal-words-ignored, txt-note-named-with-dot, no-books-flag-ignored, tilde-not-expanded`. All
  five are `ERROR anchor occurs 0 times`: the fix moved the code and did not update `MUTANTS`. I
  re-anchored all five at HEAD and the suite kills each one (`judge/runs/reanchor-mutate.log`), so
  this is a broken runner, not lost protection. Still, TESTING.md says "before pushing … every mutant
  killed", and the CHANGELOG's "126 defects, every one killed" is false at HEAD.
- Four fixes have no killing test (§1). No fix added a `MUTANTS` entry (`git diff 5d94f97 HEAD --
  tools` is empty). The CHANGELOG has no entry for this round.

**N5. PRE (silent, cost tool). `spend`'s "avoidable cold rewrites" misses about 45% of them.**
`ccost.is_cold` requires a cache read under 40k, but after an idle gap real re-caches still read the
warm ~42.2k–42.7k shared prefix. My own count over this machine's transcripts: **84 counted
($47.23); 9 more ($39.66) with ≥1 h idle, >40k written and <50k read**, the largest
`6.5 h idle, 647,597 written, 42,210 read, $6.69`. The hooks agent (D2) found the same with
slightly different criteria (11, $44.18). The report then labels these turns "large context
re-read". In the other direction, the fleet report deduplicates message ids only within a file, so a forked
session is counted twice: `cb60aaa1` repeats 82 of `8c332e5b`'s calls ($23.44). That is small, and
the adversary measured it the same way.

**N6. NEW, loud, nothing spent. Ownership by byte-equality refuses legitimate re-runs.**
`judge/probes/obsidian.py`: read two papers into `vault`, append one line to `vault/a.md`, add a
third paper, re-run → `exit 2`, with `ERROR: …papers/a.md would overwrite …vault/a.md, which Lacuna did not
write (or which has been edited since). Move it, or choose another --out. Nothing was sent to a
model.` Reading only the new paper works. The same happens after `rm -rf $LACUNA_DATA/cache`, on a new
machine, and (read agent) after a SIGHUP/SIGTERM'd run. Each run names only the first offender.
`extending.md:156` ("Deleting [the cache] costs only the money to re-read") is now false. HEAD is
safer than `5d94f97`, which silently overwrote the edit, but nothing tells a user about the new rule.

**N7. PRE (twins of fixes). The status line.** `last $0.000` after a `<synthetic>` line. It ignores
`~/…/pricing.json`. With `pricing.json` = `[]`, `"x"` or `{"models": [1,2]}` it exits 1 with
`AttributeError` and prints nothing (`judge/probes/hooks_verify.py`, same at old). All three code
paths were hardened in `lacuna_config` and not here.

**N8. NEW mismatch. `lit estimate DIR` ignores the extensions `read` now accepts.** A directory
holding `a.md B.MD c.markdown d.rst` → `estimate … {'documents': 1, 'total_tokens': 34486}`, then
`read` on the four files → `exit 0 model calls 4`. Explicit file arguments count all four, and a
directory with *only* new extensions fails loudly (exit 2). The silent case needs a mix. The read
agent adds (PRE) that `Paper.PDF` is priced as raw bytes.

**N9. NEW (fake), fail-closed. `verify` now discards correct reports that open with a preamble.**
See V1.8. Weighed: in **7 real haiku verify runs** every report opened with `- ` or "No unverifiable
claims found." (`judge/runs/real/vrep/`, `first/`, `verify-rel.log`), as the prompt asks. Low
likelihood. The real runs also show variance: one of five identical runs found nothing where the
others found one or two.

**N10. Smaller (each reproduced unless marked).**
- a. If the model writes `--out` itself, the run exits 4 and claims `--out` "holds an earlier run's
  output". The file actually holds this run's output, which may be an apology the next command
  consumes (regress.py; synth/verify agent F5). Not observed with a real planner, which used
  `.partial` both times.
- b. A symlinked `--out` plan is replaced by a regular file, and the link target keeps the old plan
  (reproduced).
- c. An unreadable `--target` → `PermissionError` traceback, exit 1, before any call (the new sha
  stamp opens it).
- d. `<synthetic>` substring skip false positives (F2 row).
- e. A 244-byte stem → the cache name exceeds 255 bytes, so the run exits 1 after the paid call
  (read agent F6; my fake could not parse the spaced name, so not reproduced by me. The arithmetic
  is 244+1+12+3 = 260).
- f. A plan written with `synth --out` into a vault silently replaces a hand-edited plan. The new
  ownership rule covers `read` only (docs agent F5).

---

## 4. Gates

| Gate | Result |
|---|---|
| `tools/check.sh` | `Python 3.9.6 OK (Ran 69 tests)`, `3.14.2 OK`, `3.12.9 OK`, 57 s (`judge/runs/check.log`) |
| `python3 tools/mutation_battery.py` (run on a byte-identical copy, `judge/battery-repo`, because it edits files in place) | `caught 16/16 applied mutations`. Two skipped: "note cache ignores which books exist" and "a long apology counts as output", both `anchor appears 0x — code moved` (`judge/runs/battery.log`) |
| Are the two skips moved code or lost protection? | **Moved code.** Re-anchored at HEAD, both are killed: `books_fp = "constant"` → `FAIL: test_books_are_offered_and_keyed_by_what_exists`; `refusal()` `hit = None` → `FAIL: test_a_verify_that_checked_nothing_is_not_filed_as_a_fact_check`. So is the variant that hashes the book list without contents (`judge/runs/reanchor.log`) |
| `tools/mutate.py` | **121/126**, 5 stale anchors, all killed when re-anchored (N4) |
| Fix reverts | 4 of the 16 I tried survive (§1) |

---

## 5. Real pipeline as a new user

Install: `claude plugin marketplace add https://github.com/aakankschit/Lacuna.git` and `claude plugin
install lacuna@lacuna --scope user`, into an isolated HOME (`judge/install/newuser`) so that
`~/.claude/settings.json` was not touched. The installed copy is byte-identical to HEAD, and GitHub
`HEAD` is `783d6ef` (`git ls-remote`). An isolated HOME is not logged in, so model calls ran as the
real user against that installed copy, with `LACUNA_DATA=judge/runs/data-user`. Ledger:
`judge/runs/data-user/cost-ledger.jsonl`, 17 calls, $1.30.

### 5.1 The README, top to bottom

| Step | Cost | Wall | Result |
|---|---|---|---|
| `doctor` | — | — | exit 0, data dir created, `config.json` empty, parsers listed as optional with fix commands |
| `spend --days 7`, `config` | free | <1 s | work |
| First run: `synth` on `examples/` → `verify` | $0.273 + $0.0096 | 82 s + 54 s | Plan written via `.partial` then renamed (nothing left behind). Verify found two real mis-attributions. README "≈$0.31": **holds** ($0.283) |
| `doctor --install-pdf` | — | 11 s | ok |
| `lit estimate papers/*.pdf --per-agent 1` | free | — | 2 docs, ~38.7k tokens, haiku/medium $0.02, budget warning names sonnet/max |
| `lit read papers/*.pdf --out notes/ --brief …` | $0.021 | 67 s | 2/2 notes, 1,429 and 1,121 words (the template asks for under 1,100). I spot-checked 11 numbers in the Zhao note against the parse, e.g. `2cmp_02 83.1 vs 85.4, p 0.030`; all present. README "~$0.015/doc": **holds** |
| same command again | $0 | 0.07 s | `cached (free)` ×2 |
| plan with a relative `--status` dead-ends file, from inside the repo | $0.344 | 102 s | the dead end was respected (V1.6 REAL) |
| `verify --target lit/plan.md` (relative, as typed) | $0.0095 | 50 s | real check, three findings (V1.2 REAL) |
| `synth --kind digest` | $0.133 | 51 s | ok |
| `doctor --install-equations`, then `read zhao2025.pdf --equations` (the only marker process) | $0.0104 | 1m20 + 2m20 | `_p3_marker.md`, 36 lines with `$` against 2 in the default parse. The marker model cache (`~/Library/Caches/datalab`, 262 MB) was unchanged before and after |
| `/spend` via `claude --plugin-dir <installPath> -p "/spend" --model haiku` | $0.0035 | — | works. Debug log: `SessionStart:startup [... lacuna/0.1.0/hooks/cache_guard.py] (plugin lacuna@inline) finished with status 0`, same for `results_hook.py` and `UserPromptSubmit` |

The README's verify range "$0.02 – $0.09" is still high. My 8 real verifies cost $0.006–$0.011.

### 5.2 Prior findings re-run with real models

V1.2 (relative target) and V1.6 (relative `--status`) are in the table above. F2 ran on 21 real
transcripts (§1).

### 5.3 Real model failures, induced (`judge/probes/real_failures.sh`, `judge/runs/real/failures.log`)

| Induced failure | Model | What it wrote | Lacuna | Right? |
|---|---|---|---|---|
| plan, notes `chmod 000` | claude-opus-5 | a file opening `BLOCKED` | exit 4, nothing left, $0.40 | yes |
| digest, notes `chmod 000` | sonnet | "PARTIAL: literature notes unreadable … contains no literature claims" under `## Status of this digest` | **exit 0, filed** ($0.044). Old check also accepts | no: N2 |
| verify, repo `chmod 000` | haiku | fact-checked a *sibling copy* of the repo it found nearby, said so, and listed what it could not check | exit 0, filed | honest, but not what was asked |
| read, garbled text | haiku | template-shaped "Not determinable … no recoverable natural-language content" | exit 0, cached. Old check also accepts | defensible: the input is the problem, and a new parse has a new key |

---

## 6. Documentation against code

The docs agent (`judge/agents/docs/REPORT.md`) walked README, extending.md and ARCHITECTURE.md by
executing them against the fake: 66 claims, **42 HOLD, 8 FAIL, 8 MISLEADING, 8 UNTESTABLE**. Every
VERDICT §7 item except the `$1` prose is still true at HEAD, because `b87e0c2` changed none of
README, CHANGELOG, TESTING.md, ARCHITECTURE.md or statusline.md. I re-checked these:

- The README's equations recipe still needs `--install-pdf` too (`need_venv()` runs for every PDF).
  It worked for me only because I had installed pymupdf4llm first.
- `extending.md:156` "Deleting it costs only the money to re-read": now false (N6).
- ARCHITECTURE.md:71 "A refusal must never become a note": now false (V1.5 row, N2).
- ARCHITECTURE.md and TESTING.md "run `tools/mutate.py` before pushing" / "every mutant killed":
  false at HEAD (N4).
- `extending.md:90` table header "`$1` is" (F4 row).
- The commit's countable claims hold: 66 → 69 tests, green on 3.9/3.12/3.14, battery 16/16. Its
  qualitative claim, "each fix … applied as a class", fails (§1).

---

## 7. Agents, and how far to trust them

Four `claude -p --model claude-opus-5-5 --effort xhigh` agents, each with its own clones of HEAD and
`5d94f97` and its own `LACUNA_DATA`, fake `claude` only. Briefs are in `judge/agents/*/BRIEF.md`
(shared rules in `PREAMBLE.md`), reports in `REPORT.md`.

| Agent | Cost | Claims I reproduced | Not reproduced by me (marked as theirs) |
|---|---|---|---|
| synth/verify | $2.93 | F1 synth apology under a heading, F2 cached apology note, F3 verify false positives, F4 verify false negatives, F5 model writes `--out`, F6 symlinked `--out`, F9 unreadable target | F7 `.unverified.md` left stale after a successful synth (plausible from the code) |
| read | $4.22 | F1, F2 note-as-source deletion, F3 NFC/NFD, F4 cross-run stem collision, F5 cache-less re-run | F5's SIGHUP/SIGTERM rows, F6 long name (my fake could not run it), F7 truncated cache note |
| hooks | $5.17 | D1 estimate mismatch, D2 cold threshold (with my own criteria), D3/D4 status-line twins, D6 status-line crash, D7 unguarded fixes | D5's constructed both-ways cases (I measured only how common the trigger is) |
| docs | $6.27 | F2 stale anchors and 4 surviving reverts, F3/F4 ownership refusals, F9 `$1` residue | the 66-claim tally as a whole |

I found no agent claim false, but two needed narrowing. The hooks agent's "the approval number is
4× low" holds only for a directory argument; explicit file arguments are counted correctly. The
synth/verify agent's verify false positives did not occur in 7 real verify runs. **The most
consequential agent claim in this project's record was wrong:** the adversary's "certain"
corroboration of the `iterations` undercount (§2).

---

## 8. What I could not test, and why

- **Whether refused iterations were billed.** The transcripts do not record the category of the 79
  earlier iterations, and I have no billing data. §2 gives bounds instead.
- **A real reader apologising inside the template with a marker phrase.** I could not induce one;
  garbled input produced an honest note without one. So N2's new part rests on the fake, not a real
  model.
- **A real upgrade from a `5d94f97` GitHub install.** I simulated the prior state exactly (N3) rather
  than install an old commit from GitHub, which the marketplace cannot pin.
- **Plugin hooks in an interactive session over hours.** I ran them only through `--plugin-dir` in
  a headless session, plus the guard on 21 real transcripts.
- `lacuna split` (needs an attached tmux), the dev-cycle skill end to end, Windows/Linux, docling's
  OCR fallback (the previous judge ran it, and the parse-path change is the atomic write, which I saw
  hold for pymupdf4llm and marker), `split-book` on a real book, concurrent `lit read` processes on
  one PDF, and whether Claude Code's Write tool follows symlinks (N10b at `5d94f97`).

---

## 9. Spend, and what I touched

| Item | List price |
|---|---|
| This judging session (Opus 5.5), from `lacuna spend --session` on its own transcript | $13.74 at the time of writing (139 API calls), ≈ $14.50 by the end |
| Four agents (`total_cost_usd`) | $18.59 |
| Real pipeline: 17 calls in the Lacuna ledger | $1.30 |
| `/spend` through the plugin loader; smoke test | $0.006 |
| **Total** | **≈ $34.40** |

**Touched outside this directory:** nothing in `~/.lacuna`, `~/.claude/settings.json` or
`~/Desktop/Lacuna`. Every install was in isolated HOMEs under `judge/install/`. The model-calling
runs and the agents left Claude Code transcripts in `~/.claude/projects`, as any run does. I read
other transcripts there read-only, copying them first where a script needed files. Marker's model
cache was unchanged. I removed the `__pycache__` directories my suite runs created in `checkout/`,
and `git status --ignored` is now clean. No pushes. **Left as found:** a disabled local-scope install
of `lacuna@lacuna` belonging to the previous review (`projectPath …/scratchpad/judge`), whose cache
holds the `5d94f97` code. It is not mine and I did not remove it; `claude plugin uninstall
lacuna@lacuna --scope local`, run from that directory, would.

---

## 10. What moves this to READY

1. Make `lacuna_wrote()` record and check *which document* a note belongs to (N1, N6). Refuse a
   cross-document replacement, and name the note in the message.
2. Stop growing a phrase list. Have each agent report success explicitly, for example a required
   final line or a JSON field in its reply, and judge `synth` output by structure as notes already are
   (N2, V1.2, V1.5, V1.8).
3. Bump `version` on every release, and write the CHANGELOG entry (N3).
4. Re-anchor `tools/mutate.py`, add the revert of every fix as a mutant, and move
   `docs/reviews/adversary-tests.py` into `tests/` (N4).
5. Make the status line reuse `lacuna_config` for `DATA`, prices and the `<synthetic>` skip, the
   last by parsing `message.model` rather than the raw line (N7, F2, F3).
6. Measure cold rewrites against the session's warm prefix, not a fixed 40k, and show refused
   iterations on their own line (N5, §2).
7. One `TEXT_EXTS` shared by `read` and `estimate` (N8).
