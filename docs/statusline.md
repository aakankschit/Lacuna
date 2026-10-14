# Status line (manual install)

```
◆ Opus 5 xhigh │ my-project │ ctx ▰▰▰▰▱▱▱▱▱▱ 163k │ cache ● 1h cache · 42m left │ → /compact or handoff at next break
$4.12 session │ last $0.120 (read 0.037 · write 0.015 · out 0.069) │ hit 97% │ 5h ▰▰▱▱▱▱ 38% ↻2h09m │ 7d ▰▰▰▰▱▱ 71% ↻83h19m
```

When the cache has gone cold, the fourth segment changes to what the next prompt will cost you:

```
cache ○ cold · next prompt re-caches ≈$0.56
```

That number is the point of the whole thing. Nothing else in Claude Code tells you that walking away
for an hour has put a charge on your next keystroke.

## Why this is not installed with the plugin

A plugin's `settings.json` only applies the `agent` and `subagentStatusLine` keys — `statusLine` is
not among them, so **no plugin can ship a status line**. It has to go in your own settings.

## Install

**Copy the script to a stable location first.** The plugin's own install path contains its version
number (`~/.claude/plugins/cache/lacuna/lacuna/0.1.0/...`), so it changes on every update and a
`statusLine` pointing into it silently stops working the next time you upgrade.

```bash
mkdir -p ~/.claude/bin
cp "$(claude plugin list --json | python3 -c \
  "import json,sys;[print(p['installPath']) for p in json.load(sys.stdin) if p['id'].split('@')[0]=='lacuna']")/statusline/statusline.py" \
  ~/.claude/bin/statusline.py
```

Then add to `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/bin/statusline.py",
    "refreshInterval": 30,
    "padding": 0
  }
}
```

The script is deliberately self-contained — one file, standard library only — so copying it works,
and it still picks up your `pricing.json` from the data directory (`$LACUNA_DATA`, with `~`
expanded, exactly as the CLI resolves it). A malformed `pricing.json` is skipped, not fatal, and
its inline price table matches the shipped `scripts/pricing.json`. Re-run the copy after a plugin
upgrade to get any changes.

`refreshInterval` is in seconds and has a minimum of 1. At 30 the cache countdown stays useful
without re-running the script constantly.

## What each segment means

| Segment | Meaning |
|---|---|
| `◆ Opus 5 xhigh` | current model and reasoning effort, coloured by model family |
| `my-project` | the workspace directory, so two sessions are distinguishable |
| `ctx ▰▰▰▰▱▱ 163k` | context tokens in use against the window |
| `cache ● 1h cache · 42m left` | the cache TTL and the time left on it; `○ cold` plus a dollar figure once it has expired; absent when Claude Code reports no expiry time |
| `→ /compact or handoff…` | appears only when the context is large enough for it to matter |
| `$4.12 session` | this session's cost at list price |
| `last $0.120 (read · write · out)` | the previous turn, split so you can see *why* it cost that. `last $0.030–$0.120` means the call was retried after a refused or failed attempt: the lower end is what was recorded, the upper adds the earlier attempts, and whether they were billed is not in the transcript |
| `hit 97%` | cache hit ratio; a number dropping here is the first sign of repeated cold writes |
| `5h` / `7d` bars | plan usage windows and when they reset |

## Caveats

Every figure is a list-price estimate from undocumented fields and transcript files; when a field is
absent the segment goes blank. [LIMITS.md](LIMITS.md) has the full statement. The `$ session` figure
is Claude Code's own; `last $` is computed from the transcript, and the two can differ by the 4–14%
that Claude Code counts but does not log.
