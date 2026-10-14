# VERDICT3: Lacuna 0.3.0 at HEAD `59f9090`

## READY WITH CAVEATS

I would tell a colleague to install it. As a new user with real models, I installed it from GitHub and
followed the README end to end, including the first run, both papers, the cache, a plan against a repo
with a dead-ends file, a relative `verify`, and a digest. I also induced three real model failures and
ran `/lacuna:spend` through the plugin loader. Every stage did what the README says, at or below the
README's prices. Each of the six VERDICT2 caveats is fixed at the trigger it was reported with; I
reproduced each fix rather than taking it from the CHANGELOG. The three gates pass as claimed.

It is not READY, for three reasons. Each has a reproduction below.

1. **The content check regressed again for one real-model shape.** A plan titled
   `# Development plan (BLOCKED)`, `— BLOCKED` or `: BLOCKED` was refused at `783d6ef`. It is now filed
   with exit 0, and `after.synth` fires (N1). The only title form still caught is the exact one in the
   test, `(PARTIAL / BLOCKED)`. This is the second time that narrowing this check to stop false
   positives has let a refusal through; VERDICT2's V1.5 was the first.
2. **Two same-named documents read at the same time into one `--out` poison the note cache** (N2). This
   is the cross-process twin of the V1.3 fix. Both runs exit 0, one note silently replaces the other,
   and in some interleavings paper A's cache key holds paper B's note. That wrong note is then served
   as "cached (free)" into any directory.
3. **`/spend`'s "avoidable cold rewrites" is mostly false positives on real data** (N3). Of the 72
   counted on this machine in the last 7 days, 54 read the whole previous context: the cache was warm.
   This is pre-existing and was not part of caveat 3, but the line exists to tell users what was
   avoidable.

None of these has an ordinary trigger on the README path. Lacuna now tells its agents to reply
`INCOMPLETE:` rather than write a document. In every induced failure in this review where the model
received that instruction (a sonnet digest and an opus plan here, both over garbled notes), it obeyed,
wrote nothing, and the run exited 4. That is why these are caveats and not blockers.

One process claim fails again. The CHANGELOG says "every fix has a test in `tests/` and a mutant in
`tools/mutate.py` (its revert)". I reverted four 0.3.0 fixes, `_atomic_copy` among them, and the suite
stayed green each time; the gates agent counts about 23 such fixes (§4). The 186/186 is honest about
the 186 mutants on the list. It says nothing about the fixes left off it.

**Caveats for a colleague installing today**

1. **Do not run two `lit read`s into one `--out` at the same time** if any two of the documents
   share a file name. Also, a *different* paper saved later at the *same path* (a reused
   `~/Downloads/fulltext.pdf`) replaces the first paper's note without a word (N2, N4).
2. **Read anything before an `after.*` hook files it.** Exit 0 still means a file did not *open* with
   one of the recognised markers or phrases. The reader is never told to say `INCOMPLETE`, so a real
   haiku given unreadable text filed and cached a note saying "Not recoverable" (N5). Several shapes
   a synth model could write are filed too (N1, N6).
3. **Do not trust the cold-rewrite count in `/spend`** (N3). The total and the "lower bound" framing
   are sound.
4. **An `after.read` hook must be idempotent.** A hook that prepends frontmatter to every note in the
   directory, as the project's own test hook does, makes a later run refuse with exit 2 when papers
   are added over several days. Nothing is spent (N7).
5. **Two documentation claims about safety are false.** "If `synth --out` changes while the run is in
   progress, it is moved to `<out>.rejected`": only when the run fails. "Deleting `.lacuna.json` is
   safe: Lacuna then asks before replacing anything": not for `verify`'s report (N8).

Verified means I ran it, and the command and output are cited. All paths are under
`work/` in this directory. Agent claims I re-ran myself are marked *reproduced*; the others are
marked as the agent's.

---

