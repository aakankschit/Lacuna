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
import argparse, concurrent.futures as cf, hashlib, json, os, re, shlex, shutil, socket, subprocess, sys, tempfile
import threading, time
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
PARSE_VERSION = "5"

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


def after(stage, out_path, written):
    """Run the user's post-stage command, if configured, with the output path as its final argument.
    `written` is what this run wrote: the files whose changes by the hook are still Lacuna's.

    This is the extension seam: a project-specific layer can file the output wherever it keeps
    records without this plugin knowing anything about it.

    The stage has already succeeded and already cost money by the time this runs, so **nothing
    here may raise** — a missing or broken command is reported and the run continues.
    """
    cmd = (lacuna_config.CONFIG.get("after") or {}).get(stage)
    if not cmd:
        return
    # What the hook does to this run's outputs is still Lacuna's: see rerecord().
    d = out_path if os.path.isdir(out_path) else os.path.dirname(out_path) or "."
    try:
        before = {name_key(p): _sha(p) for p in written if os.path.isfile(p) and not os.path.islink(p)}
    except OSError:
        before = {}
    _run_hook(stage, cmd, out_path)
    rerecord(d, before)


def _run_hook(stage, cmd, out_path):
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
    md = os.path.join(CACHE, "md", fitted(os.path.basename(path)[:-4], f"_{h}_p{PARSE_VERSION}{tag}.md",
                                          os.path.join(CACHE, "md"), spare=len(".partial")))   # prep_md's temp
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
    need_bookkeeping(f"{CACHE}/notes", f"{CACHE}/md", os.path.join(CACHE, "drafts"))
    need_short_names([f"{out_dir}/{os.path.splitext(os.path.basename(p))[0]}.md" for p in a.papers],
                     "Rename the document, or copy it under a shorter name.")
    # Each document's note is <out>/<its name>.md. Settle every name before anything is parsed or
    # paid for: two documents with one name once shared a note path, and the parallel readers then
    # cached one paper's note under the other's key, permanently.
    papers, by_note, blocked, before = [], {}, {}, {}
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
        # Held before it is checked and until this run ends, so that no other run writes it between.
        need_hold(note, f"reading {src} into")
        # A note may only replace a note this tool wrote for this document and nobody has edited
        # since. Anything else at that path is someone's: a hand-edited note, a file in a vault, or
        # the note for another paper that happens to share this one's file name.
        why = not_ours(note, src)
        if why:
            blocked[note] = why
        before[note] = file_state(note)
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
    drafts = tempfile.mkdtemp(prefix="read-", dir=os.path.join(CACHE, "drafts"))
    jobs = []
    for src, stem, note in papers:
        md = to_markdown(src, out_dir, equations=a.equations, parser=a.parser)
        body = open(md, "rb").read()
        content_h = hashlib.sha256(body).hexdigest()[:12]
        # The cache key covers everything that changes what the note says, not just the document.
        key = hashlib.sha256("\0".join(
            [content_h, PROMPT_VERSION, reader_model, reader_effort, a.brief, books_fp]
        ).encode()).hexdigest()[:12]
        cached = os.path.join(CACHE, "notes", fitted(stem, f"_{key}.md", os.path.join(CACHE, "notes")))
        # A note from before ownership was recorded (0.2.0), from a run killed between writing it
        # and recording it, or restored from a backup over one a hook had changed (B29), is still
        # provably this document's when it is exactly this document's cached note.
        if blocked.get(note, ("",))[0] in (UNRECORDED, "edited") and same_bytes(note, cached):
            del blocked[note]
        jobs.append((src, md, note, cached, os.path.join(drafts, f"{len(jobs)}.md")))
    if blocked and not a.overwrite:
        print(f"ERROR: {len(blocked)} file(s) in {out_dir} would be replaced, and Lacuna cannot show they "
              f"are notes it wrote for these documents:", file=sys.stderr)
        for _, why in blocked.values():
            print(f"  - {why}", file=sys.stderr)
        print("Nothing was sent to a model. To keep a file, move it or read into another --out. To read a "
              "different document with the same file name, rename it or use another --out. To replace "
              "these files anyway, run the same command with --overwrite.", file=sys.stderr)
        sys.exit(EX_INPUT)

    written = []

    def place(src, out, text):
        """Put `text` at the note path, unless the note changed while this run was in progress
        (someone edited it, or something else wrote it): then it is left as it is (B11)."""
        if changed_since(out, before[out]):
            return False
        _atomic_copy(text, out)           # renamed into place: never half-written, never through a link
        record(out, src)
        written.append(out)
        return True

    def one(src, md, out, cached, draft):
        left = (f"KEPT — {out} changed while this run was in progress, so it was left as it is. This run's "
                f"note is cached: run the same command with --overwrite to put it there, free")
        # A hit is judged by today's content check, not by the one that cached it: a note cached before
        # the check learned a shape (an apology, a status marker) was otherwise served free forever.
        fresh = os.path.exists(cached) and usable_output(cached, 400, f"cached note for {os.path.basename(out)}",
                                                         ("## Claim",), then="removed from the cache; reading the "
                                                         "document again (paid)", name=out)
        if os.path.exists(cached) and not fresh:
            try:
                os.remove(cached)
            except OSError:
                pass
        if fresh:
            # always refresh: --out may hold another run's note
            return os.path.basename(out), "cached (free)" if place(src, out, cached) else left, 0.0
        # The agent writes a draft in a directory of this run's own, never the note path. Judged and
        # cached at the note path, whatever was there when the agent returned was taken for its note:
        # an earlier note after a refusal, or another run's note, cached under this document's key.
        prompt = "\n\n".join(x for x in [
            a.brief, books,
            "Read the paper below in full and write its note to the given path, using exactly this template:",
            NOTE_TEMPLATE, f"Paper:\n- {md} -> {draft}", "Keep the note under 1,100 words.",
            INCOMPLETE_RULE.format(what="the paper", also=f" and do not write {draft}")] if x)
        res = claude("reader", reader_model, prompt, [os.path.dirname(md), out_dir, drafts, BOOKS_DIR],
                     effort=lacuna_config.effort("reader"), cwd=out_dir)
        # The reader is told the INCOMPLETE rule too, and its reply is read: a haiku given symbol noise
        # filed a template-shaped "Not recoverable" note, and it was cached (VERDICT3 N5).
        reply = "" if res.get("is_error") else str(res.get("result") or "").strip()
        ok = usable_output(draft, 400, "note", must_contain=("## Claim",), reply=reply, name=out)
        # source_tokens makes the calibration recipe in docs/extending.md actually runnable
        # effort and source_tokens are both required by the recalibration recipe in
        # docs/extending.md, which is per model x effort.
        ledger({"stage": "read", "model": reader_model, "effort": reader_effort,
                "paper": os.path.basename(md), "cost": usd(res), "wall_s": res["wall_s"],
                "books": bool(idx), "wrote_note": ok,
                "source_tokens": round(os.path.getsize(md) / 2.9), "is_error": res.get("is_error")})
        if ok:
            try:
                _atomic_copy(draft, cached)
            except OSError as e:      # the note is paid for and good: caching it is only a saving
                print(f"  ! could not cache the note for {src} ({e}); it will be read again next time",
                      file=sys.stderr)
                if changed_since(out, before[out]):
                    beside = free_name(out, ".new")      # not .md: synth reads every note in --out
                    _atomic_copy(draft, beside)
                    left = (f"KEPT — {out} changed while this run was in progress, so it was left as it is. "
                            f"This run's note is in {beside}")
            msg = f"${usd(res):.4f} {res['wall_s']}s" if place(src, out, draft) else left
            return os.path.basename(out), msg, usd(res)
        said = judge(reply, reply=True)
        why = (str(res.get("result"))[:120] if res.get("is_error") else
               f"it replied {said[1][:160]!r}" if said and said[0] == "fail" else
               "the file it wrote is not a note (see above)" if os.path.exists(draft) else "agent produced no note file")
        if changed_since(out, before[out]):
            # The agent wrote the note path instead of its draft, or someone edited it meanwhile. This run
            # did not write it, so it stays where it is: a user's edit was moved aside once (E11).
            why += f"; {out} changed during the run and was left as it is: check it before using it"
        return os.path.basename(out), f"FAILED ${usd(res):.4f} — {why}", usd(res)

    t0, total, failed, kept = time.time(), 0.0, 0, 0
    try:
        with cf.ThreadPoolExecutor(min(8, len(jobs))) as ex:
            for name, msg, cost in ex.map(lambda j: one(*j), jobs):
                print(f"  {name:48s} {msg}")
                total += cost
                failed += msg.startswith("FAILED")
                kept += msg.startswith("KEPT")
    finally:
        shutil.rmtree(drafts, ignore_errors=True)
    print(f"{len(jobs) - failed - kept}/{len(jobs)} notes written, ${total:.3f}, {round(time.time() - t0)}s wall "
          f"-> {out_dir}")
    if failed:
        print(f"ERROR: {failed} document(s) produced no note. Nothing downstream should run on this "
              f"directory, and no partial note was cached.", file=sys.stderr)
        sys.exit(EX_MODEL)
    if kept:
        print(f"ERROR: {kept} note(s) changed while this run was in progress and were left as they are; "
              f"see above.", file=sys.stderr)
        sys.exit(EX_INPUT)
    after("read", out_dir, written)


