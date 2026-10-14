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
import argparse, concurrent.futures as cf, glob, hashlib, json, os, re, shlex, shutil, subprocess, sys, time

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

# Bump when prep_md's clean() or validate() changes: the parsed-markdown cache is keyed on this,
# so without it a change to parsing never reaches a document that has already been parsed.
PARSE_VERSION = "3"

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
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, input=prompt, cwd=cwd)
    except OSError as e:          # `claude` is not on PATH: no model ran and nothing was spent
        return {"is_error": True, "result": f"could not run claude ({e}); run `lacuna doctor`",
                "wall_s": round(time.time() - t0)}
    try:
        res = json.loads(r.stdout)
    except ValueError:
        res = None
    if not isinstance(res, dict):  # not JSON, or JSON that is not a reply object
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
    # Run through a shell so "~", "$VAR" and shell syntax work. The output path is APPENDED as the
    # final argument: a command with no arguments of its own therefore receives it as "$1".
    try:
        if isinstance(cmd, str):
            argv = ["sh", "-c", cmd + ' "$1"', "sh", out_path]
        elif isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
            argv = [*cmd, out_path]
        else:
            raise ValueError("not a command: give a string or a list of strings")
        r = subprocess.run(argv, capture_output=True, text=True)
    except (OSError, ValueError) as e:
        print(f"  after_{stage}: could not run {cmd!r} — {e}. The output is unaffected: {out_path}")
        return
    if r.returncode == 0:
        print(f"  after_{stage}: ok")
    else:
        print(f"  after_{stage}: exit {r.returncode} — {(r.stderr or r.stdout).strip()[:200]}\n"
              f"    The output is unaffected: {out_path}")


def book_indexes():
    """The toc.md of every split book, sorted."""
    return sorted(f"{BOOKS_DIR}/{d}/toc.md" for d in os.listdir(BOOKS_DIR)
                  if os.path.exists(f"{BOOKS_DIR}/{d}/toc.md")) if os.path.isdir(BOOKS_DIR) else []


def books_block(idx=None):
    idx = book_indexes() if idx is None else idx
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


TEXT_EXTS = (".md", ".markdown", ".txt", ".text", ".rst")


def to_markdown(path, out_dir, equations=False, parser=None):
    """See below. Raises SystemExit(2) when the document does not exist."""
    """Parse a PDF to reference-stripped markdown; return the .md path (cached by content hash).

    The cache name carries the parser choice, because marker and pymupdf4llm produce different
    markdown from the same PDF and must not collide in the cache.
    """
    if not os.path.isfile(path):
        print(f"ERROR: no such document: {path}", file=sys.stderr)
        sys.exit(EX_INPUT)
    if path.lower().endswith(TEXT_EXTS):
        return path
    need_venv()
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]
    os.makedirs(f"{CACHE}/md", exist_ok=True)
    tag = f"_{parser}" if parser else ("_marker" if equations else "")
    md = f"{CACHE}/md/{os.path.basename(path)[:-4]}_{h}_p{PARSE_VERSION}{tag}.md"
    if not os.path.exists(md) or os.path.getsize(md) == 0:   # 0 bytes: an interrupted parse
        cmd = [VENV, "-I", f"{HERE}/prep_md.py", path, md]
        if equations:
            cmd.append("--equations")
        if parser:
            cmd += ["--parser", parser]
        r = subprocess.run(cmd, capture_output=True, text=True)
        # prep_md reports a fallback on stderr; pass it on so a slow parse is not a mystery.
        for line in (r.stderr or "").splitlines():
            if "falling back" in line or "trying" in line or "failed validation" in line:
                print(f"  {line.strip()}", file=sys.stderr)
        if r.returncode != 0 or not os.path.exists(md):
            print(f"ERROR: could not parse {path}.\n{(r.stderr or r.stdout).strip()[-800:]}", file=sys.stderr)
            sys.exit(EX_PARSE)
    return md


