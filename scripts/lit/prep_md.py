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
# One citation entry. Measured on three journal papers, the list cannot be cut as a block: a
# "Publisher's Note" box lands between entries 2 and 3, Nature puts a second list before Methods,
# and acknowledgements and Extended Data legends follow the last. So each paragraph is judged.
# Directly under a reference-list heading, a numbered paragraph that carries a year is an entry.
ENTRY = re.compile(r"\s*(?:[-*]\s+)?(?:\(\d{1,4}\)|\[\d{1,4}\]|\d{1,4}[.)])\s.*\b(?:1[89]|20)\d{2}\b", re.S)
# Once another heading has passed, body text may have resumed, and a Methods step is a numbered
# paragraph with a year-like number too ("lysed in 2000 uL", "collected in 2019"): every one of them
# was deleted. From there a paragraph goes only if it has the shape of a citation — who wrote it
# (AUTHOR, at the start), or where it appeared (VENUE) — and a year.
NUMBER = re.compile(r"\s*(?:[-*]\s+)?(?:\(\d{1,4}\)|\[\d{1,4}\]|\d{1,4}[.)])\s+")
_Y = r"(?:1[89]|20)\d{2}[a-z]?"
YEAR = re.compile(rf"\b{_Y}\b")
_NAME = r"[A-ZÀ-ÖØ-Þ\u0100-\u024f][^\W\d_]*(?:['’-][^\W\d_]+)*"   # Smith, O'Brien, Hernández-López, Łukasz
_INIT = r"(?:[A-Z][a-z]?\.\s*-?\s*){1,4}"                            # J. / J. A. / J.-P. / Ch.
AUTHOR = re.compile(
    rf"(?:[a-z]{{1,4}}\s+){{0,2}}{_NAME}(?:\s+{_NAME}){{0,2}},\s*{_INIT}(?=[A-Z(&;,“\"]|et\b|and\b|$)"  # Smith, J. A.;
    rf"|{_INIT}{_NAME}(?=,?\s*(?:and\s+|&\s*)?{_INIT}[A-Z]|,?\s+et\b|,\s*[“\"])"  # J. A. Smith, B. Doe / J. Smith et al.
    rf"|{_NAME}(?:,?\s+{_INIT}|\s+[A-Z]{{1,3}})?,?\s+et\.?\s+al\b"     # Smith et al. / Smith JA, et al.
    rf"|{_NAME}\s+[A-Z]{{1,3}}(?:,\s+{_NAME}\s+[A-Z]{{1,3}})+(?:,\s+et al)?\."  # Smith JA, Doe R.
    rf"|{_NAME}(?:,|\s+and\s+{_NAME})?\s+\(?{_Y}\)?(?=[.;:]|\s*$)")   # Brown 2018. / Smith and Doe (2019)
_D = r"[-–—−]"
VENUE = re.compile(
    rf"\b\d{{1,4}}\s*,\s*[A-Za-z]?\d{{1,6}}(?:\s*{_D}\s*[A-Za-z]?\d{{1,6}})?\s*\(\s*{_Y}\s*\)"  # Nature 596, 583–589 (2021)
    rf"|{_Y}\s*,\s*\d{{1,4}}\s*,\s*[A-Za-z]?\d"                        # J. Am. Chem. Soc. 2020, 142, 1234
    rf"|{_Y}\s*;\s*\d{{1,4}}\s*(?:\([^)\n]{{1,9}}\))?\s*:\s*[A-Za-z]?\d"  # J Chem. 2020;12(3):45
    rf"|\b\d{{1,4}}\s*(?:\([^)\n]{{1,9}}\)\s*[:,]|:)\s*[A-Za-z]?\d{{1,6}}\s*{_D}\s*[A-Za-z]?\d"  # 581:1–5
    r"|\bdoi\s*:|\bdoi\.org/|\bpp\.\s*\d|\bvol\.\s*\d|\barXiv:\s*\d{4}\.\d{4}", re.I)
# Where an author-year list puts the year: "(2020)", ", 2020.", "2020;". Enough on its own only
# directly under a reference-list heading; a sentence can end ", 2019." too.
CITED_YEAR = re.compile(rf"[,.;:(]\s*{_Y}\)?(?=\s*[.,;:)]|\s*$)")
LEGEND = re.compile(r"[*_\s]*(?:Extended Data\s+|Supplementary\s+)?(?:Fig(?:ure)?|Table)\b", re.I)
# A heading in any markup a parser produces. pymupdf4llm writes a same-size bold heading as
# **Appendix A**, not as `#`, so a list cut to the next `#` swallowed the appendix after it.
HEADING = re.compile(r"#{1,6}\s+\S.*|(?:(?:\*\*|__|\*|_)[^*_\n]*[^\W\d_][^*_\n]*(?:\*\*|__|\*|_)\s*)+")
SECTION = re.compile(r"(?:Appendix|Appendices|Supplementary|Supporting Information|"
                     r"(?:Online |Materials and )?Methods|Acknowledg\w*|Data availability|Code availability|"
                     r"Author contributions|Competing interests|Extended Data)\b", re.I)
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


