"""Cost-measured literature pipeline: parse -> per-document readers -> synthesis -> verification.

Every default here is measured; the evidence is in docs/benchmarks/ in this plugin.
  read    one slim cheap-tier agent per document, run in parallel. Measured 31% cheaper than two
          documents per call, because a batched call re-reads earlier ones on every turn. Notes are
          cached by content hash, so a document is never read twice.
  synth   digest (mid tier, cheap, for the record) or plan (strong tier, for development proposals).
          Both get a git file listing so they cannot invent paths, and are told to grep before
          naming a file or quoting a number.
  verify  a cheap grep-only pass over every path and number in the output. Both synthesis failure
          modes measured were "named a thing it never read", so this stage is not optional.

Which model fills each role comes from scripts/lacuna_config.py (env: LACUNA_READER,
LACUNA_DIGESTER, LACUNA_PLANNER, LACUNA_VERIFIER); --model overrides it.

usage:
  lit_pipeline.py read   <pdf-or-md> [...] [--out DIR] [--no-books] [--model M] [--brief TEXT]
  lit_pipeline.py synth  --notes DIR --repo DIR --out FILE [--kind digest|plan] [--objective TEXT]
  lit_pipeline.py verify --target FILE --repo DIR [--notes DIR]
"""
import argparse, concurrent.futures as cf, hashlib, json, os, shlex, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import lacuna_config

DATA = lacuna_config.DATA
VENV = lacuna_config.path("python")
CACHE = lacuna_config.path("cache")
BOOKS_DIR = lacuna_config.path("books")
LEDGER = lacuna_config.path("ledger")

# Bump when the reader prompt or the note template changes: it is part of the cache key, so a
# changed prompt must miss the cache rather than silently serve notes written under old assumptions.
PROMPT_VERSION = "1"

EX_OK, EX_INPUT, EX_PARSE, EX_MODEL = 0, 2, 3, 4   # exit codes, so callers can branch

NOTE_TEMPLATE = """# <short title> (<first author> <year>)

## Claim
2-3 sentences.

## Method and evaluation
What was done, with key parameters, datasets, metrics, baselines.

## Key results
Bullets. Every number with its unit AND a location (section, table, figure).

## Concepts looked up
Per concept: the concept, the source (book + chapter), the one-line definition taken away.
"Not found in the supplied sources" is a valid and useful entry. Write "none" if you looked none up.

## Relevance to the project
How it bears on the project's problem. Mark anything that is your inference, not the paper's claim.

## Open problems
Numbered. Each: the gap, the evidence in the paper, what would resolve it. Include gaps the authors do not state.
"""

AGENTS = {
    "reader": {"description": "Reads one paper and writes a structured note",
               "tools": ["Read", "Grep", "Glob", "Write"],
               "prompt": ("You read one research paper (plain text from PDF; equations may be garbled) and write a "
                          "faithful structured note for a colleague who will not see the paper. Every number carries "
                          "a location. When the paper assumes a concept you cannot state precisely, look it up in the "
                          "supplied textbooks with Grep and read only the surrounding lines. Never invent a number, a "
                          "citation or a section number. Your final reply is only the path you wrote.")},
    "synth": {"description": "Turns notes plus a codebase into a digest or a development plan",
              "tools": ["Read", "Grep", "Glob", "Write"],
              "prompt": ("You are a research software architect. Before naming any file, class or function, grep for "
                         "it; before quoting any number, grep the note or status file it comes from. If you cannot "
                         "verify it, say so rather than asserting it. You never propose what the code already does. "
                         "Your final reply is only the path you wrote.")},
    "verifier": {"description": "Greps every claim in a document and flags the unverifiable",
                 "tools": ["Read", "Grep", "Glob"],
                 "prompt": ("You are a fact-checker. You verify claims mechanically with Grep and report only what you "
                            "could not confirm. You never rewrite the document and never judge its ideas.")},
}


def claude(agent, model, prompt, dirs, effort=None, cwd=None):
    effort = effort or "medium"
    env = {**os.environ, "FORCE_PROMPT_CACHING_5M": "1", "CLAUDE_CODE_EFFORT_LEVEL": effort}
    cmd = ["claude", "-p", "--agents", json.dumps(AGENTS), "--agent", agent, "--model", model, "--effort", effort,
           "--permission-mode", "acceptEdits", "--output-format", "json"]
    for d in dirs:
        cmd += ["--add-dir", d]
    t0 = time.time()
    # the prompt goes on stdin: --add-dir is variadic and would swallow a trailing positional prompt
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, input=prompt, cwd=cwd)
    try:
        res = json.loads(r.stdout)
    except ValueError:
        res = {"is_error": True, "result": (r.stdout + r.stderr)[-400:]}
    res["wall_s"] = round(time.time() - t0)
    return res


