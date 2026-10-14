"""Split a long PDF into per-chapter text files using its top-level TOC; print token estimates.

usage: split_book.py <book.pdf> <out-dir>
Writes one .txt per top-level chapter, an index.json, and a toc.md that agents grep before reading
anything. A book with no embedded TOC cannot be split this way and is reported rather than guessed at.
"""
import json, os, re, sys, textwrap

# Control characters survive PDF extraction and make `grep` treat a file as binary, which silently
# turns a book into something a reading agent cannot search at all. Same set prep_md.py strips.
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def die2(msg):
    """Bad input."""
    print(msg, file=sys.stderr)
    sys.exit(2)


def die3(msg):
    """A dependency or a parse is missing."""
    print(msg, file=sys.stderr)
    sys.exit(3)


if len(sys.argv) != 3:
    die2(__doc__.strip().splitlines()[2])

try:
    import pymupdf
except ImportError:
    die3("ERROR: split-book needs pymupdf, which comes with the default parser.\n"
         "Run `lacuna doctor --install-pdf`, then run split-book again.")
pdf, outdir = sys.argv[1], sys.argv[2]
if not os.path.exists(pdf):
    die2(f"ERROR: no such file: {pdf}")
os.makedirs(outdir, exist_ok=True)
doc = pymupdf.open(pdf)
full_toc = doc.get_toc()
if not full_toc:
    die2(f"ERROR: {os.path.basename(pdf)} has no table of contents, so it cannot be split by "
         f"chapter. Parse it as one document instead: `lacuna lit read {pdf}`.")


def entries_at(level):
    return [(t, p) for lvl, t, p in full_toc if lvl == level and 0 < p <= doc.page_count]


# Pick the shallowest level that actually divides the book: the first with at least 5 entries,
# otherwise whichever level has the most. A book organised in Parts has two or three level-1
# entries over hundreds of pages — splitting there gives 240-page "chapters" and a toc.md that
# names none of the real ones.
USEFUL = 5
counts = {lvl: len(entries_at(lvl)) for lvl in (1, 2, 3) if entries_at(lvl)}
level = next((lvl for lvl in sorted(counts) if counts[lvl] >= USEFUL),
             max(counts, key=lambda lvl: (counts[lvl], -lvl)) if counts else 1)
toc = entries_at(level)
if not toc:
    die2(f"ERROR: {os.path.basename(pdf)} has a table of contents, but none of its entries point "
         f"at a valid page. Parse it as one document instead.")
if level > 1:
    print(f"  splitting at TOC level {level}: level 1 had {len(entries_at(1))} entries for "
          f"{doc.page_count} pages, which is too coarse to be useful.", file=sys.stderr)

# Bookmarks are not guaranteed to be sorted, and two chapters may claim the same start page.
toc.sort(key=lambda tp: tp[1])
deduped = []
for title, start in toc:
    if deduped and start == deduped[-1][1]:
        print(f"  ! {title!r} starts on the same page as {deduped[-1][0]!r} (p. {start}); "
              f"merging the titles rather than writing an empty chapter", file=sys.stderr)
        deduped[-1] = (f"{deduped[-1][0]} / {title}", start)
        continue
    deduped.append((title, start))
toc = deduped
if len(toc) < 5 and doc.page_count > 150:
    print(f"  ! only {len(toc)} entries for {doc.page_count} pages — the chapters will be large. "
          f"Check toc.md names what you expect before relying on it.", file=sys.stderr)
rows = []
for i, (title, start) in enumerate(toc):
    end = toc[i + 1][1] - 1 if i + 1 < len(toc) else doc.page_count
    if end < start:                     # cannot happen now that the list is sorted, but be sure
        end = start
    text = "\n".join(doc[p].get_text() for p in range(start - 1, end))
    text = CTRL.sub("", text)
    text = "\n".join(l for ln in text.splitlines() for l in (textwrap.wrap(ln, 300) or [""]))
    fn = f"{i:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', title)[:40].strip('_')}.txt"
    with open(f"{outdir}/{fn}", "w") as f:
        f.write(f"# {title} (pp. {start}-{end})\n\n{text}")
    if len(text.strip()) < 200:
        print(f"  ! {title!r} (pp. {start}-{end}) extracted almost no text — the PDF may have no "
              f"text layer there, or its bookmarks may be out of page order", file=sys.stderr)
    rows.append({"file": fn, "title": title, "pages": [start, end], "chars": len(text), "tok": round(len(text) / 2.9)})
with open(f"{outdir}/index.json", "w") as f:
    json.dump(rows, f, indent=1)

# toc.md is what the reading agents consult: lit_pipeline only offers a book that has one.
title = (doc.metadata or {}).get("title") or os.path.basename(os.path.normpath(outdir))
total = sum(r["tok"] for r in rows)
with open(f"{outdir}/toc.md", "w") as f:
    f.write(f"# {title}\n\n"
            f"{len(rows)} chapters, {doc.page_count} pages, ~{total:,} tokens in total.\n\n"
            f"**Grep the chapter you need; never read a whole chapter.** Each row gives the file, the\n"
            f"pages it covers and its size, so the cost of a read is visible before making it.\n"
            f"The chapter files sit in this directory, beside this index.\n\n"
            f"| chapter | file | pages | ~tokens |\n|---|---|---|---|\n")
    for r in rows:
        f.write(f"| {r['title']} | `{r['file']}` | {r['pages'][0]}-{r['pages'][1]} | {r['tok']:,} |\n")

for r in rows:
    print(f"{r['file'][:46]:46s} pp{r['pages'][0]:>4}-{r['pages'][1]:<4} ~{r['tok']:>7,} tok")
print(f"TOTAL ~{total:,} tok over {doc.page_count} pages")
print(f"wrote {outdir}/toc.md — the index agents grep; a book without it is not offered to them.")