# ---- did the model say it could not do the job?
#
# Every agent is told to reply `INCOMPLETE: <reason>` rather than write a document about failing
# (INCOMPLETE_RULE), and real failing models did, in capitals, each time it was tried. This check
# swung between filing failures and discarding paid results four times, each time by adding or
# removing phrases; so it has three outcomes, and only an unambiguous declaration fails a stage.
#
#   fail   exit 4, nothing filed or cached. In a document's OPENING (the headings above its first
#          statement, and that statement, past any frontmatter and furniture: a callout tag, a quote
#          mark, a list number, a code fence, "Status:"), or on any line of a reply:
#          - a status word IN CAPITALS set off as the document's state: "INCOMPLETE: …", "(BLOCKED)",
#            "— PARTIAL", "**FAILED**" (the rule is above _status_word);
#          - the agent saying it could not do the job as a whole: "I" or no subject, an inability, a
#            verb for the job, and the whole input as its object (_WHOLE): "I couldn't open any of
#            the notes", "Unable to read the supplied directory", "As an AI, I cannot open PDF files
#            directly", "I'm sorry, but I can't help with that"; or "The paper could not be read".
#          And a reply that holds the told `INCOMPLETE:` anywhere: "… symbol noise. INCOMPLETE: none".
#   warn   kept and filed, with one line on stderr naming the file: the same shapes where a program
#          cannot tell a failure from a finding. The status word in lower or title case ("# Partial
#          3D shape matching", "Incomplete: no baseline for proposal 5"), or on a list item with a
#          reason after it ("- **BLOCKED** until the CUDA build lands"); an inability about a part
#          ("I could not access the Supporting Information", "two of the five notes", "Table 3");
#          an apology ("I'm sorry, but …"); and any of these below the opening.
#   clean  everything else: the words inside a sentence ("37 had not completed follow-up", "the
#          authors were unable to read 5 records", "We could not complete follow-up": in a note "we"
#          are the paper's authors), in a name, and a verify finding (`claim -> what you found`).
#
# A failure phrased outside these shapes is filed, so the reply is the channel to rely on.
_LEAD_IN = re.compile(r"^(?:(?:unfortunately|regrettably|sadly|however|note|sorry|(?:my )?apologies"
                      r"|i am (?:very |so |really )?sorry|i am afraid|i apologi[sz]e|as an ai(?: language model)?)"
                      r"\b[^\w]*(?:but\b[^\w]*)?)+")
