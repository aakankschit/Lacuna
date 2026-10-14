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
  lit_pipeline.py read   <pdf-or-md> [...] [--out DIR] [--no-books] [--model M] [--brief TEXT] [--overwrite]
  lit_pipeline.py synth  --notes DIR --repo DIR --out FILE [--kind digest|plan] [--objective TEXT] [--overwrite]
  lit_pipeline.py verify --target FILE --repo DIR [--notes DIR]
"""
import argparse, concurrent.futures as cf, hashlib, json, os, re, shlex, shutil, subprocess, sys, threading, time
import unicodedata
try:
    import fcntl                       # POSIX; elsewhere the record is guarded within one process only
except ImportError:
    fcntl = None

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
PROMPT_VERSION = "2"

# Bump when prep_md's clean() or validate() changes: the parsed-markdown cache is keyed on this,
# so without it a change to parsing never reaches a document that has already been parsed.
PARSE_VERSION = "4"

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
    """Run the user's post-stage command, if configured, with the output path as its final argument.

    This is the extension seam: a project-specific layer can file the output wherever it keeps
    records without this plugin knowing anything about it.

    The stage has already succeeded and already cost money by the time this runs, so **nothing
    here may raise** — a missing or broken command is reported and the run continues.
    """
    cmd = (lacuna_config.CONFIG.get("after") or {}).get(stage)
    if not cmd:
        return
    # Run through a shell so "~", "$VAR" and shell syntax work. The output path is APPENDED as the
    # final argument (it is the command's "$1" only when it has no arguments of its own; the "$1"
    # in the sh -c string below is this wrapper's).
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
        # A guessed file that is empty, binary or unreadable is not a status file: skip it rather
        # than refuse a run over a file the user never named.
        if os.path.isfile(os.path.join(repo, c)) and not not_content(os.path.join(repo, c)):
            return os.path.join(repo, c)
    return None


def repo_tree(repo):
    """The file list an agent is given for a codebase: git's, else our own walk. Empty when the
    repository has no files, or none we are allowed to list."""
    try:
        tree = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True).stdout
    except OSError:                            # no git on this machine
        tree = ""
    return tree if tree.strip() else "\n".join(walk_files(repo))   # not a git repo: list it ourselves


# What a file must be to count as content, whichever subcommand reads it. Every input that is not
# content was once sent to a paid call and filed as a result (an empty document, a binary .txt, an
# unreadable --status): the one rule is applied to every input file before any model is called.
BINARY_RATIO = 0.30       # of the first 64 KB; real text with accents, CJK or symbols is far below it


def not_content(path, min_chars=0):
    """Why the file at `path` cannot be content (a sentence), or None when it can.

    Conservative on purpose: only a NUL byte, or a head that is mostly control characters and
    undecodable bytes, is called binary, so text in another script or encoding still passes."""
    try:
        with open(path, "rb") as f:
            raw = f.read(65536)
    except OSError as e:
        return f"cannot be read ({e.strerror or e})"
    if b"\0" in raw:
        return "is a binary file, not text"
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return "is empty" if not raw else "holds only whitespace"
    odd = sum(1 for c in text if c == "\ufffd" or (unicodedata.category(c) == "Cc" and c not in "\t\n\r\f\v"))
    if odd > BINARY_RATIO * len(text):
        return "is a binary file, not text"
    if len(text.strip()) < min_chars:
        return f"holds only {len(text.strip())} characters of text (under {min_chars})"
    return None


def refuse_inputs(problems):
    """Exit 2, naming every input that is not content and why. Nothing has been sent to a model."""
    if problems:
        print("ERROR: these inputs cannot be used:\n" + "\n".join(f"  - {p}" for p in problems)
              + "\nFix or remove them. Nothing was sent to a model.", file=sys.stderr)
        sys.exit(EX_INPUT)


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
    # prep_md.py is standard-library only; it needs the default parser's venv to run pymupdf4llm and
    # for the page and character counts that catch a truncated parse. The marker and docling parsers
    # live in their own venvs, so `--equations` alone must not demand the default one.
    own_venv = (parser or ("marker" if equations else "pymupdf4llm")) != "pymupdf4llm"
    if not own_venv or os.path.exists(VENV):
        need_venv()
        py = VENV
    else:
        py = sys.executable
        print(f"  ! no default parser venv at {VENV}: this parse is not checked against the PDF's own "
              f"text for truncation. `lacuna doctor --install-pdf` adds the check.", file=sys.stderr)
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]
    os.makedirs(f"{CACHE}/md", exist_ok=True)
    tag = f"_{parser}" if parser else ("_marker" if equations else "")
    md = f"{CACHE}/md/{os.path.basename(path)[:-4]}_{h}_p{PARSE_VERSION}{tag}.md"
    if not os.path.exists(md) or os.path.getsize(md) == 0:   # 0 bytes: an interrupted parse
        cmd = [py, "-I", f"{HERE}/prep_md.py", path, md]
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
    # Every document must be content before anything is parsed, reserved or paid for, and every bad
    # one is named: an empty .md, a whitespace-only or binary .txt, an unreadable file.
    bad = []
    for p in a.papers:
        if not os.path.isfile(p):
            bad.append(f"{p} does not exist" if not os.path.lexists(p) else f"{p} is not a file")
        elif p.lower().endswith(TEXT_EXTS):
            why = not_content(p)
            if why:
                bad.append(f"{p} {why}")
        else:
            try:
                open(p, "rb").close()
            except OSError as e:
                bad.append(f"{p} cannot be read ({e.strerror})")
    refuse_inputs(bad)
    out_dir = os.path.abspath(a.out)
    if os.path.isfile(out_dir):
        print(f"ERROR: --out {a.out!r} is a file; it must be a directory for notes.", file=sys.stderr)
        sys.exit(EX_INPUT)
    os.makedirs(out_dir, exist_ok=True)
    need_writable(out_dir, "--out")
    os.makedirs(f"{CACHE}/notes", exist_ok=True)
    # Each document's note is <out>/<its name>.md. Settle every name before anything is parsed or
    # paid for: two documents with one name once shared a note path, and the parallel readers then
    # cached one paper's note under the other's key, permanently.
    papers, by_note, blocked = [], {}, {}
    for src in map(os.path.abspath, a.papers):
        stem = os.path.splitext(os.path.basename(src))[0]
        note = f"{out_dir}/{stem}.md"
        other = by_note.get(name_key(note))
        if other and os.path.realpath(other) == os.path.realpath(src):
            continue                                  # the same document twice: read it once
        if other:
            print(f"ERROR: {other} and {src} would both be noted as {note}. Rename one, or read "
                  f"them into separate --out directories. Nothing was sent to a model.", file=sys.stderr)
            sys.exit(EX_INPUT)
        if os.path.lexists(note) and same_file(note, src):
            print(f"ERROR: {src} is inside --out, so its note would replace the document itself. Move it, "
                  f"or choose another --out. Nothing was sent to a model.", file=sys.stderr)
            sys.exit(EX_INPUT)
        if os.path.isdir(note) and not os.path.islink(note):
            print(f"ERROR: {note} is a directory, so {src} has nowhere to put its note. Nothing was sent "
                  f"to a model.", file=sys.stderr)
            sys.exit(EX_INPUT)
        # A note may only replace a note this tool wrote for this document and nobody has edited
        # since. Anything else at that path is someone's: a hand-edited note, a file in a vault, or
        # the note for another paper that happens to share this one's file name.
        why = not_ours(note, src)
        if why:
            blocked[note] = why
        # Reserve only a note we may actually write. Reserving one we are about to refuse
        # overwrote the rightful owner's record with this run's claim; the document still goes
        # through, because an unrecorded note may yet be adopted as this document's cached note.
        taken = reserve(note, src) if a.overwrite or not why else None
        if taken:
            print(f"ERROR: another Lacuna run is reading {taken} into {note} right now. Wait for it, "
                  f"or use a different --out. Nothing was sent to a model.", file=sys.stderr)
            sys.exit(EX_INPUT)
        by_note[name_key(note)] = src
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
        cached = f"{CACHE}/notes/{stem}_{key}.md"
        # A note from before ownership was recorded (0.2.0), or from a run killed between writing
        # it and recording it, is still provably this document's when it is exactly this document's
        # cached note.
        if blocked.get(note, ("",))[0] == UNRECORDED and same_bytes(note, cached):
            del blocked[note]
        jobs.append((src, md, note, cached))
    if blocked and not a.overwrite:
        print(f"ERROR: {len(blocked)} file(s) in {out_dir} would be replaced, and Lacuna cannot show they "
              f"are notes it wrote for these documents:", file=sys.stderr)
        for _, why in blocked.values():
            print(f"  - {why}", file=sys.stderr)
        print("Nothing was sent to a model. To keep a file, move it or read into another --out. To read a "
              "different document with the same file name, rename it or use another --out. To replace "
              "these files anyway, run the same command with --overwrite.", file=sys.stderr)
        sys.exit(EX_INPUT)

    def one(src, md, out, cached):
        if os.path.islink(out):
            os.remove(out)                # only with --overwrite; never write through a link
        # A hit is judged by today's content check, not by the one that cached it: a note cached before
        # the check learned a shape (an apology, a status marker) was otherwise served free forever.
        fresh = os.path.exists(cached) and usable_output(cached, 400, f"cached note for {os.path.basename(out)}",
                                                         ("## Claim",), then="removed from the cache; reading the "
                                                         "document again (paid)")
        if os.path.exists(cached) and not fresh:
            try:
                os.remove(cached)
            except OSError:
                pass
        if fresh:
            shutil.copy(cached, out)      # always refresh: --out may hold another run's note
            record(out, src)
            written.append((out, src))
            return os.path.basename(out), "cached (free)", 0.0
        # Remove any earlier note first, so "the file exists" can only mean this call wrote it.
        # Otherwise a refusal on a re-run reports success and caches the stale note under a new key.
        # (Only ever a note this tool wrote: anything else at this path stopped the run above.)
        if os.path.exists(out):
            os.remove(out)
            forget(out)
        prompt = "\n\n".join(x for x in [
            a.brief, books,
            "Read the paper below in full and write its note to the given path, using exactly this template:",
            NOTE_TEMPLATE, f"Paper:\n- {md} -> {out}", "Keep the note under 1,100 words.",
            INCOMPLETE_RULE.format(what="the paper", also=f" and do not write {out}")] if x)
        res = claude("reader", reader_model, prompt, [os.path.dirname(md), out_dir, BOOKS_DIR],
                     effort=lacuna_config.effort("reader"), cwd=out_dir)
        ok = usable_output(out, 400, "note", must_contain=("## Claim",))
        # The reader is told the INCOMPLETE rule too, and its reply is read: a haiku given symbol noise
        # filed a template-shaped "Not recoverable" note, and it was cached (VERDICT3 N5).
        reply = str(res.get("result") or "").strip()
        said = None if res.get("is_error") else reply_failure(reply)
        if said and ok:
            print(f"  ! the reader of {os.path.basename(md)} reported {said!r} — treating it as a failed run, "
                  f"not output", file=sys.stderr)
            ok = False
        # source_tokens makes the calibration recipe in docs/extending.md actually runnable
        # effort and source_tokens are both required by the recalibration recipe in
        # docs/extending.md, which is per model x effort.
        ledger({"stage": "read", "model": reader_model, "effort": reader_effort,
                "paper": os.path.basename(md), "cost": usd(res), "wall_s": res["wall_s"],
                "books": bool(idx), "wrote_note": ok,
                "source_tokens": round(os.path.getsize(md) / 2.9), "is_error": res.get("is_error")})
        if ok:
            _atomic_copy(out, cached)
            record(out, src)
            written.append((out, src))
            return os.path.basename(out), f"${usd(res):.4f} {res['wall_s']}s", usd(res)
        why = (str(res.get("result"))[:120] if res.get("is_error") else
               f"it replied {next((l for l in reply.splitlines() if said.lower() in _norm(l)), reply)[:160]!r}"
               if said else
               "the file it wrote is not a note (see above)" if os.path.exists(out) else "agent produced no note file")
        if os.path.exists(out):
            os.remove(out)      # an apology left in --out would be read as a note by the next synth
        return os.path.basename(out), f"FAILED ${usd(res):.4f} — {why}", usd(res)

    t0, total, failed, written = time.time(), 0.0, 0, []
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
    for out, src in written:
        rerecord(out, src)


# ---- did the model say it could not do the job?
#
# A model that cannot do the job writes a fluent explanation that passes any size check; an honest one
# writes "PARTIAL: literature notes unreadable" or replies "INCOMPLETE: …". Either way it says so
# FIRST. So the rule is positional: only the OPENING is judged — the first statement below any YAML
# frontmatter, headings and furniture (a callout tag, a quote mark, a list number, a code fence,
# "Status:"), and the headings above it. The same words anywhere else are findings: "37 had not
# completed follow-up", "- INCOMPLETE: data for site B", "the authors were unable to read 5 records".
# This check has swung between false positives and false negatives three times, each time by adding or
# removing phrases; it is a grammar now, and these are its three shapes:
#
#   1. A status word set off as the document's state: "(BLOCKED)", "PARTIAL:", "— Incomplete", "Not
#      completed." (the rule is above _STATUS_WORD). Inline, or followed by another word, it is part of
#      a name: PARTIAL LEAST SQUARES, "Arithmetic is INCOMPLETE", "Not completed follow-up in 37 of 412".
#   2. The agent saying it cannot do the job: an optional lead-in ("Unfortunately,", "I'm sorry, but"),
#      "I" or no subject, an inability ("cannot", "could not", "was unable to", "was not able to", "do
#      not have access") and a verb for the job or for getting at its inputs ("help", "complete",
#      "summarise", "open", "read", "access", "process"…). Not "we": in a note "we" are the paper's
#      authors ("We could not complete follow-up for 37 patients"). With no subject, only about a
#      definite thing ("Unable to read the supplied directory", "Could not open any of the notes"), as
#      an agent's inputs always are: "Unable to process long sequences, earlier models…" and "Cannot
#      complete in polynomial time" open findings. Not a numbered part of the input ("I could not read
#      Table 3"): that is a caveat about the part. And the verifier's own verbs ("check", "verify")
#      only with "any": "I could not verify the following claims:" opens a report.
#   3. "No files were found", what an agent given paths it cannot resolve replies.
#
# A line in the verify report's `claim -> what you found` form is a finding whatever its first words:
# "No files were found matching `src/ghost.py` -> …" is a report, "- PARTIAL: … -> …" a graded claim.
_LEAD_IN = re.compile(r"^(?:(?:unfortunately|regrettably|sadly|however|sorry|apologies|my apologies|note"
                      r"|i am (?:very |so |really )?sorry|i am afraid|i apologi[sz]e)\b[^\w]*(?:but\b[^\w]*)?)+")
_UNABLE = (r"(?:(?:am|was|were|have been|had been)\s+)?(?:unable|not able)\s+to"
           r"|cannot|can not|could not|did not manage to")
_JOB = (r"help|assist|complete|finish|fulfil|fulfill|comply|proceed|continue|summari[sz]e|write|produce|generate"
        r"|provide|perform|do|open|read|access|load|parse|process|view|retrieve|extract|decode")
_PART = r"\s+(?:the\s+)?(?!any\b|all\b|the\b|of\b)[a-z]+\.?\s*\(?\d"      # "Table 3", "pages 3-12"
_DEFINITE = r"\s+(?:any|all|the|this|these|those|that|it|them|your|either|both|anything)\b"
_DECLINE = re.compile(
    rf"(?:i\s+(?:{_UNABLE})\s+(?:{_JOB})\b|(?:{_UNABLE})\s+(?:{_JOB})(?={_DEFINITE}))(?!{_PART})"
    rf"|(?:i\s+)?(?:(?:{_UNABLE})\s+(?:check|verify|confirm|fact-check)\s+(?:any|a single)\b"
    r"|(?:do|did) not have access\b|(?:have|had) no access\b)"
    r"|no (?:files?|notes?|documents?) (?:were|was|could be) found\b"
    r"|not completed(?=\s*(?:[^\w\s]|$))")


def _norm(text):
    """Lower case, straight apostrophes and contractions spelled out, so the grammar has one form of each
    phrase: a curly apostrophe is what most models emit, and "couldn't" is as common as "could not"."""
    t = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"\bcan't\b", "cannot", t)
    t = re.sub(r"\bwon't\b", "will not", t)
    t = re.sub(r"n't\b", " not", t)
    return re.sub(r"\bi'm\b", "i am", t)


def _declines(text):
    """The declining phrase `text` (an opening, furniture stripped) starts with, or None. It may follow
    a lead-in ("Unfortunately,") or one short sentence ("Both notes are binary garbage.")."""
    low = _norm(text).strip()
    first = re.split(r"(?<=[.!?])\s", low, 1)[0]
    for s in (low, low[len(first):].lstrip() if len(first) < 90 else ""):
        m = _DECLINE.match(_LEAD_IN.sub("", s, count=1))
        if m:
            return m.group(0)
    return None


_FENCE = re.compile(r"\s*(?:```|~~~)")
_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s")
_YAML_KEY = re.compile(r"[a-z_][\w-]*:(?:\s|$)")


def _furniture_only(line):
    return _FENCE.match(line) or not re.search(r"\w", _FURNITURE.sub("", line, count=1))


def _lines(text):
    """`text` by line as a reader meets it: past any YAML frontmatter, and with each line that is only
    furniture blanked — a callout's `> [!WARNING]` on a line of its own, a code fence, a rule — so that
    what follows it is the opening, as it is on the page. Without this, the callout tag alone and the
    frontmatter's first key were each taken for the opening, and a `PARTIAL:` under them was filed."""
    lines = text.splitlines()[:80]
    top = next((i for i, l in enumerate(lines) if l.strip()), 0)
    if lines and lines[top].strip() == "---" and top + 1 < len(lines) and _YAML_KEY.match(lines[top + 1]):
        end = next((j for j in range(top + 2, len(lines)) if lines[j].strip() in ("---", "...")), None)
        if end is not None:
            lines = lines[end + 1:]
    return ["" if _furniture_only(l) else l for l in lines]


