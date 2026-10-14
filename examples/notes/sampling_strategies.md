# Adaptive Sampling for Expensive Objectives (Fictional Example, 2026)

> FICTIONAL EXAMPLE. This paper does not exist. Written by hand to demonstrate the note format.

## Claim
When evaluating a candidate is far more expensive than deciding which candidate to evaluate, the
allocation rule matters more than the scoring model. The paper argues that an adaptive rule that
reallocates budget between regions of the search space dominates a fixed-split rule under every
budget they test.

## Method and evaluation
Simulated search over a synthetic benchmark of 10,000 candidates with a tunable noise level. Three
allocation rules compared at four budgets. Metric: fraction of the true top-100 recovered.

## Key results
- Adaptive allocation recovered 0.71 of the top-100 against 0.52 for a fixed split, at the same
  budget (Table 2).
- The advantage disappears below a budget of 2% of the candidate pool (Section 4.3).
- Noise above sigma = 0.4 reverses the ordering: the fixed split becomes better (Figure 5).

## Concepts looked up
none

## Relevance to the project
The project's `sampler` module uses a fixed split. The reversal at high noise is the part that bears
on it — our objective noise is not characterised, so adopting this would need that measurement first.
*(This paragraph is inference, not the paper's claim.)*

## Open problems
1. No guidance on estimating sigma before committing to a rule. Evidence: Section 4.3 treats sigma
   as known. Resolving it would need a cheap pilot estimator.
2. Single-objective only. Evidence: the benchmark defines one scalar. Extending to several
   objectives with different noise levels is untested.