_SORRY = re.compile(r"(?:sorry|(?:my )?apologies|i am (?:very |so |really )?sorry|i am afraid|i apologi[sz]e|as an ai)\b")
_UNABLE = (r"(?:(?:am|was|were|have been|had been)\s+)?(?:unable|not able)\s+to|cannot|can not|could not"
           r"|did not manage to|(?:do|did) not have the (?:ability|means) to")
_JOB = (r"help|assist|complete|finish|fulfil|fulfill|comply|proceed|continue|summari[sz]e|write|produce|generate"
        r"|provide|perform|do|open|read|access|load|parse|process|view|retrieve|extract|decode|find|see")
_NP = (r"(?:(?:the|this|these|those|your|any|all|either)\s+)?(?:(?:provided|supplied|given|attached|uploaded|input"
       r"|source|literature|whole|entire|pdf)\s+)*(?:papers?|pdfs?|documents?|articles?|notes?|files?|text|inputs?"
       r"|literature|sources?|directory|codebase|repository|request|task)")
# The whole input, not a part of it: "any of the notes", not "two of the five notes", "the notes
# directory", "the paper's SI" or "the text of Table 3" — the phrase must end where the noun does.
_WHOLE = rf"(?:(?:any|all|either|each|none|both)\s+of\s+(?:them|{_NP})|{_NP})"
_END = r"(?=\s*(?:[^\w\s'-]|$)|\s+(?:so|because|since|as|and|or|you|i|that|directly)\b)"
_CANNOT = re.compile(rf"(?:i\s+)?(?:(?:{_UNABLE})\s+(?:{_JOB})\b|(?:do|did) not have access\b|(?:have|had) no access\b)")
_GIVES_UP = re.compile(
    rf"(?:i\s+)?(?:(?:{_UNABLE})\s+(?:{_JOB})|(?:do|did) not have access to|(?:have|had) no access to)\s+{_WHOLE}{_END}"
    rf"|i\s+(?:{_UNABLE})\s+(?:help|assist|comply)\b"
    rf"|(?:i\s+)?(?:{_UNABLE})\s+(?:check|verify|confirm|fact-check)\s+(?:any|a single)\b"
    rf"|{_WHOLE}\s+(?:could not|cannot|can not)\s+be\s+(?:read|opened|accessed|parsed|processed|loaded|decoded)\b"
    r"|no (?:files?|notes?|documents?) (?:were|was|could be) found\b")
_TOLD = re.compile(r"(?<![\w-])INCOMPLETE[`*_]*:")


def _norm(text):
    """Lower case, straight apostrophes and contractions spelled out, so the grammar has one form of each
    phrase: a curly apostrophe is what most models emit, and "couldn't" is as common as "could not"."""
    t = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"\bcan't\b", "cannot", t)
    t = re.sub(r"\bwon't\b", "will not", t)
    t = re.sub(r"n't\b", " not", t)
    return re.sub(r"\bi'm\b", "i am", t)


def _inability(clause):
    """"fail", "warn" or None for one clause (normalised) that opens a statement (rule above)."""
    rest = _LEAD_IN.sub("", clause, count=1)
    if _GIVES_UP.match(rest):
        return "fail"
    return "warn" if _CANNOT.match(rest) or _SORRY.match(clause) else None


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


def judge(text, reply=False):
    """("fail" or "warn", the line it turned on) for the head of a document or for a model's reply, or
    None (the rule is above). A statement runs to a blank line, a heading or the next list item, so an
    apology wrapped over two lines is read whole. A reply is all opening, line by line; one that is
    only a path, which is what an agent that did the job replies, is not prose, whatever its
    directories are called (…/Failed/notes/0.md)."""
    if reply:
        if "\n" not in text.strip() and os.path.isabs(text.strip().strip("`")):
            return None
        for line in text.splitlines():
            told = _TOLD.search(line)
            if told and "->" not in line and not _NONE_VALUE.match(line, told.end() - 1):
                return "fail", line.strip()
    blocks, block = [], None
    for line in _lines(text):
        if not line.strip():
            block = None
        elif reply or not block or block[0].lstrip().startswith("#") or line.lstrip().startswith("#") \
                or _ITEM.match(line):
            block = [line]
            blocks.append(block)
        else:
            block.append(line)
    opening, doubt = True, None
    for block in blocks:
        first = block[0]
        heading = first.lstrip().startswith("#")
        if "->" not in first:              # `claim -> what you found`: a verify finding, graded or not
            said = _norm(" ".join(_FURNITURE.sub("", l, count=1) for l in block))
            found = {_status_word(first, item=not heading and bool(_ITEM.match(first)))}
            found |= {_inability(c) for c in re.split(r"(?<=[.!?:;])\s+", said)}
            if "fail" in found and (opening or reply):
                return "fail", first.strip()
            if found - {None}:
                doubt = doubt or ("warn", first.strip())
        opening = opening and heading
    return doubt


