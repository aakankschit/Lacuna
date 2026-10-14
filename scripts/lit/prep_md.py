"""PDF -> reference-stripped, line-wrapped markdown for agent reading, with validation and fallback.

Default parser is pymupdf4llm (~4 s/document, markdown headings and tables, no control characters).
If the parse fails validation, retry with docling (~73 s/document, 100% fact recall in the
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
REF = re.compile(r"\n#*\s*\**(References|Bibliography)\**\s*\n", re.I)
# Any heading. Used to find where the citation block ends, so material a journal places AFTER the
# references (acknowledgements, data availability, Extended Data legends) is not lost with them.
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
    try:
        out = subprocess.run([VENVS["pymupdf4llm"], "-I", "-c",
                              "import sys, pymupdf; print(pymupdf.open(sys.argv[1]).page_count)", pdf],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        return int(out.strip())
    except (ValueError, OSError):
        return 0


def parse(pdf, parser):
    py = VENVS.get(parser)
    if not py or not os.path.exists(py):
        return None, f"{parser}: venv missing ({py})"
    r = subprocess.run([py, "-I", "-c", SCRIPTS[parser], pdf], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        return None, f"{parser}: {(r.stderr or '')[-160:].strip()}"
    return r.stdout, None


def validate(md, pages):
    """Return a list of problems. Thresholds come from the 2026-10-13 bake-off across 3 papers."""
    bad = []
    if len(md) < 1200 * max(pages, 1) * 0.25:          # ~1.2k chars/page was the floor seen
        bad.append(f"only {len(md):,} chars for {pages} pages")
    if CTRL.search(md):
        bad.append(f"{len(CTRL.findall(md))} control characters")
    if pages >= 4 and len(re.findall(r"(?m)^#{1,4} \S", md)) == 0:
        bad.append("no markdown headings")
    if not re.search(r"(?i)(abstract|introduction|method|result)", md):
        bad.append("no recognisable section word")
    return bad


def clean(md):
    """Strip trailing reference lists, control characters, and rewrap.

    Only a reference list in the last part of the document is cut, and only the citation block
    itself. Journal formats put a second reference list before Methods, and put acknowledgements,
    data-availability notes and Extended Data legends *after* the references — cutting from the
    last heading to the end dropped 18% of one real paper, legends included.
    """
    # Only the LAST "References" heading is the real citation list; an earlier one is usually a
    # contents-page entry. Cut from there to the NEXT heading rather than to the end of the file:
    # journals put acknowledgements, data availability and Extended Data legends after the
    # citations, and cutting to the end dropped 18% of one real paper, legends included.
    found = list(REF.finditer(md))
    if found:
        last = found[-1]
        tail = md[last.end():]
        resume = HEADING.search(tail)
        md = md[:last.start()] + (tail[resume.start():] if resume else "")
    md = CTRL.sub("", md)
    return "\n".join(l for ln in md.splitlines()
                     for l in (textwrap.wrap(ln, 300, break_long_words=False) or [""]))


def convert(pdf, out, forced=None, equations=False, quiet=False):
    pages = page_count(pdf)
    order = [forced] if forced else (["marker"] if equations else ["pymupdf4llm", "docling"])
    problems = {}
    for parser in order:
        md, err = parse(pdf, parser)
        if md is None:
            problems[parser] = err
            continue
        bad = validate(md, pages)
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
