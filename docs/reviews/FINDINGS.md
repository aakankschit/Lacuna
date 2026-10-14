# Findings — Lacuna @ `5d94f97`, tenth defect class

Three reproduced defects, plus a smaller documentation mismatch (§4), none in the nine known classes. All three are present with
`tools/check.sh --quick` green (66 tests, Python 3.12.9). A proposed test for each is in
`testbed/tests/test_tenth.py`. All three tests **fail at HEAD**. With a minimal fix for each
applied in `testbed/`, the full suite passes, **69 tests OK**. The checkout itself is unmodified.

They are ranked by what each costs a user. The first is the tenth class asked for. The other two are
real but smaller, and I have noted where they sit next to a known item.

---

## 1. `spend` (and the status line) prices only the last iteration of a multi-iteration API call. The reported dollars are silently wrong, by up to 78% on a real session.

**Summary.** Claude Code transcripts now carry `message.usage.iterations`: one entry per
sampling iteration inside a single API call. Examples are a model fallback (`message` →
`fallback_message`) and two `message` iterations where the first re-writes the whole context.
In every such line on this machine, the top-level usage equals **the last iteration only**
(329/329 lines). The documented rule is *"to calculate the total tokens consumed and billed for a
request, sum across all entries in the `usage.iterations` array"*. That is the Anthropic compaction
doc as mirrored at docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-compaction.html.
`ccost.split()` reads only the top-level fields, so every earlier iteration costs $0. The same goes
for whichever model ran it.

**Reproduction** (a real transcript copied read-only into `work-spend/iter.jsonl`; fresh HOME and
`LACUNA_DATA`):

```
$ env -i PATH=/usr/bin:/bin HOME=$PWD/work-spend/home LACUNA_DATA=$PWD/work-spend/data.X \
    python3 checkout/scripts/cost/spend.py --session $PWD/work-spend/iter.jsonl --no-color --days 1
■ This session  iter.jso  126 API calls over 41.9 h  ·  opus-5 ×79, opus-4-8 ×47
  $62.65 list price   (same tokens without caching: $222.64)
  █ cache reads $20.49 (33%)   █ cache writes $35.29 (56%)   █ output+thinking $6.86 (11%)

$ python3 -I tools-me/itercost.py checkout/scripts/cost work-spend/iter.jsonl   # same prices, summed over iterations
calls 126  multi-iteration calls 79  top-level-only $62.65  summed over iterations $285.28  missing $222.63 (78%)
```

Token totals for that session:

| | input | output | cache read | cache write |
|---|---|---|---|---|
| top-level only | 1,332 | 274,600 | 41.0M | 2.17M |
| summed over iterations | 50,119 | 274,796 | 51.1M | **25.3M** |

**Independent corroboration from Claude Code itself.** Session `e076f39c…` has one
`fable-5-1 → opus-5` fallback. Its non-final fable iteration (35,036 1h writes + 30,741 reads) prices at
$0.709. Claude Code's own `cost-state` line for that session charges `claude-fable-5-1` **$0.706**.
`spend` attributes $0 to fable and prices the whole call at opus-5 rates.

**How sure I am.** The fallback case is certain: Claude Code bills the dropped iteration.
For the `message`/`message` case, which is the $222 in the table, I am relying on the documented "sum
the iterations" rule. Claude Code's own counter for that session is zeroed (`totalCostUSD: 0`), so it
can't be cross-checked there. Even read conservatively, the figure printed is not "an estimate at list
price" of what was sent. It leaves out tokens the API reported as consumed.

**Where.**
- `scripts/cost/ccost.py:49-61` (`split`) reads `u["input_tokens"]` etc. and never `u["iterations"]`.
- It is used by both `session_report` and `fleet_report` in `scripts/cost/spend.py` (lines 48 and 112). The fleet total,
  the per-model and per-project splits and "same tokens without caching" (spend.py:52-55, also top-level) are all low.
- `statusline/statusline.py:57-81` (`last_turn`) has the same top-level read, so the "last $…" segment undercounts the
  same calls.

