<p align="center">
  <img src="assets/lacuna-logo.jpeg" alt="Lacuna — papers to code" width="300">
</p>

# Lacuna

> *lacuna* — a gap; the thing a literature has not yet answered.

**Papers to code.** A Claude Code plugin for scientific software development: it reads a body of
literature for the **open problems** in it, turns those into development proposals against your
actual codebase, fact-checks them, and then builds one with tests written independently of the code.

Its purpose is to let you **develop while staying aware of what the development costs**. Every stage
reports what it spent and appends to a cost ledger; `/spend` shows where a week went and names the
avoidable part; the status line keeps the running total and the cache state on screen. Which model
does which job is a setting you control, and the defaults come from measuring the trade-off rather
than assuming it.

## The workflow

| Stage | Command | What happens | Typical cost |
|---|---|---|---|
| **Estimate** | `lacuna lit estimate papers/*.pdf` | counts tokens locally and prints a cost table — no API calls | **free** |
| **Read** | `lacuna lit read papers/*.pdf --out notes/` | one cheap agent per document, in parallel; notes cached by content, so a document is never read twice | ~$0.015/doc |
| **Synthesise** | `lacuna lit synth --notes notes/ --repo . --out plan.md --kind plan` | ranked development proposals against your real files, with an authoritative file list so it cannot invent paths | $0.53 – $1.44 |
| **Verify** | `lacuna lit verify --target plan.md --repo . --notes notes/` | greps every path, number and attribution; reports only what it could not confirm | $0.02 – $0.09 |
| **Build** | the `dev-cycle` skill | pin a contract, implement and write tests in parallel with the test author blind to the code, then confront and mutate | varies |
| **Account** | `/spend` | this session and the last 7 days, split by cache read / cache write / output | **free** |

Two supporting pieces run on their own: a **cache guard** that warns before an idle session charges
you a cold cache rewrite, and an optional **results pane** that keeps chat short while elaborated
results go to a rendered markdown file.

Every default in that table is measured. One of those measurements is below; the rest, with
protocols and sample sizes, are in [docs/benchmarks/](docs/benchmarks/) — read
[docs/LIMITS.md](docs/LIMITS.md) before repeating any of their numbers.

## Install

```bash
claude plugin marketplace add https://github.com/aakankschit/Lacuna.git
claude plugin install lacuna@lacuna --scope user
```

`claude plugin marketplace add` takes a URL, a local path or a GitHub `owner/repo`, so
`claude plugin marketplace add aakankschit/Lacuna` is equivalent and clones over HTTPS.

Then, in Claude Code:

```
/lacuna-doctor   # checks the environment, creates the data directory, says what is missing
/spend           # costs nothing and needs nothing — start here
```

(The command is `/lacuna-doctor` rather than `/doctor` because Claude Code already has a `/doctor`
of its own. If another plugin ever claims `/spend`, `/lacuna:spend` disambiguates.)

### Reaching the `lacuna` command from a terminal

Skills and commands find the plugin by themselves. For your own shell, the install path **contains
the version number** and therefore changes on every update, so resolve it rather than hardcoding it:

```bash
claude plugin list --json | python3 -c \
  "import json,sys;[print(p['installPath']) for p in json.load(sys.stdin) if p['id'].split('@')[0]=='lacuna']"
```

Worth putting in `~/.zshrc` or `~/.bashrc` so it keeps working after an upgrade:

```bash
lacuna() {
  local root
  root=$(claude plugin list --json | python3 -c \
    "import json,sys;[print(p['installPath']) for p in json.load(sys.stdin) if p['id'].split('@')[0]=='lacuna']")
  "$root/bin/lacuna" "$@"
}
```

### Dependencies

Python 3.9+ and the `claude` CLI are the only hard requirements, and only PDF parsing needs
anything beyond them:

```bash
lacuna doctor --install-pdf          # pymupdf4llm, the default parser (~4 s/document)
lacuna doctor --install-fallback     # docling, the structured fallback (~73 s/document)
lacuna doctor --install-equations    # marker, for LaTeX fidelity — ~2 GB of models,
                                     #   and needs llama.cpp on PATH (brew install llama.cpp)
```

Each goes in its own virtual environment under the data directory, because their dependency trees
collide. The fallback and equation parsers are opt-in and `doctor` never installs them quietly.

## First run

Nothing here needs a PDF or a parser. Only the last two commands call a model, for about
$0.31 together.

```bash
# with the shell function above, or $LAC set to <installPath>/bin/lacuna
lacuna doctor                                # what is installed, what is missing, how to fix it
lacuna spend --days 7                        # free: where last week's money actually went
lacuna config                                # which model is set for each job

# the shipped example lives in the plugin; run it from there
cd "$(claude plugin list --json | python3 -c \
  "import json,sys;[print(p['installPath']) for p in json.load(sys.stdin) if p['id'].split('@')[0]=='lacuna']")"
lacuna lit synth --notes examples/notes --repo examples/demo-repo --out /tmp/plan.md \
    --kind plan --objective "What should we build next?"
lacuna lit verify --target /tmp/plan.md --repo examples/demo-repo --notes examples/notes
```