def warn(name, line):
    print(f"  ! {name}: the opening reads like it may not have done the job: \"{line[:160]}\" — check it "
          f"before relying on it.", file=sys.stderr)


# A status word in the opening — BLOCKED, PARTIAL, INCOMPLETE or FAILED — declares the document's
# state, or is part of a name. In capitals it is a failure; in any other case a doubt (rule above).
#
#   It is SET OFF: at the start (after furniture and a `Status:` label), or after an opening bracket,
#   a dash, a colon, semicolon, comma, slash, an emphasis mark or an emoji: "(BLOCKED)", "— Blocked",
#   ": INCOMPLETE", "**BLOCKED**", "_INCOMPLETE_", "🚫 BLOCKED".
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
# A list item is one entry, a proposal or a finding: there only the word at the item's start counts,
# and with a reason after it only as a doubt: "- **BLOCKED** until the CUDA build lands: port the
# scorer" is that proposal's precondition as often as it is the document's state.
_STATUS_WORD = re.compile(r"(?i)(?<![^\W_])(blocked|partial|incomplete|failed)(?![^\W_])")
_NOT_SET_OFF = "`'\".)]"          # code, a quotation, `JobState.PARTIAL` or "vs.", a closing bracket
_REASON = re.compile(r"(?i)(?:by|until|till|pending|because|due|on|as|since|awaiting|for|while|without|after)\b"
                     r"|\d|(?:development\s+)?(?:plan|digest|note|report|draft)\b")
_ALSO = re.compile(r"\s*(?:[/&]|vs\.?)\s*[*_]*[A-Za-z][\w-]*[*_]*|\s*(?:,|\band\b|\bor\b)\s*[*_]*[A-Z]{2,}\b[*_]*")
_NONE_VALUE = re.compile(r"(?i)\s*[:=\u2013\u2014-]?\s*[*_`(\[]*(?:none|nil|nothing|no|n/a|0)[*_`)\]]*(?:\s+items?)?"
                         r"\s*(?:$|[.;,!?)\]\u2013\u2014]|-\s)")
_FURNITURE = re.compile(r"^(?:\[![^\]]*\]|\d+[.)](?=\s)|[\W_])*(?:status\s*[:\u2013\u2014-][\W_]*)?", re.I)


def _status_word(line, item=False):
    """"fail", "warn" or None for the status word `line` sets off as the document's state (rule above)."""
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
        if not rest or not rest[0].isalnum() or _REASON.match(rest):
            return "fail" if m.group(1).isupper() and not (item and rest[:1].isalnum()) else "warn"
    return None

# An honest model that could not read its sources says so; it must say so where a program can see
# it, not in a fluent document that passes every check.
INCOMPLETE_RULE = ("If you could not read {what}, do not write a document explaining why{also}: reply with "
                   "one line, `INCOMPLETE: <what you could not read>`, and nothing else.")


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
    # A short temporary name: "<dst>.<pid>.part" was 11 bytes longer than a name that fits, so a
    # document with a 230-239 byte name failed here, after its reader had been paid (B13).
    tmp = os.path.join(os.path.dirname(dst), f".{os.getpid()}.{threading.get_ident()}.part")
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


def name_max(d):
    """The longest file name directory `d` (or its nearest existing parent) holds, in bytes."""
    while not os.path.isdir(d) and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    try:
        n = os.pathconf(d, "PC_NAME_MAX")
    except (OSError, ValueError, AttributeError):
        n = None
    return n if n and n > 0 else 255


def fitted(stem, suffix, d, spare=0):
    """stem + suffix, the stem cut short enough for directory `d` (with `spare` bytes over, for a
    temporary name built on it). Only for names that carry a hash (the caches): cutting the stem
    cannot make two of them one. A name that already fits is kept, so no cache entry moves."""
    room = name_max(d) - len(os.fsencode(suffix)) - spare
    if len(os.fsencode(stem)) > room:
        stem = os.fsencode(stem)[:max(room, 0)].decode("utf-8", "ignore")
    return stem + suffix


def need_short_names(paths, how):
    """Exit 2 unless every file name Lacuna will create fits its directory. Checked before any model
    call: a name the filesystem refused after the call cost the call and wrote nothing (B13, B14)."""
    long = [p for p in paths if len(os.fsencode(os.path.basename(p))) > name_max(os.path.dirname(p))]
    if long:
        print("ERROR: these file names would be longer than their directory allows "
              f"({name_max(os.path.dirname(long[0]))} bytes):\n" + "\n".join(f"  - {p}" for p in long)
              + f"\n{how} Nothing was sent to a model.", file=sys.stderr)
        sys.exit(EX_INPUT)


