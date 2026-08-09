#!/usr/bin/env python3
"""Redaction layer for the conversation recorder.

Why: the Stop hook saves every session into the vault verbatim
(import_claude.write_note), and the vault git-syncs to GitHub. Manually scrubbing a
note doesn't stick, because the recorder re-emits the original text on the next Stop
event. This module scrubs sensitive strings out of the note BEFORE it is written, so
the fix is durable instead of whack-a-mole.

Two redaction sources:
  1. Built-in credential shapes, reused from scan_secrets HIGH (API keys, tokens,
     private keys). Unambiguously must never sync, so they are always masked.
  2. A local, git-ignored deny list: _tools/.redact_terms.txt — one term per line.
         plain line   -> literal, case-insensitive substring match
         re: PATTERN  -> Python regex (case-insensitive)
         # comment
     This is where personal identifiers go (EMPLIDs, application numbers, emails,
     names, amounts). It is deny-list driven on purpose: this vault legitimately holds
     many long numeric Telegram IDs and crypto addresses, so blanket number/email/
     address redaction would shred real content. You curate exactly what to remove.

The deny list itself lists sensitive strings, so it is git-ignored and never syncs.

Public API:
    redact_text(s) -> (scrubbed, n_hits)
Idempotent: replacements leave a [redacted] marker the rules don't re-match, so the
recorder can re-run every turn without compounding.
"""
from __future__ import annotations
import re
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
DENY_FILE = TOOLS / ".redact_terms.txt"
MARK = "[redacted]"

# Reuse the high-confidence credential shapes from the secret scanner, so credentials
# are masked in the saved note too (defense in depth; also means the pre-commit guard
# has nothing left to block on these). Best-effort: if the import fails, fall back to
# deny-list-only redaction rather than break recording.
try:
    from scan_secrets import HIGH as _HIGH
    _CRED = [rx for _label, rx in _HIGH]
except Exception:
    _CRED = []


def _load_deny() -> tuple[list[str], list[re.Pattern]]:
    literals: list[str] = []
    regexes: list[re.Pattern] = []
    if not DENY_FILE.exists():
        return literals, regexes
    try:
        lines = DENY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return literals, regexes
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            pat = line[3:].strip()
            if pat:
                try:
                    regexes.append(re.compile(pat, re.I))
                except re.error:
                    pass  # a bad pattern shouldn't break the whole pass
        else:
            literals.append(line)
    return literals, regexes


def redact_text(s: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_substitutions)."""
    if not s:
        return s, 0
    n = 0
    for rx in _CRED:
        s, k = rx.subn(MARK, s)
        n += k
    literals, regexes = _load_deny()
    for rx in regexes:
        s, k = rx.subn(MARK, s)
        n += k
    # Longest literals first so a specific term wins over a shorter overlapping one.
    for term in sorted(set(literals), key=len, reverse=True):
        if not term:
            continue
        s, k = re.compile(re.escape(term), re.I).subn(MARK, s)
        n += k
    return s, n


if __name__ == "__main__":
    # Quick self-check / manual scrub: `python _tools/redact.py < file` prints redacted.
    import sys
    data = sys.stdin.read()
    out, hits = redact_text(data)
    sys.stderr.write(f"redact: {hits} substitution(s)\n")
    sys.stdout.write(out)
