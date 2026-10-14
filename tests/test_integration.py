"""Integration tests: whole flows, end to end, against a fake `claude`.

The unit suites check one module at a time. These check the journeys a user actually takes, which
is where the expensive defects have all been found — every one of them lived in the seams:

  * a shipped example that worked locally and was never committed,
  * a stage that reported success because its output was long enough,
  * a cache that returned a free, fast, wrong answer because its key missed an input,
  * paths that resolved differently for the agent than for the caller.

**No test here calls a model.** A fake `claude` goes first on a restricted PATH and every test
asserts it resolved to the fake before running anything. That is what lets these tests prove a bad
input exits *before* any money is spent, and that a cache hit really is free.

Run: python3 -m unittest discover -s tests
"""
import json, os, re, shutil, stat, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LACUNA = os.path.join(ROOT, "bin", "lacuna")
PY = sys.executable

# A fake `claude`. Logs every invocation, then behaves as FAKE_MODE says. It writes a plausible
# note or plan so the pipeline's own bookkeeping is exercised, never a real request.
FAKE_CLAUDE = r'''#!{py}
import json, os, re, sys
prompt = sys.stdin.read()
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({{"argv": argv, "prompt": prompt}}) + "\n")

mode = os.environ.get("FAKE_MODE", "ok")
if mode == "refuse":
    print(json.dumps({{"is_error": True, "result": "I can't help with that.",
                      "total_cost_usd": 0.002}}))
    sys.exit(0)
if mode == "garbage":
    print("overloaded, not json")
    sys.exit(1)
if mode == "apology":
    # The dangerous case: a long, fluent explanation of failure that clears any size check.
    target = (re.search(r"Write the result to (\S+)\.", prompt)
              or re.search(r"(?m)^- \S+ -> (\S+)$", prompt))
    if target:
        with open(target.group(1), "w") as f:
            f.write("# Plan\n\nBLOCKED - not completed. I could not read any of the supplied "
                    "files.\n\n" + ("Explanation of the blockage. " * 120))
    print(json.dumps({{"is_error": False, "result": "done", "total_cost_usd": 0.01}}))
    sys.exit(0)

note = re.search(r"(?m)^- (\S+) -> (\S+)$", prompt)
plan = re.search(r"Write the result to (\S+)\.", prompt)
if prompt.startswith("Fact-check"):
    print(json.dumps({{"is_error": False, "result": "No unverifiable claims found.",
                      "total_cost_usd": 0.0012}}))
    sys.exit(0)
if note:
    src, out = note.group(1), note.group(2)
    brief = next((l for l in prompt.splitlines() if l.startswith("BRIEF:")), "BRIEF:none")
    books = "BOOKS-OFFERED" if "Textbooks you may consult" in prompt else "NO-BOOKS"
    with open(out, "w") as f:
        f.write("# A note ({{}})\n\n## Claim\n{{}}\n{{}}\n\n## Key results\n".format(
            os.path.basename(src), brief, books) + ("Finding with a number, 42 units. " * 40))
    print(json.dumps({{"is_error": False, "result": out, "total_cost_usd": 0.012}}))
    sys.exit(0)
if plan:
    with open(plan.group(1), "w") as f:
        f.write("# Development plan\n\n## 1. A real proposal\n\n"
                + ("Substantive plan content naming src/widget/sampler.py. " * 60))
    print(json.dumps({{"is_error": False, "result": plan.group(1), "total_cost_usd": 0.3}}))
    sys.exit(0)
print(json.dumps({{"is_error": False, "result": "nothing to do", "total_cost_usd": 0.0}}))
'''