def free_name(path, tag, ext=""):
    """path+tag+ext, or path+tag-2+ext, -3 …, whichever does not exist yet. What an earlier run set
    aside for the user to look at is never replaced by a later one. The name of `path` is cut to
    fit the directory if it must be, never the tag: these names are made after the model call, and
    one too long to create cost the call."""
    d, name = os.path.split(path)
    base = os.path.join(d, fitted(name, "", d, spare=len(os.fsencode(tag + ext)) + 4) + tag)
    n = 1
    while os.path.lexists(f"{base}-{n}{ext}" if n > 1 else f"{base}{ext}"):
        n += 1
    return f"{base}-{n}{ext}" if n > 1 else f"{base}{ext}"


def file_state(path):
    """What is at `path`: its sha256, "" for something that is not a regular file, None for nothing."""
    if not os.path.lexists(path):
        return None
    return _sha(path) if os.path.isfile(path) else ""


def changed_since(path, before):
    """True when `path` is not what it was when the run started (`before`, from file_state). A plan
    or report changed during a run is the user's, or someone else's; one deleted meanwhile is not."""
    now = file_state(path)
    return now is not None and now != before


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
        # Only an entry with a checksum describes a file Lacuna wrote. 0.3.1 also kept "pending"
        # reservations here, released only on success: one left by a refused, failed or killed run
        # refused every later read of a same-named document, --overwrite included (A4), and made the
        # note such a run left read as "edited since" (A7). Runs hold paths now (hold()); old
        # reservations are ignored.
        if isinstance(v, dict) and isinstance(v.get("sha256"), str):
            good[k] = {**{x: y for x, y in v.items() if x != "pending"},
                       "source": v["source"] if isinstance(v.get("source"), str) else None}
    return good, None


WHAT = {"read": "a note", "synth": "a plan or digest", "verify": "a verify report"}


def _stage(entry):
    """The stage that wrote a recorded file. A plan and a note are not interchangeable: a paper
    named plan.md once replaced the plan recorded beside it (B15). Records written before the stage
    was recorded do not say; a synth output was the only file recorded without a source, and a
    verify report is named after the document it checked."""
    if entry.get("stage") in WHAT:
        return entry["stage"]
    if not entry.get("source"):
        return "synth"
    return "verify" if entry.get("name") == os.path.basename(entry["source"]) + ".unverified.md" else "read"


def _owners_to_update(d):
    """The record, for a writer that holds the lock. A damaged one is set aside, never silently
    replaced: it was the only evidence of which files here are Lacuna's, and rewriting it with this
    run's entries alone left every later refusal unable to say why (B28)."""
    files, damaged = _owners(d)
    if damaged:
        kept = free_name(os.path.join(d, OWNERS), f".damaged-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(os.path.join(d, OWNERS), kept)
        print(f"  ! {damaged}; it was kept as {kept} and a new record started. Files it listed are no "
              f"longer known to be Lacuna's, so a later run asks (--overwrite) before replacing them.",
              file=sys.stderr)
    return files


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


def need_bookkeeping(*dirs):
    """Exit 2 unless the ledger can be appended to and each cache directory in `dirs` made and written.
    Both are written after the paid call: a paths.ledger naming a directory paid, then crashed (J5)."""
    try:
        os.makedirs(os.path.dirname(LEDGER) or ".", exist_ok=True)
        open(LEDGER, "a").close()
        for d in dirs:
            os.makedirs(d, exist_ok=True)
        why = next((f"{d}: not writable" for d in dirs if not os.access(d, os.W_OK)), None)
    except OSError as e:
        why = f"{e.filename or LEDGER}: {e.strerror or e}"
    if why:
        print(f"ERROR: Lacuna cannot write its own records ({why}). Check paths.ledger and paths.cache in "
              f"`lacuna config`. Nothing was sent to a model.", file=sys.stderr)
        sys.exit(EX_INPUT)


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


_held = {}             # lock file -> fd, for every path this process holds


def hold(path, doing):
    """Hold `path` until this process exits. Returns None, or what the run that holds it is doing.

    One process writes a given note, plan or report at a time, from its ownership check to its final
    write. 0.3.1 reserved a path with a "pending" entry in the record, released only on success: a
    refused reader, Ctrl-C, a missing parser or a kill left it there, and every later read of a
    same-named document was refused for good, --overwrite included (A4). A re-read dropped its own
    reservation before its model call (A5), and the same document under two cache keys was let
    through (A6): two agents wrote one path, and one paper's note was cached under another's key.
    Two synths into one --out deleted each other's .partial (A11).

    A hold is an flock on a lock file beside the record. The kernel ends it when the process ends,
    however it ends (SIGKILL, a reused pid), so a hold can never go stale. The file says who holds
    it, for the message. Without fcntl a path is not held; nor where it cannot be locked, and then the
    run says so (_not_held)."""
    if fcntl is None:
        return None
    lock = os.path.join(os.path.dirname(path),
                        f".lacuna-{hashlib.sha256(name_key(path).encode()).hexdigest()[:16]}.lock")
    if lock in _held:
        return None
    for _ in range(5):
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return _not_held(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                info = json.loads(os.read(fd, 4096) or b"{}")
            except (OSError, ValueError):
                info = {}
            os.close(fd)
            return {**(info if isinstance(info, dict) else {}), "lock": lock}
        except OSError:
            os.close(fd)
            try:
                os.unlink(lock)          # nothing holds it, so nothing is left behind
            except OSError:
                pass
            return _not_held(path)
        try:      # released and unlinked between our open and our lock: hold the file now at the path
            ours = os.path.samestat(os.fstat(fd), os.stat(lock))
        except OSError:
            ours = False
        if ours:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                     "since": time.strftime("%Y-%m-%d %H:%M"), "doing": doing}).encode())
            _held[lock] = fd
            return None
        os.close(fd)
    return None


