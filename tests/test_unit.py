"""Unit level: one function, in process, with nothing on disk but a temp dir.

What belongs here is logic whose wrong implementation is a pure function of its inputs: price
lookup, config layering, parse validation and cleaning, transcript arithmetic, the content check
on a model's output, and the shipped state of the repo. Anything that needs its own process (a
CLI, a hook, the pipeline) belongs in test_modules.py or test_pipeline.py. See TESTING.md.

Run: python3 -m unittest discover -s tests
"""
import io, json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT, read, write
import lacuna_config, ccost, prep_md, split_book
import lit_pipeline as L


class TestPrices(unittest.TestCase):
    """A price table matched by substring is one typo away from charging Haiku rates for Opus."""

    def test_shipped_table_and_lookup(self):
        prices = lacuna_config.prices()
        self.assertGreaterEqual(len(prices), 5)
        for model, (inp, w5, w1, rd, out) in prices.items():
            self.assertLess(rd, inp, f"{model}: a cache read must be cheaper than fresh input")
            self.assertLessEqual(w5, w1, f"{model}: a 1h write costs at least a 5m write")
            self.assertGreater(out, inp, f"{model}: output costs more than input")
        self.assertEqual(list(prices), sorted(prices, key=lambda k: -len(k)),
                         "longest key first, or 'opus-5' swallows 'opus-5-5': a 5x error")
        self.assertNotEqual(ccost.price("claude-opus-5-5-20260101"), ccost.price("claude-opus-5-20260101"))
        self.assertEqual(ccost.price("sonnet"), prices["sonnet-5-5"],
                         "an alias must price as the version it resolves to, not as the fallback")
        self.assertTrue(all(x > 0 for x in ccost.price("some-model-we-have-never-heard-of")),
                        "an unknown model must fall back to a non-zero price, never $0")

    def test_user_pricing_layers_over_the_shipped_table(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(lacuna_config, "DATA", d):
            write(os.path.join(d, "pricing.json"),
                  json.dumps({"_retrieved": "2099-01-01", "models": {"haiku-5-5": [9, 9, 9, 9, 9]}}))
            self.assertEqual(lacuna_config.prices()["haiku-5-5"], (9.0,) * 5)
            self.assertIn("opus-5-5", lacuna_config.prices(), "an override layers over the table, not replaces it")
            self.assertEqual(lacuna_config.price_meta(), {"retrieved": "2099-01-01", "overridden": True})
            for doc in ([["haiku-5-5", 1, 1, 1, 1, 1]], {"models": [1, 2]}, "prices",    # hand-edited, wrongly
                        {"models": {"haiku-5-5": "12345"}},                  # a string is not five prices
                        {"models": {"haiku-5-5": [float("nan"), 1, 1, 1, 1]}}):   # priced every call at $nan
                with self.subTest(doc):
                    write(os.path.join(d, "pricing.json"), json.dumps(doc))
                    self.assertIn("opus-5-5", lacuna_config.prices(), "a malformed override must not take the table down")
                    self.assertFalse(lacuna_config.price_meta()["overridden"])


class TestConfig(unittest.TestCase):
    """Environment beats file beats default. Getting this wrong means silently using the wrong model."""

    def load(self, cfg=None, raw=None, env=None):
        """(effective config, CONFIG_ERRORS, SOURCES) for a config.json, with no LACUNA_* leaking in."""
        with tempfile.TemporaryDirectory() as d:
            if cfg is not None or raw is not None:
                write(os.path.join(d, "config.json"), raw if raw is not None else json.dumps(cfg))
            clean = {k: v for k, v in os.environ.items() if not k.startswith("LACUNA_")}
            with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True), \
                    mock.patch.object(lacuna_config, "DATA", d):
                return lacuna_config.load(), list(lacuna_config.CONFIG_ERRORS), dict(lacuna_config.SOURCES)

    def test_precedence_and_partial_layering(self):
        cfg, _, _ = self.load()
        self.assertEqual(cfg["models"]["planner"], "claude-opus-5")
        cfg, _, src = self.load({"models": {"planner": "from-file"}, "after": {"synth": "~/bin/x"},
                                 "paths": {"cache": "/from/file"}})
        self.assertEqual(cfg["models"]["planner"], "from-file")
        self.assertEqual(cfg["models"]["reader"], "haiku", "a partial section must not wipe the other roles")
        self.assertEqual(cfg["after"], {"read": "", "synth": "~/bin/x", "verify": ""})
        self.assertEqual(cfg["paths"]["ledger"], lacuna_config.DEFAULTS["paths"]["ledger"])
        self.assertEqual(src[("models", "planner")], "config.json")
        cfg, _, src = self.load({"models": {"planner": "from-file"}, "paths": {"cache": "/from/file"}},
                                env={"LACUNA_PLANNER": "from-env", "LACUNA_CACHE": "/from/env"})
        self.assertEqual(cfg["models"]["planner"], "from-env")
        self.assertEqual(cfg["paths"]["cache"], "/from/env")
        self.assertEqual(src[("models", "planner")], "$LACUNA_PLANNER")

    def test_paths_expand_tilde_and_variables(self):
        """config.json is edited by hand, so "~/books" must not become a directory named "~"."""
        cfg, _, _ = self.load({"paths": {"books": "~/mybooks", "cache": "$HOME/mycache"}})
        self.assertEqual(cfg["paths"]["books"], os.path.expanduser("~/mybooks"))
        self.assertEqual(cfg["paths"]["cache"], os.path.join(os.environ["HOME"], "mycache"))

    def test_a_broken_file_degrades_to_defaults_and_is_reported(self):
        """Every script imports this module, so a typo in config.json must not take the tool down."""
        cfg, errors, _ = self.load(raw="{ this is not json")
        self.assertEqual(cfg["models"]["planner"], "claude-opus-5")
        self.assertTrue(errors and "not valid JSON" in errors[0], errors)
        for section in ("paths", "models", "effort"):
            cfg, _, _ = self.load({section: None})
            self.assertEqual(cfg[section], lacuna_config.DEFAULTS[section], f"a null {section} must leave the defaults")
        cfg, errors, _ = self.load({"models": "haiku"})
        self.assertEqual(cfg["models"], lacuna_config.DEFAULTS["models"])
        self.assertTrue(errors, "a wrong-typed section is ignored and said so")
        cfg, _, _ = self.load({"effort": {"reader": None}})
        with mock.patch.object(lacuna_config, "CONFIG", cfg):
            self.assertEqual(lacuna_config.effort("reader"), "medium", "a null effort must not reach the cache key")


