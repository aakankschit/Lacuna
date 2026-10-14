"""Unit level: one function, in process, with nothing on disk but a temp dir.

What belongs here is logic whose wrong implementation is a pure function of its inputs: price
lookup, config layering, parse validation and cleaning, transcript arithmetic, the content check
on a model's output, and the shipped state of the repo. Anything that needs its own process (a
CLI, a hook, the pipeline) belongs in test_modules.py or test_pipeline.py. See TESTING.md.

Run: python3 -m unittest discover -s tests
"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT, read, write
import lacuna_config, ccost, prep_md
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
            for doc in ([["haiku-5-5", 1, 1, 1, 1, 1]], {"models": [1, 2]}, "prices"):    # hand-edited, wrongly
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
        for name, (md, pages, raw_chars, word) in cases.items():
            with self.subTest(name):
                bad = prep_md.validate(md, pages, raw_chars)
                self.assertTrue(any(word in b for b in bad), f"{name}: {bad}")
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
                        "## 5. References", "Bibliography", "References and Notes"):
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
        parts = [inspect.getsource(prep_md.validate), inspect.getsource(prep_md.clean), inspect.getsource(prep_md.body),
                 prep_md.REF.pattern, prep_md.ENTRY.pattern, prep_md.HEADING.pattern, prep_md.CTRL.pattern,
                 str(prep_md.KEEP_FRACTION), str(prep_md.CTRL_BUDGET)]
        fingerprint = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
        recorded = {"3": "8fc42ad5f8c4f200"}
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

    def convert(self, outputs, pages=4, **kw):
        """Run convert() with parsers stubbed: (ok, parsers called, text written or None)."""
        called, out = [], os.path.join(self.tmp, "out.md")
        if os.path.exists(out):
            os.remove(out)

        def fake_parse(pdf, parser):
            called.append(parser)
            md = outputs.get(parser)
            return (md, None) if md is not None else (None, f"{parser}: venv missing")
        with mock.patch.object(prep_md, "parse", fake_parse), \
                mock.patch.object(prep_md, "pdf_stats", lambda pdf: (pages, 0)), \
                mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            ok, _ = prep_md.convert("x.pdf", out, quiet=True, **kw)
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
                                  ("equations invalid", {"marker": self.BAD}, {"equations": True})]:
            with self.subTest(name):
                ok, _, text = self.convert(outputs, pages=20, **kw)
                self.assertFalse(ok, f"{name}: a parse that failed validation was accepted")
                self.assertIsNone(text, f"{name}: the bad parse was written")
        # A write that dies part-way (here, on a character it cannot encode; in life, a kill or a full
        # disk) must leave no file: the pipeline serves whatever is at that path as a finished parse.
        with self.assertRaises(UnicodeEncodeError):
            self.convert({"pymupdf4llm": self.GOOD + "\ud800"})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out.md")), "a partial parse was left in the cache")


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
            self.touch(rel)
            self.assertEqual(L.find_status(self.repo), os.path.join(self.repo, rel), f"{rel} wins once it exists")
        mine = self.touch("elsewhere/MINE.md")
        self.assertEqual(L.find_status(self.repo, mine), mine)
        self.assertIsNone(L.find_status(self.repo, os.path.join(self.repo, "typo.md")))

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

    def test_cold_detection(self):
        cold = {"u": {"cache_creation_input_tokens": 150_000, "cache_read_input_tokens": 0}}
        warm = {"u": {"cache_creation_input_tokens": 1_000, "cache_read_input_tokens": 150_000}}
        self.assertTrue(ccost.is_cold(cold))
        self.assertFalse(ccost.is_cold(warm))
        self.assertFalse(ccost.is_avoidable_cold(cold, 0), "a session's first write is the price of starting")
        self.assertTrue(ccost.is_avoidable_cold(cold, 1))


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
        bad = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "tests"}]
            for fn in filenames:
                if fn.endswith((".md", ".py", ".json", ".sh")) or fn in ("lacuna", "ccsplit"):
                    with open(os.path.join(dirpath, fn), errors="ignore") as f:
                        bad += [f"{os.path.relpath(dirpath, ROOT)}/{fn}:{i}" for i, line in enumerate(f, 1)
                                if "/Users/" in line or "/home/" in line]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
