# Study 4 — nine PDF-to-markdown parsers

*October 2026 · 9 parsers × 3 research papers (10, 14 and 22 pages) · objective metrics plus a blind fact-retrieval quiz*

## Question

Agents cannot grep a page image, and sending images costs far more than sending text. So PDFs are
parsed locally first — which means picking a parser. Which one survives real papers with tables and
equations?

## Results

Mean over three papers. **Fact recall** is the share of 25–28 curated ground-truth facts per paper
found by regex in the output — numbers, versions, identifiers, and table-bound values that must
survive as a readable row. It answers "is the number still there at all?"

| Parser | Fact recall | s/doc | kTokens | Headings | Table rows | LaTeX | Control chars |
|---|---|---|---|---|---|---|---|
| **docling** | **100%** | 72.7* | 19.5 | **25** | 21 | 1 | **0** |
| pdfplumber | **100%** | 3.0 | **18.5** | 0 | 0 | 0 | 24 |
| pdftotext -layout | **100%** | **0.1** | 26.5 | 0 | 0 | 0 | 16 |
| pypdf | 99% | 1.5 | 18.9 | 0 | 0 | 0 | 30 |
| **pymupdf4llm** | 97% | **4.0** | 19.5 | 14 | 18 | 1 | **0** |
| marker | 96% | 130.3* | 22.2 | **25** | **25** | **34** | **0** |
| pdftotext | 94% | 0.3 | 18.8 | 0 | 0 | 0 | 19 |
| pdfminer.six | 92% | 1.8 | 19.2 | 0 | 0 | 0 | 52 |
| pymupdf (raw) | 92% | 0.2 | 18.9 | 0 | 0 | 0 | 52 |

### Token cost, which you pay on every read

| Parser | vs pymupdf4llm |
|---|---|
| pdfplumber | −5% |
| docling | −1% |
| marker | **+14%** |
| pdftotext -layout | **+36%** |

\* **Re-measured in October 2026 on an M4 Max and found faster**: docling **49 s**, marker **91 s**,
pymupdf4llm 3.0 s. The ratios hold; the absolute seconds are hardware. The marker token overhead
(+14% raw) holds, but falls to +7% after reference stripping, and the LaTeX advantage replicated
exactly (34.7 expressions against 0.7).

## The downstream test that changed the conclusion

Objective recall says whether a number survived. It does not say whether a model can *retrieve* it.
A cheap-tier agent answered 12 fixed questions per paper from one parser's markdown only, using
`Read` and `Grep`, instructed to answer `NOT FOUND` rather than guess. Graded by regex.

**Almost every parser scored 12/12.** A capable reader with grep recovers nearly everything from
nearly any parse; the 1–2 question differences were reader variance (the same parser scored 12/12
and then 10/12 on a repeat of the same paper). Each quiz cost about $0.004.

That reframes the choice. **Since retrieval is robust, pick on speed, token cost, structure and
cleanliness — not on recall.**

## What the plugin therefore does

1. Parse with **pymupdf4llm** (4 s, markdown headings and tables, no control characters).
2. **Validate**: at least 60% of the characters pymupdf itself extracts from the PDF (a fixed per-page
   floor let a parse truncated to 37% through), at most a handful of control characters, at least
   some headings on a document long enough to have them, and an expected section name present.
3. If validation fails, re-parse with **docling** (49–73 s, and it OCRs scanned pages). Three parsers reached 100% recall;
   docling is the only one of those that also preserves headings and tables.
4. If both fail, **report the document and write nothing** rather than feed a bad parse downstream.
5. `--equations` routes to **marker** when equations are the object of study: 34 LaTeX expressions
   per document against ~1 for everything else.

Steps 2 and 4 exist because of a mistake described below.

## Two rules learned the hard way

**Never run two marker processes concurrently.** The first marker result was 2/12 with 10 `NOT
FOUND`, and was nearly reported as "marker silently drops Methods and Results". It was a measurement
error: two marker runs overlapped and one output was truncated to 14,380 chars instead of 38,719.
Reproduced 3 of 3 times in October 2026: with two or three marker runs overlapping, one document lost
22% of its body text and still passed validation. Marker 2.0 shares model servers through a hardcoded
lock directory (`~/.cache/datalab/surya`), so a second run attaches to the first one's servers; the
GPU is not the mechanism. Run alone, marker's body text is reproducible, but its vision model's figure
descriptions are not (see the setup table), and "38,626 and 38,719 chars on two repeats" was one
output's character and byte count, not two runs.

**Always validate a parse.** The corrupted file looked like valid markdown — title, abstract,
references, sensible headings. Nothing short of the fact quiz revealed it. A parser that fails
loudly is a lesser problem than one that fails plausibly.

## Avoid

- **`pdftotext -layout`**: perfect recall and the fastest option, but **+36% tokens** on every
  single read. On a 50-document corpus that is pure overhead.
- **The plain-text extractors** (pdftotext, pypdf, pdfplumber, pdfminer, raw pymupdf): fast and
  mostly accurate, but they produce **no headings and no table rows**, so table-bound facts become
  scattered digits, and they emit up to 52 control characters per document — enough that `grep`
  treats the file as binary and the agent's main tool stops working.

## Setup friction, which is part of "robust"

| Parser | Install | Notes |
|---|---|---|
| pymupdf4llm | `pip install pymupdf4llm` | seconds, no models |
| docling | `pip install docling` | **1.4 GB venv plus ~0.5 GB of models** on first run |
| marker | `pip install marker-pdf` | **1.3 GB venv plus ~1.8 GB of models**, downloaded on first use into `~/.cache`, not the data directory. A llama.cpp binary is **not** needed to import or to convert a born-digital PDF — only for pages requiring OCR. It leaves model servers running after the parse. |
| MinerU | `pip install "mineru[all]"` + model download + a server | **could not be made to run locally** at v4.0.12: reports "No basic, standard, or advanced engine available" and wants its cloud service, which means uploading your documents |
| GROBID | Docker or a Gradle build | **not tested.** It remains the only option here that returns a *structured reference list*, which matters for automated citation chasing |

## What this does not show

- Three documents from one field, all two-column research papers with tables. Scanned documents,
  slides, forms and books are not represented.
- Ground truth is one person's curated fact list per document.
- The quiz used one reader tier, and its variance (12/12 vs 10/12 on a repeat) is of the same size
  as most differences between parsers.
- MinerU and GROBID conclusions are about installability on one machine, not about output quality.