**Why the suite misses it.** Every usage fixture (`tests/support.py:97 usage()`, `TestTranscriptMoney`)
has the old flat shape. No file in the repo mentions `iterations`.

**Smallest test** (unit, `TestTranscriptMoney`; fails at HEAD with `0.06 != 1.66`):

```python
def test_a_call_is_priced_over_all_its_iterations(self):
    first = {"type": "message", "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 200_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 200_000}}
    last = {"type": "message", "input_tokens": 0, "output_tokens": 1_000, "cache_read_input_tokens": 200_000,
            "cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    u = {k: v for k, v in last.items() if k != "type"}; u["iterations"] = [first, last]
    # write one assistant line {"id": "m1", "model": "claude-opus-5-5", "usage": u}, then:
    s, _ = ccost.split(ccost.load(p)[0])
    self.assertAlmostEqual(sum(s.values()), (200_000 * 8 + 200_000 * 0.20 + 1_000 * 20) / 1e6, places=6)
```

The mutant is the revert of the fix: drop the `iterations` branch in `split()`. The fix used in
`testbed/` sums `split()` over the iterations, pricing each at `iteration.model or message.model`.
It keeps the last iteration's context as `ctx`, so `cache_guard` and the "ctx" column are unchanged.

---

## 2. After a session-limit or API error, `cache_guard` sees 0 tokens of context and lets the costly re-cache through silently. The status line shows `last $0.000`.

**Summary.** When Claude Code cannot complete a request (session limit, login, safeguard flag), it
writes an assistant line with `model: "<synthetic>"`, `isApiErrorMessage: true` and every usage
field 0. `cache_guard.last_call()` takes the newest main-thread assistant line with a `usage`. That is
the synthetic one, so `ctx = 0`, `ctx >= MIN_CTX` fails, and the guard says nothing. This is the
case the hook exists for: a user stopped by "You've hit your session limit · resets 1am" comes back
hours later, and their first prompt re-writes the whole context.

On this machine, 22 transcripts contain such lines. Two end on a session-limit line, sitting on 108k
and 549k tokens of real context.

This is distinct from known item 7 (`spend` dividing by zero on such a transcript). That is a crash
in another file, and guarding the division does not touch this.

**Reproduction.** The hooks subagent found this; I re-ran it myself. `limit.jsonl` is a real
transcript ending on a session-limit error. `limit_nosynth.jsonl` is the same file with **only** the 4
`<synthetic>` lines removed (checked with `diff`):

```
$ for t in limit limit_nosynth; do D=$(mktemp -d); echo "== $t"
    printf '{"session_id":"v-%s","transcript_path":"%s","cwd":"/tmp","hook_event_name":"UserPromptSubmit","prompt":"continue"}' $t $W/$t.jsonl \
    | env -i PATH=/usr/bin:/bin HOME=$HOME LACUNA_DATA=$D python3 checkout/hooks/cache_guard.py | cut -c1-200; done
== limit
== limit_nosynth
{"decision": "block", "reason": "cache_guard held this prompt. The session was idle 1.5 h, longer than the 60-min cache lifetime, so sending it re-caches all 549k tokens of context first (≈$10.98
```

The status line on the same pair, as reported by the subagent and not re-run by me:
`last $0.000 (read 0.000 · write 0.000 · out 0.000)` against `last $0.345 …`.

**Where.** `hooks/cache_guard.py:103` (in `last_call`, line 88) and `statusline/statusline.py:73` (in `last_turn`, line 57). Both loops skip
non-assistant, usage-less and sidechain lines, but not `isApiErrorMessage` / `<synthetic>`.

**Why the suite misses it.** `TestCacheGuard.transcript()` (`tests/test_modules.py:518`) only
builds real calls, plus compaction and summary extras. Nothing in the repo mentions `synthetic` or
`isApiErrorMessage`.

**Smallest test** (module, `TestCacheGuard`; fails at HEAD with `None != 'block'`):

