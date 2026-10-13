# Study 1 — model tier × reasoning effort on reading documents

*October 2026 · 3 tiers × 5 effort levels × 2 repeats = 30 runs, blind-judged · $55.92*

## Question

Does it matter which model reads a research document, and does reasoning effort buy anything? The
task was the hard one, not summarisation: extract the **open problems** a document leaves —
including those the authors do not state — in a form specific enough to become a work item.

## Setup

Each run: one agent, one tier, one effort level, reading both parsed documents and writing one
structured note per document. Each of the 15 tier × effort cells was run **twice**, giving 30 runs
and 60 notes; every figure below is the mean of 2 repeats × 2 judges. Notes were anonymised and judged by two models from different tiers against a rubric
scoring recall of open problems (against a hand-built reference list of **15** gaps),
proposal viability, and whether a proposal reached mechanism level — naming *how* a thing would
work, not just that it should be tried.

## Results

| | cheap | mid | strong |
|---|---|---|---|
| Open-problem recall (fraction of the reference list) | 0.48 | 0.54 | 0.53 |
| Viable proposals | 8 | 10 | 14 |
| Mechanism-level proposals | 1 | 1 | 4.3 |

**Recall does not depend on the tier.** All three land near half the reference list, and the cheap
tier is within 0.06 of the strong tier. Spotting that a document leaves a question open is not where
model capability shows up.

**Proposal quality does.** The strong tier produced 4.3 mechanism-level proposals against 1 for both
others — the one place in this study where paying more changed the kind of output, not just its
polish.

### Effort buys almost nothing

| tier | cost, low → max | shape |
|---|---|---|
| cheap | **47×** | rises steeply and monotonically |
| mid | **13×** | flat to high, then jumps at xhigh/max |
| strong | **1.3×** | nearly flat overall, and not monotonic: high and xhigh cost ~1.7× medium while max costs *less* than high |

(Multipliers computed from the calibration this plugin ships, so `lacuna lit estimate` reproduces
them. The strong tier declined to spend thinking tokens on this task at any level, never exceeding
about 5k even at max.)

The cheap tier will happily burn 47× the money for no measured gain in recall or viability, and the
mid tier 13×. No tier bought measurably better notes at higher effort.

**This is why the shipped pipeline fixes effort at medium everywhere except the verifier.**

## The finding that should temper the whole study

The open problems that later became real features in the project under test were found in **0–12% of
notes**. Across 30 runs and two documents, the specific gaps a domain expert had already identified
as worth building were almost never the ones the models surfaced.

What the pipeline is good for is coverage and recall at low cost — reading more than you otherwise
would, and not missing the obvious. It is not a substitute for the expert judgement that decides
which gap is worth a month of work.

## Secondary result: cheap notes plus a strong synthesiser

A follow-up compared two ways to get a development plan out of three reference textbooks:

| route | cost |
|---|---|
| strong tier reads everything itself | $5.03 – $7.71 |
| cheap tier writes notes, strong tier plans from the notes | **$0.79 – $0.86** |

Judged output quality was comparable. One sixth of the cost for the same result is the economic
basis of the whole read-then-synthesise split, and [study 2](lit-synthesis.md) tests it properly.

## What this does not show

- Two documents. Recall is measured against a hand-built reference list for those two, by one person.
- "Viable" and "mechanism-level" are rubric judgements by model judges, averaged over two.
- Effort behaviour is a property of the models as they were in October 2026 and may change.
- Nothing here is about writing code. That is [study 3](code-writing.md).