## 1. The three claims

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | The six VERDICT2 caveats are closed | **HOLDS at the reported triggers; residuals in §2** | §2 |
| 2 | Gates: `check.sh` green on every Python; `mutate.py` 186/186; `mutation_battery.py` 18/18 | **HOLDS** as stated. The separate CHANGELOG claim that every fix's revert is a killed mutant **FAILS** (§4) | `check.sh`: `Python 3.9.6 OK (Ran 88 tests)`, `3.14.2 OK`, `3.12.9 OK`, `mutant anchors OK (all 186 mutant anchors match)`, exit 0 (`work/gates/check.log`). `mutate.py --jobs 6`: `186/186 mutants killed.`, exit 0 (`work/gates/mutate.log`). Battery on a separate copy: `caught 18/18 applied mutations`, `battery exit 0` (`work/gates/battery.log`) |
| 3 | Installs from GitHub and works for someone who is not the author, at the README's prices | **HOLDS** | §5. `git ls-remote` HEAD = `59f9090`; the installed copy is byte-identical to the checkout (`diff -rq` shows only Claude Code's `.in_use`). First run $0.298 against "≈$0.31"; reading $0.0124/doc against "~$0.015/doc" |

---

## 2. The six caveats

| # | VERDICT2 caveat | Status | Settled by |
|---|---|---|---|
| 1 | Same-named documents in separate runs: the second silently replaces the first's note (N1) | **CLOSED** at the reported trigger; two residuals | `work/lead` (fake): day 1 `dl/smith2023/main.md`, day 2 `dl/jones2024/main.md` → `day2 exit 2`, 0 model calls, "…is the note for …smith2023/main.md; …jones2024/main.md is a different file with the same name…", plus `--overwrite`, which works. Residuals: the same race between **concurrent** runs, which also poisons the cache (N2), and a different paper at the **same path** (N4). Both are PRE |
| 2 | Exit 0 does not mean the stage did its job; an honest partial is filed (N2) | **CLOSED for the reported case; STILL OPEN as a class, and one shape REGRESSED** | The reported real shape (`# PARTIAL: …` with `## Status of this digest`) now gives `exit=4 filed=False hook=False` (old `exit=0`, `work/lead/v/probes.log`). **Real models:** sonnet digest over garbled notes replied `INCOMPLETE: the notes a.md and b.md are unreadable…`, exit 4, nothing written, $0.033. opus-5 plan over the same notes: exit 4, nothing written, $0.095 (`work/runs/fail/`). Still open: the reader (N5), unlisted phrasings and callouts (N6). Regressed: BLOCKED in a title (N1) |
| 3 | `/spend` under-reports; treat it as a lower bound (N5, §2) | **CLOSED** | `lacuna spend --days 7`: "`$637.58 list price, a lower bound — see docs/LIMITS.md`". **Real** `/lacuna:spend` with sonnet and haiku: both reproduced that line verbatim and added "list prices, not your bill, and … a lower bound" (`work/runs/lspend-*.json`). The cost agent checked the `usage.iterations` range is priced per iteration model and never double-counted (329 real multi-iteration lines; top-level usage equals the last iteration in all of them), and that forks are deduplicated. New, separate problem: N3 |
| 4 | Editing a note, deleting the cache or a new machine refuses every later `read`, with a wrong message (N6) | **CLOSED** | `work/lead` (fake): after `rm -rf data/cache`, `read a b c` → exit 0; with a fresh `LACUNA_DATA` → exit 0; after appending to `vault/a.md` → exit 2: "`…/vault/a.md was written by Lacuna on 2026-10-14 14:19, and has been edited since.`", plus `--overwrite`. The message no longer says Lacuna did not write it. Residuals are loud and cost nothing: N7, N10 |
| 5 | Status line behind the CLI: `<synthetic>`, `~` in `LACUNA_DATA`, malformed `pricing.json` (N7) | **CLOSED** | Cost agent: `last $` equals ccost's last main-thread call on **348/348** real transcripts. It agrees with the CLI on every `pricing.json` shape and every `LACUNA_DATA` form tried, and its inline table equals `pricing.json` (7 models). Reproduced: a copy of `statusline.py` alone imports only `json, math, os, sys, time`, and exits 0 on empty, non-JSON, list, numeric and missing-path inputs (`work/lead/sl/`). One adversarial exception, N10h |
| 6 | `claude plugin update` does not deliver fixes; the version never changes (N3) | **CLOSED** | `work/upd`: isolated HOME, marketplace from a local clone at `c034766` (0.2.0), installed; clone moved to `59f9090`; `claude plugin marketplace update lacuna` then `claude plugin update lacuna@lacuna` → "`✔ Plugin "lacuna" updated from 0.2.0 to 0.3.0`". The installed copy has the new ownership code |

---

## 3. New defects, most harmful first