class TestParse(unittest.TestCase):
    """A bad parse looks like valid markdown. Validation is the only thing that catches it, and
    reference stripping is where a careless cut loses the legends a paper puts after its citations."""

    GOOD = ("# A Paper\n\n## Abstract\n\n" + "word " * 500 + "\n\n## Introduction\n\n" + "word " * 500
            + "\n\n## Methods\n\n" + "word " * 500 + "\n\n## Results\n\n" + "word " * 500)
    BAD = "# Title\n\nshort"   # 14 characters: the truncated parse that looked fine

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lacuna-unit-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_validate_rejects_each_kind_of_bad_parse(self):
        """The length check compares against the PDF's own text: a dense paper cut to 37% clears
        any fixed per-page floor low enough to accept a sparse one."""
        full = "# A Paper\n\n## Abstract\n\n" + ("real sentence of body text. " * 1200)
        raw = len(full)
        self.assertEqual(prep_md.validate(full, 14, raw), [])
        cases = {
            "truncated to 37%": (full[:int(raw * .37)], 14, raw, "truncated"),
            "truncated to 10%": (full[:int(raw * .10)], 14, raw, "truncated"),
            "short, no baseline": ("# A Paper\n\n## Abstract\n\nShort.\n\n## Introduction\n\n" + "word " * 200, 20, 0, "chars"),
            "mostly scanned": ("# A Paper\n\n## Abstract\n\n" + "word " * 420, 12, 2130, "chars"),
            "control-character flood": (self.GOOD + "\x01" * 60, 4, 0, "control"),
            "no headings": (self.GOOD.replace("#", ""), 4, 0, "heading"),
            "no section word": ("# A Title\n\n## Something\n\n" + "word " * 4000, 4, 0, "section"),
        }
        # A font with no Unicode map: readable headings over a body of unmapped glyphs, as long as
        # the PDF's own count (which includes them). It passed, and a reader was paid to read it.
        for name, glyph in (("U+FFFD", "\ufffd"), ("Private Use", "\ue012"), ("docling", "GLYPH<c=3,font=/AB+Minion> ")):
            md = "".join(f"## {h}\n\n" + (glyph * 60 + " ") * 40 + "\n\n" for h in ("Abstract", "Introduction", "Methods", "Results"))
            cases[f"unmapped glyphs ({name})"] = (md, 12, len(md), "unreadable")
        for name, (md, pages, raw_chars, word) in cases.items():
            with self.subTest(name):
                bad = prep_md.validate(md, pages, raw_chars)
                self.assertTrue(any(word in b for b in bad), f"{name}: {bad}")
        # ...and the other direction: other scripts are letters, and digits, symbols, equations and
        # a Word bullet font in Private Use are not a reason to refuse a paper.
        bullets = "\uf0b7 " * 60
        readable = {
            "Chinese": "本文研究了蛋白质结构预测的方法，并在三个数据集上进行了评估。" * 40 + bullets,
            "Russian": "Мы исследуем метод выборки и оцениваем его на трёх наборах данных. " * 30 + bullets,
            "equations": ("We minimise $\\mathcal{L}(\\theta) = \\sum_i \\| y_i - f_\\theta(x_i) \\|^2$ over 𝜃 ∈ ℝ^d, "
                          "so ∇𝓛 = 0 at 1.5 × 10^-3; \uf061\uf062 ≤ ∑ ∫ ∂ ± √. ") * 30,
            "numeric tables": "| 0.123 | 0.456 | 0.789 | 1.234 | 5.678 |\n" * 200 + bullets,
        }
        for name, text in readable.items():
            with self.subTest(name):
                md = "".join(f"## {h}\n\n{text}\n\n" for h in ("Abstract", "Introduction", "Methods", "Results"))
                self.assertEqual(prep_md.validate(md, 4, len(md)), [], name)
        self.assertEqual(prep_md.validate(self.GOOD + "\x01\x02\x03", 4), [],
                         "a handful of control characters is cosmetic (clean() strips them); rejecting "
                         "a 75k-char parse over one stray byte made --equations exit 3")

    def test_reference_entries_are_stripped_and_what_follows_is_kept(self):
        """Measured on three journal papers: the parser puts a heading inside the list, Nature puts
        a second list before Methods, and legends follow the citations. So entries go one by one."""
        refs = "\n\n(1) Smith, J. Title. J. Chem. 2020, 1, 2.\n\n2. Jones, A. et al (2019) Other.\n\n"
        after = ("### **Publisher's Note**\n\nneutral\n\n3. Brown 2018.\n\n### Acknowledgements\n\nThanks.\n\n"
                 "**Extended Data Fig. 1 |** legend\n")
        for heading in ("# ■<sup>**REFERENCES**</sup>", "## ■ REFERENCES", "**References**", "### **References**",
                        "## 5. References", "Bibliography", "References and Notes", "## Notes and references",
                        "## REFERENCES CITED", "## Works Cited", "**Literature Cited**", "## Reference", "### Reference List",
                        "## List of References"):
            with self.subTest(heading):
                out = prep_md.clean("# P\n\n## Methods\n\n1. Dock, 2020 style.\n\nbody\n\n" + heading + refs + after)
                for gone in ("Smith", "Jones", "Brown"):
                    self.assertNotIn(gone, out)
                for kept in ("Dock, 2020 style", "body", "neutral", "Thanks", "legend"):
                    self.assertIn(kept, out)
        out = prep_md.clean("# P\n\nbody\n\n## References\n\nSmith, J. (2020). T.\n\nJones, A. (2019). O.\n\n## Appendix\n\nkept\n")
        self.assertNotIn("Smith", out, "an author-year list has no numbers to spot: cut it to the next heading")
        self.assertIn("kept", out)
        out = prep_md.clean("# P\n\nWe list all the references\nbelow.\n\nmore body\n")
        self.assertIn("more body", out, "the word at the end of a prose line is not a heading")
        for heading in ("## References to prior work", "## Literature review", "## Literature", "## Notes",
                        "## Reference genomes", "Cited works"):
            with self.subTest(heading):
                out = prep_md.clean(f"# P\n\n{heading}\n\n1. Smith, J. Title. J. Chem. 12, 1–5 (2019).\n\n"
                                    "Earlier work (Smith, 2019) showed it.\n")
                self.assertIn("Smith, J. Title", out, f"{heading!r} is a section, not a reference list")
                self.assertIn("Earlier work", out)

    def test_what_follows_a_reference_list_is_judged_by_shape(self):
        """Every numbered paragraph with a year-like number after the first References heading was
        deleted: Methods steps ("lysed in 2000 uL"), and, after an author-year list, everything up to
        the next `#` heading, so an appendix headed in bold went too. Entries go by the shape of a
        citation; any heading may end the list; and real entries after one still go."""
        fill = "Ordinary prose about the method and its evaluation. " * 6
        cases = {
            "Methods after references": (
                "## References\n\n1. Jumper, J. et al. Highly accurate protein structure prediction. Nature 596, 583–589 (2021).\n\n"
                "2. Senior, A. W., Evans, R. & Jumper, J. Improved prediction. Nature 577, 706–710 (2020).\n\n"
                "## Methods\n\n1. Cells were incubated at 37 C and lysed in 2000 uL of buffer.\n\n"
                "2. Spectra were recorded between 1800 and 2100 cm-1.\n\n3) Samples collected in 2019 were split.\n\n"
                "4. Cells (2 × 10^5, 96-well plates) were cultured as before (Smith et al., 2019).\n\n"
                "51. Mirdita, M. et al. ColabFold. Nat. Methods 19, 679–682 (2022).\n\n",   # Methods references
                ["Senior, A. W.", "Mirdita"], ["2000 uL", "1800 and 2100", "collected in 2019", "Smith et al., 2019"]),
            "bold appendix after an author-year list": (
                "## References\n\nSmith J (2020) A study. Nature 581:1-5.\n\n"
                "Tom Brown, Benjamin Mann, et al. Language models are few-shot learners. In NeurIPS, 2020.\n\n"
                "Diederik P Kingma and Jimmy Ba. Adam: a method for stochastic optimization. In ICLR, 2015.\n\n"
                "**Appendix A. Derivation of the update rule**\n\n"
                "The update follows Kingma and Ba (2015) with step size eta = 0.01.\n\n",
                ["Smith J (2020)", "few-shot", "Adam: a method"], ["Appendix A", "eta = 0.01"]),
            "entries after a stray heading": (
                "## References\n\n1. Smith, J. A. Title. J. Chem. 12, 1–5 (2019).\n\n## Nature | Vol 596\n\n"
                "2. Doe, R.; Roe, P. Title. J. Am. Chem. Soc. 2020, 142, 1234−1240.\n\n"
                "[3] A. Vaswani, N. Shazeer, and N. Parmar, “Attention is all you need,” in Proc. NeurIPS, 2017, pp. 5998–6008.\n\n"
                "4. Smith JA, Doe R, Roe P. A trial. N Engl J Med. 2020;382(5):411-420.\n\n"
                "5. The UniProt Consortium. UniProt in 2021. Nucleic Acids Res. 49, D480–D489 (2021).\n\n",
                ["Smith, J. A.", "Doe, R.;", "Vaswani", "Smith JA", "UniProt"], ["Nature | Vol 596"]),
        }
        for name, (tail, gone, kept) in cases.items():
            with self.subTest(name):
                out = prep_md.clean(f"# P\n\n## Abstract\n\n{fill}\n\n" + tail)
                for g in gone:
                    self.assertNotIn(g, out)
                for k in kept + ["Ordinary prose"]:
                    self.assertIn(k, out)
        out = prep_md.clean("# Annual report\n\nContents\n\nSummary\n\nFindings\n\nReferences\n\n"
                            f"## Summary\n\n{fill}\n\n## Findings\n\n1. By 2030 the programme must double its enrolment.\n\n"
                            "2. In 2024, 41% of sites reported a shortage.\n\n## References\n\n1. Agency X. Report 2023.\n")
        self.assertIn("By 2030", out, "a contents list naming References is not a reference list")
        self.assertIn("In 2024, 41%", out)
        self.assertNotIn("Agency X", out, "the real list at the end still goes")

    def test_cleaning_is_validated_and_keeps_references_rather_than_body(self):
        """validate() judged only the raw parse. A cut that takes more than references can be, or
        that leaves a text validate() would refuse, keeps the references instead: they cost tokens,
        a lost section costs the paper."""
        refs = "## References\n\n" + "".join(f"{i}. Smith, J. Title {i}. Nature {i}, 1–5 (2020).\n\n" for i in range(1, 60))
        for name, md, pages in (
                ("most of the parse", "# P\n\n## Abstract\n\n" + "word " * 300 + "\n\n" + refs, 1),
                ("under the floor once cut", "# P\n\n## Abstract\n\n" + "word " * 200 + "\n\n" + refs[:900], 4)):
            with self.subTest(name):
                self.assertEqual(prep_md.validate(md, pages), [], "the fixture must pass as parsed")
                text, why = prep_md.finish(md, pages)
                self.assertIn("Smith, J. Title 1.", text, "the references stay")
                self.assertIn("stripping references would remove", why)
        text, why = prep_md.finish(self.GOOD + "\n\n" + refs[:900], 4)
        self.assertIsNone(why)
        self.assertNotIn("Smith, J.", text, "an ordinary paper is still stripped")
        md = "# P\n\n## Abstract\n\n" + "word " * 300 + "\n\n" + refs
        self.convert({"pymupdf4llm": md}, pages=1, quiet=False)
        self.assertIn("falling back", self.stderr, "lit_pipeline relays only lines that say so")

    def test_control_characters_removed_and_long_lines_wrapped(self):
        out = prep_md.clean("# P\n\n" + ("abcdefghi " * 100) + "\x01\x02\n")
        self.assertNotRegex(out, r"[\x00-\x08\x0b-\x1f\x7f]")
        self.assertLessEqual(max(len(l) for l in out.splitlines()), 300)
        self.assertEqual(out.count("abcdefghi"), 100, "wrapping must not drop words")
        row = "| " + " | ".join(f"cell{i}" for i in range(60)) + " |"
        self.assertIn(row, prep_md.clean("# P\n\n" + row + "\n").splitlines(), "a table row is one line however wide")

    def test_parse_version_is_bumped_when_parsing_changes(self):
        """The parse cache is keyed on PARSE_VERSION. A change to validate(), clean() or their
        patterns that leaves it alone never reaches an already-parsed document (it happened once).
        When this fails: bump PARSE_VERSION in lit_pipeline.py and record the new fingerprint here."""
        import hashlib, inspect
        parts = [inspect.getsource(f) for f in (prep_md.validate, prep_md.unreadable, prep_md.clean, prep_md.body,
                                                 prep_md.heading, prep_md.citation, prep_md.author_year, prep_md.finish)]
        parts += [r.pattern for r in (prep_md.REF, prep_md.ENTRY, prep_md.NUMBER, prep_md.YEAR, prep_md.AUTHOR, prep_md.VENUE,
                                      prep_md.CITED_YEAR, prep_md.LEGEND, prep_md.HEADING, prep_md.SECTION, prep_md.CTRL,
                                      prep_md.JUNK)]
        parts += [str(prep_md.KEEP_FRACTION), str(prep_md.CTRL_BUDGET), str(prep_md.JUNK_SHARE), str(prep_md.STRIP_LIMIT),
                  str(prep_md.UNMEASURED_FLOOR)]
        fingerprint = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
        recorded = {"4": "e837b51bbd46d754"}
        self.assertEqual(recorded.get(L.PARSE_VERSION), fingerprint,
                         f"the parser changed: bump PARSE_VERSION and record {fingerprint!r} for it")

    def test_parse_errors_keep_the_cause_not_the_traceback(self):
        tb = ('Traceback (most recent call last):\n  File "<string>", line 1, in <module>\n'
              '    d = pymupdf.open(sys.argv[1])\n        ~~~~~~~~~~~~^^^^^^^^^^^^^^\n'
              'pymupdf.FileDataError: Failed to open file: not a PDF\n')
        fake = mock.Mock(returncode=1, stderr=tb, stdout="")
        with mock.patch.object(prep_md.subprocess, "run", return_value=fake), \
                mock.patch.object(prep_md, "VENVS", {"pymupdf4llm": sys.executable}):
            md, err = prep_md.parse("x.pdf", "pymupdf4llm")
        self.assertIsNone(md)
        self.assertIn("Failed to open file", err)
        self.assertNotIn("File \"", err)
        self.assertNotIn("^^^", err, "indented source and caret lines are noise")

    def convert(self, outputs, pages=4, stats_error=None, quiet=True, **kw):
        """Run convert() with parsers stubbed: (ok, parsers called, text written or None); what it
        said on stderr is left in self.stderr. `stats_error` is what pdf_stats() raises."""
        called, out = [], os.path.join(self.tmp, "out.md")
        if os.path.exists(out):
            os.remove(out)

        def fake_parse(pdf, parser):
            called.append(parser)
            md = outputs.get(parser)
            return (md, None) if md is not None else (None, f"{parser}: venv missing")

        def fake_stats(pdf):
            if stats_error:
                raise stats_error
            return pages, 0
        err = io.StringIO()
        try:
            with mock.patch.object(prep_md, "parse", fake_parse), mock.patch.object(prep_md, "pdf_stats", fake_stats), \
                    mock.patch("sys.stderr", err), mock.patch("sys.stdout"):
                ok, _ = prep_md.convert("x.pdf", out, quiet=quiet, **kw)
        finally:
            self.stderr = err.getvalue()
        return ok, called, (read(out) if os.path.exists(out) else None)

    def test_parser_order_fallback_and_refusal(self):
        ok, called, _ = self.convert({"pymupdf4llm": self.GOOD, "docling": self.GOOD})
        self.assertTrue(ok)
        self.assertEqual(called, ["pymupdf4llm"], "docling is ~15x slower; it must not run needlessly")
        ok, called, text = self.convert({"pymupdf4llm": self.BAD, "docling": self.GOOD.replace("A Paper", "Docling")})
        self.assertTrue(ok)
        self.assertEqual(called, ["pymupdf4llm", "docling"])
        self.assertIn("Docling", text)
        _, called, _ = self.convert({"marker": self.GOOD}, equations=True)
        self.assertEqual(called, ["marker"])
        _, called, _ = self.convert({"docling": self.GOOD}, forced="docling", equations=True)
        self.assertEqual(called, ["docling"], "--parser must beat --equations")
        for name, outputs, kw in [("none available", {}, {}),
                                  ("all invalid", {"pymupdf4llm": self.BAD, "docling": self.BAD}, {}),
                                  ("forced invalid", {"docling": self.BAD}, {"forced": "docling"}),
                                  ("equations invalid", {"marker": self.BAD}, {"equations": True}),
                                  # pymupdf could not open the PDF to count it: one page of twenty
                                  # passed as the whole paper, with the truncation check silently off
                                  ("PDF not measurable", {"pymupdf4llm": self.GOOD, "docling": self.GOOD},
                                   {"stats_error": prep_md.Unmeasured("pymupdf.FileDataError: cannot open")})]:
            with self.subTest(name):
                ok, _, text = self.convert(outputs, pages=20, **kw)
                self.assertFalse(ok, f"{name}: a parse that failed validation was accepted")
                self.assertIsNone(text, f"{name}: the bad parse was written")
                if "stats_error" in kw:
                    self.assertIn("could not count the PDF's own pages", self.stderr, "and it says why")
        # ...but with no pymupdf installed at all nothing can count it: `doctor --install-equations`
        # makes only the marker venv, and --equations must still work there, saying the check is off,
        # under the checks that need no page count.
        none = prep_md.NoPymupdf("no default parser venv at /x")
        ok, _, text = self.convert({"marker": self.GOOD}, equations=True, stats_error=none)
        self.assertTrue(ok, self.stderr)
        self.assertIn("truncation check is off", self.stderr)
        one_page = "# A Paper\n\n## Abstract\n\n" + "word " * 280
        ok, _, text = self.convert({"marker": one_page}, equations=True, stats_error=none)
        self.assertFalse(ok, "a page of text is not a paper when nothing can say how long the PDF is")
        self.assertIn("no pymupdf to count", self.stderr)

    def test_pdf_stats_tells_a_missing_pymupdf_from_a_pdf_it_cannot_read(self):
        """The first is a setup (only the marker venv); the second is evidence against the PDF."""
        def stats(returncode=0, stdout="", stderr="", venv=sys.executable):
            run = mock.Mock(return_value=mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr))
            with mock.patch.object(prep_md.subprocess, "run", run), mock.patch.dict(prep_md.VENVS, {"pymupdf4llm": venv}):
                return prep_md.pdf_stats("x.pdf")
        self.assertEqual(stats(stdout="12 34567\n"), (12, 34567))
        for name, kw in (("no venv", {"venv": os.path.join(self.tmp, "no-venv", "python")}),
                         ("venv without pymupdf", {"stdout": "no pymupdf\n"})):
            with self.subTest(name), self.assertRaises(prep_md.NoPymupdf):
                stats(**kw)
        with self.assertRaises(prep_md.Unmeasured) as cm:
            stats(returncode=1, stderr="Traceback ...\npymupdf.FileDataError: Failed to open file 'x.pdf'.\n")
        self.assertNotIsInstance(cm.exception, prep_md.NoPymupdf)
        self.assertIn("FileDataError", str(cm.exception))
        # A write that dies part-way (here, on a character it cannot encode; in life, a kill or a full
        # disk) must leave no file: the pipeline serves whatever is at that path as a finished parse.
        with self.assertRaises(UnicodeEncodeError):
            self.convert({"pymupdf4llm": self.GOOD + "\ud800"})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out.md")), "a partial parse was left in the cache")