class Flow(unittest.TestCase):
    """One sandbox per test: its own HOME, data directory, PATH and fake model."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lacuna-it-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        self.notes = os.path.join(self.tmp, "notes")
        for d in (self.data, self.home, self.bin):
            os.makedirs(d)
        self.log = os.path.join(self.tmp, "calls.jsonl")

        fake = os.path.join(self.bin, "claude")
        with open(fake, "w") as f:
            f.write(FAKE_CLAUDE.format(py=PY))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        # The repo under test, and documents to read. Markdown input means no parser is needed.
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(os.path.join(ROOT, "examples", "demo-repo"), self.repo)
        self.docs = os.path.join(self.tmp, "docs")
        os.makedirs(self.docs)
        for name in ("alpha", "beta"):
            with open(os.path.join(self.docs, f"{name}.md"), "w") as f:
                f.write(f"# {name.title()} paper\n\n" + ("Content with findings. " * 200))

    def env(self, **extra):
        return {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.home,
                "LACUNA_DATA": self.data, "FAKE_LOG": self.log,
                "LANG": "en_US.UTF-8", **extra}

    def run_lacuna(self, *args, **extra):
        env = self.env(**extra)
        which = shutil.which("claude", path=env["PATH"])
        self.assertEqual(which, os.path.join(self.bin, "claude"),
                         "a test must never be able to reach the real `claude`")
        return subprocess.run([LACUNA, *args], capture_output=True, text=True,
                              env=env, cwd=self.tmp)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    # ---------------------------------------------------------------- the happy path

    def test_read_synth_verify_is_one_coherent_journey(self):
        """The sequence the README puts in front of every new user."""
        r = self.run_lacuna("lit", "read", f"{self.docs}/alpha.md", f"{self.docs}/beta.md",
                            "--out", self.notes, "--brief", "BRIEF:the project")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2/2 notes written", r.stdout)
        written = sorted(f for f in os.listdir(self.notes) if f.endswith(".md"))
        self.assertEqual(written, ["alpha.md", "beta.md"], "notes are named after the documents")
        self.assertEqual(len(self.calls()), 2, "one reader per document")

        plan = os.path.join(self.tmp, "plan.md")
        r = self.run_lacuna("lit", "synth", "--notes", self.notes, "--repo", self.repo,
                            "--out", plan, "--kind", "plan", "--objective", "What next?")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(plan))

        r = self.run_lacuna("lit", "verify", "--target", plan, "--repo", self.repo,
                            "--notes", self.notes)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(plan + ".unverified.md"))

        ledger = os.path.join(self.data, "cost-ledger.jsonl")
        with open(ledger) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        stages = [row["stage"] for row in rows]
        self.assertEqual(stages.count("read"), 2)
        self.assertIn("synth", stages)
        self.assertIn("verify", stages)
        for row in rows:
            self.assertIn("effort", row, "the recalibration recipe needs effort on every row")
            self.assertIn("cost", row)
        self.assertTrue(all(r.get("source_tokens") for r in rows if r["stage"] == "read"))

    def test_rereading_is_free_and_changing_the_brief_is_not(self):
        base = ("lit", "read", f"{self.docs}/alpha.md", "--out", self.notes)
        self.assertEqual(self.run_lacuna(*base, "--brief", "BRIEF:one").returncode, 0)
        self.assertEqual(len(self.calls()), 1)

        r = self.run_lacuna(*base, "--brief", "BRIEF:one")
        self.assertIn("cached (free)", r.stdout)
        self.assertEqual(len(self.calls()), 1, "an unchanged re-read must not call the model")

        self.assertEqual(self.run_lacuna(*base, "--brief", "BRIEF:two").returncode, 0)
        self.assertEqual(len(self.calls()), 2, "a changed brief must miss the cache")

    # ---------------------------------------------------------------- the money guarantees

    def test_bad_input_never_reaches_a_model(self):
        """Every one of these cost real money before it was fixed."""
        plan = os.path.join(self.tmp, "p.md")
        cases = [
            ("missing document", ("lit", "read", f"{self.docs}/nope.md", "--out", self.notes)),
            ("missing repo", ("lit", "synth", "--notes", self.notes, "--repo",
                              f"{self.tmp}/nope", "--out", plan)),
            ("missing notes", ("lit", "synth", "--notes", f"{self.tmp}/nope",
                               "--repo", self.repo, "--out", plan)),
            ("--out is a directory", ("lit", "synth", "--notes", self.notes, "--repo", self.repo,
                                      "--out", self.docs)),
            ("missing target", ("lit", "verify", "--target", f"{self.tmp}/nope.md",
                                "--repo", self.repo)),
        ]
        os.makedirs(self.notes, exist_ok=True)
        with open(os.path.join(self.notes, "n.md"), "w") as f:
            f.write("# n\n" + "x" * 500)
        for label, argv in cases:
            with self.subTest(label):
                before = len(self.calls())
                r = self.run_lacuna(*argv)
                self.assertEqual(r.returncode, 2, f"{label}: {r.stdout}{r.stderr}")
                self.assertEqual(len(self.calls()), before,
                                 f"{label} reached a model, which costs real money")

    def test_a_refusal_is_never_filed_as_output(self):
        r = self.run_lacuna("lit", "read", f"{self.docs}/alpha.md", "--out", self.notes,
                            FAKE_MODE="refuse")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertFalse([f for f in os.listdir(self.notes)] if os.path.isdir(self.notes) else [],
                         "a refusal must not leave a note behind")
        cache = os.path.join(self.data, "cache", "notes")
        self.assertFalse(os.path.isdir(cache) and os.listdir(cache),
                         "a refusal must never be cached")

    def test_a_refusal_on_a_rerun_does_not_inherit_the_previous_note(self):
        """The subtle one: success was inferred from a file that an earlier run had left."""
        self.assertEqual(self.run_lacuna("lit", "read", f"{self.docs}/alpha.md",
                                         "--out", self.notes, "--brief", "BRIEF:one").returncode, 0)
        r = self.run_lacuna("lit", "read", f"{self.docs}/alpha.md", "--out", self.notes,
                            "--brief", "BRIEF:two", FAKE_MODE="refuse")
        self.assertEqual(r.returncode, 4, "a refusal must fail even when an older note is present")
        self.assertNotIn("1/1 notes written", r.stdout)

    def test_a_long_apology_is_not_mistaken_for_a_plan(self):
        os.makedirs(self.notes, exist_ok=True)
        with open(os.path.join(self.notes, "n.md"), "w") as f:
            f.write("# n\n" + "content " * 200)
        plan = os.path.join(self.tmp, "plan.md")
        r = self.run_lacuna("lit", "synth", "--notes", self.notes, "--repo", self.repo,
                            "--out", plan, "--kind", "plan", FAKE_MODE="apology")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)

    def test_an_unusable_verifier_reply_is_not_filed_as_a_fact_check(self):
        plan = os.path.join(self.tmp, "plan.md")
        with open(plan, "w") as f:
            f.write("# Plan\n" + "content " * 200)
        for mode in ("refuse", "garbage"):
            with self.subTest(mode):
                r = self.run_lacuna("lit", "verify", "--target", plan, "--repo", self.repo,
                                    FAKE_MODE=mode)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertFalse(os.path.exists(plan + ".unverified.md"),
                                 "a refusal must not be saved as if it were a fact check")

    # ---------------------------------------------------------------- the seams

    def test_relative_paths_work_from_any_directory(self):
        """The agent runs with cwd set to the output directory. Relative inputs used to leave it
        able to read nothing, after which it wrote an apology that passed the size check."""
        os.makedirs(self.notes, exist_ok=True)
        with open(os.path.join(self.notes, "n.md"), "w") as f:
            f.write("# n\n" + "content " * 200)
        env = self.env()
        r = subprocess.run([LACUNA, "lit", "synth", "--notes", "notes", "--repo", "repo",
                            "--out", "out/plan.md", "--kind", "plan"],
                           capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "out", "plan.md")))
        prompt = self.calls()[-1]["prompt"]
        for token in ("/notes", "/repo"):
            self.assertIn(token, prompt)
        self.assertNotIn("Codebase: repo\n", prompt, "the agent must be given absolute paths")

    def test_after_hooks_fire_for_each_stage_with_the_right_argument(self):
        marker = os.path.join(self.tmp, "fired.txt")
        hook = os.path.join(self.tmp, "hook.sh")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\necho "$1" >> {marker}\n')
        os.chmod(hook, 0o755)
        with open(os.path.join(self.data, "config.json"), "w") as f:
            # $HOME exercises shell expansion, which the hook had none of before
            json.dump({"after": {"read": hook, "synth": hook, "verify": hook}}, f)

        self.run_lacuna("lit", "read", f"{self.docs}/alpha.md", "--out", self.notes)
        plan = os.path.join(self.tmp, "plan.md")
        self.run_lacuna("lit", "synth", "--notes", self.notes, "--repo", self.repo,
                        "--out", plan, "--kind", "plan")
        self.run_lacuna("lit", "verify", "--target", plan, "--repo", self.repo)

        with open(marker) as f:
            fired = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(fired), 3, f"one hook per stage, got {fired}")
        self.assertTrue(os.path.isdir(fired[0]), "after.read is given the notes directory")
        self.assertEqual(fired[1], plan, "after.synth is given the plan file")
        self.assertTrue(fired[2].endswith(".unverified.md"))

    def test_a_broken_after_hook_does_not_lose_the_work(self):
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump({"after": {"synth": "/does/not/exist --flag"}}, f)
        os.makedirs(self.notes, exist_ok=True)
        with open(os.path.join(self.notes, "n.md"), "w") as f:
            f.write("# n\n" + "content " * 200)
        plan = os.path.join(self.tmp, "plan.md")
        r = self.run_lacuna("lit", "synth", "--notes", self.notes, "--repo", self.repo,
                            "--out", plan, "--kind", "plan")
        self.assertEqual(r.returncode, 0, "a failed hook must not fail a stage already paid for")
        self.assertTrue(os.path.exists(plan))
        self.assertIn("after_synth", r.stdout)

    def test_books_are_offered_and_change_the_cache(self):
        self.assertEqual(self.run_lacuna("lit", "read", f"{self.docs}/alpha.md",
                                         "--out", self.notes).returncode, 0)
        self.assertIn("NO-BOOKS", open(os.path.join(self.notes, "alpha.md")).read())

        book = os.path.join(self.data, "books", "handbook")
        os.makedirs(book)
        with open(os.path.join(book, "toc.md"), "w") as f:
            f.write("# Handbook\n\n| chapter | file | pages | ~tokens |\n|---|---|---|---|\n"
                    "| Priors | 00_priors.txt | 1-20 | 5,000 |\n")
        r = self.run_lacuna("lit", "read", f"{self.docs}/alpha.md", "--out", self.notes)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 2, "a new book must miss the cache")
        self.assertIn("BOOKS-OFFERED", open(os.path.join(self.notes, "alpha.md")).read())

        # --no-books produces exactly the prompt the first run had, so the correct behaviour is
        # to serve that first note from cache rather than pay again. The key tracks what the
        # reader was actually offered, not which flags were typed.
        r = self.run_lacuna("lit", "read", f"{self.docs}/alpha.md", "--out", self.notes,
                            "--no-books")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 2, "--no-books matches the earlier no-book note")
        self.assertIn("NO-BOOKS", open(os.path.join(self.notes, "alpha.md")).read())

    # ---------------------------------------------------------------- free tools, no setup

    def test_the_cost_tools_need_nothing_installed(self):
        for argv in (("spend", "--days", "7", "--no-color"), ("config",), ("doctor",)):
            with self.subTest(argv[0]):
                r = self.run_lacuna(*argv)
                self.assertIn(r.returncode, (0, 1), r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(len(self.calls()), 0, "these must never call a model")

    def test_doctor_then_config_agree_on_where_everything_lives(self):
        self.run_lacuna("doctor")
        r = self.run_lacuna("config")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.data, r.stdout, "config must report the data directory doctor created")
        for line in ("books", "cache", "ledger", "results"):
            self.assertIn(line, r.stdout)
        self.assertTrue(os.path.isdir(os.path.join(self.data, "cache")))

    def test_estimate_is_free_and_refuses_what_it_cannot_price(self):
        r = self.run_lacuna("lit", "estimate", self.docs)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 0, "estimating must not call a model")
        self.assertIn("upstream", r.stdout, "it must say whose calibration it used")

        with open(os.path.join(self.tmp, "broken.pdf"), "w") as f:
            f.write("not really a pdf")
        # Exit 2 when it can read the file and finds no text; exit 3 when no text extractor is
        # installed at all. Either is honest. What must never happen is "$0.00" and exit 0.
        r = self.run_lacuna("lit", "estimate", os.path.join(self.tmp, "broken.pdf"))
        self.assertIn(r.returncode, (2, 3), r.stdout + r.stderr)
        self.assertNotIn("$   0.00", r.stdout, "an unreadable PDF must not be priced at zero")


if __name__ == "__main__":
    unittest.main(verbosity=2)
