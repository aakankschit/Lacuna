---
name: lit-review
description: Cost-measured literature pipeline that turns papers, reports or textbooks into notes, a digest, or a development plan for a codebase. Parses PDFs locally, reads one document per cheap agent in parallel, caches notes so a document is never read twice, then synthesises and fact-checks. Use when the user wants documents read against a project ("what should we build from these papers?", "digest these PDFs").
disable-model-invocation: false
---

# Literature → notes → digest or plan

Driver: `${CLAUDE_PLUGIN_ROOT}/bin/lacuna lit` (subcommands `read`, `synth`, `verify`, `estimate`, `split-book`).
Every run appends a line to the cost ledger; `lacuna config` prints where that is.

The defaults below are measured, not assumed. The evidence, with sample sizes and what each number
does **not** support, is in `${CLAUDE_PLUGIN_ROOT}/docs/benchmarks/`. Read those before overriding a
default, and read `docs/LIMITS.md` before quoting a number to anyone.

## The pipeline

```bash
LAC=${CLAUDE_PLUGIN_ROOT}/bin/lacuna

# 0. estimate — free, local token count against the calibration. Do this first.
#    --json gives a machine-readable report: read `any_over_budget` and `cheapest_over_budget`
#    rather than parsing the table, and show the user the figure before spending anything.
$LAC lit estimate <pdfs-or-mds> --per-agent 1 --json

# 1. read — one slim cheap-tier agent per document, in parallel. ~$0.015/document, ~60-100 s for up to 8 documents (they run 8 at a time, then in waves).
$LAC lit read <pdfs-or-mds> --out <project>/lit/notes \
    --brief "Project: <one or two lines on what the code does and what it is for>"

# 2a. digest — mid tier, organised by objective rather than by document. ~$0.24 for 6 notes.
$LAC lit synth --notes <notes-dir> --repo <repo> --out digest.md \
    --kind digest --objective "<the question the digest must answer>"

# 2b. plan — ranked development proposals against the code, from the NOTES.
$LAC lit synth --notes <notes-dir> --repo <repo> --out plan.md \
    --kind plan --objective "<what the next iteration should achieve>"

# 3. verify — always. A cheap agent greps every named path, number and attribution. ~$0.02-0.09.
$LAC lit verify --target <digest-or-plan> --repo <repo> --notes <notes-dir>
```

Report the cost line for each stage, then hand the user the verifier's findings alongside the
output. **Never present a digest or plan without having run `verify`.** Both measured synthesis
failure modes were "named a thing it never read".

Which model fills each role comes from config (`lacuna config` to see it; `models.reader`,
`models.digester`, `models.planner`, `models.verifier`). `--model` overrides it for one run.

## Why it is built this way (measured)

- **One document per agent, not batched.** Per-document reading was 31% cheaper and ~2× faster than
  two per call: it pays 8% more startup but **42% less re-reading**, because within one call every
  later turn re-reads the earlier document. This only holds with a slim agent (Read/Grep/Write, no
  project instructions, ~4k-token start); a general-purpose agent's ~34k start would swamp it.
- **The cheap tier reads.** Recall of open problems was model-independent (0.48–0.54 across three
  tiers). A stronger reader is *worse* downstream: its notes produced 2.8 factual errors per plan
  against 1.7 for the cheap tier, with 4× the mis-attributions, because richer notes assert more and
  blur which document said what. Use the mid tier only for documents central to a manuscript.
- **Synthesis is where quality lives.** Strong vs cheap tier as synthesiser: 5.4 vs 3.6 verified
  viable proposals, 1.3 vs 0.4 at mechanism level. Reader choice barely moves either.
- **Choose the synthesis tier by output, not by budget.** A *digest* (what the literature
  establishes, organised by objective) is a mid-tier job. A *development plan* is a judgement call:
  on identical notes the mid tier planned for $0.53 with 8.0 viable proposals at 100% verified code
  references; the strong tier cost $1.44 for 8.0 viable with one more at mechanism level, at 75%
  verified. One extra mechanism-level proposal is within single-run noise. Default to the mid tier
  for routine iterations; bring in the strong tier when the goal is a publishable mechanism.
- **Plan from the notes, not from the digest.** Chaining read → digest → plan (`--digest`) costs more
  and halves mechanism-level proposals, because the digest compresses away the raw material the
  planner generates from. It does improve accuracy, but `verify` fixes that far more cheaply. Use
  `--digest` only when the digest is the thing you want planned against.
- **Effort is a cost multiplier, not a quality lever.** Up to 47× the cost from low to max for no
  measured gain on reading or synthesis. The pipeline fixes effort at medium, and high only for the
  verifier, where it is cheap and does help.
- **Notes are cached**, keyed by document content *plus* the reader model and effort, the brief, the
  prompt version and the books offered — so re-running is free, but changing any of those correctly
  misses the cache. Six notes
  are ~9k tokens against ~104k for six papers, so every later synthesis is ~11× cheaper.
