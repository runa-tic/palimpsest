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

The deny list is read line by line in whatever Windows saved or appended it as (UTF-8, UTF-16,
cp1251/cp1252). A line that is not clean UTF-8 or UTF-16 is checked in every likely reading, AND
the commit is blocked until the file is re-saved as UTF-8: which codepage it is cannot be known,
so a clean result over it would be a guess. A `re:` line whose pattern does not compile blocks
the commit as well: nothing was checked against it.

A deny list that is there but cannot be read (no permission, a directory or a dangling link in its
place) blocks every commit too: nothing was checked against it, and until the review of 2026-10-02
that passed as clean without a word.

A staged file, or a staged name, that is not clean UTF-8 is checked in every likely reading as
well (scan_secrets.readings: its BOM's encoding, UTF-8, cp1251, cp1252, cp866, UTF-16 from either
byte), and a BLOCK term in any of them counts: a file's first bytes do not say what a later writer
appended to it. WARN terms are matched in the usual reading alone, and a file that starts as a
known binary format does has that one reading (scan_secrets.known_binary).

Values are never printed. The Stop hook records this session into the vault, so echoing an
address while removing it just recreates the leak in a new file; masked forms only.

Usage:
  python tools/scan_pii.py        # scan staged changes (used by pre-commit)
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import BAD_PATTERN, DENY_FILE, DenyListUnreadable, is_hard, load_deny_report, term_pattern   # the one parser/matcher
from scan_secrets import NotScanned, masked_path, path_bytes, path_readings, staged_blobs, staged_files


def mask(term: str) -> str:
    """Identifiable to the operator, useless to a reader. Never the value itself."""
    head = term[:2]
    return f"{head}{'*' * max(3, len(term) - 2)}"


def _safe_path(rel: str, literals, regexes) -> str:
    """The path as printed: every deny-listed term in it masked, since the path itself may be
    what carries the term (and this output is recorded into the vault)."""
    raw = path_bytes(rel)
    if raw is not None:         # not UTF-8: mask what any reading of its bytes shows (see masked_path)
        rxs = [term_pattern(t) for t in literals] + list(regexes)
        return masked_path(raw, rel, lambda text: (
            (m.start(), m.end(), mask(m.group(0))) for rx in rxs for m in rx.finditer(text)))
    for t in sorted(literals, key=len, reverse=True):
        rel = term_pattern(t).sub(lambda m: mask(m.group(0)), rel)
    for rx in regexes:
        rel = rx.sub(lambda m: mask(m.group(0)), rel)
    return rel


def _scan(rel: str, way: str, content: str, hard, soft, regexes, blocking: dict, warning: dict) -> None:
    """Add this text's match counts to blocking / warning, keyed (file label, term) with the
    masked term as the value's label: a large file arrives in several runs of lines. Counts are
    kept per reading (`way`, "" for the usual one): the readings of a file are the same bytes, so
    adding them up would count one occurrence up to nine times (see _counted).

    Another reading is matched against the BLOCK terms alone (is_hard, and the patterns). A WARN
    term is a few letters or digits, which the wrong reading of any bytes holds by chance: "Ng"
    was counted 300 times in a UTF-16 file of 300 lines of Chinese with no such letters in it, and
    a two-letter Cyrillic term 38 times in 600KB of noise read as cp1251 (review, 2026-10-04)."""
    if not content:
        return
    for terms, into in ((hard, blocking), (soft if not way else (), warning)):
        for term in terms:
            if n := len(term_pattern(term).findall(content)):
                by = into.setdefault((rel, term), (mask(term), {}))[1]
                by[way] = by.get(way, 0) + n
    for rx in regexes:
        if n := len(rx.findall(content)):
            by = blocking.setdefault((rel, rx), (f"re:{mask(rx.pattern)}", {}))[1]
            by[way] = by.get(way, 0) + n


def _counted(found: dict) -> list[tuple[str, str, int, str]]:
    """(file label, masked term, count, note) per finding: the count of the reading that saw the
    term most often, and a note naming that reading when it is not the usual one, since the
    file's own editor does not show the term there. A warning is always of the usual reading."""
    out = []
    for (rel, _), (shown, by) in found.items():
        way = max(by, key=lambda w: (by[w], not w))         # the usual reading wins a tie
        out.append((rel, shown, by[way], f"  (read as {way})" if way else ""))
    return out


