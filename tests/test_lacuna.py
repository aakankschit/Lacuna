"""Tests for the parts of lacuna that are quietly wrong when they break.

No third-party dependencies: `python3 -m unittest discover tests`.

Each test exists because of a specific way this plugin could cost someone money or mislead them:
pricing that silently matches the wrong model, a note cache that serves stale notes under new
assumptions, a parse that fails plausibly instead of loudly, and config precedence that ignores the
override you just set.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "cost"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "lit"))


class TestPricing(unittest.TestCase):
    """A price table matched by substring is one typo away from charging Haiku rates for Opus."""

    def setUp(self):
        import lacuna_config
        self.rl = lacuna_config
        self.prices = lacuna_config.prices()

    def test_shipped_table_loads(self):
        self.assertGreaterEqual(len(self.prices), 5)
        for model, row in self.prices.items():
            self.assertEqual(len(row), 5, f"{model} must have 5 prices")
            self.assertTrue(all(isinstance(x, float) and x >= 0 for x in row), model)

    def test_longest_key_wins(self):
        """"opus-5" must never swallow "opus-5-5": that is a 5x pricing error in the wrong direction."""
        keys = list(self.prices)
        self.assertEqual(keys, sorted(keys, key=lambda k: -len(k)))
        import ccost
        self.assertNotEqual(ccost.price("claude-opus-5-5-20260101"),
                            ccost.price("claude-opus-5-20260101"))

    def test_cache_read_is_cheapest_and_output_dearest(self):
        for model, (inp, w5, w1, read, out) in self.prices.items():
            self.assertLess(read, inp, f"{model}: cache read should be cheaper than fresh input")
            self.assertLessEqual(w5, w1, f"{model}: a 1h write should cost at least a 5m write")
            self.assertGreater(out, inp, f"{model}: output should cost more than input")

    def test_user_override_wins_and_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pricing.json"), "w") as f:
                json.dump({"_retrieved": "2099-01-01",
                           "models": {"haiku-5-5": [9, 9, 9, 9, 9]}}, f)
            env = {**os.environ, "LACUNA_DATA": d}
            out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "lacuna_config.py"), "--json"],
                                 capture_output=True, text=True, env=env, check=True).stdout
            self.assertEqual(json.loads(out)["prices"]["haiku-5-5"], [9, 9, 9, 9, 9])

    def test_unknown_model_does_not_silently_cost_zero(self):
        import ccost
        self.assertTrue(all(x > 0 for x in ccost.price("some-model-we-have-never-heard-of")),
                        "an unknown model must fall back to a non-zero price, never $0")


class TestConfigPrecedence(unittest.TestCase):
    """Environment beats file beats default. Getting this wrong means silently using the wrong model."""

    def run_config(self, data_dir, extra_env=None):
        env = {**os.environ, "LACUNA_DATA": data_dir, **(extra_env or {})}
        out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "lacuna_config.py"), "--json"],
                             capture_output=True, text=True, env=env, check=True).stdout
        return json.loads(out)

    def test_defaults_then_file_then_env(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_config(d)["models"]["planner"], "claude-opus-5")

            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"models": {"planner": "from-file"}}, f)
            self.assertEqual(self.run_config(d)["models"]["planner"], "from-file")

            cfg = self.run_config(d, {"LACUNA_PLANNER": "from-env"})
            self.assertEqual(cfg["models"]["planner"], "from-env")
            # a partial file must not wipe the other roles
            self.assertEqual(cfg["models"]["reader"], "haiku")

    def test_tilde_and_vars_in_paths_are_expanded(self):
        """config.json is edited by hand, so "~/books" must not become a directory named "~"."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"paths": {"books": "~/mybooks", "cache": "$HOME/mycache"}}, f)
            paths = self.run_config(d)["paths"]
            self.assertEqual(paths["books"], os.path.expanduser("~/mybooks"))
            self.assertEqual(paths["cache"], os.path.join(os.environ["HOME"], "mycache"))
            self.assertNotIn("~", paths["books"])

    def test_malformed_config_does_not_crash(self):
        """A broken config file must degrade to defaults, not break every session's hooks."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w") as f:
                f.write("{ this is not json")
            self.assertEqual(self.run_config(d)["models"]["planner"], "claude-opus-5")


class TestNoteCacheKey(unittest.TestCase):
    """A cache key that ignores the prompt serves notes written under assumptions that have changed."""

    def test_key_inputs_are_all_in_the_hash(self):
        import lit_pipeline as L
        with open(os.path.join(ROOT, "scripts", "lit", "lit_pipeline.py")) as f:
            body = f.read()
        key_expr = body[body.index("key = hashlib.sha256"):body.index("jobs.append(")]
        for part in ("content_h", "PROMPT_VERSION", "reader_model", "reader_effort",
                     "a.brief", "books_fp"):
            self.assertIn(part, key_expr, f"{part} must be part of the note cache key")
        self.assertTrue(L.PROMPT_VERSION, "PROMPT_VERSION must be set")

    def test_parser_choice_changes_the_cache_path(self):
        """marker and pymupdf4llm give different markdown for one PDF; they must not collide."""
        import lit_pipeline as L
        with open(os.path.join(ROOT, "scripts", "lit", "lit_pipeline.py")) as f:
            body = f.read()
        sig = body[body.index("def to_markdown("):body.index("def cmd_read(")]
        self.assertIn("equations", sig)
        self.assertIn("parser", sig)
        self.assertIn("tag", sig, "the parser choice must appear in the cached filename")
        # the read subcommand must actually expose the flags the docs promise
        r = subprocess.run([os.path.join(ROOT, "bin", "lacuna"), "lit", "read", "--help"],
                           capture_output=True, text=True)
        for flag in ("--equations", "--parser", "--brief", "--no-books", "--model", "--out"):
            self.assertIn(flag, r.stdout, f"`lacuna lit read` should accept {flag}")
        del L

    def test_exit_codes_are_distinct(self):
        import lit_pipeline as L
        codes = [L.EX_OK, L.EX_INPUT, L.EX_PARSE, L.EX_MODEL]
        self.assertEqual(len(set(codes)), 4)
        self.assertEqual(L.EX_OK, 0)

    def test_missing_notes_dir_exits_with_input_code(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "lit", "lit_pipeline.py"),
                            "synth", "--notes", "/nonexistent-xyz", "--repo", ROOT, "--out", "/tmp/x.md"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("does not exist", r.stderr)


class TestParseValidation(unittest.TestCase):
    """A bad parse can look like valid markdown. Validation is the only thing that catches it."""

    def setUp(self):
        import prep_md
        self.prep = prep_md

    def test_truncated_output_is_rejected(self):
        """The real failure: a 14k-char file for a 20-page paper, with plausible headings."""
        md = "# A Paper\n\n## Abstract\n\nShort.\n\n## Introduction\n\n" + ("word " * 200)
        self.assertTrue(self.prep.validate(md, pages=20),
                        "a short file for a long document must fail validation")

    def test_a_flood_of_control_characters_is_rejected(self):
        """A flood means binary-ish output: grep treats the file as binary and a reader finds
        nothing. A handful is cosmetic — clean() strips them — and rejecting a good 75k-char parse
        over a single stray byte made `--equations` exit 3 on a perfectly usable document."""
        body = ("# A Paper\n\n## Abstract\n\n## Introduction\n\n## Methods\n\n## Results\n\n"
                + ("word " * 4000))
        self.assertEqual(self.prep.validate(body + "\x01\x02\x03", pages=4), [],
                         "a few control characters must not fail an otherwise good parse")
        bad = self.prep.validate(body + "\x01" * 60, pages=4)
        self.assertTrue(any("control" in b.lower() for b in bad), bad)

    def test_a_truncated_parse_is_rejected_against_the_documents_own_text(self):
        """The failure validation exists for. A fixed chars-per-page floor cannot catch it: a
        dense paper cut to 37% still clears any floor low enough to accept a sparse one."""
        full = "# A Paper\n\n## Abstract\n\n" + ("real sentence of body text. " * 1200)
        raw = len(full)
        self.assertEqual(self.prep.validate(full, 14, raw), [])
        for frac in (0.37, 0.10):
            bad = self.prep.validate(full[:int(raw * frac)], 14, raw)
            self.assertTrue(any("truncated" in b for b in bad), f"{frac:.0%}: {bad}")

    def test_plausible_output_passes(self):
        md = ("# A Paper\n\n## Abstract\n\n" + ("word " * 500)
              + "\n\n## Introduction\n\n" + ("word " * 500)
              + "\n\n## Methods\n\n" + ("word " * 500)
              + "\n\n## Results\n\n" + ("word " * 500))
        self.assertEqual(self.prep.validate(md, pages=4), [])

    def test_missing_expected_sections_is_flagged(self):
        md = "# A Title\n\n## Something\n\n" + ("word " * 4000)
        self.assertTrue(self.prep.validate(md, pages=4))


class TestTranscriptParsing(unittest.TestCase):
    """Cost comes from an internal file format. Deduplication and the split must stay correct."""

    def _transcript(self, path):
        rows = [
            {"type": "assistant", "timestamp": "2026-10-13T00:00:00Z",
             "message": {"id": "m1", "model": "claude-haiku-5-5", "content": [],
                         "usage": {"input_tokens": 100, "output_tokens": 1000,
                                   "cache_read_input_tokens": 50_000,
                                   "cache_creation_input_tokens": 10_000}}},
            # the same message id again: a streaming duplicate, which must not be double-counted
            {"type": "assistant", "timestamp": "2026-10-13T00:00:01Z",
             "message": {"id": "m1", "model": "claude-haiku-5-5", "content": [],
                         "usage": {"input_tokens": 100, "output_tokens": 1000,
                                   "cache_read_input_tokens": 50_000,
                                   "cache_creation_input_tokens": 10_000}}},
            {"type": "user", "message": {"content": "ignored"}},
            {"type": "assistant", "timestamp": "2026-10-13T00:00:02Z",
             "message": {"id": "m2", "model": "claude-haiku-5-5",
                         "content": [{"type": "tool_use", "name": "Read"}],
                         "usage": {"input_tokens": 0, "output_tokens": 10,
                                   "cache_read_input_tokens": 60_000,
                                   "cache_creation_input_tokens": 0}}},
            "{ not json at all",
        ]
        with open(path, "w") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def test_dedupes_and_splits(self):
        import ccost
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            self._transcript(p)
            calls = ccost.load(p)
            self.assertEqual(len(calls), 2, "duplicate message ids must collapse to one call")
            self.assertEqual(calls[1]["tools"], ["Read"])

            split, ctx = ccost.split(calls[0])
            self.assertEqual(ctx, 100 + 10_000 + 50_000)
            # haiku-5-5: 0.10 input, 0.20 1h write, 0.01 read, 0.50 output per MTok
            self.assertAlmostEqual(split["read"], 50_000 * 0.01 / 1e6, places=9)
            self.assertAlmostEqual(split["output"], 1_000 * 0.50 / 1e6, places=9)
            self.assertAlmostEqual(split["write1h"], 10_000 * 0.20 / 1e6, places=9)

    def test_cold_detection(self):
        import ccost
        self.assertTrue(ccost.is_cold({"u": {"cache_creation_input_tokens": 150_000,
                                             "cache_read_input_tokens": 0}}))
        self.assertFalse(ccost.is_cold({"u": {"cache_creation_input_tokens": 1_000,
                                              "cache_read_input_tokens": 150_000}}))


class TestHooksNeverBreakASession(unittest.TestCase):
    """A hook that raises is worse than a hook that does nothing: it breaks every prompt."""

    def _run_hook(self, name, payload, env=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "hooks", name)],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env={**os.environ, **(env or {})})

    def test_cache_guard_on_fresh_start(self):
        r = self._run_hook("cache_guard.py", {"hook_event_name": "SessionStart",
                                              "source": "startup", "session_id": "t"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Routing", json.loads(r.stdout)["systemMessage"])

    def test_cache_guard_warns_on_cold_resume(self):
        r = self._run_hook("cache_guard.py", {
            "hook_event_name": "SessionStart", "source": "resume", "session_id": "t2",
            "seconds_since_last_response": 9000, "context_tokens": 160_000,
            "prompt_cache_likely_expired": True, "transcript_path": "/nonexistent"})
        self.assertEqual(r.returncode, 0, r.stderr)
        msg = json.loads(r.stdout)["systemMessage"]
        self.assertIn("cache has expired", msg)
        self.assertIn("160k", msg)

    def test_cache_guard_respects_off_switch(self):
        r = self._run_hook("cache_guard.py", {"hook_event_name": "SessionStart", "source": "startup"},
                           env={"LACUNA_GUARD_OFF": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_cache_guard_survives_garbage_input(self):
        """A hook that raises breaks every prompt in the session, so garbage must exit 0 silently."""
        for bad in ("", "not json", "[]", '{"hook_event_name":"Unknown"}'):
            r = subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "cache_guard.py")],
                               input=bad, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"input {bad!r} -> exit {r.returncode}: {r.stderr}")
            self.assertEqual(r.stdout.strip(), "", f"input {bad!r} produced a hook action")
            self.assertNotIn("Traceback", r.stderr)

    def test_cache_guard_survives_a_transcript_line_without_a_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "t.jsonl")
            with open(t, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {
                    "id": "m1", "model": "claude-haiku-5-5",
                    "usage": {"input_tokens": 1, "output_tokens": 1,
                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}) + "\n")
            r = subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "cache_guard.py")],
                               input=json.dumps({"hook_event_name": "UserPromptSubmit",
                                                 "session_id": "s", "transcript_path": t,
                                                 "prompt": "hi"}),
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_results_hook_is_silent_outside_the_pane(self):
        r = self._run_hook("results_hook.py", {"hook_event_name": "SessionStart"},
                           env={"CLAUDE_RESULTS_FILE": "", "TMUX": ""})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


class TestAfterHook(unittest.TestCase):
    """The extension seam. It runs after a stage has already succeeded and already cost money,
    so a broken `after` command must never take the run down with it."""

    def _after(self, cmd, data_dir):
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump({"after": {"synth": cmd}}, f)
        code = ("import os, sys\n"
                f"os.environ['LACUNA_DATA'] = {data_dir!r}\n"
                f"sys.path.insert(0, {os.path.join(ROOT, 'scripts')!r})\n"
                f"sys.path.insert(0, {os.path.join(ROOT, 'scripts', 'lit')!r})\n"
                "import lit_pipeline as L\n"
                "L.after('synth', '/tmp/does-not-matter.md')\n")
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env={**os.environ, "LACUNA_TESTDIR": data_dir})

    def test_every_broken_command_is_survivable(self):
        with tempfile.TemporaryDirectory() as d:
            for cmd, expect in [
                ("/bin/echo filed", "ok"),
                ("/bin/does-not-exist-anywhere", "127"),        # the shell reports it
                ("/usr/bin/false", "exit 1"),
                ("/bin/echo 'two words'", "ok"),
                ("/bin/echo 'unbalanced", "exit"),              # the shell rejects it
                (["/bin/echo", "list form"], "ok"),
            ]:
                r = self._after(cmd, d)
                self.assertEqual(r.returncode, 0, f"{cmd!r} crashed:\n{r.stderr}")
                self.assertIn(expect, r.stdout, f"{cmd!r} -> {r.stdout!r}")
                if expect != "ok":
                    self.assertIn("output is unaffected", r.stdout,
                                  "a failed hook must say the stage's output survived")

    def test_hook_gets_the_path_last_and_the_shell_expands_variables(self):
        """The documented contract: the output path is APPENDED, and `~`/`$VAR` expand.

        A command with no arguments of its own therefore sees the path as `$1` — which is the
        common case and what the docs show. Before this, neither half was true: there was no
        shell, so `~/bin/hook` could not even be found.
        """
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "got.txt")
            script = os.path.join(d, "hook.sh")
            with open(script, "w") as f:
                f.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > {marker}\n')
            os.chmod(script, 0o755)

            # no arguments of its own: the path is $1
            self._after(script, d)
            with open(marker) as f:
                self.assertEqual(f.read().strip().splitlines(), ["/tmp/does-not-matter.md"])

            # with its own arguments: the path is appended last
            self._after(f'"{script}" --kind plan', d)
            with open(marker) as f:
                self.assertEqual(f.read().strip().splitlines(),
                                 ["--kind", "plan", "/tmp/does-not-matter.md"])

            # a shell variable in the command is expanded rather than passed through literally
            os.remove(marker)
            r = self._after('"$LACUNA_TESTDIR/hook.sh"', d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("ok", r.stdout, f"$VAR was not expanded: {r.stdout!r}")
            self.assertTrue(os.path.exists(marker), "the hook named by $VAR never ran")

    def test_absent_hook_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._after("", d)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")


class TestResultsPaneFallback(unittest.TestCase):
    """The pane is the only component that wants a third-party package. It must not require it."""

    def test_renders_without_rich(self):
        with tempfile.TemporaryDirectory() as d:
            block = os.path.join(d, "sitecustomize.py")
            with open(block, "w") as f:
                f.write("import sys\n"
                        "from importlib.abc import MetaPathFinder\n"
                        "class Block(MetaPathFinder):\n"
                        "    def find_spec(self, name, path=None, target=None):\n"
                        "        if name == 'rich' or name.startswith('rich.'):\n"
                        "            raise ImportError('blocked for test')\n"
                        "        return None\n"
                        "sys.meta_path.insert(0, Block())\n")
            doc = os.path.join(d, "r.md")
            with open(doc, "w") as f:
                f.write("# Title\n\nSome **bold** text and a `code` span.\n")
            r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "panes", "results_pane.py"),
                                doc, "--once"], capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": d})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Title", r.stdout)
            self.assertIn("bold", r.stdout)


class TestCLI(unittest.TestCase):
    def test_cli_dispatches_and_rejects_nonsense(self):
        cli = os.path.join(ROOT, "bin", "lacuna")
        self.assertTrue(os.access(cli, os.X_OK), "bin/lacuna must be executable")
        self.assertIn("lacuna", subprocess.run([cli, "--help"], capture_output=True, text=True).stdout)
        for argv in (["not-a-command"], ["lit", "not-a-subcommand"]):
            r = subprocess.run([cli, *argv], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, f"{argv} should exit 2")

    def test_plugin_manifests_are_valid(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as f:
            plugin = json.load(f)
        self.assertEqual(plugin["name"], "lacuna")
        for prefix in ("claude-", "anthropic-", "anthropics-", "cc-plugin-"):
            self.assertFalse(plugin["name"].startswith(prefix))
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as f:
            market = json.load(f)
        self.assertTrue(market["owner"]["name"])
        self.assertEqual([p["name"] for p in market["plugins"]], ["lacuna"])

    def test_no_absolute_user_paths_shipped(self):
        """One hardcoded home directory in a skill file ends a reader's evaluation."""
        bad = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "tests"}]
            for fn in filenames:
                if not fn.endswith((".md", ".py", ".json", ".sh")) and fn not in ("lacuna", "ccresults", "ccsplit"):
                    continue
                p = os.path.join(dirpath, fn)
                with open(p, errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if "/Users/" in line or "/home/" in line:
                            bad.append(f"{os.path.relpath(p, ROOT)}:{i}")
        self.assertEqual(bad, [], f"absolute home paths found: {bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestOutputIsJudgedByContent(unittest.TestCase):
    """A stage must not report success because a file is big enough.

    Found end-to-end: with relative paths the synthesiser could read nothing, wrote a fluent
    "BLOCKED — not completed" document, and the pipeline exited 0, logged it as written, and ran
    the user's after.synth hook on it.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts", "lit"))
        import lit_pipeline
        self.L = lit_pipeline

    def test_a_long_refusal_is_not_usable_output(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.md")
            for opening in ("# Plan\n\nBLOCKED — not completed. I could not read any of the notes.",
                            "I cannot help with this request.",
                            "# Result\n\nUnable to read the supplied directory, so no plan follows."):
                with open(p, "w") as f:
                    f.write(opening + "\n\n" + ("padding text " * 200))
                self.assertGreater(os.path.getsize(p), 800, "fixture must clear the size check")
                self.assertFalse(self.L.usable_output(p, 800, "plan"),
                                 f"a document opening {opening[:40]!r} must not count as output")

    def test_real_output_is_usable(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.md")
            with open(p, "w") as f:
                f.write("# Development plan\n\n## 1. Add a --json flag\n\n" + ("real content " * 200))
            self.assertTrue(self.L.usable_output(p, 800, "plan"))

    def test_short_output_is_not_usable(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.md")
            with open(p, "w") as f:
                f.write("# Plan\n")
            self.assertFalse(self.L.usable_output(p, 800, "plan"))


class TestShippedExampleIsActuallyShipped(unittest.TestCase):
    """The README's first run depends on examples/notes/. A .gitignore rule silently excluded it
    from every commit, so the showcase command failed for everyone while working locally."""

    def test_example_notes_exist_and_are_not_ignored(self):
        notes = os.path.join(ROOT, "examples", "notes")
        self.assertTrue(os.path.isdir(notes), "examples/notes/ is missing")
        files = [f for f in os.listdir(notes) if f.endswith(".md")]
        self.assertGreaterEqual(len(files), 3, f"expected the shipped example notes, found {files}")
        for f in files:
            self.assertGreater(os.path.getsize(os.path.join(notes, f)), 400, f)
        if shutil.which("git") and os.path.isdir(os.path.join(ROOT, ".git")):
            # --no-index tests the ignore RULES, not whether the files happen to be tracked
            # already. Without it this passes even when the rule would exclude them from a
            # fresh clone, which is exactly how they were lost in the first place.
            r = subprocess.run(["git", "-C", ROOT, "check-ignore", "--no-index",
                                os.path.join(notes, files[0])], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0,
                                "a .gitignore rule excludes examples/notes, so it will never "
                                "reach a user even though it is present locally")


class TestAliasPricing(unittest.TestCase):
    """`claude --model` and config.json both accept bare aliases, but a price table is keyed by
    concrete ids and matched by substring — so "sonnet" matched nothing and every cost involving
    an alias came out as zero or "unpriced". Found by an independent mutation battery: the 193-test
    suite that existed when this was fixed did not catch its removal."""

    def test_every_alias_resolves_to_a_real_price(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        sys.path.insert(0, os.path.join(ROOT, "scripts", "cost"))
        import lacuna_config, ccost
        for alias in ("haiku", "sonnet", "opus", "fable"):
            with self.subTest(alias):
                concrete = lacuna_config.resolve(alias)
                self.assertNotEqual(concrete, alias, f"{alias} must resolve to a concrete id")
                self.assertIn(concrete, lacuna_config.prices(), f"{concrete} is not in the table")
                row = ccost.price(alias)
                self.assertEqual(row, ccost.price(concrete),
                                 f"{alias} must price the same as {concrete}")
        # an alias must not silently fall through to the generic fallback
        self.assertNotEqual(ccost.price("haiku"), ccost.price("totally-unknown-model"),
                            "an alias priced as the fallback is the zero-cost bug returning")

    def test_a_pinned_id_still_works(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts", "cost"))
        import ccost
        self.assertGreater(ccost.price("claude-opus-5-20260101")[0], 0)
