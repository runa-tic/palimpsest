"""Follow-ups to the fixes of the 2026-10-02 review, a numbered group per finding.

A previous daily note that is UTF-16. The fix for a daily note line that is not UTF-8 made
briefing.py read the previous note with errors="replace": a note written by PowerShell 5.1's `>`
(UTF-16) then rolled over no task, with exit 0 and nothing printed, where the strict read before it
had crashed and the sync had shown it.

1. A previous daily note with a BOM is decoded in the encoding the BOM names (UTF-16 in either byte
   order, UTF-32, UTF-8): its open tasks roll over as written, CRLF and Cyrillic included, nothing
   is printed on stderr, and the note's bytes are unchanged.
2. A previous note that does not decode whole gets exactly one line on stderr naming the file, and
   exit 0; the tasks that do read still roll over. The forms: a cp1251 line in a UTF-8 note, UTF-16
   appended to a UTF-8 note (PowerShell 5.1's `>>`), UTF-8 appended to a UTF-16 note at an even
   length (which decodes as UTF-16 without an error), UTF-16 without a BOM.
3. No NUL reaches today's note from such a task, so the next refresh of today's note still exits 0:
   a note with a NUL is one briefing.py refuses to rewrite.
4. The sync logs that line: tools/sync.log holds it after a run with the briefing step.
5. A previous note in plain UTF-8 prints nothing on stderr.

With PALIMPSEST_TOOLS pointed at a tools/ whose briefing.py still reads the previous note with
errors="replace", every check of 1 to 4 fails; 5 is the control and passes there too.

The notes are built byte by byte here, as the tools' comments describe PowerShell's output; no
PowerShell is run, no model is called and nothing is sent anywhere.
"""
import codecs, json, os, subprocess, sys
from datetime import date
from pathlib import Path
from _util import Checks, make_vault, run, write

PAST = "Daily/2001-02-03.md"               # any earlier date: recent_daily() takes the latest one
PASSPORT, PLUMBER = "- [ ] renew the passport", "- [ ] позвонить сантехнику"
NOTE = f"# Saturday\n\n{PASSPORT}\n- [x] done already\n{PLUMBER}\n"
STEPS = ("pull", "import", "extract", "skills", "link", "maintenance", "dedupe", "triage", "weekly",
         "embed", "state", "push")


def today_note(v: Path) -> Path:
    return v / "Daily" / f"{date.today().isoformat()}.md"


def rolled(v: Path) -> list[str] | None:
    """The lines of today's rolled-over list, split on the platform's newline only: a task that
    kept the CR of a CRLF note must show here as a line ending in "\\r", not be split away."""
    t = today_note(v)
    if not t.exists():
        return None
    lines = t.read_bytes().decode("utf-8", errors="replace").split(os.linesep)
    start = next((i for i, l in enumerate(lines) if l.endswith("Rolled-over tasks")), None)
    if start is None:
        return None
    out = []
    for l in lines[start + 1:]:
        if l.startswith(("###", "<!--")):
            break
        if l:
            out.append(l)
    return out


def brief(body: bytes) -> tuple[Path, subprocess.CompletedProcess]:
    """A vault whose only earlier daily note holds `body`, and the briefing.py run that reads it."""
    v = make_vault()
    past = v / PAST
    past.parent.mkdir(parents=True)
    past.write_bytes(body)
    return v, run(v, "briefing.py")


def notice(r) -> bool:
    """Exactly one stderr line, naming the note that was read."""
    lines = [l for l in r.stderr.splitlines() if l.strip()]
    return len(lines) == 1 and PAST in lines[0]


def check_bom_notes(c: Checks) -> None:
    crlf = NOTE.replace("\n", "\r\n")
    for label, body in (
            ("UTF-16 LE with a BOM and CRLF, as PowerShell 5.1's `>` writes it",
             codecs.BOM_UTF16_LE + crlf.encode("utf-16-le")),
            ("UTF-16 BE with a BOM", codecs.BOM_UTF16_BE + NOTE.encode("utf-16-be")),
            ("UTF-32 with a BOM", codecs.BOM_UTF32_LE + crlf.encode("utf-32-le")),
            ("UTF-8 with a BOM and a task on its first line",
             codecs.BOM_UTF8 + f"{PASSPORT}\n{PLUMBER}\n".encode("utf-8"))):
        v, r = brief(body)
        got = rolled(v)
        c.ok(r.returncode == 0 and got == [PASSPORT, PLUMBER] and not r.stderr.strip()
             and (v / PAST).read_bytes() == body,
             f"a previous note in {label}: both open tasks roll over as written, stderr is empty, "
             "the note is unchanged",
             f"exit={r.returncode} rolled={got!r} stderr={r.stderr[-300:]!r}")


