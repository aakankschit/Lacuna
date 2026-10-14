"""Pipeline level: whole journeys through `lacuna lit` against a fake model.

Every expensive defect so far lived in a seam rather than a module: a stage that reported success
because its output was long enough, a cache that returned a free, fast, wrong answer because its
key missed an input, paths that resolved differently for the agent than for the caller. These
tests drive read, synth and verify end to end and count the model calls, which is what lets them
prove that bad input exits *before* money is spent and that a cache hit really is free.

No test here calls a model: see support.Sandbox. Run: python3 -m unittest discover -s tests
"""
import json, os, shutil, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FAKE_PARSER_PY, ROOT, Sandbox, read, write


class Pipeline(Sandbox):
    def setUp(self):
        super().setUp()
        self.notes = self.p("notes")
        self.repo = self.p("repo")
        shutil.copytree(os.path.join(ROOT, "examples", "demo-repo"), self.repo)
        self.docs = self.p("docs")
        for name in ("alpha", "beta"):
            write(os.path.join(self.docs, f"{name}.md"), f"# {name.title()} paper\n\n## Abstract\n\n"
                  + ("Content with findings. " * 200))
        self.alpha = os.path.join(self.docs, "alpha.md")

    def read(self, *extra, **kw):
        return self.lacuna("lit", "read", self.alpha, "--out", self.notes, *extra, **kw)

    def synth(self, out, *extra, **kw):
        return self.lacuna("lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out", out,
                           "--kind", "plan", *extra, **kw)

    def verify(self, target, *extra, **kw):
        return self.lacuna("lit", "verify", "--target", target, "--repo", self.repo, *extra, **kw)

    def note(self, name="alpha.md"):
        return read(os.path.join(self.notes, name))

    def cached_notes(self):
        d = os.path.join(self.data, "cache", "notes")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def ledger(self):
        with open(os.path.join(self.data, "cost-ledger.jsonl")) as f:
            return [json.loads(l) for l in f if l.strip()]


