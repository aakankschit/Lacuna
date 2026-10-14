#!/usr/bin/env python3
"""Independent mutation battery: re-introduce every defect this project actually shipped.

Each entry is a real defect found by an audit or an end-to-end run, not an invented one. A test
suite earns its size by catching these. Run it against any version of the repo:

    python3 mutation_battery.py <repo-dir>

For each mutation: apply it, run the suite, restore the file, and report whether the suite noticed.
A mutation that applies cleanly and leaves the suite GREEN is a hole.
"""
import os, shutil, subprocess, sys, tempfile

# (label, relative path, find, replace) — `find` must appear exactly once.
MUTATIONS = [
    # --- the cache family: every one of these presents as a free, fast, wrong answer ---
    ("note cache ignores the brief", "scripts/lit/lit_pipeline.py",
     "[content_h, PROMPT_VERSION, reader_model, reader_effort, a.brief, books_fp]",
     "[content_h, PROMPT_VERSION, reader_model, reader_effort, books_fp]"),
    ("note cache ignores which books exist", "scripts/lit/lit_pipeline.py",
     'books_fp = hashlib.sha256(books.encode()).hexdigest()[:8]',
     'books_fp = "constant"'),
    ("note cache ignores the reader model", "scripts/lit/lit_pipeline.py",
     "[content_h, PROMPT_VERSION, reader_model, reader_effort, a.brief, books_fp]",
     "[content_h, PROMPT_VERSION, reader_effort, a.brief, books_fp]"),
    ("parse cache ignores the parser choice", "scripts/lit/lit_pipeline.py",
     'tag = f"_{parser}" if parser else ("_marker" if equations else "")',
     'tag = ""'),

    # --- money guarantees ---
    ("synth stops validating --repo", "scripts/lit/lit_pipeline.py",
     '    need_dir(a.repo, "--repo")\n    if a.digest:', "    if a.digest:"),
    ("verify stops validating --target", "scripts/lit/lit_pipeline.py",
     '    need_file(a.target, "--target")\n', ""),
    ("verify files a refusal as a fact check", "scripts/lit/lit_pipeline.py",
     '    if res.get("is_error") or not report.strip():', "    if False:"),
    ("read infers success from a pre-existing file", "scripts/lit/lit_pipeline.py",
     "        if os.path.exists(out):\n            os.remove(out)\n", ""),
    ("a long apology counts as output", "scripts/lit/lit_pipeline.py",
     "    hit = next((m for m in REFUSAL_MARKERS if m in head), None)", "    hit = None"),

    # --- the extension seam ---
    ("after.* runs without a shell", "scripts/lit/lit_pipeline.py",
     '            argv = ["sh", "-c", cmd + \' "$1"\', "sh", out_path]',
     "            argv = [*shlex.split(cmd), out_path]"),

    # --- parsing: a bad parse looks exactly like a good one ---
    ("a parse failing validation is accepted", "scripts/lit/prep_md.py",
     "        if bad:\n", "        if bad and parser != order[-1]:\n"),
    ("truncation detection removed", "scripts/lit/prep_md.py",
     "    if raw_chars > 2000 and len(md) < raw_chars * KEEP_FRACTION:",
     "    if False:"),
    ("books keep control characters", "scripts/lit/split_book.py",
     '    text = CTRL.sub("", text)\n', ""),

    # --- configuration and accounting ---
    ("a null config section crashes every script", "scripts/lacuna_config.py",
     "        if isinstance(base.get(k), dict):", "        if isinstance(base.get(k), dict) and False:"),
    ("price table sorted shortest key first", "scripts/lacuna_config.py",
     "    return dict(sorted(p.items(), key=lambda kv: -len(kv[0])))",
     "    return dict(sorted(p.items(), key=lambda kv: len(kv[0])))"),
    ("model aliases stop resolving to a price", "scripts/lacuna_config.py",
     '    return ALIASES.get((model or "").strip(), model)', "    return model"),
    ("cold-rewrite definitions diverge again", "scripts/cost/spend.py",
     '            if kind == "main" and is_avoidable_cold(call, i):',
     '            if kind == "main" and i >= 0 and __import__("ccost").is_cold(call):'),

    # --- the hook that must never break a session ---
    ("cache_guard stops swallowing exceptions", "hooks/cache_guard.py",
     "    except Exception:\n        sys.exit(0)", "    except Exception:\n        raise"),
]


def run_suite(repo):
    env = {"PATH": "/usr/bin:/bin", "HOME": tempfile.mkdtemp(), "LANG": "en_US.UTF-8"}
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       cwd=repo, capture_output=True, text=True, env=env)
    return r.returncode == 0, (r.stderr or "").strip().splitlines()[-1:] or [""]


def main():
    repo = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    ok, line = run_suite(repo)
    print(f"baseline: {'GREEN' if ok else 'RED'}  {line[0]}")
    if not ok:
        sys.exit("the suite must be green before mutating anything")

    caught, missed, skipped = [], [], []
    for label, rel, find, repl in MUTATIONS:
        path = os.path.join(repo, rel)
        if not os.path.exists(path):
            skipped.append((label, f"{rel} is gone")); continue
        with open(path) as f:
            src = f.read()
        if src.count(find) != 1:
            skipped.append((label, f"anchor appears {src.count(find)}x — code moved"))
            continue
        backup = src
        with open(path, "w") as f:
            f.write(src.replace(find, repl, 1))
        try:
            green, last = run_suite(repo)
        finally:
            with open(path, "w") as f:
                f.write(backup)
        (missed if green else caught).append((label, last[0]))
        print(f"  {'MISSED  ' if green else 'caught  '} {label}")

    print(f"\ncaught {len(caught)}/{len(caught) + len(missed)} applied mutations")
    if skipped:
        print(f"\n{len(skipped)} could not be applied (the code moved — not necessarily a hole):")
        for label, why in skipped:
            print(f"  - {label}: {why}")
    if missed:
        print("\nHOLES — these defects would ship undetected:")
        for label, _ in missed:
            print(f"  - {label}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
