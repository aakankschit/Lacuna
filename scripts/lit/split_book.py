"""Split a long PDF into per-chapter text files using its TOC; print token estimates.

usage: split_book.py <book.pdf> <out-dir>
Writes one .txt per chapter, an index.json, and a toc.md that agents grep before reading anything.
A book with no embedded TOC cannot be split this way and is reported rather than guessed at.
"""
import json, os, re, sys, textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import lacuna_config
from prep_md import unreadable

# Control characters survive PDF extraction and make `grep` treat a file as binary, which silently
# turns a book into something a reading agent cannot search at all. Same set prep_md.py strips.
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# What a previous split wrote, and so what a new one may delete.
CHAPTER_FILE = re.compile(r"\d{2,}_[A-Za-z0-9_]*\.txt")
# Pick the shallowest level whose largest chapter is under a quarter of the book, else the level
# with the smallest largest chapter. Counting entries does not work: a book organised in Parts has
# three level-1 entries over hundreds of pages, and an e-book has Cover, Copyright, Contents and
# Preface at level 1 beside them — either way toc.md would name none of the real chapters.
COARSE = 0.25


def die2(msg):
    """Bad input."""
    print(msg, file=sys.stderr)
    sys.exit(2)


def die3(msg):
    """A dependency or a parse is missing."""
    print(msg, file=sys.stderr)
    sys.exit(3)


