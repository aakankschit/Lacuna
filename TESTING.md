# Testing

How this plugin is tested, what a test has to earn, and the rule the suite is held to. Read this
before adding a test, and before deleting one.

## Three levels

| File | Level | What goes there | How it runs |
|---|---|---|---|
| `tests/test_unit.py` | unit | one function, in process: price lookup, config layering, parse validation and reference stripping, transcript arithmetic, the content check on a model's output, the shipped state of the repo | fast; a temp dir at most |
| `tests/test_modules.py` | module | one script at a time, as its caller runs it: `lit estimate`, `spend`, the status line, the cache guard, `doctor`, `split-book`, `lacuna config`, the pane. Exit codes, the numbers printed, the files written, what it must survive | a subprocess per call, in a sandbox with its own HOME, data directory and PATH |
| `tests/test_pipeline.py` | pipeline | whole journeys through `lacuna lit read / synth / verify` against the fake model: the note cache, the money guarantees, the `after.*` hooks, PDF plumbing | the same sandbox, with the fake `claude` first on PATH and its calls counted |

`tests/support.py` holds the one fake `claude`, the fake parser interpreter and the sandbox. A test
that needs the fake to do something new extends it there rather than writing a second fake.

Put a test at the lowest level that can observe the defect. A cache key that drops an input can only
be seen by running two reads and counting model calls, so it is a pipeline test. A regex that misses
a heading is a unit test; driving the pipeline to reach it would test the same thing more slowly and
say less when it fails.

## What belongs at no level

- **Anything that calls a real model.** Never. Every pipeline and module test runs with a fake
  `claude` first on a PATH of `/usr/bin:/bin`, and `Sandbox.setUp` asserts that the fake is what
  resolves. A test that needs a model's judgement is not a test; it is a benchmark, and those live in
  `docs/benchmarks/` with their cost attached.
- **Anything whose result depends on the machine**: what happens to be installed (`rich`, `tmux`, a
  parser), the user's `~/.lacuna` or `~/.claude`, the path of the checkout. `tools/check.sh` runs the
  suite under `env -i` with a temporary HOME and no PATH beyond `/usr/bin:/bin`, on every Python it
  finds, and CI does the same. A test that goes red for a reason the next person cannot reproduce is
  worse than no test.
- **The text of the source code.** A test that reads a module and asserts a variable name appears in
  an expression passes when the code is wrong and fails when it is merely renamed. Test behaviour.
- **A constant.** That `EX_INPUT` is 2 is proven by every test that asserts exit code 2.
- **Prose in the docs**, except where a claim is machine-checkable and has been wrong before: the
  usage line and the skill naming `--json`, and the example notes being committed.
- **Real parsers.** pymupdf4llm, docling and marker are not installed in CI and must never be run by
  a test. The parse step is driven through a fake interpreter that writes known markdown; the
  validation and cleaning logic is tested in process on fixtures. When `clean()` or `validate()`
  changes, also run `prep_md.py` by hand on real papers and bump `PARSE_VERSION`.

## How a test earns its place

A test is kept because it is the thing that fails when a specific defect is present, and that defect
is one somebody could plausibly ship. The list of those defects is `tools/mutate.py`: each entry is a
one-line change to the code, most of them the exact defect an audit once found, and the runner
applies each to a scratch copy of the repo and runs the whole suite against it.

1. **Every mutant must be killed.** `python3 tools/mutate.py` exits 0 only when every entry makes at
   least one test fail. A mutant that survives is a defect the suite would let ship: add the test, or
   say in the commit why the mutant is not a defect and remove it.
2. **A new test names its mutant.** When you add a test, add the defect it catches to `MUTANTS` and
   confirm the test fails on it. If no one-line mutation makes the test fail, the test is asserting
   something that cannot be wrong, and it does not go in.
3. **A fix gets a mutant.** The revert of your fix is the mutant. This is what makes "fixed" mean
   "guarded": at the 2026-10-13 review, twelve audit fixes had no test, and the suite would have let
   every one of them regress.
4. **A test is deletable when it kills nothing that a test at the same or a lower level does not
   also kill.** `tools/mutate.py --matrix out.json` writes which tests fail for each mutant. A test
   that appears in no mutant's list is dead weight. A test whose every kill is also made by a test
   at its own level or below is redundant. A unit test that overlaps a pipeline test is kept: it is
   the one that says *where* the defect is. Delete after checking the matrix, never on taste. The
   shipped-state tests (manifests, no absolute home paths, example notes committed) have no mutant
   and are kept on the record of what they caught.
5. **Fold, do not multiply.** One test per journey or contract, with subtests for the cases.
   Seventy-three tests for one flag became fourteen with no mutant lost; the matrix is the evidence.

`python3 tools/mutate.py` takes about five minutes; `--only NAME` and `--jobs N` narrow it.

A **stale anchor** — a mutant whose `old` text no longer occurs exactly once, because a fix moved the
code — stops the run with exit 2 before anything is applied, separately from a surviving mutant. At
0.2.0 six were stale and read as six test gaps. `tools/mutate.py --check-anchors` checks only that,
in a second; `tools/check.sh` and CI run it, so a fix that moves guarded code fails at once.
`tools/mutation_battery.py` likewise fails (exit 2) when a mutation cannot be applied, unless it is
given `--allow-skips` to run against an older version of the repo.

## Where defects keep appearing

From four audits and the review that followed, in order: cache keys that omit an input (every one
presents as a free, fast, wrong answer); success judged by size or existence rather than content;
paths that resolve differently for the agent than for the caller; a defect fixed without a test;
documentation asserting behaviour nobody ran. When a change touches one of these, write the pipeline
test first.

## Before pushing

`tools/check.sh` green on every interpreter it finds, and `tools/mutate.py` with every mutant killed.
