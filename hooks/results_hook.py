#!/usr/bin/env python3
"""SessionStart hook: if this session was launched into a results pane, explain the two-pane protocol.

Silent and free unless the session is actually running inside the pane layout (`lacuna split` sets
CLAUDE_RESULTS_FILE and tmux sets TMUX). Everyone else never sees it.
"""
import json, os, sys

path = os.environ.get("CLAUDE_RESULTS_FILE")
if not path or not os.environ.get("TMUX"):
    sys.exit(0)

try:
    json.load(sys.stdin)
except ValueError:
    pass

proto_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "panes", "protocol.md")
try:
    with open(proto_path) as f:
        proto = f.read()
except OSError:
    sys.exit(0)

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": proto.replace("RESULTS_PATH", f"`{path}`")}}))
