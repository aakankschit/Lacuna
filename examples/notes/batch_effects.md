# Batch Size and Posterior Quality (Fictional Example, 2025)

> FICTIONAL EXAMPLE. This paper does not exist. Written by hand to demonstrate the note format.

## Claim
Large evaluation batches degrade posterior quality in sequential selection, because every item in a
batch is chosen from the same stale posterior. The degradation scales with batch size, not with
total budget.

## Method and evaluation
Sequential selection on three public datasets, batch sizes 1, 8, 64 and 512, fixed total budget.
Metric: regret against an oracle, and calibration of the posterior variance.

## Key results
- Regret rose 2.4× from batch size 1 to 512 at equal total budget (Table 1).
- Posterior variance was under-estimated by 31% at batch 512, and by 4% at batch 8 (Section 5.2).
- Batch 8 was statistically indistinguishable from batch 1 on all three datasets (Section 5.1).

## Concepts looked up
none

## Relevance to the project
Suggests a batch-size ceiling rather than a schedule. `demo-repo` has no batching at all, so this is
about a future feature, not a fix. *(Inference.)*

## Open problems
1. No treatment of heterogeneous evaluation cost within a batch. Evidence: all items cost 1 unit.
2. The calibration result is reported only for one model family (Section 5.2), so it may be a
   property of that family rather than of batching.
