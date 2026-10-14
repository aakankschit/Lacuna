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
| `cache/notes` (literature notes) | parsed content, `PROMPT_VERSION`, reader model, reader effort, brief, a fingerprint of the books offered |

**Bump `PARSE_VERSION` when `clean()` or `validate()` changes. Bump `PROMPT_VERSION` when the
reader prompt or note template changes.** Otherwise users are served output generated under
assumptions that no longer hold.

Other invariants:

- **Agents run with `cwd` set to the output directory**, so every path handed to one must be
  absolute. Relative paths produce an agent that can read nothing and writes an apology.
- **Success is judged by content, not size** (`usable_output`). A model that cannot do the job
  writes a long, fluent explanation that passes any byte threshold.
- A failed stage must exit non-zero and cache nothing. A refusal must never become a note.

### `scripts/lit/prep_md.py`
PDF → reference-stripped markdown. Parses, **validates**, falls back, and refuses. The validation
is the point: a bad parse looks like perfectly good markdown, so silence is not success. Parsers
live in separate virtualenvs under `$LACUNA_DATA` because their dependency trees collide.

**Never run two GPU parsers concurrently** — see `docs/benchmarks/pdf-parsers.md`.

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
both the session and fleet reports must use it, or the same transcript yields two different counts.

### `hooks/cache_guard.py`
`SessionStart` and `UserPromptSubmit`. **A hook that raises breaks every prompt in the session**,
so the whole thing is wrapped to exit 0 on any unexpected failure. Its estimate is a heuristic over
undocumented cache behaviour and may be wrong in either direction.

### `hooks/results_hook.py`
Silent unless the session is actually inside the two-pane layout (`CLAUDE_RESULTS_FILE` *and*
`TMUX`).

### `statusline/statusline.py`
Deliberately **self-contained** — one file, standard library only — so it can be copied to a stable
path. A plugin cannot ship a `statusLine`, so it is a documented manual install. It reads fields
that are not a public interface; when one is absent the segment goes blank rather than guessing.

### `skills/`, `agents/`, `commands/`
Markdown instructions for a model, not code. `${CLAUDE_PLUGIN_ROOT}` expands in skill, command and
agent **bodies** — but **not in agent frontmatter**, so an agent's `model:` cannot be configured and
must name a plain alias. The Agent tool's `model:` accepts only `sonnet`, `opus`, `haiku`, `fable` —
a pinned id such as `claude-opus-5` is rejected.

## Testing

| Suite | What it covers |
|---|---|
| `tests/test_lacuna.py` | configuration, pricing, cache keys, hooks, the CLI contract |
| `tests/test_coverage.py` | per-module behaviour: spend, prep_md, split_book, doctor, statusline |
| `tests/test_integration.py` | whole flows end to end against a fake `claude`: the full read→synth→verify journey, the money guarantees (bad input never reaches a model; a refusal is never filed as output), cache behaviour, `after.*` hooks, and that the cost tools work with nothing installed |

**No test may call a model.** Tests that drive the pipeline put a fake `claude` first on a
restricted `PATH` and assert it resolved to the fake before running. That is what lets a test prove
bad input exits *before* any money is spent, and that a cache hit really is free.

**Tests must not depend on what happens to be installed** — not `rich`, not `tmux`, not a parser,
not the user's own transcripts or home directory. A test whose result changes with the machine is
worse than no test: it goes red in CI for reasons nobody can reproduce.

Every test should be able to name the wrong implementation it would catch. The suites have been
mutation-tested; keep that true by injecting the defect you are guarding against and confirming
the test fails.

## Where defects keep appearing

Measured, from four independent audits:

1. **Cache keys.** Every defect of this class presents as a free, fast, wrong answer.
2. **Anything judged by size or existence** rather than content.
3. **Relative paths**, because agents run with a different `cwd`.
4. **Things that work here but not from a clean clone** — the shipped example notes were excluded
   by a `.gitignore` rule for days while working perfectly in the author's checkout.
5. **Documentation asserting behaviour nobody executed.** Roughly one claim in three was wrong when
   first checked by running it.