def refusal(head):
    """The declaration of failure that opens a document or a reply, or None (the rule is above).

    The opening statement runs to a blank line, a heading or the next list item: an apology wrapped
    over two lines is read whole, and a report's first finding is not part of the line above it."""
    units, statement = [], []
    for line in _lines(head):
        if not line.strip():
            if statement:
                break
        elif line.lstrip().startswith("#"):
            if statement:
                break
            units.append(line)
        elif statement and _ITEM.match(line):
            break
        else:
            statement.append(_FURNITURE.sub("", line, count=1))
    units.append(" ".join(statement))
    for unit in units:
        hit = None if "->" in unit else _declines(_FURNITURE.sub("", unit, count=1))
        if hit:
            return hit
    return declared_failure(head)


def reply_failure(reply):
    """What a model's reply says went wrong, or None.

    A reply is judged like a document, by its opening. And every agent is told to reply `INCOMPLETE:
    <reason>` when it could not read its sources (INCOMPLETE_RULE), so a line of a reply that opens with
    that word is the agent's own verdict wherever it stands: a real planner put it on line 3, under two
    lines of prose, when only the first line was read. The other markers count only at the opening."""
    hit = refusal(reply)
    if hit:
        return hit
    return next(("INCOMPLETE" for line in reply.splitlines() if declared_failure(line) == "INCOMPLETE"), None)


