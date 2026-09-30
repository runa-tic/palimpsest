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
  2. A local, git-ignored deny list: tools/.redact_terms.txt — one term per line.
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
import codecs, re, sys
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


_BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),   # before UTF-16: same lead
          (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))
_warned = False


def _deny_texts(raw: bytes) -> list[str]:
    """The deny list as text, whatever Windows saved it as. A strict UTF-8 read raised on UTF-16
    (PowerShell 5 `>`) or ANSI (Notepad), which the recorder swallowed and wrote the note with NO
    redaction, credentials included; a UTF-8 BOM silently disabled the first term (review,
    2026-09-30). Not UTF-8 and no BOM: cp1251 or cp1252 cannot be told apart, so both readings are
    denied; the wrong one only adds a term nobody writes."""
    global _warned
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            return [raw.decode(enc, errors="replace")]
    try:
        return [raw.decode("utf-8")]
    except UnicodeDecodeError:
        if not _warned:
            _warned = True
            sys.stderr.write(f"redact: {DENY_FILE.name} is not UTF-8; reading it as cp1251 and as cp1252."
                             " Re-save it as UTF-8.\n")
        return [raw.decode(enc, errors="replace") for enc in ("cp1251", "cp1252")]


def _load_deny() -> tuple[list[str], list[re.Pattern]]:
    literals: list[str] = []
    regexes: list[re.Pattern] = []
    if not DENY_FILE.exists():
        return literals, regexes
    try:
        texts = _deny_texts(DENY_FILE.read_bytes())
    except OSError:
        return literals, regexes
    for text in texts:
        # NULs: UTF-16 appended to a UTF-8 file (PowerShell 5 `>>`); U+FEFF: a BOM mid-file.
        for line in text.replace("\x00", "").replace("\ufeff", "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("re:"):
                pat = line[3:].strip()
                if pat and pat not in (r.pattern for r in regexes):
                    try:
                        regexes.append(re.compile(pat, re.I))
                    except re.error:
                        pass  # a bad pattern shouldn't break the whole pass
            elif line not in literals:
                literals.append(line)
    return literals, regexes


# A private key is a BLOCK: the header alone matched before (the scanner's pattern is only the
# BEGIN line), so redaction replaced the header and left the base64 body, which the commit guard
# then passed because no header remained; restoring the header gave a working key (review,
# 2026-09-30). This consumes the header, every base64 line after it (also as literal \n inside
# JSON tool output, also cut off before END), and the END line. Runs before the one-line patterns.
# A PGP armored key ends its header in " BLOCK", carries Version:/Comment: armor headers, and has
# two short lines before END (the last base64 line and the =XXXX checksum); both header patterns
# missed it entirely until the 2026-09-30 review.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    r"(?:(?:\s|\\[nr])+(?:(?:Proc-Type|DEK-Info|Version|Comment|Charset|Hash):[^\n\\]*"
    r"|[A-Za-z0-9+/=]{16,}))*"
    r"(?:(?:(?:\s|\\[nr])*[A-Za-z0-9+/=]{1,15}){0,2}(?:\s|\\[nr])*"
    r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)?")


def redact_text(s: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_substitutions)."""
    if not s:
        return s, 0
    s, n = _PEM_BLOCK.subn(MARK, s)
    for rx in _CRED:
        s, k = rx.subn(MARK, s)
        n += k
    try:
        literals, regexes = _load_deny()
    except Exception as e:   # keep the credential pass above: the caller would write the raw text
        sys.stderr.write(f"redact: deny list unreadable ({type(e).__name__}); credentials only\n")
        return s, n
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
    # Quick self-check / manual scrub: `python tools/redact.py < file` prints redacted.
    import sys
    data = sys.stdin.read()
    out, hits = redact_text(data)
    sys.stderr.write(f"redact: {hits} substitution(s)\n")
    sys.stdout.write(out)