_unlockable = set()    # directories already reported


def _not_held(path):
    """No hold on `path`: its filesystem cannot lock (EOPNOTSUPP on some network and FUSE mounts), or no
    lock file can be made there. The run goes on, and says so once per directory: in silence, two runs
    into one --out both paid, and one moved the other's plan aside (J4)."""
    d = os.path.dirname(path)
    if d not in _unlockable:
        _unlockable.add(d)
        print(f"  ! cannot lock {d}; concurrent runs into it are not protected", file=sys.stderr)
    return None


def need_hold(path, doing):
    """Hold `path`, or exit 2 before any model call naming the run that holds it."""
    info = hold(path, doing)
    if info is None:
        return
    who = " ".join(str(x) for x in (f"pid {info.get('pid')}" if info.get("pid") else "",
                                    f"on {info.get('host')}" if info.get("host") else "",
                                    f"since {info.get('since')}" if info.get("since") else "") if x)
    print(f"ERROR: another Lacuna run{f' ({who})' if who else ''} is {info.get('doing') or 'writing'} {path} "
          f"right now. Wait for it to finish, or write somewhere else. Nothing was sent to a model. A run "
          f"holds a path only while it runs, however it ends; if that run is gone and this persists, "
          f"delete {info['lock']}.", file=sys.stderr)
    sys.exit(EX_INPUT)


def release_all():
    """End every hold this process has. Unlinked before it is unlocked; hold() checks it still has
    the file at the path, so a run that opened it meanwhile does not hold a file nobody can see."""
    for lock, fd in list(_held.items()):
        try:
            os.unlink(lock)
        except OSError:
            pass
        os.close(fd)
        del _held[lock]


def record(path, source=None, stage="read"):
    """Record that Lacuna's `stage` wrote `path`, as it is now, from the document `source`.

    The stage has already succeeded and been paid for, so this never raises: a record that cannot
    be written costs a question (--overwrite) on the next run, not this run's output."""
    d = os.path.dirname(path)
    try:
        entry = {"name": os.path.basename(path), "sha256": _sha(path), "stage": stage,
                 "written": time.strftime("%Y-%m-%d %H:%M")}
        if source:
            entry.update(source=source, source_sha256=_sha(source))
        with _locked(d):
            files = _owners_to_update(d)
            files[name_key(path)] = entry
            _save_owners(d, files)
    except OSError as e:
        print(f"  ! could not record {path} as Lacuna's ({e}); the next run will ask before replacing it",
              file=sys.stderr)


def rerecord(d, before):
    """After an after.* hook: the hook is part of the user's pipeline, so what it does to this run's
    outputs (`before`, {key: sha256} as they were when it started) is still Lacuna's. A hook that
    prepended frontmatter, as docs/extending.md suggests, otherwise made every later run refuse the
    note as edited. Only this run's outputs, and only those still as recorded: any other file that
    changed while the hook ran — an earlier run's note, edited by the user meanwhile — became
    Lacuna's, and the next read replaced the user's edit (F1). A hook that changes other files makes
    them the user's, so the next run asks. A hook that moved a file away takes it out of the record."""
    if not before:
        return
    try:
        with _locked(d):
            files, changed = _owners_to_update(d), False
            for k, sha in before.items():
                e = files.get(k)
                if not e or e.get("sha256") != sha:
                    continue                  # edited since it was written, or re-recorded by another run
                p = os.path.join(d, e["name"])
                if os.path.islink(p) or not os.path.isfile(p):
                    del files[k]
                    changed = True
                elif _sha(p) != sha:
                    files[k] = {**e, "sha256": _sha(p)}
                    changed = True
            if changed:
                _save_owners(d, files)
    except OSError as e:
        print(f"  ! could not re-record what the hook changed in {d} ({e}); the next run may ask before "
              f"replacing those files", file=sys.stderr)


def not_ours(path, source=None, stage="read"):
    """None when Lacuna's `stage` may replace `path`; otherwise (kind, why), `why` a sentence for the user.

    Only the stage that wrote a file replaces it: a note is never written over a plan or a report,
    nor a plan over a note. `source` is the document a note is written from; a note Lacuna wrote
    from a *different* document is not ours to replace either. A document is the same one when it
    has the same bytes, wherever it is now. Same path, different bytes is not enough: a new
    download saved as fulltext.pdf over the last one is another paper, and its note silently
    replaced the first paper's (B1); a revised copy of the same paper asks too.
    A file with no record is not ours either, whatever its name: a report annotated by hand was
    replaced once its record was deleted (B12)."""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path) or not os.path.isfile(path):
        return "other", f"{path} is a {'symbolic link' if os.path.islink(path) else 'directory'}, not a file Lacuna wrote."
    d = os.path.dirname(path)
    files, damaged = _owners(d)
    entry = files.get(name_key(path))
    if not isinstance(entry, dict):
        try:
            kept = sorted(f for f in os.listdir(d) if f.startswith(f"{OWNERS}.damaged-"))
        except OSError:
            kept = []
        return UNRECORDED, (f"{path} has no record in {OWNERS} of being written by Lacuna"
                            + (f" ({damaged})" if damaged else
                               f" (an earlier record here was damaged, and was kept as {kept[-1]})" if kept else "")
                            + ". It may be your own file, one written before Lacuna kept that record (0.2.0 and "
                            "earlier), or one left by an interrupted run.")
    theirs = _stage(entry)
    if theirs != stage:
        return "other", f"{path} is {WHAT[theirs]} written by `lacuna lit {theirs}`, not {WHAT[stage]}."
    if entry.get("sha256") != _sha(path):
        return "edited", (f"{path} was written by Lacuna on {entry.get('written', 'an earlier run')}, and has "
                          f"been edited since.")
    was = entry.get("source")
    if source and was and os.path.isfile(source):
        here = os.path.realpath(source) == os.path.realpath(was)
        recorded = entry.get("source_sha256")
        if (_sha(source) == recorded) if isinstance(recorded, str) else here:
            return None
        if here:
            return "other", (f"{path} is the note for {was}, which has changed since that note was written: it "
                             f"may now be another document saved under the same name, and reading it would "
                             f"replace that note.")
        return "other", (f"{path} is the note for {was}; {source} is a different file with the same name "
                         f"(another document, or a changed copy of that one elsewhere), and reading it would "
                         f"replace that note.")
    return None


