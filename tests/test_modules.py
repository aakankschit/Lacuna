"""Module level: one script at a time, as a process, in a sandbox with nothing installed.

Each class drives one command the way its caller does — argv in, exit code and output out — on a
throwaway HOME and data directory, with a fake `claude` first on PATH that no test here invokes.
What belongs here is a script's contract: exit codes, the numbers it prints, what it writes, and
what it must survive. Whole journeys through the pipeline are in test_pipeline.py. See TESTING.md.

Run: python3 -m unittest discover -s tests
"""
import json, os, re, shutil, subprocess, sys, time, unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import LIT, PY, ROOT, SAFE_PATH, Sandbox, call, jsonl, read, strip_ansi, usage, write

LIT_COST = os.path.join(LIT, "lit_cost.py")
SHIPPED_CAL = os.path.join(LIT, "calibration.json")
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
STARTUP, HAIKU_TIER = 3_700, 100_000
TOP_KEYS = {"schema", "documents", "total_tokens", "per_agent", "readers", "largest_reader_tokens",
            "long_context_surcharge", "calibration", "budget_warn_usd", "estimates", "any_over_budget",
            "cheapest_over_budget", "synthesis", "prices_retrieved", "warnings", "configured"}


# --------------------------------------------------------------------------------------------
# lit estimate: the number the user approves before anything is spent (contract: the 2026-10-13
# dev-cycle run pinned the JSON schema; R-numbers below refer to it)
# --------------------------------------------------------------------------------------------