# A status word in the opening — BLOCKED, PARTIAL, INCOMPLETE or FAILED, in any case — declares the
# document's state, or is part of a name. Only the headings above the opening statement and that
# statement's first line are read (see above), and there the rule is one rule for both, because the
# line rule and the title rule each disagreed with the other on hyphens, brackets and FAILED:
#
#   It is SET OFF: at the start (after furniture and a `Status:` label), or after an opening bracket,
#   a dash, a colon, semicolon, comma, slash, an emphasis mark or an emoji: "(BLOCKED)", "— Blocked",
#   ": INCOMPLETE", "**BLOCKED**", "🚫 BLOCKED".
#   And it then ENDS, or gives a reason: punctuation ("BLOCKED.", "(PARTIAL, 2 of 5 readable)",
#   "BLOCKED - see below"), a word that introduces one ("by", "until", "pending"), a count ("PARTIAL
#   2/5 notes"), or the document's own name ("BLOCKED Development plan") — after any list of other
#   status words ("(PARTIAL / BLOCKED)").
#
# Otherwise it is part of a name: inline in running text ("Arithmetic is INCOMPLETE", "adopt PARTIAL
# pooling", "complete vs. INCOMPLETE"), in code ("`FAILED`", "`JobState.PARTIAL`"), joined by a hyphen
# ("INCOMPLETE-DATA", "BLOCKED-Gibbs"), or followed by another word ("PARTIAL LEAST SQUARES", "BLOCKED
# Gibbs", "Blocked items: none", "FAILED, PARTIAL and COMPLETE responses"). A value that is none
# ("Blocked: none.", "PARTIAL: 0", "Blocked — N/A") is a clean result, but "none of the notes could be
# read" is a reason. "Status of the sampler: PARTIAL" is the sampler's status, not the document's.
# A list item is one entry, a proposal or a finding: there only the status line itself counts, the
# word at the item's start and punctuated ("1. Incomplete: every note returned EACCES"), so "- **BLOCKED**
# until the CUDA build lands: port the scorer" is that proposal's precondition.
_STATUS_WORD = re.compile(r"(?i)\b(blocked|partial|incomplete|failed)\b")
_NOT_SET_OFF = "`'\".)]"          # code, a quotation, `JobState.PARTIAL` or "vs.", a closing bracket
_REASON = re.compile(r"(?i)(?:by|until|till|pending|because|due|on|as|since|awaiting|for|while|without|after)\b"
                     r"|\d|(?:development\s+)?(?:plan|digest|note|report|draft)\b")
