# Extending Lacuna

Nothing in this plugin knows anything about a particular field of research, and that is deliberate.
Everything specific to *your* work goes in a layer on top — your own documents and reference books,
the model choices your material demands, the rules your project follows, and where finished results
have to end up. That layer can be a second (private) plugin, a `.claude/` directory inside your
repo, or just a config file.

**Do not fork this plugin to customise it.** A fork means merging someone else's changes into yours
every time they release.

Instead, use the four things below. They are the parts promised to keep working the same way, so
code you write against them will not break on an update. Anything not listed here can and will
change — the section at the end says which parts those are.

## 1. The data directory

```
$LACUNA_DATA/          default ~/.lacuna — one location, whether called from a hook or a shell
├── config.json               your settings (schema below)
├── pricing.json              optional: your price table, layered over the shipped one
├── calibration.json          cost/time per model × effort, from your own runs
├── cost-ledger.jsonl         one line per pipeline stage: stage, model, cost, wall time, outcome
├── cache/
│   ├── md/                   parsed documents, keyed by content hash
│   └── notes/                notes, keyed by content + model + effort + brief + prompt version + books
├── books/<name>/             chapter files plus toc.md, from `lacuna lit split-book`
└── cache_guard/              per-session hook state
```

The plugin directory itself is **read-only** and is replaced on every update. Nothing you care about
should live there. `lacuna config` prints the effective paths.

## 2. `config.json`

Every key is optional; anything absent falls back to the shipped default. Unknown keys are ignored,
so adding your own is harmless but has no effect.

```json
{
  "models": {
    "reader":   "haiku",
    "digester": "sonnet",
    "planner":  "claude-opus-5",
    "verifier": "haiku",
    "coder":    "sonnet",
    "tester":   "sonnet"
  },
  "effort":  { "reader": "medium", "digester": "medium", "planner": "medium",
               "verifier": "high", "coder": "medium", "tester": "medium" },
  "paths":   { "python": "~/.lacuna/venv/bin/python", "books": "~/.lacuna/books", "cache": "~/.lacuna/cache",
               "results": "~/.claude/results", "ledger": "~/.lacuna/cost-ledger.jsonl" },
  "after":   { "read": "", "synth": "", "verify": "" },
  "budget_warn_usd": 2.0,
  "idle_minutes": 60
}
```

These `paths` are the defaults (with the default data directory), so copying the block changes nothing.
`~` and `$VAR` are expanded; a relative path is taken from the data directory (`$LACUNA_DATA`), never
from the directory you happen to run in. A path must point at what it names: `ledger` is a file,
the others are directories.

Each model value is passed verbatim to `claude --model`, so an alias (`haiku`, `sonnet`, `opus`,
`fable`) or a pinned id (`claude-opus-5`) both work. Prefer an alias unless you need a specific
version pinned; prefer a pinned id when you need reproducibility across a study.

Some keys have an environment override for one-off runs and CI. Environment beats file beats
default, and `lacuna config` marks which layer each value came from. The full list:

| | |
|---|---|
| models | `LACUNA_READER`, `LACUNA_DIGESTER`, `LACUNA_PLANNER`, `LACUNA_VERIFIER`, `LACUNA_CODER`, `LACUNA_TESTER` |
| paths | `LACUNA_PYTHON`, `LACUNA_BOOKS`, `LACUNA_CACHE`, `LACUNA_RESULTS`, `LACUNA_LEDGER` |
| data directory | `LACUNA_DATA` (the one place everything else lives under) |
| cache guard | `LACUNA_IDLE_MIN`, `LACUNA_MIN_CTX`, `LACUNA_GUARD_OFF=1` |

`effort.*`, `after.*` and `budget_warn_usd` are **file-only** — there is no environment override.

## 3. `after.*` — run your own command when a stage finishes

The cleanest seam, and the one to reach for first. Each value is a shell command, run with the
stage's output appended as the final argument:

```json
{ "after": { "synth": "/path/to/my-notebook-import --kind plan",
             "verify": "/path/to/my-notebook-attach" } }
```

The output path is **appended as the final argument**. A command with no arguments of its own
therefore receives it as `$1` — but with the example above (`--kind plan`) the path is `$3`.
Write the importer to read its *last* argument, or keep the command argument-free.

**What it is given differs by stage**, so check before writing an importer:

| Hook | The path argument is |
|---|---|
| `after.read` | the notes **directory** — one note per document inside it |
| `after.synth` | the digest or plan **file** |
| `after.verify` | the `.unverified.md` **file** listing claims that could not be confirmed |

Lacuna never needs to know what a notebook, a wiki or a ticket tracker is. It writes markdown and
tells your command where it is. Non-zero exits are reported, not swallowed, and do not fail the
pipeline stage that already succeeded — by the time a hook runs, the work is done and paid for.

## 4. The `lacuna` CLI

Compose it; do not import from it. The Python modules are not a public API and will be rearranged.

```
lacuna lit read|synth|verify|estimate|split-book
lacuna spend | lacuna doctor | lacuna config | lacuna pane | lacuna split
```

The exit codes are promised to keep their meanings: **0** ok, **2** refused, **3** parse failure,
**4** the model refused or produced nothing. A wrapper can branch on those — for example, retry
stage `read` with a different model on exit 4. Do not retry on 2 without a change: it is bad input,
a file Lacuna cannot show it wrote (`--overwrite` replaces it), or a file another run is writing
(`--overwrite` does not override that), each before any model call; or a file that changed while
its run was in progress, after the call was paid, with the result kept beside it or, for a note, in
the cache. A 0 can carry a warning on stderr: an output whose opening only looks like a failure is
kept, and named, for you to check.

## 5. Skills that name other skills

A project-specific skill can say "run the `lit-review` skill, with the brief set to *X* and books
restricted to *Y*, then do *Z*". Skills in different plugins can refer to each other by name. That
is usually enough, and it keeps your project's own rules in your layer rather than in a fork.

A private plugin layered on this one typically ships: its own skills (your project's rules, status
file discipline, a notebook integration as an `after.synth` command), its `books/` corpus, its own
calibration, and nothing else.

## Using a notes app (Obsidian, Heptabase, Logseq, a wiki)

Two mechanisms, and which one you need depends on how the app stores its data.

**If the app is a folder of markdown files** — Obsidian, Logseq, a git-backed wiki — point the
reader's output straight at it. Nothing to write:

```bash
lacuna lit read papers/*.pdf --out ~/Obsidian/MyVault/Literature/ --brief "<your project>"
```

The vault gets one readable note per document (`author2024.md`), which is yours to edit, link and tag.
Do the same for a plan with `lacuna lit synth --out ~/Obsidian/MyVault/Plans/2026-10-13-plan.md`.

**If the app has its own store** — Heptabase, Notion, a ticket tracker — no path can reach it, so
use an `after.*` hook that calls the app's CLI or API. The hook receives the output and does the
import; Lacuna stays ignorant of the app.

### Do not point the note cache at a vault

`paths.cache` is movable, but a notes app is the wrong destination for it, and the reason is
specific rather than stylistic.

The cache key is a hash of the **inputs** — document content, prompt version, reader model and
effort, brief, the book indexes offered — and never of the note's text. On a hit, the cached file is returned as-is. So if you edit a cached
note in your vault, which is the whole point of having it in a vault, Lacuna will go on serving your
edited text as the model's output indefinitely: nothing invalidates it, because no input changed.
You lose the distinction between what the reader wrote and what you wrote, silently and permanently.

Two lesser reasons: cache filenames carry a hash suffix (`author2024_7f3a9c2b1d4e.md`), which is noise
in a vault; and file-syncing a directory that Lacuna assumes it owns produces `(conflicted copy)`
files inside it.

Treat the cache as disposable — it is a cost optimisation, not a record. Deleting it costs only the
money to re-read. The notes in `--out` are the record.

