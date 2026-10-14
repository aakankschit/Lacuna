#!/usr/bin/env python3
"""Mutation testing: inject each known defect and check that the suite notices.

A test earns its place by being the thing that fails when a specific defect is present. This is
the list of those defects, one per line of MUTANTS, and the runner that applies each to a scratch
copy of the repo and runs the whole suite against it. Every mutant must be killed (at least one
test fails). A mutant that survives marks a defect the suite would let ship.

usage: tools/mutate.py [--jobs N] [--only SUBSTRING] [--matrix OUT.json] [--python PY] [--check-anchors]
  exit 0 when every mutant is killed, 1 when one survives, 2 when an anchor is stale.
  --check-anchors checks only that every anchor still matches, in a second; tools/check.sh runs it.
  --matrix writes {mutant: [failing test ids]} so a test's worth can be read off: a test that
  kills nothing no other test kills is dead weight; a mutant nothing kills is a missing test.

Add a mutant when you fix a defect (revert the fix as the mutant) and when you add a test (say what
it catches). Each entry: (name, file, old, new) — `old` must occur exactly once in `file`.

A stale anchor is a broken gate, not a surviving mutant; so is a mutant whose result does not compile. At 0.2.0 six anchors had stopped matching
after fixes moved the code; the runner reported them among the survivors as "ERROR", and they read
as six test gaps when they were six mutants nobody was running. So every anchor is checked before
anything runs, and one that does not match exactly once stops the run with exit 2.
"""
import argparse, concurrent.futures as cf, json, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE, PREP, SPLIT, COST, CFG, CCOST, SPEND, GUARD, STATUS, DOCTOR, PANE, MUTATE = (
    "scripts/lit/lit_pipeline.py", "scripts/lit/prep_md.py", "scripts/lit/split_book.py",
    "scripts/lit/lit_cost.py", "scripts/lacuna_config.py", "scripts/cost/ccost.py",
    "scripts/cost/spend.py", "hooks/cache_guard.py", "statusline/statusline.py",
    "scripts/doctor.py", "scripts/panes/results_pane.py", "tools/mutate.py")

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
    ("cache-hit-keeps-stale-out", PIPE, '"cached (free)" if place(src, out, cached) else left',
     '"cached (free)" if os.path.exists(out) or place(src, out, cached) else left'),
    # success was inferred from what an earlier run had left at the note path (the reader now
    # writes a draft of its own, so that is the file judged)
    ("rerun-inherits-old-note", PIPE, 'ok = usable_output(draft, 400, "note"', 'ok = usable_output(out, 400, "note"'),
    ("note-judged-by-size", PIPE, 'ok = usable_output(draft, 400, "note", must_contain=("## Claim",))',
     "ok = os.path.exists(draft) and os.path.getsize(draft) > 400"),
    ("plan-judged-by-size", PIPE, "ok = usable_output(part, 800, a.kind)",
     "ok = os.path.exists(part) and os.path.getsize(part) > 800"),
    ("note-structure-not-checked", PIPE, 'must_contain=("## Claim",))', "must_contain=())"),
    ("refusal-words-ignored", PIPE, "    for _ in range(3):                       # \"Unfortunately, I could not…\"",
     "    for _ in range(0):"),
    ("status-marker-past-opening-line", PIPE, "        if not heading:\n            return None                       # the opening line",
     "        if False:\n            return None                       # the opening line"),
    ("failed-read-exits-0", PIPE, "        sys.exit(EX_MODEL)\n    if kept:", "\n    if kept:"),
    # an apology the reader left in --out was read as a note by the next synth: it writes a draft
    # in a directory of its own run instead
    ("reader-writes-the-note-path", PIPE, '-> {draft}", "Keep the note', '-> {out}", "Keep the note'),
    ("failed-read-caches-note", PIPE, "        if ok:\n            try:\n                _atomic_copy(draft, cached)",
     "        if True:\n            try:\n                _atomic_copy(draft, cached)"),
    ("verify-ignores-is-error", PIPE, 'if res.get("is_error") or not report.strip():', "if False:"),
    ("synth-skips-repo-check", PIPE, '    need_dir(a.repo, "--repo")\n    if a.digest:', "    if a.digest:"),
    ("synth-out-dir-accepted", PIPE, "    if os.path.isdir(a.out):\n        print(f\"ERROR: --out {a.out!r} is a directory",
     "    if False:\n        print(f\"ERROR: --out {a.out!r} is a directory"),
    ("read-out-file-accepted", PIPE, "    if os.path.isfile(out_dir):", "    if False:"),
    ("relative-paths-not-absolute", PIPE, "    a.repo = os.path.abspath(a.repo)\n", "\n"),
    ("txt-note-named-with-dot", PIPE, "stem = os.path.splitext(os.path.basename(src))[0]",
     "stem = os.path.basename(src)[:-3]"),
    ("after-without-shell", PIPE, 'argv = ["sh", "-c", cmd + \' "$1"\', "sh", out_path]',
     "argv = [*cmd.split(), out_path]"),
    ("after-failure-raises", PIPE, "    except (OSError, ValueError) as e:", "    except () as e:"),
    ("walk-keeps-node-modules", PIPE, '"node_modules", "__pycache__",', '"__pycache__",'),
    ("status-order-wrong", PIPE, 'STATUS_CANDIDATES = ["research/STATUS.md", "STATUS.md", "docs/STATUS.md", "NOTES.md"]',
     'STATUS_CANDIDATES = ["NOTES.md", "STATUS.md", "docs/STATUS.md", "research/STATUS.md"]'),
    ("books-without-toc-offered", PIPE, 'if os.path.exists(f"{BOOKS_DIR}/{d}/toc.md"))', "if True)"),
    ("no-books-flag-ignored", PIPE, "idx = [] if a.no_books else book_indexes()", "idx = book_indexes()"),
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
    ("tilde-not-expanded", CFG, "os.path.abspath(os.path.expanduser(os.path.expandvars(v))) if isinstance(v, str) and v",
     "os.path.abspath(v) if isinstance(v, str) and v"),
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
    ("first-call-counted-as-rewrite", CCOST, "    return prev is not None and is_cold(c, prev)", "    return is_cold(c, prev)"),
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
    ("status-counts-sidechain", STATUS, 'or not isinstance(u, dict) or e.get("isSidechain"):', "or not isinstance(u, dict):"),
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
    # ---- ownership: a file in the user's directory is replaced only if Lacuna wrote it, for this
    # document, unedited (N1/N6 at 0.2.0: proven by cache bytes, blind to which document)
    ("ownership-ignores-document", PIPE, "    if source and was and os.path.isfile(source):", "    if False:"),
    # B1: a changed document at the same path may be another one (a new download, same name)
    ("ownership-same-path-is-same-document", PIPE, "        if (_sha(source) == recorded) if isinstance(recorded, str) else here:",
     "        if here or _sha(source) == recorded:"),
    # B15: a note is not a plan, and a plan's record names no document to compare
    ("ownership-ignores-stage", PIPE, "    if theirs != stage:", "    if False:"),
    ("legacy-plan-taken-for-note", PIPE, '    if not entry.get("source"):\n        return "synth"',
     '    if not entry.get("source"):\n        return "read"'),
    ("legacy-report-taken-for-note", PIPE, '    return "verify" if entry.get("name") == os.path.basename(entry["source"]) + ".unverified.md" else "read"',
     '    return "read"'),
    ("ownership-never-recorded", PIPE, "            files[name_key(path)] = entry\n            _save_owners(d, files)\n    except OSError as e:",
     "            files[name_key(path)] = entry\n            pass\n    except OSError as e:"),
    ("edited-note-replaced", PIPE, '    if entry.get("sha256") != _sha(path):', "    if False:"),
    ("refusal-names-only-first", PIPE, "        for _, why in blocked.values():", "        for _, why in list(blocked.values())[:1]:"),
    ("legacy-note-not-adopted", PIPE, '        if blocked.get(note, ("",))[0] in (UNRECORDED, "edited") and same_bytes(note, cached):', "        if False:"),
    # B29: a note restored from a backup over one a hook had changed is exactly its cached note
    ("restored-note-not-adopted", PIPE, 'blocked.get(note, ("",))[0] in (UNRECORDED, "edited")', 'blocked.get(note, ("",))[0] in (UNRECORDED,)'),
    ("overwrite-flag-ignored", PIPE, "    if blocked and not a.overwrite:", "    if blocked:"),
    ("overwrite-writes-through-symlink", PIPE, "        os.replace(tmp, dst)", "        shutil.copyfile(tmp, dst)"),
    ("self-note-check-skippable", PIPE, "        if os.path.lexists(note) and same_file(note, src):", "        if False:"),
    ("self-check-by-realpath", PIPE, "        return os.path.samefile(a, b)", "        return os.path.realpath(a) == os.path.realpath(b)"),
    ("collision-ignores-case", PIPE, 'unicodedata.normalize("NFC", os.path.basename(path)).casefold()',
     'unicodedata.normalize("NFC", os.path.basename(path))'),
    ("collision-ignores-unicode-form", PIPE, 'unicodedata.normalize("NFC", os.path.basename(path)).casefold()',
     "os.path.basename(path).casefold()"),
    ("synth-out-unguarded", PIPE, '    why = not_ours(a.out, stage="synth")\n', "    why = None\n"),
    ("verify-report-unguarded", PIPE, '    why = not_ours(out, stage="verify")', "    why = None"),
    # B12: with the record deleted, a report annotated by hand was "ours" because of its name
    ("unrecorded-report-replaced", PIPE, '    why = not_ours(out, stage="verify")',
     '    why = not_ours(out, stage="verify") if os.path.exists(os.path.join(os.path.dirname(out), OWNERS)) else None'),
    ("clobbered-out-kept", PIPE, "        if changed_since(a.out, before):\n            # --out changed during the run: the model",
     "        if False:\n            # --out changed during the run: the model"),
    ("hook-edit-not-rerecorded", PIPE, "    before = as_recorded(d)\n", "    before = {}\n"),
    # B10: a hook that edits every file in the directory changes earlier runs' files too
    ("hook-rerecords-only-its-output", PIPE, "    before = as_recorded(d)\n",
     "    before = {k: v for k, v in as_recorded(d).items() if os.path.isdir(out_path) or k == name_key(out_path)}\n"),
    ("hook-adopts-user-edits", PIPE, '                    and os.path.isfile(p) and _sha(p) == e["sha256"]:',
     "                    and os.path.isfile(p):"),
    ("clobbered-out-deleted", PIPE, "            os.replace(a.out, rejected)", "            os.remove(a.out)"),
    # B11: --out or the report changed while the model ran; the edit was replaced on success
    ("changed-out-replaced-on-success", PIPE, "    if changed_since(a.out, before):\n        # Edited while the model ran",
     "    if False:\n        # Edited while the model ran"),
    ("changed-report-replaced-on-success", PIPE, "    clash = changed_since(out, before)", "    clash = False"),
    ("changed-report-removed-on-failure", PIPE, "        removed = stale and not changed_since(out, before)",
     "        removed = stale"),
    ("set-aside-replaces-earlier", PIPE, "    while os.path.lexists(", "    while False and os.path.lexists("),
    ("set-aside-name-too-long", PIPE, 'fitted(name, "", d, spare=len(os.fsencode(tag + ext)) + 4) + tag', "name + tag"),
    # B28: a damaged record was silently rewritten with only this run's entries
    ("damaged-record-silently-replaced", PIPE, "    if damaged:\n        kept = free_name(", "    if False:\n        kept = free_name("),
    ("refusal-silent-about-damage", PIPE, 'was kept as {kept[-1]})" if kept else "")', 'was kept as {kept[-1]})" if False else "")'),
    # B13/B14: a name the filesystem refuses after the model call costs the call
    ("atomic-copy-temp-longer-than-name", PIPE,
     '    tmp = os.path.join(os.path.dirname(dst), f".{os.getpid()}.{threading.get_ident()}.part")',
     '    tmp = f"{dst}.{os.getpid()}.part"'),
    ("note-cache-name-unbounded", PIPE, 'fitted(stem, f"_{key}.md", os.path.join(CACHE, "notes"))', 'f"{stem}_{key}.md"'),
    ("parse-cache-name-unbounded", PIPE, 'fitted(os.path.basename(path)[:-4], f"_{h}_p{PARSE_VERSION}{tag}.md",\n'
     '                                          os.path.join(CACHE, "md"), spare=len(".partial")))',
     'os.path.basename(path)[:-4] + f"_{h}_p{PARSE_VERSION}{tag}.md")'),
    ("parse-cache-name-ignores-its-temp", PIPE, 'spare=len(".partial")))', "spare=0))"),
    ("names-not-checked-before-paying", PIPE, "    if long:\n        print(\"ERROR: these file names", "    if False:\n        print(\"ERROR: these file names"),
    ("record-unlocked-between-processes", PIPE, "                fcntl.flock(self.fd, fcntl.LOCK_EX)", "                pass"),
    ("damaged-record-raises", PIPE, "    except Exception as e:       # OSError, ValueError, a list at the top, RecursionError on deep nesting",
     "    except (OSError, ValueError) as e:"),
    ("filelist-written-through-link", PIPE, "    if os.path.islink(tree_path):\n        os.remove(tree_path)", "    if False:\n        os.remove(tree_path)"),
    ("readonly-out-accepted", PIPE, "    if not os.access(d, os.W_OK):", "    if False:"),
    # ---- an output that says it holds nothing is not output (N2: a real "PARTIAL:" digest, exit 0)
    ("partial-marker-ignored", PIPE, r'r"|(?:PARTIAL|INCOMPLETE)\**(?=\s*[:.]|\s+[\u2013\u2014-]\s|\s*$))")', r'r"|(?!x)x)")'),
    ("marker-furniture-not-stripped", PIPE, 'stripped = _FURNITURE.sub("", line, count=1)',
     'stripped = line.lstrip("# ")'),
    ("graded-finding-is-a-marker", PIPE, 'm = None if "->" in line else STATUS_MARKER', "m = STATUS_MARKER"),
    ("bracketed-title-marker-ignored", PIPE, "        m2 = _TITLE_MARKER.search(line) if heading else None\n", "        m2 = None\n"),
    ("title-marker-negation-ignored", PIPE, "        if m2 and _MARKER_NEGATED.match(line[m2.end():]):\n            m2 = None\n", ""),
    # ---- one run writes a path at a time, and nothing a run leaves behind blocks the next (A4-A11)
    ("read-path-not-held", PIPE, '        need_hold(note, f"reading {src} into")', "        pass"),
    ("synth-out-not-held", PIPE, '    need_hold(a.out, "writing a plan or digest to")', "    pass"),
    ("verify-report-not-held", PIPE, '    need_hold(out, "writing a verify report to")', "    pass"),
    ("hold-not-exclusive", PIPE, "            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)", "            pass"),
    ("hold-trusts-a-dead-holders-file", PIPE, "        try:\n            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
     "        try:\n            if os.fstat(fd).st_size:\n                raise BlockingIOError\n"
     "            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"),
    ("holds-not-released", PIPE, "    finally:\n        release_all()", "    finally:\n        pass"),
    ("hold-failure-fatal", PIPE, "            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)\n        except OSError:\n            return None",
     "            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)\n        except OSError:\n            raise"),
    ("legacy-reservation-kept", PIPE, '        if isinstance(v, dict) and isinstance(v.get("sha256"), str):',
     '        if isinstance(v, dict) and (isinstance(v.get("sha256"), str) or v.get("pending") is True):'),
    ("cache-takes-the-note-path", PIPE, "                _atomic_copy(draft, cached)", "                _atomic_copy(out, cached)"),
    ("changed-note-replaced", PIPE, "        if changed_since(out, before[out]):\n            return False",
     "        if False:\n            return False"),
    ("changed-note-kept-on-failure", PIPE, "        if changed_since(out, before[out]):\n            # The note changed during a failed run",
     "        if False:\n            # The note changed during a failed run"),
    ("note-placed-not-recorded", PIPE, "never through a link\n        record(out, src)\n", "never through a link\n"),
    ("synth-reply-not-judged", PIPE, "    if ok and said:", "    if False:"),
    ("synth-unreadable-input-crashes", PIPE, '        except OSError as e:\n            empty.append(f"{f} cannot be read',
     '        except () as e:\n            empty.append(f"{f} cannot be read'),
    ("synth-inputs-not-checked", PIPE, '    if empty:\n        print("ERROR: these inputs', '    if False:\n        print("ERROR: these inputs'),
    # ---- the status line and the guard read what the CLI reads, as the CLI reads it (N7, F2, F3)
    ("cfg-data-not-expanded", CFG, 'DATA = os.path.expanduser(os.path.expandvars(\n    os.environ.get("LACUNA_DATA") or "~/.lacuna"))',
     'DATA = os.environ.get("LACUNA_DATA") or os.path.expanduser("~/.lacuna")'),
    ("status-data-not-expanded", STATUS, 'DATA = os.path.expanduser(os.path.expandvars(os.environ.get("LACUNA_DATA") or "~/.lacuna"))',
     'DATA = os.environ.get("LACUNA_DATA") or os.path.expanduser("~/.lacuna")'),
    ("status-import-guard-narrow", STATUS, "    except Exception:     # this runs at import, outside main()'s guard: nothing here may raise",
     "    except (OSError, ValueError):"),
    ("status-entry-not-validated", STATUS, "            t = tuple(float(x) for x in v)", "            t = tuple(v)"),
    ("status-reads-synthetic", STATUS, '        if m.get("model") == "<synthetic>":   # an API error Claude Code recorded, with zero usage',
     "        if False:"),
    ("status-ignores-iterations", STATUS, 'fig = f"${low:.3f}–${low + earlier:.3f}" if earlier >= 0.0005 else f"${low:.3f}"',
     'fig = f"${low:.3f}"'),
    ("status-inline-prices-stale", STATUS, '    "haiku-4-5": (1, 1.25, 2, .10, 5),\n', "\n"),
    ("price-string-accepted", CFG, '        if k.startswith("_") or not isinstance(v, (list, tuple)):   # "12345" is not five prices',
     '        if k.startswith("_"):'),
    ("price-nonfinite-accepted", CFG, "        if len(t) == 5 and all(math.isfinite(x) and x >= 0 for x in t):", "        if len(t) == 5:"),
    ("status-price-string-accepted", STATUS, "        if not isinstance(v, (list, tuple)):\n            continue\n        try:", "        try:"),
    ("status-price-nonfinite-accepted", STATUS, 'and len(t) == 5 and all(math.isfinite(x) and x >= 0 for x in t):', "and len(t) == 5:"),
    ("status-transcript-line-not-dict", STATUS, "        if not isinstance(e, dict):           # valid JSON that is not a transcript line\n            continue\n", "\n"),
    ("status-columns-crash", STATUS, '    try:\n        cols = int(os.environ.get("COLUMNS") or 120)\n    except ValueError:\n        cols = 120',
     '    cols = int(os.environ.get("COLUMNS", 120))'),
    ("guard-line-not-dict", GUARD, '        m = e.get("message") if isinstance(e, dict) else None     # valid JSON that is not a call',
     '        m = e.get("message")'),
    ("guard-reads-synthetic", GUARD, '        if e["message"].get("model") == "<synthetic>":', "        if False:"),
    ("guard-synthetic-by-substring", GUARD, '        if e["message"].get("model") == "<synthetic>":', '        if "<synthetic>" in ln:'),
    ("guard-fallback-data-unexpanded", GUARD, '    _DATA = os.path.expanduser(os.path.expandvars(os.environ.get("LACUNA_DATA") or "~/.lacuna"))',
     '    _DATA = os.environ.get("LACUNA_DATA") or os.path.expanduser("~/.lacuna")'),
    ("pane-fallback-unexpanded", PANE, 'DEFAULT_DIR = os.path.expanduser(os.path.expandvars(os.environ.get("LACUNA_RESULTS") or "~/.claude/results"))',
     'DEFAULT_DIR = os.environ.get("LACUNA_RESULTS") or os.path.expanduser("~/.claude/results")'),
    ("estimate-bad-calibration-raises", COST, "    except (OSError, ValueError) as e:\n        CAL_WARNING", "    except () as e:\n        CAL_WARNING"),
    # ---- /spend: a lower bound that says so (N5, §2)
    ("transcript-line-not-dict", CCOST, '            if not isinstance(d, dict) or d.get("type") != "assistant":', '            if d.get("type") != "assistant":'),
    ("synthetic-counted-as-call", CCOST, '            if not isinstance(u, dict) or not mid or m.get("model") == "<synthetic>":',
     "            if not isinstance(u, dict) or not mid:"),
    ("transcript-null-count-crashes", CCOST, "            u = {k: (v if v is not None else 0) for k, v in u.items()}", "            pass"),
    ("idle-recache-over-warm-prefix-missed", CCOST, "    return (read < split(prev)[1] / 2 and ts(c", "    return False and (read < split(prev)[1] / 2 and ts(c"),
    ("compaction-counted-as-cold", CCOST, 'and ts(c["ts"]) - ts(prev["ts"]) >= IDLE_S', "and True"),
    ("model-switch-counted-as-idle", CCOST, '            and c["model"] == prev["model"])', "            and True)"),
    ("sidechain-is-previous-call", CCOST, '        if c.get("side"):\n            continue\n        if is_avoidable_cold', "        if is_avoidable_cold"),
    ("earlier-iterations-priced-at-call-model", CCOST, 'earlier=[(i, i.get("model") or m.get("model"))', 'earlier=[(i, m.get("model"))'),
    ("session-drops-refused-attempts", SPEND, "    gap = sum(earlier_usd(x) for x in retried)", "    gap = 0"),
    ("fleet-drops-refused-attempts", SPEND, "                gap += earlier_usd(call)", "                pass"),
    ("fleet-counts-forks-twice", SPEND, '            if call["id"] in seen:\n                continue\n', "\n"),
    ("spend-hides-lower-bound", SPEND, 'LOWER_BOUND = "a lower bound — see docs/LIMITS.md"', 'LOWER_BOUND = "estimated"'),
    # ---- the gate itself: a stale anchor read as a test gap at 0.2.0
    ("stale-anchor-runs-anyway", MUTATE, "    if stale:\n        print(f\"STALE ANCHORS", "    if False:\n        print(f\"STALE ANCHORS"),
    ("battery-skips-pass", "tools/mutation_battery.py", "return 1 if missed else 2 if skipped and not allow_skips else 0",
     "return 1 if missed else 0"),
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
        if src.count(old) != 1:            # stale_anchors() ran first; this is a second line of defence
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