class TestJourney(Pipeline):

    def test_read_synth_verify_is_one_coherent_journey(self):
        """The sequence the README puts in front of every new user, with after.* hooks on every
        stage and a repo that has the noise a real one has."""
        hook, marker = self.hook("after")
        self.config({"after": {"read": hook, "synth": hook, "verify": hook}})
        write(os.path.join(self.repo, "node_modules", "dep", "index.js"), "")
        r = self.lacuna("lit", "read", self.alpha, os.path.join(self.docs, "beta.md"), "--out", self.notes,
                        "--brief", "BRIEF:the project")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2/2 notes written", r.stdout)
        self.assertEqual(sorted(os.listdir(self.notes)), ["alpha.md", "beta.md"], "notes are named after the documents")
        self.assertEqual(len(self.calls()), 2, "one reader per document")

        plan = self.p("plan.md")
        r = self.synth(plan, "--objective", "What next?")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(plan))
        files = read(plan + ".filelist.txt").split("\n")
        self.assertIn(os.path.join("src", "widget", "sampler.py"), files)
        self.assertFalse([f for f in files if "node_modules" in f], "the file list must not waste context on noise")
        self.assertIn(f"Codebase: {self.repo}", self.calls()[-1]["prompt"], "the agent is given absolute paths")

        r = self.verify(plan, "--notes", self.notes)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(plan + ".unverified.md"))

        rows = self.ledger()
        self.assertEqual([row["stage"] for row in rows], ["read", "read", "synth", "verify"])
        for row in rows:
            self.assertIn("effort", row, "the recalibration recipe needs effort on every row")
            self.assertIn("cost", row)
        self.assertTrue(all(row["source_tokens"] for row in rows if row["stage"] == "read"))
        fired = read(marker).split()
        self.assertEqual(fired, [self.notes, plan, plan + ".unverified.md"], "one hook per stage, with its output")

    def test_relative_paths_and_txt_input_work_from_any_directory(self):
        """The agent runs with cwd set to the output directory. Relative inputs used to leave it
        able to read nothing, after which it wrote an apology that passed the size check."""
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "content " * 200)
        r = self.lacuna("lit", "synth", "--notes", "notes", "--repo", "repo", "--out", "out/plan.md", "--kind", "plan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.p("out", "plan.md")))
        prompt = self.calls()[-1]["prompt"]
        self.assertIn(f"Codebase: {os.path.realpath(self.repo)}", prompt)
        self.assertNotIn("Codebase: repo\n", prompt, "the agent must be given absolute paths")
        write(self.p("docs", "paper.txt"), "# Paper\n\n## Abstract\n\nText.\n")
        r = self.lacuna("lit", "read", "docs/paper.txt", "--out", "txtnotes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.p("txtnotes")), ["paper.md"], "a .txt document gets a note named after it")


class TestNoteCache(Pipeline):
    """The note cache is where money is saved or wasted. Every input that changes what a note
    says must be in the key; every omission is a free, fast, wrong answer."""

    def test_misses_on_every_input_that_changes_the_note_and_hits_otherwise(self):
        steps = [   # (extra args, prelude, expected total calls, why)
            ((), None, 1, "first read"),
            ((), None, 1, "identical: a hit"),
            (("--brief", "BRIEF:other"), None, 2, "a new brief"),
            (("--model", "sonnet"), None, 3, "a new model"),
            ((), "L.PROMPT_VERSION = '999'", 4, "the note template changed"),
        ]
        for extra, prelude, expect, why in steps:
            r = self.read(*extra, prelude=prelude)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(self.calls()), expect, f"{why}:\n{r.stdout}")
        self.config({"effort": {"reader": "high"}})
        self.assertEqual(self.read().returncode, 0)
        self.assertEqual(len(self.calls()), 5, "a new reader effort")
        os.remove(os.path.join(self.data, "config.json"))
        write(self.alpha, read(self.alpha) + "\nOne more finding.\n")
        self.assertEqual(self.read().returncode, 0)
        self.assertEqual(len(self.calls()), 6, "a changed document")
        models = [c["argv"][c["argv"].index("--model") + 1] for c in self.calls()]
        self.assertEqual(models, ["haiku", "haiku", "sonnet", "haiku", "haiku", "haiku"], "--model reaches that run only")

        write(self.alpha, read(self.alpha).replace("\nOne more finding.\n", ""))
        r = self.read("--brief", "BRIEF:other")           # back to step 3's inputs
        self.assertIn("cached (free)", r.stdout)
        self.assertEqual(len(self.calls()), 6, "unchanged inputs must not call the model again")
        self.assertIn("BRIEF:other", self.note(), "a hit must restore the cached note over whatever --out held")
        other = self.p("elsewhere")
        r = self.lacuna("lit", "read", self.alpha, "--out", other, "--brief", "BRIEF:other")
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual(read(os.path.join(other, "alpha.md")), self.note(), "a hit reaches a new --out directory")

    def test_books_are_offered_and_keyed_by_what_exists(self):
        """Found end to end: after deleting a book, the old note came back as "cached (free)"
        still citing it, because the key recorded only whether books were on, never which."""
        self.assertEqual(self.read().returncode, 0)
        self.assertIn("NO-BOOKS", self.note())
        toc = os.path.join(self.data, "books", "handbook", "toc.md")
        write(toc, "# Handbook\n\n| chapter | file | pages | ~tokens |\n|---|---|---|---|\n| Priors | 00_priors.txt | 1-20 | 5,000 |\n")
        r = self.read()
        self.assertEqual(len(self.calls()), 2, f"a new book must miss the cache\n{r.stdout}")
        self.assertIn("BOOKS-OFFERED", self.note())
        self.assertIn(toc, self.calls()[-1]["prompt"], "the reader is told where the index is")
        r = self.read("--no-books")
        self.assertEqual(len(self.calls()), 2, "--no-books produces the first prompt again, so the first note is served")
        self.assertIn("NO-BOOKS", self.note())
        write(toc, "# Handbook, re-split\n\n| chapter | file | pages | ~tokens |\n|---|---|---|---|\n| Likelihoods | 00_lik.txt | 1-30 | 9,000 |\n")
        self.assertEqual(self.read().returncode, 0)
        self.assertEqual(len(self.calls()), 3, "a re-split book is a different book")
        os.remove(toc)
        r = self.read()
        self.assertEqual(len(self.calls()), 3, f"removing the book returns to the no-book note\n{r.stdout}")
        self.assertIn("cached (free)", r.stdout)


class TestMoneyGuarantees(Pipeline):
    """Bad input never reaches a model, and a model's failure is never filed as its output."""

    def test_bad_input_exits_2_before_any_model_call(self):
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "x" * 500)
        plan, gone = self.p("p.md"), self.p("nope")
        cases = [
            ("missing document", ("lit", "read", f"{self.docs}/nope.md", "--out", self.notes)),
            ("read --out is a file", ("lit", "read", self.alpha, "--out", os.path.join(self.notes, "n.md"))),
            ("synth missing repo", ("lit", "synth", "--notes", self.notes, "--repo", gone, "--out", plan)),
            ("synth missing notes", ("lit", "synth", "--notes", gone, "--repo", self.repo, "--out", plan)),
            ("synth --out is a directory", ("lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out", self.docs)),
            ("verify missing target", ("lit", "verify", "--target", f"{self.tmp}/nope.md", "--repo", self.repo)),
            ("verify missing repo", ("lit", "verify", "--target", self.alpha, "--repo", gone)),
            ("verify missing notes", ("lit", "verify", "--target", self.alpha, "--repo", self.repo, "--notes", gone)),
        ]
        for label, argv in cases:
            with self.subTest(label):
                r = self.lacuna(*argv)
                self.assertEqual(r.returncode, 2, f"{label}: {r.stdout}{r.stderr}")
                self.assertEqual(self.calls(), [], f"{label} reached a model, which costs real money")

    def test_a_refusal_is_never_filed_as_a_note(self):
        for mode in ("refuse", "apology", "prose"):
            with self.subTest(mode):
                r = self.read(mode=mode)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertFalse(os.listdir(self.notes), "a failed read must not leave a note behind")
                self.assertEqual(self.cached_notes(), [], "a failed read must never be cached")
                self.assertFalse(self.ledger()[-1]["wrote_note"])
        # the subtle one: success was once inferred from a file an earlier run had left in --out
        self.assertEqual(self.read("--brief", "BRIEF:one").returncode, 0)
        r = self.read("--brief", "BRIEF:two", mode="refuse")
        self.assertEqual(r.returncode, 4, "a refusal must fail even when an older note is present")
        self.assertNotIn("1/1 notes written", r.stdout)
        self.assertEqual(len(self.cached_notes()), 1, "the stale note must not be cached under the new key")

    def test_an_apology_or_refusal_is_not_filed_as_a_plan_or_a_fact_check(self):
        hook, marker = self.hook("after")
        self.config({"after": {"synth": hook, "verify": hook}})
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "content " * 200)
        plan = self.p("plan.md")
        r = self.synth(plan, mode="apology")
        self.assertEqual(r.returncode, 4, "a long, fluent explanation of failure clears any size check")
        self.assertFalse(os.path.exists(marker), "after.synth must not file an apology")
        write(plan, "# Plan\n" + "content " * 200)
        for mode in ("refuse", "garbage"):
            with self.subTest(mode):
                r = self.verify(plan, mode=mode)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertFalse(os.path.exists(plan + ".unverified.md"), "a refusal reads like a clean bill of health")
                self.assertFalse(os.path.exists(marker), "after.verify fired on a failed check")


