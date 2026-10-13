# demo-repo

A deliberately small library used as the target for the lacuna worked example. It selects
items from a pool using a score and a fixed exploration split.

- `src/widget/sampler.py` — the selection rule. One fixed split, no batching, no priors.
- `src/widget/scorer.py` — scores a candidate.
- `tests/test_sampler.py` — the existing suite.

## Status

Working, single-objective, no batching. Known gaps: the exploration split is a constant, objective
noise is never measured, and there is no way to carry information between runs.

## Ruled out

- **Full Bayesian optimisation with a GP.** Tried and abandoned: the pool is too large for an
  O(n^3) fit and the gain did not justify the dependency.
