"""A daily note with one line that is not UTF-8 no longer breaks the opener, the briefing or the review.

PowerShell 5.1 appends in the ANSI code page (cp1251 on a Russian Windows), and one such line in a
daily note made the SessionStart hook exit 1 with no output, and briefing.py raise before creating
today's note, on every later day too (recent_daily() kept picking the broken note). weekly_review
had the same strict read of a file the user writes in (review, 2026-10-02).

1. briefing.py creates today's note when the previous daily note has a cp1251 line, and leaves
   that note's bytes as they were.
2. The opener exits 0 with its JSON and the briefing in it, from that same vault.
3. A cp1251 line in today's note: the refresh rewrites the block and keeps every byte outside it
   (no U+FFFD written over the user's line), and the opener still shows the briefing.
4. weekly_review refreshes its block and keeps a cp1251 line in the Reflection byte for byte.

Each of 1-4 has a check that fails with PALIMPSEST_TOOLS pointed at tools/ from 60b4bff (the
byte checks alone pass there, because the old tools raised before writing anything).
"""
import json, os, subprocess, sys
from datetime import date
from pathlib import Path
from _util import Checks, make_vault, run, write

CFG = json.dumps({"version": 1, "steps": {"pull": False}})
PAST = "Daily/2001-02-03.md"               # any earlier date: recent_daily() takes the latest one
FFFD = b"\xef\xbf\xbd"                     # U+FFFD in UTF-8: what errors="replace" writes


def ansi_line(text: str) -> bytes:
    """A line as PowerShell 5.1 appends it on a Russian Windows: cp1251, the platform's newline.
    The native newline, because the tools' text-mode writes already normalise line endings; what
    must survive is the bytes of the line itself."""
    return (text + "\n").encode("cp1251").replace(b"\n", os.linesep.encode())


