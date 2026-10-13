---
description: Show what this session and the last 7 days cost (list prices) and where the tokens went
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/lacuna:*)
---
!`${CLAUDE_PLUGIN_ROOT}/bin/lacuna spend --no-color --days 7`

Reproduce the report above verbatim inside a code block. Then add at most three bullets: the single largest avoidable cost visible in it (cold cache rewrites, an oversized context being re-read every turn, or an expensive model doing simple work) and one concrete action for each. These are list prices, not the user's bill — say so once if any figure is large. Do not run any other tools.
