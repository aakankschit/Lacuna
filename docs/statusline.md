# Status line (manual install)

```
◆ Opus 5 xhigh │ my-project │ ctx ▰▰▰▰▱▱▱▱▱▱ 163k │ cache ● 1h 42m left │ → /compact or handoff at next break
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

Add to `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/plugins/lacuna/statusline/statusline.py",
    "refreshInterval": 30,
    "padding": 0
  }
}
```

Check the real path first — `/plugin` shows where the marketplace installed it, and that path
changes when the plugin updates. If you would rather not chase it, copy the file somewhere stable:

```bash
mkdir -p ~/.claude/bin
cp <plugin dir>/statusline/statusline.py ~/.claude/bin/statusline.py
```

and point `command` at `python3 ~/.claude/bin/statusline.py`. The script is deliberately
self-contained — one file, standard library only — so copying it works. It picks up your
`pricing.json` from the data directory if you have one.

`refreshInterval` is in seconds and has a minimum of 1. At 30 the cache countdown stays useful
without re-running the script constantly.

## What each segment means

| Segment | Meaning |
|---|---|
| `◆ Opus 5 xhigh` | current model and reasoning effort, coloured by model family |
| `my-project` | the workspace directory, so two sessions are distinguishable |
| `ctx ▰▰▰▰▱▱ 163k` | context tokens in use against the window |
| `cache ● 1h 42m left` | time until the prompt cache expires; `○ cold` plus a dollar figure once it has |
| `→ /compact or handoff…` | appears only when the context is large enough for it to matter |
| `$4.12 session` | this session's cost at list price |
| `last $0.120 (read · write · out)` | the previous turn, split so you can see *why* it cost that |
| `hit 97%` | cache hit ratio; a number dropping here is the first sign of repeated cold writes |
| `5h` / `7d` bars | plan usage windows and when they reset |

## Caveats

- Dollar figures are **list price**, not your bill. On a subscription the usage bars are the figures
  that matter; see [LIMITS.md](LIMITS.md).
- The rate-limit and cache-TTL segments read fields that are not a documented interface. If they
  change, those segments go blank rather than show a wrong number.
- Cost is computed from Claude Code's transcript files, whose format is internal.
