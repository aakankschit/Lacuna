"""Proposed tests for the tenth-class findings. Each must FAIL at 5d94f97."""
import json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT, Sandbox, call, jsonl, usage, write
import ccost


class TestIterations(unittest.TestCase):
    def test_a_call_is_priced_over_all_its_iterations(self):
        # Shape copied from a real Claude Code transcript: top-level usage == the LAST iteration only.
        first = {"type": "message", "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 200_000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 200_000}}
        last = {"type": "message", "input_tokens": 0, "output_tokens": 1_000, "cache_read_input_tokens": 200_000,
                "cache_creation_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
        u = {k: v for k, v in last.items() if k != "type"}
        u["iterations"] = [first, last]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-13T00:00:00Z",
                                    "message": {"id": "m1", "model": "claude-opus-5-5", "content": [], "usage": u}}) + "\n")
            s, _ = ccost.split(ccost.load(p)[0])
        # opus-5-5: 1h write $8, read $0.20, output $20 per MTok
        self.assertAlmostEqual(sum(s.values()), (200_000 * 8 + 200_000 * 0.20 + 1_000 * 20) / 1e6, places=6)


class TestGuardIgnoresApiErrorLines(Sandbox):
    def test_a_session_limit_line_does_not_hide_the_context(self):
        then = datetime.now(timezone.utc) - timedelta(hours=3)
        real = call("m1", "claude-opus-5-5", then, usage(0, 1_000, 99_000, 500))
        err = call("e1", "<synthetic>", then + timedelta(seconds=5), usage(0, 0, 0, 0))
        err["isApiErrorMessage"] = True
        err["message"]["content"] = [{"type": "text", "text": "You've hit your session limit"}]
        tp = jsonl(self.p("t.jsonl"), [real, err])
        r = self.run_py([os.path.join(ROOT, "hooks", "cache_guard.py")],
                        input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s",
                                          "transcript_path": tp, "prompt": "go on"}))
        self.assertEqual(json.loads(r.stdout or "{}").get("decision"), "block", r.stdout + r.stderr)


class TestTildeDataDir(Sandbox):
    def test_a_tilde_in_LACUNA_DATA_is_the_home_directory(self):
        write(os.path.join(self.home, "lac", "config.json"), json.dumps({"models": {"reader": "from-file"}}))
        r = self.lacuna("config", "--json", LACUNA_DATA="~/lac")
        self.assertEqual(json.loads(r.stdout)["models"]["reader"], "from-file", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.p("~")))
