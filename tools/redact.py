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
         plain line   -> literal, case-insensitive substring match. A number of 7+ digits
                         with only " +-.()" besides (a phone, an EMPLID, an application
                         number) matches its digits in any separator spelling, but not
                         inside a longer digit run; scan_pii blocks a commit on it.
         re: PATTERN  -> Python regex (case-insensitive)
         # comment
     This is where personal identifiers go (EMPLIDs, application numbers, emails,
     names, amounts). It is deny-list driven on purpose: this vault legitimately holds
     many long numeric Telegram IDs and crypto addresses, so blanket number/email/
     address redaction would shred real content. You curate exactly what to remove.

The deny list itself lists sensitive strings, so it is git-ignored and never syncs.

A deny list that is there but cannot be read (no permission, a directory or a dangling link in its
place) is not "no deny list": load_deny_report raises DenyListUnreadable. scan_pii blocks the
commit on it and the CLI below exits 2. redact_text, which the recorder calls for every note it
writes and which must not fail, still masks credentials, leaves deny-listed terms as they are,
and says so once per process on stderr. The note is then written with those terms in it; what
keeps them out of git is scan_pii, which blocks every commit while the list cannot be read and
blocks a commit holding the terms once it can.

Public API:
    redact_text(s) -> (scrubbed, n_hits)
