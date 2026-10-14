# Limits

What this plugin's numbers do **not** establish. Read this before quoting any figure from it.

## The benchmarks are one domain, one week, one set of model versions

Every measurement was taken in October 2026, on research papers from a single computational-science
subfield and one mid-sized Python library, by one person, on one machine. The tier pattern that came
out of it — cheap reads, mid writes code, strong synthesises and tests — is a defensible default, not
a law. It may not transfer to UI code, data plumbing, refactoring, legal or medical text, or
languages other than Python.

Say "on this benchmark" when repeating these numbers. Never "in general".

## Sample sizes are small, and some findings are a single run

| Finding | n | Strength |
|---|---|---|
| Reading recall is tier-independent | 30 runs (15 cells × 2 repeats), 2 documents, blind-judged | reasonably solid |
| Reader × synthesiser grid | 18 plans, 3×3 grid × 2 context conditions, blind-judged | solid on the synthesiser axis |
| Per-document vs batched reading cost | repeated runs, same documents | solid; it is arithmetic over token counts |
| Effort is a cost multiplier | 30 runs across 5 effort levels | solid on cost, weaker on quality |
| Books halve factual errors | **2 cells, one run each** | suggestive only |
| Strong tier adds one mechanism-level proposal | single runs per cell | **within noise** |
| Mid tier matches strong tier on code | 2 tasks, 1 run per model | suggestive; replicated once |
| Strong tier is worth paying for as test author | 2 tasks | **did not replicate** — see code-writing.md |
| Parser fact recall | 9 parsers, 3 documents, 12-fact quiz | holds for those documents |

## The silent-no-op story is one case

One implementation, once, omitted a production code path and made a documented parameter a no-op
while passing every test put to it. That single case justifies writing tests blind to the
implementation as a **precaution**. It does not establish a rate, and it does not mean a given tier
will do it again.

## Cost figures are list price, not your bill

Everything this plugin prints is an estimate derived from token counts in Claude Code's transcript
files times a price table. It does not model subscription plans, included usage, rate limits, batch
discounts or enterprise agreements, and the transcript format is internal rather than a documented
contract. The price table is dated (`scripts/pricing.json`) and will rot; override it in your data
directory when it does.

`/spend` estimates. It does not reconcile with an invoice.

## The cache guard is a heuristic

Prompt-cache lifetime and the exact conditions for a cold re-write are not a documented contract.
The idle warning can be wrong in either direction: it can warn when the cache was still warm, and it
can stay quiet when you were charged. It exists to make an invisible cost noticeable, not to be
trusted to the cent.

## The status line reads undocumented fields

Rate-limit percentages and cache-TTL details come from fields that are not a public interface. They
may change or disappear without notice; when they do, those segments go blank rather than lying.

## Parser claims hold for the documents tested

GROBID was not tested (it needs Docker) and remains the only option in the comparison that produces
a structured reference list. MinerU could not be made to run locally at the version tested. Both are
stated as untested, not as rejected.

## Platform

Developed and tested on macOS with bash. Linux should work; nothing in it is macOS-specific except
convenience commands in the docs. Windows is untested rather than unsupported. The two-pane results
viewer needs tmux, or a terminal you split yourself.

## It does not read documents for you

The pipeline produces notes, a digest, a plan and a fact-check. The fact-check catches invented paths,
misquoted numbers and mis-attributions, which is the dominant error class — 27 of 75 findings in the
error taxonomy were a claim attributed to the wrong paper. It does not catch a bad idea, and nothing
here replaces reading the document when the stakes are high.
