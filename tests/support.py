"""Shared fixtures: a fake `claude`, a fake parser interpreter, and a sandbox per test.

No test may reach a real model. Every sandbox puts the fake `claude` first on a PATH that holds
nothing else but /usr/bin and /bin, and asserts that it is what resolves before running anything.
The fake logs each call, which is how a test proves that bad input exits *before* money is spent
and that a cache hit really is free.
"""
import json, os, re, shutil, stat, subprocess, sys, tempfile, unittest
from datetime import timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LACUNA = os.path.join(ROOT, "bin", "lacuna")
SCRIPTS = os.path.join(ROOT, "scripts")
LIT = os.path.join(SCRIPTS, "lit")
PY = sys.executable
SAFE_PATH = "/usr/bin:/bin"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
for p in (SCRIPTS, os.path.join(SCRIPTS, "cost"), LIT):
    sys.path.insert(0, p)

# The fake model. Logs argv and prompt, then does what FAKE_MODE says: "ok" writes a plausible note
# or plan so the pipeline's own bookkeeping is exercised; "refuse" and "garbage" are the two ways a
# real run fails; "apology" is the dangerous one — a long, fluent failure that clears a size check.
# "reply" is how a real model declines: is_error false, FAKE_REPLY as its text, nothing written.
# "notdict" does the work but replies with JSON that is not an object; "hang" does the work and
# never returns, so a test can interrupt a run part-way. "incomplete" is a model whose sources are
# unreadable: it writes a plausible note or plan anyway and, only if the prompt told it the INCOMPLETE
# rule, says in its reply that it could not read them — under a line of prose, as a real planner did.
# Not told, it replies as usual, as a real reader did. "clobber" writes its plan over --out itself
# instead of the .partial it was given.
# "body" writes exactly FAKE_BODY to the note or plan path and replies FAKE_REPLY (default: the path).
FAKE_CLAUDE = r'''#!{py}
import json, os, re, sys, time
prompt = sys.stdin.read()
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({{"argv": argv, "prompt": prompt}}) + "\n")
mode = os.environ.get("FAKE_MODE", "ok")
gave_up = mode == "incomplete" and "`INCOMPLETE: <what you could not read>`" in prompt
GAVE_UP = "The text I was given is symbol noise; no sentence survives.\nINCOMPLETE: the sources could not be read"
_d = float(os.environ.get("FAKE_DELAY", 0))      # lets a test overlap two real processes
if _d:
    import time as _t; _t.sleep(_d)
if mode == "refuse":
    print(json.dumps({{"is_error": True, "result": "I can't help with that.", "total_cost_usd": 0.002}}))
    sys.exit(0)
if mode == "garbage":
    print("overloaded, not json")
    sys.exit(1)
if mode == "reply":
    print(json.dumps({{"is_error": False, "result": os.environ["FAKE_REPLY"], "total_cost_usd": 0.002}}))
    sys.exit(0)

def done(reply):
    if mode == "hang":
        time.sleep(60)
    print(json.dumps([reply] if mode == "notdict" else reply))
    sys.exit(0)

note = re.search(r"(?m)^- (\S+) -> (\S+)$", prompt)
plan = re.search(r"Write the result to (\S+)\.", prompt)
target = (note and note.group(2)) or (plan and plan.group(1))
if mode == "clobber" and plan:
    target = target[:-len(".partial")]
if mode == "body" and target:      # exactly FAKE_BODY, so a test controls a document's opening
    with open(target, "w") as f:
        f.write(os.environ["FAKE_BODY"])
    done({{"is_error": False, "result": os.environ.get("FAKE_REPLY", target), "total_cost_usd": 0.01}})
if mode == "apology" and target:
    with open(target, "w") as f:
        f.write("# Plan\n\nBLOCKED - not completed. I could not read any of the supplied files.\n\n"
                + ("Explanation of the blockage. " * 120))
    print(json.dumps({{"is_error": False, "result": "done", "total_cost_usd": 0.01}}))
    sys.exit(0)
if mode == "prose" and target:      # fluent, long, no apology, and not a note: only its shape tells
    with open(target, "w") as f:
        f.write("The paper discusses its subject at length, and this summary follows it closely. " * 30)
    print(json.dumps({{"is_error": False, "result": "done", "total_cost_usd": 0.01}}))
    sys.exit(0)
if prompt.startswith("Fact-check"):
    print(json.dumps({{"is_error": False, "total_cost_usd": 0.0012,
                      "result": GAVE_UP if gave_up else "No unverifiable claims found."}}))
    sys.exit(0)
if note:
    model = argv[argv.index("--model") + 1]
    brief = next((l for l in prompt.splitlines() if l.startswith("BRIEF:")), "BRIEF:none")
    books = "BOOKS-OFFERED" if "Textbooks you may consult" in prompt else "NO-BOOKS"
    with open(note.group(1), errors="ignore") as f:
        title = f.readline().strip()      # so two papers with one file name get different notes
    with open(target, "w") as f:
        f.write("# A note ({{}})\n\n## Claim\n{{}}\n{{}}\n{{}}\nmodel={{}}\n\n## Key results\n".format(
            os.path.basename(note.group(1)), title, brief, books, model) + ("Finding with a number, 42 units. " * 40))
    done({{"is_error": False, "result": GAVE_UP if gave_up else target, "total_cost_usd": 0.012}})
if plan:
    objective = next((l for l in prompt.splitlines() if l.startswith("Objective:")), "")
    with open(target, "w") as f:
        f.write("# Development plan\n\n{{}}\n\n## 1. A real proposal\n\n".format(objective)
                + ("Substantive plan content naming src/widget/sampler.py. " * 60))
    done({{"is_error": False, "total_cost_usd": 0.3,
           "result": GAVE_UP if gave_up else target}})
print(json.dumps({{"is_error": False, "result": "nothing to do", "total_cost_usd": 0.0}}))
'''

