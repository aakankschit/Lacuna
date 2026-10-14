# Warm-Starting from Related Tasks (Fictional Example, 2026)

> FICTIONAL EXAMPLE. This paper does not exist. Written by hand to demonstrate the note format.

## Claim
A prior transferred from a related task beats an uninformative prior early and is indistinguishable
from it later, so the transfer should be shrunk toward the uninformative prior as data arrives
rather than used at fixed strength.

## Method and evaluation
12 task pairs with varying relatedness; shrinkage strength swept over 7 values. Metric: regret at
10, 100 and 1,000 evaluations.

## Key results
- At 10 evaluations, transferred priors cut regret by 44% (Table 3).
- At 1,000 evaluations the difference was within noise for every shrinkage strength (Table 3).
- A badly related task pair made fixed-strength transfer **worse** than no transfer, by 18%
  (Section 6, "negative transfer").

## Concepts looked up
- James-Stein shrinkage — supplied reference text, chapter 4 — shrink a per-group estimate toward a
  global estimate by an amount that falls as the group's sample size rises.

## Relevance to the project
Argues for a shrinkage parameter rather than a boolean "use prior" flag. *(Inference.)*

## Open problems
1. Relatedness is assumed known. Evidence: Section 3 selects pairs by hand. Detecting negative
   transfer online is unaddressed.
2. No interaction with batching is tested; the two mechanisms both act on early-phase estimates.
