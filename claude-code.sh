#!/bin/sh
# Open Claude Code in this vault (macOS / Linux). Run it from anywhere:
#   ./claude-code.sh          ./claude-code.sh --continue
#
# cd's to the directory this script lives in rather than a hardcoded path, so the launcher
# survives the vault being moved or renamed. Arguments pass straight through.
cd "$(dirname "$0")" || exit 1

# Pre-flight: with an unreadable palimpsest.json the sync and the session opener run on DEFAULTS
# (pull, push and state OFF), and the opener only hears of it from the next sync's receipt — a
# hook's stderr never reaches the session. So say it here, where a person is looking.
if command -v python3 >/dev/null 2>&1; then
  problem=$(python3 tools/config.py --check 2>/dev/null)
  if [ $? -eq 3 ] && [ -n "$problem" ]; then
    echo "$problem" >&2
    if [ -t 0 ] && [ -t 2 ]; then
      printf 'Press Enter to open Claude Code anyway (and fix it there)... ' >&2
      read -r _
    fi
  fi
fi

if command -v claude >/dev/null 2>&1; then
  exec claude "$@"
elif [ -x "$HOME/.local/bin/claude" ]; then
  exec "$HOME/.local/bin/claude" "$@"
else
  echo "Could not find the claude CLI — not on PATH, and not at $HOME/.local/bin/claude."
  echo "Install it, or edit this file to point at your install."
  exit 1
fi