class TestEstimate(Sandbox):

    def est(self, *args, **extra):
        return self.run_py([LIT_COST, *args], **extra)

    def est_json(self, *args, **extra):
        """Run with --json; assert exit 0 and that stdout is exactly one JSON object (R1)."""
        r = self.est(*(args if "--json" in args else (*args, "--json")), **extra)
        self.assertEqual(r.returncode, 0, f"exit {r.returncode}; stderr={r.stderr[-600:]}")
        doc, end = json.JSONDecoder().raw_decode(r.stdout.lstrip())
        self.assertEqual(r.stdout.lstrip()[end:].strip(), "", "stdout must hold one JSON object and nothing else")
        return doc

    def doc(self, name, ktokens=1, math=False):
        """A file of exactly 1000*ktokens tokens: 2900 ASCII or 2100 math characters per thousand."""
        return write(self.p(name), ("α" * int(2100 * ktokens)) if math else ("a" * int(2900 * ktokens)))

    def docs(self, n, ktokens=1):
        return [self.doc(f"f{i}.md", ktokens) for i in range(n)]

    def cal(self, obj):
        write(os.path.join(self.data, "calibration.json"), json.dumps(obj))

    def shipped(self):
        with open(SHIPPED_CAL) as f:
            return json.load(f)

    def cell(self, doc, model, effort):
        for e in doc["estimates"]:
            if (e["model"], e["effort"]) == (model, effort):
                return e
        self.fail(f"no estimate for {model}/{effort}; got {[(e['model'], e['effort']) for e in doc['estimates']]}")

    def close(self, got, want, what=""):
        self.assertAlmostEqual(got, want, delta=abs(want) * 1e-9 + 1e-12, msg=f"{what}: {got!r} != {want!r}")

    def prices(self):
        r = self.run_py([os.path.join(ROOT, "scripts", "lacuna_config.py"), "--json"])
        return json.loads(r.stdout)["prices"]

    def test_json_is_pure_exactly_the_schema_and_position_independent(self):
        """R1/R2: no header, table or footer on stdout; exact keys; JSON-native types."""
        a, b = self.doc("a.md"), self.doc("b.md")
        doc = self.est_json(a)
        self.assertEqual(set(doc), TOP_KEYS)
        for k in ("schema", "documents", "total_tokens", "per_agent", "readers", "largest_reader_tokens"):
            self.assertIs(type(doc[k]), int, k)
        for k in ("long_context_surcharge", "any_over_budget"):
            self.assertIs(type(doc[k]), bool, k)
        self.assertEqual(doc["schema"], 1)
        for e in doc["estimates"]:
            self.assertEqual(set(e), {"model", "effort", "usd", "minutes", "over_budget"})
            self.assertIsInstance(e["usd"], float)
            self.assertIsInstance(e["minutes"], float)
            self.assertIs(type(e["over_budget"]), bool)
        self.assertEqual(self.est_json("--json", a, b), self.est_json(a, "--json", b))
        self.assertEqual(self.est_json(a, "--json", b)["documents"], 2, "--json between two paths is still a flag")
        self.assertEqual(self.calls(), [], "estimating must never call a model")

    def test_tokens_grouping_and_waves(self):
        """R3/R6: discovery is dir children sorted then file args in order; groups are consecutive
        slices; minutes follow the largest group and the 8-wide reader pool."""
        d = self.p("papers")
        write(os.path.join(d, "b.md"), "b" * 5800)
        write(os.path.join(d, "a.md"), "a" * 2900)
        write(os.path.join(d, "skip.png"), "x" * 9999)
        doc = self.est_json(d, self.doc("z.md", 1), self.doc("y.md", 5), "--per-agent", "2", "--model", "haiku", "--effort", "low")
        self.assertEqual((doc["documents"], doc["total_tokens"], doc["per_agent"], doc["readers"]), (4, 9000, 2, 2))
        self.assertEqual(doc["largest_reader_tokens"], 6000 + STARTUP, "groups (3k),(6k): the largest is the last")
        mpp = self.shipped()["haiku"]["low"]["minutes_per_paper"]
        self.close(self.cell(doc, "haiku", "low")["minutes"], mpp * 2, "minutes: largest group holds 2 documents")
        self.assertEqual(self.est_json(self.doc("m.md", math=True))["total_tokens"], 1000, "math-heavy text uses the 2.1 ratio")
        files = self.docs(9)
        for val, per_agent, readers in (("0", 1, 9), ("-7", 1, 9), ("99", 99, 1)):
            doc = self.est_json(*files, "--per-agent", val, "--model", "haiku", "--effort", "low")
            self.assertEqual((doc["per_agent"], doc["readers"]), (per_agent, readers), f"--per-agent {val}")
        one_wave = self.est_json(*files[:8], "--model", "haiku", "--effort", "low")
        two_waves = self.est_json(*files, "--model", "haiku", "--effort", "low")
        self.close(one_wave["estimates"][0]["minutes"], mpp, "8 readers run at once")
        self.close(two_waves["estimates"][0]["minutes"], 2 * mpp, "a ninth document is a second wave")

    def test_every_shipped_cell_matches_the_calibration_unrounded(self):
        """R3: all 15 shipped cells; usd = total/1000 * usd_per_ktok, never rounded or shared."""
        doc = self.est_json(*self.docs(3, 2), "--per-agent", "2")     # 6000 tokens, largest group 2
        cal = self.shipped()
        self.assertEqual(len(doc["estimates"]), 15)
        self.assertEqual(len({e["usd"] for e in doc["estimates"]}), 15, "rows must not share one dict")
        for e in doc["estimates"]:
            c = cal[e["model"]][e["effort"]]
            self.close(e["usd"], 6 * c["usd_per_ktok"], f"{e['model']}/{e['effort']} usd")
            self.close(e["minutes"], c["minutes_per_paper"] * 2, f"{e['model']}/{e['effort']} minutes")
            self.assertNotEqual(e["usd"], round(e["usd"], 2), "usd must not be rounded to cents")
        self.cal({"m": {"low": {"usd_per_ktok": 1, "minutes_per_paper": 2}}})
        e = self.est_json(self.doc("a.md"))["estimates"][0]
        self.assertEqual((type(e["usd"]), type(e["minutes"])), (float, float), "integers in a hand-written calibration")

    def test_order_and_coverage_follow_the_calibration_file(self):
        """R4: models in file order (not sorted), efforts in the fixed order, empty cells omitted."""
        mk = lambda u: {"usd_per_ktok": u, "minutes_per_paper": 1.0}
        self.cal({"_source": "mine", "paper_tokens": 1, "zeta": {"low": mk(.001)}, "alpha": {"low": mk(.002)},
                  "m": {"max": mk(.05), "medium": {}, "low": mk(.01)}, "empty": {}})
        doc = self.est_json(self.doc("a.md"))
        self.assertEqual([(e["model"], e["effort"]) for e in doc["estimates"]],
                         [("zeta", "low"), ("alpha", "low"), ("m", "low"), ("m", "max")])

    def test_filters_restrict_estimates_and_the_verdict_only(self):
        """R5: --model and --effort narrow estimates, any_over_budget and cheapest_over_budget;
        documents, tokens, readers and synthesis are unchanged."""
        files = self.docs(4)
        full = self.est_json(*files, "--per-agent", "2")
        one = self.est_json(*files, "--per-agent", "2", "--model", "sonnet", "--effort", "xhigh")
        self.assertEqual([(e["model"], e["effort"]) for e in one["estimates"]], [("sonnet", "xhigh")])
        for k in ("documents", "total_tokens", "readers", "synthesis", "largest_reader_tokens"):
            self.assertEqual(one[k], full[k], f"filters must not change {k}")
        self.assertEqual({e["effort"] for e in self.est_json(*files, "--effort", "max")["estimates"]}, {"max"})
        self.config({"budget_warn_usd": 0.05})
        self.cal({"cheap": {"low": {"usd_per_ktok": .001, "minutes_per_paper": 1}},
                  "dear": {"low": {"usd_per_ktok": .5, "minutes_per_paper": 1}}})
        self.assertEqual(self.est_json(*files)["cheapest_over_budget"]["model"], "dear")
        narrowed = self.est_json(*files, "--model", "cheap")
        self.assertFalse(narrowed["any_over_budget"])
        self.assertIsNone(narrowed["cheapest_over_budget"])
        nothing = self.est_json(*files, "--effort", "max")
        self.assertEqual((nothing["estimates"], nothing["any_over_budget"]), ([], False), "filtering to nothing is not a crash")

    def test_haiku_surcharge_applies_above_the_tier_to_the_key_haiku_only(self):
        """R7: 5x for a row that is the haiku tier however it is named (`haiku`, `haiku-5-5`,
        `claude-haiku-5-5`), but not `myhaiku`; strictly above 100,000 reader tokens, with a
        warning naming --per-agent; and lowering --per-agent clears it."""
        mk = lambda: {"low": {"usd_per_ktok": .001, "minutes_per_paper": 1}}
        self.cal({"haiku": mk(), "haiku-5-5": mk(), "claude-haiku-5-5": mk(), "myhaiku": mk(), "sonnet": mk()})
        at = write(self.p("at.md"), "a" * 279270)        # 96,300 tokens + 3,700 = 100,000 exactly
        doc = self.est_json(at)
        self.assertEqual(doc["largest_reader_tokens"], HAIKU_TIER)
        self.assertFalse(doc["long_context_surcharge"], "100000 is not > 100000")
        self.assertFalse(any("--per-agent" in w for w in doc["warnings"]))
        over = write(self.p("over.md"), "a" * 279273)
        doc = self.est_json(over)
        self.assertTrue(doc["long_context_surcharge"])
        self.assertTrue(any("--per-agent" in w for w in doc["warnings"]), doc["warnings"])
        tokens = 279273 / 2.9
        self.close(self.cell(doc, "haiku", "low")["usd"], tokens / 1000 * .001 * 5, "haiku x5")
        for m in ("haiku-5-5", "claude-haiku-5-5"):
            self.close(self.cell(doc, m, "low")["usd"], tokens / 1000 * .001 * 5, m + " x5")
        for m in ("myhaiku", "sonnet"):
            self.close(self.cell(doc, m, "low")["usd"], tokens / 1000 * .001, m)
        files = [write(self.p(f"h{i}.md"), "a" * (2900 * 60)) for i in range(3)]
        self.assertTrue(self.est_json(*files, "--per-agent", "3")["long_context_surcharge"])
        self.assertFalse(self.est_json(*files, "--per-agent", "1")["long_context_surcharge"])

    def test_calibration_provenance(self):
        """R8: the shipped file is `upstream`; a file in the data directory wins outright."""
        doc = self.est_json(self.doc("a.md"))
        self.assertEqual(doc["calibration"], {"source": "upstream", "path": os.path.realpath(SHIPPED_CAL),
                                              "measured": self.shipped()["_measured"]})
        self.assertIn("Calibration: upstream", self.est(self.doc("a.md")).stdout)
        # a data-directory copy still tagged upstream is a stale copy of the shipped file (doctor
        # used to make one): the plugin's current numbers win, and the user is told it can go
        self.cal({"_source": "upstream", "old": {"low": {"usd_per_ktok": .007, "minutes_per_paper": 1.5}}})
        r = self.est(self.doc("a.md"), "--json")
        doc = json.loads(r.stdout)
        self.assertEqual(doc["calibration"]["path"], os.path.realpath(SHIPPED_CAL))
        self.assertNotIn("old", {e["model"] for e in doc["estimates"]})
        self.assertIn("stale copy", r.stderr)
        for src, want in ((None, "yours"), ("somebody", "yours")):
            with self.subTest(src=src):
                self.cal({**({"_source": src} if src else {}), "mine": {"low": {"usd_per_ktok": .007, "minutes_per_paper": 1.5}}})
                doc = self.est_json(self.doc("a.md"))
                self.assertEqual(doc["calibration"]["source"], want)
                self.assertEqual(doc["calibration"]["path"], os.path.join(self.data, "calibration.json"))
                self.assertIsNone(doc["calibration"]["measured"])
                self.assertEqual([e["model"] for e in doc["estimates"]], ["mine"], "its models alone are used")
        for bad in ("[]", "{oops"):      # hand-edited; it once ended the estimate with a traceback
            with self.subTest(calibration=bad):
                write(os.path.join(self.data, "calibration.json"), bad)
                doc = self.est_json(self.doc("a.md"))
                self.assertEqual(doc["calibration"]["source"], "upstream", "fall back to the shipped calibration")
                self.assertTrue([w for w in doc["warnings"] if "unusable" in w], doc["warnings"])

    def test_synthesis_is_priced_for_the_configured_planner(self):
        """R9: the planner comes from config, aliases resolve, the longest price key wins, and a
        model with no price gives null plus a warning rather than $0.00."""
        pr = self.prices()

        def want(key, ctx):
            return ctx * pr[key][2] / 1e6 + 8000 * pr[key][4] / 1e6   # 1h cache write + output
        a, b = self.doc("a.md"), self.doc("b.md")
        doc = self.est_json(a, b)
        s = doc["synthesis"]
        self.assertEqual((s["model"], s["context_tokens"]), ("claude-opus-5", 1500 * 2 + 6000))
        self.close(s["usd"], want("opus-5", s["context_tokens"]), "default planner")
        for planner, key in (("claude-opus-5-5", "opus-5-5"), ("sonnet", "sonnet-5-5"), ("fable", "fable-5-1")):
            with self.subTest(planner):
                s = self.est_json(a, LACUNA_PLANNER=planner)["synthesis"]
                self.assertEqual(s["model"], planner)
                self.close(s["usd"], want(key, s["context_tokens"]), planner)
        self.config({"models": {"planner": "my-sonnet-5-5-preview"}})
        self.assertEqual(self.est_json(a)["synthesis"]["model"], "my-sonnet-5-5-preview")
        doc = self.est_json(a, LACUNA_PLANNER="gpt-nothing-at-all")
        self.assertIsNone(doc["synthesis"])
        self.assertTrue(any("gpt-nothing-at-all" in w for w in doc["warnings"]), doc["warnings"])
        self.assertIn("no price for the planner model 'gpt-nothing-at-all'", self.est(a, LACUNA_PLANNER="gpt-nothing-at-all").stdout)
        write(os.path.join(self.data, "pricing.json"), json.dumps({"_retrieved": "1999-01-01", "models": {"freebie-9": [1, 1, 1, 1, 0]}}))
        doc = self.est_json(a, LACUNA_PLANNER="freebie-9")
        self.assertIsNone(doc["synthesis"], "an output price of 0 is no price")
        self.assertEqual(doc["prices_retrieved"], "1999-01-01", "provenance follows the table actually used")

    def test_budget_verdict_uses_the_one_cost_the_table_shows(self):
        """R10: the multiplied Haiku cost is the cost; human and JSON modes name the same set and
        the same cheapest row; ties go to the first; the comparison is strict."""
        big = write(self.p("big.md"), "a" * (2900 * 97))   # one reader over the tier
        self.cal({"haiku": {"low": {"usd_per_ktok": .01, "minutes_per_paper": 1}},
                  "zopus": {"low": {"usd_per_ktok": .02, "minutes_per_paper": 2}}})
        self.config({"budget_warn_usd": 1.5})
        doc = self.est_json(big)              # haiku 0.97 unmultiplied, 4.85 multiplied; zopus 1.94
        self.assertTrue(self.cell(doc, "haiku", "low")["over_budget"], "the multiplied cost is over 1.50")
        self.assertEqual(doc["cheapest_over_budget"]["model"], "zopus")
        human = self.est(big).stdout
        self.assertIn("2 of these combinations cost more than budget_warn_usd", human)
        self.assertRegex(human, r"Cheapest over the line: zopus at low")
        mk = {"low": {"usd_per_ktok": .5, "minutes_per_paper": 1}}
        self.cal({"first": dict(mk), "second": dict(mk)})
        self.config({"budget_warn_usd": .1})
        self.assertEqual(self.est_json(self.doc("a.md"))["cheapest_over_budget"]["model"], "first")
        self.cal({"m": {"low": {"usd_per_ktok": .002, "minutes_per_paper": 1}, "max": {"usd_per_ktok": 4, "minutes_per_paper": 1}}})
        self.config({"budget_warn_usd": 3})
        doc = self.est_json(self.doc("a.md"))
        self.assertEqual((doc["budget_warn_usd"], type(doc["budget_warn_usd"])), (3.0, float), "an integer budget is honoured")
        self.assertEqual([e["over_budget"] for e in doc["estimates"]], [False, True])
        self.config({"budget_warn_usd": .002})
        doc = self.est_json(self.doc("a.md"), "--effort", "low")
        self.assertFalse(doc["any_over_budget"], "cost == threshold is not over")
        self.assertIsNone(doc["cheapest_over_budget"])

    def test_the_configured_row_is_the_one_an_approval_gate_reads(self):
        """`any_over_budget` is true when ANY row of the table is, so a model told to gate approval on
        it was asking about a $2.30 sonnet/max run for a $0.018 haiku/medium one."""
        mk = lambda u: {"usd_per_ktok": u, "minutes_per_paper": 1.0}
        self.cal({"haiku": {"medium": mk(.001)}, "sonnet": {"max": mk(5.0)}})
        self.config({"budget_warn_usd": 1.0})
        f = self.doc("a.md", 10)
        doc = self.est_json(f)
        self.assertTrue(doc["any_over_budget"])
        self.assertEqual(doc["configured"], {"model": "haiku", "effort": "medium", "calibrated": True,
                                             "usd": doc["configured"]["usd"], "over_budget": False})
        self.assertAlmostEqual(doc["configured"]["usd"], 10_000 / 1000 * .001, places=9)
        # the configured row, however it is named in the calibration or in the config
        self.cal({"claude-haiku-5-5": {"medium": mk(.001)}})
        self.assertTrue(self.est_json(f)["configured"]["calibrated"])
        self.config({"budget_warn_usd": 1.0, "models": {"reader": "claude-haiku-5-5"}})
        self.assertTrue(self.est_json(f)["configured"]["calibrated"])
        self.cal({"haiku": {"medium": mk(1.0)}})
        self.assertTrue(self.est_json(f, "--model", "haiku", "--effort", "low")["configured"]["over_budget"],
                        "a filter narrows the table, not the question")
        self.config({"budget_warn_usd": 1.0, "models": {"reader": "opus"}})
        doc = self.est_json(f)
        self.assertEqual(doc["configured"],
                         {"model": "opus", "effort": "medium", "calibrated": False, "usd": None, "over_budget": True},
                         "an unknown cost is gated, never null")
        self.assertTrue(any("opus" in w and "unknown" in w for w in doc["warnings"]), doc["warnings"])

    def test_the_configured_row_follows_model_and_effort_flags(self):
        """`lit estimate --model sonnet` priced the run as the configured haiku one, so a gate on
        `configured` approved `lit read --model sonnet` at the haiku figure."""
        mk = lambda u: {"usd_per_ktok": u, "minutes_per_paper": 1.0}
        self.cal({"haiku": {"medium": mk(.001), "high": mk(.002)}, "sonnet": {"medium": mk(5.0)}})
        self.config({"budget_warn_usd": 1.0})
        f = self.doc("a.md", 10)
        self.assertFalse(self.est_json(f)["configured"]["over_budget"])
        c = self.est_json(f, "--model", "sonnet")["configured"]
        self.assertEqual((c["model"], c["effort"], c["over_budget"]), ("sonnet", "medium", True))
        c = self.est_json(f, "--effort", "high")["configured"]
        self.assertEqual((c["model"], c["effort"]), ("haiku", "high"))
        self.assertAlmostEqual(c["usd"], 10_000 / 1000 * .002, places=9)

    def test_a_mis_shaped_calibration_unreadable_files_and_foreign_types_are_refused_not_tracebacks(self):
        """D11: each of these was a traceback (or, in text mode, a table of nan)."""
        f = self.doc("a.md")
        for name, raw in {"string number": '{"haiku": {"low": {"usd_per_ktok": "0.1", "minutes_per_paper": 1}}}',
                          "missing field": '{"haiku": {"low": {"usd_per_ktok": 0.1}}}',
                          "cell is a number": '{"haiku": {"low": 5}}',
                          "NaN": '{"haiku": {"low": {"usd_per_ktok": NaN, "minutes_per_paper": 1}}}',
                          "negative": '{"haiku": {"low": {"usd_per_ktok": -1, "minutes_per_paper": 1}}}'}.items():
            for mode in ((), ("--json",)):
                with self.subTest(name, json=bool(mode)):
                    write(os.path.join(self.data, "calibration.json"), raw)
                    r = self.est(f, *mode)
                    self.assertEqual(r.returncode, 0, r.stderr[-300:])
                    self.assertNotIn("Traceback", r.stderr)
                    self.assertIn("unusable", r.stderr, "the shipped calibration is used, and the user is told so")
                    if mode:
                        self.assertEqual(json.loads(r.stdout)["calibration"]["source"], "upstream")
        os.remove(os.path.join(self.data, "calibration.json"))
        locked = write(self.p("locked.md"), "x" * 3000)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o644)
        folder = os.path.join(self.p("dir"), "folder.md")
        os.makedirs(folder)
        write(os.path.join(self.p("dir"), "real.md"), "x" * 3000)
        for name, args, code in (("unreadable file", [locked], 2), ("a .docx by name", [write(self.p("p.docx"), "x" * 3000)], 2),
                                 ("a directory named *.md", [self.p("dir")], 0)):
            with self.subTest(name):
                if name == "unreadable file" and os.access(locked, os.R_OK):
                    self.skipTest("running as a user who can read anything")
                r = self.est(*args, "--json")
                self.assertEqual((r.returncode, "Traceback" in r.stderr), (code, False), r.stderr[-300:])
                if code == 0:
                    self.assertEqual(json.loads(r.stdout)["documents"], 1, "the folder is not a document")
                else:
                    self.assertEqual(r.stdout, "")

    def test_a_directory_of_uppercase_extensions_is_priced(self):
        d = self.p("docs")
        write(os.path.join(d, "A.MD"), "a" * 3000)
        write(os.path.join(d, "B.TXT"), "b" * 3000)
        self.assertEqual(self.est_json(d)["documents"], 2)

    def test_a_calibration_warning_is_in_the_report_only_when_that_file_is_in_use(self):
        write(os.path.join(self.data, "calibration.json"), "[1]")
        f = self.doc("a.md")
        r = self.est(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unusable", r.stderr, "text mode says so on stderr")
        self.assertTrue(any("unusable" in w for w in json.loads(self.est(f, "--json").stdout)["warnings"]))
        code = ("import sys, json; sys.path.insert(0, %r)\nimport lit_cost as C\n"
                "r, x = C.estimate([%r], 1, cal=C.json.load(open(%r)))\nprint(json.dumps(r['warnings']))\n") % (
                LIT, f, SHIPPED_CAL)
        r = self.run_py(["-c", code])
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, []), "an explicit calibration inherits no warning")

    def test_bad_budget_disables_the_check_with_a_warning(self):
        """R11: config.json is hand-edited. A non-number warns and disables; 0, negative and
        null disable silently; a traceback is never the answer."""
        for name, raw in [("string", '"2"'), ("bool", "true"), ("list", "[2]"), ("dict", '{"usd": 2}'),
                          ("empty", '""'), ("inf", "Infinity"), ("-inf", "-Infinity"), ("nan", "NaN")]:
            with self.subTest(name):
                write(os.path.join(self.data, "config.json"), '{"budget_warn_usd": %s}' % raw)
                doc = self.est_json(self.doc("a.md"))
                self.assertIsNone(doc["budget_warn_usd"])
                self.assertFalse(doc["any_over_budget"])
                self.assertTrue(any("budget_warn_usd" in w for w in doc["warnings"]), doc["warnings"])
        for val in (0, -5, None):
            with self.subTest(val=val):
                self.config({"budget_warn_usd": val})
                doc = self.est_json(self.doc("a.md"))
                self.assertIsNone(doc["budget_warn_usd"])
                self.assertFalse(any("budget_warn_usd" in w for w in doc["warnings"]), "disables silently")
        self.config({"budget_warn_usd": "2"})
        r = self.est(self.doc("a.md"))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("budget_warn_usd", r.stderr)
        self.assertNotIn("combinations cost more", r.stdout)
        self.assertEqual(self.est_json(self.doc("a.md"), LACUNA_DATA=self.p("fresh"))["budget_warn_usd"], 2.0, "the shipped default")

    def test_refusals_exit_2_with_nothing_on_stdout_in_json_mode(self):
        """R12/R13: bad input exits 2 in both modes, never a traceback, and JSON mode prints no
        partial object. The unknown-model message is kept verbatim."""
        f = self.doc("a.md")
        empty = self.p("empty")
        # A file neither `estimate` nor `read` accepts. It used to be notes.rst, which encoded the
        # old mismatch — estimate ignored .rst while read charged for it — as intended behaviour.
        write(os.path.join(empty, "notes.docx"), "x")
        cases = [([f, "--effort", "extreme"], None), ([f, "--effort", "LOW"], None), ([f, "--model"], None),
                 ([f, "--model", "gpt-nothing"], "no calibration for model 'gpt-nothing'"),
                 ([self.p("nope.md")], "no such path"), ([empty], "no .md"), ([f, "--jsonn"], None), ([], None)]
        for argv, needle in cases:
            for mode in ([], ["--json"]):
                with self.subTest(argv=argv, json=bool(mode)):
                    r = self.est(*argv, *mode)
                    self.assertEqual(r.returncode, 2, r.stderr[-300:])
                    self.assertNotIn("Traceback", r.stderr)
                    if needle:
                        self.assertIn(needle, r.stderr)
                    if mode:
                        self.assertEqual(r.stdout, "", "R13: nothing on stdout on error")
        for e in EFFORTS:
            self.assertEqual({x["effort"] for x in self.est_json(f, "--effort", e)["estimates"]}, {e})
        r = self.est("--help")
        self.assertIn(r.returncode, (0, 2))
        self.assertIn("--json", r.stdout + r.stderr)
        # the help text is generated from READABLE, so it cannot drift from what the scan accepts
        exts = self.run_py(["-c", f"import sys; sys.path.insert(0, {LIT!r}); import lit_cost; "
                                  f"print(*lit_cost.READABLE)"]).stdout.split()
        self.assertGreater(len(exts), 3)
        for e in exts:
            with self.subTest(ext=e):
                self.assertIn(e, " ".join(r.stdout.split()))

    def test_unreadable_pdf_is_refused_rather_than_priced_at_zero(self):
        """Exit 2 when a PDF yields no text; exit 3 when there is no extractor at all. Never
        "$0.00, exit 0", which told a user the run was free."""
        pdf = write(self.p("broken.pdf"), "%PDF-1.4 not really a pdf")
        r = self.est(pdf, LACUNA_PYTHON=self.p("no-venv", "python"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        # the extension test is case-insensitive, as the directory scan is: an uppercase .PDF was
        # priced as raw text (its bytes counted as words) instead of going through the extractor
        upper = write(self.p("BROKEN2.PDF"), "%PDF-1.4 not really a pdf")
        r = self.est(upper, LACUNA_PYTHON=self.p("no-venv", "python"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.est(pdf, "--json", LACUNA_PYTHON=self.p("no-venv", "python")).stdout, "")
        silent = write(self.p("venv", "python"), "#!/bin/sh\nexit 0\n", 0o755)   # a parser that extracts nothing
        r = self.est(pdf, LACUNA_PYTHON=silent)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("no extractable text", r.stderr)
        self.assertNotIn("$   0.00", r.stdout)
        failing = write(self.p("venv2", "python"), "#!/bin/sh\nexit 1\n", 0o755)   # a parser that cannot open it
        r = self.est(pdf, LACUNA_PYTHON=failing)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("cannot read", r.stderr)
        self.assertNotIn("install-pdf", r.stderr, "the parser is installed; the file is the problem")

    def test_human_mode_prints_header_table_and_footer_that_agree_with_json(self):
        """R14: without --json the table is unchanged in shape and reports the same counts."""
        files = self.docs(4)
        r = self.est(*files, "--per-agent", "3")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        js = self.est_json(*files, "--per-agent", "3")
        self.assertIn(f"{js['documents']} documents, ~{js['total_tokens']:,} tokens; {js['readers']} reader(s) "
                      f"at {js['per_agent']} per reader (largest reader context ~{js['largest_reader_tokens']:,} tokens)", r.stdout)
        for m in ("haiku", "sonnet", "claude-opus-5"):
            self.assertRegex(r.stdout, rf"(?m)^{m}\s+\$")
        self.assertIn("synthesis over 4 notes with claude-opus-5", r.stdout)
        self.assertIn(SHIPPED_CAL, r.stdout)
        self.assertEqual(r.stdout.count("{"), 0, "human mode must not print JSON")
        r = self.est(files[0], "--model", "haiku", "--effort", "medium")
        self.assertNotRegex(r.stdout, r"(?m)^sonnet\s+\$", "the table has one row; the footer may name any path")
        self.assertNotIn("xhigh", r.stdout)

    def test_estimate_does_not_mutate_its_inputs(self):
        """R15/A3: one CLI call visits each cell once, so in-place edits of the calibration are
        invisible to it; the function is probed in process instead."""
        self.cal({"_source": "mine", "haiku": {e: {"usd_per_ktok": .001, "minutes_per_paper": 1} for e in EFFORTS}})
        big = self.doc("big.md", 120)          # surcharge on, so the 5x path runs
        probe = ("import copy, json, sys\nsys.path.insert(0, sys.argv[1])\nimport lit_cost, lacuna_config\n"
                 "cal = copy.deepcopy(lit_cost.CAL); before = copy.deepcopy(cal)\n"
                 "cfg = copy.deepcopy(lacuna_config.CONFIG); cfg_before = copy.deepcopy(cfg)\n"
                 "a, _ = lit_cost.estimate([sys.argv[2]], 1, cal=cal, config=cfg)\n"
                 "b, _ = lit_cost.estimate([sys.argv[2]], 1, cal=cal, config=cfg)\n"
                 "print(json.dumps([cal == before, cfg == cfg_before, a == b]))\n")
        r = self.run_py(["-c", probe, LIT, big])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        self.assertEqual(json.loads(r.stdout), [True, True, True])
        cal_path = os.path.join(self.data, "calibration.json")
        before = read(cal_path)
        self.est_json(big)
        self.assertEqual(read(cal_path), before, "the calibration file is never rewritten")

    def test_usage_and_skill_name_the_json_flag(self):
        """R16: the two documents that tell a model how to call this."""
        usage_lines = [l for l in read(os.path.join(ROOT, "bin", "lacuna")).splitlines() if "lacuna lit estimate" in l]
        self.assertTrue(any("--json" in l for l in usage_lines), usage_lines)
        # every flag a subcommand accepts is in its entry of `lacuna --help` (--overwrite, the flag
        # every refusal names, was missing)
        usage = self.lacuna("--help").stdout.splitlines()
        for entry, script, args in (
                ("  lacuna doctor", os.path.join(ROOT, "scripts", "doctor.py"), []),
                ("  lacuna spend", os.path.join(ROOT, "scripts", "cost", "spend.py"), []),
                ("  lacuna lit read", os.path.join(LIT, "lit_pipeline.py"), ["read"]),
                ("  lacuna lit synth", os.path.join(LIT, "lit_pipeline.py"), ["synth"]),
                ("  lacuna lit verify", os.path.join(LIT, "lit_pipeline.py"), ["verify"]),
                ("  lacuna lit estimate", LIT_COST, [])):
            with self.subTest(entry=entry):
                i = next(n for n, l in enumerate(usage) if l.startswith(entry))
                text = usage[i]
                for l in usage[i + 1:]:
                    if not l.startswith(" " * 18):          # the command's own continuation lines
                        break
                    text += " " + l
                helped = self.run_py([script, *args, "--help"]).stdout
                flags = set(re.findall(r"(?m)^\s+(?:-\w, )?(--[a-z][\w-]*)", helped)) - {"--help"}
                self.assertGreater(len(flags), 0)
                self.assertEqual(flags - set(re.findall(r"--[a-z][\w-]*", text)), set(), text)
        skill = read(os.path.join(ROOT, "skills", "lit-review", "SKILL.md"))
        self.assertIn("lit estimate", skill)
        self.assertIn("configured.over_budget", skill, "the skill must name the field it gates approval on")


# --------------------------------------------------------------------------------------------
# spend: the headline number, from fixture transcripts
# --------------------------------------------------------------------------------------------

class TestSpend(Sandbox):
    """Fixture numbers, worked by hand (sonnet-5-5: 2 input, 4 1h-write, 0.10 read, 10 output;
    haiku-5-5 output 0.50 per MTok):

      #0  1k in, 100k write,        1k out  = $0.4120   cold-shaped, but the first call of a session
      #1         1k write, 101k read, 2k out = $0.0341
      #2         150k write,         1k out  = $0.6100   a cold rewrite
      session $1.0561; without caching $0.746; subagents 0.05 + 0.10 (haiku) + 0.10 (sonnet) = $0.25
    """
    SID = "aaaa1111-0000-0000-0000-000000000000"

    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.home, ".claude", "projects", "-work-proj")
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
        self.assertEqual(r.returncode, 0, r.stderr)
        session, fleet = strip_ansi(r.stdout).split("■ Last")
        return session, fleet

    def test_session_report_numbers_and_subagent_rollup(self):
        s, _ = self.spend("--session", self.session)
        self.assertIn("3 API calls over 2.0 h", s, "duplicate message ids must collapse")
        self.assertIn("$1.06 list price", s)
        self.assertIn("same tokens without caching: $0.75", s)
        self.assertIn("1 cold rewrites cost $0.61", s, "the first call of a session is not a rewrite; the 150k write after it is")
        line = next(l for l in s.splitlines() if "subagents:" in l)
        self.assertIn("subagents: 3 runs, $0.25", line)
        self.assertIn("haiku-5-5 ×2 $0.15", line)
        self.assertIn("sonnet-5-5 ×1 $0.10", line)
        # 13 of 309 real transcripts: a session that died on its first call, with all-zero usage.
        died = jsonl(os.path.join(self.proj, "dead0000.jsonl"), [call("d0", "claude-opus-5-5", datetime.now(timezone.utc), usage())])
        s, _ = self.spend("--session", died)
        self.assertIn("1 API calls", s)
        self.assertIn("$0.00 list price", s)

    def test_fleet_report_agrees_with_the_session_and_honours_the_window(self):
        """The session report skipped a session's first call when counting cold rewrites and the
        fleet report did not, so one transcript was "1 cold rewrite" and "2 cold rewrites"."""
        old = datetime.now(timezone.utc) - timedelta(days=30)
        jsonl(os.path.join(self.home, ".claude", "projects", "-work-old", "bbbb.jsonl"),
              [call("o1", "claude-sonnet-5-5", old, usage(out=1_000_000))])   # $10, in a file touched today
        _, fleet = self.spend("--session", self.session, "--days", "7")
        self.assertIn("$1.31 list price", fleet, "calls older than the window must not count, whatever the file's mtime")
        self.assertIn("main $1.06", fleet)
        self.assertIn("subagents $0.25", fleet)
        self.assertIn("1 cold rewrites $0.61", fleet)
        self.assertIn("cache hit 28.6%", fleet)       # 101k read of 353k input-side tokens
        self.assertIn("proj $1.31", fleet)

    def test_says_it_is_a_lower_bound_and_shows_refused_attempts_as_a_range(self):
        """/spend under-reported silently. A refused-then-retried call keeps its earlier attempts in
        usage.iterations, outside the top-level usage, and whether they were billed is not in the
        transcript: so the report shows both ends, and says the figure is a lower bound. A forked
        session's copied calls were counted twice by the fleet report."""
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        retried = call("r1", "claude-sonnet-5-5", now, {**usage(out=1_000), "iterations": [
            {**usage(write=100_000), "model": "claude-sonnet-5-5", "type": "message"},
            {**usage(out=1_000), "model": "claude-sonnet-5-5", "type": "message"}]})
        t = jsonl(os.path.join(self.home, ".claude", "projects", "-work-retry", "cccc.jsonl"), [retried])
        with open(self.session) as f:                                   # a fork: the same calls, a new file
            write(os.path.join(self.proj, "fork0000.jsonl"), f.read())
        s, fleet = self.spend("--session", t)
        self.assertIn("$0.01–$0.41 list price, a lower bound", s)       # +100k sonnet 1h write, $0.40
        self.assertIn("1 call(s) were retried", s)
        self.assertIn("$1.32–$1.72 list price, a lower bound", fleet, "a fork's copied calls are counted once")
        self.assertIn("1 call(s) were retried", fleet, "the fleet report says so too, not only the session's")
        s, _ = self.spend("--session", self.session)
        self.assertIn("$1.06 list price, a lower bound", s, "no retries: one figure, still called a lower bound")
        self.assertNotIn("retried", s)

    def test_every_subtotal_shows_the_range_not_only_the_headline(self):
        """The docs say every figure is a lower bound with earlier attempts as the top of a range.
        The subagent line, the per-model and per-kind fleet figures and the category split priced
        only the last attempt, beside a headline that was a range."""
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        sid = "dddd4444-0000-0000-0000-000000000000"
        proj = os.path.join(self.home, ".claude", "projects", "-work-sub")
        main = jsonl(os.path.join(proj, sid + ".jsonl"), [call("zm1", "claude-sonnet-5-5", now, usage(out=1_000))])
        retried = call("zs1", "claude-opus-5-5", now, {**usage(out=1_000), "iterations": [
            {**usage(out=500_000), "model": "claude-opus-5-5", "type": "message"},     # +$10.00 if billed
            {**usage(out=1_000), "model": "claude-opus-5-5", "type": "message"}]})
        jsonl(os.path.join(proj, sid, "subagents", "a.jsonl"), [retried])
        s, fleet = self.spend("--session", main)
        sub = next(l for l in s.splitlines() if "subagents:" in l)
        self.assertIn("subagents: 1 runs, $0.02–$10.02", sub)
        self.assertIn("opus-5-5 ×1 $0.02–$10.02", sub)
        self.assertIn("subagents $0.27–$10.27", fleet)
        self.assertIn("opus-5-5 $0.02–$10.02", fleet)
        self.assertIn("work-sub $0.03–$10.03", fleet)
        # the session's own category split and expensive-turn list, for a retried main-thread call
        t = jsonl(os.path.join(proj, "eeee5555.jsonl"), [call("zr1", "claude-opus-5-5", now, {**usage(out=1_000), "iterations": [
            {**usage(out=500_000), "model": "claude-opus-5-5", "type": "message"},
            {**usage(out=1_000), "model": "claude-opus-5-5", "type": "message"}]})])
        late = now - timedelta(hours=3)
        cold = jsonl(os.path.join(proj, "ffff6666.jsonl"), [
            call("zc1", "claude-opus-5-5", late, usage(0, 0, 100_000, 10)),
            call("zc2", "claude-opus-5-5", now, {**usage(0, 100_000, 0, 10), "iterations": [
                {**usage(out=500_000), "model": "claude-opus-5-5", "type": "message"},
                {**usage(0, 100_000, 0, 10), "model": "claude-opus-5-5", "type": "message"}]})])
        s, fleet = self.spend("--session", cold)
        self.assertRegex(s, r"1 cold rewrites cost \$\d+\.\d\d–\$")
        self.assertRegex(fleet, r"\d cold rewrites \$\d+\.\d\d–\$")
        with open(t, "a") as f:
            f.write("[" * 100_000 + "\n")           # nested past the recursion limit: a torn line, not a crash
        s, _ = self.spend("--session", t)
        self.assertIn("output+thinking $0.02–$10.02", s)
        self.assertIn("$0.020–$10.020", s, "the most-expensive-turns list")

    def test_a_warm_cache_with_a_big_new_write_is_not_labelled_a_cold_rewrite(self):
        """The per-turn label in the session table had its own copy of the old fixed-threshold rule;
        only the count line used is_cold. A call that read 11,115 of the previous 11,117 tokens and
        wrote 43,903 45 s later is the cache working."""
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        s_ = "claude-sonnet-5-5"
        t = jsonl(os.path.join(self.home, ".claude", "projects", "-w-p", "aaaa.jsonl"), [
            call("wa", s_, now - timedelta(seconds=45), usage(0, 11_117, 0, 100)),
            call("wb", s_, now, usage(0, 43_903, 11_115, 100))])
        s, _ = self.spend("--session", t)
        self.assertNotIn("cold rewrite", s)
        late = jsonl(os.path.join(self.home, ".claude", "projects", "-w-p", "bbbb.jsonl"), [
            call("wc", s_, now - timedelta(hours=3), usage(0, 50_000, 0, 100)),
            call("wd", s_, now, usage(0, 52_000, 0, 100))])
        s, _ = self.spend("--session", late)
        self.assertIn("cold rewrite", s)

    def test_session_id_prefix_resolves_and_an_unknown_id_exits_2(self):
        jsonl(os.path.join(self.proj, "zzzz9999.jsonl"), [call("x", "claude-haiku-5-5", datetime.now(timezone.utc), usage(out=10))])
        s, _ = self.spend("--session", "aaaa1111", "--cwd", "/work/proj")
        self.assertIn("aaaa1111", s)
        self.assertIn("3 API calls", s)
        r = self.run_py([os.path.join(ROOT, "scripts", "cost", "spend.py"), "--no-color", "--session", "ffff0000-not-a-session", "--cwd", "/work/proj"])
        self.assertEqual(r.returncode, 2, "an id that matches nothing must not report the cwd's newest session instead")
        self.assertNotIn("aaaa1111", r.stdout)


# --------------------------------------------------------------------------------------------
# status line: runs on every refresh, reads fields that are not a stable interface
# --------------------------------------------------------------------------------------------

class TestStatusline(Sandbox):

    def render(self, payload, columns="120", **env):
        r = self.run_py([os.path.join(ROOT, "statusline", "statusline.py")], input=json.dumps(payload), COLUMNS=columns,
                        **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [strip_ansi(l) for l in r.stdout.splitlines()]

    def payload(self, **over):
        now = datetime.now(timezone.utc)
        transcript = jsonl(self.p("t.jsonl"), [
            call("m1", "claude-opus-5-5", now, {"input_tokens": 10, "cache_creation_input_tokens": 1_000,
                                                "cache_read_input_tokens": 100_000, "output_tokens": 1_000}),
            call("m2", "claude-opus-5-5", now, usage(out=1_000_000), sidechain=True),   # a subagent, not the last turn
            {"type": "user", "message": {"content": "next"}},
            "{ torn write",
        ])
        d = {"model": {"display_name": "Opus 5.5", "id": "claude-opus-5-5"}, "workspace": {"project_dir": "/x/myproj"},
             "context_window": {"current_usage": {"input_tokens": 10, "cache_creation_input_tokens": 1_000,
                                                  "cache_read_input_tokens": 149_000}, "context_window_size": 1_000_000},
             "cost": {"total_cost_usd": 3.5}, "transcript_path": transcript}
        d.update(over)
        return d

    def test_renders_the_last_turn_and_the_cache_state(self):
        """Prices must be the exact model's: matching "opus-5" for opus-5-5 says $1.50 not $1.20.
        And with no prompt_cache field at all (Claude Code omits it before a process's first
        request) the segment says nothing rather than guessing cold."""
        l1, l2 = self.render(self.payload())
        self.assertIn("Opus 5.5", l1)
        self.assertIn("myproj", l1)
        self.assertIn("150k", l1)
        self.assertNotIn("cold", l1)
        self.assertIn("$3.50 session", l2)
        # opus-5-5: 100k read x0.20 + 1k 1h-write x8 + 1k out x20 + 10 in x4, per MTok
        self.assertIn("last $0.048 (read 0.020 · write 0.008 · out 0.020)", l2)
        l1, _ = self.render(self.payload(prompt_cache={"expires_at": time.time() - 60, "ttl": "1h"}))
        self.assertIn("cold · next prompt re-caches ≈$1.20", l1)
        self.assertIn("/compact", l1)
        for pc in ({"hit_ratio": 0.9}, {"expires_at": None, "ttl": "1h"}):
            self.assertNotIn("cold", self.render(self.payload(prompt_cache=pc))[0], f"{pc}: no expiry time is no evidence of a cold cache")
        l1, l2 = self.render(self.payload(prompt_cache={"expires_at": time.time() + 1800, "ttl": "1h", "hit_ratio": 0.93},
                                          rate_limits={"five_hour": {"used_percentage": 42, "resets_at": time.time() + 7200},
                                                       "seven_day": {"used_percentage": None}}))
        self.assertIn("left", l1)
        self.assertIn("hit 93%", l2)
        self.assertIn("42%", l2)
        self.assertNotIn("7d", l2, "a window with no percentage goes blank, not 0%")

    def test_degrades_when_fields_are_absent_null_or_the_terminal_is_narrow(self):
        for name, payload in {"empty object": {},
                              "nulls": {"model": None, "context_window": None, "cost": None, "prompt_cache": None,
                                        "rate_limits": None, "workspace": None, "transcript_path": None},
                              "partial": {"model": {"id": "claude-haiku-5-5"}, "context_window": {"current_usage": {"input_tokens": None}},
                                          "rate_limits": {"five_hour": {}}},
                              "missing transcript": {"transcript_path": self.p("nope.jsonl")}}.items():
            with self.subTest(name):
                l1, l2 = self.render(payload)
                self.assertIn("ctx -", l1)
                self.assertIn("$0.00 session", l2)
                self.assertNotIn("last $", l2)
        p = self.payload(prompt_cache={"hit_ratio": 0.5}, rate_limits={"five_hour": {"used_percentage": 10}})
        self.assertIn("5h", self.render(p, "120")[1])
        narrow = self.render(p, "80")[1]
        self.assertNotIn("5h", narrow)
        self.assertIn("last $", narrow)
        r = self.run_py([os.path.join(ROOT, "statusline", "statusline.py")], input="not json")
        self.assertEqual((r.returncode, r.stderr), (0, ""), "a status line that prints a traceback is worse than none")
        # every field is typed: `true` as a path was open(True), file descriptor 1, and closing it
        # killed the status line's own stdout (exit 120). A wrong-typed field blanks its segment.
        for name, over in {"bool path": {"transcript_path": True}, "int path": {"transcript_path": 1},
                           "list path": {"transcript_path": ["x"]}, "NUL path": {"transcript_path": "a\0b"},
                           "model": {"model": 5}, "model name": {"model": {"display_name": 7, "id": []}},
                           "effort": {"effort": "high"}, "window": {"context_window": [1]},
                           "usage": {"context_window": {"current_usage": {"input_tokens": "9", "cache_read_input_tokens": [1]},
                                                        "context_window_size": "big"}},
                           "nan": {"context_window": {"current_usage": {"input_tokens": float("nan")}}},
                           "cache": {"prompt_cache": {"expires_at": "soon", "hit_ratio": [1], "ttl": 5}},
                           "rates": {"rate_limits": {"five_hour": {"used_percentage": float("inf"), "resets_at": "x"},
                                                     "seven_day": 5}},
                           "cost": {"cost": {"total_cost_usd": "free"}}, "cwd": {"cwd": 5, "workspace": {"project_dir": 3}},
                           "top level": []}.items():
            with self.subTest(name):
                r = self.run_py([os.path.join(ROOT, "statusline", "statusline.py")],
                                input=json.dumps(over if isinstance(over, dict) else over).replace("NaN", "NaN"))
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                if isinstance(over, dict):
                    self.assertIn("session", strip_ansi(r.stdout), "a valid segment is still printed")

    def test_reads_the_data_dir_prices_and_transcript_as_the_cli_does(self):
        """It is self-contained by design, and fell behind the CLI three ways at once: "~" in
        LACUNA_DATA left a user's pricing.json unread (status line $0.020, spend $2.00 for one
        call); a malformed pricing.json blanked it; and after an API error it read Claude Code's
        zero-usage <synthetic> line as the last turn and said "last $0.000"."""
        p = self.payload()
        write(os.path.join(self.home, "lac", "pricing.json"), json.dumps({"opus-5-5": [400, 500, 800, 20, 2000]}))
        self.assertIn("last $4.804 ", self.render(p, LACUNA_DATA="~/lac")[1], "a 100x price under ~/ was ignored")
        for bad in ([], "x", {"models": [1, 2]}, {"models": {"opus-5-5": [1, 2, 3, 4, "x"]}},
                    {"models": {"opus-5-5": "12345"}}, {"models": {"opus-5-5": [float("nan")] * 5}}, "deep"):
            with self.subTest(pricing=bad):
                write(os.path.join(self.data, "pricing.json"), "[" * 100_000 if bad == "deep" else json.dumps(bad))
                self.assertIn("last $0.048 ", self.render(p)[1], "a malformed pricing.json must fall back, not blank")
        os.remove(os.path.join(self.data, "pricing.json"))
        now = datetime.now(timezone.utc)
        with open(p["transcript_path"], "a") as f:
            f.write(json.dumps(call("<synthetic>", "<synthetic>", now, usage())) + "\n")
            f.write('[1, 2]\n{"type": "assistant", "message": "a string"}\n')   # valid JSON, not a call
        self.assertIn("last $0.048 ", self.render(p, columns="")[1], "an API error is not the last turn")
        # A refused first attempt is in usage.iterations and not in the top-level usage. Whether it
        # was billed is not recorded, so the figure is a range, never silently the lower end.
        retried = call("m3", "claude-opus-5-5", now, {**usage(10, 1_000, 100_000, 1_000), "iterations": [
            {**usage(0, 300_000, 0, 0), "model": "claude-opus-5-5", "type": "message"},
            {**usage(10, 1_000, 100_000, 1_000), "model": "claude-opus-5-5", "type": "fallback_message"}]})
        jsonl(p["transcript_path"], [retried])
        self.assertIn("last $0.048–$2.448 ", self.render(p)[1])


# --------------------------------------------------------------------------------------------
# cache guard and results hook
# --------------------------------------------------------------------------------------------

class TestCacheGuard(Sandbox):
    """The hold on the first idle prompt is the hook's whole point; it must let the user through
    on a resend, stay quiet after /compact, and never take a session down with it."""

    def guard(self, payload=None, raw=None, **env):
        return self.run_py([os.path.join(ROOT, "hooks", "cache_guard.py")],
                           input=raw if raw is not None else json.dumps(payload), **env)

    def transcript(self, ago, ctx=100_000, extra=(), with_timestamp=True):
        row = call("m1", "claude-opus-5-5", datetime.now(timezone.utc) - ago, usage(0, 1_000, ctx - 1_000, 500))
        if not with_timestamp:
            del row["timestamp"]
        return jsonl(self.p("t.jsonl"), [row, *extra])

    def prompt(self, tp, sid="s1"):
        return self.guard({"hook_event_name": "UserPromptSubmit", "session_id": sid, "transcript_path": tp, "prompt": "carry on please"})

    def test_session_start_routing_and_cold_resume_warning(self):
        r = self.guard({"hook_event_name": "SessionStart", "source": "startup", "session_id": "t"})
        self.assertIn("Routing", json.loads(r.stdout)["systemMessage"])
        r = self.guard({"hook_event_name": "SessionStart", "source": "resume", "session_id": "t2", "seconds_since_last_response": 9000,
                        "context_tokens": 160_000, "prompt_cache_likely_expired": True, "transcript_path": "/nonexistent"})
        msg = json.loads(r.stdout)["systemMessage"]
        self.assertIn("cache has expired", msg)
        self.assertIn("160k", msg)

    def test_holds_the_first_idle_prompt_then_lets_a_resend_through(self):
        tp = self.transcript(timedelta(hours=3))
        r = self.prompt(tp)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("100k", out["reason"])
        self.assertIn("≈$0.80", out["reason"])                 # 100k x $8/MTok opus-5-5 1h write
        self.assertEqual(read(os.path.join(self.data, "cache_guard", "s1.prompt.txt")), "carry on please")
        r = self.prompt(tp)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), "the resend must go through")
        self.assertEqual(json.loads(self.prompt(tp).stdout)["decision"], "block", "and the next idle prompt is held again")

    def test_never_holds_a_recent_small_or_compacted_session(self):
        self.assertEqual(self.prompt(self.transcript(timedelta(minutes=2))).stdout.strip(), "", "recent")
        self.assertEqual(self.prompt(self.transcript(timedelta(hours=3), ctx=20_000)).stdout.strip(), "", "below LACUNA_MIN_CTX")
        compacted = {"type": "system", "subtype": "compact_boundary", "timestamp": call("x", "m", datetime.now(timezone.utc), {})["timestamp"]}
        self.assertEqual(self.prompt(self.transcript(timedelta(hours=3), extra=[compacted])).stdout.strip(), "",
                         "/compact replaced the context; the pre-compaction figure would quote 3-6x the real cost")
        undated = [{"type": "summary", "summary": "Fix the parser", "leafUuid": "x"},
                   {"type": "system", "subtype": "compact_boundary"}]
        self.assertIn("block", self.prompt(self.transcript(timedelta(hours=3), extra=undated)).stdout,
                      "an undated line is no evidence of compaction and must not switch the guard off")

    def test_judges_the_last_call_and_the_data_dir_as_the_cli_does(self):
        """Claude Code records an API failure as a zero-usage line whose model is <synthetic>; read
        as the newest call it hid a 500k-token re-cache. The first fix skipped any line containing
        the text, so a real call that merely mentioned it was skipped too and an idle hour was
        invented. And if lacuna_config cannot be imported, "~" in LACUNA_DATA must still expand."""
        now = datetime.now(timezone.utc)
        real = call("m1", "claude-opus-5", now - timedelta(hours=3), usage(10, 0, 500_000, 50))
        failed = call("<synthetic>", "<synthetic>", now - timedelta(hours=2, minutes=55), usage())
        r = self.guard({"hook_event_name": "SessionStart", "source": "resume", "session_id": "s",
                        "transcript_path": jsonl(self.p("t1.jsonl"), [real, failed, "[1, 2]"])})
        self.assertIn("500k", r.stdout, "the guard must see the real call's context, not the synthetic zero")
        mentions = call("m2", "claude-opus-5-5", now - timedelta(minutes=2), usage(0, 1_000, 99_000, 500))
        mentions["message"]["content"] = [{"type": "text", "text": 'a line whose model is "<synthetic>" is an error'}]
        self.assertEqual(self.prompt(jsonl(self.p("t2.jsonl"), [real, mentions])).stdout.strip(), "",
                         "a real call two minutes ago was skipped for mentioning <synthetic>")
        alone = write(self.p("standalone", "cache_guard.py"), read(os.path.join(ROOT, "hooks", "cache_guard.py")))
        r = self.run_py([alone], input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                                   "transcript_path": self.transcript(timedelta(hours=3)),
                                                   "prompt": "go"}), LACUNA_DATA="~/guard-data")
        self.assertIn("block", r.stdout, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.home, "guard-data", "cache_guard", "s1.prompt.txt")),
                        "a literal ./~ directory was made beside the cwd")

    def test_never_breaks_a_session(self):
        """A hook that raises breaks every prompt. Garbage input, a transcript line without a
        timestamp, a hand-edited config value and the off switch must all exit 0 quietly."""
        for bad in ("", "not json", "[]", '{"hook_event_name":"Unknown"}'):
            with self.subTest(bad):
                r = self.guard(raw=bad)
                self.assertEqual((r.returncode, r.stdout.strip(), "Traceback" in r.stderr), (0, "", False))
        tp = self.transcript(timedelta(hours=3), with_timestamp=False)
        r = self.prompt(tp)
        self.assertEqual((r.returncode, "Traceback" in r.stderr), (0, False), r.stderr[-300:])
        # hook JSON of the wrong type: `true` as a path is open(True), file descriptor 1
        tp = self.transcript(timedelta(hours=3))
        for name, over in {"bool path": {"transcript_path": True}, "list path": {"transcript_path": [tp]},
                           "NUL path": {"transcript_path": "a\0b"}, "sid": {"session_id": ["x"], "transcript_path": tp},
                           "path-like sid": {"session_id": "../../escape", "transcript_path": tp},
                           "prompt": {"prompt": 5, "transcript_path": tp},
                           "resume numbers": {"hook_event_name": "SessionStart", "source": "resume", "transcript_path": tp,
                                              "seconds_since_last_response": "long", "context_tokens": [1],
                                              "estimated_cache_write_usd": "x", "prompt_cache_likely_expired": "yes"}}.items():
            with self.subTest(name):
                r = self.guard({"hook_event_name": "UserPromptSubmit", "session_id": "s9", **over})
                self.assertEqual((r.returncode, "Traceback" in r.stderr), (0, False), r.stderr[-300:])
                if r.stdout.strip():
                    json.loads(r.stdout)                  # whatever it says is still valid hook JSON
        for stray in ("escape.json", "escape.prompt.txt"):      # a session id names files: it cannot climb out
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.data), stray)), stray)
        # whatever else goes wrong (here: the state directory is a file) still exits 0
        import shutil
        shutil.rmtree(os.path.join(self.data, "cache_guard"), ignore_errors=True)
        write(os.path.join(self.data, "cache_guard"), "in the way")
        r = self.prompt(self.transcript(timedelta(hours=3)), sid="s4")
        self.assertEqual((r.returncode, "Traceback" in r.stderr), (0, False), r.stderr[-300:])
        os.remove(os.path.join(self.data, "cache_guard"))
        r = self.guard({"hook_event_name": "SessionStart", "session_id": "s6", "transcript_path": True})
        self.assertIn("Routing", json.loads(r.stdout)["systemMessage"], "a bool path must not close the hook's stdout")
        # a transcript line nested past the recursion limit is skipped like any torn line
        r = self.prompt(self.transcript(timedelta(hours=3), extra=["[" * 100_000]), sid="s5")
        self.assertEqual(json.loads(r.stdout)["decision"], "block")
        # an unusable number in the environment is the default, not an OverflowError at import
        for var in ("LACUNA_MIN_CTX", "LACUNA_IDLE_MIN"):
            for bad in ("inf", "-inf", "nan", "-5", "1e999", "0", "abc"):
                with self.subTest(var=var, value=bad):
                    r = self.guard({"hook_event_name": "SessionStart", "source": "startup", "session_id": "t"}, **{var: bad})
                    self.assertEqual((r.returncode, "Traceback" in r.stderr), (0, False), r.stderr[-300:])
                    self.assertIn("60 min", json.loads(r.stdout)["systemMessage"])
        r = self.guard({"hook_event_name": "UserPromptSubmit", "session_id": "s7", "prompt": "x",
                        "transcript_path": self.transcript(timedelta(hours=3), ctx=50_000)}, LACUNA_MIN_CTX="inf")
        self.assertEqual(json.loads(r.stdout)["decision"], "block", "MIN_CTX=inf is the 40k default, not 'never hold'")
        for cfg in ({"idle_minutes": "sixty"}, {"idle_minutes": None}, {"idle_minutes": float("inf")},
                    {"idle_minutes": -1}, {"idle_minutes": 0}, {"idle_minutes": True}):
            with self.subTest(cfg):
                self.config(cfg)
                r = self.guard({"hook_event_name": "SessionStart", "source": "startup", "session_id": "t"})
                self.assertEqual((r.returncode, "Traceback" in r.stderr), (0, False), r.stderr[-300:])
                self.assertIn("60 min", json.loads(r.stdout)["systemMessage"], "an unusable value falls back to the default")
        r = self.guard({"hook_event_name": "SessionStart", "source": "startup"}, LACUNA_GUARD_OFF="1")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))
        r = self.run_py([os.path.join(ROOT, "hooks", "results_hook.py")], input='{"hook_event_name":"SessionStart"}',
                        CLAUDE_RESULTS_FILE="", TMUX="")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), "the results hook is silent outside the pane")


