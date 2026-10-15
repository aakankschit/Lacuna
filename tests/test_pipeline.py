"""Pipeline level: whole journeys through `lacuna lit` against a fake model.

Every expensive defect so far lived in a seam rather than a module: a stage that reported success
because its output was long enough, a cache that returned a free, fast, wrong answer because its
key missed an input, paths that resolved differently for the agent than for the caller. These
tests drive read, synth and verify end to end and count the model calls, which is what lets them
prove that bad input exits *before* money is spent and that a cache hit really is free.

No test here calls a model: see support.Sandbox. Run: python3 -m unittest discover -s tests
"""
import json, os, re, shutil, signal, subprocess, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FAKE_PARSER_PY, LACUNA, ROOT, Sandbox, ls, read, write
import lit_pipeline as L


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
        self.assertEqual(ls(self.notes), ["alpha.md", "beta.md"], "notes are named after the documents")
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

    def test_relative_paths_and_text_input_work_from_any_directory(self):
        """The agent runs with cwd set to the output directory. Relative inputs used to leave it
        able to read nothing, after which it wrote an apology that passed the size check; a
        relative --target left the verifier replying that it had checked nothing, and exit 0."""
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "content " * 200)
        write(os.path.join(self.repo, "research", "STATUS.md"), "# Status\n\nDead end: warm starts.\n")
        repo, tmp = os.path.realpath(self.repo), os.path.realpath(self.tmp)
        r = self.lacuna("lit", "synth", "--notes", "../notes", "--repo", ".", "--out", "../out/plan.md",
                        "--kind", "plan", "--status", "research/STATUS.md", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.p("out", "plan.md")))
        prompt = self.calls()[-1]["prompt"]
        self.assertIn(f"Codebase: {repo}.", prompt, "the agent must be given absolute paths")
        self.assertIn(f"status file {repo}/research/STATUS.md in full", prompt, "--status as typed inside the repo")
        r = self.lacuna("lit", "verify", "--target", "out/plan.md", "--repo", "repo", "--notes", "notes")
        self.assertEqual(r.returncode, 0, r.stderr)
        prompt = self.calls()[-1]["prompt"]
        self.assertIn(f"Fact-check this document: {tmp}/out/plan.md\n", prompt)
        self.assertIn(f"Codebase: {repo}\nSource notes: {tmp}/notes\n", prompt)
        names = ("paper.txt", "Notes.MD", "essay.markdown", "spec.rst")
        for name in names:
            write(self.p("docs", name), "# Paper\n\n## Abstract\n\nText.\n")
        write(self.p("reldata", "books", "handbook", "toc.md"), "# Handbook\n")
        r = self.lacuna("lit", "read", *(f"docs/{n}" for n in names), "--out", "txtnotes", LACUNA_DATA="reldata")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(ls(self.p("txtnotes")), ["Notes.md", "essay.md", "paper.md", "spec.md"],
                         "a text document is read as it is, whatever its extension, and its note is named after it")
        self.assertIn(f"- index: {tmp}/reldata/books/handbook/toc.md\n", self.calls()[-1]["prompt"],
                      "a relative LACUNA_DATA must reach the agent as an absolute path")


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
        # A changed document may be another one saved under the same name, so replacing its note
        # takes --overwrite (TestOwnership); what is asked here is only whether it misses the cache.
        self.assertEqual(self.read("--overwrite").returncode, 0)
        self.assertEqual(len(self.calls()), 6, "a changed document")
        models = [c["argv"][c["argv"].index("--model") + 1] for c in self.calls()]
        self.assertEqual(models, ["haiku", "haiku", "sonnet", "haiku", "haiku", "haiku"], "--model reaches that run only")

        write(self.alpha, read(self.alpha).replace("\nOne more finding.\n", ""))
        r = self.read("--brief", "BRIEF:other", "--overwrite")      # back to step 3's inputs
        self.assertIn("cached (free)", r.stdout)
        self.assertEqual(len(self.calls()), 6, "unchanged inputs must not call the model again")
        self.assertIn("BRIEF:other", self.note(), "a hit must restore the cached note over whatever --out held")
        other = self.p("elsewhere")
        r = self.lacuna("lit", "read", self.alpha, "--out", other, "--brief", "BRIEF:other")
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual(read(os.path.join(other, "alpha.md")), self.note(), "a hit reaches a new --out directory")

    def test_a_cache_entry_is_whole_or_absent(self):
        """An interrupted copy into the cache left a truncated note, served as a free hit from then
        on. A copy that fails partway now leaves no entry; the note itself is paid for and placed."""
        torn = ("import os, shutil\n_copy = shutil.copyfile\n"
                "def torn(a, b, **k):\n"
                "    _copy(a, b, **k)\n"
                "    if os.sep + 'cache' + os.sep in b:\n"
                "        d = open(b).read(); open(b, 'w').write(d[:500]); raise OSError('interrupted')\n"
                "shutil.copyfile = torn\n")
        r = self.read(prelude=torn)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("could not cache the note", r.stderr)
        r = self.read()
        self.assertNotIn("cached (free)", r.stdout, "a torn cache entry was served as a free hit")
        self.assertEqual(len(self.calls()), 2)

    def test_one_note_per_document_and_only_over_its_own_notes(self):
        """Note names were taken from the parse-cache file name with any 12-hex "hash" stripped, so
        notes_3f2a9c1b7d4e.md and notes.md shared one note path, and the parallel readers cached
        one paper's note under the other's key. A document passed twice was paid for twice."""
        hexy = write(os.path.join(self.docs, "notes_3f2a9c1b7d4e.md"), read(self.alpha))
        plain = write(os.path.join(self.docs, "notes.md"), read(self.alpha) + "\nAnother paper.\n")
        # A long name that fits is a name: its cache entry once failed on a temporary name 11 bytes
        # longer than itself, or on its own 16-byte hash suffix, after the reader had been paid.
        nm = os.pathconf(self.tmp, "PC_NAME_MAX")
        fits = write(os.path.join(self.docs, "f" * (nm - 16) + ".md"), read(self.alpha) + "\nA third.\n")
        cut = write(os.path.join(self.docs, "c" * (nm - 8) + ".md"), read(self.alpha) + "\nA fourth.\n")
        for attempt in ("first read", "cache hit"):
            r = self.lacuna("lit", "read", hexy, plain, self.alpha, self.alpha, fits, cut, "--out", self.notes)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(self.calls()), 5, f"{attempt}: one call per distinct document, none on a hit")
            self.assertEqual(ls(self.notes), sorted(["alpha.md", "notes.md", "notes_3f2a9c1b7d4e.md",
                                                     os.path.basename(fits), os.path.basename(cut)]))
            for name in ls(self.notes):
                self.assertIn(f"# A note ({name})", self.note(name), f"{attempt}: {name} holds another paper's note")
        # What a failed run sets aside is named after the note, and cut to fit when it must be.
        long_note = os.path.join(self.notes, os.path.basename(cut))
        r = self.lacuna("lit", "read", cut, "--out", self.notes, "--brief", "BRIEF:again", mode="refuse",
                        FAKE_EDIT=long_note)
        self.assertEqual(r.returncode, 4, r.stderr[-300:])
        self.assertEqual(len([f for f in ls(self.notes) if ".rejected" in f]), 1)

    def test_books_are_offered_and_keyed_by_what_exists(self):
        """Found end to end: after deleting a book, the old note came back as "cached (free)"
        still citing it, because the key recorded only whether books were on, never which. The
        books live under macOS's "Application Support", so a space in the path must work."""
        books = self.p("Application Support", "books")
        read_ = lambda *extra: self.read(*extra, LACUNA_BOOKS=books)
        self.assertEqual(read_().returncode, 0)
        self.assertIn("NO-BOOKS", self.note())
        self.assertFalse(self.ledger()[-1]["books"], "the ledger records the books offered, not the flag")
        toc = os.path.join(books, "handbook", "toc.md")
        write(toc, "# Handbook\n\n| chapter | file | pages | ~tokens |\n|---|---|---|---|\n| Priors | 00_priors.txt | 1-20 | 5,000 |\n")
        r = read_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 2, f"a new book must miss the cache\n{r.stdout}")
        self.assertIn("BOOKS-OFFERED", self.note())
        self.assertTrue(self.ledger()[-1]["books"])
        self.assertIn(toc, self.calls()[-1]["prompt"], "the reader is told where the index is")
        r = read_("--no-books")
        self.assertEqual(len(self.calls()), 2, "--no-books produces the first prompt again, so the first note is served")
        self.assertIn("NO-BOOKS", self.note())
        write(toc, "# Handbook, re-split\n\n| chapter | file | pages | ~tokens |\n|---|---|---|---|\n| Likelihoods | 00_lik.txt | 1-30 | 9,000 |\n")
        self.assertEqual(read_().returncode, 0)
        self.assertEqual(len(self.calls()), 3, "a re-split book is a different book")
        os.remove(toc)
        r = read_()
        self.assertEqual(len(self.calls()), 3, f"removing the book returns to the no-book note\n{r.stdout}")
        self.assertIn("cached (free)", r.stdout)