class TestBookPlan(unittest.TestCase):
    """Which pages go in which chapter file. A page in no file is a page no reader can find."""

    def chapters(self, toc, pages):
        with mock.patch("sys.stderr"):
            return split_book.plan(toc, pages)

    def test_every_page_lands_in_one_chapter(self):
        """Split at level 2, a book lost its level-1 List of Symbols and filed its Appendix under
        the last chapter; pages before the first bookmark were dropped at any level."""
        toc = [[1, "List of Symbols", 1], [1, "Part I", 5]] + [[2, f"Chapter {c}", 5 + (c - 1) * 19] for c in range(1, 6)]
        toc += [[1, "Part II", 100]] + [[2, f"Chapter {c}", 100 + (c - 6) * 18] for c in range(6, 11)]
        toc += [[1, "Appendix A", 191]]
        sections = toc[:-1] + [[1, "Appendix A", 191], [2, "A.1 Integrals", 195]]
        for name, book, pages, first, last in (
                ("level 2 beside level-1 leaves", toc, 200, ("List of Symbols", 1, 4), ("Appendix A", 191, 200)),
                ("a level-1 appendix with sections", sections, 200, ("List of Symbols", 1, 4), ("A.1 Integrals", 195, 200)),
                ("first bookmark on page 15", [[1, "Chapter 1", 15], [1, "Chapter 2", 40]], 60,
                 ("Before the first bookmark", 1, 14), ("Chapter 2", 40, 60))):
            with self.subTest(name):
                level, chapters = self.chapters(book, pages)
                self.assertEqual((chapters[0], chapters[-1]), (first, last))
                covered = [p for _, a, b in chapters for p in range(a, b + 1)]
                self.assertEqual(covered, list(range(1, pages + 1)), "every page once, in order")
        level, chapters = self.chapters(toc, 200)
        self.assertEqual(level, 2)
        self.assertIn(("Chapter 10", 172, 190), chapters, "the appendix is not filed under the last chapter")
        self.assertNotIn("Part I", [t for t, _, _ in chapters], "a Part that starts with its first chapter is not a chapter")
        self.assertIn(("Appendix A", 191, 194), self.chapters(sections, 200)[1], "its pages before A.1 are its own")

    def test_titles_are_one_clean_line(self):
        """A NUL in a bookmark title went into toc.md and its chapter's first line, and `rg` then
        skipped the chapter as binary."""
        self.assertEqual(split_book.one_line("Chapter 1\x00"), "Chapter 1")
        self.assertEqual(split_book.one_line("Chapter 2\rKernels\n\tand priors"), "Chapter 2 Kernels and priors")
        self.assertEqual(split_book.one_line(None), "")