def ledger(row):
    p = LEDGER
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(p, "a") as f:
        f.write(json.dumps(row) + "\n")


def usd(res):
    return res.get("total_cost_usd") or 0


def after(stage, out_path):
    """Run the user's post-stage command, if configured, with the output path as $1.

    This is the extension seam: a project-specific layer can file the output wherever it keeps
    records without this plugin knowing anything about it.

    The stage has already succeeded and already cost money by the time this runs, so **nothing
    here may raise** — a missing or broken command is reported and the run continues.
    """
    cmd = (lacuna_config.CONFIG.get("after") or {}).get(stage)
    if not cmd:
        return
    argv = None
    try:
        argv = ([*shlex.split(cmd)] if isinstance(cmd, str) else [*cmd]) + [out_path]
        r = subprocess.run(argv, capture_output=True, text=True)
    except (OSError, ValueError) as e:
        what = repr(argv[0]) if argv else repr(cmd)
        print(f"  after_{stage}: could not run {what} — {e}. The output is unaffected: {out_path}")
        return
    if r.returncode == 0:
        print(f"  after_{stage}: ok")
    else:
        print(f"  after_{stage}: exit {r.returncode} — {(r.stderr or r.stdout).strip()[:200]}\n"
              f"    The output is unaffected: {out_path}")


def books_block():
    idx = sorted(f"{BOOKS_DIR}/{d}/toc.md" for d in os.listdir(BOOKS_DIR)
                 if os.path.exists(f"{BOOKS_DIR}/{d}/toc.md")) if os.path.isdir(BOOKS_DIR) else []
    if not idx:
        return ""
    return ("Textbooks you may consult (grep first, then read only the lines you need; never a whole chapter):\n"
            + "\n".join(f"- index: {p}" for p in idx)
            + "\nUse them to pin down any concept a paper assumes. Cite book and chapter. Say so if a concept is absent.")


SKIP_DIRS = {".git", ".hg", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".mypy_cache", ".pytest_cache", ".ipynb_checkpoints"}


def walk_files(repo):
    """Relative paths under repo, skipping version control, caches and build output."""
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        out += [os.path.relpath(os.path.join(root, f), repo) for f in files if not f.startswith(".")]
    return sorted(out)


STATUS_CANDIDATES = ["research/STATUS.md", "STATUS.md", "docs/STATUS.md", "NOTES.md"]


def find_status(repo, explicit=None):
    """A project's living status file, if it keeps one. Optional by design — most repos do not."""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for c in STATUS_CANDIDATES:
        if os.path.exists(os.path.join(repo, c)):
            return os.path.join(repo, c)
    return None


def need_venv():
    if not os.path.exists(VENV):
        print(f"ERROR: no PDF parser interpreter at {VENV}.\n"
              f"Run `{os.path.join(lacuna_config.ROOT, 'bin', 'lacuna')} doctor` to set one up, or point "
              f"LACUNA_PYTHON at a python that has pymupdf4llm installed.", file=sys.stderr)
        sys.exit(EX_PARSE)


def to_markdown(path, out_dir, equations=False, parser=None):
    """Parse a PDF to reference-stripped markdown; return the .md path (cached by content hash).

    The cache name carries the parser choice, because marker and pymupdf4llm produce different
    markdown from the same PDF and must not collide in the cache.
    """
    if path.endswith((".md", ".txt")):
        return path
    need_venv()
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]
    os.makedirs(f"{CACHE}/md", exist_ok=True)
    tag = f"_{parser}" if parser else ("_marker" if equations else "")
    md = f"{CACHE}/md/{os.path.basename(path)[:-4]}_{h}{tag}.md"
    if not os.path.exists(md):
        cmd = [VENV, "-I", f"{HERE}/prep_md.py", path, md]
        if equations:
            cmd.append("--equations")
        if parser:
            cmd += ["--parser", parser]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(md):
            print(f"ERROR: could not parse {path}.\n{(r.stderr or r.stdout).strip()[-800:]}", file=sys.stderr)
            sys.exit(EX_PARSE)
    return md