def cmd_read(a):
    """One slim reader per paper, in parallel. The agent writes into --out (its cwd); Python does the
    caching, so a paper is never read twice and no agent needs write access outside --out."""
    out_dir = os.path.abspath(a.out)
    if os.path.isfile(out_dir):
        print(f"ERROR: --out {a.out!r} is a file; it must be a directory for notes.", file=sys.stderr)
        sys.exit(EX_INPUT)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(f"{CACHE}/notes", exist_ok=True)
    # Each document's note is <out>/<its name>.md. Settle every name before anything is parsed or
    # paid for: two documents with one name once shared a note path, and the parallel readers then
    # cached one paper's note under the other's key, permanently.
    papers, by_note = [], {}
    for src in map(os.path.abspath, a.papers):
        stem = os.path.splitext(os.path.basename(src))[0]
        note = f"{out_dir}/{stem}.md"
        other = by_note.get(note.casefold())          # casefold: macOS treats Paper.md as paper.md
        if other and os.path.realpath(other) == os.path.realpath(src):
            continue                                  # the same document twice: read it once
        if other:
            print(f"ERROR: {other} and {src} would both be noted as {note}. Rename one, or read "
                  f"them into separate --out directories. Nothing was sent to a model.", file=sys.stderr)
            sys.exit(EX_INPUT)
        # A note may only replace a note this tool wrote. Anything else at that path is the user's:
        # a hand-edited note, an unrelated file in a vault, or the document itself.
        if os.path.lexists(note) and not lacuna_wrote(note, stem):
            why = ("is inside --out, so its note would replace the document itself"
                   if os.path.realpath(note) == os.path.realpath(src) else
                   f"would overwrite {note}, which Lacuna did not write (or which has been edited since)")
            print(f"ERROR: {src} {why}. Move it, or choose another --out. Nothing was sent to a model.",
                  file=sys.stderr)
            sys.exit(EX_INPUT)
        by_note[note.casefold()] = src
        papers.append((src, stem, note))
    reader_model = a.model or lacuna_config.model("reader")
    idx = [] if a.no_books else book_indexes()
    books = books_block(idx)
    # The cache key must cover every input that changes what the note says. A boolean "books on"
    # is not enough: adding or removing a book changed nothing, so the old note came back as
    # "cached (free)" still citing a book that was no longer there. Nor is the list of books:
    # a book re-split into new chapters is a different book, so the indexes themselves count.
    indexes = "".join(open(t, "rb").read().decode(errors="ignore") for t in idx)
    books_fp = hashlib.sha256((books + indexes).encode()).hexdigest()[:8]
    reader_effort = lacuna_config.effort("reader")
    jobs = []
    for src, stem, note in papers:
        md = to_markdown(src, out_dir, equations=a.equations, parser=a.parser)
        body = open(md, "rb").read()
        content_h = hashlib.sha256(body).hexdigest()[:12]
        # The cache key covers everything that changes what the note says, not just the document.
        key = hashlib.sha256("\0".join(
            [content_h, PROMPT_VERSION, reader_model, reader_effort, a.brief, books_fp]
        ).encode()).hexdigest()[:12]
        jobs.append((md, note, f"{CACHE}/notes/{stem}_{key}.md"))

    def one(md, out, cached):
        if os.path.exists(cached) and os.path.getsize(cached) > 400:
            shutil.copy(cached, out)      # always refresh: --out may hold another run's note
            return os.path.basename(out), "cached (free)", 0.0
        # Remove any earlier note first, so "the file exists" can only mean this call wrote it.
        # Otherwise a refusal on a re-run reports success and caches the stale note under a new key.
        # (Only ever a note this tool wrote: anything else at this path stopped the run above.)
        if os.path.exists(out):
            os.remove(out)
        prompt = "\n\n".join(x for x in [
            a.brief, books,
            "Read the paper below in full and write its note to the given path, using exactly this template:",
            NOTE_TEMPLATE, f"Paper:\n- {md} -> {out}", "Keep the note under 1,100 words."] if x)
        res = claude("reader", reader_model, prompt, [os.path.dirname(md), out_dir, BOOKS_DIR],
                     effort=lacuna_config.effort("reader"), cwd=out_dir)
        ok = usable_output(out, 400, "note", must_contain=("## Claim",))
        # source_tokens makes the calibration recipe in docs/extending.md actually runnable
        # effort and source_tokens are both required by the recalibration recipe in
        # docs/extending.md, which is per model x effort.
        ledger({"stage": "read", "model": reader_model, "effort": reader_effort,
                "paper": os.path.basename(md), "cost": usd(res), "wall_s": res["wall_s"],
                "books": bool(idx), "wrote_note": ok,
                "source_tokens": round(os.path.getsize(md) / 2.9), "is_error": res.get("is_error")})
        if ok:
            shutil.copy(out, cached)
            return os.path.basename(out), f"${usd(res):.4f} {res['wall_s']}s", usd(res)
        why = (str(res.get("result"))[:120] if res.get("is_error") else
               "the file it wrote is not a note (see above)" if os.path.exists(out) else "agent produced no note file")
        if os.path.exists(out):
            os.remove(out)      # an apology left in --out would be read as a note by the next synth
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


# Phrases a model writes when it could not do the job. A file of apologies passes any size check,
# which is how a blocked run came to report success and fire the user's after.* hook.
REFUSAL_MARKERS = (
    "not completed", "cannot complete", "could not complete", "unable to complete",
    "i can't help", "i cannot help", "i'm unable to", "i am unable to", "no files were found",
    "unable to read", "could not read any", "i don't have access",
    "could not check any", "cannot check any", "unable to check any", "could not verify any",
)


