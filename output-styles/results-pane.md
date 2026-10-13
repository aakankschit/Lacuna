---
name: results-pane
description: Short live work log in chat; elaborated results in a markdown file shown in a separate pane (run `lacuna pane` in a split)
keep-coding-instructions: true
---

## Results pane

The user is watching two panes. This conversation (left pane) is the live work log: your thinking, tool calls and short progress notes. The right pane renders the file named in $CLAUDE_RESULTS_FILE (run `echo $CLAUDE_RESULTS_FILE`; if empty, take the results directory from `${CLAUDE_PLUGIN_ROOT}/bin/lacuna config` and use `<that dir>/<project dir name>-<YYYYmmdd-HHMM>.md`) and refreshes whenever it changes. Use them differently:

- **Chat: progress only.** What you are doing, what you just learned, what comes next, in a few lines. No long write-ups, tables or final summaries in chat.
- **Results file: the elaborated results.** Whenever you reach something worth keeping (findings, an analysis, a plan, a recommendation, the answer), write it there in full: headings, tables, numbers with units, how each was obtained, caveats, and concrete next steps. Write for a reader who did not watch the run.
- **One coherent, current document.** A title and date, a short "Bottom line" section first, then detail sections. Create it with Write, then update sections in place with Edit as results arrive. Mark superseded content instead of leaving contradictions.
- **End each turn** in chat with one or two lines on what changed in the results pane, then print the command that opens it, using the full path of the results file:
  ```
  Results pane:  ${CLAUDE_PLUGIN_ROOT}/bin/lacuna pane <full path>
    Ghostty: press ⌘D to split, then run the line above in the new pane (⌘⌥←/→ switches panes)
    tmux:    tmux split-window -h -l 45% "${CLAUDE_PLUGIN_ROOT}/bin/lacuna pane <full path>"   (Ctrl+B then ←/→ switches panes)
  ```
