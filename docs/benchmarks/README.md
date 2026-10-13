# Benchmarks

Four studies, run October 2026, that produced the defaults this plugin ships. Read
[../LIMITS.md](../LIMITS.md) first: these are small samples from one domain.

| Study | What it measured | n | Spend |
|---|---|---|---|
| [lit-reading.md](lit-reading.md) | model tier × reasoning effort on extracting open problems | 30 runs, 2 documents | $55.92 |
| [lit-synthesis.md](lit-synthesis.md) | reader tier × synthesiser tier, and whether reference books help | 20 plans | $52.25 |
| [code-writing.md](code-writing.md) | three tiers implementing one specification, blind-reviewed | 3 runs, 1 task | $2.33 |
| [pdf-parsers.md](pdf-parsers.md) | 9 PDF-to-markdown parsers on fact recall, speed and token cost | 9 × 3 documents | — |

## Shared protocol

**Tiers.** Results are reported as cheap / mid / strong tier rather than by model name, because
model names go stale faster than the pattern does. At the time of measurement these were Haiku 5.5,
Sonnet 5.5, and Opus 5 (with Opus 5.5 and Fable 5.1 also measured where noted).

**Blind judging.** Outputs were anonymised to letters before judging, judged independently by two
models from different tiers against a written rubric, and the scores averaged. Where the two judges
disagreed, that is reported rather than averaged away.

**Mechanical verification.** Every claim a judge made about the codebase — a named file, a quoted
number, an attribution — was re-checked with `grep` afterwards. Several judge findings did not
survive that check and were dropped. The headline defect in the code-writing study was confirmed
mechanically (counting references to the missing code path), not taken on a judge's word.

**Cost.** Taken from each run's reported `total_cost_usd` at list price, not estimated from token
counts after the fact.

## Reproducing them

The harness that ran these studies is not shipped: it is bound to the author's document corpus and
the repository under test, and publishing it would mean publishing both. What is shipped is the
pipeline the studies produced (`lacuna lit`) and the cost ledger it writes, which is enough to calibrate
the same numbers for your own corpus:

```bash
lacuna lit read <your documents> --out notes/ --brief "<your project>"   # reader cost per document
lacuna lit synth --notes notes/ --repo <your repo> --out plan.md         # synthesis cost
lacuna lit verify --target plan.md --repo <your repo> --notes notes/     # error rate per plan
```

The ledger line from each stage gives you cost and wall time per document and per model. Scoring
"viable proposals" needs a rubric and a judge, and whether a proposal is viable is specific to your
project — that part does not generalise and is why the harness is not here.