# Stands in for the parser venv's python: `<venv-python> -I prep_md.py <pdf> <md> [flags]`.
FAKE_PARSER_PY = r'''#!/bin/sh
echo "$@" >> "$FAKE_PARSE_LOG"
if [ -n "$FAKE_PARSE_FAIL" ]; then echo "parse exploded" >&2; exit 1; fi
printf '# Parsed\n\n## Abstract\n\nParsed body.\n' > "$4"
'''


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)
    return path


def read(path):
    with open(path) as f:
        return f.read()


def ls(path):
    """What a user sees in a directory: sorted, without dotfiles (Lacuna's .lacuna.json record)."""
    return sorted(f for f in os.listdir(path) if not f.startswith("."))


def strip_ansi(s):
    return ANSI.sub("", s)


def usage(inp=0, write=0, read=0, out=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read, "output_tokens": out}


def call(mid, model, when, u, sidechain=False, tools=()):
    """One assistant line of a Claude Code transcript."""
    row = {"type": "assistant", "timestamp": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "message": {"id": mid, "model": model, "usage": u,
                       "content": [{"type": "tool_use", "name": t} for t in tools]}}
    if sidechain:
        row["isSidechain"] = True
    return row


def jsonl(path, rows):
    return write(path, "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))


class Sandbox(unittest.TestCase):
    """One sandbox per test: its own HOME, data directory, PATH and fake model."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lacuna-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home, self.data, self.bin = (os.path.join(self.tmp, d) for d in ("home", "data", "bin"))
        for d in (self.home, self.data, self.bin):
            os.makedirs(d)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        self.parse_log = os.path.join(self.tmp, "parse.log")
        fake = write(os.path.join(self.bin, "claude"), FAKE_CLAUDE.format(py=PY))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.assertEqual(shutil.which("claude", path=f"{self.bin}:{SAFE_PATH}"), fake,
                         "a test must never be able to reach the real `claude`")

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def env(self, **extra):
        env = {"PATH": f"{self.bin}:{SAFE_PATH}", "HOME": self.home, "LACUNA_DATA": self.data,
               "FAKE_LOG": self.log, "FAKE_PARSE_LOG": self.parse_log, "LANG": "en_US.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def run_py(self, argv, cwd=None, input=None, **extra):
        """Run a script under the sandbox environment."""
        return subprocess.run([PY, *argv], capture_output=True, text=True, encoding="utf-8",
                              env=self.env(**extra), cwd=cwd or self.tmp, input=input,
                              stdin=None if input is not None else subprocess.DEVNULL)

    def lacuna(self, *args, mode="ok", prelude=None, cwd=None, **extra):
        """`lacuna <args>` through bin/lacuna. With `prelude`, run lit_pipeline in-process after
        executing it, so a test can bump a version constant the way a release would."""
        extra.setdefault("FAKE_MODE", mode)
        if prelude is None:
            return subprocess.run([LACUNA, *args], capture_output=True, text=True, encoding="utf-8",
                                  env=self.env(**extra), cwd=cwd or self.tmp)
        code = (f"import sys\nsys.path.insert(0, {LIT!r})\nimport lit_pipeline as L\n{prelude}\n"
                f"sys.argv = ['lit_pipeline.py'] + {list(args[1:])!r}\nL.main()\n")
        return self.run_py(["-c", code], cwd=cwd, **extra)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def config(self, cfg):
        write(os.path.join(self.data, "config.json"), json.dumps(cfg))

    def hook(self, name):
        """A hook script that records its arguments, one per line, in <name>.args."""
        marker = self.p(f"{name}.args")
        script = write(self.p(f"{name}.sh"), f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{marker}"\n', 0o755)
        return script, marker
