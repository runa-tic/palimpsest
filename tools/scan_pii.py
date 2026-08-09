#!/usr/bin/env python3
"""Block a commit whose staged content contains a deny-listed PII term.

`redact.py` runs inside the conversation recorder, so it only protects notes the *hook*
writes. Anything authored by hand — an atomic note, a project page, a daily entry — never
passes through it, and a term added to `_tools/.redact_terms.txt` months ago walks back
into the vault the moment someone types it. Found exactly that on 2026-08-08: a farm login
address, deny-listed since 07-23, sitting in an atomic note written 08-04.

Two tiers, matching the secret scanner's block/warn split:
  BLOCK  terms that are unambiguously an identifier — anything containing "@", or an
         alphabetic term of 5+ characters (surnames, handles, reference codes).
  WARN   short or numeric literals (amounts, ids). These collide with legitimate vault
         content — the vault is full of numbers — so they report and let the commit through
         rather than wedging the automated sync on a coincidence.

Values are never printed. The Stop hook records this session into the vault, so echoing an
address while removing it just recreates the leak in a new file; masked forms only.

Usage:
  python _tools/scan_pii.py        # scan staged changes (used by pre-commit)
"""
from __future__ import annotations
import sys, re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import _load_deny          # the one parser for .redact_terms.txt
from scan_secrets import staged_files, staged_content


def mask(term: str) -> str:
    """Identifiable to the operator, useless to a reader. Never the value itself."""
    head = term[:2]
    return f"{head}{'*' * max(3, len(term) - 2)}"


def is_hard(term: str) -> bool:
    if "@" in term:
        return True
    return len(term) >= 5 and sum(c.isalpha() for c in term) >= len(term) - 1


def main() -> int:
    literals, regexes = _load_deny()
    if not literals and not regexes:
        return 0
    hard = [t for t in literals if is_hard(t)]
    soft = [t for t in literals if not is_hard(t)]

    blocking: list[tuple[str, str, int]] = []
    warning: list[tuple[str, str, int]] = []
    for rel in staged_files():
        content = staged_content(rel)
        if not content:
            continue
        for term in hard:
            n = len(re.findall(re.escape(term), content, re.I))
            if n:
                blocking.append((rel, mask(term), n))
        for term in soft:
            n = len(re.findall(re.escape(term), content, re.I))
            if n:
                warning.append((rel, mask(term), n))
        for rx in regexes:
            n = len(rx.findall(content))
            if n:
                blocking.append((rel, f"re:{mask(rx.pattern)}", n))

    for rel, m, n in warning:
        print(f"pii-scan WARN: {rel} — {n}x deny-listed literal {m} (numeric/short; not blocking)")

    if not blocking:
        print("pii-scan: clean (staged changes).")
        return 0

    print("")
    print("Commit blocked by pii-scan — staged content contains deny-listed PII:")
    for rel, m, n in blocking:
        print(f"  {rel}: {n}x {m}")
    print("")
    print("Scrub the value from the file (do NOT paste it into the terminal — this session")
    print("is recorded into the vault). If the term no longer needs denying, remove it from")
    print("_tools/.redact_terms.txt. To bypass once: git commit --no-verify")
    return 1


if __name__ == "__main__":
    sys.exit(main())