### Lacuna replaces only its own files

A vault is yours, so `read`, `synth` and `verify` replace a file there only when Lacuna can show it
wrote that file — for the same document, in the case of a note — and nobody has edited it since.
The evidence is `.lacuna.json`, kept beside the files it describes (a dotfile, so Obsidian ignores
it). It travels with the folder, so moving the vault or deleting the cache changes nothing.

When a run would replace a file that fails one of those tests, it exits 2 before any model call
and names every such file: edited since Lacuna wrote it, not recorded as Lacuna's (your own file,
or one written before 0.3.0 kept records), the note for a different document with the same file
name (`main.pdf` from two papers' folders), the note for a document that has changed since (a new
download saved over the old one and a revised copy of the same paper look alike, so both ask), or a
file another stage wrote (a paper named `plan.md` read into the folder that holds your plan). Move
the file or choose another `--out` to keep it; re-run with `--overwrite` to replace it. What an
`after.*` hook does to the files its run wrote (frontmatter it prepends, say) still counts as
Lacuna's output. A change it makes to any other file (an earlier run's note) makes that file yours,
so the next run that would replace it asks: write such a hook to skip a file it has already
changed. A note you restore to exactly that document's cached note is Lacuna's again.

A note, plan or report that changes while its run is in progress is never replaced or deleted.
When the run succeeds, the file is left as it is and the run exits 2 naming it: a plan or report is
written beside it as `<out>.new.md`; a note is in the cache, so the same command with `--overwrite`
puts it there for free. When the run fails, the changed file is left where it is (the model may
have written it, so check it); it is no longer as Lacuna recorded it, so the next run asks before
replacing it. A run never moves, renames or deletes a file it did not write itself: not an edited
note, not a report that describes an older version of the document. A `.new.md` never replaces an
earlier one: the next is `-2`, then `-3`.

Two runs never write one file at once. A run holds each note, plan or report it will write from
the check above to its last write; another run that needs the same file exits 2 before any model
call, naming the run that holds it (its process id). A hold ends when its run ends, however it ends
(a refusal, Ctrl-C, a killed terminal), so nothing a run leaves behind blocks the next one.

Deleting `.lacuna.json` is safe: Lacuna then asks before replacing anything it finds, and adopts a
note again when it is exactly that document's cached note. A damaged one is kept as
`.lacuna.json.damaged-<time>` beside it, the run says so, and a new record is started; the same
then holds, and a later refusal mentions the damaged copy.

### A note on frontmatter

The shipped note template has no YAML frontmatter, so Obsidian will not see properties or tags on a
generated note. If you want them, add them in your own layer with an `after.read` hook that prepends
frontmatter to each file in the directory it is given — rather than editing the template here, which
would change the prompt version and correctly invalidate every cached note you already have.

## Things that are *not* extension points

- **The Python modules.** `lacuna_config`, `lit_pipeline`, `ccost` and the rest will move and rename.
- **Hook ordering.** Plugin hooks are additive but have no guaranteed order; a second plugin's
  `SessionStart` hook must not assume this one ran first. Put anything you depend on in the data
  directory, not in the session environment.
- **The note template and the prompt text.** These change with measurements. When they do,
  `PROMPT_VERSION` in `lit_pipeline.py` is bumped and your note cache correctly misses — so you are
  never silently served notes written under old assumptions.

## Calibrating for your own corpus

The shipped `calibration.json` is marked `"_source": "upstream"` and `lacuna lit estimate` says so every
time it uses it. Those numbers come from someone else's documents on someone else's machine. After a
dozen of your own runs, rebuild it from `cost-ledger.jsonl`: keep the `read` rows with
`wrote_note: true`, group them by `model` and `effort`, take `cost / source_tokens * 1000` as
`usd_per_ktok` and `wall_s / 60` as `minutes_per_paper`, replace the rows, and set `_source` to
anything but `upstream`. Estimates stop being fiction at that point.