_ALSO = re.compile(r"\s*(?:[/&]|vs\.?)\s*[*_]*[A-Za-z][\w-]*[*_]*|\s*(?:,|\band\b|\bor\b)\s*[*_]*[A-Z]{2,}\b[*_]*")
_NONE_VALUE = re.compile(r"(?i)\s*[:=\u2013\u2014-]?\s*[*_`(\[]*(?:none|nil|nothing|no|n/a|0)[*_`)\]]*(?:\s+items?)?"
                         r"\s*(?:$|[.;,!?)\]\u2013\u2014]|-\s)")
_FURNITURE = re.compile(r"^(?:\[![^\]]*\]|\d+[.)](?=\s)|[^\w])*(?:status\s*[:\u2013\u2014-](?:[^\w])*)?", re.I)


def _status_word(line, item=False):
    """The status word `line` sets off as the document's state, upper-cased, or None (rule above)."""
    if "->" in line:
        return None                     # `claim -> what you found`: a verify finding, graded or not
    text = _FURNITURE.sub("", line, count=1)
    for m in _STATUS_WORD.finditer(text):
        before = text[:m.start()].rstrip()
        if item and before:
            continue                    # on a list item, only the item's own status line counts
        last = before[-1:]
        if last and (last.isalnum() or last in _NOT_SET_OFF or last == "-" and not before[-2:-1].isspace()):
            continue                    # running text, code, a quotation, "vs.", a hyphenated word
        if re.search(r"(?i)\bstatus\s+of\b", before):
            continue                    # "Status of the sampler: PARTIAL"
        after = text[m.end():]
        if re.match(r"-\w|`", after):
            continue                    # INCOMPLETE-DATA, BLOCKED-Gibbs; `FAILED` in code
        after = after.lstrip("*_")
        also = _ALSO.match(after)
        while also and also.group(0):
            after = after[also.end():]
            also = _ALSO.match(after)
        if _NONE_VALUE.match(after):
            continue                    # "Blocked: none." is a clean result, not a failure
        rest = after.lstrip()
        if not rest or not rest[0].isalnum() or not item and _REASON.match(rest):
            return m.group(1).upper()
    return None