def usable_output(path, min_bytes, what, must_contain=(), then="treating it as a failed run, not output",
                  reply="", name=None):
    """True when a stage produced real output, not a short file or a declaration of failure.

    Size alone is not evidence: a model that cannot do the job writes a fluent paragraph saying so.
    Structure is the strongest evidence: a note that lacks the template's headings is not a note,
    whatever it says. After that, judge() reads its opening and the model's `reply`: a failure in
    either is no output; a doubt is output, kept, with one warning naming `name` (what the user sees).
    """
    if not os.path.exists(path) or os.path.getsize(path) < min_bytes:
        return False
    with open(path, errors="ignore") as f:
        head = f.read(1200)
    missing = next((m for m in must_contain if m not in head), None)
    if missing:
        print(f"  ! the {what} has no {missing!r} section — {then}", file=sys.stderr)
        return False
    found = [v for v in (judge(head), judge(reply, reply=True) if reply else None) if v]
    hit = next((v for v in found if v[0] == "fail"), found[0] if found else None)
    if hit and hit[0] == "fail":
        print(f"  ! the {what} says it could not do the job: {hit[1][:160]!r} — {then}", file=sys.stderr)
        return False
    if hit:
        warn(name or path, hit[1])
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
    need_short_names([a.out, f"{a.out}.filelist.txt"], "Choose a shorter --out.")
    out_dir = os.path.dirname(a.out)
    os.makedirs(out_dir, exist_ok=True)
    need_writable(out_dir, "the directory of --out")
    need_hold(a.out, "writing a plan or digest to")     # its .filelist.txt with it
    need_bookkeeping()
    # --out is often a file the user keeps (lit/plan.md in a repo, a page in a vault). A hand-edited
    # plan was once replaced without a word: the ownership rule covered read's notes only.
    why = not_ours(a.out, stage="synth")
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
                hit = judge(fh.read(1200))
        except OSError as e:
            empty.append(f"{f} cannot be read ({e.strerror})")
            continue
        if hit and hit[0] == "fail":
            empty.append(f"{f} says it holds no literature: {hit[1][:160]!r}")
        elif hit:
            warn(f, hit[1])
    if empty:
        print("ERROR: these inputs cannot be used as literature:\n" + "\n".join(f"  - {e}" for e in empty)
              + "\nFix their permissions, remove them, or run the stage that wrote them again. Nothing was "
                "sent to a model.",
              file=sys.stderr)
        sys.exit(EX_INPUT)
    tree = repo_files
    status = find_status(a.repo, a.status)
    tree_path = f"{a.out}.filelist.txt"
    if os.path.islink(tree_path):
        os.remove(tree_path)                   # Lacuna's own scratch name: never write through a link
    open(tree_path, "w").write(tree)
    # The agent writes beside --out, never over it. Judged at --out, an earlier run's plan passed for
    # this run's when the model refused; and an apology written there was taken as input by the next
    # command. --out is replaced only by output that passed the content check. The name is this run's
    # own: a shared <out>.partial was deleted by a concurrent run (J4), and written by a killed run's
    # model after a retry had started (F2), so another run's output passed for this one's.
    part = os.path.join(out_dir, f".lacuna-{os.getpid()}-{os.urandom(4).hex()}.partial")
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
    before = file_state(a.out)
    res = claude("synth", model, prompt, dirs, effort=lacuna_config.effort(role), cwd=out_dir)
    # A model that could not read its sources may say so in its reply and still leave a document
    # behind; a real digester filed "PARTIAL: literature notes unreadable", exit 0. Its own word wins.
    ok = usable_output(part, 800, a.kind, reply=str(res.get("result") or ""), name=a.out)
    ledger({"stage": "synth", "kind": a.kind, "model": model, "effort": lacuna_config.effort(role),
            "notes": len(notes),
            "from_digest": bool(a.digest), "cost": usd(res), "wall_s": res["wall_s"], "wrote": ok,
            "is_error": res.get("is_error")})
    print(f"{a.kind} ({model}): ${usd(res):.3f}, {res['wall_s']}s -> {a.out}")
    if not ok:
        for f in (part, tree_path):
            if os.path.exists(f):
                os.remove(f)
        if changed_since(a.out, before):
            # --out changed during the run: the model wrote it rather than the file it was given, or
            # someone edited it meanwhile. This run did not write it, so it stays where it is: a
            # concurrent run's plan was moved aside by this path once (J4).
            kept = (f"{a.out} changed during this run (the model may have written it, or it was edited), so it "
                    f"is not a checked plan; it was left as it is.")
        else:
            kept = (f"{a.out} was left as it was, and holds an earlier run's output, not this one's."
                    if os.path.exists(a.out) else "Nothing was written.")
        print(f"ERROR: {model} wrote no usable output. {kept}\n{str(res.get('result'))[:400]}", file=sys.stderr)
        sys.exit(EX_MODEL)
    if changed_since(a.out, before):
        # Edited while the model ran (or written by something else): checked before the call, it
        # was replaced on success all the same, and the edit was lost (B11). Keep both.
        new = free_name(a.out, ".new", ".md")
        os.replace(part, new)
        record(new, stage="synth")
        print(f"ERROR: {a.out} changed while this run was in progress, so it was left as it is. This run's "
              f"{a.kind} is in {new}: compare the two, and move it over {a.out} to use it.", file=sys.stderr)
        sys.exit(EX_INPUT)
    if os.path.islink(a.out):
        os.remove(a.out)     # only with --overwrite; replacing the link, not writing through it
    os.replace(part, a.out)
    record(a.out, stage="synth")
    after("synth", a.out, [a.out])


