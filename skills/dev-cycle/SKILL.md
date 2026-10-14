---
name: dev-cycle
description: Implement a specified change with independent tests — pin a contract, have one agent implement it while another writes adversarial tests blind to the code, then combine, run regression and verify. Use for algorithm, numerical or library changes where being quietly wrong is expensive.
disable-model-invocation: false
---

# Development cycle: contract → parallel implement/test → verify

The expensive failure mode is **code that passes every test and is still wrong**. In a measured
three-way comparison, every implementation passed its own tests, a hidden conformance test and a
326-test regression suite — while one of them had omitted an entire production code path, which made
a documented parameter a silent no-op, and had written a test asserting the parameter has no effect.
So the tests here are written by a different agent that is deliberately kept away from the code.

Routing is measured (`${CLAUDE_PLUGIN_ROOT}/docs/benchmarks/code-writing.md`): the mid tier matches
the strong tier on mathematical fidelity, spec conformance and code quality at a fifth of the cost
and a third of the time. The cheap tier should not write algorithm code.

**The "pay for a strong test author" half of that is not holding up.** On a second task — a CLI
flag rather than a numerical class — mid-tier and cheap-tier test authors given the same contract
caught 21 of the same 22 mutations as the strong tier, at $0.78 and $0.30 against $1.88, and both
caught a defect the strong tier's suite missed. Two tasks is not a result either way. Treat the
tester tier as **an open question**: start with the mid tier, and reach for the strong one when the
thing being tested is numerical or mirrors an existing implementation, which is the only case where
it has ever been shown to help.

## 1. Pin the contract first

Write `CONTRACT.md` before any agent runs. It must fix: module path, class and method names, exact
signatures, attribute names and types, error behaviour, and a numbered list of **binding
requirements** that double as grading criteria.

Resolve contradictions now, not during implementation. Where a design document disagrees with the
code it extends, say which wins and why, in the contract. (Measured instance: a plan document gave a
field a default of `1.0` while the class it extended used `0.0`; every model followed the document,
and the contract had to choose — numerical identity with the existing class won.)

If a requirement says "mirror the existing class", **enumerate** what that includes. Three models
independently missed the same two features of a reference class because the instruction was general.

## 2. Isolate

One `git worktree` per agent, on its own branch:

```bash
git worktree add -b build/<name>-impl  ../wt-<name>-impl  main
git worktree add -b build/<name>-tests ../wt-<name>-tests main
```

**Check how the package imports.** With an editable install pointing elsewhere, a worktree's
`import` resolves to the *other* checkout and both agents will test code they did not write. Verify
with `python -c "import <pkg>; print(<pkg>.__file__)"` inside the worktree, and if it needs
`PYTHONPATH=<worktree>/src`, put that requirement in both agents' prompts.

Record the baseline: run the existing suite on `main` and note the counts, so later failures can be
attributed.

## 3. Implement and test in parallel

- `coder` in the impl worktree: the contract, the reference implementation to mirror, no test files.
- `test-author` in the tests worktree: the contract and the reference only, explicitly told the
  implementation is absent and must not be sought. Expect collection to fail on the missing module;
  require that the file parses and collects.

Run them concurrently — they touch different files in different worktrees.

**Blindness is requested, not enforced.** Worktrees of one repository share a single object store,
so the test author can read the implementation with `git show <impl-branch>:<path>` whether or not
you asked it to. If the result matters, audit the transcript for that afterwards rather than
assuming. Checking took one grep and is the only way to know.

**Isolate the environment too, not just the files.** Two agents sharing `$LACUNA_DATA` — or a
`~/.claude/agents/` file with the same name as a plugin agent, which shadows it — will quietly
influence each other. Give each agent its own data directory, and confirm which agent definition
actually loaded.

**Models.** The agent files carry a plain `sonnet` / `opus` default, because agent frontmatter
cannot read configuration.

If you spawn them with the **Agent tool**, its `model:` accepts only `sonnet`, `opus`, `haiku` or
`fable` — a pinned id such as `claude-opus-5` is **rejected**, and the spawn fails rather than
falling back. So either omit `model:` and accept the frontmatter default, or pass one of those four
aliases.

If you need a pinned version — for a study that has to be reproducible — spawn headless instead,
where any model string works:

```bash
LAC=${CLAUDE_PLUGIN_ROOT}/bin/lacuna
CODER=$($LAC config --json | python3 -c "import json,sys;print(json.load(sys.stdin)['models']['coder'])")
claude -p --model "$CODER" --agents "$(cat agents.json)" --agent coder < prompt.txt
```

Say which route you took. An agent silently running on a different model than the user configured
is the kind of thing that invalidates a comparison without anyone noticing.

**Check the agents can actually run code.** Under a restrictive permission mode a headless agent may
write files but have every `Bash` call refused, so it implements blind and cannot self-verify. Either
grant a mode that permits the test command, or accept that **you** own verification in step 5 — and
say which, because an agent's "I verified this" is worthless when its shell was blocked. Agents
should report the refusal rather than claim success; treat a silent claim of verification with
suspicion.

## 4. Combine and confront

Copy the implementation and the tests onto one branch, then run the independent tests against the
implementation. **Expect failures, and triage each one before fixing anything:**

- a real defect in the implementation → fix the implementation;
- a test asserting something the contract does not pin → fix the test, and note the contract gap;
- a genuine contract ambiguity → decide, record it in the contract, then fix both;
- a requirement the contract's own rules make untestable → amend the contract and say so. (This
  one turns up in practice: a requirement about behaviour no permitted test can observe is not a
  requirement, it is a wish.)

Never resolve a failure by weakening an assertion without naming which of the four it was.

**A green first confrontation is a result, not a formality.** On a well-pinned contract, 69 of 69
independent tests passed on first contact. That means the contract was precise, not that the work
is verified — step 5 is where verification happens, and it is not optional just because nothing
went red.

## 5. Verify

1. The independent tests pass.
2. The full existing suite matches the baseline, counted not eyeballed.
3. **Mutate the implementation and confirm the suite notices.** A suite that passes proves nothing
   on its own. Inject the defect class you are afraid of — make a parameter a no-op, alias a
   caller's dict, make a multi-step update non-atomic, share one state object between keys — run the
   suite, count failures, then restore the file. Any mutation that produces zero failures marks an
   untested path.
4. For each binding requirement, state where it is proven. A requirement with no test is not done.
5. If the work produced written claims, fact-check them: `${CLAUDE_PLUGIN_ROOT}/bin/lacuna lit verify
   --target <summary> --repo <repo>`.

## 6. Hand back

Report: branch name, files added, test counts, baseline comparison, which mutations the suite
caught, cost per stage, and any binding requirement left unmet. **Leave the merge to the user.**

To get cost per stage, read each agent's `total_cost_usd` from its `--output-format json` result.
**Include your own orchestration cost and say so separately** — measured once, it was $2.48 against
$2.08 for both agents combined. A routing decision that counts only the agents is measuring the
smaller half.
