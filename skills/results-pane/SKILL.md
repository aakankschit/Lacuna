---
name: results-pane
description: Turn on the two-pane mode for this session. Chat stays a short live work log; elaborated results go to a markdown file shown in a separate pane. Needs tmux, or any terminal that can split.
disable-model-invocation: true
---

# Results pane mode

1. Pick the results file: run `echo $CLAUDE_RESULTS_FILE`. If it is empty, ask `${CLAUDE_PLUGIN_ROOT}/bin/lacuna config` for the results directory and use `<that dir>/<project dir name>-<YYYYmmdd-HHMM>.md`.
2. Create it now with a title, the date, and an empty "Bottom line" section.
3. Print the open-the-pane command block from the protocol below, with the full path.
4. From now on, follow the protocol below for the rest of the session.

## Results pane

The user is watching two panes. This conversation (left pane) is the live work log: your thinking, tool calls and short progress notes. The right pane renders that results file and refreshes whenever it changes. Use them differently:

- **Chat: progress only.** What you are doing, what you just learned, what comes next, in a few lines. No long write-ups, tables or final summaries in chat.
- **Results file: the elaborated results.** Whenever you reach something worth keeping (findings, an analysis, a plan, a recommendation, the answer), write it there in full: headings, tables, numbers with units, how each was obtained, caveats, and concrete next steps. Write for a reader who did not watch the run.
- **One coherent, current document.** A title and date, a short "Bottom line" section first, then detail sections. Create it with Write, then update sections in place with Edit as results arrive. Mark superseded content instead of leaving contradictions.
- **End each turn** in chat with one or two lines on what changed in the results pane, then print the command that opens it, using the full path of the results file, with both paths in quotes exactly as shown (a path can contain spaces):
  ```
  Results pane:  "${CLAUDE_PLUGIN_ROOT}/bin/lacuna" pane "<full path>"
    Ghostty: press ⌘D to split, then run the line above in the new pane (⌘⌥←/→ switches panes)
    tmux:    tmux split-window -h -l 45% "\"${CLAUDE_PLUGIN_ROOT}/bin/lacuna\" pane \"<full path>\""   (Ctrl+B then ←/→ switches panes)
  ```