def cmd_read(a):
    """One slim reader per paper, in parallel. The agent writes into --out (its cwd); Python does the
    caching, so a paper is never read twice and no agent needs write access outside --out."""
    out_dir = os.path.abspath(a.out)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(f"{CACHE}/notes", exist_ok=True)
    reader_model = a.model or lacuna_config.model("reader")
    books = "" if a.no_books else books_block()
    jobs = []
    for src in a.papers:
        md = to_markdown(src, out_dir, equations=a.equations, parser=a.parser)
        body = open(md, "rb").read()
        content_h = hashlib.sha256(body).hexdigest()[:12]
        # The cache key covers everything that changes what the note says, not just the document.
        key = hashlib.sha256("\0".join(
            [content_h, PROMPT_VERSION, reader_model, a.brief, str(not a.no_books)]
        ).encode()).hexdigest()[:12]
        stem = os.path.basename(md)[:-3]
        if stem.endswith(f"_{content_h}"):
            stem = stem[: -(len(content_h) + 1)]
        jobs.append((md, f"{out_dir}/{stem}.md", f"{CACHE}/notes/{stem}_{key}.md"))

    def one(md, out, cached):
        if os.path.exists(cached) and os.path.getsize(cached) > 400:
            if not os.path.exists(out):
                shutil.copy(cached, out)
            return os.path.basename(out), "cached (free)", 0.0
        prompt = "\n\n".join(x for x in [
            a.brief, books,
            "Read the paper below in full and write its note to the given path, using exactly this template:",
            NOTE_TEMPLATE, f"Paper:\n- {md} -> {out}", "Keep the note under 1,100 words."] if x)
        res = claude("reader", reader_model, prompt, [os.path.dirname(md), out_dir, BOOKS_DIR],
                     effort=lacuna_config.effort("reader"), cwd=out_dir)
        ok = os.path.exists(out) and os.path.getsize(out) > 400
        ledger({"stage": "read", "model": reader_model, "paper": os.path.basename(md), "cost": usd(res),
                "wall_s": res["wall_s"], "books": not a.no_books, "wrote_note": ok,
                "is_error": res.get("is_error")})
        if ok:
            shutil.copy(out, cached)
            return os.path.basename(out), f"${usd(res):.4f} {res['wall_s']}s", usd(res)
        why = str(res.get("result"))[:120] if res.get("is_error") else "agent produced no note file"
        return os.path.basename(out), f"FAILED ${usd(res):.4f} — {why}", usd(res)

    t0, total, failed = time.time(), 0.0, 0
    with cf.ThreadPoolExecutor(min(8, len(jobs))) as ex:
        for name, msg, cost in ex.map(lambda j: one(*j), jobs):
            print(f"  {name:48s} {msg}")
            total += cost
            failed += msg.startswith("FAILED")
    print(f"{len(jobs) - failed}/{len(jobs)} notes written, ${total:.3f}, {round(time.time() - t0)}s wall -> {out_dir}")
    if failed:
        print(f"ERROR: {failed} document(s) produced no note. Nothing downstream should run on this "
              f"directory, and no partial note was cached.", file=sys.stderr)
        sys.exit(EX_MODEL)
    after("read", out_dir)