def main() -> int:
    try:
        literals, regexes, problems = load_deny_report()
    except DenyListUnreadable as e:
        # Fail closed. This returned "no terms", so the scan exited 0 having checked nothing, and
        # the unattended push went on trusting it (review, 2026-10-02). The reason is an error's
        # name or fixed words, never a value: nothing of the list was read.
        print("")
        print(f"Commit blocked by pii-scan — tools/{DENY_FILE.name} is there but cannot be read ({e}),")
        print("so no staged file was checked against it. Make it a file this user can read and commit")
        print("again. If you mean to have no deny list, delete it.")
        return 1
    if not literals and not regexes and not problems:
        return 0
    hard = [t for t in literals if is_hard(t)]
    soft = [t for t in literals if not is_hard(t)]

    found: dict[tuple, tuple[str, dict[str, int]]] = {}      # (label, term) -> (masked, count per reading)
    warned: dict[tuple, tuple[str, dict[str, int]]] = {}
    unscanned: list[tuple[str, str]] = []
    failed = False
    for rel, blocks in staged_blobs(staged_files()):
        # Scan the path as well as the content: a note named after a person or a conversation
        # title carries the term in its filename, where a contents-only scan never looks.
        # A name that is not UTF-8 is read every likely way: as the lossless decode has it, its
        # bytes are surrogates that no term matches (review, 2026-10-02).
        path_label = _safe_path(f"{rel} [path]", literals, regexes)
        for way, text in path_readings(rel):
            _scan(path_label, way, text, hard, soft, regexes, found, warned)
        label = _safe_path(rel, literals, regexes)
        try:
            for _, way, text, _ in blocks:
                _scan(label, way, text, hard, soft, regexes, found, warned)
        except NotScanned as e:
            unscanned.append((label, e.why))
            failed |= e.fails
    blocking, warning = _counted(found), _counted(warned)

    for rel, why in unscanned:
        print(f"pii-scan: NOT scanned ({why}): {rel}")
    if failed:
        print("pii-scan: FAILED — a staged file above could not be scanned, so the commit is not clean.")

    for rel, m, n, note in warning:
        print(f"pii-scan WARN: {rel} — {n}x deny-listed literal {m} (numeric/short; not blocking){note}")

    unclean = [(i, what) for i, what in problems if what != BAD_PATTERN]
    if unclean:
        # Fail closed, as the strict read did before (with a traceback): a line in a codepage that
        # is neither cp1251 nor cp1252 is a term this scan cannot match, and a one-line stderr
        # warning in an unattended push surfaces nowhere. Line numbers only, never the values.
        print("")
        print(f"Commit blocked by pii-scan — tools/{DENY_FILE.name} is not clean UTF-8:")
        for i, what in unclean:
            print(f"  line {i}: {what}")
        print("Every term was still checked in each likely reading" + (" (findings below)." if blocking else "."))
        print("Open it, check that the lines listed read correctly, save it as UTF-8 (Notepad:")
        print("Save As, Encoding UTF-8) and commit again.")
    if len(unclean) < len(problems):
        # Fail closed here too. A pattern that does not compile was dropped without a word, so a
        # list holding only that line gave exit 0 with nothing printed, and whatever the pattern
        # was written to catch was committed (review, 2026-10-04). The number, never the pattern.
        print("")
        print(f"Commit blocked by pii-scan — tools/{DENY_FILE.name} has a pattern that cannot be used:")
        for i, what in problems:
            if what == BAD_PATTERN:
                print(f"  line {i}: {what}")
        print("Nothing was checked against it. Correct the pattern, or remove the line, and commit again.")
    if not blocking:
        if problems or failed:
            return 1
        print("pii-scan: clean (staged changes).")
        return 0

    print("")
    print("Commit blocked by pii-scan — staged content contains deny-listed PII:")
    for rel, m, n, note in blocking:
        print(f"  {rel}: {n}x {m}{note}")
    print("")
    print("Scrub the value from the file (do NOT paste it into the terminal — this session")
    print("is recorded into the vault). If the term no longer needs denying, remove it from")
    print("tools/.redact_terms.txt. To bypass once: git commit --no-verify")
    return 1


if __name__ == "__main__":
    sys.exit(main())