That last pair is worth running in order. On the example, `synth` cost $0.302 and `verify` cost
$0.0099 — and `verify` found four over-claims in the plan `synth` had just written, including a
finding attributed to the wrong source. **`verify` is not optional.** The dominant error is a true
claim pinned to the wrong document, and a few cents catches it.

## Examples

Customisation is these commands plus one settings file. Nothing below edits the plugin.

**Run it on your own papers and your own repo**

```bash
lacuna lit estimate ~/papers/*.pdf --per-agent 1          # free: what this will cost
lacuna lit read ~/papers/*.pdf --out ~/project/lit/notes/ \
    --brief "Project: a Python library for X. We care about Y."
lacuna lit synth --notes ~/project/lit/notes/ --repo ~/project --out ~/project/lit/plan.md \
    --kind plan --objective "What should the next release do?"
lacuna lit verify --target ~/project/lit/plan.md --repo ~/project --notes ~/project/lit/notes/
```

**Change which model does a job** — for one run, or permanently

```bash
LACUNA_PLANNER=fable lacuna lit synth --notes notes/ --repo . --out plan.md --kind plan
lacuna config                      # shows every role and where its value came from
```

Permanently, in `~/.lacuna/config.json` (strict JSON — no comments, or the whole file is ignored):

```json
{ "models": { "planner": "claude-opus-5", "reader": "haiku" } }
```

**Add reference books the readers can consult**

```bash
lacuna lit split-book ~/books/bayesian-optimization.pdf ~/.lacuna/books/bayes-opt
lacuna lit read papers/*.pdf --out notes/      # books are offered automatically
lacuna lit read papers/*.pdf --out notes/ --no-books    # or skip them
```

**Write notes straight into Obsidian** (any vault is just a folder of markdown)

```bash
lacuna lit read papers/*.pdf --out ~/Obsidian/MyVault/Literature/ --brief "..."
```

**File finished results into an app that is not a folder** (Heptabase, Notion, a tracker)

In `~/.lacuna/config.json`. Your command runs through a shell, so `~` and `$VAR` work, and the
output path is appended as the final argument — so a command with no arguments of its own
receives it as `$1`:

```json
{ "after": { "synth": "~/bin/import-to-notebook --kind plan" } }
```

**Read a document with equations faithfully**

```bash
lacuna doctor --install-equations
lacuna lit read theory-paper.pdf --out notes/ --equations
```

## One experiment, in full

The design that set the reading defaults. Everything else is in
[docs/benchmarks/](docs/benchmarks/).

**Question.** Does it matter which model reads a research paper, and does raising its reasoning
effort buy anything?

| | |
|---|---|
| Task | extract the **open problems** a paper leaves, including ones the authors do not state |
| Documents | 2 research papers, 33,461 tokens of text in total |
| Varied | 3 model tiers × 5 reasoning-effort levels, **2 repeats each** = 30 runs |
| Held constant | same prompt, same note template, same slim agent (Read/Grep/Write) |
| Judged | outputs anonymised, scored by 2 models from different tiers against a written rubric; every figure is the mean of 2 repeats × 2 judges. Recall is against a hand-built list of 15 gaps |

**Result 1 — quality barely moves across tiers.**

| | cheap | mid | strong |
|---|---|---|---|
| Open-problem recall | 0.48 | 0.54 | 0.53 |
| Viable proposals | 8 | 10 | 14 |
| Mechanism-level proposals | 1 | 1 | 4.3 |

**Result 2 — cost moves enormously.** Dollars to read the same two papers, from the calibration this
plugin ships, which `lacuna lit estimate` will print for you:

| tier | low | medium | high | xhigh | max | max ÷ low |
|---|---|---|---|---|---|---|
| cheap | $0.011 | **$0.018** | $0.059 | $0.123 | $0.516 | **47×** |
| mid | $0.174 | **$0.181** | $0.221 | $0.539 | $2.302 | 13× |
| strong | $0.563 | **$0.570** | $0.979 | $1.029 | $0.737 | 1.3× |

**What changed because of it.** Spotting that a paper leaves a question open does not depend on the
model, so the cheap tier reads and the pipeline fixes effort at medium — the bolded column. The
strong tier's real advantage is *proposals*, not reading, so it is spent on synthesis instead.
Reading two papers costs 1.8 cents rather than 57, and nothing measured got worse.

**What it does not show.** Two papers, one field, one person's reference list of 15 gaps.
The strong tier's edge on mechanism-level proposals is real in this sample and untested elsewhere.
Costs are list price, not your bill. [docs/LIMITS.md](docs/LIMITS.md) is the honest version.