class Unmeasured(Exception):
    """pymupdf is there and could not count this PDF: evidence against the PDF."""


class NoPymupdf(Unmeasured):
    """pymupdf cannot be imported for the default parser: nothing could count any PDF."""


def pdf_stats(pdf):
    """(pages, characters of raw extractable text) straight from pymupdf. Raises NoPymupdf when it
    is not installed, Unmeasured (with the cause) when it could not read this PDF.

    The character count is the thing that makes truncation detectable. A fixed "chars per page"
    threshold cannot: a dense paper truncated to a third of its text still clears any floor low
    enough not to reject a sparse one. Comparing a parse against the document's OWN text is the
    only check that works for both.
    """
    py = VENVS["pymupdf4llm"]
    if not os.path.exists(py):
        raise NoPymupdf(f"no default parser venv at {py}")
    try:
        r = subprocess.run(
            [py, "-I", "-c",
             "import sys\n"
             "try:\n    import pymupdf\nexcept ImportError:\n    print('no pymupdf')\n    sys.exit()\n"
             "d = pymupdf.open(sys.argv[1])\n"
             "print(d.page_count, sum(len(p.get_text()) for p in d))", pdf],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise NoPymupdf(f"{py} would not run: {e}")
    if r.stdout.strip() == "no pymupdf":
        raise NoPymupdf(f"{py} has no pymupdf")
    try:
        pages, chars = r.stdout.split()
        return int(pages), int(chars)
    except ValueError:
        err = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        raise Unmeasured(err[-1].strip() if err else f"pymupdf exited {r.returncode} and printed nothing")


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
# A font with no Unicode map extracts as U+FFFD or Private Use code points (docling writes such a
# glyph as GLYPH<c=…,font=…>). Headings set in another font stay readable, so every other check
# passed a parse that was 99.8% unmapped glyphs and a reader was paid to read it. Letters and
# digits of any script count as readable — Chinese, Cyrillic, Greek, math italic, a table of
# numbers — and symbols count for neither side, so equations do not trip it.
JUNK = re.compile(r"[\ufffd\ue000-\uf8ff\U000f0000-\U0010ffff]|GLYPH<[^>\n]{0,80}>")
JUNK_SHARE = 0.20


def unreadable(text):
    """What is wrong when `text` is mostly unmapped glyphs, else None. split_book asks too."""
    n_junk = len(JUNK.findall(text))
    if n_junk <= 50:
        return None
    readable = sum(c.isalnum() for c in JUNK.sub("", text))
    if n_junk > JUNK_SHARE * (n_junk + readable):
        return (f"{n_junk:,} unreadable glyphs to {readable:,} letters and digits — the PDF's fonts "
                f"have no Unicode map, so the text is not text")
    return None


# With nothing to count the PDF's pages, the floor cannot scale with them; what is left is a floor
# no whole paper falls under. It does not catch one page of twenty — only pymupdf's count does.
UNMEASURED_FLOOR = 2000


def validate(md, pages, raw_chars=0, measured=True):
    """Return a list of problems. `raw_chars` is the PDF's own extractable character count;
    `measured` is False when nothing could count the PDF at all."""
    bad = []
    if not measured and len(md) < UNMEASURED_FLOOR:
        bad.append(f"only {len(md):,} chars, and with no pymupdf to count the PDF's pages there is no "
                   f"telling that this is all of it")
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
    junk = unreadable(md)
    if junk:
        bad.append(junk)
    if pages >= 4 and len(re.findall(r"(?m)^#{1,4} \S", md)) == 0:
        bad.append("no markdown headings")
    if not re.search(r"(?i)(abstract|introduction|method|result)", md):
        bad.append("no recognisable section word")
    return bad


def heading(p):
    """Whether a paragraph opens with a heading in any markup a parser produces: `#`, a line wholly
    in bold or italics, a line in capitals, or a bare section name ("Appendix A", "Methods")."""
    line = p.strip().split("\n", 1)[0].strip()
    if not line or len(line) > 120:
        return False
    words = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", line)
    letters = [c for c in words if c.isalpha()]
    return bool(HEADING.fullmatch(line) or HEADING.fullmatch(words)
                or (len(letters) >= 4 and all(c.isupper() for c in letters) and not re.search(r"\d", words))
                or (len(line) <= 100 and SECTION.match(words)))


def citation(p):
    """Whether a numbered paragraph has the shape of a reference: a year, and an author list at the
    start or a journal's volume and pages. A Methods step has a number and a year-like number too."""
    m = NUMBER.match(p)
    rest = p[m.end():] if m else ""
    return bool(m and YEAR.search(rest) and (AUTHOR.match(rest) or VENUE.search(rest)))


def author_year(p):
    """An unnumbered entry of an author-year list: a year, where a citation puts one."""
    q = p.strip()
    return bool(YEAR.search(q) and not LEGEND.match(q)
                and (AUTHOR.match(q) or CITED_YEAR.search(q) or VENUE.search(q)))


def body(tail):
    """Everything after a References heading, minus the citation entries.

    Under a reference-list heading, a numbered paragraph with a year goes, and so does an unnumbered
    one with its year where a citation puts it (an author-year list). Any other heading may mean
    body text has resumed — Methods, an appendix in bold — so after one, only a numbered paragraph
    shaped like a citation goes, until the next reference-list heading.
    """
    kept, in_list = [], True
    for p in re.split(r"\n\s*\n", tail):
        if REF.fullmatch("\n" + p.strip() + "\n"):
            in_list = True                 # a second list, as Nature puts before Methods
            continue
        if heading(p):
            in_list = False
        elif citation(p) or in_list and (ENTRY.match(p) or not NUMBER.match(p) and author_year(p)):
            continue
        kept.append(p)
    return "\n\n".join(kept)


def clean(md, strip_refs=True):
    """Strip citation entries, control characters, and rewrap.

    From the first References heading on, citation entries go one paragraph at a time; prose,
    headings and legends stay. Cutting the list as a block was measured wrong both ways: to the
    end of the file it dropped 18% of one paper, legends included; to the next heading it kept
    all but two entries of another, because the parser puts a heading inside the list.
    """
    first = next(REF.finditer(md), None)
    if first and strip_refs:
        md = md[:first.start()] + "\n" + body(md[first.end():])
    md = CTRL.sub("", md)
    return "\n".join(l for ln in md.splitlines()           # a table row stays one line, however wide
                     for l in ([ln] if ln.lstrip().startswith("|") else textwrap.wrap(ln, 300, break_long_words=False) or [""]))


# References are a minority of a paper's text, a review's included. A cut of more than half has
# taken body text with it; so has one that leaves a text validate() would refuse. Either way the
# references stay in: they cost a reader tokens, where a lost Methods section costs the paper.
STRIP_LIMIT = 0.50


def finish(md, pages, measured=True):
    """(clean(md), None), or the text with its references kept and why, when stripping them
    removed an implausible share of the parse or left one that no longer validates."""
    text = clean(md)
    cut = 1 - len(text) / max(len(md), 1)
    bad = validate(text, pages, 0, measured)
    if cut > STRIP_LIMIT or bad:
        return clean(md, strip_refs=False), (f"stripping references would remove {cut:.0%} of the text"
                                             + (f" and leave {'; '.join(bad)}" if bad else ""))
    return text, None


def convert(pdf, out, forced=None, equations=False, quiet=False):
    measured, unmeasured = True, None
    try:
        pages, raw_chars = pdf_stats(pdf)
    except NoPymupdf as e:
        # `doctor --install-equations` makes the marker venv and nothing else, and --equations then
        # runs prep_md without the default one. That is a choice, not evidence against this PDF: the
        # parse goes ahead under the checks that need no page count, and says the rest are off.
        print(f"  {os.path.basename(pdf)}: {e}, so nothing can count the PDF's pages: the truncation "
              f"check is off for this parse (`lacuna doctor --install-pdf` adds it)", file=sys.stderr)
        pages, raw_chars, measured = 0, 0, False
    except Unmeasured as e:
        # pymupdf is there and could not read this PDF: evidence, refused below.
        pages, raw_chars, unmeasured = 0, 0, str(e)
    order = [forced] if forced else (["marker"] if equations else ["pymupdf4llm", "docling"])
    problems = {}
    for parser in order:
        md, err = parse(pdf, parser)
        if md is None:
            problems[parser] = err
            continue
        bad = validate(md, pages, raw_chars, measured)
        if unmeasured:
            # Without the PDF's own counts the truncation check is off, and one page of a 20-page
            # paper passed as the whole paper. A check that cannot run is a check that fails.
            bad.append(f"could not count the PDF's own pages and characters ({unmeasured}), so a "
                       f"truncated parse cannot be told from a whole one")
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
        text, refs_kept = finish(md, pages, measured)
        if refs_kept and not quiet:
            print(f"  {parser}: {refs_kept} -> falling back to the text with its references kept",
                  file=sys.stderr)
        # Write aside, then rename: the pipeline treats a file at `out` as a finished parse, so a
        # write cut short (a kill, a full disk) must never leave one there.
        with open(out + ".partial", "w") as f:
            f.write(text)
        os.replace(out + ".partial", out)
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
