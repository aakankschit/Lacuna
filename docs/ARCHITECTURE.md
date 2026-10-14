# Architecture

What each file is, what it may assume, and where the sharp edges are. Read this before changing
anything, and before testing it.

## The shape of the thing

Lacuna is a Claude Code plugin wrapping a **shell-out orchestrator**. It does not call an API
itself: it builds prompts, runs `claude -p` as a subprocess with a slim agent definition, and reads
back JSON. Everything else is caching, validation and accounting around that one move.

```
                   bin/lacuna  ──  the only public surface
                        │
        ┌───────────────┼────────────────┬──────────────┐
        │               │                │              │
   scripts/lit/    scripts/cost/    scripts/panes/  scripts/doctor.py
   the pipeline    the accounting   the viewer      first-run setup
        │               │
        └──────┬────────┘
         scripts/lacuna_config.py   ── one source of truth for paths, models, prices
                        │
              $LACUNA_DATA (~/.lacuna)   ── all mutable state, survives plugin updates
```

**The plugin directory is read-only** and is replaced wholesale on update. Nothing may write into
it. Everything mutable lives in `$LACUNA_DATA`.

## Files

### `bin/lacuna`
A bash dispatcher. Resolves `$ROOT` from its own location, routes subcommands to scripts, and
nothing else. It must stay thin: the exit-code contract (0 ok, 2 bad input, 3 parse failure, 4 the
model refused or produced nothing) is implemented by the scripts, not here.

### `scripts/lacuna_config.py`
Imported by every other script, so **it must never raise on bad input**. Three layers, later wins:
built-in defaults → `$LACUNA_DATA/config.json` → `LACUNA_*` environment variables. Tracks where
each value came from (`SOURCES`) and records rather than throws on a malformed file
(`CONFIG_ERRORS`). Also owns the price table and `resolve()`, which maps aliases (`sonnet`) to
concrete ids (`sonnet-5-5`) — a price table is keyed by concrete ids and matched by substring, so
an unresolved alias silently prices at zero.

### `scripts/lit/lit_pipeline.py`
The orchestrator. Three subcommands:

- **`read`** — one slim agent per document, in parallel (pool of 8). Parses via `prep_md.py`, then
  spawns readers.
- **`synth`** — one agent over the notes plus the codebase; emits a digest or a plan.
- **`verify`** — one grep-only agent that fact-checks a digest or plan.

**Two caches, and they are the most dangerous code here.** Both are keyed on a hand-picked subset
of their inputs, and *anything left out of a key fails silently as a free cache hit* — the worst
possible failure mode, because it looks like a saving.

| Cache | Key covers |
|---|---|
| `cache/md` (parsed documents) | PDF bytes, parser choice, `PARSE_VERSION` |
| `cache/notes` (literature notes) | parsed content, `PROMPT_VERSION`, reader model, reader effort, brief, the book indexes offered (their paths and their contents) |

**Bump `PARSE_VERSION` when `clean()` or `validate()` changes. Bump `PROMPT_VERSION` when the
reader prompt or note template changes.** Otherwise users are served output generated under
assumptions that no longer hold.

Other invariants:

- **Agents run with `cwd` set to the output directory**, so every path handed to one must be
  absolute. Relative paths produce an agent that can read nothing and writes an apology.
- **Success is judged by content, not size** (`usable_output`). A model that cannot do the job
  writes a long, fluent explanation that passes any byte threshold. An honest one may say it has
  nothing ("PARTIAL: literature notes unreadable"): a status marker on the opening line (`BLOCKED`,
  `PARTIAL:`, `INCOMPLETE:`, after headings and furniture) fails the stage — only there, because the
  same words lower down are findings, and `synth`/`verify` agents are told to reply
  `INCOMPLETE: <reason>` rather than write a document about failing — their own word wins.
- A failed stage must exit non-zero and cache nothing. A refusal must never become a note.
- **Lacuna replaces a file in a user's directory only if it wrote it** — in the same stage, and for
  the same document (the same bytes) in the case of a note — and nobody has edited it since. The
  evidence is `.lacuna.json` beside the files (`record`, `not_ours`), never the cache: the cache
  knows that Lacuna wrote some note, not for which document, and is deleted or left behind on
  another machine. Anything else exits 2 before any model call, naming each file and the way
  forward (`--overwrite`). The record is locked across processes, re-written for every file an
  `after.*` hook changed, and compared by file identity (`samefile`), not by path, because macOS
  folds case and Unicode form. A plan or report that changes while the model runs is never
  replaced: the result goes beside it and the run exits 2. A file name too long for its directory
  stops the run before the model call; cache names are cut to fit.