Idempotent: replacements leave a [redacted] marker the rules don't re-match, so the
recorder can re-run every turn without compounding.
"""
from __future__ import annotations
import codecs, re, sys, unicodedata
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


_BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"))   # whole file only
# Where a UTF-16 section starts: its BOM at the start of the file or of a line, or anywhere when the
# next two bytes are an ASCII character in that byte order. PowerShell 5 `>>` appends UTF-16 with a
# BOM to a UTF-8 file, and the file may not end in a newline. "\xff\xfe" is also cp1251 "яю", which
# never starts a line and is never followed by a NUL.
_UTF16_START = re.compile(
    rb"(?:\A|(?<=\n)|(?<=\n\x00))(?:\xff\xfe|\xfe\xff)|\xff\xfe(?=[^\x00]\x00)|\xfe\xff(?=\x00[^\x00])")
_LEGACY = ("cp1251", "cp1252")     # Windows ANSI for the lists this vault holds; not tellable apart


def _plausible(text: str) -> bool:
    """A UTF-16 reading of bytes that are not UTF-16 (or in the other byte order) comes out as CJK,
    replacement or control characters."""
    return all(("\x20" <= c < "\u3000" and c != "\x7f") or c in "\t\r" or "\uff00" <= c < "\ufff0"
               for c in text)


def _bomless_utf16(b: bytes) -> list[str]:
    """Readings of a line with NULs in it: UTF-16 without a BOM, in whichever byte order gives
    text. Split on b"\n", a little-endian line leads with the NUL of the previous newline and a
    big-endian one ends in the NUL of its own."""
    odd = len(b) % 2
    return [t for enc, cut in (("utf-16-le", b[odd:]), ("utf-16-be", b[:len(b) - odd]))
            if _plausible(t := cut.decode(enc, errors="replace"))]


def _byte_lines(chunk: bytes, first: int, lines: list, problems: list) -> None:
    """A section with no UTF-16 BOM, one line at a time: UTF-8 first; a line that is not UTF-8
    alone is read as cp1251 AND cp1252. Decoding the whole file at once turned every UTF-8 term
    into mojibake the moment one line was ANSI (review of fix/scanners, 2026-09-30)."""
    for i, b in enumerate(chunk.replace(codecs.BOM_UTF8, b"").split(b"\n"), first):
        if b"\x00" in b:
            if readings := _bomless_utf16(b):
                lines.extend((i, t) for t in readings)
                continue
            problems.append((i, "has NUL bytes but is not UTF-16; read without them"))
            b = b.replace(b"\x00", b"")
        try:
            lines.append((i, b.decode("utf-8")))
        except UnicodeDecodeError:
            readings = [b.decode(enc, errors="replace") for enc in _LEGACY]
            lines.extend((i, t) for t in readings)
            problems.append((i, "not UTF-8, read as cp1251 and as cp1252"
                             + ("; bytes neither codepage defines" if all("\ufffd" in t for t in readings)
                                else "")))


def _is_utf16(b: bytes, text: str, last: bool) -> bool:
    """Whether a line of a section with a UTF-16 BOM really is UTF-16. The byte order is known here,
    so CJK and emoji are text: judging them by _plausible re-read "\u674e" as the bytes "Ng", which
    then redacted every "ng" in every transcript (second review, 2026-09-30). Wrong bytes read as
    UTF-16 give replacement, control, unassigned, surrogate or private-use characters, or (UTF-8
    appended after the section) run on past the section's last UTF-16 newline with a b"\n" inside
    or an odd length. Only a line so marked is re-read as bytes. Limit: a character new in a Unicode
    version later than this Python's reads as unassigned, and its line is re-read and flagged
    (fail closed); a BOM-less UTF-16 CJK line still fails _plausible in both byte orders."""
    if last and (b"\n" in b or len(b) % 2):
        return False
    return not any(c == "\ufffd" or (unicodedata.category(c) in ("Cc", "Cn", "Cs", "Co") and c not in "\t\r")
                   for c in text)


def _utf16_lines(bom: bytes, chunk: bytes, first: int, lines: list, problems: list) -> int:
    """A section that starts with a UTF-16 BOM, one line at a time. A line that does not read as
    UTF-16 (UTF-8 appended after it, or the rare cp1251 line starting "\u044f\u044e") is read as bytes
    too, BOM included."""
    enc = "utf-16-le" if bom == codecs.BOM_UTF16_LE else "utf-16-be"
    parts = chunk.split("\n".encode(enc))
    for i, b in enumerate(parts, first):
        text = b.decode(enc, errors="replace")
        lines.append((i, text))
        if not _is_utf16(b, text, last=i == first + len(parts) - 1):
            problems.append((i, "inside a UTF-16 section but not UTF-16; read both ways"))
            _byte_lines((bom if i == first else b"") + b.replace(b"\x00", b""), i, lines, problems)
    return first + len(parts) - 1


def deny_lines(raw: bytes) -> tuple[list[str], list[tuple[int, str]]]:
    """The deny list's lines, whatever Windows saved or appended it as, and (line number, what was
    wrong) for every line that was not clean UTF-8 or UTF-16. Never returns a value in a problem."""
    lines: list[tuple[int, str]] = []
    problems: list[tuple[int, str]] = []
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            text = raw.decode(enc, errors="replace")
            return text.splitlines(), ([(1, "undecodable bytes")] if "\ufffd" in text else [])
    line = 1
    starts = [(m.start(), m.group(0)) for m in _UTF16_START.finditer(raw)]
    # A byte section runs to the first UTF-16 BOM; a UTF-16 section runs to the next BOM.
    if not starts or starts[0][0] > 0:
        head = raw[:starts[0][0]] if starts else raw
        _byte_lines(head, line, lines, problems)
        line += head.count(b"\n")
    for k, (at, bom) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(raw)
        line = _utf16_lines(bom, raw[at + 2:end], line, lines, problems)
    return [t for _, t in lines], problems


class DenyListUnreadable(Exception):
    """The deny list is there and could not be read. The message is why: the name of the error or
    a few fixed words, never anything from the list."""


_warned = False


def load_deny_report() -> tuple[list[str], list[re.Pattern], list[tuple[int, str]]]:
    """(literals, regexes, problems): problems name the lines that were not clean UTF-8 or UTF-16,
    by number only. scan_pii refuses to call a commit clean over any of them.

    Raises DenyListUnreadable when the list is there and cannot be read. That returned no terms
    and no problems, exactly what no deny list returns, so scan_pii exited 0 having checked
    nothing and redaction left every term in place, both silently (review, 2026-10-02)."""
    global _warned
    literals: list[str] = []
    regexes: list[re.Pattern] = []
    try:
        raw = DENY_FILE.read_bytes()
    except FileNotFoundError:
        # No list is a setup this supports. A link whose target is gone is a list that was set up
        # and is now out of reach (a volume not mounted), so it counts as unreadable.
        if DENY_FILE.is_symlink():
            raise DenyListUnreadable("a link to a file that is not there") from None
        return literals, regexes, []
    except OSError as e:        # no permission, a directory in its place, a failing disk
        raise DenyListUnreadable(type(e).__name__) from None
    texts, problems = deny_lines(raw)
    if problems and not _warned:
        _warned = True
        sys.stderr.write(f"redact: {DENY_FILE.name} is not clean UTF-8 (line(s) "
                         f"{', '.join(str(i) for i, _ in problems)}); those lines are read in every"
                         " likely codepage. Re-save it as UTF-8.\n")
    for line in texts:
        # NULs: stray bytes of a UTF-16 newline; U+FEFF: a BOM that is not at a section start.
        line = line.replace("\x00", "").replace("\ufeff", "").strip()
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
    return literals, regexes, problems


def _load_deny() -> tuple[list[str], list[re.Pattern]]:
    literals, regexes, _ = load_deny_report()
    return literals, regexes


def is_phone(term: str) -> bool:
    return sum(c.isdigit() for c in term) >= 7 and all(c.isdigit() or c in " +-.()" for c in term)


def term_pattern(term: str) -> re.Pattern:
    """How a literal deny-list term matches, here and in scan_pii. A phone number is listed once but
    written many ways ('+1 555-0100' / '+1 (555) 0100' / '15550100'), and a literal match caught
    only the spelling on the list, so its digits match with any separators between them. Not
    inside a longer run of digits: a 7-digit id blocks the commit now, and a Telegram id or a
    timestamp that merely contains its digits must not stop the unattended nightly push."""
    if is_phone(term):
        return re.compile(r"(?<![0-9])" + r"[\s().+-]*".join(c for c in term if c.isdigit()) + r"(?![0-9])")
    return re.compile(re.escape(term), re.I)


# A private key is a BLOCK: the header alone matched before (the scanner's pattern is only the
# BEGIN line), so redaction replaced the header and left the base64 body, which the commit guard
# then passed because no header remained; restoring the header gave a working key (review,
# 2026-09-30). This consumes the header, every base64 line after it (also as literal \n inside
# JSON tool output, also cut off before END), and the END line. Runs before the one-line patterns.
# A PGP armored key ends its header in " BLOCK", carries Version:/Comment: armor headers, and has
# two short lines before END (the last base64 line and the =XXXX checksum); both header patterns
# missed it entirely until the 2026-09-30 review.
# A key pasted into a thinking callout or a quote has "> " (or "> > ") before every line, and only
# its header was consumed until the second 2026-09-30 review, so the body stayed in the note.
_SEP = r"(?:[\s>]|\\[nr])"
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    rf"(?:{_SEP}+(?:(?:Proc-Type|DEK-Info|Version|Comment|Charset|Hash):[^\n\\]*"
    r"|[A-Za-z0-9+/=]{16,}))*"
    rf"(?:(?:{_SEP}*[A-Za-z0-9+/=]{{1,15}}){{0,2}}{_SEP}*"
    r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)?")


def _mark(m: re.Match) -> str:
    """A rule with a named group "v" (a secret found by its label) masks the value and keeps the
    label, so the note still says what was there: '"SecretAccessKey": "[redacted]"'."""
    if "v" not in m.re.groupindex or m.start("v") < 0:
        return MARK
    s0 = m.start()
    return m.group(0)[:m.start("v") - s0] + MARK + m.group(0)[m.end("v") - s0:]


_unreadable_said = False


def redact_text(s: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_substitutions)."""
    global _unreadable_said
    if not s:
        return s, 0
    s, n = _PEM_BLOCK.subn(MARK, s)
    for rx in _CRED:
        s, k = rx.subn(_mark, s)
        n += k
    try:
        literals, regexes = _load_deny()
    except Exception as e:   # keep the credential pass above: the caller would write the raw text
        # The recorder calls this for every title and note of a turn, so once per process. It
        # cannot refuse to record, and what it writes is not in git yet: scan_pii stops it there.
        if not _unreadable_said:
            _unreadable_said = True
            why = str(e) if isinstance(e, DenyListUnreadable) else type(e).__name__
            sys.stderr.write(f"redact: {DENY_FILE.name} cannot be read ({why}): credentials were redacted,"
                             " deny-listed terms were NOT. scan_pii blocks every commit until it can be read.\n")
        return s, n
    for rx in regexes:
        s, k = rx.subn(MARK, s)
        n += k
    # Longest literals first so a specific term wins over a shorter overlapping one.
    for term in sorted(set(literals), key=len, reverse=True):
        if not term:
            continue
        s, k = term_pattern(term).subn(MARK, s)
        n += k
    return s, n


