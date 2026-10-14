#!/usr/bin/env python3
"""Mutation testing: inject each known defect and check that the suite notices.

A test earns its place by being the thing that fails when a specific defect is present. This is
the list of those defects, one per line of MUTANTS, and the runner that applies each to a scratch
copy of the repo and runs the whole suite against it. Every mutant must be killed (at least one
test fails). A mutant that survives marks a defect the suite would let ship.

usage: tools/mutate.py [--jobs N] [--only SUBSTRING] [--matrix OUT.json] [--python PY]
  exit 0 when every mutant is killed, 1 otherwise.
  --matrix writes {mutant: [failing test ids]} so a test's worth can be read off: a test that
  kills nothing no other test kills is dead weight; a mutant nothing kills is a missing test.

Add a mutant when you fix a defect (revert the fix as the mutant) and when you add a test (say what
it catches). Each entry: (name, file, old, new) — `old` must occur exactly once in `file`.
"""
import argparse, concurrent.futures as cf, json, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE, PREP, SPLIT, COST, CFG, CCOST, SPEND, GUARD, STATUS, DOCTOR, PANE = (
    "scripts/lit/lit_pipeline.py", "scripts/lit/prep_md.py", "scripts/lit/split_book.py",
    "scripts/lit/lit_cost.py", "scripts/lacuna_config.py", "scripts/cost/ccost.py",
    "scripts/cost/spend.py", "hooks/cache_guard.py", "statusline/statusline.py",
    "scripts/doctor.py", "scripts/panes/results_pane.py")

