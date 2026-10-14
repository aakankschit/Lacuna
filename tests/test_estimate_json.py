"""Tests for `lacuna lit estimate --json` against CONTRACT.md (pinned 2026-10-13).

No third-party dependencies, no network, no model calls:
    python3 -m unittest tests.test_estimate_json -v

Everything goes through the CLI (`python3 scripts/lit/lit_cost.py ...`) with a per-test temp
LACUNA_DATA and HOME, exactly like tests/test_coverage.py. No internal function name of
lit_cost.py is imported, so these tests cannot inherit the implementation's shape.

Expected numbers come from the reference's own inputs, not from magic constants:
  * token counts use the pinned fact that a file of 2900 ASCII characters is exactly 1000.0
    tokens (and 2100 math characters is exactly 1000.0 tokens), so test files are built to
    have exact token counts;
  * usd / minutes come from the usd_per_ktok and minutes_per_paper values in whichever
    calibration file the test wrote (or the shipped one, read off disk);
  * synthesis prices come from `scripts/lacuna_config.py --json`, i.e. lacuna_config.prices();
  * prices_retrieved comes from the pricing.json the run would actually read.

Most tests here fail until --json exists. They assert the contract, never the current behaviour.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIT_COST = os.path.join(ROOT, "scripts", "lit", "lit_cost.py")
CONFIG_PY = os.path.join(ROOT, "scripts", "lacuna_config.py")
SHIPPED_CAL = os.path.join(ROOT, "scripts", "lit", "calibration.json")
SHIPPED_PRICING = os.path.join(ROOT, "scripts", "pricing.json")

PY = sys.executable
SAFE_PATH = "/usr/bin:/bin"          # nothing user-installed: no real `claude`, no pdftotext
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
TOP_KEYS = {"schema", "documents", "total_tokens", "per_agent", "readers",
            "largest_reader_tokens", "long_context_surcharge", "calibration",
            "budget_warn_usd", "estimates", "any_over_budget", "cheapest_over_budget",
            "synthesis", "prices_retrieved", "warnings"}
EST_KEYS = {"model", "effort", "usd", "minutes", "over_budget"}
STARTUP = 3_700
HAIKU_TIER = 100_000


def base_env(home, data, **extra):
    env = {"PATH": SAFE_PATH, "HOME": home, "LACUNA_DATA": data, "PYTHONIOENCODING": "utf-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


class EstimateCase(unittest.TestCase):
    """A temp cwd, fake HOME and fake LACUNA_DATA per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lacuna-estjson-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.home)
        os.makedirs(self.data)

    # ---- helpers -----------------------------------------------------------------------
    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def run_est(self, *args, env_extra=None):
        return subprocess.run([PY, LIT_COST, *args], capture_output=True, text=True,
                              encoding="utf-8", cwd=self.tmp, stdin=subprocess.DEVNULL,
                              env=base_env(self.home, self.data, **(env_extra or {})))

    def est_json(self, *args, env_extra=None):
        """Run with --json, assert R1 purity, return (doc, result)."""
        r = self.run_est(*args, env_extra=env_extra)
        self.assertEqual(r.returncode, 0, f"exit {r.returncode}; stderr={r.stderr[-600:]}")
        self.assertNotIn("Traceback", r.stderr)
        doc, end = json.JSONDecoder().raw_decode(r.stdout.lstrip())
        self.assertIsInstance(doc, dict)
        self.assertEqual(r.stdout.lstrip()[end:].strip(), "",
                         "stdout must hold exactly one JSON object and nothing else")
        return doc, r

    def doc_file(self, name, ktokens=1, math=False):
        """A .md file whose token count is exactly 1000*ktokens (pinned tokens() facts)."""
        if math:
            return write(self.p(name), "α" * int(2100 * ktokens))
        return write(self.p(name), "a" * int(2900 * ktokens))

    def write_cal(self, obj):
        return write(os.path.join(self.data, "calibration.json"), json.dumps(obj))

    def write_config(self, obj):
        return write(os.path.join(self.data, "config.json"), json.dumps(obj))

    def shipped_cal(self):
        with open(SHIPPED_CAL) as f:
            return json.load(f)

    def prices(self):
        """lacuna_config.prices() as the run itself would see it: longest key first."""
        r = subprocess.run([PY, CONFIG_PY, "--json"], capture_output=True, text=True,
                           env=base_env(self.home, self.data), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        return json.loads(r.stdout)["prices"]

    def synthesis_usd(self, planner, context_tokens):
        """The reference's synthesis cost, or None when the lookup finds no output price."""
        for k, v in self.prices().items():
            if k in planner:
                return None if not v[4] else context_tokens * v[2] / 1e6 + 8000 * v[4] / 1e6
        return None

    def assert_close(self, got, want, what=""):
        self.assertAlmostEqual(got, want, delta=abs(want) * 1e-9 + 1e-12, msg=f"{what}: {got!r} != {want!r}")

    def cells(self, doc):
        return [(e["model"], e["effort"]) for e in doc["estimates"]]

    def cell(self, doc, model, effort):
        for e in doc["estimates"]:
            if e["model"] == model and e["effort"] == effort:
                return e
        self.fail(f"no estimate for {model}/{effort}; got {self.cells(doc)}")


class JsonCase(EstimateCase):
    """Appends --json to every invocation, so no test can accidentally exercise human mode."""

    def est_json(self, *args, env_extra=None):
        return super().est_json(*(args if "--json" in args else (*args, "--json")),
                                env_extra=env_extra)


# ========================================================================================
# R1/R2 — pure stdout, exact key set, exact types
# ========================================================================================
class TestShapeAndPurity(JsonCase):

    def test_stdout_is_exactly_one_json_object(self):
        """R1: no header line, no table, no Calibration footer on stdout."""
        f = self.doc_file("a.md")
        doc, r = self.est_json(f)
        self.assertNotIn("Calibration:", r.stdout)
        self.assertNotIn("documents,", r.stdout)
        self.assertNotIn("Recalibrate", r.stdout)
        self.assertEqual(doc["schema"], 1)

    def test_top_level_key_set_is_exact(self):
        """R2: no extra keys (e.g. a leaked 'per_agent_requested'), none missing."""
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual(set(doc), TOP_KEYS)

    def test_estimate_element_key_set_is_exact(self):
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertTrue(doc["estimates"])
        for e in doc["estimates"]:
            self.assertEqual(set(e), EST_KEYS)

    def test_types_are_json_native_not_coerced(self):
        """R2: bools are booleans not 0/1; counts are ints not 1000.0."""
        doc, _ = self.est_json(self.doc_file("a.md"))
        for k in ("schema", "documents", "total_tokens", "per_agent", "readers",
                  "largest_reader_tokens"):
            self.assertIs(type(doc[k]), int, f"{k} must be a JSON integer, got {doc[k]!r}")
        for k in ("long_context_surcharge", "any_over_budget"):
            self.assertIs(type(doc[k]), bool, f"{k} must be a JSON boolean, got {doc[k]!r}")
        self.assertIsInstance(doc["calibration"], dict)
        self.assertIsInstance(doc["estimates"], list)
        self.assertIsInstance(doc["warnings"], list)
        for w in doc["warnings"]:
            self.assertIsInstance(w, str)
        self.assertIsInstance(doc["prices_retrieved"], str)
        for e in doc["estimates"]:
            self.assertIsInstance(e["model"], str)
            self.assertIn(e["effort"], EFFORTS)
            self.assertIsInstance(e["usd"], float)
            self.assertIsInstance(e["minutes"], float)
            self.assertIs(type(e["over_budget"]), bool)

    def test_json_flag_is_position_independent(self):
        """R-surface: --json may appear anywhere on the command line."""
        f = self.doc_file("a.md")
        first, _ = self.est_json("--json", f, "--model", "haiku")
        middle, _ = self.est_json(f, "--json", "--model", "haiku")
        last, _ = self.est_json(f, "--model", "haiku", "--json")
        self.assertEqual(first, middle)
        self.assertEqual(middle, last)

# ========================================================================================
# R3/R6 — the numbers, and grouping
# ========================================================================================
class TestNumbersAndGrouping(JsonCase):

    def test_totals_and_default_grouping(self):
        d = self.p("papers")
        write(os.path.join(d, "a.md"), "a" * 2900)
        write(os.path.join(d, "b.txt"), "b" * 5800)
        write(os.path.join(d, "skip.png"), "x" * 99999)
        doc, _ = self.est_json(d)
        self.assertEqual(doc["documents"], 2)
        self.assertEqual(doc["total_tokens"], 3000)
        self.assertEqual(doc["per_agent"], 1)
        self.assertEqual(doc["readers"], 2)
        self.assertEqual(doc["largest_reader_tokens"], 2000 + STARTUP)
        self.assertFalse(doc["long_context_surcharge"])

    def test_math_heavy_ratio_is_the_reference_ratio(self):
        """tokens() uses 2.1 for math-heavy text: an implementation that hard-codes 2.9 fails."""
        doc, _ = self.est_json(self.doc_file("m.md", math=True))
        self.assertEqual(doc["total_tokens"], 1000)

    def test_grouping_is_consecutive_in_discovery_order(self):
        """R6: groups are consecutive slices of the discovery list, not sorted or re-balanced.

        Discovery order is dir children (sorted) then file args in command-line order:
        a=1k, b=2k, z=1k, y=5k -> per-agent 2 -> groups (3k), (6k): largest is the *last* group.
        """
        d = self.p("papers")
        write(os.path.join(d, "b.md"), "b" * 5800)
        write(os.path.join(d, "a.md"), "a" * 2900)
        z = self.doc_file("z.md", 1)
        y = self.doc_file("y.md", 5)
        doc, _ = self.est_json(d, z, y, "--per-agent", "2", "--model", "haiku", "--effort", "low")
        self.assertEqual(doc["documents"], 4)
        self.assertEqual(doc["total_tokens"], 9000)
        self.assertEqual(doc["per_agent"], 2)
        self.assertEqual(doc["readers"], 2)
        self.assertEqual(doc["largest_reader_tokens"], 6000 + STARTUP)
        mpp = self.shipped_cal()["haiku"]["low"]["minutes_per_paper"]
        self.assert_close(self.cell(doc, "haiku", "low")["minutes"], mpp * 2, "minutes")

    def test_short_last_group_counts_as_a_reader_and_minutes_use_the_largest_group(self):
        """R6: 5 documents at 3 per reader -> 2 readers; largest group holds 3 documents."""
        files = [self.doc_file(f"f{i}.md", 1) for i in range(5)]
        doc, _ = self.est_json(*files, "--per-agent", "3", "--model", "sonnet", "--effort", "high")
        self.assertEqual(doc["readers"], 2)
        self.assertEqual(doc["per_agent"], 3)
        self.assertEqual(doc["largest_reader_tokens"], 3000 + STARTUP)
        mpp = self.shipped_cal()["sonnet"]["high"]["minutes_per_paper"]
        self.assert_close(self.cell(doc, "sonnet", "high")["minutes"], mpp * 3, "minutes")

    def test_per_agent_zero_and_negative_report_one(self):
        """R6: the effective per_agent is max(1, --per-agent) and is what is reported."""
        files = [self.doc_file(f"f{i}.md", 1) for i in range(3)]
        for val in ("0", "-7"):
            with self.subTest(per_agent=val):
                doc, _ = self.est_json(*files, "--per-agent", val)
                self.assertEqual(doc["per_agent"], 1)
                self.assertEqual(doc["readers"], 3)
                self.assertEqual(doc["largest_reader_tokens"], 1000 + STARTUP)

    def test_per_agent_above_document_count_is_one_reader(self):
        files = [self.doc_file(f"f{i}.md", 1) for i in range(3)]
        doc, _ = self.est_json(*files, "--per-agent", "99", "--model", "haiku", "--effort", "max")
        self.assertEqual(doc["per_agent"], 99)
        self.assertEqual(doc["readers"], 1)
        self.assertEqual(doc["largest_reader_tokens"], 3000 + STARTUP)
        mpp = self.shipped_cal()["haiku"]["max"]["minutes_per_paper"]
        self.assert_close(self.cell(doc, "haiku", "max")["minutes"], mpp * 3, "minutes")

    def test_per_agent_changes_the_reported_numbers(self):
        """Silent no-op guard: --per-agent must be observable in the JSON."""
        files = [self.doc_file(f"f{i}.md", 1) for i in range(4)]
        a, _ = self.est_json(*files, "--per-agent", "1")
        b, _ = self.est_json(*files, "--per-agent", "4")
        self.assertNotEqual((a["readers"], a["largest_reader_tokens"], a["per_agent"]),
                            (b["readers"], b["largest_reader_tokens"], b["per_agent"]))

    def test_usd_and_minutes_are_not_rounded(self):
        """R3: usd/minutes are unrounded floats; $0.00054 must not arrive as 0.0."""
        self.write_cal({"m": {"low": {"usd_per_ktok": 0.00054, "minutes_per_paper": 0.4}}})
        files = [self.doc_file(f"f{i}.md", 1) for i in range(3)]
        doc, _ = self.est_json(*files, "--per-agent", "3")
        e = self.cell(doc, "m", "low")
        self.assert_close(e["usd"], 3 * 0.00054, "usd")
        self.assert_close(e["minutes"], 0.4 * 3, "minutes")
        self.assertNotEqual(e["usd"], round(e["usd"], 2))
        self.assertNotEqual(e["minutes"], round(e["minutes"]))

    def test_every_shipped_cell_matches_the_calibration_file(self):
        """R3: all 15 shipped cells, no surcharge: usd = total/1000 * usd_per_ktok."""
        files = [self.doc_file(f"f{i}.md", 2) for i in range(3)]   # 6000 tokens total
        doc, _ = self.est_json(*files, "--per-agent", "2")
        cal = self.shipped_cal()
        self.assertEqual(len(doc["estimates"]), 15)
        for e in doc["estimates"]:
            c = cal[e["model"]][e["effort"]]
            self.assert_close(e["usd"], 6 * c["usd_per_ktok"], f"{e['model']}/{e['effort']} usd")
            self.assert_close(e["minutes"], c["minutes_per_paper"] * 2,
                              f"{e['model']}/{e['effort']} minutes")


# ========================================================================================
# R4 — order and coverage
# ========================================================================================
class TestOrderAndCoverage(JsonCase):

    def test_model_order_follows_the_loaded_file_and_skips_metadata_keys(self):
        """R4: file order (not sorted, not dict-of-shipped), and non-object keys are skipped."""
        self.write_cal({"_source": "mine", "_measured": "2026-01-01", "paper_tokens": 33461,
                        "zeta": {"low": {"usd_per_ktok": 0.001, "minutes_per_paper": 1.0}},
                        "alpha": {"low": {"usd_per_ktok": 0.002, "minutes_per_paper": 2.0}},
                        "haiku": {"low": {"usd_per_ktok": 0.003, "minutes_per_paper": 3.0}}})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual([e["model"] for e in doc["estimates"]], ["zeta", "alpha", "haiku"])

    def test_effort_order_is_fixed_regardless_of_file_order(self):
        """R4: low, medium, high, xhigh, max even when the file lists them backwards."""
        mk = lambda u: {"usd_per_ktok": u, "minutes_per_paper": 1.0}
        self.write_cal({"m": {"max": mk(0.05), "xhigh": mk(0.04), "high": mk(0.03),
                              "medium": mk(0.02), "low": mk(0.01)}})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual([e["effort"] for e in doc["estimates"]], EFFORTS)

    def test_missing_and_empty_cells_are_omitted_not_nulled(self):
        """R4: a missing or {} calibration entry produces no element at all."""
        self.write_cal({"m": {"low": {"usd_per_ktok": 0.01, "minutes_per_paper": 1.0},
                              "medium": {},
                              "max": {"usd_per_ktok": 0.02, "minutes_per_paper": 2.0}}})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual(self.cells(doc), [("m", "low"), ("m", "max")])
        for e in doc["estimates"]:
            self.assertIsNotNone(e["usd"])
            self.assertIsNotNone(e["minutes"])

    def test_model_with_no_cells_at_all_contributes_nothing(self):
        self.write_cal({"empty": {}, "m": {"low": {"usd_per_ktok": 0.01, "minutes_per_paper": 1.0}}})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual(self.cells(doc), [("m", "low")])

    def test_shipped_order_is_haiku_sonnet_opus_times_five_efforts(self):
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual(self.cells(doc),
                         [(m, e) for m in ("haiku", "sonnet", "claude-opus-5") for e in EFFORTS])


# ========================================================================================
# R5 — filters
# ========================================================================================
class TestFilters(JsonCase):

    def setUp(self):
        super().setUp()
        self.files = [self.doc_file(f"f{i}.md", 1) for i in range(4)]

    def test_model_filter_restricts_only_estimates(self):
        full, _ = self.est_json(*self.files, "--per-agent", "2")
        one, _ = self.est_json(*self.files, "--per-agent", "2", "--model", "sonnet")
        self.assertEqual({e["model"] for e in one["estimates"]}, {"sonnet"})
        self.assertEqual([e["effort"] for e in one["estimates"]], EFFORTS)
        for k in ("documents", "total_tokens", "readers", "synthesis", "per_agent",
                  "largest_reader_tokens", "calibration", "prices_retrieved"):
            self.assertEqual(one[k], full[k], f"--model must not change {k}")

    def test_effort_filter_restricts_only_estimates(self):
        full, _ = self.est_json(*self.files, "--per-agent", "2")
        one, _ = self.est_json(*self.files, "--per-agent", "2", "--effort", "xhigh")
        self.assertEqual({e["effort"] for e in one["estimates"]}, {"xhigh"})
        self.assertEqual([e["model"] for e in one["estimates"]],
                         ["haiku", "sonnet", "claude-opus-5"])
        for k in ("documents", "total_tokens", "readers", "synthesis"):
            self.assertEqual(one[k], full[k], f"--effort must not change {k}")

    def test_effort_filter_is_not_a_no_op(self):
        """Silent no-op guard: --effort must change the estimate set in JSON mode."""
        a, _ = self.est_json(*self.files, "--effort", "low")
        b, _ = self.est_json(*self.files, "--effort", "max")
        self.assertEqual(len(a["estimates"]), 3)
        self.assertNotEqual(self.cells(a), self.cells(b))
        self.assertNotEqual([e["usd"] for e in a["estimates"]],
                            [e["usd"] for e in b["estimates"]])

    def test_both_filters_give_exactly_one_cell(self):
        doc, _ = self.est_json(*self.files, "--model", "haiku", "--effort", "medium")
        self.assertEqual(self.cells(doc), [("haiku", "medium")])
        c = self.shipped_cal()["haiku"]["medium"]
        self.assert_close(doc["estimates"][0]["usd"], 4 * c["usd_per_ktok"], "usd")

    def test_filters_narrow_the_budget_verdict(self):
        """R5: filters affect any_over_budget and cheapest_over_budget."""
        self.write_config({"budget_warn_usd": 0.05})
        self.write_cal({"cheap": {"low": {"usd_per_ktok": 0.001, "minutes_per_paper": 1.0}},
                        "dear": {"low": {"usd_per_ktok": 0.5, "minutes_per_paper": 1.0}}})
        allm, _ = self.est_json(*self.files)
        self.assertTrue(allm["any_over_budget"])
        self.assertEqual(allm["cheapest_over_budget"]["model"], "dear")
        only_cheap, _ = self.est_json(*self.files, "--model", "cheap")
        self.assertFalse(only_cheap["any_over_budget"])
        self.assertIsNone(only_cheap["cheapest_over_budget"])

    def test_filtering_down_to_nothing_is_not_a_crash(self):
        """A model that lacks the requested effort yields an empty estimates array, exit 0."""
        self.write_cal({"m": {"low": {"usd_per_ktok": 0.01, "minutes_per_paper": 1.0}}})
        doc, _ = self.est_json(*self.files, "--effort", "max")
        self.assertEqual(doc["estimates"], [])
        self.assertFalse(doc["any_over_budget"])
        self.assertIsNone(doc["cheapest_over_budget"])
        self.assertEqual(set(doc), TOP_KEYS)


# ========================================================================================
# R7 — the Haiku long-context surcharge
# ========================================================================================
class TestHaikuSurcharge(JsonCase):

    def big(self, n=1):
        """Documents of 97,000 tokens each: one reader is just over HAIKU_TIER."""
        return [write(self.p(f"big{i}.md"), "a" * (2900 * 97)) for i in range(n)]

    def test_surcharge_applies_only_to_the_key_haiku(self):
        """R7: 5x for the model keyed exactly 'haiku'; 'haiku-5-5' and 'myhaiku' get 1x."""
        mk = lambda: {"low": {"usd_per_ktok": 0.001, "minutes_per_paper": 1.0}}
        self.write_cal({"haiku": mk(), "haiku-5-5": mk(), "myhaiku": mk(), "sonnet": mk()})
        files = self.big(1)
        doc, _ = self.est_json(*files)
        self.assertTrue(doc["long_context_surcharge"])
        total = doc["total_tokens"]
        self.assert_close(self.cell(doc, "haiku", "low")["usd"], total / 1000 * 0.001 * 5, "haiku")
        for m in ("haiku-5-5", "myhaiku", "sonnet"):
            self.assert_close(self.cell(doc, m, "low")["usd"], total / 1000 * 0.001, m)

    def test_surcharge_warning_names_per_agent(self):
        doc, _ = self.est_json(*self.big(1))
        self.assertTrue(doc["long_context_surcharge"])
        self.assertTrue(any("--per-agent" in w for w in doc["warnings"]),
                        f"warnings must advise lowering --per-agent: {doc['warnings']}")

    def test_no_surcharge_below_the_tier_and_no_such_warning(self):
        doc, _ = self.est_json(self.doc_file("a.md", 1))
        self.assertFalse(doc["long_context_surcharge"])
        self.assertFalse(any("--per-agent" in w for w in doc["warnings"]), doc["warnings"])
        c = self.shipped_cal()["haiku"]["low"]
        self.assert_close(self.cell(doc, "haiku", "low")["usd"], c["usd_per_ktok"], "1x haiku")

    def test_tier_boundary_is_strictly_greater(self):
        """R7: exactly 100,000 tokens of reader context is not a surcharge; one more is."""
        at = write(self.p("at.md"), "a" * 279270)        # 96,300.0 tokens + 3700 == 100000
        doc, _ = self.est_json(at, "--model", "haiku", "--effort", "low")
        self.assertEqual(doc["largest_reader_tokens"], HAIKU_TIER)
        self.assertFalse(doc["long_context_surcharge"], "100000 is not > 100000")
        c = self.shipped_cal()["haiku"]["low"]["usd_per_ktok"]
        self.assert_close(doc["estimates"][0]["usd"], 96300.0 / 1000 * c, "unmultiplied")

        over = write(self.p("over.md"), "a" * 279273)    # 96,301.03... tokens
        doc2, _ = self.est_json(over, "--model", "haiku", "--effort", "low")
        self.assertTrue(doc2["long_context_surcharge"])
        self.assert_close(doc2["estimates"][0]["usd"], 279273 / 2.9 / 1000 * c * 5, "5x")

    def test_lowering_per_agent_clears_the_surcharge(self):
        """R6+R7 interaction: the surcharge depends on the grouping, not on the total."""
        files = [write(self.p(f"h{i}.md"), "a" * (2900 * 60)) for i in range(3)]  # 60k each
        high, _ = self.est_json(*files, "--per-agent", "3")
        low, _ = self.est_json(*files, "--per-agent", "1")
        self.assertTrue(high["long_context_surcharge"])
        self.assertFalse(low["long_context_surcharge"])
        self.assertEqual(high["total_tokens"], low["total_tokens"])
        self.assert_close(self.cell(high, "haiku", "low")["usd"],
                          5 * self.cell(low, "haiku", "low")["usd"], "5x vs 1x")


# ========================================================================================
# R8 — calibration provenance
# ========================================================================================
class TestCalibrationProvenance(JsonCase):

    def test_shipped_calibration_is_upstream_with_its_path_and_measured_date(self):
        doc, _ = self.est_json(self.doc_file("a.md"))
        cal = doc["calibration"]
        self.assertEqual(set(cal), {"source", "path", "measured"})
        self.assertEqual(cal["source"], "upstream")
        self.assertEqual(os.path.realpath(cal["path"]), os.path.realpath(SHIPPED_CAL))
        self.assertTrue(os.path.isabs(cal["path"]))
        self.assertEqual(cal["measured"], self.shipped_cal()["_measured"])

    def test_user_calibration_wins_and_its_models_alone_are_used(self):
        self.write_cal({"_measured": "2026-02-03",
                        "mine": {"low": {"usd_per_ktok": 0.007, "minutes_per_paper": 1.5}}})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual(doc["calibration"]["source"], "yours")
        self.assertEqual(os.path.realpath(doc["calibration"]["path"]),
                         os.path.realpath(os.path.join(self.data, "calibration.json")))
        self.assertEqual(doc["calibration"]["measured"], "2026-02-03")
        self.assertEqual(self.cells(doc), [("mine", "low")])
        self.assert_close(doc["estimates"][0]["usd"], 0.007, "usd")

    def test_missing_measured_is_null_not_absent(self):
        self.write_cal({"m": {"low": {"usd_per_ktok": 0.01, "minutes_per_paper": 1.0}}})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertIn("measured", doc["calibration"])
        self.assertIsNone(doc["calibration"]["measured"])

    def test_source_is_upstream_iff_the_loaded_file_says_upstream(self):
        for src, want in [("upstream", "upstream"), ("yours", "yours"), ("somebody", "yours")]:
            with self.subTest(src=src):
                self.write_cal({"_source": src,
                                "m": {"low": {"usd_per_ktok": 0.01, "minutes_per_paper": 1.0}}})
                doc, _ = self.est_json(self.doc_file("a.md"))
                self.assertEqual(doc["calibration"]["source"], want)
                self.assertEqual(os.path.realpath(doc["calibration"]["path"]),
                                 os.path.realpath(os.path.join(self.data, "calibration.json")))


# ========================================================================================
# R9 — synthesis
# ========================================================================================
class TestSynthesis(JsonCase):

    def test_context_tokens_and_usd_match_the_price_table(self):
        files = [self.doc_file(f"f{i}.md", 1) for i in range(3)]
        doc, _ = self.est_json(*files)
        s = doc["synthesis"]
        self.assertEqual(set(s), {"model", "usd", "context_tokens"})
        self.assertEqual(s["context_tokens"], 1500 * 3 + 6000)
        self.assertIs(type(s["context_tokens"]), int)
        self.assertEqual(s["model"], "claude-opus-5")
        self.assert_close(s["usd"], self.synthesis_usd("claude-opus-5", s["context_tokens"]), "usd")

    def test_context_tokens_scale_with_documents_not_tokens(self):
        a, _ = self.est_json(self.doc_file("a.md", 1))
        b, _ = self.est_json(self.doc_file("a.md", 1), self.doc_file("b.md", 1))
        self.assertEqual(b["synthesis"]["context_tokens"] - a["synthesis"]["context_tokens"], 1500)
        self.assertGreater(b["synthesis"]["usd"], a["synthesis"]["usd"])

    def test_planner_env_override_changes_model_and_price(self):
        """R9: $LACUNA_PLANNER must be observable; a hard-coded planner id fails here."""
        f = self.doc_file("a.md")
        default, _ = self.est_json(f)
        other, _ = self.est_json(f, env_extra={"LACUNA_PLANNER": "claude-haiku-5-5"})
        self.assertEqual(other["synthesis"]["model"], "claude-haiku-5-5")
        self.assert_close(other["synthesis"]["usd"],
                          self.synthesis_usd("claude-haiku-5-5",
                                             other["synthesis"]["context_tokens"]), "usd")
        self.assertNotEqual(other["synthesis"]["usd"], default["synthesis"]["usd"])

    def test_planner_from_config_json_changes_model(self):
        self.write_config({"models": {"planner": "my-sonnet-5-5-preview"}})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual(doc["synthesis"]["model"], "my-sonnet-5-5-preview")
        self.assert_close(doc["synthesis"]["usd"],
                          self.synthesis_usd("my-sonnet-5-5-preview",
                                             doc["synthesis"]["context_tokens"]), "usd")

    def test_price_lookup_prefers_the_longest_matching_key(self):
        """R9: 'opus-5-5' must win over 'opus-5' for the id claude-opus-5-5."""
        doc, _ = self.est_json(self.doc_file("a.md"),
                               env_extra={"LACUNA_PLANNER": "claude-opus-5-5"})
        pr = self.prices()
        want = doc["synthesis"]["context_tokens"] * pr["opus-5-5"][2] / 1e6 \
            + 8000 * pr["opus-5-5"][4] / 1e6
        self.assert_close(doc["synthesis"]["usd"], want, "longest-key price")
        self.assertNotAlmostEqual(
            doc["synthesis"]["usd"],
            doc["synthesis"]["context_tokens"] * pr["opus-5"][2] / 1e6 + 8000 * pr["opus-5"][4] / 1e6,
            places=6)

    def test_unknown_planner_gives_null_synthesis_a_warning_and_exit_zero(self):
        doc, r = self.est_json(self.doc_file("a.md"),
                               env_extra={"LACUNA_PLANNER": "gpt-nothing-at-all"})
        self.assertIsNone(doc["synthesis"])
        self.assertTrue(any("gpt-nothing-at-all" in w for w in doc["warnings"]),
                        f"a warning must name the planner id: {doc['warnings']}")
        self.assertEqual(r.returncode, 0)

    def test_zero_output_price_counts_as_no_price(self):
        """R9: a matched row with output price 0 means synthesis is null, not $0.00."""
        write(os.path.join(self.data, "pricing.json"),
              json.dumps({"_retrieved": "2026-09-09",
                          "models": {"freebie-9": [1.0, 1.0, 1.0, 1.0, 0.0]}}))
        doc, _ = self.est_json(self.doc_file("a.md"),
                               env_extra={"LACUNA_PLANNER": "freebie-9"})
        self.assertIsNone(doc["synthesis"])
        self.assertTrue(any("freebie-9" in w for w in doc["warnings"]), doc["warnings"])

    def test_prices_retrieved_follows_the_price_table_actually_used(self):
        default, _ = self.est_json(self.doc_file("a.md"))
        with open(SHIPPED_PRICING) as fh:
            self.assertEqual(default["prices_retrieved"], json.load(fh)["_retrieved"])
        write(os.path.join(self.data, "pricing.json"),
              json.dumps({"_retrieved": "1999-01-01", "models": {"opus-5": [1, 1, 1, 1, 1]}}))
        over, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual(over["prices_retrieved"], "1999-01-01")


# ========================================================================================
# R10 — one cost, one budget verdict (contract change, both modes)
# ========================================================================================
class TestBudgetVerdict(JsonCase):

    def test_over_budget_uses_the_multiplied_haiku_cost(self):
        """R10: the 5x cost is the cost. The reference compares the unmultiplied one and
        would report haiku as under budget and name the dearer row as cheapest."""
        # 97,000 tokens in one reader -> surcharge on.
        f = write(self.p("big.md"), "a" * (2900 * 97))
        # haiku: 0.97 unmultiplied (under 1.50), 4.85 multiplied; zopus 1.94
        self.write_cal({"haiku": {"low": {"usd_per_ktok": 0.01, "minutes_per_paper": 1.0}},
                        "zopus": {"low": {"usd_per_ktok": 0.02, "minutes_per_paper": 2.0}}})
        self.write_config({"budget_warn_usd": 1.5})
        doc, _ = self.est_json(f)
        self.assertTrue(doc["long_context_surcharge"])
        h, z = self.cell(doc, "haiku", "low"), self.cell(doc, "zopus", "low")
        self.assert_close(h["usd"], doc["total_tokens"] / 1000 * 0.01 * 5, "haiku usd")
        self.assertTrue(h["over_budget"], "multiplied haiku cost is over 1.50")
        self.assertTrue(z["over_budget"])
        self.assertTrue(doc["any_over_budget"])
        self.assertEqual(doc["cheapest_over_budget"]["model"], "zopus")
        self.assertEqual(doc["cheapest_over_budget"]["effort"], "low")
        self.assert_close(doc["cheapest_over_budget"]["usd"], z["usd"], "cheapest usd")

    def test_human_mode_counts_and_names_the_same_set(self):
        """R10 in human mode: the warning line must agree with the JSON verdict."""
        f = write(self.p("big.md"), "a" * (2900 * 97))
        self.write_cal({"haiku": {"low": {"usd_per_ktok": 0.01, "minutes_per_paper": 1.0}},
                        "zopus": {"low": {"usd_per_ktok": 0.02, "minutes_per_paper": 2.0}}})
        self.write_config({"budget_warn_usd": 1.5})
        doc, _ = self.est_json(f)
        human = self.run_est(f)
        self.assertEqual(human.returncode, 0, human.stderr[-400:])
        n = sum(1 for e in doc["estimates"] if e["over_budget"])
        self.assertEqual(n, 2)
        self.assertIn(f"{n} of these combinations cost more than budget_warn_usd", human.stdout)
        line = [l for l in human.stdout.splitlines() if "Cheapest over the line" in l]
        self.assertTrue(line, human.stdout)
        self.assertIn(doc["cheapest_over_budget"]["model"], line[0])
        self.assertNotIn("haiku at", line[0])

    def test_cheapest_breaks_ties_in_estimates_order(self):
        mk = {"low": {"usd_per_ktok": 0.5, "minutes_per_paper": 1.0}}
        self.write_cal({"first": dict(mk), "second": dict(mk)})
        self.write_config({"budget_warn_usd": 0.1})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertEqual([e["usd"] for e in doc["estimates"]][0],
                         [e["usd"] for e in doc["estimates"]][1])
        self.assertEqual(doc["cheapest_over_budget"]["model"], "first")

    def test_over_budget_is_strictly_greater(self):
        self.write_cal({"m": {"low": {"usd_per_ktok": 0.002, "minutes_per_paper": 1.0}}})
        self.write_config({"budget_warn_usd": 0.002})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assert_close(doc["estimates"][0]["usd"], 0.002, "usd")
        self.assertFalse(doc["estimates"][0]["over_budget"], "cost == threshold is not over")
        self.assertFalse(doc["any_over_budget"])
        self.assertIsNone(doc["cheapest_over_budget"])

    def test_nothing_over_budget_is_null_not_missing(self):
        self.write_config({"budget_warn_usd": 1000.0})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertFalse(doc["any_over_budget"])
        self.assertIsNone(doc["cheapest_over_budget"])
        self.assertFalse(any(e["over_budget"] for e in doc["estimates"]))
        self.assert_close(doc["budget_warn_usd"], 1000.0, "threshold")


# ========================================================================================
# R11 — a bad budget_warn_usd must not crash (both modes)
# ========================================================================================
class TestBudgetConfigRobustness(JsonCase):

    BAD = [("string", "2"), ("bool-true", True), ("list", [2]), ("dict", {"usd": 2}),
           ("empty-string", "")]

    def test_non_numeric_budget_is_a_warning_not_a_traceback_in_json_mode(self):
        for name, val in self.BAD:
            with self.subTest(name):
                self.write_config({"budget_warn_usd": val})
                doc, r = self.est_json(self.doc_file("a.md"))
                self.assertEqual(r.returncode, 0)
                self.assertNotIn("Traceback", r.stderr)
                self.assertIsNone(doc["budget_warn_usd"])
                self.assertFalse(doc["any_over_budget"])
                self.assertFalse(any(e["over_budget"] for e in doc["estimates"]))
                self.assertIsNone(doc["cheapest_over_budget"])
                self.assertTrue(any("budget_warn_usd" in w for w in doc["warnings"]),
                                f"{val!r}: warnings must mention budget_warn_usd: {doc['warnings']}")

    def test_non_numeric_budget_warns_on_stderr_in_human_mode(self):
        self.write_config({"budget_warn_usd": "2"})
        r = self.run_est(self.doc_file("a.md"))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("budget_warn_usd", r.stderr)
        self.assertNotIn("combinations cost more", r.stdout)

    def test_zero_negative_and_null_disable_silently(self):
        for val in (0, 0.0, -5, -0.5, None):
            with self.subTest(val=val):
                self.write_config({"budget_warn_usd": val})
                doc, r = self.est_json(self.doc_file("a.md"))
                self.assertIsNone(doc["budget_warn_usd"])
                self.assertFalse(doc["any_over_budget"])
                self.assertFalse(any("budget_warn_usd" in w for w in doc["warnings"]),
                                 f"{val!r} disables silently: {doc['warnings']}")

    def test_absent_config_uses_the_shipped_default(self):
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertIsNotNone(doc["budget_warn_usd"])
        self.assertIsInstance(doc["budget_warn_usd"], float)
        self.assert_close(doc["budget_warn_usd"], 2.0, "default threshold")

    def test_integer_budget_is_honoured(self):
        self.write_cal({"m": {"low": {"usd_per_ktok": 2.0, "minutes_per_paper": 1.0},
                              "max": {"usd_per_ktok": 4.0, "minutes_per_paper": 1.0}}})
        self.write_config({"budget_warn_usd": 3})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertNotIsInstance(doc["budget_warn_usd"], bool)
        self.assert_close(doc["budget_warn_usd"], 3.0, "threshold")
        self.assertFalse(self.cell(doc, "m", "low")["over_budget"])
        self.assertTrue(self.cell(doc, "m", "max")["over_budget"])

    def test_bool_false_disables(self):
        self.write_config({"budget_warn_usd": False})
        doc, _ = self.est_json(self.doc_file("a.md"))
        self.assertIsNone(doc["budget_warn_usd"])
        self.assertFalse(doc["any_over_budget"])


# ========================================================================================
# R12/R13 — refusals
# ========================================================================================
class TestRefusals(EstimateCase):

    def assert_refused(self, args, code, *, needle=None):
        for mode in ([], ["--json"]):
            with self.subTest(json=bool(mode)):
                r = self.run_est(*args, *mode)
                self.assertEqual(r.returncode, code, f"stderr={r.stderr[-400:]}")
                self.assertNotIn("Traceback", r.stderr)
                if needle:
                    self.assertIn(needle, r.stderr)
                if mode:
                    self.assertEqual(r.stdout, "", "R13: JSON mode writes nothing to stdout on error")

    def test_bad_effort_exits_two_in_both_modes(self):
        """R12: an unknown effort is refused, not printed as a column of dashes."""
        f = self.doc_file("a.md")
        for bad in ("extreme", "LOW", "", "medium "):
            with self.subTest(effort=bad):
                self.assert_refused([f, "--effort", bad], 2)

    def test_every_valid_effort_is_accepted(self):
        f = self.doc_file("a.md")
        for e in EFFORTS:
            with self.subTest(effort=e):
                r = self.run_est(f, "--effort", e, "--json")
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                doc = json.loads(r.stdout)
                self.assertEqual({x["effort"] for x in doc["estimates"]}, {e})

    def test_unknown_model_message_is_kept_verbatim(self):
        f = self.doc_file("a.md")
        self.assert_refused([f, "--model", "gpt-nothing"], 2,
                            needle="no calibration for model 'gpt-nothing'")

    def test_missing_path_exits_two_with_empty_stdout(self):
        self.assert_refused([self.p("nope.md")], 2, needle="no such path")

    def test_no_documents_exits_two_with_empty_stdout(self):
        empty = self.p("empty")
        os.makedirs(empty)
        write(os.path.join(empty, "notes.rst"), "x")
        self.assert_refused([empty], 2)

    def test_unknown_flag_exits_two_with_empty_stdout(self):
        self.assert_refused([self.doc_file("a.md"), "--jsonn"], 2)

    def test_no_arguments_exits_two(self):
        self.assert_refused([], 2)

    @unittest.skipIf(shutil.which("pdftotext", path=SAFE_PATH), "pdftotext is on the safe PATH")
    def test_pdf_without_a_parser_exits_three_with_empty_stdout(self):
        """R13: the PDF dependency failure keeps its exit code 3 and prints no JSON."""
        pdf = write(self.p("paper.pdf"), "%PDF-1.4 not really a pdf")
        self.assert_refused([pdf], 3)

    def test_help_does_not_emit_json(self):
        r = self.run_est("--help")
        self.assertIn(r.returncode, (0, 2))
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("usage", (r.stdout + r.stderr).lower())
        self.assertIn("--json", r.stdout + r.stderr)


# ========================================================================================
# R14/R15 — human mode intact, no shared mutable state
# ========================================================================================
class TestHumanModeAndStability(EstimateCase):

    def test_human_mode_still_prints_header_table_and_footer(self):
        """R14: without --json the reference's human output is unchanged in shape."""
        files = [self.doc_file(f"f{i}.md", 1) for i in range(2)]
        r = self.run_est(*files, "--per-agent", "2")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("2 documents, ~2,000 tokens; 1 reader(s) at 2 per reader", r.stdout)
        self.assertIn(f"(largest reader context ~{2000 + STARTUP:,} tokens)", r.stdout)
        self.assertIn("Calibration: upstream", r.stdout)
        self.assertIn(SHIPPED_CAL, r.stdout)
        self.assertIn("synthesis over 2 notes with claude-opus-5", r.stdout)
        self.assertIn("Prices retrieved", r.stdout)
        for m in ("haiku", "sonnet", "claude-opus-5"):
            self.assertRegex(r.stdout, rf"(?m)^{m}\s+\$")
        self.assertEqual(r.stdout.count("{"), 0, "human mode must not print JSON")

    def test_human_mode_honours_model_and_effort_filters(self):
        f = self.doc_file("a.md")
        r = self.run_est(f, "--model", "haiku", "--effort", "medium")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertNotIn("sonnet", r.stdout)
        self.assertIn("medium", r.stdout)
        self.assertNotIn("xhigh", r.stdout)

    def test_two_identical_runs_give_identical_json(self):
        """R15: no shared mutable state leaking between the table and the JSON."""
        files = [self.doc_file(f"f{i}.md", 1) for i in range(3)]
        a = self.run_est(*files, "--per-agent", "2", "--json")
        b = self.run_est(*files, "--per-agent", "2", "--json")
        self.assertEqual(a.returncode, 0, a.stderr[-300:])
        self.assertEqual(json.loads(a.stdout), json.loads(b.stdout))

    def test_running_does_not_rewrite_the_calibration_or_config_files(self):
        """R15: the loaded calibration must not be mutated on disk."""
        cal = {"_source": "upstream", "_measured": "2026-03-03",
               "m": {"low": {"usd_per_ktok": 0.01, "minutes_per_paper": 1.0}}}
        cal_path = os.path.join(self.data, "calibration.json")
        write(cal_path, json.dumps(cal))
        cfg_path = os.path.join(self.data, "config.json")
        write(cfg_path, json.dumps({"budget_warn_usd": 0.001}))
        before = (open(cal_path).read(), open(cfg_path).read())
        r = self.run_est(self.doc_file("a.md"), "--json")
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual((open(cal_path).read(), open(cfg_path).read()), before)
        self.assertEqual(json.loads(open(cal_path).read()), cal)

    def test_json_and_human_modes_agree_on_the_counts_they_both_show(self):
        files = [self.doc_file(f"f{i}.md", 1) for i in range(4)]
        human = self.run_est(*files, "--per-agent", "3")
        self.assertEqual(human.returncode, 0, human.stderr[-300:])
        rj = self.run_est(*files, "--per-agent", "3", "--json")
        self.assertEqual(rj.returncode, 0, rj.stderr[-300:])
        js = json.loads(rj.stdout)
        self.assertIn(f"{js['documents']} documents, ~{js['total_tokens']:,} tokens; "
                      f"{js['readers']} reader(s) at {js['per_agent']} per reader", human.stdout)
        self.assertIn(f"~{js['largest_reader_tokens']:,} tokens", human.stdout)


# ========================================================================================
# Amendments A1–A3 (added at the confront step by the orchestrator, not the blind author)
# ========================================================================================
class TestAmendments(JsonCase):

    def test_json_between_two_paths_is_still_a_flag(self):
        """A5: "anywhere" includes between positionals; argparse's default parser rejects that."""
        a, b = self.doc_file("a.md"), self.doc_file("b.md")
        middle, _ = self.est_json(a, "--json", b)
        last, _ = self.est_json(a, b, "--json")
        self.assertEqual(middle, last)
        self.assertEqual(middle["documents"], 2)

    def test_integer_calibration_values_still_emit_floats(self):
        """A1: a hand-written calibration with integers must not leak JSON ints into usd/minutes."""
        self.write_cal({"m": {"low": {"usd_per_ktok": 1, "minutes_per_paper": 2}}})
        self.write_config({"budget_warn_usd": 0.5})
        doc, _ = self.est_json(self.doc_file("a.md"))
        e = self.cell(doc, "m", "low")
        for key in ("usd", "minutes"):
            self.assertIsInstance(e[key], float, f"{key} = {e[key]!r}")
        self.assertIsInstance(doc["cheapest_over_budget"]["usd"], float)

    def test_non_finite_budget_disables_with_a_warning(self):
        """A2: Infinity, -Infinity and NaN are valid to Python's json, and none is a threshold."""
        for name, val in (("inf", "Infinity"), ("-inf", "-Infinity"), ("nan", "NaN")):
            with self.subTest(name):
                write(os.path.join(self.data, "config.json"), '{"budget_warn_usd": %s}' % val)
                doc, r = self.est_json(self.doc_file("a.md"))
                self.assertIsNone(doc["budget_warn_usd"])
                self.assertFalse(doc["any_over_budget"])
                self.assertTrue(any("budget_warn_usd" in w for w in doc["warnings"]), doc["warnings"])

    def test_estimate_does_not_mutate_the_calibration_or_config_it_is_given(self):
        """A3: R15 in-process. One CLI call visits each cell once, so in-place edits are invisible there."""
        self.write_cal({"_source": "mine",
                        "haiku": {e: {"usd_per_ktok": 0.001, "minutes_per_paper": 1.0} for e in EFFORTS}})
        big = self.doc_file("big.md", 120)          # long-context surcharge on, so the 5x path runs
        probe = (
            "import copy, json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import lit_cost, lacuna_config\n"
            "cal = copy.deepcopy(lit_cost.CAL); before = copy.deepcopy(cal)\n"
            "cfg = copy.deepcopy(lacuna_config.CONFIG); cfg_before = copy.deepcopy(cfg)\n"
            "a, _ = lit_cost.estimate([sys.argv[2]], 1, cal=cal, config=cfg)\n"
            "b, _ = lit_cost.estimate([sys.argv[2]], 1, cal=cal, config=cfg)\n"
            "print(json.dumps({'cal_same': cal == before, 'cfg_same': cfg == cfg_before, 'repeat_same': a == b}))\n")
        r = subprocess.run([PY, "-c", probe, os.path.dirname(LIT_COST), big], capture_output=True, text=True,
                           cwd=self.tmp, env=base_env(self.home, self.data))
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        self.assertEqual(json.loads(r.stdout), {"cal_same": True, "cfg_same": True, "repeat_same": True})


# ========================================================================================
# R16 — docs
# ========================================================================================
class TestDocs(unittest.TestCase):

    def test_usage_line_shows_the_flag(self):
        with open(os.path.join(ROOT, "bin", "lacuna")) as f:
            line = [l for l in f if "lit estimate" in l and "lacuna lit estimate" in l]
        self.assertTrue(line, "no usage line for `lacuna lit estimate` in bin/lacuna")
        self.assertTrue(any("--json" in l for l in line), f"usage line lacks [--json]: {line}")

    def test_skill_step_zero_uses_json_and_names_any_over_budget(self):
        with open(os.path.join(ROOT, "skills", "lit-review", "SKILL.md")) as f:
            text = f.read()
        head = text.split("## ")[0] + "".join(s for s in text.split("## ")[:3])
        self.assertIn("lit estimate", head)
        self.assertIn("--json", head, "SKILL.md step 0 must show the --json invocation")
        self.assertIn("any_over_budget", text,
                      "SKILL.md must name any_over_budget as the approval gate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
