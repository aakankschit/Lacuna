#!/usr/bin/env python3
"""SessionStart hook: if this session was launched into a results pane, explain the two-pane protocol.

Silent and free unless the session is actually running inside the pane layout (`lacuna split` sets
CLAUDE_RESULTS_FILE and tmux sets TMUX). Everyone else never sees it.

A hook that raises breaks every prompt, so nothing here may: any failure exits 0 quietly.
"""
import json, os, sys


def main():
    path = os.environ.get("CLAUDE_RESULTS_FILE")
    if not path or not os.environ.get("TMUX"):
        return

    try:
        json.load(sys.stdin)
    except ValueError:                   # includes UnicodeDecodeError under a US-ASCII locale
        pass

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proto_path = os.path.join(root, "scripts", "panes", "protocol.md")
    # The file is UTF-8 (arrows, a command key). The locale's default codec is not: under
    # LC_ALL=en_US.US-ASCII the read raised and this hook took the session down with it.
    with open(proto_path, encoding="utf-8") as f:
        proto = f.read()

    # ensure_ascii (the default) keeps the output ASCII, so the encoding of stdout cannot matter.
    # The protocol's `${CLAUDE_PLUGIN_ROOT}` is literal text to the model, so give it the real path;
    # the protocol quotes it, because the path may hold spaces.
    proto = proto.replace("RESULTS_PATH", f"`{path}`").replace("${CLAUDE_PLUGIN_ROOT}", root)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": proto}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:                    # the hook must never break a session
        pass
    sys.exit(0)