# --------------------------------------------------------------------------------------------
# doctor: the first thing a stranger runs
# --------------------------------------------------------------------------------------------

class TestDoctor(Sandbox):

    def doctor(self, **env):
        env.setdefault("PATH", SAFE_PATH)                      # no fake claude either: nothing installed
        env.setdefault("LACUNA_PYTHON", self.p("no-venv", "bin", "python"))
        return self.run_py([os.path.join(ROOT, "scripts", "doctor.py")], **env)

    def test_reports_what_is_missing_and_creates_the_data_directory(self):
        r = self.doctor()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertRegex(r.stdout, r"\[MISS\] claude CLI not on PATH")
        self.assertRegex(r.stdout, r"\[note\] pymupdf4llm not installed", "PDF parsers are optional, so a note, not a blocker")
        self.assertRegex(r.stdout, r"fix: \S*bin/lacuna doctor --install-pdf", "a new user has no `lacuna` on PATH yet")
        self.assertIn("1 BLOCKING", r.stdout)
        # NOT the count of optional pieces: it varies with whether rich and tmux are installed
        for rel in ("cache", "books", "config.json"):
            self.assertTrue(os.path.exists(os.path.join(self.data, rel)), rel)
        # a copy here would beat every later shipped calibration forever (lit_cost prefers the data dir)
        self.assertFalse(os.path.exists(os.path.join(self.data, "calibration.json")),
                         "doctor must not copy the shipped calibration into the data directory")
        self.assertFalse([d for d in os.listdir(self.data) if d.startswith("venv")], "no venv without --install-*")
        cfg = json.loads(read(os.path.join(self.data, "config.json")))
        self.assertEqual(cfg.get("models"), {}, "defaults are not baked in: a later release's better default must reach this user")
        self.assertNotIn("effort", cfg)

    def test_leaves_existing_user_files_alone(self):
        for body in ('{"models": {"planner": "mine"}}', "{ hand-edited and broken"):
            with self.subTest(body=body):
                cfg = write(os.path.join(self.data, "config.json"), body)
                cal = write(os.path.join(self.data, "calibration.json"), '{"_source": "mine"}')
                r = self.doctor()
                self.assertNotIn("Traceback", r.stderr)
                self.assertIn("left untouched", r.stdout)
                self.assertEqual((read(cfg), read(cal)), (body, '{"_source": "mine"}'))

    def test_reports_a_settings_file_it_could_not_use(self):
        """A malformed config.json, pricing.json or calibration.json is ignored by every other
        command, and doctor once printed [ok] beside each and exited 0."""
        fake = write(self.p("bin2", "claude"), "#!/bin/sh\nexit 0\n", 0o755)
        env = dict(PATH=f"{os.path.dirname(fake)}:{SAFE_PATH}")
        self.assertEqual(self.doctor(**env).returncode, 0, "precondition: nothing else is wrong")
        for name, body in (("config.json", '{"models": {"coder": "x",}}'), ("pricing.json", '{"models": {"a": [1,],}}'),
                           ("calibration.json", '{"haiku": {')):
            with self.subTest(name):
                path = write(os.path.join(self.data, name), body)
                r = self.doctor(**env)
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertRegex(r.stdout, r"\[MISS\][^\n]*\n\s+" + re.escape(path), "the file is named, as a problem")
                self.assertNotRegex(r.stdout, r"\[ok  \] " + re.escape(path), "an unusable file is not [ok]")
                self.assertEqual(read(path), body, "doctor must not rewrite it")
                os.remove(path)

    def test_a_setting_that_fell_back_is_a_note_and_a_stale_calibration_is_called_stale(self):
        """A mistyped value falls back to its default; doctor called it a blocking MISS ("Fix the MISS
        lines before anything will work") and /lacuna-doctor, whose injection fails on exit 1, then
        showed nothing. A copy of the shipped calibration tagged upstream is ignored by `lit estimate`,
        so doctor must not call it "yours"."""
        fake = write(self.p("bin2", "claude"), "#!/bin/sh\nexit 0\n", 0o755)
        env = dict(PATH=f"{os.path.dirname(fake)}:{SAFE_PATH}")
        for body in ('{"models": {"reader": 5}}', '{"effort": {"reader": true}}', '{"paths": []}'):
            with self.subTest(body):
                path = write(os.path.join(self.data, "config.json"), body)
                r = self.doctor(**env)
                self.assertEqual(r.returncode, 0, r.stdout)
                self.assertRegex(r.stdout, r"\[note\] a setting was not used as written")
                self.assertNotIn("MISS", r.stdout)
                self.assertNotIn("BLOCKING", r.stdout)
        os.remove(path)
        cal = write(os.path.join(self.data, "calibration.json"), '{"_source": "upstream"}')
        r = self.doctor(**env)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("stale copy", r.stdout)
        self.assertNotIn("yours", r.stdout.split(cal)[1].split("\n\n")[0])

    def test_the_dev_cycle_headless_recipe_passes_effort(self):
        """The recipe pinned the model and dropped the agents' effort, so a headless coder ran at the
        CLI default. Effort is a cost multiplier."""
        skill = read(os.path.join(ROOT, "skills", "dev-cycle", "SKILL.md"))
        cmd = next(l for l in skill.splitlines() if l.startswith("claude -p"))
        self.assertIn('--effort "$EFFORT"', cmd)
        self.assertIn("['effort']['coder']", skill, "and takes it from the configuration")

    def test_every_shell_injection_in_a_command_or_skill_is_guarded(self):
        """A failing !`cmd` aborts the whole slash-command invocation and Claude never sees the
        output (code.claude.com, slash-commands). doctor exits 1 exactly when it has something to
        report, and spend exits 2 on a bad argument, so each injection ends `|| true`."""
        import glob
        files = glob.glob(os.path.join(ROOT, "commands", "*.md")) + glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md"))
        found = []
        for f in files:
            for line in read(f).splitlines():
                if re.search(r"(?<!`)!`[^`]+`", line):
                    found.append((f, line))
                    self.assertRegex(line, r"\|\| true`", f"{os.path.relpath(f, ROOT)}: {line}")
        self.assertGreaterEqual(len(found), 2, "the check must see the injections it guards")

    def test_checks_the_directories_lacuna_resolves(self):
        """cache and books follow config.json and LACUNA_CACHE/LACUNA_BOOKS; doctor checked, and
        created, <data>/cache and <data>/books regardless."""
        cache, books = self.p("elsewhere", "cache"), self.p("elsewhere", "books")
        r = self.doctor(LACUNA_CACHE=cache, LACUNA_BOOKS=books)
        for d in (cache, books):
            self.assertTrue(os.path.isdir(d), d)
            self.assertRegex(r.stdout, r"\[ok  \] " + re.escape(d))
        self.assertEqual([x for x in ("cache", "books") if os.path.exists(os.path.join(self.data, x))], [])

    def test_a_data_directory_that_is_a_file_is_a_problem_not_a_traceback(self):
        blocker = write(self.p("afile"), "x")
        r = self.doctor(LACUNA_DATA=blocker)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"\[MISS\] " + re.escape(blocker))

    def test_finds_claude_and_probes_a_parser_by_the_symbols_actually_used(self):
        """`import marker` succeeds for any marker version (a namespace package), so the probe
        must import what prep_md imports; on Python 3.9 pip resolves a marker with no
        `marker.converters` and the bare import still passed."""
        fake = write(self.p("bin2", "claude"), "#!/bin/sh\nexit 0\n", 0o755)
        marker_py = write(os.path.join(self.data, "venv-marker", "bin", "python"),
                          '#!/bin/sh\ncase "$*" in *converters*) exit 1;; *) exit 0;; esac\n', 0o755)
        r = self.doctor(PATH=f"{os.path.dirname(fake)}:{SAFE_PATH}")
        self.assertRegex(r.stdout, r"\[ok  \] claude CLI " + re.escape(fake))
        self.assertRegex(r.stdout, r"\[note\] marker-pdf not installed", "a venv whose marker lacks the symbols used is not installed")
        self.assertIn("Nothing is blocking", r.stdout)
        self.assertEqual(r.returncode, 0, "optional pieces must not make doctor fail")
        self.assertTrue(os.path.exists(marker_py))