class TestOwnership(Pipeline):
    """Lacuna replaces a file in a user's directory only when it wrote it, for this document, and
    nobody has edited it since — and says which of those failed, and what to do, when it will not.

    Ownership was once proven by byte-equality with the note cache. That asked whether Lacuna wrote
    a note, never for which document, so main.pdf from one paper's folder silently replaced
    main.pdf's note from another's on the next day's run. And it tied ownership to the cache, so
    deleting the cache, or moving the notes to another machine, refused every later run with a
    message saying Lacuna had not written notes that it had.
    """

    def test_notes_carry_the_document_they_were_read_from(self):
        smith = write(self.p("downloads", "smith2023", "main.md"), "# Smith 2023\n\n## Abstract\n\n" + "Smith. " * 300)
        jones = write(self.p("downloads", "jones2024", "main.md"), "# Jones 2024\n\n## Abstract\n\n" + "Jones. " * 300)
        note = os.path.join(self.notes, "main.md")
        read_ = lambda doc, *extra, **kw: self.lacuna("lit", "read", doc, "--out", self.notes, *extra, **kw)

        self.assertEqual(read_(smith).returncode, 0)
        smith_note = read(note)
        r = read_(jones)
        self.assertEqual(r.returncode, 2, "a second paper with the same file name replaced the first one's note")
        self.assertEqual((read(note), len(self.calls())), (smith_note, 1), "refused before any model call")
        self.assertIn(f"is the note for {smith}", r.stderr)
        self.assertIn("--overwrite", r.stderr, "a refusal must say how to go ahead")

        # Nothing about the cache or the machine decides ownership: the record travels with --out.
        shutil.rmtree(os.path.join(self.data, "cache"))
        self.assertEqual(read_(smith).returncode, 0, "deleting the cache made every later read exit 2")
        moved_notes, moved_smith = self.p("laptop", "notes"), self.p("laptop", "papers", "main.md")
        shutil.copytree(self.notes, moved_notes)
        write(moved_smith, read(smith))
        r = self.lacuna("lit", "read", moved_smith, "--out", moved_notes, "--brief", "BRIEF:new machine",
                        LACUNA_DATA=self.p("laptop", "data"))
        self.assertEqual(r.returncode, 0, f"the same paper on another machine was refused:\n{r.stderr}")

        # An edited note is the user's. Every such file is named, with the way forward.
        self.assertEqual(self.lacuna("lit", "read", self.alpha, os.path.join(self.docs, "beta.md"),
                                     "--out", self.notes).returncode, 0)
        for name in ("alpha.md", "beta.md"):
            write(os.path.join(self.notes, name), self.note(name) + "\nMy own annotation.\n")
        calls = len(self.calls())
        r = self.lacuna("lit", "read", self.alpha, os.path.join(self.docs, "beta.md"), "--out", self.notes,
                        "--brief", "BRIEF:again")
        self.assertEqual((r.returncode, len(self.calls())), (2, calls), r.stderr)
        self.assertNotIn("did not write", r.stderr, "Lacuna did write it; the message must say what is true")
        for name in ("alpha.md", "beta.md"):
            self.assertIn(f"{name} was written by Lacuna on ", r.stderr)
            self.assertIn("My own annotation.", self.note(name))
        self.assertIn("has been edited since", r.stderr)

        r = read_(jones, "--overwrite")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Jones", read(note), "--overwrite is the stated way forward and must work")
        mine = write(self.p("mine.md"), "my own file\n")
        os.remove(os.path.join(self.notes, "alpha.md"))
        os.symlink(mine, os.path.join(self.notes, "alpha.md"))
        self.assertEqual(self.read("--overwrite").returncode, 0)
        self.assertEqual(read(mine), "my own file\n", "--overwrite wrote a note through a symlink")

        # A note written before the record existed (0.2.0) is adopted when it is exactly this
        # document's cached note; anything else stays refused.
        os.remove(os.path.join(self.notes, ".lacuna.json"))
        r = read_(jones)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cached (free)", r.stdout)
        os.remove(os.path.join(self.notes, ".lacuna.json"))
        self.assertEqual(read_(smith).returncode, 2, "an unrecorded note that is not this document's is not ours")
        # An after.read hook is part of the user's pipeline: what it leaves is still Lacuna's note.
        # extending.md suggests one that prepends frontmatter, and it made every later run refuse.
        hook = write(self.p("frontmatter.sh"), '#!/bin/sh\nfor f in "$1"/*.md; do printf -- "---\\ntags: [paper]\\n---\\n" | '
                     'cat - "$f" > "$f.tmp" && mv "$f.tmp" "$f"; done\n', 0o755)
        self.config({"after": {"read": hook}})
        self.assertEqual(read_(jones).returncode, 0)
        self.assertTrue(read(note).startswith("---\ntags"), "the hook ran")
        self.assertEqual(read_(jones).returncode, 0, "a note the after.read hook changed was refused as edited")
        os.remove(os.path.join(self.data, "config.json"))
        # A damaged record must refuse cleanly, never with a traceback.
        record = os.path.join(self.notes, ".lacuna.json")
        saved = read(record)
        write(record, "[" * 100_000)
        r = read_(jones)
        self.assertEqual(r.returncode, 2, r.stderr[-300:])
        self.assertIn(".lacuna.json could not be read", r.stderr)

    def test_what_is_cached_is_what_this_runs_agent_wrote(self):
        """The reader wrote the note path, and whatever was there when it returned was judged and
        cached: an earlier note after a refusal, another run's note, an edit made meanwhile. It
        writes a draft of its own now; the note is replaced only by that draft, and only if nobody
        changed it during the run."""
        note = os.path.join(self.notes, "alpha.md")
        self.assertEqual(self.read("--brief", "BRIEF:one").returncode, 0)
        first = self.note()
        r = self.read("--brief", "BRIEF:two", FAKE_EDIT=note)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"KEPT — {note} changed while this run was in progress", r.stdout)
        self.assertEqual(self.note(), first + "EDITED WHILE THE MODEL RAN\n", "an edit made during the run was lost")
        r = self.read("--brief", "BRIEF:two", "--overwrite")
        self.assertIn("cached (free)", r.stdout, "the note this run paid for was not kept")
        self.assertIn("BRIEF:two", self.note())
        self.assertNotIn("EDITED", self.note(), "the cache took what was at the note path, not what the agent wrote")
        r = self.read("--brief", "BRIEF:three", mode="refuse", FAKE_EDIT=note)
        self.assertEqual(r.returncode, 4)
        self.assertIn("EDITED", read(note + ".rejected"), "a note that changed during a failed run is moved aside")
        self.assertFalse(os.path.exists(note), "and not left for the next synth to read")

    def test_each_note_is_recorded_when_it_is_written(self):
        """A run where one document is refused exits 4 and never reaches the after.read hook. The
        notes it did write, read or served from the cache, must be recorded all the same: otherwise
        deleting the cache made the next read refuse them as not Lacuna's."""
        beta = os.path.join(self.docs, "beta.md")
        self.assertEqual(self.lacuna("lit", "read", self.alpha, "--out", self.p("first")).returncode, 0)
        for label in ("read by the model", "served from the cache"):
            with self.subTest(label):
                out = self.p(label.split()[0])
                if label == "read by the model":
                    shutil.rmtree(os.path.join(self.data, "cache"))
                r = self.lacuna("lit", "read", self.alpha, beta, "--out", out, FAKE_REFUSE_IF="beta.md")
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn("cached (free)" if label != "read by the model" else "1/2 notes written", r.stdout)
                shutil.rmtree(os.path.join(self.data, "cache"))
                r = self.lacuna("lit", "read", self.alpha, "--out", out)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_document_inside_out_under_another_spelling_survives_overwrite(self):
        """macOS folds case and Unicode form: Paper.MD inside --out is the note path Paper.md. The
        self-check compared realpaths, which differ, so --overwrite deleted the document and the
        run crashed after paying. On a case-sensitive disk the two are separate files; either
        way the document must survive."""
        for name, out in (("Paper.MD", "notes"), ("paper.md", "NOTES")):
            with self.subTest(name=name, out=out):
                d = self.p(f"case-{name}")
                doc = write(os.path.join(d, "notes", name), "# Paper\n\n## Abstract\n\n" + "Text. " * 200)
                text = read(doc)
                r = self.lacuna("lit", "read", doc, "--out", os.path.join(d, out), "--overwrite")
                self.assertIn(r.returncode, (0, 2), r.stderr[-400:])
                self.assertEqual(read(doc), text, "the document was deleted or overwritten by its own note")

    def test_a_plan_at_synth_out_is_replaced_only_if_lacuna_wrote_it(self):
        """The ownership rule once covered read's notes only: synth --out silently replaced a
        hand-edited plan in a vault. And a model that wrote --out itself, instead of the .partial
        it was given, left its failed output there under a message calling it an earlier run's."""
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "content " * 200)
        plan = self.p("vault", "plan.md")
        self.assertEqual(self.synth(plan).returncode, 0)
        self.assertEqual(self.synth(plan, "--objective", "again").returncode, 0, "its own plan is Lacuna's to replace")
        write(plan, read(plan) + "\nMy decision: do proposal 2 first.\n")
        r = self.synth(plan)
        self.assertEqual((r.returncode, len(self.calls())), (2, 2), r.stderr)
        self.assertIn("has been edited since", r.stderr)
        self.assertIn("My decision", read(plan))
        self.assertEqual(self.synth(plan, "--overwrite").returncode, 0)
        self.assertNotIn("My decision", read(plan))

        mine = write(self.p("mine.txt"), "mine\n")
        os.remove(plan + ".filelist.txt")
        os.symlink(mine, plan + ".filelist.txt")
        self.assertEqual(self.synth(plan).returncode, 0)
        self.assertEqual(read(mine), "mine\n", "the file list was written through a symlink")
        r = self.synth(self.p("vault", "other.md"), mode="clobber")
        self.assertEqual(r.returncode, 4)
        self.assertFalse(os.path.exists(self.p("vault", "other.md")), "the model's failed output was left for the next command")
        self.assertTrue(os.path.exists(self.p("vault", "other.md.rejected")),
                        "moved aside, never deleted: --out may have changed because the user edited it")
        self.assertIn("wrote", r.stderr)
        self.assertNotIn("earlier run's output", r.stderr, "the file held this run's output, not an earlier one")

    def test_a_file_is_replaced_only_by_the_stage_and_document_that_wrote_it(self):
        """Same path is not same document: a new download saved as fulltext.md over the last one is
        another paper, and its note silently replaced the first paper's. Nor is a note a plan: a
        paper named plan.md replaced the plan synth had recorded beside it, because a plan's record
        names no document to compare. Records written before the stage was recorded must hold."""
        lit = self.p("lit")
        note = os.path.join(lit, "fulltext.md")
        paper = write(self.p("downloads", "fulltext.md"), "# Paper A about kinases\n\n" + "A. " * 300)
        read_ = lambda doc, *extra: self.lacuna("lit", "read", doc, "--out", lit, *extra)
        self.assertEqual(read_(paper).returncode, 0)
        first = read(note)
        write(paper, "# Paper B about glaciers\n\n" + "B. " * 300)
        calls = len(self.calls())
        r = read_(paper)
        self.assertEqual((r.returncode, len(self.calls())), (2, calls), r.stderr)
        self.assertIn(f"{note} is the note for {paper}, which has changed since that note was written", r.stderr)
        self.assertIn("--overwrite", r.stderr)
        self.assertEqual(read(note), first, "the first paper's note was replaced by the second's")
        self.assertEqual(read_(paper, "--overwrite").returncode, 0, "--overwrite is the way forward")
        self.assertIn("Paper B", read(note))
        copy = write(self.p("copy.md"), read(note))       # a link is the user's, even to the same bytes
        os.remove(note)
        os.symlink(copy, note)
        calls = len(self.calls())
        self.assertEqual((read_(paper).returncode, len(self.calls())), (2, calls))
        os.remove(note)
        write(note, read(copy))

        self.assertEqual(self.read().returncode, 0)               # notes for synth to read
        plan, x = os.path.join(lit, "plan.md"), write(os.path.join(lit, "x.md"), "# X\n" + "x " * 300)
        y = write(os.path.join(lit, "y.md"), "# Y\n" + "y " * 300)
        self.assertEqual(self.synth(plan).returncode, 0)
        self.assertEqual(self.verify(plan).returncode, 0)
        self.assertEqual(self.synth(x + ".unverified.md").returncode, 0)
        self.assertEqual(read_(write(self.p("papers", "y.md.unverified.md"), "# Y paper\n" + "w " * 300)).returncode, 0)
        named_like = [write(self.p("papers", n), f"# A paper called {n}\n" + "z " * 300)
                      for n in ("plan.md", "plan.md.unverified.md")]
        clashes = [
            ("a note over a plan", ("lit", "read", named_like[0], "--out", lit), plan),
            ("a note over a verify report", ("lit", "read", named_like[1], "--out", lit), plan + ".unverified.md"),
            ("a plan over a note", ("lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out", note), note),
            ("a report over a plan", ("lit", "verify", "--target", x, "--repo", self.repo), x + ".unverified.md"),
            ("a report over a note", ("lit", "verify", "--target", y, "--repo", self.repo), y + ".unverified.md"),
        ]
        same = [("read", (paper,)), ("synth", (plan,)), ("verify", (plan,))]
        record = os.path.join(lit, ".lacuna.json")
        for version in ("0.3.2", "0.3.1, which did not record the stage"):
            if version != "0.3.2":
                entries = json.loads(read(record))
                for e in entries["files"].values():
                    del e["stage"]
                write(record, json.dumps(entries))
            for label, argv, kept in clashes:
                with self.subTest(version=version, clash=label):
                    before, calls = read(kept), len(self.calls())
                    r = self.lacuna(*argv)
                    self.assertEqual((r.returncode, len(self.calls())), (2, calls), r.stdout + r.stderr)
                    self.assertIn(" written by `lacuna lit ", r.stderr)
                    self.assertEqual(read(kept), before)
            for stage, args in same:
                with self.subTest(version=version, same_stage=stage):
                    r = {"read": lambda: read_(*args), "synth": lambda: self.synth(*args),
                         "verify": lambda: self.verify(*args)}[stage]()
                    self.assertEqual(r.returncode, 0, f"its own {stage} output is Lacuna's to replace: {r.stderr}")

    def test_what_an_after_hook_leaves_is_still_lacunas(self):
        """A hook that prepends frontmatter to every note in the directory, as extending.md
        suggests, edits earlier runs' notes as well as this run's; only this run's were re-recorded,
        so the next read of an earlier document was refused as edited. after.synth and after.verify
        are given one file, and may change its neighbours just the same."""
        hook = write(self.p("every.py"), "import os, sys\n"
                     "d = sys.argv[-1] if os.path.isdir(sys.argv[-1]) else os.path.dirname(sys.argv[-1])\n"
                     "for f in sorted(os.listdir(d)):\n"
                     "    if f.endswith('.md'):\n"
                     "        p = os.path.join(d, f)\n"
                     "        t = open(p).read()\n"
                     "        open(p, 'w').write('---\\ntags: [lit]\\n---\\n' + t)\n")
        self.config({"after": {stage: [sys.executable, hook] for stage in ("read", "synth", "verify")}})
        beta = os.path.join(self.docs, "beta.md")
        self.assertEqual(self.read().returncode, 0)
        self.assertEqual(self.lacuna("lit", "read", beta, "--out", self.notes).returncode, 0)
        r = self.read()
        self.assertEqual(r.returncode, 0, f"an earlier note the hook edited again was refused:\n{r.stderr}")
        self.assertEqual(self.note("beta.md").count("tags: [lit]"), 2, "each run's hook edited every note")

        # Only what was still Lacuna's before the hook ran: the hook touching a file the user edited
        # does not make the user's edit Lacuna's.
        write(os.path.join(self.notes, "beta.md"), self.note("beta.md") + "\nMy own annotation.\n")
        self.assertEqual(self.read().returncode, 0)
        r = self.lacuna("lit", "read", beta, "--out", self.notes)
        self.assertEqual(r.returncode, 2, "a user's edit became Lacuna's because a hook touched the file")
        self.assertIn("has been edited since", r.stderr)

        # A note restored from a backup is exactly that document's cached note: adopted, not refused.
        cache = os.path.join(self.data, "cache", "notes")
        (cached,) = [f for f in os.listdir(cache) if f.startswith("alpha_")]
        shutil.copy(os.path.join(cache, cached), os.path.join(self.notes, "alpha.md"))
        r = self.read()
        self.assertEqual(r.returncode, 0, f"the note restored from its cached copy was refused:\n{r.stderr}")
        self.assertIn("cached (free)", r.stdout)

        lit = self.p("lit")
        plan = os.path.join(lit, "plan.md")
        self.assertEqual(self.synth(plan).returncode, 0)
        self.assertEqual(self.verify(plan).returncode, 0)
        self.assertEqual(self.synth(plan, "--objective", "again").returncode, 0)     # its hook edits the report
        r = self.verify(plan)
        self.assertEqual(r.returncode, 0, f"a report after.synth edited was refused as edited:\n{r.stderr}")
        self.assertEqual(self.synth(plan, "--objective", "and again").returncode, 0)

    def test_a_plan_or_report_changed_during_the_run_is_never_replaced(self):
        """--out was checked before the model call and replaced after it, so an edit made while the
        model ran was lost on a successful run. Neither the edit nor the paid result may be."""
        hook, marker = self.hook("after")
        self.config({"after": {"synth": hook, "verify": hook}})
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "content " * 200)
        plan = self.p("vault", "plan.md")
        self.assertEqual(self.synth(plan).returncode, 0)
        first = read(plan)
        os.remove(marker)
        edited = first + "EDITED WHILE THE MODEL RAN\n"
        r = self.synth(plan, FAKE_EDIT=plan)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(read(plan), edited, "the edit made during the run was lost")
        self.assertIn("# Development plan", read(plan + ".new.md"), "the paid result was lost")
        self.assertIn(f"This run's plan is in {plan}.new.md", r.stderr)
        self.assertFalse(os.path.exists(marker), "a result not filed where it was asked for is not handed to the hook")
        r = self.synth(plan, "--overwrite", FAKE_EDIT=plan)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(read(plan), edited + "EDITED WHILE THE MODEL RAN\n")
        self.assertTrue(os.path.exists(plan + ".new-2.md"), "the second result replaced the first")
        fresh = self.p("vault", "fresh.md")
        self.assertEqual(self.synth(fresh, FAKE_EDIT=fresh).returncode, 2, "--out created during the run")
        self.assertEqual(read(fresh), "EDITED WHILE THE MODEL RAN\n")
        write(plan + ".rejected", "set aside by an earlier run\n")
        r = self.synth(plan, "--overwrite", mode="refuse", FAKE_EDIT=plan)
        self.assertEqual(r.returncode, 4)
        self.assertIn("EDITED WHILE", read(plan + ".rejected-2"), "a failed run must move a changed --out aside")
        self.assertEqual(read(plan + ".rejected"), "set aside by an earlier run\n", "and never over an earlier one")

        target = write(self.p("vault", "t.md"), "# T\n" + "content " * 200)
        report = target + ".unverified.md"
        self.assertEqual(self.verify(target).returncode, 0)
        first = read(report)
        os.remove(marker)
        r = self.verify(target, FAKE_EDIT=report)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(read(report), first + "EDITED WHILE THE MODEL RAN\n")
        self.assertIn("# Unverified claims in t.md", read(report + ".new.md"))
        self.assertFalse(os.path.exists(marker))
        write(target, read(target) + "A new proposal.\n")      # the report no longer describes it
        r = self.verify(target, "--overwrite", mode="refuse", FAKE_EDIT=report)
        self.assertEqual(r.returncode, 4)
        self.assertTrue(os.path.exists(report), "a report edited during a failed run was deleted")

    def test_deleting_or_damaging_the_record_makes_every_stage_ask(self):
        """extending.md says deleting .lacuna.json is safe: Lacuna then asks before replacing
        anything. verify did not ask, and replaced a report annotated by hand. And a damaged record
        was silently rewritten with only that run's entries, so later refusals could not say why."""
        target = write(self.p("lit", "plan.md"), "# Plan\n" + "content " * 200)
        report = target + ".unverified.md"
        self.assertEqual(self.verify(target).returncode, 0)
        write(report, read(report) + "- checked by hand: fine\n")
        os.remove(self.p("lit", ".lacuna.json"))
        calls = len(self.calls())
        r = self.verify(target)
        self.assertEqual((r.returncode, len(self.calls())), (2, calls), r.stdout + r.stderr)
        self.assertIn("has no record", r.stderr)
        self.assertIn("checked by hand", read(report))
        self.assertEqual(self.verify(target, "--overwrite").returncode, 0)
        self.assertEqual(self.read().returncode, 0)
        plan = self.p("lit", "digest.md")
        self.assertEqual(self.synth(plan).returncode, 0)
        os.remove(self.p("lit", ".lacuna.json"))
        self.assertEqual(self.synth(plan).returncode, 2, "an unrecorded plan was replaced")
        os.remove(os.path.join(self.notes, ".lacuna.json"))
        self.assertEqual(self.read().returncode, 0, "an unrecorded note equal to its cached note is adopted")

        record = os.path.join(self.notes, ".lacuna.json")
        write(record, "{ not json")
        r = self.lacuna("lit", "read", os.path.join(self.docs, "beta.md"), "--out", self.notes)
        self.assertEqual(r.returncode, 0, r.stderr)
        (kept,) = [f for f in os.listdir(self.notes) if f.startswith(".lacuna.json.damaged-")]
        self.assertEqual(read(os.path.join(self.notes, kept)), "{ not json", "the damaged record was not kept")
        self.assertIn(f".lacuna.json could not be read (JSONDecodeError); it was kept as {self.notes}/{kept}", r.stderr)
        write(os.path.join(self.notes, "alpha.md"), self.note() + "\nMy own annotation.\n")
        r = self.read()
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"an earlier record here was damaged, and was kept as {kept}", r.stderr)