MUTANTS = [
    # ---- note cache key: every omission is a free, fast, wrong answer
    ("key-drops-content", PIPE, "[content_h, PROMPT_VERSION,", "[PROMPT_VERSION,"),
    ("key-drops-prompt-version", PIPE, "[content_h, PROMPT_VERSION,", "[content_h,"),
    ("key-drops-model", PIPE, "PROMPT_VERSION, reader_model, reader_effort", "PROMPT_VERSION, reader_effort"),
    ("key-drops-effort", PIPE, "reader_model, reader_effort, a.brief", "reader_model, a.brief"),
    ("key-drops-brief", PIPE, "reader_effort, a.brief, books_fp", "reader_effort, books_fp"),
    ("key-drops-books", PIPE, "reader_effort, a.brief, books_fp", "reader_effort, a.brief"),
    ("key-books-ignore-index-content", PIPE, "books_fp = hashlib.sha256((books + indexes).encode()).hexdigest()[:8]",
     "books_fp = hashlib.sha256(books.encode()).hexdigest()[:8]"),
    ("key-books-as-flag", PIPE, "books_fp = hashlib.sha256((books + indexes).encode()).hexdigest()[:8]",
     "books_fp = str(not a.no_books)"),
    ("parse-cache-drops-version", PIPE, "_{h}_p{PARSE_VERSION}{tag}.md", "_{h}{tag}.md"),
    ("parse-cache-drops-parser-tag", PIPE, "_{h}_p{PARSE_VERSION}{tag}.md", "_{h}_p{PARSE_VERSION}.md"),
    # ---- success judged by existence or size, not content
    ("cache-hit-keeps-stale-out", PIPE, "        shutil.copy(cached, out)      # always refresh",
     "        if not os.path.exists(out): shutil.copy(cached, out)  #"),
    ("rerun-inherits-old-note", PIPE, "        if os.path.exists(out):\n            os.remove(out)\n", "\n"),
    ("note-judged-by-size", PIPE, 'ok = usable_output(out, 400, "note", must_contain=("## Claim",))',
     "ok = os.path.exists(out) and os.path.getsize(out) > 400"),
    ("plan-judged-by-size", PIPE, "ok = usable_output(a.out, 800, a.kind)",
     "ok = os.path.exists(a.out) and os.path.getsize(a.out) > 800"),
    ("note-structure-not-checked", PIPE, 'must_contain=("## Claim",))', "must_contain=())"),
    ("refusal-words-ignored", PIPE, "hit = next((m for m in REFUSAL_MARKERS if m in head.lower()), None)", "hit = None"),
    ("any-blocked-is-refusal", PIPE, 'if "BLOCKED" in head[:400]:', 'if "blocked" in head[:400].lower():'),
    ("failed-read-exits-0", PIPE, "        sys.exit(EX_MODEL)\n    after(\"read\", out_dir)", "\n    after(\"read\", out_dir)"),
    ("failed-read-leaves-apology", PIPE, "        if os.path.exists(out):\n            os.remove(out)      # an apology", "        if False:\n            os.remove(out)      # an apology"),
    ("failed-read-caches-note", PIPE, "        if ok:\n            shutil.copy(out, cached)", "        if True:\n            shutil.copy(out, cached)"),
    ("verify-ignores-is-error", PIPE, 'if res.get("is_error") or not report.strip():', "if False:"),
    ("synth-skips-repo-check", PIPE, '    need_dir(a.repo, "--repo")\n    if a.digest:', "    if a.digest:"),
    ("synth-out-dir-accepted", PIPE, "    if os.path.isdir(a.out):\n        print(f\"ERROR: --out {a.out!r} is a directory",
     "    if False:\n        print(f\"ERROR: --out {a.out!r} is a directory"),
    ("read-out-file-accepted", PIPE, "    if os.path.isfile(out_dir):", "    if False:"),
    ("relative-paths-not-absolute", PIPE, "    a.repo = os.path.abspath(a.repo)\n", "\n"),
    ("txt-note-named-with-dot", PIPE, "os.path.splitext(os.path.basename(md))[0])", "os.path.basename(md)[:-3])"),
    ("after-without-shell", PIPE, 'argv = ["sh", "-c", cmd + \' "$1"\', "sh", out_path]',
     "argv = [*cmd.split(), out_path]"),
    ("after-failure-raises", PIPE, "    except (OSError, ValueError) as e:", "    except () as e:"),
    ("walk-keeps-node-modules", PIPE, '"node_modules", "__pycache__",', '"__pycache__",'),
    ("status-order-wrong", PIPE, 'STATUS_CANDIDATES = ["research/STATUS.md", "STATUS.md", "docs/STATUS.md", "NOTES.md"]',
     'STATUS_CANDIDATES = ["NOTES.md", "STATUS.md", "docs/STATUS.md", "research/STATUS.md"]'),
    ("books-without-toc-offered", PIPE, 'if os.path.exists(f"{BOOKS_DIR}/{d}/toc.md"))', "if True)"),
    ("no-books-flag-ignored", PIPE, 'books = "" if a.no_books else books_block()', "books = books_block()"),
    ("ledger-drops-effort", PIPE, '"effort": reader_effort,\n', "\n"),
    ("model-flag-ignored", PIPE, 'reader_model = a.model or lacuna_config.model("reader")',
     'reader_model = lacuna_config.model("reader")'),
    # ---- prep_md: a bad parse looks like good markdown
    ("parser-changed-version-not-bumped", PREP, "KEEP_FRACTION = 0.60", "KEEP_FRACTION = 0.61"),
    ("validate-no-truncation-check", PREP, "if raw_chars > 2000 and len(md) < raw_chars * KEEP_FRACTION:", "if False:"),
    ("validate-no-page-floor", PREP, "    if len(md) < 1200 * max(pages, 1) * 0.25:", "    if False:"),
    ("validate-floor-only-without-baseline", PREP, "    if len(md) < 1200 * max(pages, 1) * 0.25:", "    if not raw_chars and len(md) < 1200 * max(pages, 1) * 0.25:"),
    ("validate-any-ctrl-rejects", PREP, "CTRL_BUDGET = 8", "CTRL_BUDGET = 0"),
    ("validate-ctrl-flood-accepted", PREP, "    if n_ctrl > CTRL_BUDGET:", "    if False:"),
    ("validate-no-heading-check", PREP, 'if pages >= 4 and len(re.findall(r"(?m)^#{1,4} \\S", md)) == 0:', "if False:"),
    ("validate-no-section-check", PREP, 'if not re.search(r"(?i)(abstract|introduction|method|result)", md):', "if False:"),
    ("last-parser-accepted-when-bad", PREP, "        if bad:\n", "        if bad and parser != order[-1]:\n"),
    ("fallback-never-tried", PREP, 'else ["pymupdf4llm", "docling"])', 'else ["pymupdf4llm"])'),
    ("refs-cut-to-end-of-file", PREP, 'md = md[:first.start()] + "\\n" + body(md[first.end():])', "md = md[:first.start()]"),
    ("refs-not-stripped", PREP, "    first = next(REF.finditer(md), None)", "    first = None"),
    ("refs-acs-heading-missed", PREP, r"(?:<[^>\n]*>)*\**\s*(?:\d+[.)]\s*)?", r"\**\s*(?:\d+[.)]\s*)?"),
    ("refs-prose-mention-cuts", PREP, 'REF = re.compile(r"\\n[#*\\s■□●▪•]*', 'REF = re.compile(r"[#*\\s■□●▪•]*'),
    ("clean-keeps-ctrl", PREP, '    md = CTRL.sub("", md)\n    return', "    return"),
    ("wrap-splits-table-rows", PREP, '([ln] if ln.lstrip().startswith("|") else', "([ln] if False else"),
    ("parse-error-keeps-traceback-noise", PREP, 'if l.strip() and not l.startswith((" ", "\\t")) and not l.lstrip().startswith(\'File "\')]', 'if l.strip() and not l.lstrip().startswith(("File \\"", "  "))]'),
    ("wrap-drops-words", PREP, "textwrap.wrap(ln, 300, break_long_words=False) or [\"\"]", "[ln[:300]]"),
    ("parser-flag-ignored", PREP, '    if "--parser" in sys.argv:', "    if False:"),
    # ---- split_book: the index a reader greps
    ("book-keeps-ctrl", SPLIT, '    text = CTRL.sub("", text)\n', "\n"),
    ("book-level-1-only", SPLIT, "toc = entries_at(level)\nif not toc:", "level = 1\ntoc = entries_at(level)\nif not toc:"),
    ("book-unsorted-toc", SPLIT, "toc.sort(key=lambda tp: tp[1])", "pass"),
    ("book-same-page-empty-chapter", SPLIT, "    if deduped and start == deduped[-1][1]:", "    if False:"),
    ("book-chapter-overruns", SPLIT, "end = toc[i + 1][1] - 1 if", "end = toc[i + 1][1] if"),
    ("book-no-toc-guessed", SPLIT, "if not full_toc:\n    die2(", "if False:\n    die2("),
    ("book-toc-not-written", SPLIT, 'with open(f"{outdir}/toc.md", "w") as f:', 'with open(f"{outdir}/toc.txt", "w") as f:'),
    # ---- lit_cost: the estimate the user approves before spending (from the dev-cycle run)
    ("est-per-agent-ignored", COST, "k = max(1, per_agent)", "k = 1"),
    ("est-effort-filter-noop", COST, "efforts = [effort] if effort else list(EFFORTS)", "efforts = list(EFFORTS)"),
    ("est-planner-hardcoded", COST, 'planner = lacuna_config.model("planner")', 'planner = "claude-opus-5"'),
    ("est-shared-row-dict", COST, 'ests.append({"model": m, "effort": e, "usd": usd,',
     'ests.append(ests[-1] if ests else {"model": m, "effort": e, "usd": usd,'),
    ("est-mutates-calibration", COST, "            c = cal.get(m, {}).get(e)\n            if not c:\n                continue\n",
     "            c = cal.get(m, {}).get(e)\n            if not c:\n                continue\n            c['usd_per_ktok'] = 0\n"),
    ("est-budget-ignores-surcharge", COST, '"over_budget": warn is not None and usd > warn})',
     '"over_budget": warn is not None and total / 1000 * c["usd_per_ktok"] > warn})'),
    ("est-bool-budget-accepted", COST, "if isinstance(raw, (int, float)) and not isinstance(raw, bool):", "if isinstance(raw, (int, float)):"),
    ("est-budget-warning-dropped", COST, "    if budget_msg:\n        warnings.append(budget_msg)\n", "\n"),
    ("est-partial-stdout-on-error", COST, "    missing = [p for p in a.paths if not os.path.exists(p)]",
     "    print('{')\n    missing = [p for p in a.paths if not os.path.exists(p)]"),
    ("est-tie-picks-last", COST, 'cheap = min(over, key=lambda x: x["usd"]) if over else None',
     'cheap = min(reversed(over), key=lambda x: x["usd"]) if over else None'),
    ("est-budget-ge", COST, '"over_budget": warn is not None and usd > warn})', '"over_budget": warn is not None and usd >= warn})'),
    ("est-surcharge-ge", COST, "surcharge = biggest > HAIKU_TIER", "surcharge = biggest >= HAIKU_TIER"),
    ("est-surcharge-any-haiku", COST, '(5 if m == "haiku" and surcharge else 1)', '(5 if "haiku" in m and surcharge else 1)'),
    ("est-models-sorted", COST, "known = [m for m in cal if isinstance(cal[m], dict)]", "known = sorted(m for m in cal if isinstance(cal[m], dict))"),
    ("est-usd-rounded", COST, 'usd = float(total / 1000 * c["usd_per_ktok"]', 'usd = round(total / 1000 * c["usd_per_ktok"]'),
    ("est-effort-unvalidated", COST, 'ap.add_argument("--effort", choices=EFFORTS,', 'ap.add_argument("--effort",'),
    ("est-synth-priced-at-5m", COST, "_, _, w1h, _, out_price = next(", "_, w1h, _, _, out_price = next("),
    ("est-source-from-path", COST, 'src = "upstream" if cal.get("_source") == "upstream" else "yours"',
     'src = "upstream" if CAL_PATH.startswith(HERE) else "yours"'),
    ("est-minutes-times-readers", COST, '"minutes": float(c["minutes_per_paper"] * biggest_n * waves)',
     '"minutes": float(c["minutes_per_paper"] * len(groups))'),
    ("est-ignores-reader-pool", COST, "waves = -(-len(groups) // POOL)", "waves = 1"),
    ("est-minutes-int", COST, '"minutes": float(c["minutes_per_paper"] * biggest_n * waves)',
     '"minutes": c["minutes_per_paper"] * biggest_n * waves'),
    ("est-error-prints-json", COST, 'def die2(msg):\n    """Bad input."""\n', 'def die2(msg):\n    """Bad input."""\n    print("{}")\n'),
    ("est-neg-inf-silent", COST, "        if not math.isfinite(raw):", "        if raw != raw or raw == float('inf'):"),
    ("est-alias-unpriced", COST, "concrete = lacuna_config.resolve(planner)", "concrete = planner"),
    ("est-corrupt-pdf-says-install-parser", COST, '        die2(f"ERROR: cannot read', '        pass  # die2(f"ERROR: cannot read'),
    ("est-zero-token-pdf-priced", COST, "if f.endswith(\".pdf\") and tokens(f) < 20]", "if False]"),
    ("est-math-ratio-ignored", COST, "ratio = 2.1 if", "ratio = 2.9 if"),
    # ---- config: precedence, merging, prices
    ("env-does-not-beat-file", CFG, "        if v:\n            cfg[section][key] = v", "        if False:\n            cfg[section][key] = v"),
    ("partial-section-wipes-defaults", CFG, "            if isinstance(v, dict):\n                _merge(base[k], v)",
     "            if isinstance(v, dict):\n                base[k] = v"),
    ("null-section-crashes", CFG, "            elif v is not None:", "            else:\n                base[k] = v\n            if False:"),
    ("tilde-not-expanded", CFG, "os.path.expanduser(os.path.expandvars(v)) if isinstance(v, str) else v", "v"),
    ("malformed-config-raises", CFG, "    except ValueError as e:\n        CONFIG_ERRORS.append(f\"{path}: not valid JSON",
     "    except () as e:\n        CONFIG_ERRORS.append(f\"{path}: not valid JSON"),
    ("prices-shortest-key-first", CFG, "key=lambda kv: -len(kv[0])", "key=lambda kv: len(kv[0])"),
    ("user-pricing-ignored", CFG, '    p.update(_price_table(_read_json(os.path.join(DATA, "pricing.json"))))\n', "\n"),
    ("alias-not-resolved", CFG, 'return ALIASES.get((model or "").strip(), model)', "return model"),
    ("null-effort-crashes", CFG, 'return CONFIG["effort"].get(role) or "medium"', 'return CONFIG["effort"].get(role, "medium")'),
    ("config-crashes-on-string-budget", CFG, "budget     warn above {_fmt_usd(cfg.get('budget_warn_usd'))}",
     "budget     warn above ${cfg.get('budget_warn_usd', 0):.2f}"),
    # ---- transcripts and money
    ("streaming-dup-double-counted", CCOST, "            if mid not in calls:\n                order.append(mid)", "            order.append(mid)"),
    ("write-priced-at-5m", CCOST, "w5, w1 = 0, u.get(\"cache_creation_input_tokens\", 0)  # no breakdown: assume 1h",
     "w1, w5 = 0, u.get(\"cache_creation_input_tokens\", 0)"),
    ("first-call-counted-as-rewrite", CCOST, "return index > 0 and is_cold(c)", "return is_cold(c)"),
    ("fleet-ignores-window", SPEND, "            if ts(call[\"ts\"]) < since:\n                continue\n", "\n"),
    ("unknown-session-falls-through", SPEND, '        if not path:\n            print(f"ERROR: no transcript matches', '        if False:\n            print(f"ERROR: no transcript matches'),
    ("subagents-not-rolled-up", SPEND, '    subs = glob.glob(os.path.join(path[:-6], "subagents", "*.jsonl"))', "    subs = []"),
    # ---- cache guard: a hook that raises breaks every prompt
    ("guard-raises-on-garbage", GUARD, "    except SystemExit:\n        raise\n    except Exception:\n        sys.exit(0)", "    except SystemExit:\n        raise"),
    ("guard-crashes-on-bad-idle", GUARD, "IDLE_MIN = _num(os.environ.get(\"LACUNA_IDLE_MIN\") or _CFG.get(\"idle_minutes\"), 60)",
     "IDLE_MIN = float(os.environ.get(\"LACUNA_IDLE_MIN\") or _CFG.get(\"idle_minutes\", 60))"),
    ("guard-never-holds", GUARD, "    if idle >= IDLE_MIN and ctx >= MIN_CTX:", "    if False:"),
    ("guard-holds-small-context", GUARD, "    if idle >= IDLE_MIN and ctx >= MIN_CTX:", "    if idle >= IDLE_MIN:"),
    ("guard-holds-resend", GUARD, "        if time.time() - st.get(\"held_at\", 0) < GRACE_S:", "        if False:"),
    ("guard-undated-line-disables-guard", GUARD, "        except ValueError:\n            continue\n        if when >= after_ts:",
     "        except ValueError:\n            return True\n        if when >= after_ts:"),
    ("guard-ignores-compaction", GUARD, '        if compacted_since(d.get("transcript_path") or "", ts):', "        if False:"),
    ("guard-off-switch-ignored", GUARD, '    if os.environ.get("LACUNA_GUARD_OFF"):\n        return', "    if False:\n        return"),
    ("guard-prices-at-5m", GUARD, "hit = next(((k, v[2]) for k, v in _PRICES.items() if k in (model or \"\")), None)",
     "hit = next(((k, v[1]) for k, v in _PRICES.items() if k in (model or \"\")), None)"),
    ("guard-no-resume-warning", GUARD, '        if d.get("source") in ("resume", "fork") and (call or "context_tokens" in d):', "        if False:"),
    # ---- status line: wrong numbers on every refresh
    ("status-guesses-cold", STATUS, '        elif pc.get("expires_at"):\n', "        else:\n"),
    ("status-null-expiry-is-cold", STATUS, '        elif pc.get("expires_at"):\n', "        elif pc:\n"),
    ("status-shortest-price-key", STATUS, "PRICE = dict(sorted(PRICE.items(), key=lambda kv: -len(kv[0])))", "PRICE = dict(sorted(PRICE.items(), key=lambda kv: len(kv[0])))"),
    ("status-counts-sidechain", STATUS, 'if e.get("type") != "assistant" or not u or e.get("isSidechain"):', 'if e.get("type") != "assistant" or not u:'),
    ("status-crashes-on-null", STATUS, '    cw = d.get("context_window") or {}', '    cw = d.get("context_window")'),
    ("status-ignores-width", STATUS, "print(sep.join(l2 if cols >= 90 else l2[:2]))", "print(sep.join(l2))"),
    # ---- doctor and the pane: first contact
    ("doctor-parser-blocks", DOCTOR, 'why="the default parser, ~4 s/document — needed only to read PDFs", blocking=False)',
     'why="the default parser, ~4 s/document — needed only to read PDFs", blocking=True)'),
    ("doctor-overwrites-config", DOCTOR, "    if os.path.exists(cfg_path):\n        say(OK, f\"{cfg_path}\", \"yours; left untouched\")\n    else:", "    if True:"),
    ("doctor-freezes-defaults", DOCTOR, '    "models": {},\n}', '    "models": dict(lacuna_config.DEFAULTS["models"]),\n}'),
    ("doctor-bare-marker-import", DOCTOR, '    code = probe or f"import {module}"', '    code = f"import {module}"'),
    ("pane-typo-makes-directory", PANE, "        elif not positional:\n            os.makedirs(target, exist_ok=True)", "        elif True:\n            os.makedirs(target, exist_ok=True)"),
    ("pane-flag-is-target", PANE, 'positional = [a for a in argv if not a.startswith("-")]', "positional = list(argv)"),
    ("pane-needs-rich", PANE, "except ImportError:                 # plain text", "except ():                 # plain text"),
]