# An honest model that could not read its sources says so; it must say so where a program can see
# it, not in a fluent document that passes every check.
INCOMPLETE_RULE = ("If you could not read {what}, do not write a document explaining why{also}: reply with "
                   "one line, `INCOMPLETE: <what you could not read>`, and nothing else.")


def declared_failure(text):
    """The status word a document or reply opens with (BLOCKED, PARTIAL, INCOMPLETE, FAILED), or None.
    Frontmatter and lines that are only furniture are passed over (_lines)."""
    for line in _lines(text)[:40]:
        if not line.strip():
            continue
        heading = line.lstrip().startswith("#")
        word = _status_word(line, item=not heading and bool(_ITEM.match(line)))
        if word:
            return word
        if not heading:
            return None                       # the opening line was not a marker
    return None


# ---- ownership: which files in the user's directories Lacuna may replace
#
# Lacuna writes into directories that belong to the user — a notes folder, an Obsidian vault, a
# repo's lit/plan.md — so it replaces a file only when it can show that it wrote that file, for the
# document it is writing now, and that nobody has edited it since. The evidence is a record kept
# beside the files (OWNERS), not the note cache. Ownership was once proven by byte-equality with
# the cache, which asked "did Lacuna write this?" and never "for which document?": a note read on
# Monday from smith2023/main.pdf was silently replaced on Tuesday by jones2024/main.pdf's, and
# deleting the cache or moving to another machine refused every later run with a message saying
# Lacuna had not written notes that it had.
OWNERS = ".lacuna.json"
UNRECORDED = "unrecorded"
_owners_lock = threading.Lock()


def _atomic_copy(src, dst):
    """Copy so the destination is never seen half-written.

    A cache entry is read by a later run as proof the work is done. An interrupted plain copy left
    a truncated note that the next run served as a free hit — the same defect already fixed for the
    parse cache, in its sibling path.
    """
    tmp = f"{dst}.{os.getpid()}.part"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def name_key(path):
    """One key per file that the filesystem treats as one. macOS folds case (Paper.md is paper.md)
    and Unicode normalisation (café typed and café pasted from a PDF are one name)."""
    return unicodedata.normalize("NFC", os.path.basename(path)).casefold()


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def same_bytes(a, b):
    return os.path.isfile(a) and os.path.isfile(b) and _sha(a) == _sha(b)


