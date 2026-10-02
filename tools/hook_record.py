#!/usr/bin/env python3
"""Stop-hook recorder: live-saves the current Claude Code session into the vault.

Claude Code runs this after every assistant turn (Stop event). It reads the hook JSON
from stdin, grabs `transcript_path`, and updates that session's conversation note under
40 Resources/Claude Conversations/. Fast and idempotent; exits 0 no matter what so it
never blocks the session.
"""
import os, sys, json
from pathlib import Path

try:
    # UTF-8 whatever the code page, as callers read it, and backslashreplace: under "strict" a
    # surrogate (argv or a path that is not UTF-8) crashed the very error that carried it.
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

def main():
    # Our own tools spawn `claude -p` (which also fires hooks). Skip those.
    if os.environ.get("CLAUDE_BRAIN_NO_HOOK"):
        return
    try:
        # Bytes, decoded as UTF-8: Claude Code sends UTF-8, but a piped text stdin on Windows
        # decodes in the ANSI code page, so a non-ASCII user name in transcript_path became
        # mojibake (or a decode error) and the turn was silently never recorded.
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return
    tp = data.get("transcript_path")
    if not tp:
        return
    try:
        import import_claude as ic
        # The tg_bridge is a real (but headless/sdk-cli) conversation; keep it.
        allow_sdk = bool(os.environ.get("CLAUDE_BRAIN_BRIDGE"))
        ic.process_transcript(Path(tp), allow_sdk=allow_sdk)
    except Exception:
        # Never fail the hook — recording is best-effort.
        pass

if __name__ == "__main__":
    main()