def stale_anchors(mutants):
    """(name, why) for every mutant that cannot be applied exactly as written."""
    out = []
    for name, file, old, new in mutants:
        try:
            with open(os.path.join(ROOT, file)) as f:
                n = f.read().count(old)
        except OSError as e:
            out.append((name, f"{file}: {e.strerror}"))
            continue
        if n != 1:
            out.append((name, f"anchor occurs {n} times in {file}"))
        elif old == new:
            out.append((name, "the mutation changes nothing"))
        elif file.endswith(".py"):
            # A mutant that does not compile is "killed" by every test for the wrong reason. Two
            # were, once: their anchors stopped mid-line and turned a comment into code.
            try:
                compile(open(os.path.join(ROOT, file)).read().replace(old, new), file, "exec")
            except SyntaxError as e:
                out.append((name, f"the mutated {file} does not compile (line {e.lineno}), so it tests nothing"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--only", help="run mutants whose name contains this")
    ap.add_argument("--matrix", help="write the kill matrix here as JSON")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--check-anchors", action="store_true", help="only check that every anchor matches")
    a = ap.parse_args()
    todo = [m for m in MUTANTS if not a.only or a.only in m[0]]
    names = [m[0] for m in todo]
    assert len(set(names)) == len(names), "duplicate mutant names"
    stale = stale_anchors(todo)
    if stale:
        print(f"STALE ANCHORS: {len(stale)} of {len(todo)} mutants cannot be applied, so the gate did not run.\n"
              "The code they guard has moved. Re-anchor each one at the code as it is now and confirm the\n"
              "suite still kills it; never delete one to make this pass.", file=sys.stderr)
        for name, why in stale:
            print(f"  {name:36s} {why}", file=sys.stderr)
        return 2
    if a.check_anchors:
        print(f"all {len(todo)} mutant anchors match")
        return 0
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