```python
def test_a_session_limit_line_does_not_hide_the_context(self):
    then = datetime.now(timezone.utc) - timedelta(hours=3)
    real = call("m1", "claude-opus-5-5", then, usage(0, 1_000, 99_000, 500))
    err = call("e1", "<synthetic>", then + timedelta(seconds=5), usage(0, 0, 0, 0))
    err["isApiErrorMessage"] = True
    self.assertEqual(json.loads(self.prompt(jsonl(self.p("t.jsonl"), [real, err])).stdout)["decision"], "block")
```

---

## 3. A `~` in `LACUNA_DATA` splits the data directory in two. The user's `config.json` is silently ignored and `doctor` creates a literal `./~/` in the project.

**Summary.** `lacuna_config.py:19` takes `LACUNA_DATA` verbatim. The default `paths` derived from it
(cache, books, ledger, parser venv) are later passed through `expanduser` (line 136), so they land in
`$HOME/lacuna/…`. `config.json` and `pricing.json` (lines 120, 189, 198) and everything `doctor` creates
(doctor.py:92-108) use the unexpanded string, which is relative to the cwd.

A literal tilde is easy to get: `export LACUNA_DATA="~/lacuna"` (quoted), a `.env` file, or any
`env` block in a JSON config. Nothing warns, and `doctor` reports every step `[ok]`.

**Relation to known item 6.** That item is about relative paths reaching the agent. The obvious fix
is `abspath(DATA)`. That fix would make this split consistent but still wrong: `abspath("~/lacuna")`
is `<cwd>/~/lacuna`, so the user's real `~/lacuna/config.json` would still never be read. The fix
needs `expanduser` here. I'm listing this third because it sits next to item 6. If the item-6 fix
already uses `expanduser`, drop it.

**Reproduction** (the subagent found this; I re-ran both commands):

```
$ mkdir -p v1/home/lacuna v1/proj; echo '{"models":{"reader":"sonnet"}}' > v1/home/lacuna/config.json
$ cd v1/proj && env -i PATH=/usr/bin:/bin HOME=$PWD/../home LACUNA_DATA='~/lacuna' ../../checkout/bin/lacuna config
data dir   ~/lacuna   (missing — run `lacuna doctor`)
cache      …/v1/proj/../home/lacuna/cache
roles      reader=haiku  digester=sonnet  …          ← config.json says sonnet; no [config.json] tag

$ cd v2/proj && env -i … LACUNA_DATA='~/lacuna' ../../checkout/bin/lacuna doctor; find . -maxdepth 3
./~/lacuna/cache  ./~/lacuna/calibration.json  ./~/lacuna/config.json  ./~/lacuna/books
```

The `$HOME` side gets nothing from `doctor`, yet the pipeline's `CACHE` points there. There is also
now a directory named `~` inside the user's repo, and `rm -rf ~` is one slip away.

**Why the suite misses it.** `Sandbox.env()` (`tests/support.py`) always passes an absolute
`LACUNA_DATA`. `test_paths_expand_tilde_and_variables` covers `~` only inside `config.json` `paths`.

**Smallest test** (module, `TestConfigReport`; fails at HEAD with `'haiku' != 'from-file'`):

```python
def test_a_tilde_in_LACUNA_DATA_is_the_home_directory(self):
    write(os.path.join(self.home, "lac", "config.json"), json.dumps({"models": {"reader": "from-file"}}))
    r = self.lacuna("config", "--json", LACUNA_DATA="~/lac")
    self.assertEqual(json.loads(r.stdout)["models"]["reader"], "from-file")
    self.assertFalse(os.path.exists(self.p("~")))
```

---

## What I attacked that held, or that I'm not reporting

- **Cross-file double counting in `spend`'s fleet report** (resumed or forked sessions repeating message ids). It
  is real but tiny: 17 of 4,787 ids over 14 days. I'm not reporting it.
