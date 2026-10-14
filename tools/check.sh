#!/usr/bin/env bash
# Run the whole suite the way CI does: every Python on this machine, in a scrubbed environment.
#
# This exists because a test that passes here and fails in CI is worse than no test. The suite was
# once red on every macOS CI job for a single reason — one test counted optional pieces, so its
# result depended on whether `rich` and `tmux` happened to be installed. A scrubbed environment is
# the only way to catch that class of mistake before pushing.
#
#   env -i          nothing inherited: no PATH additions, no virtualenv, no LACUNA_* overrides
#   HOME=<temp>     so no test can see the author's ~/.lacuna or ~/.claude
#   PATH=/usr/bin:/bin   no homebrew, so no tmux and no accidentally-visible tools
#
# usage: tools/check.sh [--quick]     (--quick runs only the default python3)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

pythons=()
if [ "${1:-}" = "--quick" ]; then
  pythons=("$(command -v python3)")
else
  for p in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3.1[0-9] \
           "$(command -v python3)"; do
    [ -x "$p" ] && pythons+=("$p")
  done
fi

# de-duplicate by reported version, keeping the first path for each
declare -a seen=() chosen=()
for p in "${pythons[@]}"; do
  v="$("$p" --version 2>&1)" || continue
  case " ${seen[*]-} " in *" $v "*) continue ;; esac
  seen+=("$v"); chosen+=("$p")
done

fail=0
for p in "${chosen[@]}"; do
  v="$("$p" --version 2>&1)"
  tmphome="$(mktemp -d)"
  printf '%-16s %-42s ' "$v" "$p"
  out="$(env -i PATH=/usr/bin:/bin HOME="$tmphome" LANG=en_US.UTF-8 \
         "$p" -m unittest discover -s tests 2>&1)"
  if printf '%s' "$out" | tail -1 | grep -q '^OK'; then
    echo "OK   ($(printf '%s' "$out" | grep -oE 'Ran [0-9]+ tests' | head -1))"
  else
    echo "FAILED"
    printf '%s\n' "$out" | tail -25
    fail=1
  fi
  rm -rf "$tmphome"
done

echo
if [ "$fail" -eq 0 ]; then
  echo "All green on ${#chosen[@]} interpreter(s), with no rich, no tmux and a scrubbed HOME."
else
  echo "Something failed above. A failure here is a real failure: nothing in the suite is allowed"
  echo "to depend on what happens to be installed on the machine."
fi
exit "$fail"