class TestPipelineHelpers(unittest.TestCase):
    """What the synthesiser is told exists, which books a reader is offered, and whether a model's
    output counts as output."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="lacuna-repo-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def touch(self, rel, text=""):
        return write(os.path.join(self.repo, rel), text)

    def test_walk_files_skips_vcs_caches_build_and_hidden(self):
        for rel in ("README.md", "src/a.py", "docs/b.md", ".git/HEAD", "node_modules/x/i.js",
                    "__pycache__/a.pyc", "build/o.o", "dist/w.whl", ".venv/bin/python", ".hidden/s", ".env"):
            self.touch(rel)
        self.assertEqual(L.walk_files(self.repo), ["README.md", "docs/b.md", "src/a.py"])

    def test_find_status_order_and_explicit_path(self):
        self.assertIsNone(L.find_status(self.repo))
        for rel in ["NOTES.md", "docs/STATUS.md", "STATUS.md", "research/STATUS.md"]:   # lowest first
            self.touch(rel, "# Status\ndead end: X\n")
            self.assertEqual(L.find_status(self.repo), os.path.join(self.repo, rel), f"{rel} wins once it exists")
        mine = self.touch("elsewhere/MINE.md")
        self.assertEqual(L.find_status(self.repo, mine), mine)
        self.assertIsNone(L.find_status(self.repo, os.path.join(self.repo, "typo.md")))

    def test_not_content_refuses_what_cannot_be_read_and_passes_real_text(self):
        def check(data, **kw):
            return L.not_content(write(os.path.join(self.repo, "f.txt"), data) if isinstance(data, str)
                                 else self._raw(data), **kw)
        for label, data in [("empty", ""), ("whitespace", " \n\t\n "), ("NUL byte", b"text\0text" * 50),
                            ("all 256 byte values", bytes(range(256)) * 20),
                            ("mostly control characters", "\x01\x02\x03\x04 ok" * 100)]:
            with self.subTest(label):
                self.assertTrue(check(data), f"{label} is not content")
        # Both directions: odd but real text must pass.
        for label, data in [("accents", "caf\u00e9 na\u00efve r\u00e9sum\u00e9 \u00fcber " * 40),
                            ("CJK", "\u81ea\u7136\u8a00\u8a9e\u51e6\u7406\u306e\u7814\u7a76 " * 40),
                            ("emoji and math", "\u2200x \u2208 \u211d: x\u00b2 \u2265 0 \U0001f600 " * 40),
                            ("form feeds and tabs", "page one\n\f\npage two\t\ttabbed\r\n" * 40),
                            ("a few stray controls", "A real paragraph about sampling. " * 60 + "\x1b[0m\x07"),
                            ("latin-1 bytes in prose", ("Une \u00e9tude sur l'eau. " * 60).encode("latin-1")),
                            ("one line", "# T\n")]:
            with self.subTest(label):
                self.assertIsNone(check(data), f"{label} is content")
        self.assertIn("characters", check("hi\n", min_chars=400))
        self.assertIsNone(check("x" * 400, min_chars=400))
        self.assertIn("cannot be read", L.not_content(os.path.join(self.repo, "nope.txt")))

    def _raw(self, data):
        path = os.path.join(self.repo, "raw.bin")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_books_block_offers_only_books_with_a_toc(self):
        books = os.path.join(self.repo, "books")
        write(os.path.join(books, "zeta", "toc.md"), "# Zeta")
        write(os.path.join(books, "alpha", "toc.md"), "# Alpha")
        write(os.path.join(books, "unsplit", "00_ch.txt"), "text")
        with mock.patch.object(L, "BOOKS_DIR", books):
            block = L.books_block()
        self.assertEqual([l for l in block.splitlines() if l.startswith("- index: ")],
                         [f"- index: {os.path.join(books, d, 'toc.md')}" for d in ("alpha", "zeta")])
        with mock.patch.object(L, "BOOKS_DIR", os.path.join(self.repo, "no-such-dir")):
            self.assertEqual(L.books_block(), "")

    def test_usable_output_judges_content_not_size(self):
        """A blocked synthesiser wrote a fluent "BLOCKED — not completed" document that cleared the
        size check, exited 0 and fired the user's hook. But a paid note must not be thrown away
        over an ordinary word either."""
        p = os.path.join(self.repo, "out.md")
        with mock.patch("sys.stderr"):
            for opening in ("# Plan\n\nBLOCKED — not completed. I could not read any of the notes.",
                            "I cannot help with this request.",
                            "# Result\n\nUnable to read the supplied directory, so no plan follows.",
                            "# Plan\n\nBLOCKED: the notes directory is not readable."):
                write(p, opening + "\n\n" + ("padding text " * 200))
                self.assertGreater(os.path.getsize(p), 800, "fixture must clear the size check")
                self.assertFalse(L.usable_output(p, 800, "plan"), f"{opening[:40]!r} must not count as output")
            write(p, "# Development plan\n\n## 1. Add a --json flag\n\n" + ("real content " * 200))
            self.assertTrue(L.usable_output(p, 800, "plan"))
            write(p, "# Blocked Gibbs sampling (Smith 2024)\n\n## Claim\nBlocked updates converge faster.\n\n"
                     "## Key results\n" + ("Finding, 42 units (Table 2). " * 60))
            self.assertTrue(L.usable_output(p, 400, "note"), "ordinary prose is not a refusal")
            write(p, "# Follow-up in a cohort (Lee 2025)\n\n## Claim\nOf 412 patients, 37 had not completed follow-up "
                     "(Table 2), and the authors were unable to read 5 records.\n\n## Key results\n" + ("Finding. " * 80))
            self.assertTrue(L.usable_output(p, 400, "note", must_contain=("## Claim",)),
                            "an apology's words inside a section are a finding: the note was paid for, keep it")
            write(p, "# Plan\n")
            self.assertFalse(L.usable_output(p, 800, "plan"))


class TestTranscriptMoney(unittest.TestCase):
    """Cost comes from an internal file format. Deduplication and the split must stay correct."""

    def test_dedupes_and_prices_the_split(self):
        u = {"input_tokens": 100, "output_tokens": 1000, "cache_read_input_tokens": 50_000,
             "cache_creation_input_tokens": 10_000}
        rows = [
            {"type": "assistant", "timestamp": "2026-10-13T00:00:00Z",
             "message": {"id": "m1", "model": "claude-haiku-5-5", "content": [], "usage": u}},
            {"type": "assistant", "timestamp": "2026-10-13T00:00:01Z",   # a streaming duplicate
             "message": {"id": "m1", "model": "claude-haiku-5-5", "content": [], "usage": u}},
            {"type": "user", "message": {"content": "ignored"}},
            {"type": "assistant", "timestamp": "2026-10-13T00:00:02Z",
             "message": {"id": "m2", "model": "claude-haiku-5-5", "content": [{"type": "tool_use", "name": "Read"}],
                         "usage": {"input_tokens": 0, "output_tokens": 10, "cache_read_input_tokens": 60_000,
                                   "cache_creation_input_tokens": 0}}},
            "{ not json at all",
            [1, 2], {"type": "assistant", "message": "a string"},      # valid JSON, not a call: skipped
        ]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                f.write("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
            calls = ccost.load(p)
        self.assertEqual(len(calls), 2, "duplicate message ids must collapse to one call")
        self.assertEqual(calls[1]["tools"], ["Read"])
        split, ctx = ccost.split(calls[0])
        self.assertEqual(ctx, 100 + 10_000 + 50_000)
        # haiku-5-5: 0.10 input, 0.125 5m write, 0.20 1h write, 0.01 read, 0.50 output per MTok
        self.assertAlmostEqual(split["read"], 50_000 * 0.01 / 1e6, places=9)
        self.assertAlmostEqual(split["output"], 1_000 * 0.50 / 1e6, places=9)
        self.assertAlmostEqual(split["write1h"], 10_000 * 0.20 / 1e6, places=9,
                               msg="a write with no TTL breakdown is priced as the dearer 1h write")

    def test_avoidable_cold_rewrites_are_judged_against_the_call_before(self):
        """Every case here was miscounted once. A fixed "reads under 40k" missed idle re-caches
        that still read Claude Code's ~42k warm prefix (9 of them, $39.66, on one machine). A
        <synthetic> API-error line counted as a session's first call, so the real first write was
        called avoidable. The fix must not count a compaction's rewrite or a big new file."""
        t = lambda m: f"2026-10-13T{m // 60:02d}:{m % 60:02d}:00Z"
        u = lambda w, r: {"input_tokens": 5, "output_tokens": 500, "cache_creation_input_tokens": w,
                          "cache_read_input_tokens": r}
        row = lambda mid, minute, w, r, model="claude-opus-5-5", **kw: {
            "type": "assistant", "timestamp": t(minute), **kw, "message": {"id": mid, "model": model, "usage": u(w, r)}}
        rows = [row("err", 0, 0, 0), *[]]
        rows[0]["message"]["model"] = "<synthetic>"
        rows += [
            row("first", 1, 650_000, 40_000),        # the session's first write: the price of starting
            row("warm", 3, 2_000, 690_000),
            row("side", 4, 60_000, 0, isSidechain=True),   # a subagent's thread: neither judged nor "previous"
            row("idle", 130, 649_000, 42_500),       # 2 h idle: re-cached, reading only the warm prefix
            row("compact", 131, 60_000, 42_000),     # a minute later: a compaction's new context, not idle
            row("grow", 140, 100_000, 100_000),      # a large new file, read on a warm cache
            row("cold", 300, 150_000, 0),            # the case the old rule did catch
            row("warm2", 302, 1_000, 150_000),
            row("switch", 320, 140_000, 42_000, model="claude-sonnet-5-5"),   # /model, 18 min on: not idle
            row("tokens", 322, 1_000, 182_000, output_tokens=None),          # a null count is 0
        ]
        rows[-1]["message"]["usage"]["output_tokens"] = None
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in rows))
            calls = ccost.load(p)
        self.assertEqual([c["id"] for c in calls][0], "first", "an API error is not a call")
        cold = sorted(calls[i]["id"] for i in ccost.avoidable_colds(calls))
        self.assertEqual(cold, ["cold", "idle"])

    def test_a_small_context_is_judged_by_the_same_rule_as_a_large_one(self):
        """VERDICT3 N3: a session whose second call reads all of the first call's context and writes
        a big tool result 45 s later is the cache working, not an idle re-cache; and a re-cache after
        an idle hour counts even when the whole context is under 40k. Fixed thresholds got both wrong."""
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
        O, S = "claude-opus-5-5", "claude-sonnet-5-5"

        def row(mid, after, write, read, model=O):
            return {"type": "assistant", "timestamp": (base + after).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "message": {"id": mid, "model": model, "usage": {
                        "input_tokens": 5, "output_tokens": 500, "cache_creation_input_tokens": write,
                        "cache_read_input_tokens": read}}}
        td = lambda **k: timedelta(**k)
        cases = {   # name: (rows, ids that are avoidable cold rewrites)
            "warm: reads all of the previous 11,117, writes 43,903, 45 s later":
                ([row("a", td(), 11_117, 0), row("b", td(seconds=45), 43_903, 11_115)], []),
            "cache invalidated 30 s on is not idle":
                ([row("a", td(), 100_000, 0), row("b", td(seconds=30), 101_000, 0)], []),
            "a /model switch ten minutes on is not idle":
                ([row("a", td(), 100_000, 0), row("b", td(minutes=10), 101_000, 0, S)], []),
            "idle two hours, a 50k context re-written":
                ([row("a", td(), 50_000, 0), row("b", td(hours=2), 52_000, 0)], ["b"]),
            "idle three hours on a context that was under 40k":
                ([row("a", td(), 30_000, 0), row("b", td(minutes=1), 2_000, 30_000), row("c", td(hours=3), 45_000, 0)], ["c"]),
            "a ten-minute tool run outlives the five-minute cache":
                ([row("a", td(), 80_000, 0), row("b", td(minutes=10), 82_000, 0)], ["b"]),
            "the first write of a session is the price of starting":
                ([row("a", td(), 60_000, 0)], []),
        }
        for name, (rows, want) in cases.items():
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as d:
                    p = os.path.join(d, "t.jsonl")
                    with open(p, "w") as f:
                        f.write("".join(json.dumps(r) + "\n" for r in rows))
                    calls = ccost.load(p)
                self.assertEqual(sorted(calls[i]["id"] for i in ccost.avoidable_colds(calls)), want)

    def test_refused_attempts_are_priced_apart_from_the_call(self):
        """The top-level usage is the last attempt only; earlier ones sit in usage.iterations, each
        with its own model. Their cost is reported as the top of a range, never added in."""
        last = {"input_tokens": 2, "output_tokens": 271, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 65_700, "cache_creation": {"ephemeral_1h_input_tokens": 65_700}}
        first = {"input_tokens": 17, "output_tokens": 0, "cache_read_input_tokens": 30_741,
                 "cache_creation_input_tokens": 35_036, "model": "claude-fable-5-1",
                 "cache_creation": {"ephemeral_1h_input_tokens": 35_036}}
        c = {"type": "assistant", "timestamp": "2026-10-13T00:00:00Z",
             "message": {"id": "m", "model": "claude-opus-5", "usage": {**last, "iterations": [first, last]}}}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps(c) + "\n")
            call, = ccost.load(p)
        # fable-5-1: 10 input, 20 1h write, 0.25 read per MTok
        self.assertAlmostEqual(ccost.earlier_usd(call), (17 * 10 + 35_036 * 20 + 30_741 * 0.25) / 1e6, places=9)
        self.assertAlmostEqual(sum(ccost.split(call)[0].values()), (2 * 5 + 65_700 * 10 + 271 * 25) / 1e6, places=9,
                               msg="the call itself is still priced from the top-level usage, at its own model")


class TestShippedState(unittest.TestCase):
    """What a stranger gets from a clean clone. The example notes were once excluded by a
    .gitignore rule for days while working perfectly in the author's checkout."""

    def test_plugin_manifests_and_cli_are_valid(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as f:
            plugin = json.load(f)
        self.assertEqual(plugin["name"], "lacuna")
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as f:
            market = json.load(f)
        self.assertTrue(market["owner"]["name"])
        self.assertEqual([p["name"] for p in market["plugins"]], ["lacuna"])
        cli = os.path.join(ROOT, "bin", "lacuna")
        self.assertTrue(os.access(cli, os.X_OK), "bin/lacuna must be executable")
        self.assertIn("lacuna", subprocess.run([cli, "--help"], capture_output=True, text=True).stdout)
        for argv in (["not-a-command"], ["lit", "not-a-subcommand"]):
            self.assertEqual(subprocess.run([cli, *argv], capture_output=True).returncode, 2, argv)

    def test_no_absolute_user_paths_shipped(self):
        """One hardcoded home directory in a shipped file ends a reader's evaluation.

        Matches an ABSOLUTE path only: the slash must not follow a word character, so a relative
        path that merely contains the word (`v1/home/lacuna`, `scripts/home/x`) is not a finding.
        """
        absolute_home = re.compile(r"(?<![\w.~])/(?:Users|home)/\w")
        bad = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "tests"}]
            for fn in filenames:
                if fn.endswith((".md", ".py", ".json", ".sh")) or fn in ("lacuna", "ccsplit"):
                    with open(os.path.join(dirpath, fn), errors="ignore") as f:
                        bad += [f"{os.path.relpath(dirpath, ROOT)}/{fn}:{i}" for i, line in enumerate(f, 1)
                                if absolute_home.search(line)]
        self.assertEqual(bad, [], f"absolute home paths found: {bad}")

    def test_example_notes_exist_and_are_not_ignored(self):
        notes = os.path.join(ROOT, "examples", "notes")
        files = [f for f in os.listdir(notes) if f.endswith(".md")]
        self.assertGreaterEqual(len(files), 3, f"expected the shipped example notes, found {files}")
        for f in files:
            self.assertGreater(os.path.getsize(os.path.join(notes, f)), 400, f)
        if shutil.which("git") and os.path.isdir(os.path.join(ROOT, ".git")):
            # --no-index tests the ignore RULES, not whether the files happen to be tracked already
            r = subprocess.run(["git", "-C", ROOT, "check-ignore", "--no-index", os.path.join(notes, files[0])],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, "a .gitignore rule excludes examples/notes from a fresh clone")



    def test_a_standalone_status_line_prices_every_shipped_model(self):
        """The documented install copies statusline.py on its own, where scripts/pricing.json is out
        of reach, so its inline table is all it has. It lacked haiku-4-5: such a turn showed
        $2.200 for $0.600."""
        import importlib.util
        with tempfile.TemporaryDirectory() as d:
            alone = shutil.copy(os.path.join(ROOT, "statusline", "statusline.py"), d)
            spec = importlib.util.spec_from_file_location("statusline_alone", alone)
            mod = importlib.util.module_from_spec(spec)
            with mock.patch.dict(os.environ, {"LACUNA_DATA": d}):
                spec.loader.exec_module(mod)
        with open(os.path.join(ROOT, "scripts", "pricing.json")) as f:
            shipped = {k: tuple(map(float, v)) for k, v in json.load(f)["models"].items()}
        self.assertEqual({k: tuple(map(float, v)) for k, v in mod.PRICE.items()}, shipped)


