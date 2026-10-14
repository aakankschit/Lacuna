"""PDF -> reference-stripped, line-wrapped markdown for agent reading, with validation and fallback.

Default parser is pymupdf4llm (~4 s/document, markdown headings and tables, no control characters).
If the parse fails validation, retry with docling (~1 min/document, 100% fact recall in the
parser bake-off; see docs/benchmarks/pdf-parsers.md). A parse that fails both is reported rather than passed downstream: a bad parse can look
like valid markdown, so silence is not success.

usage: prep_md.py <pdf> <out.md> [<pdf> <out.md> ...] [--equations] [--parser NAME] [--quiet]
  --equations  route to marker (LaTeX fidelity: ~34 expressions/document vs ~1); needs the marker venv
  --parser     force one of: pymupdf4llm, docling, marker
"""
import os, re, subprocess, sys, textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import lacuna_config

DATA = lacuna_config.DATA
# A reference-list heading. The leading junk covers numbered headings, bold markers and ACS's
# "■ REFERENCES", which pymupdf4llm renders as "# ■<sup>**REFERENCES**</sup>" — hence the tags.
REF = re.compile(r"\n[#*\s■□●▪•]*(?:<[^>\n]*>)*\**\s*(?:\d+[.)]\s*)?(References|Bibliography|"
                 r"References and Notes|Literature Cited)\**(?:<[^>\n]*>)*[:\s]*\n", re.I)
# One citation entry: a numbered paragraph that carries a year. Measured on three journal papers,
# the list cannot be cut as a block: a "Publisher's Note" box lands between entries 2 and 3, Nature
# puts a second list before Methods, and acknowledgements and Extended Data legends follow the last.
ENTRY = re.compile(r"\s*(?:[-*]\s+)?(?:\(\d{1,4}\)|\[\d{1,4}\]|\d{1,4}[.)])\s.*\b(?:1[89]|20)\d{2}\b", re.S)
HEADING = re.compile(r"\n#{1,4}\s+\S")
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# each parser runs in its own venv so their dependencies never collide
# Created by `lacuna doctor --install-*`; docling and marker are optional extras.
VENVS = {
    "pymupdf4llm": lacuna_config.path("python"),
    "docling": f"{DATA}/venv-docling/bin/python",
    "marker": f"{DATA}/venv-marker/bin/python",
}
SCRIPTS = {
    "pymupdf4llm": "import sys, pymupdf4llm; print(pymupdf4llm.to_markdown(sys.argv[1]), end='')",
    "docling": ("import sys\n"
                "from docling.document_converter import DocumentConverter\n"
                "print(DocumentConverter().convert(sys.argv[1]).document.export_to_markdown(), end='')"),
    "marker": ("import sys\n"
               "from marker.converters.pdf import PdfConverter\n"
               "from marker.models import create_model_dict\n"
               "from marker.output import text_from_rendered\n"
               "t, _, _ = text_from_rendered(PdfConverter(artifact_dict=create_model_dict())(sys.argv[1]))\n"
               "print(t, end='')"),
}


def page_count(pdf):
    return pdf_stats(pdf)[0]