class TestMoneyGuarantees(Pipeline):
    """Bad input never reaches a model, and a model's failure is never filed as its output."""

    def test_bad_input_exits_2_before_any_model_call(self):
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "x" * 500)
        plan, gone = self.p("p.md"), self.p("nope")
        twins = [write(self.p(year, "review.md"), "# Review\n" + year * 300) for year in ("2023", "2024")]
        txt, pdf = write(self.p("docs", "paper.txt"), "# Paper\n"), write(self.p("docs", "paper.pdf"), "%PDF")
        vault_note = write(self.p("vault", "alpha.md"), "# My own alpha notes\n")
        alpha_text = read(self.alpha)
        cases_ = [write(self.p(d, n), "# Paper\n" + d * 300) for d, n in (("c1", "Paper.md"), ("c2", "paper.md"))]
        forms = [write(self.p(d, n), "# Paper\n" + d * 300) for d, n in (("u1", "caf\u00e9.md"), ("u2", "cafe\u0301.md"))]
        os.makedirs(self.p("linked"))
        os.symlink(self.p("elsewhere.md"), self.p("linked", "alpha.md"))
        hand_plan = write(self.p("vault", "plan.md"), "# My plan, written by hand\n")
        locked = write(self.p("locked", "n.md"), "# n\n" + "x" * 500)
        os.chmod(locked, 0)
        os.makedirs(self.p("readonly"))
        os.chmod(self.p("readonly"), 0o555)
        partial = write(self.p("partial-digest.md"), "# PARTIAL: literature notes unreadable\n\n## Status of this "
                        "digest\n\nThis digest contains no literature claims.\n" + "Padding. " * 120)
        # A name the filesystem refuses after the model call costs the call and writes nothing.
        nm = os.pathconf(self.tmp, "PC_NAME_MAX")
        venv_py = write(self.p("venv", "python"), FAKE_PARSER_PY, 0o755)
        long_pdf = write(self.p("long", "x" * nm), "%PDF")       # no extension: parsed, noted as <name>.md
        long_target = write(self.p("long", "t" * (nm - 8) + ".md"), "# Plan\n" + "x" * 500)
        os.makedirs(self.p("dirnote", "alpha.md", "inner"))
        # An input that is not content: every file argument of every subcommand, each named in the refusal.
        os.makedirs(self.p("emptyrepo"))
        dangling = self.p("dangling", "n.md")
        os.makedirs(os.path.dirname(dangling))
        os.symlink(self.p("nowhere.md"), dangling)
        binary = self.p("binnote", "x.md")
        os.makedirs(os.path.dirname(binary))
        with open(binary, "wb") as f:
            f.write(bytes(range(256)) * 20)
        locked_status = write(self.p("locked-status.md"), "dead end: X\n")
        os.chmod(locked_status, 0)
        locked_target = write(self.p("locked-target.md"), "# plan\n" * 50)
        os.chmod(locked_target, 0)
        locked_pdf = write(self.p("docs", "locked.pdf"), "%PDF-1.4 not really a pdf")
        os.chmod(locked_pdf, 0)
        os.makedirs(self.p("sealed"))
        os.chmod(self.p("sealed"), 0)
        for f in (locked_status, locked_target, locked_pdf):
            self.addCleanup(os.chmod, f, 0o644)
        self.addCleanup(os.chmod, self.p("sealed"), 0o755)
        empties = [
            ("an empty .md document", ("lit", "read", write(self.p("docs", "empty.md"), ""), "--out", self.notes)),
            ("a whitespace-only .txt document", ("lit", "read", write(self.p("docs", "blank.txt"), " \n\t\n"),
                                                 "--out", self.notes)),
            ("a binary .txt document", ("lit", "read", write(self.p("docs", "bin.txt"), "x"), binary, "--out",
                                        self.notes)),
            ("a document it cannot read", ("lit", "read", locked_pdf, "--out", self.notes)),
            ("synth --status it cannot read", ("lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out",
                                               plan, "--status", locked_status)),
            ("synth --status that is empty", ("lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out",
                                              plan, "--status", write(self.p("empty-status.md"), ""))),
            ("synth --repo with no files", ("lit", "synth", "--notes", self.notes, "--repo", self.p("emptyrepo"),
                                            "--out", plan)),
            ("synth --digest that is a few bytes", ("lit", "synth", "--digest", write(self.p("d.md"), "hi\n"),
                                                    "--repo", self.repo, "--out", plan, "--kind", "plan")),
            ("synth --digest that is binary", ("lit", "synth", "--digest", binary, "--repo", self.repo, "--out",
                                               plan, "--kind", "plan")),
            ("synth --notes holding a binary note", ("lit", "synth", "--notes", os.path.dirname(binary), "--repo",
                                                     self.repo, "--out", plan)),
            ("synth --notes holding a dangling link", ("lit", "synth", "--notes", os.path.dirname(dangling),
                                                       "--repo", self.repo, "--out", plan)),
            ("verify --target that is empty", ("lit", "verify", "--target", write(self.p("t.md"), ""), "--repo",
                                               self.repo)),
            ("verify --target that is binary", ("lit", "verify", "--target", binary, "--repo", self.repo)),
            ("verify --target it cannot read", ("lit", "verify", "--target", locked_target, "--repo", self.repo)),
            ("synth --notes directory it cannot read", ("lit", "synth", "--notes", self.p("sealed"), "--repo",
                                                        self.repo, "--out", plan)),
            ("verify --notes directory it cannot read", ("lit", "verify", "--target", self.alpha, "--repo",
                                                         self.repo, "--notes", self.p("sealed"))),
            ("verify --repo with no files", ("lit", "verify", "--target", self.alpha, "--repo", self.p("emptyrepo"))),
        ]
        cases = [
            ("two documents, one name", ("lit", "read", *twins, "--out", self.notes)),
            ("a .txt and a .pdf, one name", ("lit", "read", txt, pdf, "--out", self.notes)),
            ("the document is inside --out", ("lit", "read", self.alpha, "--out", self.docs)),
            ("the document is inside --out, even with --overwrite", ("lit", "read", self.alpha, "--out", self.docs,
                                                                     "--overwrite")),
            ("--out holds a file Lacuna did not write", ("lit", "read", self.alpha, "--out", self.p("vault"))),
            ("one name in two cases", ("lit", "read", *cases_, "--out", self.notes)),
            ("one name in two Unicode forms", ("lit", "read", *forms, "--out", self.notes)),
            ("a symlink at the note path", ("lit", "read", self.alpha, "--out", self.p("linked"))),
            ("synth --out is a file Lacuna did not write", ("lit", "synth", "--notes", self.notes, "--repo",
                                                            self.repo, "--out", hand_plan)),
            ("synth from a digest that says it is empty", ("lit", "synth", "--digest", partial, "--repo", self.repo,
                                                           "--out", plan, "--kind", "plan")),
            ("synth from notes it cannot read", ("lit", "synth", "--notes", os.path.dirname(locked), "--repo",
                                                 self.repo, "--out", plan)),
            ("read into a directory it cannot write", ("lit", "read", self.alpha, "--out", self.p("readonly"))),
            ("synth into a directory it cannot write", ("lit", "synth", "--notes", self.notes, "--repo", self.repo,
                                                       "--out", self.p("readonly", "plan.md"))),
            ("a directory at the note path, even with --overwrite", ("lit", "read", self.alpha, "--out",
                                                                     self.p("dirnote"), "--overwrite")),
            ("synth missing --status", ("lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out", plan,
                                        "--status", gone)),
            ("missing document", ("lit", "read", f"{self.docs}/nope.md", "--out", self.notes)),
            ("read --out is a file", ("lit", "read", self.alpha, "--out", os.path.join(self.notes, "n.md"))),
            ("synth missing repo", ("lit", "synth", "--notes", self.notes, "--repo", gone, "--out", plan)),
            ("synth missing notes", ("lit", "synth", "--notes", gone, "--repo", self.repo, "--out", plan)),
            ("synth --out is a directory, even with --overwrite", ("lit", "synth", "--notes", self.notes, "--repo",
                                                                   self.repo, "--out", self.docs, "--overwrite")),
            ("verify missing target", ("lit", "verify", "--target", f"{self.tmp}/nope.md", "--repo", self.repo)),
            ("verify missing repo", ("lit", "verify", "--target", self.alpha, "--repo", gone)),
            ("verify missing notes", ("lit", "verify", "--target", self.alpha, "--repo", self.repo, "--notes", gone)),
            ("a note name longer than --out allows", ("lit", "read", long_pdf, "--out", self.notes)),
            ("synth --out too long for its .filelist.txt", ("lit", "synth", "--notes", self.notes, "--repo", self.repo,
                                                            "--out", self.p("long", "p" * (nm - 5) + ".md"))),
            ("a verify report name longer than allowed", ("lit", "verify", "--target", long_target, "--repo", self.repo)),
        ]
        for label, argv in cases + empties:
            if ("cannot read" in label or "cannot write" in label) and os.geteuid() == 0:
                continue                      # root ignores file modes
            with self.subTest(label):
                r = self.lacuna(*argv, LACUNA_PYTHON=venv_py)
                self.assertEqual(r.returncode, 2, f"{label}: {r.stdout}{r.stderr}")
                self.assertNotIn("Traceback", r.stderr, f"{label}: bad input must be a message, not a crash")
                self.assertEqual(self.calls(), [], f"{label} reached a model, which costs real money")
        self.assertEqual(read(vault_note), "# My own alpha notes\n", "a user's file in --out was overwritten")
        self.assertEqual(read(hand_plan), "# My plan, written by hand\n", "a hand-written plan was overwritten")
        self.assertFalse(os.path.lexists(self.p("elsewhere.md")), "a note was written through a symlink")
        self.assertEqual(read(self.alpha), alpha_text, "the document was deleted or overwritten by its own note")

    def test_every_bad_input_is_named_in_one_refusal(self):
        blank, empty = write(self.p("docs", "blank.txt"), "  \n"), write(self.p("docs", "empty.md"), "")
        r = self.lacuna("lit", "read", self.alpha, blank, empty, "--out", self.notes)
        self.assertEqual((r.returncode, self.calls()), (2, []), r.stderr)
        self.assertIn(blank, r.stderr)
        self.assertIn(empty, r.stderr)
        self.assertNotIn(self.alpha, r.stderr, "a good document is not blamed")
        self.assertFalse(os.path.exists(self.notes), "a refused run leaves --out alone")
        r = self.lacuna("lit", "synth", "--notes", self.notes, "--repo", self.p("nope"), "--out", self.p("plan.md"))
        self.assertIn("is not a directory", r.stderr, "a missing --repo is not an empty one")

    def test_a_status_file_nobody_named_is_skipped_not_refused(self):
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "x" * 500)
        write(os.path.join(self.repo, "research", "STATUS.md"), "")
        write(os.path.join(self.repo, "NOTES.md"), "dead end: widget cache\n")
        r = self.verify(write(self.p("t.md"), "# plan\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        prompt = self.calls()[-1]["prompt"]
        self.assertIn(os.path.join(self.repo, "NOTES.md"), prompt, "the empty research/STATUS.md is not a status file")
        self.assertNotIn("research/STATUS.md", prompt)

    def test_verify_prompt_paths_are_absolute(self):
        """The agent runs in another directory: `research/STATUS.md` named relative checked nothing."""
        write(os.path.join(self.repo, "research", "STATUS.md"), "# Status\ndead end: widget cache\n")
        target = write(self.p("t.md"), "# plan\n")
        for extra in ((), ("--notes", self.notes)):
            os.makedirs(self.notes, exist_ok=True)
            r = self.verify(target, *extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            prompt = self.calls()[-1]["prompt"]
            self.assertIn(os.path.join(self.repo, "research", "STATUS.md"), prompt)
            for rel in re.findall(r"(?<![/\w.-])(?:research|docs|src)/\S+", prompt):
                self.fail(f"the prompt names {rel} relative to the caller's directory")
        os.remove(os.path.join(self.repo, "research", "STATUS.md"))
        self.verify(target)
        self.assertNotIn("STATUS", self.calls()[-1]["prompt"], "no status file, no instruction to grep one")

    def test_a_refusal_is_never_filed_as_a_note(self):
        """"incomplete" is VERDICT3 N5: a reader given unreadable text wrote a template-shaped note, and
        it was cached. It is told the INCOMPLETE rule now, and its reply is read."""
        for mode in ("refuse", "apology", "prose", "incomplete"):
            with self.subTest(mode):
                r = self.read(mode=mode)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertFalse(ls(self.notes), "a failed read must not leave a note behind")
                self.assertEqual(self.cached_notes(), [], "a failed read must never be cached")
                self.assertFalse(self.ledger()[-1]["wrote_note"])
                if mode != "refuse":
                    self.assertNotIn("produced no note file", r.stdout, "it did write one; the content was rejected")
                if mode == "incomplete":
                    self.assertIn("INCOMPLETE: the sources could not be read", r.stdout, "say what the reader said")
        # the subtle one: success was once inferred from a file an earlier run had left in --out
        self.assertEqual(self.read("--brief", "BRIEF:one").returncode, 0)
        r = self.read("--brief", "BRIEF:two", mode="refuse")
        self.assertEqual(r.returncode, 4, "a refusal must fail even when an older note is present")
        self.assertNotIn("1/1 notes written", r.stdout)
        self.assertEqual(len(self.cached_notes()), 1, "the stale note must not be cached under the new key")
        # A note cached before the check learned a shape was served free forever (VERDICT3 N10b): a hit
        # is judged by today's check, and one that fails it is read again, and paid for, not served.
        (cached,) = self.cached_notes()
        cached = os.path.join(self.data, "cache", "notes", cached)
        write(cached, "# A note (alpha.md)\n\n## Claim\nI\u2019m unable to complete this: I could not read any of "
                      "the paper.\n\n## Key results\n" + "Nothing. " * 80)
        calls = len(self.calls())
        r = self.lacuna("lit", "read", self.alpha, "--out", self.p("elsewhere"), "--brief", "BRIEF:one")
        self.assertEqual((r.returncode, len(self.calls())), (0, calls + 1), r.stdout + r.stderr)
        self.assertNotIn("cached (free)", r.stdout)
        self.assertIn("removed from the cache", r.stderr, "say why a hit was paid for")
        self.assertNotIn("unable to complete", read(cached), "the failed note must not stay cached")
        self.assertNotIn("unable to complete", read(self.p("elsewhere", "alpha.md")))

    def test_a_doubtful_opening_is_filed_with_one_warning(self):
        """Each stage discarded paid results whose opening only looked like a failure: a title
        "Partial 3D …", a summary bullet "Incomplete: …", a verify line without its arrow (r2-content
        E1, E3). They are filed, exit 0, with one warning that names the file."""
        doubt = "the opening reads like it may not have done the job"
        body = ("# Partial 3D Shape Matching for Virtual Screening (Kumar 2022)\n\n## Claim\nShape overlap ranks "
                "actives first (Table 2).\n\n## Key results\n" + "Finding, 42 units (Table 2). " * 40)
        r = self.read(mode="body", FAKE_BODY=body, FAKE_REPLY="INCOMPLETE: the text is symbol noise")
        self.assertEqual(r.returncode, 4, "a doubt in the note does not outweigh the reader's own word")
        r = self.read(mode="body", FAKE_BODY=body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.note(), body, "a paid note was discarded over its title")
        self.assertEqual(len(self.cached_notes()), 1)
        note = os.path.join(self.notes, "alpha.md")
        self.assertIn(f"! {note}: {doubt}", r.stderr)
        self.assertEqual(r.stderr.count(doubt), 1, "one warning per file")
        plan = self.p("lit", "plan.md")
        r = self.synth(plan, mode="body", FAKE_BODY="# Development plan\n\n## 1. Seam\n\n" + "Naming src/a.py. " * 60,
                       FAKE_REPLY="Wrote it.\n\n- Incomplete: no note gives a baseline for proposal 5.")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("## 1. Seam", read(plan))
        self.assertIn(f"! {note}: {doubt}", r.stderr, "a note that looks like a failure is still literature")
        self.assertIn(f"! {plan}: {doubt}", r.stderr)
        r = self.verify(plan, mode="reply", FAKE_REPLY="- `src/ghost.py` -> not found\n- Incomplete: proposal 2 "
                                                       "cites the wrong note.")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cites the wrong note", read(plan + ".unverified.md"))
        self.assertIn(f"! {plan}.unverified.md: {doubt}", r.stderr)

    def test_a_failed_synth_files_nothing_and_leaves_the_last_plan_alone(self):
        """synth judged success by the file at --out, so over an earlier plan a refusal exited 0 and
        fired the hook; and an apology it wrote stayed there for verify and synth --digest to read."""
        hook, marker = self.hook("after")
        self.config({"after": {"synth": hook}})
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "content " * 200)
        out = self.p("out")
        plan = os.path.join(out, "plan.md")
        r = self.synth(plan, mode="apology")
        self.assertEqual(r.returncode, 4, "a long, fluent explanation of failure clears any size check")
        self.assertEqual(ls(out), [], "a failed synth must leave nothing for the next command to read")
        self.assertFalse(os.path.exists(marker), "after.synth must not file an apology")
        self.assertEqual(self.synth(plan, "--objective", "speed").returncode, 0)
        first = read(plan)
        os.remove(marker)
        for mode in ("refuse", "garbage", "apology", "incomplete"):
            with self.subTest(mode):
                r = self.synth(plan, "--objective", "accuracy", mode=mode)
                self.assertEqual(r.returncode, 4, "an earlier plan on disk passed for this run's output")
                self.assertEqual(read(plan), first, "a failed run must leave the earlier plan exactly as it was")
                self.assertEqual(ls(out), ["plan.md"])
                self.assertFalse(os.path.exists(marker), "after.synth fired on a failed run")
                self.assertFalse(self.ledger()[-1]["wrote"])
        # Interrupted part-way (Ctrl-C, a closed terminal): what it wrote must not pass for a later run's.
        hung = subprocess.Popen([LACUNA, "lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out", plan,
                                 "--kind", "plan", "--objective", "interrupted"], env=self.env(FAKE_MODE="hang"),
                                cwd=self.tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 20
        while len(ls(out)) < 3 and time.time() < deadline:     # plan, file list, and the run's output
            time.sleep(0.05)
        os.killpg(hung.pid, signal.SIGKILL)
        hung.wait()
        self.assertEqual(len(ls(out)), 3, "the interrupted run never wrote its output")
        r = self.synth(plan, mode="refuse")
        self.assertEqual(r.returncode, 4, "an interrupted run's output passed for this run's")
        self.assertEqual(read(plan), first)
        self.assertEqual(ls(out), ["plan.md"])

    def test_a_verify_that_checked_nothing_is_not_filed_as_a_fact_check(self):
        """A real verifier given a relative path replied "I could not check any claims", with
        is_error false: it exited 0, wrote the reply as the report, and fired the hook."""
        hook, marker = self.hook("after")
        self.config({"after": {"verify": hook}})
        plan, report = self.p("plan.md"), self.p("plan.md.unverified.md")
        write(plan, "# Plan\n" + "content " * 200)
        for mode, reply in [("refuse", None), ("garbage", None), ("incomplete", None),
                            ("reply", "I cannot help with that request."),
                            ("reply", "INCOMPLETE: none of the files in the codebase could be opened"),
                            ("reply", "No unverifiable claims found.\n\nINCOMPLETE: the notes directory was empty"),
                            ("reply", "I could not check any claims because the document is not in the codebase.\n"
                                      "- `runs/real/plan.md` -> not found.")]:
            with self.subTest(mode, reply=reply):
                r = self.verify(plan, mode=mode, FAKE_REPLY=reply)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertFalse(os.path.exists(report), "a refusal reads like a clean bill of health")
                self.assertFalse(os.path.exists(marker), "after.verify fired on a failed check")
        self.assertEqual(self.verify(plan).returncode, 0)
        self.assertEqual(self.verify(plan, mode="refuse").returncode, 4)
        self.assertTrue(os.path.exists(report), "a failed re-check of an unchanged document keeps the report it paid for")
        self.assertEqual(self.verify(plan).returncode, 0, "its own report is Lacuna's to replace")
        write(report, read(report) + "\n- checked by hand: fine\n")
        calls = len(self.calls())
        r = self.verify(plan)
        self.assertEqual((r.returncode, len(self.calls())), (2, calls), "a hand-annotated report was replaced")
        self.assertIn("checked by hand", read(report))
        self.assertEqual(self.verify(plan, "--overwrite").returncode, 0)
        write(plan, read(plan) + "\nA new proposal.\n")
        self.assertEqual(self.verify(plan, mode="refuse").returncode, 4)
        self.assertFalse(os.path.exists(report), "a report must not sit beside a version it never checked")
        victim = write(self.p("victim.md"), "keep me\n")
        os.symlink(victim, report)
        self.assertEqual(self.verify(plan, "--overwrite").returncode, 0)
        self.assertEqual(read(victim), "keep me\n", "--overwrite wrote the report through a symlink")
        os.remove(report)
        os.makedirs(report)                               # where a recorded report was
        calls = len(self.calls())
        for extra in ((), ("--overwrite",)):
            r = self.verify(plan, *extra)
            self.assertEqual((r.returncode, len(self.calls())), (2, calls), r.stdout + r.stderr)

    def test_a_missing_tool_or_an_odd_reply_stays_inside_the_exit_codes(self):
        """0, 2, 3, 4 is the contract a caller branches on. A traceback (exit 1) after a paid call
        is the worst outcome: the money is spent and the caller cannot tell what happened."""
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "content " * 200)
        plan = write(self.p("plan.md"), "# Plan\n" + "content " * 200)
        empty = self.p("empty-bin")
        os.makedirs(empty)
        for stage, argv in [("read", ("read", self.alpha, "--out", self.notes)),
                            ("synth", ("synth", "--notes", self.notes, "--repo", self.repo, "--out", self.p("p2.md"))),
                            ("verify", ("verify", "--target", plan, "--repo", self.repo))]:
            with self.subTest(f"no claude on PATH: {stage}"):
                r = self.lacuna("lit", *argv, prelude="", PATH=empty)
                self.assertEqual(r.returncode, 4, r.stderr)
                self.assertIn("could not run claude", r.stdout + r.stderr)
        r = self.lacuna("lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out", self.p("p3.md"),
                        prelude="", PATH=self.bin)
        self.assertEqual(r.returncode, 0, f"no git on PATH: {r.stderr}")
        self.assertIn(os.path.join("src", "widget", "sampler.py"), read(self.p("p3.md.filelist.txt")))
        r = self.read(mode="notdict")
        self.assertEqual(r.returncode, 0, f"a reply that is JSON but not an object: {r.stderr}")
        self.assertEqual(len(self.cached_notes()), 1, "the note it wrote is checked and cached like any other")
        self.assertEqual(self.ledger()[-1]["stage"], "read")


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
            (True, "could not run"),                        # config.json is hand-edited
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
        self.assertEqual(ls(self.notes), ["smith2024.md"], "the note is named after the PDF, no hash")
        self.read_pdf()
        self.assertEqual(len(self.parse_lines()), 3, "an unchanged document is not parsed again")
        cache = os.path.join(self.data, "cache", "md")
        default = next(f for f in os.listdir(cache) if not f.endswith(("_docling.md", "_marker.md")))
        open(os.path.join(cache, default), "w").close()       # what an interrupted parse leaves
        self.assertEqual(self.read_pdf().returncode, 0)
        self.assertEqual(len(self.parse_lines()), 4, "an empty parse must be redone, not handed to a reader")
        self.read_pdf(prelude="L.PARSE_VERSION = '999'")
        self.assertEqual(len(self.parse_lines()), 5, "a change to clean()/validate() must reach parsed documents")
        nm = os.pathconf(self.tmp, "PC_NAME_MAX")
        long_pdf = write(self.p("x" * (nm - 4) + ".pdf"), "%PDF-1.4 not really a pdf")
        for _ in range(2):
            r = self.lacuna("lit", "read", long_pdf, "--out", self.notes, LACUNA_PYTHON=self.venv_py)
            self.assertEqual(r.returncode, 0, f"a PDF whose name fits must parse: {r.stderr[-300:]}")
        self.assertEqual(len(self.parse_lines()), 6, "a long name is parsed once, then served from the cache")

    def test_equations_needs_only_the_marker_venv(self):
        """`doctor --install-equations` makes venv-marker and nothing else; the README recipe then runs
        `read --equations`. prep_md.py is standard-library code, so a missing default venv is no reason
        to refuse, though a PDF read without it must say its truncation check is off."""
        marker_py = write(os.path.join(self.data, "venv-marker", "bin", "python"), "#!/bin/sh\n", 0o755)
        gone = self.p("no-such-venv", "python")
        # sys.executable is what prep_md runs under when there is no venv: the fake stands in for it
        prelude = f"sys.executable = {self.venv_py!r}"
        for extra in (("--equations",), ("--parser", "marker"), ("--parser", "docling")):
            with self.subTest(extra):
                r = self.read_pdf(*extra, LACUNA_PYTHON=gone, prelude=prelude)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("truncation", r.stderr)
        self.assertEqual(len(self.parse_lines()), 2, "--equations and --parser marker share one cached parse")
        r = self.read_pdf(LACUNA_PYTHON=gone, prelude=prelude)
        self.assertEqual((r.returncode, len(self.parse_lines())), (3, 2), "the default parser still needs its venv")
        self.assertTrue(os.path.exists(marker_py))

    def test_parse_failures_exit_3_before_any_model_call(self):
        r = self.read_pdf(LACUNA_PYTHON=self.p("no-such-venv", "python"))
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("doctor", r.stderr)
        r = self.read_pdf(FAKE_PARSE_FAIL="1")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("could not parse", r.stderr)
        self.assertEqual(self.calls(), [])


class TestConcurrentRuns(Pipeline):
    """One run writes a given note, plan or report at a time, from its ownership check to its final
    write, and nothing a run leaves behind blocks the next.

    0.3.1 reserved a note path in the record and released it only on success, so a refused reader,
    Ctrl-C or a kill refused every later read of a same-named document for good, --overwrite
    included. A re-read dropped its own reservation before its model call, and the same document
    under another --brief was let through: two agents wrote one path, and a cache key could hold
    another document's note. Two synths into one --out deleted each other's work."""

    def test_a_path_another_run_holds_is_refused_before_paying(self):
        os.makedirs(self.notes)
        write(os.path.join(self.notes, "n.md"), "# n\n" + "content " * 200)
        plan, target = self.p("lit", "plan.md"), write(self.p("lit", "t.md"), "# T\n" + "content " * 200)
        self.addCleanup(L.release_all)
        for path in (os.path.join(self.notes, "alpha.md"), plan, target + ".unverified.md"):
            self.assertIsNone(L.hold(path, "testing"))     # this test process is the other run
        read_ = ("lit", "read", self.alpha, "--out", self.notes)
        synth = ("lit", "synth", "--notes", self.notes, "--repo", self.repo, "--out", plan)
        verify = ("lit", "verify", "--target", target, "--repo", self.repo)
        for label, argv in [("read", read_), ("read --overwrite", read_ + ("--overwrite",)),
                            ("the same document under another --brief", read_ + ("--brief", "BRIEF:other")),
                            ("synth", synth), ("synth --overwrite", synth + ("--overwrite",)),
                            ("verify", verify), ("verify --overwrite", verify + ("--overwrite",))]:
            with self.subTest(label):
                r = self.lacuna(*argv)
                self.assertEqual((r.returncode, len(self.calls())), (2, 0), r.stdout + r.stderr)
                self.assertIn(f"another Lacuna run (pid {os.getpid()} ", r.stderr)
        L.release_all()
        for argv in (read_, synth, verify):
            self.assertEqual(self.lacuna(*argv).returncode, 0)
        self.assertEqual([f for d in (self.notes, self.p("lit")) for f in os.listdir(d) if f.endswith(".lock")], [],
                         "a run that has ended holds nothing")

    def test_nothing_a_run_leaves_behind_blocks_the_next(self):
        other = write(self.p("elsewhere", "alpha.md"), "# Another alpha\n\n## Abstract\n\n" + "Other. " * 300)

        def later(out):
            r = self.lacuna("lit", "read", other, "--out", out)
            self.assertEqual(r.returncode, 0, f"{out}: {r.stderr}")

        out = self.p("refused")
        self.assertEqual(self.lacuna("lit", "read", self.alpha, "--out", out, mode="refuse").returncode, 4)
        later(out)
        out = self.p("collision")
        self.assertEqual(self.lacuna("lit", "read", self.alpha, other, "--out", out).returncode, 2)
        later(out)
        out, calls = self.p("killed"), len(self.calls())
        killed = subprocess.Popen([LACUNA, "lit", "read", self.alpha, "--out", out], env=self.env(FAKE_MODE="hang"),
                                  cwd=self.tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        deadline = time.time() + 20
        while len(self.calls()) == calls and time.time() < deadline:     # its reader is running
            time.sleep(0.05)
        os.killpg(killed.pid, signal.SIGKILL)
        killed.wait()
        later(out)
        # 0.3.1 left reservations in the record, and the half-written note of a run killed meanwhile
        old = self.p("old")
        write(os.path.join(old, ".lacuna.json"), json.dumps(
            {"files": {"alpha.md": {"name": "alpha.md", "pending": True, "source": self.alpha}}}))
        later(old)
        write(os.path.join(old, ".lacuna.json"), json.dumps(
            {"files": {"alpha.md": {"name": "alpha.md", "pending": True, "source": self.alpha}}}))
        write(os.path.join(old, "alpha.md"), "# half a note\n")
        r = self.lacuna("lit", "read", self.alpha, "--out", old)
        self.assertEqual(r.returncode, 2)
        self.assertIn("has no record", r.stderr, "the note a killed run left was called edited")

    def test_two_runs_at_once_one_wins_and_the_other_is_refused_before_paying(self):
        """End to end, with real timing: the deterministic tests above are the guard."""
        a = write(self.p("a", "main.md"), "Title A\n\n" + "body " * 300)
        b = write(self.p("b", "main.md"), "Title B\n\n" + "body " * 300)
        out = self.p("notes")
        os.makedirs(out, exist_ok=True)
        env = self.env(FAKE_DELAY="2")
        procs = [subprocess.Popen([LACUNA, "lit", "read", doc, "--out", out],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for doc in (a, b)]
        outs = [p.communicate() for p in procs]
        codes = sorted(p.returncode for p in procs)
        self.assertEqual(codes, [0, 2], f"one must win and one must be refused:\n{outs}")
        note = read(os.path.join(out, "main.md"))
        self.assertGreater(len(note), 400, "the winner's note must be complete")
        keys = [f for f in os.listdir(os.path.join(self.data, "cache", "notes")) if f.endswith(".md")]
        self.assertLessEqual(len(keys), 1, f"only the winner may cache a note, got {keys}")
        for k in keys:
            cached = read(os.path.join(self.data, "cache", "notes", k))
            self.assertEqual(cached, note, "a cache key must hold its own document's note")


if __name__ == "__main__":
    unittest.main(verbosity=2)