def tier(text, reply=False):
    """What the content check does with `text`: "fail" (exit 4, nothing filed), "warn" (filed, with a
    warning) or None (filed)."""
    found = L.judge(text, reply=reply)
    return found[0] if found else None


class TestRefusalPosition(unittest.TestCase):
    """An apology is recognised by POSITION and SCOPE, not presence.

    Defects were shipped here in both directions, four times. A phrase list discarded correct notes
    ("37 had not completed follow-up"); looking only before the first heading let an apology under
    `## Status` be filed; and a grammar that failed a stage on any caveat at the opening discarded
    45 paid results ("I could not access the Supporting Information"). Only the agent saying it
    could not do the job as a whole fails a stage now; a caveat about a part, or an apology below
    the opening, is kept with a warning.
    """

    def test_an_apology_is_caught_wherever_a_block_opens_with_it(self):
        for text, label in [
            ("# Development plan\n\n## Status\n\nNot completed. I could not read any of the notes.",
             "under a heading, after a short sentence"),
            ("# Plan\n\n- Not completed: I could not read any of the notes.", "as a list item, after a label"),
            ("# Plan\n\n1. I cannot help with that request.", "as a numbered item"),
            ("I could not read any of the supplied files.\n\npadding", "bare opening"),
            ("BLOCKED - not completed.\n\nmore text", "a bare status marker"),
            ("# Digest\n\n> I am unable to summarise documents I could not open.", "in a blockquote"),
            # What a real sonnet digester filed, exit 0, when it could read none of the notes: honest
            # about having nothing, and phrased so that no apology marker opens a block.
            ("# PARTIAL: literature notes unreadable\n\n## Status of this digest\n\nThis digest contains no "
             "literature claims. Every note in the directory returned a permission error.", "a self-declared partial"),
            ("INCOMPLETE: the notes directory could not be read.", "the agents' own failure line"),
            ("`INCOMPLETE: the notes could not be read`", "echoing the prompt's backticks"),
            ("> [!warning] PARTIAL: two of three notes unreadable", "in a callout"),
            ("Status: INCOMPLETE — no literature was readable", "behind a Status: label"),
            ("# Development plan — next iteration (PARTIAL / BLOCKED)\n\n## Read this first", "a real planner's title"),
            # VERDICT3 N6: each of these was filed, exit 0, with the hook fired.
            ("# Development plan\n\n> [!WARNING]\n> PARTIAL: literature notes unreadable", "a callout tag on its own line"),
            ("---\ntitle: Plan\ntags: [lit]\n---\n\nPARTIAL: literature notes unreadable", "after YAML frontmatter"),
            ("```\nINCOMPLETE: the notes could not be read\n```", "in a code fence"),
            ("# Development plan\n\nI couldn't open any of the literature notes, so no proposals follow.", "couldn't open"),
            ("# Development plan\n\nI was unable to access the notes, so what follows is generic.", "was unable to access"),
            ("# A note (x)\n\n## Claim\nI\u2019m sorry, but I can\u2019t help with summarising this paper.",
             "I'm sorry, but, curly apostrophes, under a heading with no blank line"),
            ("Unfortunately, I was not able to read the paper: every page is symbol noise.", "was not able to"),
            ("# Plan\n\nI don't have access to the notes, so no plan follows.", "no access to the whole input"),
            ("Both notes are binary garbage. I could not read either of them.", "after one short sentence"),
            ("Unfortunately,\nI could not read any of the notes.", "wrapped over two lines"),
            ("Sorry, unfortunately, I could not read any of the notes.", "two lead-ins"),
            ("No files were found at the given path.", "a verifier that resolved nothing"),
            ("I could not check any claims because the document is not in the codebase.\n- `runs/real/plan.md` -> not found.",
             "a real verifier's reply, its finding below the apology"),
            # r2-content E6, E8: filed, exit 0.
            ("# Development plan\n\n_INCOMPLETE: none of the notes could be read._", "underscore emphasis"),
            ("# Development plan\n\n_I could not read any of the notes, so no proposals follow._", "an underscored apology"),
            ("**Status:** _BLOCKED_ by unreadable notes", "an underscored marker behind a bold label"),
            ("# Paper (n.d.)\n\n## Claim\nAs an AI, I cannot open PDF files directly.", "as an AI"),
            ("# Development plan\n\nI don't have the ability to open these notes; they appear to be binary.",
             "no ability to"),
            ("# Paper (n.d.)\n\n## Claim\nThe document could not be read; it contains only symbol noise.",
             "the whole input, as a passive subject"),
        ]:
            with self.subTest(label):
                self.assertEqual(tier(text), "fail", f"{label} must fail the stage")

    def test_a_doubt_is_kept_with_a_warning(self):
        """The shapes a program cannot tell from a finding. Each was a paid result discarded (r2-content
        E1-E3), or a failure filed in silence (E5): the user reads the warning and decides."""
        for text, label in [
            ("1. Incomplete: every note returned EACCES.", "a status word in title case"),
            ("# Partial 3D Shape Matching for Virtual Screening (Kumar 2022)\n\n## Claim", "a real title, E1"),
            ("# Gatekeepers (Zhang 2019)\n\n## Claim\nBlocked by a gatekeeper mutation, the pocket admits only "
             "type II inhibitors (Fig. 3).", "a real claim, E1"),
            ("# Development plan\n\nScope note. I could not access the GitHub repository cited in batch_effects.md, "
             "so proposal 4's baseline is the note's own figure.", "a caveat about a part, E2"),
            ("# Weighted cycle closure (Li 2023)\n\n> **Note:** I could not access the Supporting Information, so "
             "Table S1-S4 values are not included.\n\n## Claim", "the Supporting Information, E2"),
            ("> [!NOTE]\n> I don't have access to the notes directory.", "a directory of the input"),
            ("# A note (x)\n\n## Claim\nI could not read Table 3 (the scan is blurred); the rest shows X improves Y "
             "by 12% (Section 4).", "a numbered part"),
            ("# A note (Vaswani 2017)\n\n## Claim\nUnable to process long sequences in parallel, recurrent models are "
             "slow to train (Section 1).", "an inability with no subject, about no input"),
            ("# Digest\n\nThe three papers agree that warm starts cut sampler burn-in by about 40% on the benchmarks "
             "they share (Table 2). I could not read the supplementary tables, so per-benchmark figures are omitted.",
             "a caveat after a long first sentence of content"),
            ("# Plan\n\n- **BLOCKED** until the CUDA build lands: port the scorer", "a status tag on a proposal"),
            ("# Plan\n\n- BLOCKED by unreadable notes: no proposals can be made.", "the same shape, failing"),
            ("# Plan\n\nThree proposals follow.\n\n- INCOMPLETE: data for site B (Table 3)", "a marker below the opening"),
            ("# Plan\n\nThree proposals follow.\n- INCOMPLETE: data for site B (Table 3)", "on the next list item"),
            ("# Development plan\n\n**Objective:** What should we build next?\n\n## Status\n\nI could not read any of "
             "the notes, so no proposals follow.", "an apology below the opening, E5"),
            ("# Paper (n.d.)\n\n## Claim\nI'm sorry, but the provided file appears to contain only garbled symbols.",
             "an apology about nothing it could not do"),
        ]:
            with self.subTest(label):
                self.assertEqual(tier(text), "warn", f"{label} is kept, with a warning")

    def test_the_same_words_inside_a_sentence_are_findings(self):
        for text, label in [
            ("# Note\n\n## Claim\n\nOf 412 patients, 37 had not completed follow-up (Table 2).",
             "clinical finding"),
            ("# Note\n\n## Claim\n\nThe solver cannot complete in polynomial time.", "complexity"),
            ("# Verify\n\n- JobState.BLOCKED -> not found in the repo", "BLOCKED in a finding"),
            ("# Plan\n\n## 1. A real proposal\n\nSubstantive content naming src/widget.py.", "a plan"),
            ("# PARTIAL LEAST SQUARES for batch correction\n\n## 1. Replace PCA", "a title in capitals"),
            ("# Digest\n\n## Partial results\n\nTwo of three trials were incomplete: see Table 2.", "partial as a word"),
            ('- PARTIAL: "54-69/100" -> the note gives 54-69 for ROCS only', "a verify finding graded PARTIAL"),
            ("# INCOMPLETE-DATA METHODS (Rubin 1976)\n\n## Claim", "a hyphenated title"),
            ("# Development plan (PARTIAL LEAST SQUARES)\n\n## 1. Replace PCA", "a bracketed title"),
            ("# BLOCKED GIBBS SAMPLING (Smith 2020)\n\n## Claim", "a title in capitals"),
            # VERDICT3 N10a: findings that open with the words, each discarded after it was paid for.
            ("# A note (Lee 2025)\n\n## Claim\n\nNot completed follow-up in 37 of 412 patients biased the estimate.",
             "a Claim opening 'Not completed follow-up'"),
            ("# A note (Lee 2025)\n\n## Claim\nNot completed: follow-up for 37 of 412 patients, which biases the "
             "hazard ratio upward (Table 2).", "'Not completed:' as a finding's label"),
            ("No files were found matching `src/ghost.py` -> the repo has src/a.py only", "a verify finding"),
            ("I could not verify the following claims:\n\n- `0.83` -> not in any note", "a verify report's first line"),
            ("# A note (Lee 2025)\n\n## Claim\n\nWe could not complete follow-up for 37 of 412 patients, which biases "
             "the estimate (Table 2).", "the paper's authors, as 'we'"),
        ]:
            with self.subTest(label):
                self.assertIsNone(tier(text), f"{label} must be filed without a word")