# A model writes "I could not read any…", not "could not read any…", and a curly apostrophe is
# what most of them actually emit. Normalise both away before matching, or the phrase list only
# catches the one phrasing someone happened to paste into it.
_LEAD = re.compile(r"^(?:unfortunately|sorry|apologies|note)\b[,:\s]*|^(?:i|we)\s+(?:am\s+|'m\s+)?")


def _norm(text):
    return text.lower().replace("\u2019", "'").replace("\u2018", "'")


def _opens_with_apology(low):
    for _ in range(3):                       # "Unfortunately, I could not…"
        stripped = _LEAD.sub("", low, count=1)
        hit = next((m for m in REFUSAL_MARKERS if low.startswith(m) or stripped.startswith(m)), None)
        if hit:
            return hit
        if stripped == low:
            return None
        low = stripped
    return None


def refusal(head):
    """The apology in the opening of a document, or None.

    Position, not presence. A model that could not do the job *opens a block* with the apology:
    "Not completed. I could not read any of the notes." A correct document contains the same words
    inside a sentence: "37 had not completed follow-up", "cannot complete in polynomial time".

    So each block in the opening is tested at its START, after stripping markdown furniture. That
    catches an apology under a heading or as a list item — the earlier version only looked before
    the first heading, which let `## Status` + an apology be filed as a plan — without discarding a
    finding that merely mentions the words.
    """
    for block in re.split(r"\n\s*\n", head[:1500])[:6]:
        stripped = re.sub(r"^(?:[#>\s]*|\s*(?:[-*+]|\d+[.)])\s*)+", "", block).strip()
        if not stripped:
            continue
        low = _norm(stripped)
        hit = _opens_with_apology(low)
        if hit:
            return hit
        # The apology may follow one short lead-in sentence in the same block.
        first_sentence = re.split(r"(?<=[.!?])\s", low, 1)[0]
        if len(first_sentence) < 90:
            hit = _opens_with_apology(low[len(first_sentence):].lstrip())
            if hit:
                return hit
    # A status marker in capitals, as a blocked agent writes it — but only standing alone, not
    # inside prose such as "- JobState.BLOCKED -> not found".
    if re.search(r"(?m)^[#>\s*-]*\**BLOCKED\b", head[:600]):
        return "BLOCKED"
    return None


def lacuna_wrote(path, stem):
    """True when `path` is byte-for-byte a note in the cache: one this tool wrote, and may replace."""
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    with open(path, "rb") as f:
        body = f.read()
    for c in glob.glob(f"{glob.escape(CACHE)}/notes/{glob.escape(stem)}_*.md"):
        with open(c, "rb") as f:
            if f.read() == body:
                return True
    return False


def usable_output(path, min_bytes, what, must_contain=()):
    """True when a stage produced real output, not a short file or an explanation of failure.

    Size alone is not evidence: a model that cannot do the job writes a fluent paragraph saying so.
    Structure is the strongest evidence: a note that lacks the template's headings is not a note,
    whatever it says. After that, see refusal().
    """
    if not os.path.exists(path) or os.path.getsize(path) < min_bytes:
        return False
    with open(path, errors="ignore") as f:
        head = f.read(1200)
    missing = next((m for m in must_contain if m not in head), None)
    if missing:
        print(f"  ! the {what} has no {missing!r} section — treating it as a failed run, not output", file=sys.stderr)
        return False
    hit = refusal(head)
    if hit:
        print(f"  ! the {what} opens with {hit!r} — treating it as a failed run, not output",
              file=sys.stderr)
        return False
    return True


def need_dir(p, what):
    if not p or not os.path.isdir(p):
        print(f"ERROR: {what} {p!r} is not a directory. Nothing was sent to a model.", file=sys.stderr)
        sys.exit(EX_INPUT)


def need_file(p, what):
    if not p or not os.path.isfile(p):
        print(f"ERROR: {what} {p!r} does not exist. Nothing was sent to a model.", file=sys.stderr)
        sys.exit(EX_INPUT)


