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

A weekly review with UTF-16 in it. weekly_review reads and writes the review with surrogateescape,
which keeps the bytes of a UTF-16 tail but not its line ends: "\r\x00\n\x00" came back as
"\n\x00\n\x00" (on macOS), with exit 0 and nothing printed.

6. weekly_review leaves a review with UTF-16 appended to it (PowerShell 5.1's `>>`), or UTF-16 as a
   whole, byte-identical, exits 1 and says it is UTF-16. Re-saved as UTF-8, the review is refreshed
   again.

With a weekly_review.py without that guard the first two fail: the appended one is rewritten with
exit 0, and the whole one, though left alone with exit 1, is reported as having no generated block.
The re-save is the control and passes there too.

Test vault builders. Six of them, in five scripts, still copied TOOLS_SRC with shutil.copytree
after make_vault had stopped, so a used clone's gitignored state (its deny list, sync receipt and
logs/) reached their vaults.

7. No test script calls copytree on TOOLS_SRC: _util.copy_tools is how a tools/ tree reaches a
   test vault.

Check 7 reads the test scripts next to this one, which PALIMPSEST_TOOLS does not swap: copied into
a tests/ where a builder still copies TOOLS_SRC whole, it fails and names each call.

The notes are built byte by byte here, as the tools' comments describe PowerShell's output; no
PowerShell is run, no model is called and nothing is sent anywhere.
"""
import ast, codecs, json, os, subprocess, sys
from datetime import date
from pathlib import Path
from _util import Checks, make_vault, run, write

HERE = Path(__file__).resolve().parent
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


def check_weekly(c: Checks) -> None:
    v = make_vault()
    write(v, PAST, f"{PASSPORT}\n")
    run(v, "weekly_review.py")
    weekly = sorted((v / "Reviews" / "Weekly").glob("*.md"))
    if not weekly:
        c.ok(False, "weekly_review wrote this week's file to append to")
        return
    out = weekly[0]
    first = out.read_bytes()
    write(v, "20 Projects/Garden.md", "---\nstatus: active\n---\n")      # so a refresh changes the block
    for label, body in (
            ("UTF-16 appended to its Reflection (PowerShell 5.1's `>>`)",
             first + "- from PowerShell\r\n".encode("utf-16-le")),
            ("UTF-16 as a whole, with a BOM",
             codecs.BOM_UTF16_LE + first.decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le"))):
        out.write_bytes(body)
        r = run(v, "weekly_review.py")
        c.ok(r.returncode == 1 and out.read_bytes() == body and out.name in r.stderr and "UTF-16" in r.stderr,
             f"weekly_review leaves a review with {label} byte-identical, exits 1 and says it is UTF-16",
             f"exit={r.returncode} same={out.read_bytes() == body} tail={out.read_bytes()[-12:]!r} "
             f"stderr={r.stderr[-300:]!r}")
    out.write_bytes(first)
    r = run(v, "weekly_review.py")
    c.ok(r.returncode == 0 and b"[[Garden]]" in out.read_bytes(),
         "control: re-saved as UTF-8, the review is refreshed again",
         f"exit={r.returncode} {(r.stdout + r.stderr)[-300:]!r}")


def copies_of_tools_src(source: str) -> list[int]:
    """The lines of every copytree(...) call with TOOLS_SRC among its arguments."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) != "copytree":
            continue
        args = [*node.args, *(k.value for k in node.keywords)]
        if any(isinstance(n, ast.Name) and n.id == "TOOLS_SRC" or isinstance(n, ast.Attribute) and n.attr == "TOOLS_SRC"
               for a in args for n in ast.walk(a)):
            out.append(node.lineno)
    return out


def check_builders(c: Checks) -> None:
    scripts = sorted(HERE.glob("test_*.py"))
    found = [f"{p.name}:{n}" for p in scripts for n in copies_of_tools_src(p.read_text(encoding="utf-8"))]
    # The scan itself, on the call it looks for: a scan that finds nothing anywhere proves nothing.
    sees = (copies_of_tools_src('shutil.copytree(TOOLS_SRC, v / "tools")\n') == [1]
            and copies_of_tools_src('copytree(dst=d, src=_util.TOOLS_SRC / "x")\n') == [1]
            and copies_of_tools_src('_util.copy_tools(v / "tools")\nshutil.copytree(a, b)\n') == [])
    c.ok(sees and Path(__file__).resolve() in scripts and not found,
         f"none of the {len(scripts)} test scripts copies TOOLS_SRC with copytree; vaults get their "
         "tools from _util.copy_tools", f"scan works: {sees}; calls: {', '.join(found) or 'none'}")


def main() -> int:
    c = Checks("review 2026-10-02, follow-ups")
    check_bom_notes(c)
    check_undecodable_notes(c)
    check_sync_logs_notice(c)
    check_clean_note(c)
    check_weekly(c)
    check_builders(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