RUNNER = r"""
import json, sys, unittest
loader = unittest.TestLoader()
suite = loader.discover("tests")
class R(unittest.TextTestResult):
    def __init__(s, *a, **k):
        super().__init__(*a, **k); s.failed = set()
    def addFailure(s, t, e): super().addFailure(t, e); s.failed.add(t.id())
    def addError(s, t, e): super().addError(t, e); s.failed.add(t.id())
    def addSubTest(s, t, sub, e):
        super().addSubTest(t, sub, e)
        if e is not None: s.failed.add(t.id())
r = unittest.TextTestRunner(stream=open("/dev/null", "w"), resultclass=R).run(suite)
print(json.dumps({"ran": r.testsRun, "failed": sorted(r.failed)}))
"""


def run_one(name, file, old, new, python):
    tmp = tempfile.mkdtemp(prefix="lacuna-mut-")
    try:
        shutil.copytree(ROOT, os.path.join(tmp, "repo"), ignore=shutil.ignore_patterns(".git", "__pycache__"))
        target = os.path.join(tmp, "repo", file)
        src = open(target).read()
        if src.count(old) != 1:
            return name, {"error": f"anchor occurs {src.count(old)} times in {file}"}
        open(target, "w").write(src.replace(old, new))
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        r = subprocess.run([python, "-c", RUNNER], cwd=os.path.join(tmp, "repo"), capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "en_US.UTF-8"})
        try:
            return name, json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return name, {"error": (r.stderr or r.stdout)[-600:]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--only", help="run mutants whose name contains this")
    ap.add_argument("--matrix", help="write the kill matrix here as JSON")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()
    todo = [m for m in MUTANTS if not a.only or a.only in m[0]]
    names = [m[0] for m in todo]
    assert len(set(names)) == len(names), "duplicate mutant names"
    matrix, alive = {}, []
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for name, res in ex.map(lambda m: run_one(*m, a.python), todo):
            matrix[name] = res
            if "error" in res:
                status = "ERROR  " + res["error"].strip().splitlines()[-1][:100]
            else:
                status = f"killed by {len(res['failed']):3d}" if res["failed"] else "SURVIVED"
            if "error" in res or not res.get("failed"):
                alive.append(name)
            print(f"  {name:36s} {status}", flush=True)
    if a.matrix:
        with open(a.matrix, "w") as f:
            json.dump({k: v.get("failed", v) for k, v in matrix.items()}, f, indent=1, sort_keys=True)
    print(f"\n{len(todo) - len(alive)}/{len(todo)} mutants killed" + (f"; NOT caught: {', '.join(alive)}" if alive else "."))
    return 1 if alive else 0


if __name__ == "__main__":
    sys.exit(main())