def check_undecodable_notes(c: Checks) -> None:
    utf8 = f"# Saturday\n\n{PASSPORT}\n".encode("utf-8")
    # `echo >>` from Git Bash onto a UTF-16 note. An even number of bytes, so the UTF-16 read pairs
    # them all up and raises nothing: the case no decode error announces.
    mixed = codecs.BOM_UTF16_LE + f"{PASSPORT}\r\n".encode("utf-16-le") + b"- [ ] from git bash\n"
    try:
        mixed.decode("utf-16")
        fixture = ""
    except UnicodeDecodeError as e:
        fixture = f" (the fixture no longer decodes as UTF-16: {e})"
    garbled = PLUMBER.encode("cp1251").decode("utf-8", errors="replace")
    for label, body, want in (
            ("a UTF-8 note with a cp1251 line", utf8 + (PLUMBER + "\n").encode("cp1251"), [PASSPORT, garbled]),
            ("a UTF-8 note with UTF-16 appended (PowerShell 5.1's `>>`)",
             utf8 + "- [ ] appended\r\n".encode("utf-16-le"), [PASSPORT]),
            ("a UTF-16 note with UTF-8 appended at an even length, which decodes as UTF-16 without an error",
             mixed, [PASSPORT, "- [ ] from git bash"]),
            ("a UTF-16 note without a BOM", f"{PASSPORT}\r\n".encode("utf-16-le"), ["- *(none)*"])):
        v, r = brief(body)
        got = rolled(v)
        c.ok(r.returncode == 0 and notice(r) and got == want and (v / PAST).read_bytes() == body
             and not (body is mixed and fixture),
             f"{label}: one stderr line names the note, exit 0, and what reads rolls over",
             f"exit={r.returncode} rolled={got!r} want={want!r} stderr={r.stderr[-400:]!r}{fixture}")

    # The last UTF-8 line has no newline, so the appended UTF-16 runs on from it: the task rolled
    # over with the NULs of what followed, into today's note.
    v, r = brief(utf8 + b"- [ ] call the bank" + "- [ ] appended\r\n".encode("utf-16-le"))
    raw = today_note(v).read_bytes() if today_note(v).exists() else b"\x00"
    nul = b"\x00" in raw
    again = run(v, "briefing.py")
    c.ok(r.returncode == 0 and not nul and again.returncode == 0 and "Refreshed" in again.stdout,
         "a task cut short by appended UTF-16 carries no NUL into today's note, and the next refresh "
         "of today's note exits 0",
         f"exit={r.returncode} NUL in today's note: {nul} rolled={rolled(v)!r} | again: exit={again.returncode} "
         f"{(again.stdout + again.stderr)[-300:]!r}")


def check_sync_logs_notice(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", json.dumps({"version": 1, "steps": {**{k: False for k in STEPS}, "briefing": True}}))
    past = v / PAST
    past.parent.mkdir(parents=True)
    past.write_bytes(f"{PASSPORT}\n".encode("utf-8") + (PLUMBER + "\n").encode("cp1251"))
    r = run(v, "sync.py")
    log = v / "tools" / "sync.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    c.ok(r.returncode == 0 and "briefing: done" in r.stdout
         and any(l.startswith("STDERR: briefing:") and PAST in l for l in text.splitlines()),
         "the sync's briefing step is clean and sync.log holds the line naming the note",
         f"exit={r.returncode} stdout={r.stdout[-300:]!r} log={text[-500:]!r}")


def check_clean_note(c: Checks) -> None:
    v, r = brief(NOTE.encode("utf-8"))
    got = rolled(v)
    c.ok(r.returncode == 0 and got == [PASSPORT, PLUMBER] and not r.stderr.strip(),
         "control: a previous note in plain UTF-8 rolls over and prints nothing on stderr",
         f"exit={r.returncode} rolled={got!r} stderr={r.stderr[-300:]!r}")


def main() -> int:
    c = Checks("review 2026-10-02, follow-ups")
    check_bom_notes(c)
    check_undecodable_notes(c)
    check_sync_logs_notice(c)
    check_clean_note(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