NEW means the behaviour differs from `783d6ef` (VERDICT2's HEAD) because of a change since. PRE means
it is the same there. Fake-model reproductions are under `work/lead/v/` (`probes*.py`, `race.sh`, logs
`*.log`), run against clean `git archive` copies of both commits (`work/lead/new`, `work/lead/old`).

**N1. NEW (regression caused by a fix), silent. BLOCKED in a plan's title is filed again.**
`declared_failure()` now judges only the opening line. A heading counts only through `_TITLE_MARKER`,
which needs a bracketed marker followed by a separator *and* a second closing bracket.
`work/lead/v/probes5.log`, the same plan body each time (the `after.synth` hook is a marker file):

```
'# Development plan (BLOCKED)'           [old] exit=4 filed=False hook=False   [new] exit=0 filed=True hook=True
'# Development plan — BLOCKED'           [old] exit=4 filed=False hook=False   [new] exit=0 filed=True hook=True
'# Development plan: BLOCKED'            [old] exit=4 filed=False hook=False   [new] exit=0 filed=True hook=True
'# Development plan (PARTIAL / BLOCKED)' [old] exit=4                          [new] exit=4
```

The one form caught is the one in `tests/test_unit.py:486`. VERDICT2 records a real planner writing
a file opening `BLOCKED`. The comment above `_TITLE_MARKER` cites a real planner title `(PARTIAL / BLOCKED)`. So BLOCKED in a
heading is a known real-model behaviour, and `(BLOCKED)` is its most natural form. Weighed: in 0.3.0
the agent is told to reply `INCOMPLETE:` and *not* write the file. Both real planners and digesters I
induced to fail did exactly that (§5.2), so this needs a model that ignores the instruction.
(Agent C2 found `— BLOCKED`; I found the other two. Reproduced.)

**N2. PRE, silent, poisons the cache. Two concurrent reads of same-named documents into one `--out`.**
The collision guard and the ownership check both run before any lock, and each process caches
whatever is at the note path when its own agent returns. In `work/lead/v/race.sh <reader seconds>
<stagger>`, `a/main.md` ("Title A") and `b/main.md` ("Title B") are read by two processes
(`race.log`):

```
3 s, B 1 s later: A exit=0 B exit=0 | notes/main.md is: Title B | cache main_86686862d9f0.md: Title A (correct)
0 s, together:    A exit=0 B exit=0 | notes/main.md is: Title B | cache main_86686862d9f0.md (A's key): Title B
                  A read again into a fresh --out: exit=0 note is Title B; calls now 2   ← served free, wrong
3 s, together:    A exit=0 B exit=0 | notes/main.md is: Title A | cache main_170ec5490bcd.md (B's key): Title A
```

In every interleaving one note silently replaces the other. The window is the whole reader call
(0.3–7.8 min a paper by the calibration). Cache poisoning needs the replies to land close together.
This is the V1.3 defect ("the parallel readers then cached one paper's note under the other's key,
permanently", `lit_pipeline.py:255`) between processes rather than threads. Distinct names are safe:
the ownership agent ran 20 iterations of 2–4 concurrent reads × 8 documents (488 calls) and lost no
record entry, while a copy without the `flock` lost entries in 20 of 20. (Ownership agent O1;
reproduced.) Trigger: two sessions reading into one vault at once. Uncommon.

**N3. PRE, silent, cost tool. Most "avoidable cold rewrites" are warm caches.**
`ccost.is_cold()` returns True for `write > 40k and read < 40k` before it looks at the gap or the
previous call. The 0.3.0 idle-gap and same-model rules apply only to the new branch for reads of 40k
and over. So a small session that adds a >40k tool result (any reader agent opening a long paper) is
an "avoidable cold rewrite… with handoff + /clear before breaks". On this machine's real transcripts
(`work/lead/v/colds.py`, read-only):

```
7d:  counted 72 ($32.96); read >=90% of previous context (warm): 54 ($7.59); <300 s after previous call: 62
     e.g. session dbc645ff call #2 read 11115 of previous 11117, wrote 43903, 45s after
```

The count is about 4× too high and the dollars about 30% too high. The session report labels the
same calls "cold rewrite" instead of "+Nk new context". (Cost agent C1; reproduced.)

**N4. PRE, silent. A different paper at the same path replaces the first's note.** `not_ours()`
treats a source at the recorded path as the same document even when its bytes differ, by design: "a
document is the same one when it is at the same path (and may have changed)". In
`work/lead/v/probes4.log`, day 1 reads `Downloads/fulltext.md` (paper A), which the user then files
away; day 2 reads a new download at the same name (paper B):
`[new] day1=0 day2=0 calls=2 notes/=['fulltext.md'] fulltext.md is B`. The record already holds
`source_sha256`, so the case is detectable. This is caveat 1's harm by another route. (Ownership agent
O11; reproduced.)

**N5. PRE, silent (real model). The reader files and caches an honest "nothing".** The 0.3.0
`INCOMPLETE` rule went to `synth` and `verify` only. The reader's reply is never read, and its note
is judged by `## Claim` plus the opening-phrase list. **Real**: `lit read garbled.txt` with haiku →
`1/1 notes written, $0.002`, exit 0, cached as `garbled_82b9ad1d9ec3.md`. The note's Claim reads "Not
recoverable. The supplied text file … is a single line of symbol noise" (`work/runs/fail/rnotes/`).
With the fake, a valid note plus the reply `INCOMPLETE: pages 3-12 were garbled` is cached too, at
both commits. Defensible when the input is the problem (a new parse has a new key), but it is what
caveat 2 says. Twin: an empty `.md`, whitespace `.txt` or binary `.txt` is sent to a reader and paid
for (`probes2.log` C7: `empty.md read exit=0 calls=1`).

**N6. PRE, silent. Refusal shapes the check still misses**, each filed with exit 0 and the hook at
both commits (`probes.log`):
- a two-line Obsidian callout `> [!WARNING]` / `> PARTIAL: …`. The CHANGELOG says callouts are
  handled, but only the one-line form is.
- YAML frontmatter before `PARTIAL:`
- "I couldn't open any of the literature notes…" and "I was unable to access the notes…", which are
  not in the phrase list
- a synth *reply* that says it failed in prose. The reply check reads only its first line. The
  **real** opus-5 reply in §5.2 began "Both literature notes are binary garbage — …" and put
  `INCOMPLETE:` on line 3, and `declared_failure()` on that reply returns `None`. It was caught only
  because the model also obeyed "do not write". A synth-written note "I'm sorry, but I can't help with
  summarising this paper" is cached too: `_LEAD` does not strip "I'm sorry, but".
(Content agent C1/C3, docs agent F5; reproduced.)

**N7. NEW (partial fix), loud, nothing spent. Non-idempotent `after.read` hooks.** `rerecord()`
re-records only the notes written in the current run, but `after.read` is given the whole directory.
`probes2.log` F3 uses a hook that prepends frontmatter to every note, as the suite's own hook does
(`tests/test_pipeline.py:268`). Day 1 reads zeta; day 2 reads alpha, and the hook edits zeta again.
Day 3, re-reading zeta → `exit=2 … zeta.md was written by Lacuna …, and has been edited since.` An
idempotent hook works (`day3=0`). The CHANGELOG says this case is fixed.

**N8. NEW claims, false. Two safety statements in `docs/extending.md:170-174`.**
- "If `synth --out` changes while the run is in progress, it is moved to `<out>.rejected`." On a
  successful run the edit is overwritten: `F1_edit_during_success [new] second=0 edit survives=False
  rejected=False` (`probes2.log`). The code compares `before` only on the failure path.
- "Deleting `.lacuna.json` is safe: Lacuna then asks before replacing anything." `verify`'s report is
  checked with `namespaced=True`, so an annotated report is replaced silently once the record is gone:
  `F2 [new] second=0 annotation survives=False`. With the record present it is protected:
  `F2b [new] second=2 annotation survives=True`. The record's own `about` text makes the same claim.
(Docs agent F1/F2, ownership agent O3/O9; reproduced.)

**N9. NEW (regressions caused by fixes), rare triggers.**
- **`_atomic_copy`'s temp name is about 11 bytes longer than the cache name**, so document stems of
  230–239 bytes now fail with exit 1 *after* the paid call. The next run exits 2 and asks for
  `--overwrite`, which pays again. In `probes2.log` C6: `[old] 230:exit=0 … 239:exit=0`,
  `[new] 230:exit=1,rerun=2 … 239:exit=1,rerun=2`; 244 fails on both (VERDICT2 N10e, still open).
- **A paper named like a plan replaces the plan.** `synth` records its output with `source: null`, and
  `not_ours()` skips the different-document test when no source is recorded. In `probes4.log`:
  `[old] read=2 lit/plan.md now: # Development plan`, `[new] read=0 lit/plan.md now: # A note (plan.md)`.
- **`lit estimate DIR` now prices an uppercase `.PDF` as raw text.** The extension fix lowercased the
  directory scan but not `tokens()`. In `work/lead/v/runs/pdfcase`, `Zhao2025.PDF` (the real paper,
  ~19k tokens) gives `[old] ERROR: no .md, .txt or .pdf documents found` and
  `[new] 1 documents, ~380,186 tokens … haiku medium $1.03`, plus a false long-context warning.
  `read` parses the same file correctly. (Docs F9, cost C5; reproduced.)

**N10. Smaller, each reproduced unless marked.**
- a. NEW, false positives that discard a paid result: a verify report whose first finding *starts*
  with "No files were found…" → exit 4, "NOT been fact-checked" (old exit 0). A plan whose opening line
  is `Blocked: none. All dependencies are in the tree.` → exit 4. A note whose Claim opens "Not completed
  follow-up in 37 of 412…" → exit 4, and the re-run pays again. The usual `claim -> finding` order is
  unaffected (`C4_verify_claim_first [new] exit=0`). In VERDICT2's 7 real verifies none opened this way.
- b. NEW. A failed note cached by ≤0.2.0 is served free forever. A cache hit is not re-checked, and
  `PROMPT_VERSION` was not bumped when the check tightened. `synth` then refuses it with advice ("run
  the stage that wrote them again") that loops (`probes3.log`). This needs an upgraded cache holding
  such a note.
- c. PRE, money. `synth --status` that is unreadable is passed to the paid planner (`exit=0 calls=1`),
  so a dead-ends file can be silently ignored. This is the twin of the new notes pre-flight. An empty
  `--repo`, a tiny or binary `--digest`, and `verify --target` that is empty are also paid for (content
  agent C7).
- d. PRE. `verify` with an unreadable `--target` → `PermissionError` traceback, exit 1, before any
  call (VERDICT2 N10c, still open). A dangling `.md` symlink in `--notes` → traceback (agent).
- e. NEW, loud. A damaged `.lacuna.json` is silently rewritten with only this run's entries. The next
  run then refuses the others as unrecorded, with no mention of the damage (ownership O6, the agent's).
  A note restored from backup is refused as "edited since" even when it equals the cached note (O10,
  the agent's).
- f. PRE. The verify prompt still tells the model to grep `research/STATUS.md`, relative to the
  caller's cwd (`lit_pipeline.py:882`; VERDICT2 V1.6 residual).
- g. NEW, cost tool, no real occurrence yet. The session report's subagent line ignores
  `usage.iterations` (cost C4, the agent's). The CHANGELOG's "84 → 92, $47.23 → $87.67" reproduces only
  at `--days` ≥ 45, and LIMITS' `$62.65–$285.28` is `$62.65–$298.89` from the code (cost C2/C3, the
  agent's).
- h. Adversarial only. `"transcript_path": true` makes the status line `open(1)` and exit 120 (`rc=120
  … OSError`, reproduced; Claude Code never sends a boolean). `LACUNA_MIN_CTX=inf` raises in
  `cache_guard` at import, and a deeply nested `config.json` raises `RecursionError` in every CLI
  command (cost C8, the agent's).

---

## 4. Gates

| Gate | Result |
|---|---|
| `tools/check.sh` | 3.9.6, 3.14.2 and 3.12.9 OK (88 tests each); anchors OK (186); exit 0 |
| `python3 tools/mutate.py` | `186/186 mutants killed.`, exit 0 |
| `tools/mutation_battery.py` | `caught 18/18 applied mutations`, exit 0; no skips; the target copy's `git status` is clean afterwards |
| Stale-anchor gate | **HOLDS** (gates agent; its logs `work/agents/gates/logs/stale-*.log`). An anchor made to occur 0 or 2 times → `--check-anchors` exit 2, the full run exit 2 before applying anything, and `check.sh` exit 1. CI runs `--check-anchors` on all six legs. None of the 186 mutants is a no-op or comment-only. The battery exits 2 on `783d6ef` (one mutation cannot apply) and 0 with `--allow-skips`; a hash of every file shows it no longer edits its target |
| Is every 0.3.0 fix guarded (its revert killed)? | **No. The CHANGELOG's "every fix has a test in `tests/` and a mutant in `tools/mutate.py` (its revert)" FAILS.** The gates agent reverted 74 changes in `783d6ef..59f9090` one at a time: 36 killed, 38 survived, of which it judged about 23 real fixes and about 12 redundant code. I reproduced four survivors (`work/lead/v/revert.log`, suite run as `check.sh` does): `_atomic_copy → shutil.copy` (the fix the last commit is named for), synth's `INCOMPLETE_RULE` removed from the prompt, synth's `need_writable` removed, and the curly-apostrophe normalisation in `_norm` removed. Each gives `SURVIVES OK`, all 88 tests green. On the last, HEAD's `refusal('…I can’t help…')` returns `"i can't help"`; reverted, the apology passes. The agent's other survivors include `rerecord` after `after.synth`/`after.verify` (demo: the next run exits 2 with a frontmatter hook), `namespaced=True`, `_owners`' per-entry filter (demo: a string entry → `TypeError`, exit 1), the estimate `.lower()`, and the lead-in-sentence branch of `refusal` |

What the gates cannot see: N1 and N9 are regressions *inside* fixes that each have a killing mutant.
The tests encode the reported instance (`(PARTIAL / BLOCKED)`, a 0–229-byte name, lowercase `.pdf`),
and a sibling shape breaks without any test noticing. 186/186 says the suite would catch each fix
being reverted. It does not say the fix covers its class.

---

## 5. Real pipeline as a new user

Install: `claude plugin marketplace add https://github.com/aakankschit/Lacuna.git` and `claude plugin
install lacuna@lacuna --scope user` into an isolated HOME (`work/install/home`), so
`~/.claude/settings.json` was untouched. Version 0.3.0. Model calls ran as the real user against the
installed copy with `LACUNA_DATA=work/runs/data-user`. Ledger: 10 calls, **$0.911**.

### 5.1 The README, top to bottom

| Step | Cost | Wall | Result |
|---|---|---|---|
| `--version`, `doctor`, `config`, `spend --days 7` | free | <3 s | 0.3.0; doctor exit 0 ("Nothing is blocking", parsers optional with fix commands); spend prints "a lower bound — see docs/LIMITS.md" |
| First run: `synth` on `examples/` → `verify`, run from the install path | $0.291 + $0.0071 | 88 s + 41 s | exit 0 both. **$0.298 against "about $0.31": holds.** Verify found two real errors (an `explore=1.0` claim, and a single-objective attribution to two notes when only one says it) |
| `doctor --install-pdf` | — | 11 s | ok |
| `lit estimate papers/*.pdf --per-agent 1` | free | — | 2 docs, ~38.7k tokens, haiku/medium $0.02, budget warning names sonnet/max |
| `lit read papers/*.pdf --out notes/ --brief …` | $0.025 | 83 s | 2/2 notes (1,130 and 1,121 words). **$0.0124/doc against "~$0.015/doc": holds.** 15 of 15 numbers spot-checked in the Zhao note are in the parse (`83.1`, `85.4`, `0.030`, `72.4`, `81.5`, `63.4`, `84.4`, `0.61`, `0.067`, `22,000`, `2.18 billion`, `30,000`, `0.004`, `358`, `2cmp_10`) |
| same command again | $0 | 0.08 s | `cached (free)` ×2 |
| plan with relative `--status research/STATUS.md` naming roulette-wheel selection a dead end | $0.345 | 107 s | exit 0. The plan lists it under rejected, citing STATUS.md (`work/runs/proj/repo/lit/plan.md:12,174`) |
| `verify --target lit/plan.md --repo .` (relative) | $0.0093 | 53 s | three real findings, including a mis-attributed OP#7 |
| `synth --kind digest` | $0.105 | 40 s | ok; honours the dead end |
| hand-edit `lit/plan.md`, re-run synth | $0 | — | exit 2, "has been edited since… Nothing was sent to a model… --overwrite" |
| `/lacuna:spend` through `--plugin-dir`, sonnet and haiku | $0.085 + $0.005 | — | report verbatim with "a lower bound", range guidance followed. Debug log: the plugin's `cache_guard.py` and `results_hook.py` ran on `SessionStart` and `UserPromptSubmit`, status 0 |

The README's verify range "$0.02–$0.09" and synth range "$0.53–$1.44" are still above what I paid:
$0.007–$0.009 and $0.29–$0.35. They over-state, which is the safe direction.

A note on this machine: the author has a personal `~/.claude/commands/spend.md` that shadows the
plugin's `/spend`. It runs an older `spend.py` without the lower-bound label. My first four `/spend`
runs hit that, not the plugin, so I discarded them and used `/lacuna:spend`. A new user would not have
the shadow. The README's "`/lacuna:spend` disambiguates" covers it.

### 5.2 Real model failures, induced (`work/runs/fail/`)

| Induced failure | Model | What it did | Lacuna | Right? |
|---|---|---|---|---|
| digest over two notes of mojibake (>400 bytes, so they pass the pre-flight) | sonnet | replied `INCOMPLETE: the notes a.md and b.md are unreadable…`, wrote nothing | exit 4, "Nothing was written", $0.033 | yes |
| plan over the same notes | claude-opus-5 | replied with a prose line, then `INCOMPLETE:` on line 3; wrote nothing | exit 4, nothing written, $0.095 | yes. Caught by the missing file, not by the reply check (N6) |
| read a `.txt` of mojibake | haiku | template-shaped "Not recoverable" note | exit 0, cached, $0.002 | defensible, but an honest "nothing" filed as output (N5) |

---

## 6. Documentation against code

The docs agent executed 33 claims against the fake model: 19 HOLD, 7 FAIL, 6 MISLEADING, plus
untestables. I reproduced the FAILs that matter (N7, N8, N9 estimate). Of VERDICT2 §6:
- **Fixed:** `extending.md` "deleting [the cache] costs only the money to re-read". It is true now,
  checked above.
- **Still wrong:**
  - the README equations recipe still needs `--install-pdf` too, because `need_venv()` runs first
  - ARCHITECTURE's "A refusal must never become a note" (N5, N6)
  - the `$1` table header and both `$1` comments (cosmetic)
  - the relative `research/STATUS.md` in the verify prompt (N10f)
- **New and cosmetic:** `lacuna --help` does not list `--overwrite`, the flag every 0.3.0 refusal tells
  you to use, and the `estimate` help still says ".md/.txt/.pdf".

---

## 7. Agents, and how far to trust them

Five Opus 5.5 subagents at high effort, one per code region, each with its own copy of HEAD and of
`783d6ef`, its own `LACUNA_DATA` and the fake `claude` only (`work/agents/<name>/`, shared rules in
`work/agents/PREAMBLE.md`). None could write its `REPORT.md` (the harness refused). Their reports are
quoted from their final messages, and their scripts and logs are in place.

| Agent | Region | Claims I reproduced | Not reproduced by me (marked as theirs) |
|---|---|---|---|
| ownership | `.lacuna.json`, locking, `_atomic_copy`, read/synth/verify outputs | O1 concurrent race, O2 hooks, O3 verify after record loss, O4 read over plan, O9 edit during run, O11 same path | O5 `.rejected` overwritten, O6 damaged record, O7/O8 read-only parent and target, O10 backup restore |
| content | content checks, money guarantees, caches | C1, C2 (and found two more title shapes), C3, C4, C5, C6, C7 | C8 dangling symlink, C9 SIGTERM ledger, C10 alias key |
| cost | ccost, spend, status line, cache_guard, config | C1 cold count (on this machine's data, my own script), C5 `.PDF`, C8 boolean path | C2/C3 figure mismatches, C4 subagent iterations, C6/C7 fuzz, the 348/348 status-line agreement |
| docs | every doc claim, executed | F1, F2, F3, F5, F9 | the full 33-claim tally |
| gates | mutate, battery, check.sh, fix reverts | 4 of its 23 surviving reverts (§4), battery exit codes | the stale-anchor experiments, 70 of the 74 reverts, 3.9 syntax sweep, flakiness (3 runs) |

Judged:
- I found no agent claim false, but several needed narrowing. The ownership agent's "cache poisoned"
  in O1 is true only for some interleavings: in one of my three timings A's key held the right note.
  The content agent's C4 verify false positive needs a finding that *starts* with the phrase; the
  `claim -> finding` order the prompt asks for is unaffected.
- The cost agent's C1 is the most consequential agent claim this round, so I measured it with my own
  script. It holds: 54 of 72.
- I discarded my own first `/spend` test (§5.1), which would have produced a false "the command drops
  the lower bound" finding.

---

## 8. What I could not test, and why

- **A real model writing a BLOCKED-titled plan under 0.3.0's instructions.** Both real failures I
  induced obeyed `INCOMPLETE_RULE`. So N1 rests on the fake plus the real titles in the record.
- **Concurrent reads with real models.** N2 is shown with a fake that sleeps; I did not pay for two
  parallel real readers.
- **An upgrade from GitHub at 0.2.0.** The marketplace cannot pin a commit, so I used a local-clone
  marketplace moved from `c034766` to `59f9090`. The update mechanism is the same.
- **`--equations` / marker.** No 0.3.0 change touches the parse path (`git diff 783d6ef HEAD --
  scripts/lit/prep_md.py` is empty), and marker is a 3 GB install with a one-process rule. I did not
  run it. Nor did I run docling, `split-book` on a real book, `lacuna split` (needs tmux attached), the
  dev-cycle skill, or Linux/Windows.
- **Whether refused `usage.iterations` attempts were billed.** That is not recorded, as VERDICT2 said.
  The tool now shows a range, which is the honest answer.
- **Interactive sessions over hours** for the hooks. I ran them headless through `--plugin-dir` only.

---

## 9. Spend, and what I touched

| Item | List price |
|---|---|
| This judging session (Opus 5.5), `lacuna spend --session` on its own transcript | $7.60 at the time of writing (96 calls), about $8.50 by the end |
| Five subagents (Opus 5.5, high effort), the same report's subagent line | $18.55 |
| Real pipeline: 10 calls in the Lacuna ledger (`work/runs/data-user/cost-ledger.jsonl`) | $0.91 |
| `/spend` and `/lacuna:spend` through the plugin loader (6 runs, `total_cost_usd`) | $0.16 |
| **Total** | **about $28.10** |

These are list prices from the tool under review, so by its own account they are a lower bound.

**Touched outside this directory:** nothing in `~/.lacuna`, `~/.claude/settings.json` or
`~/Desktop/Lacuna`. Installs were in isolated HOMEs under `work/install` and `work/upd`. They are
cleaned up below. Real model runs and the subagents left Claude Code transcripts in
`~/.claude/projects`, as any run does. I read other transcripts there read-only; the cost agent
copied a sample into its own workspace first. No marker process ran. No pushes. `checkout/` is clean
(`git status --ignored` empty); I removed one `__pycache__` I created in `checkout/tests`.

**One thing I disturbed, and you should know about it.** At the end I removed the `lacuna-mut-*`
scratch directories that `tools/mutate.py` leaves in `$TMPDIR` when a suite run outlives its
`rmtree`. I selected them by modification time, at or after this review began (14:06). At that moment
another Claude session, the author's, was running `python3 tools/mutate.py` in `~/Desktop/Lacuna`
(started 14:48, PID 36398). About eight of the directories I removed were that run's **live** scratch
copies. `mutate.py` reports a mutant whose run errors as "NOT caught", so that run most likely ends
red rather than falsely green. Even so, **its result should be discarded and the run repeated.** I did
not touch `~/Desktop/Lacuna` itself or anything else of that session's. Directories from before
14:06 (an earlier review's) are left as they were.

Also left as found: the local-scope `lacuna@lacuna` install that VERDICT2 attributes to an earlier
review. Both of my isolated installs (`work/install`, `work/upd`) are uninstalled, and their
marketplaces removed.

---

## 10. What moves this to READY

1. Judge a heading by the same marker rule as a line, `(BLOCKED)`, `— BLOCKED` and `: BLOCKED`
   included. Add each as a test, then check the false-positive titles still pass (N1).
2. Hold the note path from the pre-flight to the cache write. Either lock `--out` for the whole
   `read`, or have each process write its note to a private temp name and cache *that*, not whatever
   is at the shared path when it returns (N2).
3. Make `is_cold` require the idle gap on both branches, or judge every cold write against the
   previous call (N3).
4. Treat a recorded source whose bytes changed as another document, and ask (N4).
5. Give the reader the `INCOMPLETE` rule and read its reply. Search the whole synth reply for a line
   that opens with the marker, not only its first line (N5, N6).
6. Re-record every file in `--out` an `after.read` hook changed, or say in extending.md that hooks
   must be idempotent. Fix or remove the two safety sentences (N7, N8).
7. For each 0.3.0 fix, add the sibling that broke as a mutant: the `(BLOCKED)` title, a 235-byte stem,
   an uppercase `.PDF` in a directory, `read` onto a `source: null` file (N9).
8. Add the revert of each unguarded fix in §4 to `MUTANTS` and a test that kills it. Start with
   `_atomic_copy`, the `INCOMPLETE_RULE` in both prompts, `rerecord` after `after.synth`/`after.verify`,
   and `_norm`'s apostrophe. Then retire the CHANGELOG sentence or make it true.
