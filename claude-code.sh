#!/bin/sh
# Open Claude Code in this vault (macOS / Linux). Run it from anywhere:
#   ./claude-code.sh          ./claude-code.sh --continue
#
# cd's to the directory this script lives in rather than a hardcoded path, so the launcher
# survives the vault being moved or renamed. Arguments pass straight through.
cd "$(dirname "$0")" || exit 1

if command -v claude >/dev/null 2>&1; then
  exec claude "$@"
elif [ -x "$HOME/.local/bin/claude" ]; then
  exec "$HOME/.local/bin/claude" "$@"
else
  echo "Could not find the claude CLI — not on PATH, and not at $HOME/.local/bin/claude."
  echo "Install it, or edit this file to point at your install."
  exit 1
fi