class TestFixesThatSurvivedTheirOwnRevert(unittest.TestCase):
    """Four fixes were shipped with no test: reverting any of them left the suite green.

    Their tests existed, in docs/reviews/, outside tests/ — so nothing ever ran them. A test that
    is not discovered is not a test. The other three now live with what they guard: the
    <synthetic> skip in TestCacheGuard, casefold and islink in the ownership tests.
    """

    def test_lacuna_data_is_expanded(self):
        """A "~" in LACUNA_DATA made a literal ./~/ beside the cwd while config was read elsewhere."""
        import subprocess
        out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "lacuna_config.py"), "--json"],
                             capture_output=True, text=True,
                             env={**os.environ, "LACUNA_DATA": "~/lacuna-expand-check"}).stdout
        data = json.loads(out)["data_dir"]
        self.assertNotIn("~", data, "LACUNA_DATA must be expanded, not taken literally")
        self.assertTrue(data.startswith(os.path.expanduser("~")), data)


class TestOwnershipRecord(unittest.TestCase):

    def test_a_hand_edited_or_old_record_is_read_without_raising(self):
        """The record is a file in the user's folder: hand-edited, or left by an older Lacuna. Only
        a well-formed entry with a checksum describes a file Lacuna wrote."""
        with tempfile.TemporaryDirectory() as d:
            note = write(os.path.join(d, "a.md"), "note")
            for label, record, want in [
                ("an entry that is not a table", {"files": {"a.md": 5, "b.md": {"sha256": "x"}}}, ["b.md"]),
                ("no files table", {}, []),
                ("a files table that is a list", {"files": ["a.md"]}, []),
                ("a source that is not a path", {"files": {"a.md": {"sha256": "x", "source": 5}}}, ["a.md"]),
                # 0.3.1 kept reservations here, released only on success; one left by a failed run
                # refused every later read of a same-named document, and made its note "edited since"
                ("a 0.3.1 reservation", {"files": {"a.md": {"pending": True, "source": "/x/a.md"},
                                                   "b.md": {"pending": True, "sha256": "x", "source": "/x/b.md"}}},
                 ["b.md"]),
            ]:
                with self.subTest(label):
                    write(os.path.join(d, ".lacuna.json"), json.dumps(record))
                    files, _ = L._owners(d)
                    self.assertEqual(sorted(files), want)
                    self.assertFalse(any("pending" in e for e in files.values()), "a reservation holds nothing now")
                    L.not_ours(note, source=os.path.join(d, "doc.md"))

    def test_recording_never_raises_and_a_moved_file_leaves_the_record(self):
        """record() runs after the stage has been paid for: a record that cannot be written costs a
        question on the next run, never this run's output. A file an after.* hook moved away is no
        longer Lacuna's to account for."""
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            note = write(os.path.join(d, "n.md"), "note")
            if os.geteuid() != 0:
                os.chmod(d, 0o555)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    L.record(note)
                os.chmod(d, 0o755)
                self.assertIn("could not record", err.getvalue())
            L.record(note)
            before = {L.name_key(note): L._sha(note)}
            os.remove(note)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                L.rerecord(d, before)
            self.assertEqual((L._owners(d)[0], err.getvalue()), ({}, ""))

    def test_two_processes_recording_into_one_directory_lose_nothing(self):
        """Two `lit read` runs into one --out kept 9 of 16 entries: each read the record, added its
        own and replaced the file. The next run refused the other 7 notes as not Lacuna's."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + '/lit')\n"
                "import lit_pipeline as L\n"
                "for i in range(150):\n"
                "    p = f'{sys.argv[2]}/{sys.argv[3]}{i}.md'; open(p, 'w').write(str(i)); L.record(p)\n")
        with tempfile.TemporaryDirectory() as d:
            procs = [subprocess.Popen([sys.executable, "-c", code, os.path.join(ROOT, "scripts"), d, tag],
                                      env={**os.environ, "LACUNA_DATA": d}) for tag in ("a", "b")]
            for p in procs:
                p.wait()
            with open(os.path.join(d, ".lacuna.json")) as f:
                self.assertEqual(len(json.load(f)["files"]), 300)


class TestMutationGate(unittest.TestCase):
    """At 0.2.0 six mutant anchors had gone stale after fixes moved the code. The runner listed
    them among the survivors, so a broken gate read as six test gaps, and nobody re-anchored them."""

    def test_a_stale_anchor_stops_the_gate_before_anything_runs(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import mutate
        stale = ("moved-code", "scripts/lit/lit_pipeline.py", "text that is nowhere in the file", "x")
        with mock.patch.object(mutate, "MUTANTS", [stale]), \
                mock.patch.object(mutate, "run_one", side_effect=AssertionError("a mutant ran")), \
                mock.patch.object(sys, "argv", ["mutate.py"]), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(mutate.main(), 2, "a stale anchor must fail the gate, distinctly from a survivor")
        self.assertIn("moved-code", err.getvalue())
        self.assertIn("anchor occurs 0 times", err.getvalue())
        # The independent battery printed "caught 16/16 applied mutations", exit 0, with two skipped.
        import mutation_battery
        with mock.patch.object(mutation_battery, "MUTATIONS", [stale]), \
                mock.patch.object(mutation_battery, "run_suite", return_value=(True, ["OK"])), \
                mock.patch.object(sys, "argv", ["mutation_battery.py", ROOT]), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            self.assertEqual(mutation_battery.main(), 2, "a battery that tested nothing must not pass")
        self.assertIn("FAILED: 1 of 1 could not be applied", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMalformedExternalData(unittest.TestCase):
    """Two defensive fixes shipped without tests: the project's own runner caught both as
    surviving mutants. Transcripts and pricing.json are written by other software and by hand;
    neither may take a tool down, and neither may be accepted as valid when it is not."""

    def test_statusline_ignores_a_price_row_that_is_not_numbers(self):
        """A row like ["four", 5, 8, 0.2, 20] must be skipped, not turned into a bogus price."""
        sys.path.insert(0, os.path.join(ROOT, "statusline"))
        import importlib, statusline
        importlib.reload(statusline)
        good = statusline._price_table({"models": {"opus-5-5": [4, 5, 8, 0.2, 20]}})
        self.assertEqual(good["opus-5-5"], (4.0, 5.0, 8.0, 0.2, 20.0))
        for bad in ({"models": {"opus-5-5": ["four", 5, 8, 0.2, 20]}},
                    {"models": {"opus-5-5": [None, 5, 8, 0.2, 20]}},
                    {"models": {"opus-5-5": [{"a": 1}, 5, 8, 0.2, 20]}}):
            with self.subTest(bad=str(bad)[:40]):
                self.assertEqual(statusline._price_table(bad), {},
                                 "a non-numeric price row must be skipped, not coerced")

    def test_a_null_token_count_in_a_transcript_is_zero_not_a_crash(self):
        """Claude Code has written null counts. ccost.load is the entry point for every cost
        tool, so a crash here takes down spend, the status line and the ledger at once."""
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        sys.path.insert(0, os.path.join(ROOT, "scripts", "cost"))
        import ccost
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-14T00:00:00Z",
                                    "message": {"id": "m1", "model": "claude-haiku-5-5",
                                                "usage": {"input_tokens": None, "output_tokens": 10,
                                                          "cache_read_input_tokens": None,
                                                          "cache_creation_input_tokens": 5}}}) + "\n")
            calls = ccost.load(p)
            self.assertEqual(len(calls), 1)
            split, ctx = ccost.split(calls[0])          # must not raise
            self.assertEqual(ctx, 5)
            self.assertGreater(split["output"], 0)


class TestWrongShapedTranscripts(unittest.TestCase):
    """Transcripts are written by other software. Each guard below survived its own revert at 0.3.1,
    and the status line carries its own copy of every one (it is self-contained by design), so each
    case runs through ccost and through statusline."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "scripts", "cost"))
        sys.path.insert(0, os.path.join(ROOT, "statusline"))
        import importlib, statusline
        cls.sl = importlib.reload(statusline)

    def rows(self, *rows):
        d = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "t.jsonl")
        with open(p, "w") as f:
            f.write("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))
        return p

    @staticmethod
    def line(mid="m1", model="claude-opus-5-5", usage=None, **kw):
        u = {"input_tokens": 10, "output_tokens": 1000, "cache_read_input_tokens": 100_000,
             "cache_creation_input_tokens": 1000}
        u.update(usage or {})
        return {"type": "assistant", "timestamp": "2026-10-14T00:00:00Z",
                "message": {"id": mid, "model": model, "usage": u, **kw}}

    def test_ccost_load_skips_or_zeroes_what_is_not_the_expected_shape(self):
        good = self.line("good")
        odd = self.line("odd")
        odd["message"]["usage"]["iterations"] = [3, {"output_tokens": 2}, {"output_tokens": 1}]
        odd["message"]["usage"]["cache_creation"] = "1h"
        cases = {
            "usage is a list": ([{**self.line("a"), "message": {"id": "a", "model": "m", "usage": [1]}}, good], ["good"]),
            "iterations hold a non-object": ([odd], ["odd"]),
            "string, bool and NaN counts": ([self.line("c", usage={"output_tokens": "1000", "input_tokens": True,
                                                                      "cache_read_input_tokens": float("nan")})], ["c"]),
            "model is a number": ([self.line("d", model=5)], ["d"]),
            "content is a number": ([self.line("e", content=7)], ["e"]),
            "timestamp is a number": ([{**self.line("f"), "timestamp": 1760400000}], ["f"]),
        }
        for name, (rows, want) in cases.items():
            with self.subTest(name):
                calls = ccost.load(self.rows(*rows))
                self.assertEqual([c["id"] for c in calls], want)
                for c in calls:
                    ccost.split(c), ccost.earlier_usd(c), ccost.ts(c["ts"]), ccost.short(c["model"])   # none may raise
        c = ccost.load(self.rows(self.line("c", usage={"output_tokens": "1000"})))[0]
        self.assertEqual(ccost.split(c)[0]["output"], 0, "a string count is 0, not 1000 and not a crash")
        self.assertEqual(ccost.ts("yesterday"), 0)
        self.assertEqual(ccost.ts(None), 0)
        self.assertEqual(ccost.ts(1760400000), 0, "a numeric timestamp is no timestamp, and must not raise")

    def test_the_status_line_reads_the_same_shapes_the_same_way(self):
        sl = self.sl
        self.assertEqual([sl._n({"x": v}, "x") for v in (None, "1000", True, float("nan"), float("inf"), 7)],
                         [0, 0, 0, 0, 0, 7])
        sl.cost({"output_tokens": 5}, 5)                      # a model that is not text prices at the fallback
        sl.cost({"output_tokens": 5}, ["x"])
        good = self.line("good")
        wrong = {
            "iterations is an object": {"iterations": {"a": 1}},
            "iterations hold a non-object": {"iterations": [3, {"output_tokens": 2}, {"output_tokens": 1}]},
            "string count": {"output_tokens": "1000"},
        }
        for name, extra in wrong.items():
            with self.subTest(name):
                row = self.line("w", usage=extra)
                got = sl.last_turn(self.rows(row))
                self.assertIsNotNone(got, "the segment survives a field of the wrong type")
        got = sl.last_turn(self.rows(self.line("w", model=5)))
        self.assertIsNotNone(got)
        # an attempt is priced at its own model: a haiku refusal under an opus call is not an opus one
        two = self.line("t", usage={"iterations": [
            {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 300_000,
             "cache_read_input_tokens": 0, "model": "claude-haiku-5-5"},
            {"input_tokens": 10, "output_tokens": 1000, "cache_creation_input_tokens": 1000,
             "cache_read_input_tokens": 100_000, "model": "claude-opus-5-5"}]})
        _, earlier = sl.last_turn(self.rows(two))
        want = 300_000 * 0.20 / 1e6                           # haiku-5-5's 1h write; opus-5-5's would be 8.00 -> $2.40
        self.assertAlmostEqual(earlier, want, places=6)
        c = ccost.load(self.rows(two))[0]
        self.assertAlmostEqual(ccost.earlier_usd(c), want, places=6, msg="ccost prices the attempt at its own model too")

    def test_prices_that_are_negative_or_not_finite_are_dropped_by_every_reader(self):
        sl = self.sl
        for name, table in {"negative": [1, -2, 3, 4, 5], "infinite": [1, float("inf"), 3, 4, 5],
                            "nan": [1, float("nan"), 3, 4, 5]}.items():
            with self.subTest(name):
                self.assertEqual(lacuna_config._price_table({"models": {"x": table}}), {})
                self.assertEqual(sl._price_table({"models": {"x": table}}), {})
        for reader in (lacuna_config._price_table, sl._price_table):
            self.assertEqual(list(reader({"models": {"claude-opus-5-5": [1, 2, 3, 4, 5]}})), ["opus-5-5"],
                             "a price keyed by the full model id is the alias's price too")

    def test_the_cache_guard_skips_a_compact_looking_line_that_is_not_an_object(self):
        sys.path.insert(0, os.path.join(ROOT, "hooks"))
        import cache_guard
        p = self.rows('"isCompactSummary"', '[1, "compact_boundary"]', '5')
        self.assertFalse(cache_guard.compacted_since(p, 0))


class TestFleetReportOrder(unittest.TestCase):
    """A fork copies its parent's calls, message ids and all, into a new file; the fleet report
    counts each id once, in the first file it meets. That file must not depend on what order the
    filesystem lists them in, or the same disk gives a different "by project" on another machine."""

    def test_the_first_file_in_sorted_order_is_credited_whatever_order_glob_returns(self):
        import contextlib, io
        from datetime import datetime, timezone
        sys.path.insert(0, os.path.join(ROOT, "scripts", "cost"))
        import spend
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = json.dumps({"type": "assistant", "timestamp": now, "message": {
            "id": "same", "model": "claude-sonnet-5-5", "usage": {"output_tokens": 1_000_000}}}) + "\n"
        with tempfile.TemporaryDirectory() as root:
            for proj in ("-work-aaa", "-work-zzz"):
                os.makedirs(os.path.join(root, proj))
                with open(os.path.join(root, proj, "s.jsonl"), "w") as f:
                    f.write(row)
            real = spend.glob.glob
            for name, order in {"as listed": lambda xs: xs, "reversed": lambda xs: list(reversed(xs))}.items():
                with self.subTest(name), mock.patch.object(spend, "ROOT", root), \
                        mock.patch.object(spend.glob, "glob", lambda *a, **k: order(real(*a, **k))):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        spend.COLOR = False
                        spend.fleet_report(7)
                    self.assertIn("work-aaa $10.00", out.getvalue())
                    self.assertNotIn("work-zzz", out.getvalue())


class TestEstimateAndReadAgree(unittest.TestCase):
    """`lit estimate` exists so nobody is surprised by a bill. It quoted three extensions while
    `read` accepted six, so a mixed folder was priced for a fraction of what the run paid for."""

    def test_the_two_accept_the_same_documents(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        sys.path.insert(0, os.path.join(ROOT, "scripts", "lit"))
        import lit_cost, lit_pipeline
        self.assertEqual(set(lit_cost.READABLE), set(lit_pipeline.TEXT_EXTS) | {".pdf"},
                         "estimate must price exactly the documents read will accept")

    def test_a_mixed_directory_is_priced_in_full(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            for name in ("a.md", "b.markdown", "c.rst", "d.text", "e.txt"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("# doc\n\n" + ("body " * 400))
            r = subprocess.run([os.path.join(ROOT, "bin", "lacuna"), "lit", "estimate", d],
                               capture_output=True, text=True,
                               env={**os.environ, "LACUNA_DATA": os.path.join(d, "data")})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("5 documents", r.stdout, f"all five should be priced:\n{r.stdout}")


class TestCacheWritesAreAtomic(unittest.TestCase):
    """A cache entry is read by a later run as proof the work is done, so it must appear whole or
    not at all. An interrupted plain copy left a truncated note served as a free hit."""

    def test_note_cache_copy_goes_through_a_temporary(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts", "lit"))
        import lit_pipeline
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "s.md"), os.path.join(d, "c.md")
            with open(src, "w") as f:
                f.write("# note\n" + ("x" * 5000))
            lit_pipeline._atomic_copy(src, dst)
            with open(dst) as f:
                self.assertEqual(len(f.read()), 5007)
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".part")], [],
                             "no temporary may be left behind")

    def test_a_failed_copy_leaves_no_partial_entry(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts", "lit"))
        import lit_pipeline
        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "sub", "c.md")       # parent does not exist: copy must fail
            with self.assertRaises(OSError):
                lit_pipeline._atomic_copy(__file__, dst)
            self.assertFalse(os.path.exists(dst))
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".part")], [])
            dst = os.path.join(d, "c.md")              # the copy is made, then cannot be renamed
            with mock.patch.object(lit_pipeline.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    lit_pipeline._atomic_copy(__file__, dst)
            self.assertEqual(os.listdir(d), [], "the temporary was left beside the cache entry")


class TestStatusMarkerEdges(unittest.TestCase):
    """This check has swung between false positives and false negatives four times. All three
    outcomes are pinned here so the next narrowing or widening cannot pass silently."""

    # The rows are the 0.3.1 fix review's table (F = a failure, P = a result; the ids are its own),
    # each a shape a real model could write. Each is judged as the opening of a plan and as a reply.
    BODY = "\n\n## 1. A real proposal\n\n" + "Substantive plan content naming src/widget/sampler.py. " * 60

    def judged(self, text):
        return tier((text + self.BODY)[:1200]), tier(text, reply=True)

    def test_a_status_word_set_off_in_the_opening_is_a_failure(self):
        """In capitals it fails the stage: the protocol word agents are told to use, and what failing
        models wrote. In any other case it is as often a title or a finding, so it is a doubt; F10,
        F11, F12, F40, F46, F54 and F59 failed the stage until the user chose this (r2-content E1)."""
        for text, cid in [
            ('# Development plan (BLOCKED)', 'F01'),
            ('# Development plan — BLOCKED', 'F02'),
            ('# Development plan: BLOCKED', 'F03'),
            ('# Development plan [BLOCKED]', 'F04'),
            ('# Development plan **BLOCKED**', 'F05'),
            ('# Development plan — BLOCKED.', 'F06'),
            ('# Development plan (PARTIAL)', 'F07'),
            ('# Development plan — PARTIAL', 'F08'),
            ('Status: INCOMPLETE', 'F09'),
            ('# Development plan – BLOCKED', 'F13'),
            ('# Development plan - BLOCKED', 'F14'),
            ('# Development plan (BLOCKED).', 'F15'),
            ('# Development plan 🚫 BLOCKED', 'F16'),
            ('# 🚫 Development plan — BLOCKED 🚫', 'F17'),
            ('# Development plan (*BLOCKED*)', 'F18'),
            ('Development plan (BLOCKED)\n==========================', 'F19'),
            ('# Development plan (BLOCKED) #', 'F20'),
            ('# Development plan (BLOCKED: notes unreadable)', 'F21'),
            ('# Development plan — BLOCKED by missing notes', 'F22'),
            ('# Development plan (BLOCKED — see below)', 'F23'),
            ('# Development plan (BLOCKED - see below)', 'F24'),
            ('# Development plan: BLOCKED by unreadable notes', 'F25'),
            ('# Development plan: INCOMPLETE: notes unreadable', 'F26'),
            ('INCOMPLETE: notes unreadable', 'F27'),
            ('BLOCKED by missing notes', 'F28'),
            ('BLOCKED, notes unreadable', 'F29'),
            ('[BLOCKED] Notes could not be read', 'F30'),
            ('(BLOCKED) Notes could not be read', 'F31'),
            ('**BLOCKED** — notes unreadable', 'F32'),
            ('BLOCKED.', 'F33'),
            ('INCOMPLETE: none of the literature notes could be read', 'F34'),
            ('BLOCKED: none of the notes are readable', 'F35'),
            ('PARTIAL: 0 of 5 notes were readable', 'F36'),
            ('# Development plan — BLOCKED: none of the notes could be read', 'F37'),
            ('# Development plan (FAILED)', 'F38'),
            ('FAILED: the notes could not be read', 'F39'),
            ('**Status:** BLOCKED by unreadable notes', 'F41'),
            ('# Development plan (BLOCKED; notes unreadable)', 'F42'),
            ('# Development plan (BLOCKED / PARTIAL)', 'F43'),
            ('# Development plan — Status: BLOCKED', 'F44'),
            ('> [!WARNING] BLOCKED', 'F45'),
            ('# BLOCKED Development plan', 'F47'),
            ('# Development plan (INCOMPLETE — notes unreadable)', 'F48'),
            ('# Development plan (PARTIAL - 2 of 5 notes readable)', 'F49'),
            ('# Development plan (PARTIAL, 2 of 5 notes readable)', 'F50'),
            ('# Development plan (PARTIAL 2/5 notes)', 'F51'),
            ('# Development plan (BLOCKED pending readable notes)', 'F52'),
            ('# Development plan — BLOCKED until the notes are re-read', 'F53'),
            ('# DEVELOPMENT PLAN (BLOCKED)', 'F55'),
            ('# DEVELOPMENT PLAN — BLOCKED BY UNREADABLE NOTES', 'F56'),
            ('# Development plan (PARTIAL / BLOCKED)', 'F57'),
            ('INCOMPLETE: 0 of 12 claims could be checked; the repository is empty', 'F58'),
            ('# Development plan — BLOCKED (notes unreadable)', 'F60'),
            ('## BLOCKED', 'F61'),
            ('Development plan — BLOCKED', 'F62'),
        ]:
            with self.subTest(cid, text=text[:50]):
                self.assertEqual(self.judged(text), ("fail", "fail"), f"{cid} {text!r}")
        for text, cid in [
            ('# Development plan (Blocked)', 'F10'),
            ('# Development plan (blocked)', 'F11'),
            ('# Development plan — Blocked', 'F12'),
            ('Status: Blocked — the notes are binary', 'F40'),
            ('# Development plan (status: blocked)', 'F46'),
            ('# Development Plan: Blocked — Notes Unreadable', 'F54'),
            ('# Development plan: Incomplete', 'F59'),
            ('**Partial**: the notes were unreadable', 'bold, title case'),
            ('# Partial 3q tetrasomy: a case series (Ito 2021)', 'a real title, E1'),
            ('# Failed by Default: Why RBFE Networks Miss Scaffold Hops (Lee 2024)', 'a real title, E1'),
        ]:
            with self.subTest(cid, text=text[:50]):
                self.assertEqual(self.judged(text), ("warn", "warn"), f"{cid} {text!r}")

    def test_a_status_reporting_none_is_a_clean_result(self):
        """"none" must be the whole value: "INCOMPLETE: none of the files could be opened" is a reason."""
        for text, cid in [
            ('Blocked: none.', 'P06'),
            ('BLOCKED: none', 'P08'),
            ('PARTIAL: none', 'P09'),
            ('Blocked: nothing. All dependencies are in the tree.', 'P11'),
            ('Blocked: N/A', 'P12'),
            ('Blocked: *none*', 'P13'),
            ('Blocked — none', 'P14'),
            ('Blocked: no', 'P15'),
            ('Blocked: (none)', 'P16'),
            ('# Development plan\n\n## Status\n\nBlocked: none.', 'P25'),
            ('PARTIAL: 0', 'P31'),
            ('Blocked: None — everything needed is in the repo', 'P32'),
            ('Blocked: **none**', 'P36'),
            ('Blocked: `none`', 'P37'),
            ('Blocked: 0 items', 'P38'),
            ('BLOCKED: no', 'P40'),
            ('# Blocked: none. All dependencies are in the tree.', 'H1'),
            ('# Plan — PARTIAL: 0', 'H2'),
            ('INCOMPLETE: nil', 'H3'),
        ]:
            with self.subTest(cid, text=text[:50]):
                self.assertEqual(self.judged(text), (None, None), f"{cid} {text!r}")

    def test_a_reply_is_judged_on_every_line(self):
        """Every agent is told to reply INCOMPLETE when it could not read its sources. A real planner
        put it on line 3, under two lines of prose, and only the first line was read (VERDICT3 N6). A
        reply is all opening: a marker in capitals at the start of any line fails, and the told word
        anywhere. "Wrote the plan.\\nBLOCKED: …" was filed until the user chose this rule. A reply that
        is only a path is not prose: a data directory under …/Failed/ failed every read (E4)."""
        for text in ("Both literature notes are binary garbage -\nno text survives.\nINCOMPLETE: notes unreadable",
                     "/x/plan.md.partial\n\n`INCOMPLETE: two notes are empty`",
                     "Done. I could not open any of the notes, so the plan rests on the repo alone.",
                     "Wrote the plan.\nBLOCKED: proposal 3 waits for the CUDA build.",
                     "`INCOMPLETE`: the paper text is symbol noise",
                     "The extracted text is symbol noise. INCOMPLETE: no sentence survives.",
                     "Reply: `INCOMPLETE: the paper text is unreadable`"):
            with self.subTest(text[:40]):
                self.assertEqual(tier(text, reply=True), "fail", text)
        for text in ("Wrote /x/plan.md.partial.\n\nSummary:\n- 6 proposals, most valuable first\n- Incomplete: no "
                     "note gives a baseline for proposal 5, so its test compares against the current sampler.",
                     "incomplete: the paper text is symbol noise",
                     "Wrote /x/0.md. I couldn't access the supplementary material, so SI tables are omitted.",
                     "- `src/ghost.py` -> not found\n- Incomplete: proposal 2 cites the wrong note."):
            with self.subTest(text[:40]):
                self.assertEqual(tier(text, reply=True), "warn", text)
        for text in ("/x/notes/a.md", "/Users/me/Failed/lacuna-data/cache/drafts/read-abc/0.md",
                     "`/Users/me/BLOCKED/repo/lit/plan.md.partial`",
                     "Wrote /x/notes/a.md.\n\nPages 3-12 were garbled; the note says which numbers are missing.",
                     "Wrote it.\nINCOMPLETE-data methods are covered in proposal 2.",
                     "- `src/a.py` -> exists\n- INCOMPLETE: data for site B -> the note has it (Table 3)",
                     "I checked 14 claims; 2 failed:\n\n- `src/ghost.py` -> not found",
                     "No unverifiable claims found."):
            with self.subTest(text[:40]):
                self.assertIsNone(tier(text, reply=True), text)

    def test_a_status_word_in_a_name_is_not_a_failure(self):
        """Real titles: a technique, a finding, a code constant. Discarding them is the false positive
        this check keeps swinging into, and each one is a paid result thrown away."""
        for text, cid in [
            ('# PARTIAL LEAST SQUARES for batch correction', 'P01'),
            ('# BLOCKED GIBBS SAMPLING (Smith 2020)', 'P02'),
            ('# INCOMPLETE-DATA METHODS (Rubin 1976)', 'P03'),
            ('# Development plan (PARTIAL LEAST SQUARES)', 'P04'),
            ('# Development plan (Partial least squares)', 'P05'),
            ('Status: no blockers', 'P07'),
            ('# Development plan\n\nTwo proposals follow.\n\n## Blocked items', 'P10'),
            ('# Development plan: handling INCOMPLETE, noisy labels', 'P17'),
            ('# Development plan: when are the data INCOMPLETE?', 'P18'),
            ('# Arithmetic is INCOMPLETE (Gödel 1931)', 'P19'),
            ('# PARTIAL / FULL pooling for hierarchical models', 'P20'),
            ('# Development plan: adopt PARTIAL pooling', 'P21'),
            ('# PLS (PARTIAL LEAST SQUARES) regression', 'P22'),
            ('# Why phase III trials FAILED (Smith 2020)', 'P23'),
            ('# FAILED, PARTIAL and COMPLETE responses in oncology (Lee 2019)', 'P24'),
            ('# Development plan (v2; PARTIAL rollout)', 'P26'),
            ('Blocked items: none', 'P27'),
            ('# Development plan — no blockers', 'P28'),
            ('# PARTIAL-observation MDPs (Kaelbling 1998)', 'P29'),
            ('# BLOCKED Gibbs (Smith 2020)', 'P30'),
            ('# BLOCKED-Gibbs vs. collapsed samplers', 'P33'),
            ('# ON THE INCOMPLETE BETA FUNCTION (Pearson 1934)', 'P34'),
            ('# Missing data: complete vs. INCOMPLETE (Little 2002)', 'P35'),
            ('# Development plan (no blockers; PARTIAL LEAST SQUARES first)', 'P39'),
            ('# Development plan: from FAILED runs to a working sampler', 'P41'),
            ('# Status of the sampler: PARTIAL', 'P42'),
            ('# Development plan: retry jobs stuck in `FAILED`', 'A2 code'),
            ('# Development plan: handle `JobState.PARTIAL` in the scheduler', 'A2 code'),
            ('# Development plan (the `BLOCKED` state machine)', 'A2 code'),
            ('# Development plan: emit `INCOMPLETE` when a batch is cut short', 'A2 code'),
            ('# `FAILED` jobs and how to retry them', 'code at the start'),
            ('# Missing data INCOMPLETE (Little 2002)', 'A2 inline'),
            ('# **Blocked** Gibbs sampling (Smith 2020)', 'a bold name'),
            ('# Plan\n\n- Port the scorer to CUDA (BLOCKED)\n- Add a --json flag', 'a tag on a proposal'),
        ]:
            with self.subTest(cid, text=text[:50]):
                self.assertEqual(self.judged(text), (None, None), f"{cid} {text!r}")


class TestHolds(unittest.TestCase):
    """One run writes a given note, plan or report at a time. 0.3.1 reserved a path with a "pending"
    entry in the record, released only on success: a refused reader, Ctrl-C, a missing parser or a
    kill left it, and every later read of a same-named document was refused for good, --overwrite
    included. A hold ends with the process that took it, however that process ends."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.addCleanup(L.release_all)
        self.note = os.path.join(self.d, "main.md")

    def holder(self):
        """Another process holding the note path until it is killed or told to exit."""
        code = (f"import sys; sys.path.insert(0, {os.path.join(ROOT, 'scripts', 'lit')!r})\n"
                f"import lit_pipeline as L\nL.hold({self.note!r}, 'testing')\nprint('held', flush=True)\n"
                f"sys.stdin.read()\n")
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, env={"HOME": home, "LACUNA_DATA": home, "PATH": "/usr/bin:/bin"})
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_a_hold_ends_with_its_process_however_it_ends(self):
        for how in ("killed", "exits without releasing"):
            with self.subTest(how):
                p = self.holder()
                info = L.hold(self.note, "reading")
                self.assertEqual((info["pid"], info["doing"]), (p.pid, "testing"), "who holds it, for the message")
                if how == "killed":
                    p.kill()
                else:
                    p.stdin.close()
                p.wait()
                self.assertIsNone(L.hold(self.note, "reading"), "a hold outlived the run that took it")
                L.release_all()
        self.assertEqual(os.listdir(self.d), [], "a released hold leaves nothing behind")

    def test_a_hold_that_cannot_be_taken_is_not_fatal_and_not_silent(self):
        """flock fails with EOPNOTSUPP on some network and FUSE mounts, and that was swallowed: nothing
        was held, two runs into one --out both paid, and one moved the other's plan aside (J4). The
        run goes on unprotected, says so once per directory, and leaves no lock file behind."""
        import contextlib, errno
        warning = f"cannot lock {self.d}; concurrent runs into it are not protected"
        err = io.StringIO()
        with mock.patch.object(L.fcntl, "flock", side_effect=OSError(errno.EOPNOTSUPP, "Operation not supported")), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(L.hold(self.note, "reading"))
            self.assertIsNone(L.hold(os.path.join(self.d, "other.md"), "reading"))
        self.assertEqual(err.getvalue().count(warning), 1, err.getvalue())
        self.assertEqual(os.listdir(self.d), [], "a lock file nothing holds was left behind")
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        d = tempfile.mkdtemp(dir=self.d)          # no lock file can be made: not held, and said
        os.chmod(d, 0o555)
        self.addCleanup(os.chmod, d, 0o755)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(L.hold(os.path.join(d, "main.md"), "reading"))
        self.assertIn(f"cannot lock {d}", err.getvalue())


class TestNameKeyFolding(unittest.TestCase):
    """macOS treats Paper.MD and paper.md as one file, and café typed as one character the same as
    café typed as two. The collision guard and the ownership record both key on name_key, so if it
    stops folding either, two documents quietly share one note path again."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts", "lit"))
        import lit_pipeline
        self.key = lit_pipeline.name_key

    def test_case_is_folded(self):
        self.assertEqual(self.key("/x/Paper.MD"), self.key("/y/paper.md"),
                         "a case-folding filesystem gives these one note path")

    def test_unicode_form_is_normalised(self):
        import unicodedata
        nfc = unicodedata.normalize("NFC", "café.md")      # é as one character
        nfd = unicodedata.normalize("NFD", "café.md")      # e + combining accent
        self.assertNotEqual(nfc, nfd, "the fixture must really differ before normalisation")
        self.assertEqual(self.key("/x/" + nfc), self.key("/y/" + nfd),
                         "APFS stores one and returns the other; they are one name")

    def test_different_names_stay_different(self):
        self.assertNotEqual(self.key("/x/alpha.md"), self.key("/x/beta.md"))