def cmd_synth(a):
    """digest: Sonnet consolidates the notes by theme. plan: Opus (or --model) proposes development work.

    A plan may read the notes directly, or a digest, or both (--digest with --notes). The digest-first
    chain is cheaper but adds a second chance to garble an attribution, so the plan is always told that
    the notes are authoritative over the digest.
    """
    notes = []
    if a.notes:
        if not os.path.isdir(a.notes):
            print(f"ERROR: notes directory {a.notes!r} does not exist. Run `read` first.", file=sys.stderr)
            sys.exit(EX_INPUT)
        notes = sorted(f"{a.notes}/{f}" for f in os.listdir(a.notes)
                       if f.endswith(".md") and os.path.getsize(f"{a.notes}/{f}") > 400)
    if not notes and not a.digest:
        print(f"ERROR: no usable notes in {a.notes!r} and no --digest. Run `read` first.", file=sys.stderr)
        sys.exit(EX_INPUT)
    out_dir = os.path.dirname(os.path.abspath(a.out)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tree = subprocess.run(["git", "-C", a.repo, "ls-files"], capture_output=True, text=True).stdout
    if not tree.strip():
        tree = "\n".join(walk_files(a.repo))   # not a git repo: list it ourselves
    status = find_status(a.repo, a.status)
    tree_path = f"{a.out}.filelist.txt"
    open(tree_path, "w").write(tree)
    role = "digester" if a.kind == "digest" else "planner"
    model = a.model or lacuna_config.model(role)

    if a.kind == "digest":
        task = ("Write a digest of the notes, organised by the objective below rather than by paper. For each "
                "theme: what the literature establishes (citing which note), where the sources disagree, and what "
                "it implies for the project. Keep every number with its unit and the note it came from. End with "
                "the three most important open problems across all the papers.")
    else:
        task = ("Write a development plan for the next iteration. Up to 8 proposals, most valuable first, each with: "
                "proposal, source (which note and which paper section), mechanism (naming real files/classes you "
                "have grepped), why the project needs it, test (metric + baseline), cost/risk, dependencies. Then a "
                "sequencing section, then a rejected section with one-line reasons.")

    sources = []
    if a.digest:
        sources.append(f"Digest of the literature (your starting point): {a.digest}")
    if notes:
        label = ("Underlying notes — authoritative where they disagree with the digest; consult them for any number "
                 "or attribution you rely on" if a.digest else "Literature notes (read all)")
        sources.append(f"{label}:\n" + "\n".join(f"- {n}" for n in notes))

    prompt = "\n\n".join([
        f"Objective: {a.objective}",
        *sources,
        f"Codebase: {a.repo}. The authoritative file list is {tree_path} — never name a path that is not in it.",
        ("Read the README and the main source directories" +
         (f", and the project status file {status} in full — anything it records as tried, rejected or a dead "
          "end must not be re-proposed" if status else "") + "."),
        task, f"Write the result to {a.out}. Under 2,000 words."])
    dirs = [a.repo, out_dir] + ([a.notes] if a.notes else []) + \
           ([os.path.dirname(os.path.abspath(a.digest))] if a.digest else [])
    res = claude("synth", model, prompt, dirs, effort=lacuna_config.effort(role), cwd=out_dir)
    ok = os.path.exists(a.out) and os.path.getsize(a.out) > 800
    ledger({"stage": "synth", "kind": a.kind, "model": model, "notes": len(notes),
            "from_digest": bool(a.digest), "cost": usd(res), "wall_s": res["wall_s"], "wrote": ok,
            "is_error": res.get("is_error")})
    print(f"{a.kind} ({model}): ${usd(res):.3f}, {res['wall_s']}s -> {a.out}")
    if not ok:
        print(f"ERROR: {model} wrote no output. {str(res.get('result'))[:400]}", file=sys.stderr)
        sys.exit(EX_MODEL)
    after("synth", a.out)


def cmd_verify(a):
    """Cheap grep-based fact check. The verifier reports in its final message; we save that."""
    prompt = "\n\n".join([
        f"Fact-check this document: {a.target}",
        f"Codebase: {a.repo}" + (f"\nSource notes: {a.notes}" if a.notes else ""),
        "For every claim of these three kinds, verify it mechanically and report ONLY failures:",
        "1. A named file, directory, class or function: grep the codebase. Report any that does not exist.",
        "2. A quoted number: grep the notes and research/STATUS.md for it. Report any you cannot find at its "
        "claimed source, and give the value you did find.",
        "3. An attribution to a paper: check it against the note for that paper. Report mismatches.",
        "Reply with a markdown list: one line per failure, as `claim -> what you found`. "
        "Reply exactly 'No unverifiable claims found.' if there are none. Do not comment on the ideas, "
        "do not summarise the document, and do not use any file-writing tool."])
    dirs = [a.repo, os.path.dirname(os.path.abspath(a.target))] + ([a.notes] if a.notes else [])
    vmodel = lacuna_config.model("verifier")
    res = claude("verifier", vmodel, prompt, dirs, effort=lacuna_config.effort("verifier"))
    ledger({"stage": "verify", "model": vmodel, "target": os.path.basename(a.target), "cost": usd(res),
            "wall_s": res["wall_s"], "is_error": res.get("is_error")})
    report = str(res.get("result") or "")
    out = f"{a.target}.unverified.md"
    try:
        open(out, "w").write(f"# Unverified claims in {os.path.basename(a.target)}\n\n{report}\n")
        where = out
    except OSError:
        where = "(not saved; printed below)"
    print(f"verify: ${usd(res):.4f}, {res['wall_s']}s -> {where}\n")
    print(report[:3000])
    if where != "(not saved; printed below)":
        after("verify", out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("papers", nargs="+")
    r.add_argument("--out", default="notes")
    r.add_argument("--model", help="override the reader model for this run")
    r.add_argument("--no-books", action="store_true")
    r.add_argument("--brief", default="")
    r.add_argument("--equations", action="store_true",
                   help="parse with marker for LaTeX fidelity (~34 expressions/document vs ~1); "
                        "slower and needs `lacuna doctor --install-equations`")
    r.add_argument("--parser", choices=["pymupdf4llm", "docling", "marker"],
                   help="force one parser instead of the validate-and-fall-back default")
    s = sub.add_parser("synth")
    s.add_argument("--notes")
    s.add_argument("--repo", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--kind", choices=["digest", "plan"], default="digest")
    s.add_argument("--objective", default="What should we build next?")
    s.add_argument("--digest", help="a digest to start from (plan kind); notes stay authoritative")
    s.add_argument("--model", help="override the default model for this stage")
    s.add_argument("--status", help="path to the project's living status file (auto-detected if omitted)")
    v = sub.add_parser("verify")
    v.add_argument("--target", required=True)
    v.add_argument("--repo", required=True)
    v.add_argument("--notes")
    a = ap.parse_args()
    {"read": cmd_read, "synth": cmd_synth, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    main()