## Prices

`scripts/pricing.json` carries a **dated** price table with its source. Prices rot; drop your own
copy in the data directory rather than waiting for a release. Every dollar figure this plugin prints
is **list price, not your bill** — it does not model subscriptions, included usage, rate limits or
batch discounts.

## Making it your own

Lacuna allows users to personalize its setup. To tailor it for your use case, it is recommended
that you add external data at these junctions in the codebase — four points that exist so your
material and the pipeline can meet without either one having to change the other.

Everything you supply stays *outside* this repository. That is the point of having junctions at
all: the alternative is editing Lacuna's own files, and then every release either overwrites your
changes or leaves you merging somebody else's project into yours indefinitely.

`lacuna config` prints what is currently in effect and which layer each setting came from —
built-in default, your config file, or an environment variable — so you can always see what the
pipeline will actually do before it spends anything.

### 1. The data directory — `~/.lacuna/`

Where your external data lives: reference books split into chapters, your cost calibration, the
note cache, the cost ledger. `lacuna doctor` creates it. A plugin update replaces the plugin folder
and never touches this one, so nothing you put here is at risk from an upgrade.

```bash
lacuna lit split-book ~/books/bayesian-optimization.pdf ~/.lacuna/books/bayes-opt
export LACUNA_DATA=~/Dropbox/lacuna        # or keep it in sync / under version control
```

Readers are offered any book with a `toc.md`, and grep the index rather than reading chapters
whole. Measured effect: books do not produce more ideas, but they roughly halve factual errors.

### 2. `config.json` — which model does what, and what it may spend

Six roles, each taking any string `claude --model` accepts — an alias (`haiku`, `opus`, `fable`) or
a pinned id (`claude-opus-5`) when a study needs reproducibility.

```json
{
  "models": { "reader": "haiku", "digester": "sonnet", "planner": "claude-opus-5",
              "verifier": "haiku", "coder": "sonnet", "tester": "claude-opus-5" },
  "paths":  { "books": "~/.lacuna/books" },
  "budget_warn_usd": 2.0
}
```

`reader`, `digester`, `planner` and `verifier` are read by the pipeline. `coder` and `tester` are
read by the `dev-cycle` skill, which passes them when it spawns those agents — the agent files
themselves carry a plain `sonnet` / `opus` default. `budget_warn_usd` makes `lacuna lit estimate`
flag any combination that would cost more.

Change a role when your material demands it — some models refuse some subject matter in headless
runs, and pinning the planner to one that does not is a one-line fix rather than a workaround.
`LACUNA_PLANNER=fable lacuna lit synth ...` overrides any of it for a single run.

### 3. `after.read` / `after.synth` / `after.verify` — where results end up

A command of *yours*, run when a stage finishes, with the output as its argument. This is how
results reach a lab notebook, a ticket, a commit, or a Slack channel; Lacuna never learns what any
of those are.

```json
{ "after": { "synth": "~/bin/file-into-notebook --kind plan" } }
```

`after.read` is given the notes **directory**; `after.synth` and `after.verify` are given a
**file**. A hook that fails is reported and the run continues — by the time it fires, the work is
done and already paid for.

If your notes app is a plain folder of markdown — Obsidian, Logseq, a wiki — you need no hook at
all: point the reader straight at it with `--out ~/Vault/Literature/`.

### 4. The `lacuna` command — the surface your own scripts call

```bash
lacuna lit read papers/*.pdf --out notes/ --brief "..."   # exit 4 = a model refused
```

Call the CLI; do not import Python from inside this repo. Exit codes are stable (0 ok, 2 bad input,
3 parse failure, 4 the model refused or produced nothing) so a wrapper can branch on them — retry a
refused document on a different model, say. The modules behind the CLI will be renamed.

---

[docs/extending.md](docs/extending.md) specifies all four in full, and — the half that matters more
— says what is deliberately *not* stable, so you can tell what is safe to build on.

A worked example: the author's own chemistry project is a separate private plugin that points
`books` at its corpus, pins `planner` to a model that does not refuse its source material, and sets
`after.synth` to a script filing each plan into a notebook app. It changes no file in this repo.

## What the name does not promise

The name is an aspiration about *where to look*, not a claim to have automated insight.

Lacuna produces notes, a digest, a plan and a fact-check. The fact-check catches invented paths,
misquoted numbers and mis-attributions. **It does not catch a bad idea**, and nothing here replaces
reading the document when the stakes are high.

The honest number: in the benchmark, the open problems that later turned into real features were
surfaced in **0–12% of notes**. What this buys is coverage at a price low enough to read a whole
reading list instead of three papers — the gap you would have found anyway, found sooner, alongside
forty you will discard. Deciding which gap is worth a month of work is still yours.

MIT licensed. Developed on macOS; Linux should be fine; Windows is untested.