def pdf_stats(pdf):
    """(pages, characters of raw extractable text) straight from pymupdf.

    The character count is the thing that makes truncation detectable. A fixed "chars per page"
    threshold cannot: a dense paper truncated to a third of its text still clears any floor low
    enough not to reject a sparse one. Comparing a parse against the document's OWN text is the
    only check that works for both.
    """
    try:
        out = subprocess.run(
            [VENVS["pymupdf4llm"], "-I", "-c",
             "import sys, pymupdf\n"
             "d = pymupdf.open(sys.argv[1])\n"
             "print(d.page_count, sum(len(p.get_text()) for p in d))", pdf],
            capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        pages, chars = out.split()
        return int(pages), int(chars)
    except (ValueError, OSError):
        return 0, 0


def parse(pdf, parser):
    py = VENVS.get(parser)
    if not py or not os.path.exists(py):
        return None, f"venv missing ({py})"
    r = subprocess.run([py, "-I", "-c", SCRIPTS[parser], pdf], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        # Keep enough to see the exception type and its message. 160 characters reliably cut the
        # cause off the front of a traceback and left only the final frame.
        err = (r.stderr or "").strip()
        lines = [l for l in err.splitlines()      # drop the indented source and caret lines
                 if l.strip() and not l.startswith((" ", "\t")) and not l.lstrip().startswith('File "')]
        return None, " | ".join(lines[-3:]) if lines else err[-400:]
    return r.stdout, None


# A parse must keep most of the text the PDF actually contains. Measured: a parse truncated to
# 37% of its text passed the old per-page floor, which is the exact failure validation exists for.
KEEP_FRACTION = 0.60
# Control characters are stripped by clean(), so a handful is cosmetic. A flood means the parser
# produced binary-ish output; grep would treat the file as binary and the reader would find nothing.
CTRL_BUDGET = 8


def validate(md, pages, raw_chars=0):
    """Return a list of problems. `raw_chars` is the PDF's own extractable character count."""
    bad = []
    if raw_chars > 2000 and len(md) < raw_chars * KEEP_FRACTION:
        bad.append(f"kept only {len(md) / raw_chars:.0%} of the document's {raw_chars:,} "
                   f"characters ({len(md):,}) — the parse is truncated")
    # The floor stays even with a baseline: pymupdf's own count is also tiny for a mostly scanned
    # document, and a one-page text layer must not pass the other eleven pages as read.
    if len(md) < 1200 * max(pages, 1) * 0.25:
        bad.append(f"only {len(md):,} chars for {pages} pages")
    n_ctrl = len(CTRL.findall(md))
    if n_ctrl > CTRL_BUDGET:
        bad.append(f"{n_ctrl} control characters")
    if pages >= 4 and len(re.findall(r"(?m)^#{1,4} \S", md)) == 0:
        bad.append("no markdown headings")
    if not re.search(r"(?i)(abstract|introduction|method|result)", md):
        bad.append("no recognisable section word")
    return bad


def body(tail):
    """Everything after a References heading, minus the citation entries.

    An author-year list has no numbered entries to recognise; it is cut to the next heading, which
    keeps whatever section follows it.
    """
    paras = re.split(r"\n\s*\n", tail)
    kept = [p for p in paras if not ENTRY.match(p)]
    if len(kept) == len(paras):
        m = HEADING.search("\n" + tail)
        return tail[max(0, m.start() - 1):] if m else ""
    return "\n\n".join(kept)


def clean(md):
    """Strip citation entries, control characters, and rewrap.

    From the first References heading on, every numbered paragraph with a year in it goes; prose,
    headings and legends stay. Cutting the list as a block was measured wrong both ways: to the
    end of the file it dropped 18% of one paper, legends included; to the next heading it kept
    all but two entries of another, because the parser puts a heading inside the list.
    """
    first = next(REF.finditer(md), None)
    if first:
        md = md[:first.start()] + "\n" + body(md[first.end():])
    md = CTRL.sub("", md)
    return "\n".join(l for ln in md.splitlines()           # a table row stays one line, however wide
                     for l in ([ln] if ln.lstrip().startswith("|") else textwrap.wrap(ln, 300, break_long_words=False) or [""]))


def convert(pdf, out, forced=None, equations=False, quiet=False):
    pages, raw_chars = pdf_stats(pdf)
    order = [forced] if forced else (["marker"] if equations else ["pymupdf4llm", "docling"])
    problems = {}
    for parser in order:
        md, err = parse(pdf, parser)
        if md is None:
            problems[parser] = err
            continue
        bad = validate(md, pages, raw_chars)
        if bad:
            # A parse that fails validation is never accepted, whatever its position in the order.
            # A bad parse looks like valid markdown; passing it downstream is the failure this
            # whole step exists to prevent.
            problems[parser] = "; ".join(bad)
            if not quiet:
                nxt = order[order.index(parser) + 1] if parser != order[-1] else None
                print(f"  {parser} failed validation ({problems[parser]})"
                      + (f" -> trying {nxt}" if nxt else " -> no parser left"), file=sys.stderr)
            continue
        text = clean(md)
        open(out, "w").write(text)
        if not quiet:
            print(f"{out}  {len(text):,} chars  ~{len(text) / 2.9:,.0f} tok  via {parser}")
        return True, []
    missing_fallback = any("venv missing" in str(v) or "No module named" in str(v)
                           for k, v in problems.items() if k == "docling")
    hint = ("  (the docling fallback is not installed: `lacuna doctor --install-fallback`)"
            if missing_fallback and not forced and not equations else "")
    print(f"ERROR {os.path.basename(pdf)}: no parser produced usable output.{hint} "
          + " | ".join(f"{k}: {v}" for k, v in problems.items()), file=sys.stderr)
    return False, list(problems)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    forced = None
    for f in flags:
        if f.startswith("--parser"):
            forced = f.split("=", 1)[1] if "=" in f else None
    if "--parser" in sys.argv:
        forced = sys.argv[sys.argv.index("--parser") + 1]
        args = [a for a in args if a != forced]
    ok = True
    for pdf, out in zip(args[0::2], args[1::2]):
        good, _ = convert(pdf, out, forced, "--equations" in flags, "--quiet" in flags)
        ok &= good
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