def same_file(a, b):
    """One file under two names. macOS folds case and Unicode form, so Paper.MD inside --out IS the
    note path notes/Paper.md, though their realpaths differ; comparing realpaths let --overwrite
    delete the document."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def _owners(d):
    """The record of files Lacuna wrote in `d`: (entries, why it could not be read or None).
    Hand-edited or damaged, it must never raise; whatever is not a well-formed entry is ignored."""
    p = os.path.join(d, OWNERS)
    if not os.path.exists(p):
        return {}, None
    try:
        with open(p) as f:
            files = json.load(f).get("files")
        if not isinstance(files, dict):
            raise ValueError("no files table")
    except Exception as e:       # OSError, ValueError, a list at the top, RecursionError on deep nesting
        return {}, f"{OWNERS} could not be read ({type(e).__name__})"
    good = {}
    for k, v in files.items():
        # A completed entry carries the note's checksum. A reservation, written before the model
        # runs, cannot: the note does not exist yet. Dropping those silently let two concurrent
        # runs each believe the path was free.
        if isinstance(v, dict) and (isinstance(v.get("sha256"), str) or v.get("pending") is True):
            good[k] = {**v, "source": v["source"] if isinstance(v.get("source"), str) else None}
    return good, None


class _locked:
    """The record is read, changed and replaced by up to 8 reader threads and by any other Lacuna
    process writing to the same directory; without a lock between processes, two concurrent reads
    into one --out kept 9 of 16 entries, and the next run refused the other 7."""

    def __init__(self, d):
        self.d, self.fd = d, None

    def __enter__(self):
        _owners_lock.acquire()
        if fcntl:
            try:
                self.fd = os.open(self.d, os.O_RDONLY)
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            except OSError:
                self.fd = None

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)        # releases the lock
        _owners_lock.release()


def need_writable(d, what):
    if not os.access(d, os.W_OK):
        print(f"ERROR: {what} {d} is not writable. Nothing was sent to a model.", file=sys.stderr)
        sys.exit(EX_INPUT)


def _save_owners(d, files):
    tmp = os.path.join(d, f"{OWNERS}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w") as f:
        json.dump({"about": "Files Lacuna wrote in this directory, and what from, so that it only ever "
                            "replaces its own. Deleting this makes Lacuna ask (--overwrite) before "
                            "replacing any of them.", "files": files}, f, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(d, OWNERS))


def reserve(path, source):
    """Claim `path` for `source` before any model call, atomically across processes.

    Returns the source that already holds it, or None on success. The collision guard and the
    ownership check both ran before any lock, so two concurrent `read`s of same-named documents
    into one --out each cached whatever was at the note path when its own agent returned: one
    note silently replaced the other, and one paper's cache key could end up holding the other
    paper's note, served free from then on.
    """
    d = os.path.dirname(path)
    try:
        with _locked(d):
            files = _owners(d)[0]
            held = files.get(name_key(path))
            if held and held.get("source") and os.path.realpath(held["source"]) != os.path.realpath(source):
                if held.get("pending"):
                    return held["source"]          # another process is reading it right now
            entry = dict(held) if held and held.get("source") and \
                os.path.realpath(held["source"]) == os.path.realpath(source) else \
                {"name": os.path.basename(path), "source": source,
                 "written": time.strftime("%Y-%m-%d %H:%M")}
            entry["pending"] = True          # keep sha256 etc. so a re-read of the same document
            files[name_key(path)] = entry    # is not locked out of its own note
            _save_owners(d, files)
    except OSError:
        return None          # cannot reserve: fall through to the existing per-run guard
    return None


def record(path, source=None):
    """Record that Lacuna wrote `path`, as it is now, from the document `source`.

    The stage has already succeeded and been paid for, so this never raises: a record that cannot
    be written costs a question (--overwrite) on the next run, not this run's output."""
    d = os.path.dirname(path)
    try:
        entry = {"name": os.path.basename(path), "sha256": _sha(path), "written": time.strftime("%Y-%m-%d %H:%M")}
        if source:
            entry.update(source=source, source_sha256=_sha(source))
        with _locked(d):
            files = _owners(d)[0]
            files[name_key(path)] = entry
            _save_owners(d, files)
    except OSError as e:
        print(f"  ! could not record {path} as Lacuna's ({e}); the next run will ask before replacing it",
              file=sys.stderr)


def forget(path):
    d = os.path.dirname(path)
    try:
        with _locked(d):
            files = _owners(d)[0]
            if files.pop(name_key(path), None) is not None:
                _save_owners(d, files)
    except OSError:
        pass


def rerecord(path, source=None):
    """After an after.* hook: the hook is part of the user's pipeline, so what it leaves is still
    Lacuna's output. A hook that prepended frontmatter, as docs/extending.md suggests, otherwise made
    every later run refuse the note as edited. A hook that moved the file away takes it out of the
    record."""
    if os.path.isfile(path):
        record(path, source)
    else:
        forget(path)


def not_ours(path, source=None, namespaced=False):
    """None when Lacuna may replace `path`; otherwise (kind, why), `why` a sentence for the user.

    `source` is the document the new file is written from. When given, a file Lacuna wrote from a
    *different* document is not ours to replace either. A document is the same one when it is at
    the same path (and may have changed) or has the same bytes (and may have moved).
    `namespaced` is for names only Lacuna makes (plan.md.unverified.md): an unrecorded file there
    was written by an earlier Lacuna, so only an edit since it was recorded makes it the user's."""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path) or not os.path.isfile(path):
        return "other", f"{path} is a {'symbolic link' if os.path.islink(path) else 'directory'}, not a file Lacuna wrote."
    files, damaged = _owners(os.path.dirname(path))
    entry = files.get(name_key(path))
    if not isinstance(entry, dict):
        if namespaced:
            return None
        return UNRECORDED, (f"{path} has no record in {OWNERS} of being written by Lacuna"
                            + (f" ({damaged})" if damaged else "") + ". It may be your own file, one "
                            f"written before Lacuna kept that record (0.2.0 and earlier), or one left by an "
                            f"interrupted run.")
    if entry.get("sha256") != _sha(path):
        return "edited", (f"{path} was written by Lacuna on {entry.get('written', 'an earlier run')}, and has "
                          f"been edited since.")
    theirs = entry.get("source")
    if source and theirs and not (os.path.realpath(source) == os.path.realpath(theirs)
                                  or (os.path.isfile(source) and _sha(source) == entry.get("source_sha256"))):
        return "other", (f"{path} is the note for {theirs}; {source} is a different file with the same name "
                         f"(another document, or a changed copy of that one elsewhere), and reading it would "
                         f"replace that note.")
    return None