class TestAfterHooks(Pipeline):
    """The extension seam. It runs after a stage has already succeeded and already cost money, so
    it runs through a shell (`~` and `$VAR` work, the path is appended as the final argument) and
    a broken command is reported, never raised."""

    def after(self, cmd, **env):
        self.config({"after": {"synth": cmd}})
        return self.lacuna("lit", prelude=f"L.after('synth', {self.p('plan.md')!r}); sys.exit(0)", **env)

    def test_shell_semantics(self):
        out = self.p("plan.md")
        hook, marker = self.hook("hook")
        write(os.path.join(self.home, "bin", "file-it"), f'#!/bin/sh\nprintf "%s\\n" "$@" > "{marker}"\n', 0o755)
        for cmd, env, want in [
            ("~/bin/file-it", {}, [out]),
            ("$HOOK_DIR/hook.sh", {"HOOK_DIR": self.tmp}, [out]),
            (f'"{hook}" --kind plan', {}, ["--kind", "plan", out]),
            (f'{hook} "$1" --kind plan', {}, [out, "--kind", "plan"]),
        ]:
            with self.subTest(cmd):
                os.remove(marker) if os.path.exists(marker) else None
                r = self.after(cmd, **env)
                self.assertIn("after_synth: ok", r.stdout, r.stdout + r.stderr)
                self.assertEqual(read(marker).split("\n")[:len(want)], want)

    def test_every_broken_command_is_survivable(self):
        for cmd, expect in [
            ("/bin/does-not-exist-anywhere", "127"),        # the shell reports it
            ("/usr/bin/false", "exit 1"),
            ("/bin/echo 'unbalanced", "exit"),              # the shell rejects it
            (["/bin/does-not-exist-anywhere"], "could not run"),
            (["/bin/echo", "list form"], "ok"),
        ]:
            with self.subTest(str(cmd)):
                r = self.after(cmd)
                self.assertEqual(r.returncode, 0, f"{cmd!r} crashed:\n{r.stderr}")
                self.assertIn(expect, r.stdout, f"{cmd!r} -> {r.stdout!r}")
                if expect != "ok":
                    self.assertIn("output is unaffected", r.stdout, "a failed hook must say the stage's output survived")
        r = self.after("")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), "no hook, no noise")


