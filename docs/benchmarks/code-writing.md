# Study 3 — three tiers implementing one specification

*October 2026 · 1 task, 1 run per tier, hidden conformance test + 2 blind reviewers · $2.33*

**This is the study behind the `dev-cycle` skill, and it is the one with the smallest sample and the
most important finding.** One task, one run per model. Read it as a warning about test design, not
as a ranking.

## Setup

One real task from a live project: implement a specification that extends an existing class — K
independent posterior states per object, mirroring an existing single-objective class exactly. The
kind of change where being quietly wrong is expensive and nothing visibly breaks.

Each tier worked in its own `git worktree` on its own branch, from the same specification, with the
reference class available and no sight of the others' work. Baseline before any change: **326
passed, 5 skipped**.

Then three independent checks:
1. a **hidden conformance test** written before any model ran, which no model could see;
2. the project's full existing suite;
3. **blind review** by two models from different tiers, given the three implementations as A/B/C,
   the reference class and the specification.

## Objective results — no discrimination whatsoever

| tier | hidden test | own tests | regression | scope violations | impl lines | test lines | cost | wall |
|---|---|---|---|---|---|---|---|---|
| cheap | **7/7** | 39 pass | 326 pass | 0 | 283 | 424 | **$0.025** | 116 s |
| mid | **7/7** | 13 pass | 326 pass | 0 | **212** | 147 | $0.235 | **79 s** |
| strong | **7/7** | 42 pass | 326 pass | 0 | 431 | 485 | $1.142 | 231 s |

Every tier: full marks on the hidden test, zero regressions, nothing touched outside scope, all of
its own tests green.

**On this evidence you would pick the cheap tier and save 98% of the cost. That would be a mistake.**

## Blind review — where the differences actually were

Scores are the mean of two judges, 1–3.

| tier | math fidelity | spec conformance | test quality | code quality | verdict (judge 1 / judge 2) |
|---|---|---|---|---|---|
| cheap | **2.0** | 2.0 | 2.5 | 2.0 | **needs work / needs work** |
| mid | **3.0** | **3.0** | 2.5 | **3.0** | minor changes / minor changes |
| strong | **3.0** | **3.0** | **3.0** | **3.0** | **as is** / minor changes |

### The defect that passed every test

The cheap tier's implementation **had no alternative update path at all** — zero references to the
two state variables that path requires, against 7 and 6 for the mid tier and 8 and 7 for the strong
tier. Verified mechanically, not taken from the judges:

- Both of the project's recommended presets enable that path. Any object built on this class would
  have silently fallen back to the default update rule.
- In the reference class, the `mode` parameter is read **only** inside that path. With the path
  missing, configuring `mode` became dead state — a documented parameter that does nothing.
- The implementation **shipped a test asserting that `mode` has no effect**, locking the wrong
  behaviour in as if it were intended.

As one judge put it: *"the parameter is validated, stored and tested but never read by any update
rule, so configuring it is a silent no-op."*

**The hidden conformance test missed this**, because every check in it ran on the default path —
where the parameter legitimately has no effect in the reference class either. The risk lived in the
non-default path that production actually uses.

That is a lesson about test design before it is a lesson about model tiers, and it is why
`test-author` is told to go after non-default paths and to prove that every accepted parameter
changes an observable result.

### Divergences in the stronger implementations

Neither was merge-ready either, but both were honest mistakes rather than missing machinery:

- **mid tier**: stored a caller's dict by reference and left it partially filled, so later mutation
  by the caller changed behaviour; an all-or-nothing initialiser that one missing input could block
  entirely; one global flag where the specification wanted per-key flags.
- **strong tier**: a non-atomic multi-step initialiser — a late validation failure left earlier keys
  already mutated; keys omitted from the input silently stayed in their initial state; an aggregate
  property reporting a state that contradicted its parts.

**All three** missed the same two features of the reference class, both of which the specification
had covered only by the phrase "mirror the existing class". A general instruction does not make a
model enumerate what it generalises over.

## Follow-up: did the design survive contact?

The same task was then built properly with the `dev-cycle` workflow — a pinned contract, the mid
tier implementing, the strong tier writing tests in a separate worktree with no sight of the
implementation:

| | result |
|---|---|
| Independent tests on first confrontation | **129 passed, 0 failed** |
| Hidden conformance test | 7/7 |
| Full regression | **455 passed, 5 skipped** (baseline 326 + 5) |
| Cost | $0.27 implementation + $2.13 tests = **$2.40** |

129 independently written tests passing on first contact is a property of the *contract*, not of the
models: both agents were working from a document precise enough that neither had to guess.

Then the suite was attacked, by mutating the implementation and counting failures:

| injected defect | tests that failed |
|---|---|
| the exact cheap-tier defect — parameter becomes a no-op | **19** |
| a caller's dict aliased instead of copied | 2 |
| multi-step initialiser made non-atomic | 1 |
| one state object shared across all keys | **22** |

Every defect class the blind review had found in *any* tier was caught. A suite that merely passes
proves nothing; this is the step that tells you it would have noticed.

## What this does not show

- **One task, one run per model.** An algorithm-mirroring task in Python. Nothing here generalises
  to UI code, data plumbing or refactoring, and a second run could reorder the tiers.
- The judges agreed closely on verdicts and on the main defect, and that defect was then confirmed
  mechanically — but they are models, scoring with a rubric.
- **Two bugs in the author's own hidden test surfaced during grading**, both fixed before these
  numbers: it compared a field during a phase where the specification and the reference class
  genuinely disagree, and it called a method that the reference class also refuses in that state.
  Before the fix, all three tiers "failed" 2/7 for conforming to the specification instead of to the
  test. A hidden test is a test too, and it is written by someone who can be wrong.