def usable_output(path, min_bytes, what, must_contain=(), then="treating it as a failed run, not output"):
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
        print(f"  ! the {what} has no {missing!r} section — {then}", file=sys.stderr)
        return False
    hit = refusal(head)
    if hit:
        print(f"  ! the {what} opens with {hit!r} — {then}", file=sys.stderr)
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
        refuse_inputs([f"--status {a.status} {why}" for why in [not_content(a.status)] if why])
    repo_files = repo_tree(a.repo)
    if not repo_files.strip():
        refuse_inputs([f"--repo {a.repo} has no files (or none Lacuna can list), so there is no codebase to read"])
    # The agent runs with cwd set to the output directory, so relative inputs would resolve
    # against the wrong place and it would silently read nothing.
    if os.path.isdir(a.out):
        print(f"ERROR: --out {a.out!r} is a directory; give the path of the file to write.",
              file=sys.stderr)
        sys.exit(EX_INPUT)
    a.repo = os.path.abspath(a.repo)
    a.out = os.path.abspath(a.out)
    # --out is often a file the user keeps (lit/plan.md in a repo, a page in a vault). A hand-edited
    # plan was once replaced without a word: the ownership rule covered read's notes only.
    why = not_ours(a.out)
    if why and not a.overwrite:
        print(f"ERROR: {why[1]} This run would replace it. Nothing was sent to a model. To keep it, move it "
              f"or choose another --out; to replace it anyway, run the same command with --overwrite.",
              file=sys.stderr)
        sys.exit(EX_INPUT)
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
        try:
            names = sorted(f for f in os.listdir(a.notes) if f.endswith(".md"))
        except OSError as e:
            refuse_inputs([f"notes directory {a.notes} cannot be listed ({e.strerror})"])
        bad = []
        for f in names:
            p = f"{a.notes}/{f}"
            try:
                if os.path.getsize(p) > 400:       # a stub is no note; skipped, as before
                    notes.append(p)
            except OSError as e:                   # a dangling link, a note in an unreadable place
                bad.append(f"{p} cannot be read ({'a symbolic link to nothing' if os.path.islink(p) else e.strerror})")
        refuse_inputs(bad)
    if not notes and not a.digest:
        print(f"ERROR: no usable notes in {a.notes!r} and no --digest. Run `read` first.", file=sys.stderr)
        sys.exit(EX_INPUT)
    # An input that says it holds nothing ("PARTIAL: literature notes unreadable") is refused before
    # the call, by the same test its output will face: a plan from it is paid for and empty.
    # One that cannot be read is refused too: the agent could not read it either, and a real
    # digester given unreadable notes was paid to file "PARTIAL: literature notes unreadable".
    # A digest shorter than a note is no digest ("hi"); a note that is binary is no note.
    checks = [(a.digest, not_content(a.digest, 400))] if a.digest else []
    refuse_inputs([f"{f} {why}" for f, why in checks + [(n, not_content(n)) for n in notes] if why])
    empty = []
    for f in ([a.digest] if a.digest else []) + notes:
        try:
            with open(f, errors="ignore") as fh:
                hit = refusal(fh.read(1200))
        except OSError as e:
            empty.append(f"{f} cannot be read ({e.strerror})")
            continue
        if hit:
            empty.append(f"{f} opens with {hit!r}: it says it holds no literature")
    if empty:
        print("ERROR: these inputs cannot be used as literature:\n" + "\n".join(f"  - {e}" for e in empty)
              + "\nFix their permissions, remove them, or run the stage that wrote them again. Nothing was "
                "sent to a model.",
              file=sys.stderr)
        sys.exit(EX_INPUT)
    out_dir = os.path.dirname(os.path.abspath(a.out)) or "."
    os.makedirs(out_dir, exist_ok=True)
    need_writable(out_dir, "the directory of --out")
    tree = repo_files
    status = find_status(a.repo, a.status)
    tree_path = f"{a.out}.filelist.txt"
    if os.path.islink(tree_path):
        os.remove(tree_path)                   # Lacuna's own scratch name: never write through a link
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
        task, f"Write the result to {part}. Under 2,000 words.",
        INCOMPLETE_RULE.format(what="the literature you were given (the notes or the digest)",
                               also=f" and do not write {part}")])
    dirs = [a.repo, out_dir] + ([a.notes] if a.notes else []) + \
           ([os.path.dirname(os.path.abspath(a.digest))] if a.digest else [])
    before = _sha(a.out) if os.path.isfile(a.out) else None
    res = claude("synth", model, prompt, dirs, effort=lacuna_config.effort(role), cwd=out_dir)
    ok = usable_output(part, 800, a.kind)
    # A model that could not read its sources may say so in its reply and still leave a document
    # behind; a real digester filed "PARTIAL: literature notes unreadable", exit 0. Its own word wins.
    said = reply_failure(str(res.get("result") or ""))
    if ok and said:
        print(f"  ! the model reported {said!r} — treating it as a failed run, not output", file=sys.stderr)
        ok = False
    ledger({"stage": "synth", "kind": a.kind, "model": model, "effort": lacuna_config.effort(role),
            "notes": len(notes),
            "from_digest": bool(a.digest), "cost": usd(res), "wall_s": res["wall_s"], "wrote": ok,
            "is_error": res.get("is_error")})
    print(f"{a.kind} ({model}): ${usd(res):.3f}, {res['wall_s']}s -> {a.out}")
    if not ok:
        for f in (part, tree_path):
            if os.path.exists(f):
                os.remove(f)
        if os.path.isfile(a.out) and _sha(a.out) != before:
            # --out changed during the run: the model wrote it rather than the .partial it was
            # given, or someone edited it meanwhile. Either way it is not an earlier run's plan,
            # and the next command must not read it as one; moved aside, never deleted.
            os.replace(a.out, f"{a.out}.rejected")
            forget(a.out)
            kept = (f"{a.out} changed during this run (the model wrote it directly, or it was edited), so it "
                    f"is not a checked plan. It was moved to {a.out}.rejected for you to inspect.")
        else:
            kept = (f"{a.out} was left as it was, and holds an earlier run's output, not this one's."
                    if os.path.exists(a.out) else "Nothing was written.")
        print(f"ERROR: {model} wrote no usable output. {kept}\n{str(res.get('result'))[:400]}", file=sys.stderr)
        sys.exit(EX_MODEL)
    if os.path.islink(a.out):
        os.remove(a.out)     # only with --overwrite; replacing the link, not writing through it
    os.replace(part, a.out)
    record(a.out)
    after("synth", a.out)
    rerecord(a.out)