class TestPdfPlumbing(Pipeline):
    """PDF input goes through a parser interpreter; its flags, cache names and failures matter."""

    def setUp(self):
        super().setUp()
        self.venv_py = write(self.p("venv", "python"), FAKE_PARSER_PY, 0o755)
        self.pdf = write(self.p("smith2024.pdf"), "%PDF-1.4 not really a pdf")

    def read_pdf(self, *extra, **kw):
        kw.setdefault("LACUNA_PYTHON", self.venv_py)
        return self.lacuna("lit", "read", self.pdf, "--out", self.notes, *extra, **kw)

    def parse_lines(self):
        return [l.split() for l in read(self.parse_log).splitlines() if l.strip()]

    def test_parser_flags_tag_the_cache_and_a_parse_version_bump_reparses(self):
        for extra in ((), ("--parser", "docling"), ("--equations",)):
            r = self.read_pdf(*extra)
            self.assertEqual(r.returncode, 0, r.stderr)
        default, docling, marker = self.parse_lines()
        self.assertNotIn("--parser", default)
        self.assertEqual(docling[-2:], ["--parser", "docling"])
        self.assertIn("--equations", marker)
        self.assertTrue(docling[3].endswith("_docling.md") and marker[3].endswith("_marker.md"), (docling, marker))
        self.assertEqual(len(os.listdir(os.path.join(self.data, "cache", "md"))), 3,
                         "three parsers give different markdown for one PDF; they must not collide")
        self.assertEqual(os.listdir(self.notes), ["smith2024.md"], "the note is named after the PDF, no hash")
        self.read_pdf()
        self.assertEqual(len(self.parse_lines()), 3, "an unchanged document is not parsed again")
        self.read_pdf(prelude="L.PARSE_VERSION = '999'")
        self.assertEqual(len(self.parse_lines()), 4, "a change to clean()/validate() must reach parsed documents")

    def test_parse_failures_exit_3_before_any_model_call(self):
        r = self.read_pdf(LACUNA_PYTHON=self.p("no-such-venv", "python"))
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("doctor", r.stderr)
        r = self.read_pdf(FAKE_PARSE_FAIL="1")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("could not parse", r.stderr)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