- **`hooks.json` schema, hook output shapes, SessionStart resume fields, `Z` timestamp parsing on 3.9+**:
  all match what Claude Code expects (hooks subagent, checked against the 2.1.299 binary's schema).
- **Price-table coverage**: every model in my transcripts (`opus-5`, `opus-5-5`, `sonnet-5-5`, `haiku-5-5`,
  `fable-5-1`, `opus-4-8`, `haiku-4-5-…`) matches a key. `<synthetic>` falls back but has zero usage.
- **Not counted.** The skills subagent noted that a renamed copy of a document is re-read and paid for again (cache filename is the stem). That is the same root as known item 3, so it is not counted. The README `--install-equations` path fails loudly (exit 3), not silently.
- **Non-object `$LACUNA_DATA/pricing.json`** (e.g. `[]`): the config subagent says `_price_table` would raise,
  against the "never raise" rule. It read this in the code and did not run it, so it is unverified and not listed.
- **`lacuna config set` races**: there is no write path (`bin/lacuna` has no `config set`).

---

## 4. Smaller: the docs tell a model and a user that an `after.*` hook gets the output path as `$1`, but the docs' own example puts it in `$3`. The importer misfires and the stage still exits 0.

The skills subagent found this. I verified it by reading the code and the docs. I did not re-run its fake-`claude` synth.

- **What the docs say.** `docs/extending.md:76-77` says each hook runs "with the stage's output appended as `$1`". The example
  directly below is `"synth": "/path/to/my-notebook-import --kind plan"`. The table at :84-89 says "`$1` is … the digest or plan file".
  `skills/lit-review/SKILL.md:131-132`, which is the text the model reads, says "each runs with the output path as `$1`".
- **What the code does.** `scripts/lit/lit_pipeline.py:132` runs `sh -c cmd + ' "$1"' sh <out>`. The path is the *last*
  argument, so the documented example receives `$1=--kind $2=plan $3=<out>`. That is deliberate and pinned by
  `TestAfterHooks.test_shell_semantics` (test_pipeline.py:237). README.md:162-164 describes it correctly.
- **Reproduction** (subagent run, with an importer that checks `$1` the way the table describes):
  `lacuna lit synth … --kind plan` prints `after_synth: exit 1 — import: '--kind' is not a file`, `exit=0`, and
  `inbox/` stays empty.
- **Why the suite misses it.** No test reads the extending.md example.
- **Smallest test.** Load the JSON block from extending.md §3, substitute `self.hook()` for the path, run
  `L.after("synth", out)`, and assert that the recorded first argument equals `out`.
- **Fix.** Change the docs, not the code: "appended as the last argument", plus an example of the form `"cmd \"$1\" --kind plan"`.

Also confirmed: `scripts/panes/protocol.md:10,12` reaches the model through `hooks/results_hook.py` with
`${CLAUDE_PLUGIN_ROOT}` still literal. The hook does a `.replace("RESULTS_PATH", …)` only. I ran it with the variable set,
and the injected context still contains `${CLAUDE_PLUGIN_ROOT}/bin/lacuna pane …`. A user who copies that command runs
`/bin/lacuna`. The impact is low, because `lacuna split` has already opened the pane by that point.


## Spend

- **Subagents.** Three Opus 5.5 subagents at high effort, each with a narrow brief: hooks/status line,
  skills/agents vs code, config/doctor/data dir. Tokens: hooks ≈61k, config ≈57k, skills ≈150k (≈270k total).
- **No real model calls.** The subagents and the tests used no real model, and every test and repro used a
  fake or no `claude`.
- **Web.** One web search and one fetch, for the `usage.iterations` billing rule.
- **Transcripts.** Read only, from `~/.claude/projects`. Samples were copied into `work-hooks/` and `work-spend/`.
- **Untouched.** Nothing in `~/.lacuna`, `~/.claude/settings.json` or `~/Desktop/Lacuna` was touched. Nothing
  was pushed. Note that `checkout/` was missing at start, so I cloned it from GitHub at `5d94f97`.
- **Not re-run.** I did not re-run `tools/mutate.py` or the repo's `mutation_battery.py`.