def opener(v: Path) -> tuple[subprocess.CompletedProcess, str | None]:
    env = {k: val for k, val in os.environ.items() if k != "CLAUDE_BRAIN_NO_HOOK"}
    r = subprocess.run([sys.executable, str(v / "tools" / "hook_session_start.py")], cwd=v,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    try:
        return r, json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return r, None


def split_block(raw: bytes, start: str, end: str) -> tuple[bytes, bytes] | None:
    """The bytes before the generated block and after it."""
    a, b = raw.find(start.encode()), raw.find(end.encode())
    return (raw[:a], raw[b + len(end.encode()):]) if 0 <= a < b else None


def check_previous_note(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", CFG)
    past = v / PAST
    past.parent.mkdir(parents=True)
    past.write_bytes("# Saturday\n\n- [ ] renew the passport\n".encode("utf-8")
                     + ansi_line("- [ ] позвонить сантехнику"))
    original = past.read_bytes()
    today = v / "Daily" / f"{date.today().isoformat()}.md"

    r = run(v, "briefing.py")
    raw = today.read_bytes() if today.exists() else b""
    c.ok(r.returncode == 0 and b"- [ ] renew the passport" in raw,
         "briefing creates today's note, tasks rolled over, past a cp1251 line in the previous one",
         f"exit={r.returncode} exists={today.exists()}\n{(r.stdout + r.stderr)[-400:]}")
    c.ok(past.read_bytes() == original, "...and the previous note's bytes are unchanged")
    try:
        raw.decode("utf-8")
        valid = bool(raw)
    except UnicodeDecodeError:
        valid = False
    c.ok(valid, "...and today's note is valid UTF-8: the cp1251 task rolls over as U+FFFD, not as raw bytes",
         repr(raw[-300:]))

    today.unlink(missing_ok=True)
    r, ctx = opener(v)
    c.ok(r.returncode == 0 and ctx is not None and "renew the passport" in ctx,
         "the opener exits 0 with its JSON and the briefing rendered from that vault",
         f"exit={r.returncode} stdout={r.stdout[-300:]!r} stderr={r.stderr[-400:]!r}")
    c.ok(past.read_bytes() == original, "...and the previous note's bytes are still unchanged")


def check_todays_note(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", CFG)
    write(v, PAST, "# Saturday\n\n- [ ] renew the passport\n")
    today = v / "Daily" / f"{date.today().isoformat()}.md"
    run(v, "briefing.py")
    if not today.exists():
        c.ok(False, "briefing created today's note to append to")
        return
    mine = ansi_line("- 09:40 заметка из PowerShell")
    with open(today, "ab") as fh:
        fh.write(mine)
    before = today.read_bytes()

    write(v, "20 Projects/Garden.md", "---\nstatus: active\n---\n")      # so the refresh changes the block
    r = run(v, "briefing.py")
    after = today.read_bytes()
    sb, sa = split_block(before, "<!-- briefing:start -->", "<!-- briefing:end -->"), \
        split_block(after, "<!-- briefing:start -->", "<!-- briefing:end -->")
    c.ok(r.returncode == 0 and b"[[Garden]]" in after and "not UTF-8" in r.stderr,
         "briefing refreshes today's note when it holds a cp1251 line, and says the line is not UTF-8",
         f"exit={r.returncode}\n{(r.stdout + r.stderr)[-400:]}")
    c.ok(sb is not None and sb == sa and mine in after and FFFD not in after,
         "...and every byte outside the block is the user's, the cp1251 line included, no U+FFFD",
         f"before={before[-200:]!r}\nafter={after[-200:]!r}")

    r, ctx = opener(v)
    c.ok(r.returncode == 0 and ctx is not None and "renew the passport" in ctx,
         "the opener exits 0 with its JSON and today's briefing when today's note has a cp1251 line",
         f"exit={r.returncode} stdout={r.stdout[-300:]!r} stderr={r.stderr[-400:]!r}")
    c.ok(mine in today.read_bytes() and FFFD not in today.read_bytes(),
         "...and the opener's own refresh kept that line's bytes too")

    # UTF-16 cannot round-trip through the text-mode refresh: whole (PowerShell 5.1 `>`) or
    # appended (`>>`), the note is left byte-identical and the run fails where the sync shows it.
    for label, body in (("a UTF-16 note with a BOM", "\ufeff# Today\r\n- [ ] a task\r\n".encode("utf-16-le")),
                        ("a UTF-8 note with a UTF-16 append",
                         before + "- 10:00 from PowerShell >>\r\n".encode("utf-16-le"))):
        today.write_bytes(body)
        r = run(v, "briefing.py")
        c.ok(r.returncode == 1 and today.read_bytes() == body and "not UTF-8" in r.stderr,
             f"briefing leaves {label} byte-identical and fails, saying why",
             f"exit={r.returncode} same={today.read_bytes() == body} {r.stderr[-300:]!r}")


def check_weekly(c: Checks) -> None:
    v = make_vault()
    write(v, PAST, "- [ ] renew the passport\n")
    run(v, "weekly_review.py")
    weekly = sorted((v / "Reviews" / "Weekly").glob("*.md"))
    if not weekly:
        c.ok(False, "weekly_review wrote this week's file to append to")
        return
    out = weekly[0]
    mine = ansi_line("- заметка из PowerShell")
    with open(out, "ab") as fh:
        fh.write(mine)
    before = out.read_bytes()

    write(v, "20 Projects/Garden.md", "---\nstatus: active\n---\n")
    r = run(v, "weekly_review.py")
    after = out.read_bytes()
    sb, sa = split_block(before, "<!-- weekly:start -->", "<!-- weekly:end -->"), \
        split_block(after, "<!-- weekly:start -->", "<!-- weekly:end -->")
    c.ok(r.returncode == 0 and b"[[Garden]]" in after,
         "weekly_review refreshes its block when the Reflection holds a cp1251 line",
         f"exit={r.returncode}\n{(r.stdout + r.stderr)[-400:]}")
    c.ok(sb is not None and sb == sa and mine in after and FFFD not in after,
         "...and keeps every byte outside the block, the cp1251 line included, no U+FFFD",
         f"before={before[-200:]!r}\nafter={after[-200:]!r}")


def main() -> int:
    c = Checks("daily reads (review 2026-10-02)")
    check_previous_note(c)
    check_todays_note(c)
    check_weekly(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