def cmd_verify(a):
    """Cheap grep-based fact check. The verifier reports in its final message; we save that."""
    need_dir(a.repo, "--repo")
    if a.notes:
        need_dir(a.notes, "--notes")
        try:
            os.listdir(a.notes)
        except OSError as e:
            refuse_inputs([f"--notes {a.notes} cannot be listed ({e.strerror})"])
    # Missing, a directory, unreadable, empty or binary: --target is refused here, before any model call.
    refuse_inputs([f"--target {a.target} {why}" for why in [not_content(a.target)] if why])
    if not repo_tree(a.repo).strip():
        refuse_inputs([f"--repo {a.repo} has no files (or none Lacuna can list), so there is no codebase to check against"])
    # The prompt is all the verifier knows: a relative path there checked nothing and exited 0.
    a.target, a.repo = os.path.abspath(a.target), os.path.abspath(a.repo)
    if a.notes:
        a.notes = os.path.abspath(a.notes)
    out = f"{a.target}.unverified.md"
    need_short_names([out], "Rename --target, or copy it under a shorter name.")
    need_hold(out, "writing a verify report to")
    need_bookkeeping()
    if os.path.isdir(out) and not os.path.islink(out):      # read and synth refuse one too
        print(f"ERROR: {out} is a directory, so the report has nowhere to go. Nothing was sent to a model.",
              file=sys.stderr)
        sys.exit(EX_INPUT)
    why = not_ours(out, stage="verify")       # a report annotated by hand is the user's
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
    before = file_state(out)
    res = claude("verifier", vmodel, prompt, dirs, effort=lacuna_config.effort("verifier"))
    ledger({"stage": "verify", "model": vmodel, "effort": lacuna_config.effort("verifier"),
            "target": os.path.basename(a.target), "cost": usd(res),
            "wall_s": res["wall_s"], "is_error": res.get("is_error")})
    report = str(res.get("result") or "")

    def failed(why):
        # An earlier report is never removed, even one that describes another version of the document:
        # this run did not write it, and it may be the user's annotated copy (F4).
        stale = os.path.exists(out) and checked not in open(out, errors="ignore").read(400)
        print(f"ERROR: the verifier ({vmodel}) {why}. Nothing was written, and "
              f"{os.path.basename(a.target)} has NOT been fact-checked."
              + (f" {out} describes another version of it." if stale else "")
              + f"\n{report[:400]}", file=sys.stderr)
        sys.exit(EX_MODEL)

    if res.get("is_error") or not report.strip():
        failed("returned no usable report")
    hit = judge(report, reply=True)     # a reply that checked nothing arrives with is_error: false
    if hit and hit[0] == "fail":
        failed(f"replied {hit[1][:160]!r} rather than a report")
    # A report edited while the verifier ran is never replaced; this run's goes beside it (B11).
    clash = changed_since(out, before)
    dst = free_name(out, ".new", ".md") if clash else out
    try:
        if os.path.islink(dst):
            os.remove(dst)     # only with --overwrite; never write through a link
        open(dst, "w").write(f"# Unverified claims in {os.path.basename(a.target)}\n\n"
                             f"_Checked the version with {checked}._\n\n{report}\n")
        record(dst, a.target, stage="verify")
        where = dst
    except OSError:
        where = "(not saved; printed below)"
    print(f"verify: ${usd(res):.4f}, {res['wall_s']}s -> {where}\n")
    print(report[:3000])
    if hit:
        warn(dst, hit[1])
    if clash:
        print(f"ERROR: {out} changed while this run was in progress, so it was left as it is. This run's "
              + (f"report is in {dst}: compare the two, and move it over {out} to use it." if where == dst
                 else "report is printed above."), file=sys.stderr)
        sys.exit(EX_INPUT)
    if where != "(not saved; printed below)":
        after("verify", out, [out])


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
    v.add_argument("--overwrite", action="store_true",
                   help="replace the report even if Lacuna cannot show it wrote it (edited, or not recorded)")
    a = ap.parse_args()
    try:
        {"read": cmd_read, "synth": cmd_synth, "verify": cmd_verify}[a.cmd](a)
    finally:
        release_all()


if __name__ == "__main__":
    main()