if __name__ == "__main__":
    # Quick self-check / manual scrub: `python tools/redact.py < file` prints redacted.
    import sys
    # Bytes in and out, never through the console's code page: a text stdin on a Windows ANSI code
    # page misread a UTF-8 note, so a deny-listed term in it went through unredacted.
    try:
        # UTF-8 whatever the code page, as callers read it, and backslashreplace: under "strict" a
        # surrogate (argv or a path that is not UTF-8) crashed the very error that carried it.
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    raw = sys.stdin.buffer.read()
    # A deny list that cannot be read left every term in the output under "0 substitution(s)" and
    # exit 0 (review, 2026-10-02). redact_text only warns, for the recorder's sake; this refuses.
    try:
        load_deny_report()
    except DenyListUnreadable as e:
        sys.stderr.write(f"redact: {DENY_FILE.name} is there but cannot be read ({e}), so no deny-listed term"
                         " would be redacted. Nothing was written.\n")
        sys.exit(2)
    # A file with a BOM is redacted in its own encoding and written back in it, BOM included:
    # PowerShell 5.1 `>` writes UTF-16, which read as UTF-8 matched no term and came out with
    # "0 substitution(s)" and exit 0 (review, 2026-10-02). UTF-32 before UTF-16: same lead bytes.
    boms = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF8, "utf-8"),
            (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
    bom, enc = next(((b, e) for b, e in boms if raw.startswith(b)), (b"", "utf-8"))
    # Anything else must be UTF-8, strictly: errors="replace" turned a cp1251 note into U+FFFD, and
    # NULs (UTF-16 without a BOM, or appended by `>>`) are valid UTF-8 that no term matches. Refuse,
    # and write nothing, rather than print a mangled or unredacted copy.
    try:
        data = raw[len(bom):].decode(enc)
    except UnicodeDecodeError:
        data = None
    if data is None or (enc == "utf-8" and "\x00" in data):
        why = "has NUL bytes (UTF-16 without a BOM?)" if data is not None else f"is not {enc.upper()}"
        sys.stderr.write(f"redact: input {why}; re-save it as UTF-8. Nothing was written.\n")
        sys.exit(2)
    out, hits = redact_text(data)
    done = bom + out.encode(enc)
    # A BOM describes how a file began, not everything after it. Text appended to a UTF-16 file in
    # UTF-8 (`echo >>` from Git Bash), in a code page (cmd.exe's `echo >>` writes the console's:
    # cp866 on a Russian Windows, UTF-8 only after `chcp 65001`; other programs write the ANSI
    # one), or as UTF-16 after an odd number of bytes still decodes as UTF-16, as CJK or
    # private-use characters, so a term in it went out unredacted with "0 substitution(s)" and exit
    # 0. The first fix re-read the input as UTF-8 alone, on the claim that cmd appends UTF-8, which
    # it does not (review, 2026-10-02). So what is about to be written is read every other likely
    # way (scan_secrets.readings), and if any reading still shows a deny-listed term or a
    # credential, nothing is written. Which writer left it there is not known and not claimed.
    # Clean UTF-8 has no other reading, so this costs a UTF-8 file nothing.
    from scan_secrets import readings           # no fallback: without it this check cannot be made
    for way, text in readings(done):
        if way and redact_text(text)[1]:
            sys.stderr.write(f"redact: input starts as {enc.upper()}, but read as {way} it holds a deny-listed"
                             " term or a credential that redaction did not reach (a file in more than one"
                             " encoding?); re-save it as UTF-8. Nothing was written.\n")
            sys.exit(2)
    sys.stderr.write(f"redact: {hits} substitution(s)\n")
    sys.stdout.buffer.write(done)                    # bytes: no newline translation on Windows