def cmd_verify(a):
    """Cheap grep-based fact check. The verifier reports in its final message; we save that."""
    need_file(a.target, "--target")
    need_dir(a.repo, "--repo")
    if a.notes:
        need_dir(a.notes, "--notes")
        try:
            os.listdir(a.notes)
        except OSError as e:
            refuse_inputs([f"--notes {a.notes} cannot be listed ({e.strerror})"])
    refuse_inputs([f"--target {a.target} {why}" for why in [not_content(a.target)] if why])
    if not repo_tree(a.repo).strip():
        refuse_inputs([f"--repo {a.repo} has no files (or none Lacuna can list), so there is no codebase to check against"])
    # The prompt is all the verifier knows: a relative path there checked nothing and exited 0.
    a.target, a.repo = os.path.abspath(a.target), os.path.abspath(a.repo)
    if a.notes:
        a.notes = os.path.abspath(a.notes)
    out = f"{a.target}.unverified.md"
    why = not_ours(out, namespaced=True)       # a report annotated by hand is the user's
    if why and not a.overwrite:
        print(f"ERROR: {why[1]} This run would replace it. Nothing was sent to a model. To keep it, move it; "
              f"to replace it anyway, run the same command with --overwrite.", file=sys.stderr)
        sys.exit(EX_INPUT)
    status = find_status(a.repo)       # absolute: the agent runs in another directory
    with open(a.target, "rb") as f:
        checked = f"sha256 {hashlib.sha256(f.read()).hexdigest()[:12]}"
    prompt = "\n\n".join([
        f"Fact-check this document: {a.target}",
        f"Codebase: {a.repo}" + (f"\nSource notes: {a.notes}" if a.notes else ""),
        "For every claim of these three kinds, verify it mechanically and report ONLY failures:",
        "1. A named file, directory, class or function: grep the codebase. Report any that does not exist.",
        "2. A quoted number: grep the notes" + (f" and {status}" if status else "") + " for it. Report any you cannot find at its "
        "claimed source, and give the value you did find.",
        "3. An attribution to a paper: check it against the note for that paper. Report mismatches.",
        "Reply with a markdown list: one line per failure, as `claim -> what you found`. "
        "Reply exactly 'No unverifiable claims found.' if there are none. Do not comment on the ideas, "
        "do not summarise the document, and do not use any file-writing tool.",
        INCOMPLETE_RULE.format(what="the document or the codebase", also="")])
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
    hit = reply_failure(report)     # a reply that checked nothing arrives with is_error: false
    if hit:
        failed(f"replied with {hit!r} rather than a report")
    try:
        if os.path.islink(out):
            os.remove(out)     # only with --overwrite; never write through a link
        open(out, "w").write(f"# Unverified claims in {os.path.basename(a.target)}\n\n"
                             f"_Checked the version with {checked}._\n\n{report}\n")
        record(out, a.target)
        where = out
    except OSError:
        where = "(not saved; printed below)"
    print(f"verify: ${usd(res):.4f}, {res['wall_s']}s -> {where}\n")
    print(report[:3000])
    if where != "(not saved; printed below)":
        after("verify", out)
        rerecord(out, a.target)


def main():
    lacuna_config.warn_config_errors()   # a config.json that was not used as written, before any money is spent
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
    r.add_argument("--overwrite", action="store_true",
                   help="replace notes in --out that Lacuna cannot show are its own for these documents "
                        "(edited, unrecorded, or another document's with the same name)")
    s = sub.add_parser("synth")
    s.add_argument("--notes")
    s.add_argument("--repo", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--kind", choices=["digest", "plan"], default="digest")
    s.add_argument("--objective", default="What should we build next?")
    s.add_argument("--digest", help="a digest to start from (plan kind); notes stay authoritative")
    s.add_argument("--model", help="override the default model for this stage")
    s.add_argument("--status", help="path to the project's living status file (auto-detected if omitted)")
    s.add_argument("--overwrite", action="store_true",
                   help="replace --out even if Lacuna cannot show it wrote it (edited, or not written by Lacuna)")
    v = sub.add_parser("verify")
    v.add_argument("--target", required=True)
    v.add_argument("--repo", required=True)
    v.add_argument("--notes")
    v.add_argument("--model", help="override the verifier model for this run")
    v.add_argument("--overwrite", action="store_true", help="replace the report even if it has been edited")
    a = ap.parse_args()
    {"read": cmd_read, "synth": cmd_synth, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    main()