# --------------------------------------------------------------------------------------------
# config report, split-book, prep_md's CLI, the results pane
# --------------------------------------------------------------------------------------------

class TestConfigReport(Sandbox):

    def test_shows_provenance_paths_and_survives_a_hand_edited_value(self):
        self.config({"models": {"planner": "mine"}, "budget_warn_usd": "2", "paths": {"books": "/srv/books"}})
        r = self.lacuna("config", LACUNA_READER="from-env")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.data, r.stdout)
        self.assertRegex(r.stdout, r"planner=mine \[config.json\]")
        self.assertRegex(r.stdout, r"reader=from-env \[\$LACUNA_READER\]")
        self.assertRegex(r.stdout, r"books +/srv/books \[config.json\]")
        for name in ("cache", "ledger", "results", "parser py"):
            self.assertIn(name, r.stdout)
        self.assertIn("not a number", r.stdout, "a string budget is reported, not a traceback")
        self.assertEqual(json.loads(self.lacuna("config", "--json").stdout)["models"]["planner"], "mine")
        self.assertEqual(self.calls(), [])

    def test_absurdly_nested_json_files_are_reported_not_raised(self):
        """json.load raises RecursionError on deep nesting, which is not a ValueError: every command
        died on a hand-edited config.json, though this module must never raise on bad input."""
        deep = "[" * 100_000 + "]" * 100_000
        for name in ("config.json", "pricing.json", "calibration.json"):
            with self.subTest(name):
                write(os.path.join(self.data, name), deep)
                r = self.lacuna("config")
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                self.assertNotIn("Traceback", r.stderr)
                r = self.lacuna("lit", "estimate", write(self.p("a.md"), "x " * 500))
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                self.assertNotIn("Traceback", r.stderr)
                os.remove(os.path.join(self.data, name))
        write(os.path.join(self.data, "config.json"), deep)
        self.assertIn("not valid JSON", self.lacuna("config").stdout)

    def test_a_config_or_pricing_file_that_was_not_used_is_said_by_every_command_that_spends(self):
        """A trailing comma sent `synth` to the default planner, skipped the user's after.synth hook
        and priced at shipped rates, and only `lacuna config` ever mentioned it."""
        doc = self.doc_file()
        write(os.path.join(self.data, "config.json"), '{"models": {"planner": "sonnet"},}')
        runs = {"lit estimate": self.lacuna("lit", "estimate", doc),
                "spend": self.lacuna("spend"),
                "lit verify": self.lacuna("lit", "verify", "--target", self.p("nope.md"), "--repo", self.tmp)}
        for name, r in runs.items():
            with self.subTest(name):
                self.assertIn("config.json", r.stderr)
                self.assertIn("not valid JSON", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.calls(), [])
        os.remove(os.path.join(self.data, "config.json"))
        for body, needle in (("[1, 2", "pricing.json: not valid JSON"), ("[1]", "should be an object"),
                             ('{"models": {"opus-5-5": "12345", "sonnet-5-5": [1, -2, 3, 4, 5]}}', "opus-5-5")):
            with self.subTest(body):
                write(os.path.join(self.data, "pricing.json"), body)
                for r in (self.lacuna("lit", "estimate", doc), self.lacuna("spend")):
                    self.assertIn(needle, r.stderr)
        os.remove(os.path.join(self.data, "pricing.json"))
        for r in (self.lacuna("lit", "estimate", doc), self.lacuna("spend")):
            self.assertEqual(r.stderr, "", "nothing to say when the files are fine")
        # a hook and the status line are not the place to talk
        write(os.path.join(self.data, "config.json"), '{"models": {},}')
        r = self.run_py([os.path.join(ROOT, "hooks", "cache_guard.py")], input='{"hook_event_name": "SessionStart"}')
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def doc_file(self):
        return write(self.p("a.md"), "a" * 3000)

    def test_wrong_typed_settings_fall_back_to_the_default_and_say_so(self):
        """D10: a number for a model crashed estimate, read and synth; `"cache": true` made a
        directory named ./True; `config` showed the raw value rather than the one in effect."""
        doc = self.doc_file()
        for cfg in ({"models": {"planner": 5}}, {"models": {"planner": ["sonnet"]}}, {"models": {"reader": {"id": "x"}}},
                    {"effort": {"reader": 5}}, {"paths": {"cache": True}}, {"paths": {"python": ["a"]}},
                    {"after": {"read": 5}}, {"after": {"read": ["/bin/echo", "x"]}}, {"paths": {"cache": "a\u0000b"}}, {"models": {"reader": "  "}}):
            with self.subTest(cfg):
                self.config(cfg)
                for args in (("config",), ("config", "--json"), ("lit", "estimate", doc, "--json"), ("lit", "estimate", doc)):
                    r = self.lacuna(*args)
                    self.assertEqual(r.returncode, 0, f"{args}: {r.stderr[-300:]}")
                    self.assertNotIn("Traceback", r.stderr)
                out = json.loads(self.lacuna("config", "--json").stdout)
                self.assertEqual(out["models"], json.loads(json.dumps(out["models"])))
                for section in ("models", "effort", "paths"):
                    for k, v in out[section].items():
                        self.assertIsInstance(v, str, f"{section}.{k} shows the value in effect")
                        self.assertTrue(v.strip())
        self.config({"models": {"planner": 5}, "paths": {"cache": True}})
        r = self.lacuna("config")
        self.assertIn("planner should be text, not 5", r.stdout)
        self.assertIn("planner=claude-opus-5", r.stdout, "the default is shown, not 5")
        self.assertNotRegex(r.stdout, r"planner=5")
        r = self.lacuna("lit", "read", doc, "--out", self.p("out"))
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "True")), "a bool is not a path")

    def test_a_null_or_empty_setting_means_the_default_and_is_not_an_error(self):
        self.config({"effort": {"reader": None}, "models": {"planner": ""}, "paths": {"cache": None}})
        r = self.lacuna("config")
        self.assertNotIn("should be text", r.stdout)
        self.assertEqual(json.loads(self.lacuna("config", "--json").stdout)["errors"], [])

    def test_config_json_output_is_strict_json(self):
        for body in ('{"budget_warn_usd": NaN}', '{"idle_minutes": Infinity}', '{"after": {"read": [NaN]}}'):
            with self.subTest(body):
                write(os.path.join(self.data, "config.json"), body)
                r = self.lacuna("config", "--json")
                self.assertEqual(r.returncode, 0, r.stderr)
                json.loads(r.stdout, parse_constant=lambda c: self.fail(f"{c} is not JSON"))

    def test_a_relative_path_is_taken_from_the_data_directory_not_the_cwd(self):
        """A relative paths.cache followed the cwd, so the cache split and papers were read again."""
        write(os.path.join(self.data, "config.json"), '{"paths": {"cache": "mycache", "books": "/srv/books"}}')
        for cwd in (self.p("one"), self.p("two")):
            os.makedirs(cwd)
            got = json.loads(self.lacuna("config", "--json", cwd=cwd).stdout)["paths"]
            self.assertEqual(got["cache"], os.path.join(os.path.abspath(self.data), "mycache"))
            self.assertEqual(got["books"], "/srv/books", "an absolute path is left alone")

    def test_a_price_override_applies_however_the_model_is_named(self):
        """D15: `claude-opus-5-5` in pricing.json priced nothing for a role configured as `opus`,
        because the alias resolves to `opus-5-5`."""
        f = self.doc_file()
        base = json.loads(self.lacuna("config", "--json").stdout)["prices"]
        self.assertIn("opus-5-5", base)
        write(os.path.join(self.data, "pricing.json"), json.dumps({"models": {"claude-opus-5-5": [1, 2, 3, 4, 77]}}))
        self.config({"models": {"planner": "opus"}})
        prices = json.loads(self.lacuna("config", "--json").stdout)["prices"]
        self.assertEqual(prices["opus-5-5"], [1, 2, 3, 4, 77])
        self.assertNotIn("claude-opus-5-5", prices)
        syn = json.loads(self.lacuna("lit", "estimate", f, "--json").stdout)["synthesis"]
        self.assertAlmostEqual(syn["usd"], syn["context_tokens"] * 3 / 1e6 + 8_000 * 77 / 1e6, places=9)


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
def open(path):
    if _spec.get("corrupt"): raise RuntimeError("Failed to open file %r." % path)
    return _Doc()
