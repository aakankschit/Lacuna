---
description: Check the Lacuna environment and report exactly what is missing
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/lacuna:*)
---
!`${CLAUDE_PLUGIN_ROOT}/bin/lacuna doctor`

Reproduce the report above verbatim inside a code block. Then, only if any line is marked MISS, give the exact commands to fix those lines in the order they should be run, and say which parts of the plugin already work without them (`/spend` and the cost tools need nothing but Python). If nothing is missing, say so in one line and suggest `/spend` as the next step. Do not run any other tools.