def one_line(title):
    """A bookmark or metadata title as one clean line. A NUL in a title went into toc.md and the
    first line of its chapter, and `rg` then skipped the chapter as binary; a newline would break
    the chapter's heading and toc.md's table row."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(title or ""))).strip()


def plan(full_toc, page_count):
    """(level, [(title, first page, last page)]): the chapters to write, in page order, covering
    every page of the book once. Empty when no entry points at a valid page."""
    valid = [(lvl, one_line(t), p) for lvl, t, p in full_toc if 0 < p <= page_count]

    def largest_chapter(level):
        """Pages in the longest run between consecutive entries at this level; the last runs to the end."""
        starts = sorted({p for lvl, _, p in valid if lvl == level})
        return max(b - a for a, b in zip(starts, starts[1:] + [page_count + 1]))

    largest = {lvl: largest_chapter(lvl) for lvl in (1, 2, 3) if any(l == lvl for l, _, _ in valid)}
    level = next((lvl for lvl in sorted(largest) if largest[lvl] <= COARSE * page_count),
                 min(largest, key=lambda lvl: (largest[lvl], lvl)) if largest else 1)
    if level > 1:
        print(f"  splitting at TOC level {level}: level 1 "
              + (f"would give a {largest[1]}-page chapter" if 1 in largest else "has no usable entries")
              + f" in a {page_count}-page book.", file=sys.stderr)
    # A split below level 1 keeps each shallower entry that has pages of its own — pages before
    # anything at the split level beneath it: a List of Symbols or an Appendix at level 1 beside
    # the Parts that hold the chapters, a Part's title page. Without them those pages were
    # dropped, or filed under the chapter before them.
    toc = []
    for i, (lvl, title, start) in enumerate(valid):
        below = []
        for l2, _, p2 in valid[i + 1:]:
            if l2 <= lvl:
                break
            if l2 <= level:
                below.append(p2)
        if lvl == level or lvl < level and (not below or start < min(below)):
            toc.append((title, start))
    if not toc:
        return level, []
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
    # Pages before the first bookmark (a title page, a preface nobody bookmarked) are a chapter too.
    if deduped[0][1] > 1:
        deduped.insert(0, ("Before the first bookmark", 1))
    chapters = []
    for i, (title, start) in enumerate(deduped):
        end = deduped[i + 1][1] - 1 if i + 1 < len(deduped) else page_count
        if end < start:                 # cannot happen now that the list is sorted, but be sure
            end = start
        chapters.append((title, start, end))
    return level, chapters


def offered_as(outdir):
    """The toc.md path a reader is offered for a book split into `outdir`, or None when it is not
    offered: lit_pipeline offers <books>/<name>/toc.md and nothing deeper or elsewhere."""
    books = lacuna_config.path("books")
    parent = os.path.dirname(os.path.abspath(outdir))
    try:
        inside = os.path.samefile(parent, books)    # macOS folds case and Unicode form
    except OSError:
        inside = False
    return os.path.join(books, os.path.basename(os.path.abspath(outdir)), "toc.md") if inside else None


def previous_chapters(outdir):
    """The chapter files an earlier split listed in index.json, so a re-split can remove the ones it
    does not write again: stale chapters stayed beside a new toc.md and turned up in every grep."""
    try:
        with open(os.path.join(outdir, "index.json")) as f:
            rows = json.load(f)
        names = [r["file"] for r in rows]
    except (OSError, ValueError, TypeError, KeyError):
        return set()
    return {n for n in names if isinstance(n, str) and CHAPTER_FILE.fullmatch(n)
            and os.path.isfile(os.path.join(outdir, n))}


def main():
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
    try:
        doc = pymupdf.open(pdf)
        full_toc = doc.get_toc()
    except Exception as e:      # pymupdf's own FileDataError, or whatever MuPDF raises on a damaged file
        die3(f"ERROR: pymupdf could not read {os.path.basename(pdf)} ({type(e).__name__}: {e}). "
             f"It may be damaged, encrypted or not a PDF; nothing was written.")
    if not full_toc:
        die2(f"ERROR: {os.path.basename(pdf)} has no table of contents, so it cannot be split by "
             f"chapter. Parse it as one document instead: `lacuna lit read {pdf}`.")
    level, chapters = plan(full_toc, doc.page_count)
    if not chapters:
        die2(f"ERROR: {os.path.basename(pdf)} has a table of contents, but none of its entries point "
             f"at a valid page. Parse it as one document instead.")

    os.makedirs(outdir, exist_ok=True)
    stale = previous_chapters(outdir)
    # A book is offered to readers when its toc.md exists, so it goes first and comes back last:
    # a split cut short must not leave an index naming a half-rewritten set of chapters.
    if os.path.exists(f"{outdir}/toc.md"):
        os.remove(f"{outdir}/toc.md")
    rows = []
    for i, (title, start, end) in enumerate(chapters):
        text = "\n".join(doc[p].get_text() for p in range(start - 1, end))
        text = CTRL.sub("", text)
        text = "\n".join(l for ln in text.splitlines() for l in (textwrap.wrap(ln, 300) or [""]))
        fn = f"{i:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', title)[:40].strip('_')}.txt"
        with open(f"{outdir}/{fn}", "w") as f:
            f.write(f"# {title} (pp. {start}-{end})\n\n{text}")
        if len(text.strip()) < 200:
            print(f"  ! {title!r} (pp. {start}-{end}) extracted almost no text — the PDF may have no "
                  f"text layer there, or its bookmarks may be out of page order", file=sys.stderr)
        elif unreadable(text):          # long, and still nothing a grep can match
            print(f"  ! {title!r} (pp. {start}-{end}): {unreadable(text)}", file=sys.stderr)
        rows.append({"file": fn, "title": title, "pages": [start, end], "chars": len(text), "tok": round(len(text) / 2.9)})
    # By file, not by name: on a case-insensitive disk "00_Introduction.txt" and "00_INTRODUCTION.txt"
    # are one file, and a re-split with a case-only title change deleted every chapter it had written.
    written = [os.path.join(outdir, r["file"]) for r in rows]
    for fn in stale:
        old = os.path.join(outdir, fn)
        if not any(os.path.samefile(old, w) for w in written if os.path.exists(w)):
            os.remove(old)
    widest = max(r["pages"][1] - r["pages"][0] + 1 for r in rows)
    if widest > COARSE * doc.page_count:
        print(f"  ! the largest chapter covers {widest} of {doc.page_count} pages — check that toc.md "
              f"names what you expect before relying on it", file=sys.stderr)
    with open(f"{outdir}/index.json", "w") as f:
        json.dump(rows, f, indent=1)

    # toc.md is what the reading agents consult: lit_pipeline only offers a book that has one.
    title = one_line((doc.metadata or {}).get("title")) or os.path.basename(os.path.normpath(outdir))
    total = sum(r["tok"] for r in rows)
    with open(f"{outdir}/toc.md", "w") as f:
        f.write(f"# {title}\n\n"
                f"{len(rows)} chapters, {doc.page_count} pages, ~{total:,} tokens in total.\n\n"
                f"**Grep the chapter you need; never read a whole chapter.** Each row gives the file, the\n"
                f"pages it covers and its size, so the cost of a read is visible before making it.\n"
                f"The chapter files sit in this directory, beside this index.\n\n"
                f"| chapter | file | pages | ~tokens |\n|---|---|---|---|\n")
        for r in rows:
            cell = r["title"].replace("|", "\\|")       # a `|` in a title would start a new column
            f.write(f"| {cell} | `{r['file']}` | {r['pages'][0]}-{r['pages'][1]} | {r['tok']:,} |\n")

    for r in rows:
        print(f"{r['file'][:46]:46s} pp{r['pages'][0]:>4}-{r['pages'][1]:<4} ~{r['tok']:>7,} tok")
    print(f"TOTAL ~{total:,} tok over {doc.page_count} pages")
    print(f"wrote {outdir}/toc.md — the index agents grep.")
    offered = offered_as(outdir)
    if offered:
        print(f"Readers are offered it as {offered}.")
    else:
        # Exit 0 all the same: the chapters are fine, and may be wanted for something else.
        books = lacuna_config.path("books")
        print(f"  ! this book is NOT offered to readers: they are offered {books}/<name>/toc.md and "
              f"nothing else. Split it into {os.path.join(books, os.path.basename(os.path.abspath(outdir)))} "
              f"to have it offered.", file=sys.stderr)


if __name__ == "__main__":
    main()