'''


class TestSplitBook(Sandbox):
    """toc.md is the only thing a reader agent sees of a book. Wrong page ranges send it to the
    wrong chapter; an unsearchable chapter file is worse than none."""

    PAGES = ["alpha page", "beta page", "gamma page", "delta page", "epsilon page"]

    def split(self, toc, pages=None, title="My Book", outdir=None, corrupt=False):
        write(self.p("fake", "pymupdf.py"), FAKE_PYMUPDF)
        write(self.p("spec.json"), json.dumps({"pages": pages or self.PAGES, "toc": toc, "title": title, "corrupt": corrupt}))
        self.outdir = outdir or self.p("books", "mybook")
        return self.run_py([os.path.join(LIT, "split_book.py"), write(self.p("book.pdf"), "%PDF"), self.outdir],
                           PYTHONPATH=self.p("fake"), FAKE_PDF_SPEC=self.p("spec.json"))

    def index(self):
        return json.loads(read(os.path.join(self.outdir, "index.json")))

    def chapter(self, name):
        return read(os.path.join(self.outdir, name))

    def test_chapters_page_ranges_index_and_toc(self):
        r = self.split([[1, "Introduction", 1], [2, "A subsection", 2], [1, "Methods & Results", 3], [1, "Appendix", 5]])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(x["file"], x["pages"]) for x in self.index()],
                         [("00_Introduction.txt", [1, 2]), ("01_Methods_Results.txt", [3, 4]), ("02_Appendix.txt", [5, 5])])
        intro, methods, appendix = (self.chapter(x["file"]) for x in self.index())
        self.assertIn("beta page", intro)
        self.assertNotIn("gamma page", intro, "a chapter must stop the page before the next starts")
        self.assertIn("delta page", methods)
        self.assertIn("epsilon page", appendix)
        toc = self.chapter("toc.md")
        self.assertTrue(toc.startswith("# My Book\n"))
        self.assertIn("3 chapters, 5 pages", toc)
        self.assertIn("| Methods & Results | `01_Methods_Results.txt` | 3-4 |", toc)
        for x in self.index():
            self.assertIn(f"| {x['tok']:,} |", toc)
            self.assertEqual(x["tok"], round(x["chars"] / 2.9))
        self.split([[1, "Only", 1]], title="")
        self.assertTrue(self.chapter("toc.md").startswith("# mybook\n"), "the title falls back to the directory name")

    def test_chapters_are_greppable(self):
        """Control characters survive PDF extraction and make grep treat a file as binary, so a
        book that keeps them is silently unsearchable: a real reader grepped three times, no hits."""
        pages = ["alpha\x00page with a \x01NUL and \x1f other controls", *self.PAGES[1:]]
        r = self.split([[1, "Introduction", 1], [1, "Methods", 3]], pages=pages)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.chapter("00_Introduction.txt")
        self.assertEqual(re.findall(r"[\x00-\x08\x0b-\x1f\x7f]", body), [])
        self.assertIn("alphapage", body.replace(" ", ""), "the words themselves must survive")
        g = subprocess.run(["grep", "-c", "page", os.path.join(self.outdir, "00_Introduction.txt")], capture_output=True, text=True)
        self.assertEqual(g.returncode, 0, "grep found nothing in the chapter file")
        self.assertNotIn("Binary file", g.stdout + g.stderr)
        # The same, from a bookmark title: it is the chapter file's first line and a row of toc.md.
        r = self.split([[1, "Chapter 1\x00", 1], [1, "Chapter 2\rKernels | Priors", 3]], title="Bayes\x00Opt")
        self.assertEqual(r.returncode, 0, r.stderr)
        toc = self.chapter("toc.md")
        self.assertEqual(re.findall(r"[\x00-\x08\x0b-\x1f\x7f]", toc + self.chapter("00_Chapter_1.txt")), [])
        self.assertIn("| Chapter 2 Kernels \\| Priors | `01_Chapter_2_Kernels_Priors.txt` |", toc,
                      "a `|` in a title must not start a new column")
        g = subprocess.run(["grep", "-c", "alpha", os.path.join(self.outdir, "00_Chapter_1.txt")], capture_output=True, text=True)
        self.assertEqual((g.returncode, g.stdout.strip()), (0, "1"), g.stdout + g.stderr)

    def test_warns_of_a_chapter_with_no_readable_text(self):
        """A font with no Unicode map extracts as U+FFFD: a long chapter that no grep can match."""
        r = self.split([[1, "Readable", 1], [1, "Garbled", 3]],
                       pages=["alpha page " * 40, "beta page " * 40, "\ufffd" * 400, "\ue012" * 400, "epsilon page"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("'Garbled' (pp. 3-5): 800 unreadable glyphs", r.stderr)
        self.assertNotIn("'Readable'", r.stderr)

    def test_resplit_replaces_its_own_chapters_and_nothing_else(self):
        """Re-splitting into the same directory left the old chapters beside the new toc.md, and
        every grep over the book found them."""
        r = self.split([[1, f"Old chapter {i}", i] for i in range(1, 6)])
        self.assertEqual(r.returncode, 0, r.stderr)
        mine = [write(os.path.join(self.outdir, n), "my own notes") for n in ("notes.txt", "99_mine.txt")]
        r = self.split([[1, "New one", 1], [1, "New two", 3]])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(self.outdir)),
                         ["00_New_one.txt", "01_New_two.txt", "99_mine.txt", "index.json", "notes.txt", "toc.md"],
                         "the old split's chapters go; files it did not write stay")
        self.assertEqual([read(m) for m in mine], ["my own notes"] * 2)

    def test_resplit_never_deletes_a_chapter_it_just_wrote(self):
        """On a case-insensitive disk a re-split with a case-only title change wrote
        00_INTRODUCTION.txt over 00_Introduction.txt, then deleted the "stale" name: every new
        chapter gone, exit 0. A symlink makes two names one file on any disk."""
        r = self.split([[1, "Introduction", 1], [1, "Methods", 3]])
        self.assertEqual(r.returncode, 0, r.stderr)
        real = os.path.join(self.outdir, "00_Introduction.txt")
        os.symlink(real, os.path.join(self.outdir, "00_Intro.txt"))
        r = self.split([[1, "Intro", 1], [1, "Methods", 3]], outdir=self.outdir)
        self.assertEqual(r.returncode, 0, r.stderr)
        for row in self.index():
            self.assertTrue(os.path.exists(os.path.join(self.outdir, row["file"])), row["file"])
        self.assertIn("alpha page", self.chapter("00_Intro.txt"))

    def test_says_whether_readers_are_offered_the_book(self):
        """Readers are offered <books>/<name>/toc.md and nothing else; a book split anywhere else
        exited 0 and was silently never offered."""
        books = os.path.join(self.data, "books")
        r = self.split([[1, "Only", 1]], outdir=os.path.join(books, "gp"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"offered it as {os.path.join(books, 'gp', 'toc.md')}", r.stdout)
        self.assertNotIn("NOT offered", r.stderr)
        r = self.split([[1, "Only", 1]], outdir=self.p("mybooks", "gp"))
        self.assertEqual(r.returncode, 0, "the chapters are fine; only the offer is missing")
        self.assertIn("NOT offered", r.stderr)
        self.assertIn(os.path.join(books, "gp"), r.stderr, "and it says where to put the book instead")

    def test_picks_a_useful_toc_level_and_tolerates_unsorted_or_shared_pages(self):
        """A book organised in Parts has a handful of level-1 entries over hundreds of pages;
        splitting there gives 240-page "chapters". Bookmarks are not guaranteed sorted, and two
        can claim one page: before, that gave `pp. 3-2` and `pp. 18-0` with the text misfiled."""
        toc = [[1, "Part I", 1], [1, "Part II", 21]] + [[2, f"Chapter {i}", i * 4 - 3] for i in range(1, 11)]
        r = self.split(sorted(toc, key=lambda e: e[2]), pages=[f"page {i} text" for i in range(1, 41)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.index()), 10, "split on the chapters, not the Parts")
        self.assertIn("Chapter 1", self.chapter("toc.md"))
        self.assertIn("TOC level 2", r.stderr)
        front = [[1, t, i + 1] for i, t in enumerate(("Cover", "Title", "Copyright", "Contents", "Preface"))]
        toc = front + [[1, "Part I", 10], [1, "Part II", 150]] + [[2, f"Chapter {i}", 10 + (i - 1) * 23] for i in range(1, 13)]
        r = self.split(sorted(toc, key=lambda e: e[2]), pages=[f"page {i} text" for i in range(1, 295)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(len(self.index()), 12, "front-matter bookmarks at level 1 must not win on count")
        self.assertIn("Chapter 7", self.chapter("toc.md"))
        self.assertEqual(self.split([[2, "Only subsections", 1], [2, "Another", 3]]).returncode, 0, "a level-2-only TOC is usable")
        r = self.split([[1, "Third", 4], [1, "First", 1], [1, "Also first", 1]])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(x["title"], x["pages"]) for x in self.index()], [("First / Also first", [1, 3]), ("Third", [4, 5])],
                         "sorted by page, and two entries on one page are merged rather than one left empty")
        self.assertTrue(all(x["chars"] > 0 for x in self.index()))

    def test_refuses_a_book_with_no_toc(self):
        r = self.split([])
        self.assertEqual(r.returncode, 2)
        self.assertIn("no table of contents", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.outdir, "toc.md")), "a book that cannot be split must not be offered to readers")
        r = self.split([[1, "Only", 1]], corrupt=True)
        self.assertEqual(r.returncode, 3, "a PDF pymupdf cannot open is a parse failure, not a crash")
        self.assertIn("could not read book.pdf (RuntimeError: Failed to open file", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(self.outdir), "nothing was written")


class TestPrepMdCLI(Sandbox):

    def test_parser_flag_in_both_forms_and_the_failure_exit(self):
        """The argv parsing is hand-rolled; `--parser NAME` must not become a path."""
        pdf = write(self.p("x.pdf"), "%PDF")
        for argv in (["--parser", "docling"], ["--parser=docling"]):
            with self.subTest(argv):
                out = self.p("out.md")
                r = self.run_py([os.path.join(LIT, "prep_md.py"), pdf, out, *argv], LACUNA_PYTHON=self.p("no-venv", "python"))
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("docling: venv missing", r.stderr)
                self.assertNotIn("pymupdf4llm:", r.stderr, "the forced parser was ignored")
                self.assertFalse(os.path.exists(out))


class TestResultsPane(Sandbox):
    """The only component that wants a third-party package, and it must not require it."""

    PANE = os.path.join(ROOT, "scripts", "panes", "results_pane.py")

    def test_renders_without_rich(self):
        block = write(self.p("block", "sitecustomize.py"),
                      "import sys\nfrom importlib.abc import MetaPathFinder\n"
                      "class Block(MetaPathFinder):\n"
                      "    def find_spec(self, name, path=None, target=None):\n"
                      "        if name == 'rich' or name.startswith('rich.'): raise ImportError('blocked for test')\n"
                      "sys.meta_path.insert(0, Block())\n")
        doc = write(self.p("r.md"), "# Title\n\nSome **bold** text and a `code` span.\n")
        r = self.run_py([self.PANE, doc, "--once"], PYTHONPATH=os.path.dirname(block))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Title", r.stdout)
        self.assertIn("bold", r.stdout)

    def test_plain_text_frame_fits_the_terminal(self):
        """Without rich a long paragraph is one line the terminal wraps, so a 10-row frame needed
        26 rows and scrolled the header away."""
        block = self.blocked_rich()
        probe = write(self.p("draw.py"),
                      "import io, os, re, sys\nsys.path.insert(0, os.path.dirname(sys.argv[1]))\n"
                      "import results_pane as RP\nassert not RP.HAVE_RICH\n"
                      "RP.shutil.get_terminal_size = lambda *a, **k: os.terminal_size((40, 10))\n"
                      "p = RP.Pane(sys.argv[2]); p.render(); out, real = io.StringIO(), sys.stdout\n"
                      "sys.stdout = out; p.draw(); sys.stdout = real\n"
                      "lines = re.sub(r'\\x1b\\[[0-9;?]*[A-Za-z]', '', out.getvalue()).split('\\n')\n"
                      "print(len(lines), sum(max(1, -(-len(l) // 40)) for l in lines))\n")
        doc = write(self.p("long.md"), "# Results\n\n" + "\n\n".join("A long paragraph is one line. " * 8 for _ in range(8)) + "\n")
        r = self.run_py([probe, self.PANE, doc], PYTHONPATH=os.path.dirname(block))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["10", "10"], "frame lines, physical rows: both must equal the terminal height")

    def test_a_dangling_symlink_in_the_results_directory_is_skipped(self):
        d = self.p("pane-dir")
        write(os.path.join(d, "new.md"), "# Still here\n")
        os.symlink(self.p("gone.md"), os.path.join(d, "old.md"))
        r = self.run_py([self.PANE, d, "--once"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Still here", r.stdout)

    def blocked_rich(self):
        return write(self.p("block", "sitecustomize.py"),
                     "import sys\nfrom importlib.abc import MetaPathFinder\n"
                     "class Block(MetaPathFinder):\n"
                     "    def find_spec(self, name, path=None, target=None):\n"
                     "        if name == 'rich' or name.startswith('rich.'): raise ImportError('blocked for test')\n"
                     "sys.meta_path.insert(0, Block())\n")

    def test_flags_and_typos_are_not_targets(self):
        """`--once` and `--help` were once taken as the target and created directories of that
        name; a mistyped file name became a directory too."""
        results = self.p("results")
        write(os.path.join(results, "r.md"), "# Hello from results\n")
        for flag in ("--once", "--help"):
            with self.subTest(flag):
                cwd = self.p("cwd" + flag)
                os.makedirs(cwd)
                r = self.run_py([self.PANE, flag], cwd=cwd, LACUNA_RESULTS=results)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(os.listdir(cwd), [], f"{flag} was treated as a path")
                if flag == "--once":
                    self.assertIn("Hello from results", r.stdout, "--once alone follows the default dir")
        r = self.run_py([self.PANE, self.p("results.txt"), "--once"])
        self.assertEqual(r.returncode, 2, "a missing path that is not .md is a typo, not a directory to create")
        self.assertFalse(os.path.exists(self.p("results.txt")))
        # Copied on its own, without lacuna_config beside it, "~" in LACUNA_RESULTS must still expand.
        alone, cwd = write(self.p("alone", "results_pane.py"), read(self.PANE)), self.p("cwd-alone")
        os.makedirs(cwd)
        r = self.run_py([alone, "--once"], cwd=cwd, LACUNA_RESULTS="~/pane-results")
        self.assertEqual((r.returncode, os.listdir(cwd)), (0, []), r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "pane-results")))


# --------------------------------------------------------------------------------------------
# the plugin's own path: a quote or a space in it, and the pane protocol that prints commands
# --------------------------------------------------------------------------------------------

class TestPluginPath(Sandbox):
    """The plugin is installed wherever the user's HOME is. `$ROOT` was interpolated into a
    `python -c` string (a quote was a SyntaxError) and into the tmux pane command unquoted (a space
    left `lacuna split` exiting 0 with a dead pane)."""

    def plugin(self, name="O'Brien plugins"):
        root = os.path.join(self.tmp, name, "lacuna")
        for d in ("bin", "scripts", "hooks"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(root, d), ignore=shutil.ignore_patterns("__pycache__"))
        return root

    def fake_tmux(self):
        """Runs the pane command the way tmux does: one string, through a shell, no terminal."""
        log = self.p("tmux.log")
        write(os.path.join(self.bin, "tmux"),
              '#!/bin/bash\necho "tmux $*" >> "$TMUX_LOG"\n'
              'if [ "$1" = split-window ]; then sh -c "${@: -1}" </dev/null >> "$TMUX_LOG" 2>&1; '
              'echo "pane exit=$?" >> "$TMUX_LOG"; fi\n', 0o755)
        write(os.path.join(self.bin, "claude"), "#!/bin/sh\nexit 0\n", 0o755)
        return log

    def test_split_book_survives_quotes_and_spaces_in_the_plugin_path(self):
        py = write(self.p("bookpy"), '#!/bin/sh\necho "$@" > "$BOOK_LOG"\n', 0o755)
        for name in ("O'Brien plugins", 'say "hi" \\ $HOME'):
            with self.subTest(name):
                root, log = self.plugin(name), self.p("book.log")
                r = subprocess.run([os.path.join(root, "bin", "lacuna"), "lit", "split-book", "b.pdf", "out"],
                                   capture_output=True, text=True, cwd=self.tmp,
                                   env=self.env(LACUNA_PYTHON=py, BOOK_LOG=log))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(os.path.join(root, "scripts", "lit", "split_book.py"), read(log))

    def test_split_hands_tmux_a_pane_command_that_runs(self):
        log = self.fake_tmux()
        for name in ("O'Brien plugins", "My Plugins"):
            with self.subTest(name):
                root = self.plugin(name)
                os.makedirs(self.p("proj dir"), exist_ok=True)
                r = subprocess.run([os.path.join(root, "bin", "lacuna"), "split"], capture_output=True, text=True,
                                   cwd=self.p("proj dir"), env=self.env(TMUX="/x,1,0", TMUX_LOG=log))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("pane exit=0", read(log), "the pane command died: " + read(log)[-400:])
                os.remove(log)

    def test_the_pane_commands_the_protocol_prints_run_with_spaces_in_both_paths(self):
        """The model prints the `Results pane:` and tmux lines from the protocol, with the real
        paths filled in. Unquoted, a space gave `expected one FILE or DIR` (exit 2)."""
        root, log = self.plugin(), self.fake_tmux()
        results = write(self.p("res dir", "My Project-1.md"), "# Findings here\n")
        hook = self.run_py([os.path.join(root, "hooks", "results_hook.py")], input="{}", CLAUDE_RESULTS_FILE=results, TMUX="x")
        sources = {"hook": json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]}
        for rel in (("skills", "results-pane", "SKILL.md"), ("output-styles", "results-pane.md")):
            sources[rel[-1]] = read(os.path.join(ROOT, *rel)).replace("${CLAUDE_PLUGIN_ROOT}", root)
        for name, text in sources.items():
            line = re.search(r"(?m)^\s*Results pane:\s+(.+)$", text).group(1).replace("<full path>", results)
            tmux = re.search(r"(?m)^\s*tmux:\s+(tmux .+?)\s{2,}\(", text).group(1).replace("<full path>", results)
            for what, cmd in (("open", line), ("tmux", tmux)):
                with self.subTest(name, command=what):
                    if os.path.exists(log):
                        os.remove(log)             # each command must show the document itself
                    r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, cwd=self.tmp,
                                       env=self.env(TMUX_LOG=log), stdin=subprocess.DEVNULL)
                    self.assertEqual(r.returncode, 0, f"{cmd}\n{r.stderr}")
                    self.assertIn("Findings here", r.stdout + (read(log) if os.path.exists(log) else ""))

    def test_results_hook_never_raises_and_says_the_real_path(self):
        root = self.plugin()
        hook, results = os.path.join(root, "hooks", "results_hook.py"), self.p("r.md")
        proto = os.path.join(root, "scripts", "panes", "protocol.md")
        shown = self.run_py([hook], input="{}", CLAUDE_RESULTS_FILE=results, TMUX="x")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        ctx = json.loads(shown.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(shown.stdout.isascii(), "the output must not depend on the locale")
        self.assertIn(results, ctx)
        self.assertIn(os.path.join(root, "bin", "lacuna"), ctx, "${CLAUDE_PLUGIN_ROOT} is literal text to the model")
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", ctx)
        # The protocol holds non-ASCII characters; the locale's codec must not decide whether it reads.
        ascii_only = dict(LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
        enc = self.run_py(["-c", "import locale; print(locale.getpreferredencoding(False))"], **ascii_only).stdout.strip()
        if enc.upper().replace("_", "-") not in ("ASCII", "US-ASCII", "ANSI-X3.4-1968"):
            self.skipTest(f"cannot force an ASCII locale here (got {enc})")
        r = self.run_py([hook], input="{}", CLAUDE_RESULTS_FILE=results, TMUX="x", **ascii_only)
        self.assertEqual((r.returncode, "Traceback" in r.stderr), (0, False), r.stderr[-300:])
        self.assertIn(results, r.stdout, "it must still say something, not just survive")
        with open(proto, "wb") as f:                      # an unreadable protocol: still exit 0, quietly
            f.write(b"\xff\xfe not utf-8")
        r = self.run_py([hook], input="{}", CLAUDE_RESULTS_FILE=results, TMUX="x")
        self.assertEqual((r.returncode, r.stdout, "Traceback" in r.stderr), (0, "", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
