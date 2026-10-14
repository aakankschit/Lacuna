"""Tests for the parts of lacuna that test_lacuna.py does not reach.

No third-party dependencies, no network, no model calls: `python3 -m unittest discover -s tests`.

The pipeline shells out to `claude -p`. Every test that drives it puts a fake `claude` first on a
PATH that holds nothing else but /usr/bin and /bin, and asserts that the fake is what resolves, so
a real model is never reached. The fake records each call, which is how these tests prove that a
bad input exits *before* money is spent and that a cache hit really is free.

Several tests here document known defects and are expected to fail until the code is fixed. Each
says so in its docstring ("KNOWN DEFECT n"); they assert the correct behaviour, never the current one.
"""
import json, os, re, shutil, stat, subprocess, sys, tempfile, time, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIT = os.path.join(ROOT, "scripts", "lit")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "cost"))
sys.path.insert(0, LIT)

PY = sys.executable
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SAFE_PATH = "/usr/bin:/bin"   # nothing user-installed, so no real `claude` can be found


def strip_ansi(s):
    return ANSI.sub("", s)


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)
    return path


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_env(home, data, **extra):
    """A clean environment: no LACUNA_* or CLAUDE_* leaking in from the machine running the tests."""
    env = {"PATH": SAFE_PATH, "HOME": home, "LACUNA_DATA": data, "PYTHONIOENCODING": "utf-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


class TempCase(unittest.TestCase):
    """A temp directory with a fake home and data directory per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lacuna-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.home)
        os.makedirs(self.data)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def run_py(self, argv, env_extra=None, cwd=None, stdin=None, input=None):
        return subprocess.run([PY, *argv], capture_output=True, text=True, encoding="utf-8",
                              env=base_env(self.home, self.data, **(env_extra or {})),
                              cwd=cwd or self.tmp, input=input,
                              stdin=None if input is not None else (stdin or subprocess.DEVNULL))


# --------------------------------------------------------------------------------------------
# The fake model. It never contacts anything: it logs the call and does what FAKE_CLAUDE_MODE says.
# --------------------------------------------------------------------------------------------

FAKE_CLAUDE = r'''#!{py}
import json, os, re, sys
prompt = sys.stdin.read()
argv = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({{"argv": argv, "prompt": prompt}}) + "\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "write")
if mode == "garbage":
    print("Error: overloaded; this is not JSON")
    sys.exit(1)
if mode == "refuse":
    print(json.dumps({{"is_error": True, "result": "I can't help with that.", "total_cost_usd": 0.0021}}))
    sys.exit(0)
model = argv[argv.index("--model") + 1]
if prompt.startswith("Fact-check"):
    print(json.dumps({{"is_error": False, "result": "No unverifiable claims found.", "total_cost_usd": 0.001}}))
    sys.exit(0)
m = re.search(r"(?m)^- \S+ -> (\S+)$", prompt) or re.search(r"Write the result to (\S+)\. Under", prompt)
out = m.group(1)
briefs = [l for l in prompt.splitlines() if l.startswith("BRIEF:")]
with open(out, "w") as f:
    f.write("# Note\n\n" + "\n".join(briefs) + "\nmodel=" + model + "\n"
            + "A faithful sentence about the paper, with a location. " * 30)
print(json.dumps({{"is_error": False, "result": out, "total_cost_usd": 0.0123}}))
'''

# Stands in for the parser venv's python: `<venv-python> -I prep_md.py <pdf> <md> [flags]`.
FAKE_PARSER_PY = r'''#!/bin/sh
echo "$@" >> "$FAKE_PARSE_LOG"
if [ -n "$FAKE_PARSE_FAIL" ]; then echo "parse exploded" >&2; exit 1; fi
printf '# Parsed\n\n## Abstract\n\nParsed body.\n' > "$4"
'''


class PipelineCase(TempCase):
    """Runs lit_pipeline against the fake model, in a throwaway data directory."""

    def setUp(self):
        super().setUp()
        self.bin = self.p("bin")
        self.fake = write(os.path.join(self.bin, "claude"), FAKE_CLAUDE.format(py=PY), 0o755)
        self.log = self.p("claude.log")
        self.parse_log = self.p("parse.log")
        self.path = f"{self.bin}:{SAFE_PATH}"
        # the budget rule, enforced: the only `claude` this test can reach is the fake
        self.assertEqual(shutil.which("claude", path=self.path), self.fake)

    def pipeline(self, *args, mode="write", prelude="", env=None):
        code = (f"import sys\nsys.path.insert(0, {LIT!r})\nimport lit_pipeline as L\n{prelude}\n"
                f"sys.argv = ['lit_pipeline.py'] + {list(args)!r}\nL.main()\n")
        extra = {"PATH": self.path, "FAKE_CLAUDE_LOG": self.log, "FAKE_CLAUDE_MODE": mode,
                 "FAKE_PARSE_LOG": self.parse_log, **(env or {})}
        return self.run_py(["-c", code], extra)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def config(self, cfg):
        write(os.path.join(self.data, "config.json"), json.dumps(cfg))

    def hook(self, name):
        """A hook script that records its arguments, one per line, in <name>.args."""
        marker = self.p(f"{name}.args")
        script = write(self.p(f"{name}.sh"), f'#!/bin/sh\nprintf "%s\\n" "$@" > "{marker}"\n', 0o755)
        return script, marker

    def read_text(self, path):
        with open(path) as f:
            return f.read()


class TestNoteCacheBehaviour(PipelineCase):
    """The note cache is where money is saved or wasted. These drive `read` end to end against a
    fake model and count the calls, rather than reading the key expression as text."""

    def setUp(self):
        super().setUp()
        self.paper = write(self.p("papers", "smith2024.md"), "# Smith 2024\n\n## Abstract\n\nText.\n")
        self.notes = self.p("notes")
        self.cached = os.path.join(self.data, "cache", "notes")

    def read(self, *extra, mode="write", prelude="", out=None):
        return self.pipeline("read", self.paper, "--out", out or self.notes, *extra, mode=mode, prelude=prelude)

    def test_identical_rerun_is_free_and_reaches_a_new_out_dir(self):
        r = self.read("--brief", "BRIEF:alpha")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 1)
        other = self.p("elsewhere")
        r = self.read("--brief", "BRIEF:alpha", out=other)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 1, "an unchanged rerun must not call the model again")
        self.assertIn("cached (free)", r.stdout)
        self.assertEqual(self.read_text(os.path.join(other, "smith2024.md")),
                         self.read_text(os.path.join(self.notes, "smith2024.md")))

    def test_each_key_input_misses_the_cache(self):
        """brief, model, effort and prompt version each change what a note says; each must miss.

        `--no-books` is deliberately NOT in this list: with no books installed it changes nothing
        about the prompt, so serving the cache is correct. Which books exist is covered by
        test_adding_or_removing_a_book_misses_the_cache.
        """
        steps = [
            ((), "", 1),                                        # first read
            ((), "", 1),                                        # identical: hit
            (("--brief", "BRIEF:other"), "", 2),                # new brief
            (("--model", "sonnet"), "", 3),                     # new model
            ((), "L.PROMPT_VERSION = '999'", 4),                # prompt template bumped
            ((), "", 4),                                        # back to the first: still cached
        ]
        for extra, prelude, expect in steps:
            with self.subTest(extra=extra, prelude=prelude):
                r = self.read(*extra, prelude=prelude)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(len(self.calls()), expect, r.stdout)
        models = [c["argv"][c["argv"].index("--model") + 1] for c in self.calls()]
        self.assertEqual(models, ["haiku", "haiku", "sonnet", "haiku"],
                         "--model must reach the model for that run only")

    def test_adding_or_removing_a_book_misses_the_cache(self):
        """Found end to end: after deleting a book, the old note came back as "cached (free)"
        still citing it, because the key recorded only whether books were on, never which."""
        books = os.path.join(self.data, "books", "handbook")
        os.makedirs(books, exist_ok=True)

        r = self.read()                                  # 1: no books
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 1)

        with open(os.path.join(books, "toc.md"), "w") as f:
            f.write("# Handbook\n\n| chapter | file | pages | ~tokens |\n|---|---|---|---|\n"
                    "| Priors | 00_priors.txt | 1-20 | 5,000 |\n")
        r = self.read()                                  # 2: a book appeared
        self.assertEqual(len(self.calls()), 2, f"adding a book must miss the cache\n{r.stdout}")

        os.remove(os.path.join(books, "toc.md"))
        r = self.read()                                  # 3: back to none -> the first key again
        self.assertEqual(len(self.calls()), 2,
                         f"removing it must return to the no-book note, not a new call\n{r.stdout}")
        self.assertIn("cached (free)", r.stdout)

    def test_refusal_on_a_fresh_dir_exits_4_and_caches_nothing(self):
        r = self.read(mode="refuse")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertFalse(os.listdir(self.cached) if os.path.isdir(self.cached) else [])

    def test_refusal_on_a_rerun_is_not_reported_as_success(self):
        """KNOWN DEFECT 3: success is judged by `out` existing, and `out` can be last run's note.
        A refusal must exit 4 and must not cache the stale note under the new key."""
        write(os.path.join(self.notes, "smith2024.md"), "# STALE note from an earlier run\n" + "x" * 600)
        r = self.read("--brief", "BRIEF:new", mode="refuse")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(r.returncode, 4, "a refusal was reported as success:\n" + r.stdout)
        self.assertEqual(os.listdir(self.cached) if os.path.isdir(self.cached) else [], [],
                         "the stale note was cached under the new key")

    def test_cache_hit_restores_the_cached_note_over_a_different_one(self):
        """KNOWN DEFECT 3: a cache hit only copies when `out` is absent, so `out` keeps a note
        written under different assumptions while the run reports the cached one."""
        self.assertEqual(self.read("--brief", "BRIEF:alpha").returncode, 0)
        self.assertEqual(self.read("--brief", "BRIEF:beta").returncode, 0)
        r = self.read("--brief", "BRIEF:alpha")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 2, "the third run should have been a cache hit")
        note = self.read_text(os.path.join(self.notes, "smith2024.md"))
        self.assertIn("BRIEF:alpha", note, "out still holds the note written for a different brief")


class TestPdfPlumbing(PipelineCase):
    """PDF input goes through a parser interpreter; its flags, cache names and failures matter."""

    def setUp(self):
        super().setUp()
        self.venv_py = write(self.p("venv", "python"), FAKE_PARSER_PY, 0o755)
        with open(self.p("smith2024.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 not really a pdf")
        self.pdf = self.p("smith2024.pdf")
        self.notes = self.p("notes")

    def read(self, *extra, env=None):
        return self.pipeline("read", self.pdf, "--out", self.notes, *extra,
                             env={"LACUNA_PYTHON": self.venv_py, **(env or {})})

    def parse_lines(self):
        with open(self.parse_log) as f:
            return [l.split() for l in f if l.strip()]

    def test_parser_flags_reach_the_parser_and_tag_the_cache(self):
        for extra in ((), ("--parser", "docling"), ("--equations",)):
            r = self.read(*extra)
            self.assertEqual(r.returncode, 0, r.stderr)
        default, docling, marker = self.parse_lines()
        self.assertNotIn("--parser", default)
        self.assertNotIn("--equations", default)
        self.assertEqual(docling[-2:], ["--parser", "docling"])
        self.assertIn("--equations", marker)
        self.assertTrue(docling[3].endswith("_docling.md"), docling[3])
        self.assertTrue(marker[3].endswith("_marker.md"), marker[3])
        self.assertEqual(len(os.listdir(os.path.join(self.data, "cache", "md"))), 3,
                         "three parsers must give three cache entries, not collide")

    def test_missing_parser_interpreter_exits_3_before_any_model_call(self):
        r = self.read(env={"LACUNA_PYTHON": self.p("no-such-venv", "python")})
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("doctor", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_failed_parse_exits_3_before_any_model_call(self):
        r = self.read(env={"FAKE_PARSE_FAIL": "1"})
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("could not parse", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_note_from_a_pdf_is_named_after_the_pdf(self):
        """KNOWN DEFECT 10: the stem strip uses the markdown hash, the filename the PDF-bytes hash,
        so the note comes out as smith2024_<hash>.md."""
        r = self.read()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(self.notes)), ["smith2024.md"])


class TestSynthAndVerifyInputs(PipelineCase):
    """A typo in a path must cost nothing: exit 2 before the model is called."""

    def setUp(self):
        super().setUp()
        self.notes = self.p("notes")
        write(os.path.join(self.notes, "a.md"), "# A note\n" + "n" * 600)
        self.repo = self.p("repo")
        write(os.path.join(self.repo, "README.md"), "# repo\n")
        self.target = write(self.p("plan.md"), "# Plan\n\nUse src/a.py.\n")

    def test_missing_paths_exit_2_before_any_model_call(self):
        """KNOWN DEFECT 2: --repo, --target and --notes are not checked before the model call."""
        gone = self.p("does-not-exist")
        cases = {
            "synth --repo": ("synth", "--notes", self.notes, "--repo", gone, "--out", self.p("o.md")),
            "verify --target": ("verify", "--target", self.p("missing.md"), "--repo", self.repo),
            "verify --repo": ("verify", "--target", self.target, "--repo", gone),
            "verify --notes": ("verify", "--target", self.target, "--repo", self.repo, "--notes", gone),
        }
        for name, argv in cases.items():
            with self.subTest(name):
                before = len(self.calls())
                r = self.pipeline(*argv)
                self.assertEqual(len(self.calls()), before, f"{name}: the model was called (money spent)")
                self.assertEqual(r.returncode, 2, f"{name}: {r.stdout}{r.stderr}")

    def test_synth_lists_the_repo_without_vcs_noise_and_fires_after(self):
        write(os.path.join(self.repo, "src", "a.py"), "x = 1\n")
        write(os.path.join(self.repo, "node_modules", "dep", "index.js"), "")
        write(os.path.join(self.repo, ".git", "HEAD"), "garbage\n")
        hook, marker = self.hook("after_synth")
        self.config({"after": {"synth": hook}})
        out = self.p("out", "digest.md")
        r = self.pipeline("synth", "--notes", self.notes, "--repo", self.repo, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        files = self.read_text(out + ".filelist.txt").split("\n")
        self.assertIn(os.path.join("src", "a.py"), files)
        self.assertFalse([f for f in files if "node_modules" in f or ".git" in f], files)
        self.assertEqual(self.read_text(marker).split("\n")[0], out)


class TestVerifyOutcome(PipelineCase):
    """A refusal written out as a fact-check reads like a clean bill of health."""

    def setUp(self):
        super().setUp()
        self.repo = self.p("repo")
        write(os.path.join(self.repo, "README.md"), "# repo\n")
        self.target = write(self.p("plan.md"), "# Plan\n\nUse src/a.py.\n")
        self.report = self.target + ".unverified.md"
        self.hook_script, self.marker = self.hook("after_verify")
        self.config({"after": {"verify": self.hook_script}})

    def verify(self, mode):
        return self.pipeline("verify", "--target", self.target, "--repo", self.repo, mode=mode)

    def test_clean_report_is_saved_and_handed_to_the_after_hook(self):
        r = self.verify("write")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No unverifiable claims found.", self.read_text(self.report))
        self.assertEqual(self.read_text(self.marker).split("\n")[0], self.report)

    def assert_failed_cleanly(self, mode):
        r = self.verify(mode)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(r.returncode, 4, f"{mode}: exited {r.returncode}\n{r.stdout}")
        self.assertFalse(os.path.exists(self.marker), f"{mode}: after.verify fired on a failed check")
        if os.path.exists(self.report):
            self.assertFalse(self.read_text(self.report).startswith("# Unverified claims"),
                             f"{mode}: the failure was saved as if it were a fact-check")

    def test_refusal_exits_4_and_is_not_filed(self):
        """KNOWN DEFECT 1: is_error is never checked; a refusal exits 0 and fires after.verify."""
        self.assert_failed_cleanly("refuse")

    def test_non_json_reply_exits_4_and_is_not_filed(self):
        """KNOWN DEFECT 1: a non-JSON reply is saved as the report and exits 0."""
        self.assert_failed_cleanly("garbage")


class TestAfterHookShellSemantics(PipelineCase):
    """Every document calls `after.*` a shell command that receives the output as $1, and the README
    example starts with `~/bin/...`. A hook that silently never runs loses the user's records."""

    def after(self, cmd, env=None):
        self.config({"after": {"synth": cmd}})
        return self.pipeline(env=env, prelude=f"L.after('synth', {self.out!r}); sys.exit(0)")

    def setUp(self):
        super().setUp()
        self.out = self.p("plan.md")

    def test_tilde_in_the_command_is_expanded(self):
        """KNOWN DEFECT 4."""
        marker = self.p("tilde.args")
        write(os.path.join(self.home, "bin", "file-it"), f'#!/bin/sh\nprintf "%s\\n" "$@" > "{marker}"\n', 0o755)
        r = self.after("~/bin/file-it")
        self.assertIn("after_synth: ok", r.stdout, r.stdout)
        self.assertEqual(self.read_text(marker).split("\n")[0], self.out)

    def test_environment_variable_in_the_command_is_expanded(self):
        """KNOWN DEFECT 4."""
        hook, marker = self.hook("var")
        r = self.after("$HOOK_DIR/" + os.path.basename(hook), env={"HOOK_DIR": os.path.dirname(hook)})
        self.assertIn("after_synth: ok", r.stdout, r.stdout)
        self.assertEqual(self.read_text(marker).split("\n")[0], self.out)

    def test_command_with_its_own_arguments_sees_the_output_as_dollar_1(self):
        """KNOWN DEFECT 4: with arguments, the path arrives last and a literal "$1" is passed."""
        hook, marker = self.hook("args")
        r = self.after(f'{hook} "$1" --kind plan')
        self.assertIn("after_synth: ok", r.stdout, r.stdout)
        self.assertEqual(self.read_text(marker).split("\n")[:3], [self.out, "--kind", "plan"])


class TestRepoDiscovery(unittest.TestCase):
    """What the synthesis model is told exists. A file list full of node_modules wastes context;
    the wrong status file means re-proposing what was already rejected."""

    def setUp(self):
        import lit_pipeline
        self.L = lit_pipeline
        self.repo = tempfile.mkdtemp(prefix="lacuna-repo-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def touch(self, rel, text=""):
        return write(os.path.join(self.repo, rel), text)

    def test_walk_files_skips_vcs_caches_build_and_hidden(self):
        for rel in ("README.md", "src/a.py", "docs/b.md", ".git/HEAD", "node_modules/x/i.js",
                    "__pycache__/a.pyc", "src/__pycache__/c.pyc", "build/o.o", "dist/w.whl",
                    ".venv/bin/python", "venv/bin/python", ".hidden/secret", ".env"):
            self.touch(rel)
        self.assertEqual(self.L.walk_files(self.repo), ["README.md", "docs/b.md", "src/a.py"])

    def test_find_status_discovery_order(self):
        self.assertIsNone(self.L.find_status(self.repo))
        order = ["NOTES.md", "docs/STATUS.md", "STATUS.md", "research/STATUS.md"]   # lowest first
        for rel in order:
            self.touch(rel)
            self.assertEqual(self.L.find_status(self.repo), os.path.join(self.repo, rel),
                             f"{rel} should win once it exists")

    def test_explicit_status_is_never_swapped_for_a_discovered_one(self):
        self.touch("research/STATUS.md")
        mine = self.touch("elsewhere/MINE.md")
        self.assertEqual(self.L.find_status(self.repo, mine), mine)
        self.assertIsNone(self.L.find_status(self.repo, os.path.join(self.repo, "typo.md")))

    def test_books_block_offers_only_books_with_a_toc(self):
        books = os.path.join(self.repo, "books")
        write(os.path.join(books, "zeta", "toc.md"), "# Zeta")
        write(os.path.join(books, "alpha", "toc.md"), "# Alpha")
        write(os.path.join(books, "unsplit", "00_ch.txt"), "text")
        with mock.patch.object(self.L, "BOOKS_DIR", books):
            block = self.L.books_block()
        listed = re.findall(r"- index: (\S+)", block)
        self.assertEqual(listed, [os.path.join(books, "alpha", "toc.md"), os.path.join(books, "zeta", "toc.md")])
        for empty in (os.path.join(self.repo, "no-such-dir"), os.path.join(books, "unsplit")):
            with mock.patch.object(self.L, "BOOKS_DIR", empty):
                self.assertEqual(self.L.books_block(), "")


class TestBooksReachTheReader(PipelineCase):
    """--no-books must keep the textbook block out of the prompt, not just out of the cache key."""

    def test_no_books_removes_the_block(self):
        write(self.p("books", "stats", "toc.md"), "# Stats\n")
        paper = write(self.p("papers", "p.md"), "# P\n")
        env = {"LACUNA_BOOKS": self.p("books")}
        self.assertEqual(self.pipeline("read", paper, "--out", self.p("n1"), env=env).returncode, 0)
        self.assertEqual(self.pipeline("read", paper, "--out", self.p("n2"), "--no-books", env=env).returncode, 0)
        with_books, without = self.calls()
        self.assertIn(self.p("books", "stats", "toc.md"), with_books["prompt"])
        self.assertNotIn("toc.md", without["prompt"])


# --------------------------------------------------------------------------------------------
# prep_md: parser order, fallback, reference stripping, failure.
# --------------------------------------------------------------------------------------------

GOOD_MD = ("# A Paper\n\n## Abstract\n\n" + "word " * 500 + "\n\n## Introduction\n\n" + "word " * 500
           + "\n\n## Methods\n\n" + "word " * 500 + "\n\n## Results\n\n" + "word " * 500)
BAD_MD = "# Title\n\nshort"   # 14 characters: the truncated parse that looked fine


class TestPrepMdConvert(unittest.TestCase):
    """Which parser runs, in what order, and what happens when none of them is good enough."""

    def setUp(self):
        import prep_md
        self.prep = prep_md
        self.tmp = tempfile.mkdtemp(prefix="lacuna-prep-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "out.md")
        self.called = []

    def convert(self, outputs, pages=4, **kw):
        def fake_parse(pdf, parser):
            self.called.append(parser)
            md = outputs.get(parser)
            return (md, None) if md is not None else (None, f"{parser}: venv missing")
        with mock.patch.object(self.prep, "parse", fake_parse), \
                mock.patch.object(self.prep, "page_count", lambda pdf: pages), \
                mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            return self.prep.convert("x.pdf", self.out, quiet=True, **kw)

    def test_default_order_stops_at_the_first_valid_parse(self):
        ok, bad = self.convert({"pymupdf4llm": GOOD_MD, "docling": GOOD_MD})
        self.assertTrue(ok)
        self.assertEqual(self.called, ["pymupdf4llm"], "docling is ~18x slower; it must not run needlessly")

    def test_invalid_parse_falls_back_to_docling(self):
        docling = GOOD_MD.replace("A Paper", "Docling Paper")
        ok, _ = self.convert({"pymupdf4llm": BAD_MD, "docling": docling}, pages=4)
        self.assertTrue(ok)
        self.assertEqual(self.called, ["pymupdf4llm", "docling"])
        with open(self.out) as f:
            self.assertIn("Docling Paper", f.read())

    def test_equations_and_forced_parser_run_exactly_one_parser(self):
        self.convert({"marker": GOOD_MD}, equations=True)
        self.assertEqual(self.called, ["marker"])
        self.called.clear()
        self.convert({"docling": GOOD_MD}, forced="docling", equations=True)
        self.assertEqual(self.called, ["docling"], "--parser must beat --equations")

    def test_no_parser_available_fails_without_writing(self):
        ok, problems = self.convert({})
        self.assertFalse(ok)
        self.assertEqual(sorted(problems), ["docling", "pymupdf4llm"])
        self.assertFalse(os.path.exists(self.out))

    def test_last_parser_failing_validation_is_rejected(self):
        """KNOWN DEFECT 5: `if bad and parser != order[-1]` accepts a failing parse from the last
        parser in the order, so a 14-byte parse reaches the reader."""
        for name, outputs, kw in [
            ("default order", {"pymupdf4llm": BAD_MD, "docling": BAD_MD}, {}),
            ("forced parser", {"docling": BAD_MD}, {"forced": "docling"}),
            ("equations", {"marker": BAD_MD}, {"equations": True}),
        ]:
            with self.subTest(name):
                if os.path.exists(self.out):
                    os.remove(self.out)
                ok, _ = self.convert(outputs, pages=20, **kw)
                self.assertFalse(ok, f"{name}: a parse that failed validation was accepted")
                self.assertFalse(os.path.exists(self.out), f"{name}: the bad parse was written")


class TestPrepMdClean(unittest.TestCase):
    """The reference list is the largest part of many papers, and none of it is worth reading."""

    def setUp(self):
        import prep_md
        self.clean = prep_md.clean

    def test_everything_after_the_last_references_heading_is_dropped(self):
        md = ("# Paper\n\n## Contents\n\nIntro\n\n## References\n\nkept: the contents page listed it\n\n"
              "## Body\n\nbody text\n\n## References\n\n[1] Smith. 2020.\n[2] Jones. 2021.\n")
        out = self.clean(md)
        self.assertIn("body text", out)
        self.assertIn("kept: the contents page listed it", out)
        self.assertNotIn("Smith", out)

    def test_heading_variants_are_recognised(self):
        for heading in ("**Bibliography**", "# REFERENCES", "References"):
            with self.subTest(heading):
                self.assertNotIn("Smith", self.clean(f"# P\n\nbody\n{heading}\n[1] Smith.\n"))

    def test_a_mention_of_references_in_prose_is_not_a_heading(self):
        md = "# P\n\nAs the references below show, this holds.\n\nmore body\n"
        self.assertIn("more body", self.clean(md))

    def test_control_characters_removed_and_long_lines_wrapped(self):
        out = self.clean("# P\n\n" + ("abcdefghi " * 100) + "\x01\x02\n")
        self.assertNotRegex(out, r"[\x00-\x08\x0b-\x1f\x7f]")
        self.assertLessEqual(max(len(l) for l in out.splitlines()), 300)
        self.assertEqual(out.count("abcdefghi"), 100, "wrapping must not drop words")


class TestPrepMdCLI(TempCase):
    """The argv parsing in prep_md is hand-rolled; `--parser NAME` must not become a path."""

    def test_parser_flag_both_forms_and_failure_exit(self):
        pdf = write(self.p("x.pdf"), "%PDF")
        for argv in (["--parser", "docling"], ["--parser=docling"]):
            with self.subTest(argv):
                out = self.p("out.md")
                r = self.run_py([os.path.join(LIT, "prep_md.py"), pdf, out, *argv],
                                {"LACUNA_PYTHON": self.p("no-venv", "python")})
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("docling: venv missing", r.stderr)
                self.assertNotIn("pymupdf4llm:", r.stderr, "the forced parser was ignored")
                self.assertFalse(os.path.exists(out))


# --------------------------------------------------------------------------------------------
# split_book, with a stand-in pymupdf so no real PDF library or file is needed.
# --------------------------------------------------------------------------------------------

FAKE_PYMUPDF = '''
import json, os
_spec = json.load(open(os.environ["FAKE_PDF_SPEC"]))
class _Page:
    def __init__(self, t): self._t = t
    def get_text(self): return self._t
class _Doc:
    def __init__(self):
        self._pages = _spec["pages"]
        self.page_count = len(self._pages)
        self.metadata = {"title": _spec.get("title", "")}
    def get_toc(self): return [tuple(x) for x in _spec["toc"]]
    def __getitem__(self, i): return _Page(self._pages[i])
def open(path): return _Doc()
'''


class TestSplitBook(TempCase):
    """toc.md is the only thing a reader agent sees of a book. Wrong page ranges send it to the
    wrong chapter; a guessed split of a TOC-less book is worse than a refusal."""

    PAGES = ["alpha page", "beta page", "gamma page", "delta page", "epsilon page"]

    def split(self, toc, title="My Book"):
        write(self.p("fake", "pymupdf.py"), FAKE_PYMUPDF)
        write(self.p("spec.json"), json.dumps({"pages": self.PAGES, "toc": toc, "title": title}))
        pdf = write(self.p("book.pdf"), "%PDF")
        self.outdir = self.p("books", "mybook")
        return self.run_py([os.path.join(LIT, "split_book.py"), pdf, self.outdir],
                           {"PYTHONPATH": self.p("fake"), "FAKE_PDF_SPEC": self.p("spec.json")})

    def chapter(self, name):
        with open(os.path.join(self.outdir, name)) as f:
            return f.read()

    def test_chapters_are_greppable(self):
        """A reading agent finds a concept by grepping the chapter files. Control characters
        survive PDF extraction and make grep treat a file as binary, so a book that keeps them is
        silently unsearchable — measured: three greps for words that were present, no matches."""
        self.PAGES = ["alpha\x00page with a \x01NUL and \x1f other controls",
                      "beta page", "gamma page", "delta page", "epsilon page"]
        r = self.split([[1, "Introduction", 1], [1, "Methods", 3]])
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.chapter("00_Introduction.txt")
        self.assertEqual(re.findall(r"[\x00-\x08\x0b-\x1f\x7f]", body), [],
                         "control characters make the chapter unsearchable")
        self.assertIn("alphapage", body.replace(" ", ""), "the words themselves must survive")
        # grep must treat it as text, not binary
        g = subprocess.run(["grep", "-c", "page", os.path.join(self.outdir, "00_Introduction.txt")],
                           capture_output=True, text=True)
        self.assertEqual(g.returncode, 0, "grep found nothing in the chapter file")
        self.assertNotIn("Binary file", g.stdout + g.stderr)

    def test_chapters_page_ranges_and_toc(self):
        r = self.split([[1, "Introduction", 1], [2, "A subsection", 2], [1, "Methods & Results", 3],
                        [1, "Appendix", 5]])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.outdir, "index.json")) as f:
            index = json.load(f)
        self.assertEqual([(x["file"], x["pages"]) for x in index],
                         [("00_Introduction.txt", [1, 2]), ("01_Methods_Results.txt", [3, 4]),
                          ("02_Appendix.txt", [5, 5])])
        intro, methods, appendix = (self.chapter(x["file"]) for x in index)
        self.assertIn("beta page", intro)
        self.assertNotIn("gamma page", intro, "a chapter must stop the page before the next starts")
        self.assertIn("delta page", methods)
        self.assertNotIn("epsilon page", methods)
        self.assertIn("epsilon page", appendix)
        toc = self.chapter("toc.md")
        self.assertTrue(toc.startswith("# My Book\n"))
        self.assertIn("3 chapters, 5 pages", toc)
        self.assertIn("| Methods & Results | `01_Methods_Results.txt` | 3-4 |", toc)
        for x in index:
            self.assertIn(f"| {x['tok']:,} |", toc)
            self.assertEqual(x["tok"], round(x["chars"] / 2.9))

    def test_title_falls_back_to_the_directory_name(self):
        r = self.split([[1, "Only", 1]], title="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.chapter("toc.md").startswith("# mybook\n"))

    def test_refuses_a_book_without_a_top_level_toc(self):
        for toc in ([], [[2, "Only subsections", 1]]):
            with self.subTest(toc=toc):
                r = self.split(toc)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("no top-level table of contents", r.stderr)
                self.assertFalse(os.path.exists(os.path.join(self.outdir, "toc.md")),
                                 "a book that cannot be split must not be offered to readers")
                self.assertFalse(os.path.exists(os.path.join(self.outdir, "index.json")))


# --------------------------------------------------------------------------------------------
# spend: session and fleet reports from fixture transcripts.
# --------------------------------------------------------------------------------------------

def usage(inp=0, write=0, read=0, out=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read, "output_tokens": out}


def call(mid, model, when, u, sidechain=False, tools=()):
    row = {"type": "assistant", "timestamp": iso(when),
           "message": {"id": mid, "model": model, "usage": u,
                       "content": [{"type": "tool_use", "name": t} for t in tools]}}
    if sidechain:
        row["isSidechain"] = True
    return row


def jsonl(path, rows):
    write(path, "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))
    return path


class TestSpend(TempCase):
    """`lacuna spend` is the plugin's headline number. Fixture numbers, worked by hand
    (sonnet-5-5: 2 input, 4 1h-write, 0.10 read, 10 output; haiku-5-5 output 0.50 per MTok):

      #0  1k in, 100k write,       1k out  = $0.4120   cold-shaped, but the first call of a session
      #1         1k write, 101k read, 2k out = $0.0341
      #2         150k write,         1k out  = $0.6100   a cold rewrite
      session $1.0561; without caching $0.746; subagents 0.05 + 0.10 (haiku) + 0.10 (sonnet) = $0.25
    """

    SID = "aaaa1111-0000-0000-0000-000000000000"

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.home, ".claude", "projects")
        self.proj = os.path.join(self.projects, "-work-proj")
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        sonnet = "claude-sonnet-5-5"
        self.session = jsonl(os.path.join(self.proj, f"{self.SID}.jsonl"), [
            {"type": "user", "message": {"content": "hi"}},
            call("m0", sonnet, now - timedelta(hours=2), usage(1_000, 100_000, 0, 1_000)),
            call("m1", sonnet, now - timedelta(hours=1), usage(0, 1_000, 101_000, 2_000), tools=["Read"]),
            call("m1", sonnet, now - timedelta(hours=1), usage(0, 1_000, 101_000, 2_000)),  # streaming dup
            call("m2", sonnet, now, usage(0, 150_000, 0, 1_000)),
            "{ truncated line",
        ])
        subs = os.path.join(self.proj, self.SID, "subagents")
        jsonl(os.path.join(subs, "a.jsonl"), [call("s1", "claude-haiku-5-5", now, usage(out=100_000))])
        jsonl(os.path.join(subs, "b.jsonl"), [call("s2", "claude-haiku-5-5", now, usage(out=200_000))])
        jsonl(os.path.join(subs, "c.jsonl"), [call("s3", sonnet, now, usage(out=10_000))])

    def spend(self, *args):
        r = self.run_py([os.path.join(ROOT, "scripts", "cost", "spend.py"), "--no-color", *args])
        return r, strip_ansi(r.stdout)

    def session_part(self, out):
        return out.split("■ Last")[0]

    def test_session_report_numbers(self):
        r, out = self.spend("--session", self.session)
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.session_part(out)
        self.assertIn("3 API calls over 2.0 h", s, "duplicate message ids must collapse")
        self.assertIn("$1.06 list price", s)
        self.assertIn("same tokens without caching: $0.75", s)
        self.assertIn("1 cold rewrites cost $0.61", s,
                      "the first call of a session is not a rewrite; the 150k write after it is")

    def test_subagents_are_rolled_up_by_model(self):
        r, out = self.spend("--session", self.session)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = next(l for l in out.splitlines() if "subagents:" in l)
        self.assertIn("subagents: 3 runs, $0.25", line)
        self.assertIn("haiku-5-5 ×2 $0.15", line)
        self.assertIn("sonnet-5-5 ×1 $0.10", line)

    def test_fleet_report_splits_main_and_subagents(self):
        r, out = self.spend("--session", self.session, "--days", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        fleet = out.split("■ Last")[1]
        self.assertIn("4 transcripts", fleet)
        self.assertIn("$1.31 list price", fleet)
        self.assertIn("main $1.06", fleet)
        self.assertIn("subagents $0.25", fleet)
        self.assertIn("cache hit 28.6%", fleet)       # 101k read of 353k input-side tokens
        self.assertIn("sonnet-5-5 $1.16", fleet)
        self.assertIn("haiku-5-5 $0.15", fleet)
        self.assertIn("proj $1.31", fleet)

    def test_fleet_excludes_transcripts_older_than_the_window(self):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        stale = jsonl(os.path.join(self.projects, "-work-old", "bbbb.jsonl"),
                      [call("o1", "claude-sonnet-5-5", old, usage(out=1_000_000))])   # $10
        os.utime(stale, (old.timestamp(), old.timestamp()))
        r, out = self.spend("--session", self.session, "--days", "7")
        self.assertIn("$1.31 list price", out.split("■ Last")[1])

    def test_fleet_and_session_agree_on_cold_rewrites(self):
        """NEW DEFECT (found writing these tests): the session report skips a session's first call
        when counting cold rewrites (`i > 0`) but the fleet report does not, so one transcript is
        "1 cold rewrite" in the session view and "2 cold rewrites ← avoidable" in the fleet view.
        A session's first cache write cannot be avoided by a handoff."""
        r, out = self.spend("--session", self.session, "--days", "1")
        fleet = out.split("■ Last")[1]
        self.assertIn("1 cold rewrites $0.61", fleet)

    def test_session_id_prefix_resolves_to_that_transcript(self):
        jsonl(os.path.join(self.proj, "zzzz9999.jsonl"), [call("x", "claude-haiku-5-5",
                                                                datetime.now(timezone.utc), usage(out=10))])
        r, out = self.spend("--session", "aaaa1111", "--cwd", "/work/proj")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("aaaa1111", self.session_part(out))
        self.assertIn("3 API calls", self.session_part(out))

    def test_unknown_session_id_exits_2(self):
        """KNOWN DEFECT 9: an id that matches nothing silently reports the cwd's newest session."""
        r, out = self.spend("--session", "ffff0000-not-a-session", "--cwd", "/work/proj")
        self.assertEqual(r.returncode, 2, out)
        self.assertNotIn("aaaa1111", out, "a different session was reported as the one asked for")


# --------------------------------------------------------------------------------------------
# statusline
# --------------------------------------------------------------------------------------------

class TestStatusline(TempCase):
    """Runs on every refresh and reads fields that are not a stable interface: it must render
    the right numbers when they are there and something sane when they are not."""

    def render(self, payload, columns="120"):
        r = self.run_py([os.path.join(ROOT, "statusline", "statusline.py")], {"COLUMNS": columns},
                        input=json.dumps(payload))
        return r, [strip_ansi(l) for l in r.stdout.splitlines()]

    def transcript(self):
        now = datetime.now(timezone.utc)
        return jsonl(self.p("t.jsonl"), [
            call("m1", "claude-opus-5-5", now, {"input_tokens": 10, "cache_creation_input_tokens": 1_000,
                                                "cache_read_input_tokens": 100_000, "output_tokens": 1_000}),
            # a later subagent call: huge, and not the main thread's last turn
            call("m2", "claude-opus-5-5", now, usage(out=1_000_000), sidechain=True),
            {"type": "user", "message": {"content": "next"}},
            "{ torn write",
        ])

    def payload(self, **over):
        d = {"model": {"display_name": "Opus 5.5", "id": "claude-opus-5-5"},
             "workspace": {"project_dir": "/x/myproj"},
             "context_window": {"current_usage": {"input_tokens": 10, "cache_creation_input_tokens": 1_000,
                                                  "cache_read_input_tokens": 149_000},
                                "context_window_size": 1_000_000},
             "cost": {"total_cost_usd": 3.5}, "transcript_path": self.transcript()}
        d.update(over)
        return d

    def test_renders_last_turn_split_from_the_transcript(self):
        r, lines = self.render(self.payload())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(lines), 2)
        l1, l2 = lines
        self.assertIn("Opus 5.5", l1)
        self.assertIn("myproj", l1)
        self.assertIn("150k", l1)
        self.assertIn("$3.50 session", l2)
        # opus-5-5: 100k read x0.20 + 1k 1h-write x8 + 1k out x20 + 10 in x4, per MTok
        self.assertIn("last $0.048 (read 0.020 · write 0.008 · out 0.020)", l2)

    def test_cold_cache_estimate_uses_the_exact_model_price(self):
        """opus-5-5 1h writes are $8/MTok; matching "opus-5" instead would say $1.50."""
        r, (l1, _) = self.render(self.payload())
        self.assertIn("cold · next prompt re-caches ≈$1.20", l1)
        self.assertIn("/compact", l1)

    def test_warm_cache_and_plan_usage_segments(self):
        p = self.payload(prompt_cache={"expires_at": time.time() + 1800, "ttl": "1h", "hit_ratio": 0.93},
                         rate_limits={"five_hour": {"used_percentage": 42, "resets_at": time.time() + 7200},
                                      "seven_day": {"used_percentage": None}})
        r, (l1, l2) = self.render(p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("left", l1)
        self.assertNotIn("cold", l1)
        self.assertIn("hit 93%", l2)
        self.assertIn("42%", l2)
        self.assertNotIn("7d", l2, "a window with no percentage must go blank, not show 0%")

    def test_degrades_when_fields_are_absent_or_null(self):
        for name, payload in {
            "empty object": {},
            "nulls": {"model": None, "context_window": None, "cost": None, "prompt_cache": None,
                      "rate_limits": None, "workspace": None, "transcript_path": None},
            "partial usage": {"model": {"id": "claude-haiku-5-5"},
                              "context_window": {"current_usage": {"input_tokens": None}},
                              "rate_limits": {"five_hour": {}}},
            "missing transcript": {"transcript_path": self.p("nope.jsonl")},
        }.items():
            with self.subTest(name):
                r, lines = self.render(payload)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(len(lines), 2, r.stdout)
                self.assertIn("ctx -", lines[0])
                self.assertIn("$0.00 session", lines[1])
                self.assertNotIn("last $", lines[1])

    def test_narrow_terminal_keeps_only_the_first_two_cost_segments(self):
        p = self.payload(prompt_cache={"hit_ratio": 0.5}, rate_limits={"five_hour": {"used_percentage": 10}})
        _, (_, wide) = self.render(p, columns="120")
        _, (_, narrow) = self.render(p, columns="80")
        self.assertIn("5h", wide)
        self.assertNotIn("5h", narrow)
        self.assertIn("last $", narrow)


# --------------------------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------------------------

class TestDoctor(TempCase):
    """The first thing a stranger runs. It must say what is missing, not crash on it, and never
    touch a config file the user has already edited."""

    def doctor(self):
        return self.run_py([os.path.join(ROOT, "scripts", "doctor.py")],
                           {"LACUNA_PYTHON": self.p("no-venv", "bin", "python")})

    def test_reports_missing_pieces_without_crashing(self):
        r = self.doctor()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertRegex(r.stdout, r"\[MISS\] claude CLI not on PATH")
        # pymupdf4llm is optional (PDFs only), so it is a note, not a blocker — audit finding W25
        self.assertRegex(r.stdout, r"\[note\] pymupdf4llm not installed")
        # the fix line now carries the full path to bin/lacuna, because a new user does not yet
        # have `lacuna` on PATH — audit finding W25
        self.assertRegex(r.stdout, r"fix: \S*bin/lacuna doctor --install-pdf")
        self.assertRegex(r.stdout, r"\[note\] docling not installed")   # optional: not blocking
        # only a missing `claude` blocks; the three parsers are optional — audit finding W25
        self.assertIn("1 BLOCKING", r.stdout)
        self.assertIn("5 optional piece(s) not installed", r.stdout)
        for rel in ("cache", "books", "config.json", "calibration.json"):
            self.assertTrue(os.path.exists(os.path.join(self.data, rel)), rel)
        self.assertFalse([d for d in os.listdir(self.data) if d.startswith("venv")],
                         "doctor without --install-* must not create any venv")
        with open(os.path.join(self.data, "config.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["models"]["planner"], "claude-opus-5")
        self.assertEqual(cfg["after"], {"read": "", "synth": "", "verify": ""})

    def test_is_idempotent(self):
        self.doctor()
        snap = {f: open(os.path.join(self.data, f), "rb").read() for f in ("config.json", "calibration.json")}
        r = self.doctor()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("(created)", r.stdout)
        self.assertNotIn("written with the shipped defaults", r.stdout)
        self.assertIn("left untouched", r.stdout)
        for f, before in snap.items():
            self.assertEqual(open(os.path.join(self.data, f), "rb").read(), before, f)

    def test_never_overwrites_an_existing_user_file(self):
        for body in ('{"models": {"planner": "mine"}}', "{ hand-edited and broken"):
            with self.subTest(body=body):
                cfg = write(os.path.join(self.data, "config.json"), body)
                cal = write(os.path.join(self.data, "calibration.json"), '{"_source": "mine"}')
                r = self.doctor()
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(open(cfg).read(), body)
                self.assertEqual(open(cal).read(), '{"_source": "mine"}')

    def test_finds_claude_when_it_is_on_path(self):
        fake = write(self.p("bin", "claude"), "#!/bin/sh\nexit 0\n", 0o755)
        r = self.run_py([os.path.join(ROOT, "scripts", "doctor.py")],
                        {"PATH": f"{os.path.dirname(fake)}:{SAFE_PATH}",
                         "LACUNA_PYTHON": self.p("no-venv", "bin", "python")})
        self.assertRegex(r.stdout, r"\[ok  \] claude CLI " + re.escape(fake))
        # with `claude` present and only optional parsers missing, nothing blocks
        self.assertIn("Nothing is blocking", r.stdout)
        self.assertEqual(r.returncode, 0, "optional pieces must not make doctor fail")


# --------------------------------------------------------------------------------------------
# lacuna_config: the sections test_lacuna.py does not reach
# --------------------------------------------------------------------------------------------

class TestConfigSections(TempCase):
    """config.json is edited by hand. A partial section must layer over the defaults, not replace them."""

    def effective(self, cfg):
        write(os.path.join(self.data, "config.json"), json.dumps(cfg))
        r = self.run_py([os.path.join(ROOT, "scripts", "lacuna_config.py"), "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_after_section_merges_per_stage(self):
        after = self.effective({"after": {"synth": "~/bin/x"}})["after"]
        self.assertEqual(after, {"read": "", "synth": "~/bin/x", "verify": ""})

    def test_partial_paths_override_keeps_the_other_defaults(self):
        paths = self.effective({"paths": {"books": "/srv/books"}})["paths"]
        self.assertEqual(paths["books"], "/srv/books")
        self.assertEqual(paths["cache"], os.path.join(self.data, "cache"))
        self.assertEqual(paths["python"], os.path.join(self.data, "venv", "bin", "python"))
        self.assertEqual(paths["ledger"], os.path.join(self.data, "cost-ledger.jsonl"))

    def test_env_path_override_beats_file(self):
        write(os.path.join(self.data, "config.json"), json.dumps({"paths": {"cache": "/from/file"}}))
        r = self.run_py([os.path.join(ROOT, "scripts", "lacuna_config.py"), "--json"],
                        {"LACUNA_CACHE": "/from/env"})
        self.assertEqual(json.loads(r.stdout)["paths"]["cache"], "/from/env")

    def test_unknown_keys_are_harmless(self):
        cfg = self.effective({"my_layer": {"x": 1}, "models": {"summariser": "opus"},
                              "effort": {"reader": "low"}})
        self.assertEqual(cfg["models"]["reader"], "haiku")
        self.assertEqual(cfg["effort"]["reader"], "low")
        self.assertEqual(cfg["effort"]["verifier"], "high")

    def test_a_null_section_degrades_to_defaults(self):
        """NEW DEFECT (found writing these tests): `"paths": null` or `"models": null` in a valid
        config.json crashes lacuna_config on import (AttributeError), and with it `lacuna spend`,
        `lacuna doctor` and the whole pipeline. The invalid-JSON case is handled; this one is not."""
        for section in ("paths", "models", "effort"):
            with self.subTest(section):
                write(os.path.join(self.data, "config.json"), json.dumps({section: None}))
                r = self.run_py(["-c", "import sys; sys.path.insert(0, sys.argv[1]); import lacuna_config as c; "
                                       "print(c.model('reader'), c.effort('verifier'), c.path('cache'))",
                                 os.path.join(ROOT, "scripts")])
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                self.assertEqual(r.stdout.split(), ["haiku", "high", os.path.join(self.data, "cache")])


# --------------------------------------------------------------------------------------------
# cache_guard beyond SessionStart
# --------------------------------------------------------------------------------------------

class TestCacheGuardPrompts(TempCase):
    """The hold on the first idle prompt is the hook's whole point; it must also let the user
    through on a resend, and must never take a session down with it."""

    def hook(self, payload, raw=None):
        return subprocess.run([PY, os.path.join(ROOT, "hooks", "cache_guard.py")],
                              input=raw if raw is not None else json.dumps(payload),
                              capture_output=True, text=True,
                              env=base_env(self.home, self.data))

    def transcript(self, ago, with_timestamp=True):
        row = call("m1", "claude-opus-5-5", datetime.now(timezone.utc) - ago, usage(0, 1_000, 99_000, 500))
        if not with_timestamp:
            del row["timestamp"]
        return jsonl(self.p("t.jsonl"), [row])

    def prompt(self, tp, sid="s1"):
        return self.hook({"hook_event_name": "UserPromptSubmit", "session_id": sid,
                          "transcript_path": tp, "prompt": "carry on please"})

    def test_holds_the_first_idle_prompt_then_lets_a_resend_through(self):
        tp = self.transcript(timedelta(hours=3))
        r = self.prompt(tp)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("100k", out["reason"])
        self.assertIn("≈$0.80", out["reason"])                 # 100k x $8/MTok opus-5-5 1h write
        with open(os.path.join(self.data, "cache_guard", "s1.prompt.txt")) as f:
            self.assertEqual(f.read(), "carry on please")
        r = self.prompt(tp)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), "the resend must go through")
        self.assertEqual(json.loads(self.prompt(tp).stdout)["decision"], "block",
                         "after one pass-through the next idle prompt is held again")

    def test_a_recent_session_is_never_held(self):
        r = self.prompt(self.transcript(timedelta(minutes=2)))
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))

    def test_transcript_line_without_timestamp_is_survivable(self):
        """KNOWN DEFECT 6: last_call indexes e["timestamp"] and raises KeyError."""
        tp = self.transcript(timedelta(hours=3), with_timestamp=False)
        for payload in ({"hook_event_name": "UserPromptSubmit", "session_id": "s", "transcript_path": tp},
                        {"hook_event_name": "SessionStart", "source": "resume", "session_id": "s",
                         "transcript_path": tp}):
            with self.subTest(payload["hook_event_name"]):
                r = self.hook(payload)
                self.assertEqual(r.returncode, 0, r.stderr[-400:])
                self.assertNotIn("Traceback", r.stderr)
                if r.stdout.strip():
                    self.assertIsInstance(json.loads(r.stdout), dict)


# --------------------------------------------------------------------------------------------
# small CLIs: lit_cost and the results pane
# --------------------------------------------------------------------------------------------

class TestLitCostCLI(TempCase):
    """`lacuna lit estimate` is the "before you spend anything" command. A traceback there is the
    first impression; exit 2 is the documented answer to bad input."""

    def estimate(self, *args):
        return self.run_py([os.path.join(LIT, "lit_cost.py"), *args])

    def test_counts_tokens_of_a_directory(self):
        d = self.p("papers")
        write(os.path.join(d, "a.md"), "a" * 2900)
        write(os.path.join(d, "b.txt"), "b" * 2900)
        write(os.path.join(d, "fig.png"), "not text" * 1000)
        r = self.estimate(d, "--model", "haiku", "--effort", "medium")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 documents, ~2,000 tokens", r.stdout)
        self.assertIn("Calibration: upstream", r.stdout)

    def test_unknown_model_is_refused_not_guessed(self):
        f = write(self.p("a.md"), "a" * 100)
        r = self.estimate(f, "--model", "gpt-nothing")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no calibration for model 'gpt-nothing'", r.stderr)

    def test_bad_invocations_exit_2_without_a_traceback(self):
        """KNOWN DEFECT 7: StopIteration on a trailing flag, ValueError with no arguments."""
        f = write(self.p("a.md"), "a" * 100)
        for argv in ([], [f, "--model"], ["--per-agent"]):
            with self.subTest(argv=argv):
                r = self.estimate(*argv)
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(r.returncode, 2, r.stderr[-300:])

    def test_help_prints_usage(self):
        """KNOWN DEFECT 7: `--help` raises StopIteration."""
        r = self.estimate("--help")
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn(r.returncode, (0, 2))
        self.assertIn("usage", (r.stdout + r.stderr).lower())


class TestResultsPaneArgs(TempCase):
    """`lacuna pane --once` is what the docs tell a non-interactive shell to run."""

    def test_flags_are_not_taken_as_the_target(self):
        """KNOWN DEFECT 8: argv[1] is the target even when it is a flag, so `--once` and `--help`
        create directories of that name in the cwd."""
        results = self.p("results")
        write(os.path.join(results, "r.md"), "# Hello from results\n")
        for flag in ("--once", "--help"):
            with self.subTest(flag):
                cwd = self.p("cwd" + flag)
                os.makedirs(cwd)
                r = self.run_py([os.path.join(ROOT, "scripts", "panes", "results_pane.py"), flag],
                                {"LACUNA_RESULTS": results}, cwd=cwd)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(os.listdir(cwd), [], f"{flag} was treated as a path")
                if flag == "--once":
                    self.assertIn("Hello from results", r.stdout, "--once alone should follow the default dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