- **The synthesiser gets an authoritative file list** (`git ls-files`, or a filtered walk outside a
  repo) and is told to grep before naming a path. In the 18-plan grid no plan named a file or class
  that does not exist; an earlier capability test, run without the file list, did invent one.
- **Verification is cheap and catches the dominant error class.** A cheap pass found, for $0.02, a
  0.1%-vs-1% mix-up, a wrong time breakdown and two over-strong attributions; for $0.085 it
  independently reproduced both blind judges' findings on a plan.

## PDF parsing

`read` parses with **pymupdf4llm** (~4 s/document), validates the output, and falls back to
**docling** (~1 min/document, with OCR) if validation fails. A document that fails both is reported, not passed
downstream — there is deliberately no plain-text fallback. `--equations` routes to **marker** when LaTeX fidelity matters (~34 expressions/document against
~1 elsewhere, at 91–130 s and +14% raw tokens). It needs ~3 GB of venv and models.

From a bake-off of 9 parsers over 3 papers (`docs/benchmarks/pdf-parsers.md`):

- A cheap reader with `grep` recovered 12/12 quiz facts from almost every parser, so **retrieval is
  robust**; choose on speed, token cost, structure and cleanliness instead.
- `pdftotext -layout` costs **+36% tokens** for the same documents; plain-text extractors lose all
  tables and emit control characters that make `grep` treat the file as binary.
- **Never run two marker processes concurrently** — reproduced 3 times out of 3: 22% of the body
  text vanished and the result still passed validation. Marker 2.0 shares model servers through a
  hardcoded lock directory, so a second run can attach to the first one's.
- **Marker's figure descriptions are generated, not extracted, and are not reproducible.** On two
  of three papers they changed between runs and contained invented compounds and identifiers. Use
  `--equations` for the mathematics, and do not trust its prose about figures.
- **Never read a PDF with the `Read` tool** for extraction: it sends page images, which cannot be
  grepped, section-filtered or reference-stripped. Images are for when the figure itself is the
  object of study.

## Books and long references

`lacuna lit split-book <pdf> <books-dir>/<name>` splits a long document into chapter files plus a
grep-able `toc.md`. A book split into a directory of its own directly inside the configured books
directory is offered to readers, which grep the index and read only the lines they need; split-book
warns when it is not. `--no-books` skips them.

**Books do not generate ideas; they prevent mistakes.** In the ablation, idea yield was identical
with and without them, but errors roughly halved (4.0 → 1.5 per plan for the cheap reader) and
fabricated attributions fell. They roughly quadruple the reading step. Keep them for unfamiliar
mathematics and for the cheapest reader, which looks things up most; drop them for routine triage of
familiar material. (Two cells, one run each — suggestive, not established.)

## Before a long run

`lacuna lit estimate` prints cost and wall time per model × effort and says whether the calibration is
yours or upstream. Show the user the plan and the estimate before starting anything above the
configured warning threshold (`budget_warn_usd`, default $2), and wait for approval.

## After the run

- If the project keeps a living status or decisions file, offer to record the conclusions in it. The
  synthesiser already reads one if it finds it and is told not to re-propose anything recorded as a
  dead end.
- Configure `after.read` / `after.synth` / `after.verify` in `config.json` to file outputs anywhere
  else automatically. Each runs through a shell with the output path **appended as the final
  argument**, so a command with no arguments of its own sees it as `$1`; one that carries its
  own arguments must read the last.
- Recalibrate from the cost ledger once it has more runs than the calibration was built on.

## If a stage exits 2 because a file is not Lacuna's

`read`, `synth` and `verify` replace a file only when Lacuna wrote it (for the same document, in the
case of a note) and nobody has edited it since; the record is `.lacuna.json` beside the files. When
one of those is not true they exit 2 before any model call and name every such file and why: edited
since, no record, another document's note with the same file name, a document changed since its note
was written, or a file another stage wrote. **Show the user that message and ask.** Do not add
`--overwrite` on your own: it discards their edits, or another paper's note. For two papers with one
file name (`main.pdf` in two folders), rename one or use another `--out`.

One exit 2 comes after the model call: a note, plan or report that changed while the stage ran is
left as it is. A plan or report's result is saved beside it as `<out>.new.md`; a note's is cached, and
the same command with `--overwrite` places it for free. Tell the user and let them choose; never run
`synth` or `verify` again for it, which pays again. If the message says another Lacuna run is writing
the file, that run is still going: wait for it rather than retrying.

## If a model refuses

A headless agent can refuse a document that the interactive session handles without complaint, and
tiers differ in how often that happens. `read` exits 4 and caches nothing rather than storing a
refusal as a note. Try another model for that role (`--model`) and report the refusal to the user
rather than working around it silently. The same exit 4 covers an agent that says it could not read
its sources (`INCOMPLETE: …`) or files a document saying so (`PARTIAL: …`): nothing is filed. Find
out what it could not read before re-running — a paid re-run on unreadable inputs fails again.