### `scripts/lit/prep_md.py`
PDF → reference-stripped markdown. Parses, **validates**, falls back, and refuses. The validation
is the point: a bad parse looks like perfectly good markdown, so silence is not success. Parsers
live in separate virtualenvs under `$LACUNA_DATA` because their dependency trees collide.

**Never run two marker processes concurrently**: they share model servers, and 22% of a document's text went missing while the output still passed validation. See `docs/benchmarks/pdf-parsers.md`.

### `scripts/lit/split_book.py`
Long PDF → per-chapter text files plus `toc.md`, the index a reading agent greps. Must strip
control characters: they make `grep` treat a file as binary, which silently turns a book into
something no agent can search.

### `scripts/lit/lit_cost.py`
A local, free estimate from `calibration.json`. Never calls a model. The shipped calibration is
marked `_source: upstream` and must say so whenever it is used — those numbers are somebody else's
runs on somebody else's hardware.

### `scripts/cost/ccost.py`, `scripts/cost/spend.py`
Parse Claude Code's transcript JSONL — **an internal format, not a contract**. Every figure is an
estimate at list price, never a bill. `ccost.is_avoidable_cold()` is the single definition of a
wasteful cold cache write (a session's first write is unavoidable; later ones mean it sat idle);
both the session and fleet reports must use it (through `avoidable_colds()`), or the same transcript
yields two different counts. A cold write is judged against the previous call on the same thread,
not a fixed threshold: an idle re-cache still reads the ~42k prefix that is warm for everyone.
Every figure is a **lower bound**, and the reports say so: earlier attempts in `usage.iterations`
are shown as the top of a range, because whether they were billed is not recorded.

### `hooks/cache_guard.py`
`SessionStart` and `UserPromptSubmit`. **A hook that raises breaks every prompt in the session**,
so the whole thing is wrapped to exit 0 on any unexpected failure. Its estimate is a heuristic over
undocumented cache behaviour and may be wrong in either direction.

### `hooks/results_hook.py`
Silent unless the session is actually inside the two-pane layout (`CLAUDE_RESULTS_FILE` *and*
`TMUX`).

### `statusline/statusline.py`
Deliberately **self-contained** — one file, standard library only — so it can be copied to a stable
path. Self-contained is not free to differ: it resolves `$LACUNA_DATA`, reads price files and skips
`<synthetic>` lines exactly as `lacuna_config` and `ccost` do, by duplicated code, and its inline
price table is the shipped one. Change one and change the other. A plugin cannot ship a `statusLine`, so it is a documented manual install. It reads fields
that are not a public interface; when one is absent the segment goes blank rather than guessing.

### `skills/`, `agents/`, `commands/`
Markdown instructions for a model, not code. `${CLAUDE_PLUGIN_ROOT}` expands in skill, command and
agent **bodies** — but **not in agent frontmatter**, so an agent's `model:` cannot be configured and
must name a plain alias. The Agent tool's `model:` accepts only `sonnet`, `opus`, `haiku`, `fable` —
a pinned id such as `claude-opus-5` is rejected.

## Testing

Three suites by level, `tests/test_unit.py`, `tests/test_modules.py` and `tests/test_pipeline.py`,
with one fake `claude` in `tests/support.py`, and a mutation list in `tools/mutate.py` that every
test has to justify itself against. **No test may call a model.** [TESTING.md](../TESTING.md) is the
strategy: what belongs at each level, what belongs at none, and how a test earns or loses its place.
`tools/check.sh` runs the suite on every Python on the machine in a scrubbed environment, and
checks that every mutant's anchor still matches; run it, and `tools/mutate.py`, before pushing.

## Where defects keep appearing

Measured, from four independent audits:

1. **Cache keys.** Every defect of this class presents as a free, fast, wrong answer.
2. **Anything judged by size or existence** rather than content.
3. **Relative paths**, because agents run with a different `cwd`.
4. **Things that work here but not from a clean clone** — the shipped example notes were excluded
   by a `.gitignore` rule for days while working perfectly in the author's checkout.
5. **Documentation asserting behaviour nobody executed.** Roughly one claim in three was wrong when
   first checked by running it.
6. **A defect fixed without a test.** Twelve audit fixes had none at the 2026-10-13 review; the suite
   would have let each regress. The revert of a fix is its mutant (`tools/mutate.py`).
