# Study 2 — which reader, which synthesiser

*October 2026 · 6 documents, 3 readers × 3 synthesisers × 2 context conditions = 18 plans, blind-judged · $39.56*

## Question

Study 1 showed that cheap notes plus a strong synthesiser is six times cheaper than having the
strong model read everything. This study asks where in that chain quality actually comes from, and
what each combination costs.

## Setup

Six research documents, parsed locally. For each cell: a reader tier wrote one note per document,
then a synthesiser tier read the notes plus the codebase (with an authoritative `git ls-files`
listing) and wrote a ranked development plan. Plans were anonymised and judged by two models against
a rubric; every claim in every plan was then re-checked with `grep` against the code and the notes.

Two sets of 9 cells, as a control on where the project context enters: one where the reader got
a project brief, one where only the synthesiser saw the project. One run per cell, no repeats.

## Result 1 — the synthesiser is the lever

| | cheap synthesiser | strong synthesiser |
|---|---|---|
| Verified viable proposals | 3.6 | **5.4** |
| Mechanism-level proposals | 0.4 | **1.3** |

Reader tier barely moved either number. **If you have one place to spend money in this pipeline, it
is synthesis.**

## Result 2 — a stronger reader is *worse*

| reader tier | factual errors per plan | mis-attributions |
|---|---|---|
| cheap | **1.7** | baseline |
| strong | 2.8 | **4×** baseline |

This was the study's surprise. Richer notes are not better notes. The stronger reader's notes assert
more, hedge less, and blur which document said what — and the synthesiser downstream inherits all of
it. The cheap reader writes flatter notes that stay closer to the source.

## Result 3 — what the errors actually are

**75 raw findings** from two judges across the 18 plans. Averaging the two judges gives 37.5
findings, or **2.1 per plan**:

| error | count |
|---|---|
| Claim attributed to the wrong document | 27 |
| A number from the project's status file misquoted | 18 |
| Data invented outright (a column that exists in no file) | 5 |
| **A named file, class or function that does not exist** | **0** |
| **Re-proposing something the project recorded as a dead end** | **0** |

The two failure modes everyone fears — hallucinated code references and re-proposing dead ends —
did not occur once, because the synthesiser is handed an authoritative file list and the project's
own record of what has been ruled out, and is told to grep before naming anything.

What does happen is misattribution: the model knows the fact and gets the source wrong. That is
exactly what a cheap grep-based verification pass catches, which is why `lacuna lit verify` is not
optional. In one case it reproduced both blind judges' findings independently for $0.085.

## Result 4 — the cost of each route

| route | cost | viable | mechanism-level | code refs verified |
|---|---|---|---|---|
| cheap read → **mid** plan | **$0.53** | 8.0 | 1.0 | **100%** |
| cheap read → **strong** plan | $1.44 | 8.0 | **2.0** | 75% |
| cheap read → strong plan, briefed reader | $1.54 | — best overall — | | |
| cheap read → mid digest → strong plan | more | **5.5–6.0** | halved | higher |

**The mid tier is the value planner.** One extra mechanism-level proposal for 2.7× the money is
within single-run noise, and the strong tier's accuracy is *worse* — a gap that verification closes
for under ten cents.

**Do not chain digest → plan.** It cuts viable proposals from 8.0 to 5.5–6.0 and halves
mechanism-level ones, because the digest compresses away the raw material the planner generates
from. It does improve accuracy, but verification does that far more cheaply. Chain only when the
digest itself is the thing you want planned against.

## Result 5 — one document per agent, not batched

| | per-document | 2 per call |
|---|---|---|
| cost | **$0.091** | $0.132 |
| wall time | **102 s** | ~200 s |

31% cheaper and twice as fast. The mechanism is cache arithmetic, not model behaviour: a batched
call re-reads every earlier document on every later turn. Per-document pays 8% more startup and
**42% less re-reading**. This only holds with a slim agent (Read/Grep/Write, no project
instructions, ~4k-token start); a general-purpose agent's ~34k start swamps the saving.

## Result 6 — reference books prevent mistakes, they do not generate ideas

Ablation with and without three reference textbooks available for readers to grep:

| | without books | with books |
|---|---|---|
| Viable proposals | identical | identical |
| Factual errors per plan (cheap reader) | 4.0 | **1.5** |

Books roughly halve the error rate and cut fabricated attributions, at roughly 4× the reading cost
(for the cheap tier, +$0.19 for six documents — still negligible). Keep them for unfamiliar
mathematics and for the cheapest reader, which looks things up most.

**Two cells, one run each. Suggestive, not established.**

## A validation worth more than any rubric score

All 18 plans independently converged on the same architectural change as the first thing to build —
and that change is what the project's own unpublished design document, written before the benchmark
and never shown to any agent, specifies in its first section. The pipeline reproduced a design
decision an expert had already made, from the literature alone.

That is one instance, and it says nothing about novelty: reproducing a decision an expert already
reached is evidence of soundness, not of insight.

## What this does not show

- One codebase, one document set, one project. Six documents is not a literature.
- Single runs per cell on the quality axes; the cost and timing numbers are the robust part.
- "Viable" is a rubric judgement, and viability is project-specific by definition.
- The book ablation is two cells.