def cmd_synth(a):
    """digest: Sonnet consolidates the notes by theme. plan: Opus (or --model) proposes development work.

    A plan may read the notes directly, or a digest, or both (--digest with --notes). The digest-first
    chain is cheaper but adds a second chance to garble an attribution, so the plan is always told that
    the notes are authoritative over the digest.
    """
    need_dir(a.repo, "--repo")
    if a.digest:
        need_file(a.digest, "--digest")
    if a.status:
        need_file(a.status, "--status")    # silently ignored, a dead end would be proposed again
    # The agent runs with cwd set to the output directory, so relative inputs would resolve
    # against the wrong place and it would silently read nothing.
    if os.path.isdir(a.out):
        print(f"ERROR: --out {a.out!r} is a directory; give the path of the file to write.",
              file=sys.stderr)
        sys.exit(EX_INPUT)
    a.repo = os.path.abspath(a.repo)
    a.out = os.path.abspath(a.out)
    if a.notes:
        a.notes = os.path.abspath(a.notes)
    if a.digest:
        a.digest = os.path.abspath(a.digest)
    if a.status:
        a.status = os.path.abspath(a.status)
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
    try:
        tree = subprocess.run(["git", "-C", a.repo, "ls-files"], capture_output=True, text=True).stdout
    except OSError:                            # no git on this machine
        tree = ""
    if not tree.strip():
        tree = "\n".join(walk_files(a.repo))   # not a git repo: list it ourselves
    status = find_status(a.repo, a.status)
    tree_path = f"{a.out}.filelist.txt"
    open(tree_path, "w").write(tree)
    # The agent writes beside --out, never over it. Judged at --out, an earlier run's plan passed for
    # this run's when the model refused; and an apology written there was taken as input by the next
    # command. --out is replaced only by output that passed the content check.
    part = f"{a.out}.partial"
    if os.path.exists(part):
        os.remove(part)      # left by an interrupted run; never this run's output
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
        task, f"Write the result to {part}. Under 2,000 words."])
    dirs = [a.repo, out_dir] + ([a.notes] if a.notes else []) + \
           ([os.path.dirname(os.path.abspath(a.digest))] if a.digest else [])
    res = claude("synth", model, prompt, dirs, effort=lacuna_config.effort(role), cwd=out_dir)
    ok = usable_output(part, 800, a.kind)
    ledger({"stage": "synth", "kind": a.kind, "model": model, "effort": lacuna_config.effort(role),
            "notes": len(notes),
            "from_digest": bool(a.digest), "cost": usd(res), "wall_s": res["wall_s"], "wrote": ok,
            "is_error": res.get("is_error")})
    print(f"{a.kind} ({model}): ${usd(res):.3f}, {res['wall_s']}s -> {a.out}")
    if not ok:
        for f in (part, tree_path):
            if os.path.exists(f):
                os.remove(f)
        kept = (f"{a.out} was left as it was, and holds an earlier run's output, not this one's."
                if os.path.exists(a.out) else "Nothing was written.")
        print(f"ERROR: {model} wrote no usable output. {kept}\n{str(res.get('result'))[:400]}", file=sys.stderr)
        sys.exit(EX_MODEL)
    os.replace(part, a.out)
    after("synth", a.out)


def cmd_verify(a):
    """Cheap grep-based fact check. The verifier reports in its final message; we save that."""
    need_file(a.target, "--target")
    need_dir(a.repo, "--repo")
    if a.notes:
        need_dir(a.notes, "--notes")
    # The prompt is all the verifier knows: a relative path there checked nothing and exited 0.
    a.target, a.repo = os.path.abspath(a.target), os.path.abspath(a.repo)
    if a.notes:
        a.notes = os.path.abspath(a.notes)
    out = f"{a.target}.unverified.md"
    with open(a.target, "rb") as f:
        checked = f"sha256 {hashlib.sha256(f.read()).hexdigest()[:12]}"
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
    vmodel = a.model or lacuna_config.model("verifier")
    res = claude("verifier", vmodel, prompt, dirs, effort=lacuna_config.effort("verifier"))
    ledger({"stage": "verify", "model": vmodel, "effort": lacuna_config.effort("verifier"),
            "target": os.path.basename(a.target), "cost": usd(res),
            "wall_s": res["wall_s"], "is_error": res.get("is_error")})
    report = str(res.get("result") or "")

    def failed(why):
        # An earlier report may stay only while the document is still the version it checked.
        stale = os.path.exists(out) and checked not in open(out, errors="ignore").read(400)
        if stale:
            os.remove(out)
        print(f"ERROR: the verifier ({vmodel}) {why}. Nothing was written, and "
              f"{os.path.basename(a.target)} has NOT been fact-checked."
              + (" The earlier report described another version of it and was removed." if stale else "")
              + f"\n{report[:400]}", file=sys.stderr)
        sys.exit(EX_MODEL)

    if res.get("is_error") or not report.strip():
        failed("returned no usable report")
    hit = refusal(report)     # a reply that checked nothing arrives with is_error: false
    if hit:
        failed(f"replied with {hit!r} rather than a report")
    try:
        open(out, "w").write(f"# Unverified claims in {os.path.basename(a.target)}\n\n"
                             f"_Checked the version with {checked}._\n\n{report}\n")
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
    v.add_argument("--model", help="override the verifier model for this run")
    a = ap.parse_args()
    {"read": cmd_read, "synth": cmd_synth, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    main()
