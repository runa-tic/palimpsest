#!/usr/bin/env python3
"""Block a commit whose staged content contains a deny-listed PII term.

`redact.py` runs inside the conversation recorder, so it only protects notes the *hook*
writes. Anything authored by hand — an atomic note, a project page, a daily entry — never
passes through it, and a term added to `tools/.redact_terms.txt` months ago walks back
into the vault the moment someone types it. Found exactly that on 2026-08-08: a farm login
address, deny-listed since 07-23, sitting in an atomic note written 08-04.

Two tiers, matching the secret scanner's block/warn split:
  BLOCK  terms that are unambiguously an identifier — anything containing "@", a phone number
         or other run of 7+ digits (matched in any separator spelling), or an alphabetic term of
         5+ characters (surnames, full names, handles, reference codes); spaces, hyphens,
         apostrophes and dots do not count against "alphabetic".
  WARN   short numeric literals and short words (amounts, small ids, 2-4 letter names). These
         collide with legitimate vault content — the vault is full of numbers — so they report
         and let the commit through rather than wedging the automated sync on a coincidence.

Values are never printed. The Stop hook records this session into the vault, so echoing an
address while removing it just recreates the leak in a new file; masked forms only.

Usage:
  python tools/scan_pii.py        # scan staged changes (used by pre-commit)
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import _load_deny, is_phone, term_pattern   # the one parser/matcher for .redact_terms.txt
from scan_secrets import staged_files, staged_content


def mask(term: str) -> str:
    """Identifiable to the operator, useless to a reader. Never the value itself."""
    head = term[:2]
    return f"{head}{'*' * max(3, len(term) - 2)}"


def is_hard(term: str) -> bool:
    # A phone number is what SETUP asks for first and promises is blocked at commit time; it only
    # warned, which in the unattended nightly push surfaces nowhere (review, 2026-09-30).
    if "@" in term or is_phone(term):
        return True
    # Name punctuation is not "numeric": counting each space as a non-letter made every full name
    # of three words ("Mary Ann Lee") a soft term that only warned (review, 2026-09-30).
    other = sum(not (c.isalpha() or c in " -'.") for c in term)
    return len(term) >= 5 and other <= 1 and sum(c.isalpha() for c in term) >= 3


def _safe_path(rel: str, literals, regexes) -> str:
    """The path as printed: every deny-listed term in it masked, since the path itself may be
    what carries the term (and this output is recorded into the vault)."""
    for t in sorted(literals, key=len, reverse=True):
        rel = term_pattern(t).sub(lambda m: mask(m.group(0)), rel)
    for rx in regexes:
        rel = rx.sub(lambda m: mask(m.group(0)), rel)
    return rel


def _scan(rel: str, content: str, hard, soft, regexes, blocking: list, warning: list) -> None:
    if not content:
        return
    for term in hard:
        n = len(term_pattern(term).findall(content))
        if n:
            blocking.append((rel, mask(term), n))
    for term in soft:
        n = len(term_pattern(term).findall(content))
        if n:
            warning.append((rel, mask(term), n))
    for rx in regexes:
        n = len(rx.findall(content))
        if n:
            blocking.append((rel, f"re:{mask(rx.pattern)}", n))


def main() -> int:
    literals, regexes = _load_deny()
    if not literals and not regexes:
        return 0
    hard = [t for t in literals if is_hard(t)]
    soft = [t for t in literals if not is_hard(t)]

    blocking: list[tuple[str, str, int]] = []
    warning: list[tuple[str, str, int]] = []
    for rel in staged_files():
        # Scan the path as well as the content: a note named after a person or a conversation
        # title carries the term in its filename, where a contents-only scan never looks.
        for label, content in ((f"{rel} [path]", rel), (rel, staged_content(rel) or "")):
            _scan(_safe_path(label, literals, regexes), content, hard, soft, regexes, blocking, warning)


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
    print("tools/.redact_terms.txt. To bypass once: git commit --no-verify")
    return 1


if __name__ == "__main__":
    sys.exit(main())
