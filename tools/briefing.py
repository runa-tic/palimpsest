#!/usr/bin/env python3
"""Generate (or enrich) today's daily note with a morning briefing.

Fills today's Daily/YYYY-MM-DD.md with:
  - unchecked tasks rolled over from the most recent previous daily note
  - active projects
  - a resurfaced "note of the day" (rotates daily)
  - a digest of conversations from the last 2 days

Safe to re-run: if today's note already has a briefing block it is refreshed in place,
and your own content below it is preserved, as is any rolled-over task you ticked in it.

Usage (from vault root):  python tools/briefing.py
"""
from __future__ import annotations
import codecs, sys, re, random
from pathlib import Path
from datetime import date, datetime

try:
    # UTF-8 whatever the code page, as callers read it, and backslashreplace: under "strict" a
    # surrogate (argv or a path that is not UTF-8) crashed the very error that carried it.
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
DAILY = VAULT / "Daily"
NOTES = VAULT / "10 Notes"
PROJECTS = VAULT / "20 Projects"
CONVS = VAULT / "40 Resources" / "Claude Conversations"
START, END = "<!-- briefing:start -->", "<!-- briefing:end -->"
# One well-formed block: a START with no other START before its END. A plain START.*?END ran from
# an orphan START (its END deleted while editing) to the next block's END, and the refresh
# replaced everything the user had written in between.
BLOCK = re.compile(re.escape(START) + r"(?:(?!" + re.escape(START) + r").)*?" + re.escape(END), re.DOTALL)
ROLLED = "### ↩️ Rolled-over tasks"
TASK_LINE = re.compile(r"^- \[[ xX]\] (.+?)[ \t]*$", re.M)

def recent_daily(before: str) -> Path | None:
    cands = sorted(p for p in DAILY.glob("*.md")
                   if re.match(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem < before)
    return cands[-1] if cands else None

# The encoding a BOM names, for a note that is only read. UTF-32 first: UTF-32 LE's BOM begins with
# UTF-16 LE's.
READ_BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
             (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"), (codecs.BOM_UTF8, "utf-8-sig"))
OPEN_TASK = re.compile(r"^\s*- \[ \] (.+)$", re.M)


def tasks_in(text: str) -> list[str]:
    # Line ends as a text-mode read gives them (bytes.decode keeps the CRs, and a task would roll
    # over ending in "\r"), and a NUL ends a line too: a task that carried one into today's note
    # made every later refresh of it exit 1, because main() refuses to rewrite a note with a NUL.
    return OPEN_TASK.findall(re.sub(r"\r\n?|\x00", "\n", text))


def open_tasks(p: Path | None) -> list[str]:
    if not p or not p.exists():
        return []
    # The note is only read here, so it is decoded in the encoding its BOM names. A note written by
    # PowerShell 5.1's `>` is UTF-16: read as UTF-8 with errors="replace" it rolled over no task,
    # with exit 0 and nothing printed, where the strict read before that had crashed and the sync
    # had shown it (review, 2026-10-02).
    raw = p.read_bytes()
    enc = next((e for bom, e in READ_BOMS if raw.startswith(bom)), "utf-8")
    try:
        text = raw.decode(enc)
        whole = "\x00" not in text
    except UnicodeDecodeError:
        # Never a raise: one line saved in the ANSI code page made this fail before today's note
        # existed, and recent_daily() picked the same note again every later day. A task with such
        # bytes rolls over showing U+FFFD.
        text, whole = raw.decode(enc, errors="replace"), False
    tasks = tasks_in(text)
    if enc in ("utf-16", "utf-32"):
        # A BOM says how the note began, not what was appended to it: UTF-8 added by a shell's
        # `echo >>` reads as CJK in UTF-16, and without an error when it is an even number of
        # bytes. Read as UTF-8, only such text holds a task.
        appended = tasks_in(raw.decode("utf-8", errors="replace"))
        tasks, whole = tasks + appended, whole and not appended
    if not whole:
        # Exit 0 all the same: nothing is written to this note, and today's briefing is still due.
        # stderr is what the sync logs.
        name = {"utf-8-sig": "UTF-8"}.get(enc, enc.upper())
        print(f"briefing: Daily/{p.name} is not all {name} text (a line in an ANSI code page, or text "
              "appended in another encoding by a shell's `>>`?); a task in such a line rolls over "
              "garbled or not at all. Re-save it as UTF-8.", file=sys.stderr)
    return tasks

def active_projects() -> list[str]:
    out = []
    for p in PROJECTS.glob("*.md"):
        if p.name.startswith("_"):
            continue
        if re.search(r"^status:\s*active", p.read_text(encoding="utf-8", errors="ignore"), re.M):
            out.append(p.stem)
    # sorted: glob order is the filesystem's (APFS and NTFS disagree), so two machines rendering
    # the same day produced blocks that differed only in order — and a union merge keeps both.
    return sorted(out)

def note_of_day() -> str | None:
    notes = [p.stem for p in NOTES.glob("*.md") if not p.name.startswith("_")]
    if not notes:
        return None
    random.seed(date.today().toordinal())  # stable for the whole day
    return random.choice(sorted(notes))

def recent_convs(days: int = 2) -> list[str]:
    cutoff = datetime.now().toordinal() - days
    out = []
    for p in CONVS.rglob("*.md"):
        if p.name.startswith("_"):
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})", p.stem)
        if m:
            try:
                if datetime.strptime(m.group(1), "%Y-%m-%d").toordinal() >= cutoff:
                    out.append(p.stem)
            except ValueError:
                pass
    return sorted(out)  # same reason as active_projects(): byte-identical on every machine

def skills_nudge() -> str | None:
    """One line about the proposal queue, or nothing if there is nothing to do.

    The queue reached 622 against 20 promoted precisely because it was invisible — it lived
    in a folder nobody opened. Surfacing the batch size (not the queue size) is the point:
    "8 to look at" is an invitation, "622 waiting" is a reason to close the note.
    """
    proposed = VAULT / "Skills" / "_proposed"
    review = VAULT / "Reviews" / "Skill Proposals.md"
    if not proposed.exists():
        return None
    n = len(list(proposed.glob("*.md")))
    if not n:
        return None
    if not review.exists():
        return (f"- {n} unreviewed proposal(s). Run `python tools/triage_skills.py` to get a "
                f"batch worth reading.")
    m = re.search(r"→\s*(\d+)\s+in this batch", review.read_text(encoding="utf-8", errors="ignore"))
    batch = m.group(1) if m else "a few"
    return (f"- We have **{n} proposed skills**; I've picked **{batch}** worth tackling in "
            f"[[Skill Proposals]]. Promote the keepers, delete the rest — 10 minutes.")


def dupes_nudge() -> str | None:
    """One line about the duplicate backlog, naming the biggest family only.

    dedupe.py has flagged 35 families / 73 removable notes for weeks and none were ever
    merged, for the same reason 622 skill proposals were never read: a report nobody opens
    is a report that does not exist. Name one family, not the total — "merge these 5" is a
    task, "73 removable" is a statistic.
    """
    rpt = VAULT / "Reviews" / "Duplicate Candidates.md"
    if not rpt.exists():
        return None
    txt = rpt.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"would remove (\d+) files", txt)
    removable = int(m.group(1)) if m else 0
    if removable < 1:
        return None
    fam = re.search(r"^## (\d+) notes — up to \d+ removable\n((?:- \[\[.+?\]\]\n)+)", txt, re.M)
    if not fam:
        return None
    members = re.findall(r"\[\[(.+?)\]\]", fam.group(2))
    return (f"- **{removable} duplicate note(s)** could be merged away. Biggest family is "
            f"**{fam.group(1)} notes** starting with [[{members[0]}]] — fold them into the "
            f"best-written one. Full list: [[Duplicate Candidates]].")


def memory_nudge() -> str | None:
    """Memory entries asserting state that has aged out.

    This is the one surface where staleness is actively dangerous: memory is loaded into
    context at the start of every session and read as current fact. An entry still saying
    the operator is abroad, 47 days after they came home, does not sit inertly — it shapes
    what gets assumed and acted on.
    """
    health = VAULT / "Reviews" / "Vault Health.md"
    if not health.exists():
        return None
    txt = health.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"## 🧠 Memory needing re-verification \((\d+)\)", txt)
    if not m or m.group(1) == "0":
        return None
    first = re.search(r"^- `(\d+)d` \*\*(.+?)\*\*", txt[m.end():], re.M)
    oldest = f" Oldest is **{first.group(2)}** at {first.group(1)} days." if first else ""
    return (f"- **{m.group(1)} memory entries** assert state that has aged out.{oldest} "
            f"They load into context every session and are read as current — confirm, correct "
            f"or delete. List: [[Vault Health]].")


def build_block() -> str:
    today = date.today().isoformat()
    tasks = open_tasks(recent_daily(today))
    lines = [START, f"## 🌅 Briefing — {date.today():%A, %B %d}", ""]
    lines.append("### ↩️ Rolled-over tasks")
    lines += [f"- [ ] {t}" for t in tasks] if tasks else ["- *(none)*"]
    lines.append("\n### 🎯 Active projects")
    aps = active_projects()
    lines += [f"- [[{a}]]" for a in aps] if aps else ["- *(none)*"]
    nod = note_of_day()
    lines.append("\n### 💡 Note of the day")
    lines.append(f"- [[{nod}]]" if nod else "- *(no notes yet)*")
    rc = recent_convs()
    if rc:
        lines.append("\n### 🤖 Recent conversations")
        lines += [f"- [[{c}]]" for c in rc]
    # Last, deliberately. These are invitations, not tasks: putting them above the day's
    # actual work would train you to skip the whole block.
    nudge = skills_nudge()
    if nudge:
        lines.append("\n### 🛠 Skill proposals")
        lines.append(nudge)
    dn = dupes_nudge()
    if dn:
        lines.append("\n### 👯 Duplicate notes")
        lines.append(dn)
    mn = memory_nudge()
    if mn:
        lines.append("\n### 🧠 Memory freshness")
        lines.append(mn)
    lines.append(END)
    return "\n".join(lines)

def rolled_span(block: str) -> tuple[int, int] | None:
    """Where the rolled-over task list sits in a block: after its heading, up to the next one."""
    h = block.find(ROLLED + "\n")
    if h < 0:
        return None
    a = h + len(ROLLED) + 1
    nxt = re.compile(r"^\s*(?:###|" + re.escape(END) + ")", re.M).search(block, a)
    return a, nxt.start() if nxt else len(block)


def keep_ticks(block: str, old: str) -> str:
    """Carry the rolled-over list of the block being replaced into the new one.

    The rolled-over tasks are ticked HERE — this block is where they show up in the day's note —
    and every refresh (each session start, each sync) rebuilt them from yesterday's note as
    "- [ ]", so a task done this morning came back open, and rolled over again tomorrow. The old
    list is kept as it is (ticks, and any task added to it by hand); a task not in it is added.
    """
    new_s, old_s = rolled_span(block), rolled_span(old)
    if not new_s or not old_s:
        return block
    prev = old[old_s[0]:old_s[1]]
    kept = set(TASK_LINE.findall(prev))
    lines = [l for l in prev.splitlines() if TASK_LINE.match(l)]
    lines += [l for l in block[new_s[0]:new_s[1]].splitlines()
              if TASK_LINE.match(l) and TASK_LINE.match(l).group(1) not in kept]
    return block[:new_s[0]] + "\n".join(lines or ["- *(none)*"]) + "\n" + block[new_s[1]:]


def main():
    today = date.today().isoformat()
    DAILY.mkdir(parents=True, exist_ok=True)
    f = DAILY / f"{today}.md"
    block = build_block()
    if f.exists():
        # surrogateescape on the read AND the write, never "replace": this note is written back, and
        # a line the user saved in another code page must come back as the bytes they wrote, not as
        # U+FFFD (the strict read raised on it instead; review, 2026-10-02).
        raw = f.read_bytes()
        if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) or b"\x00" in raw:
            # UTF-16, whole (PowerShell 5.1's `>`) or appended (its `>>`): surrogateescape keeps
            # its bytes but the text-mode round trip turns its CRs into LFs and appends UTF-8 to
            # it, so refreshing it corrupts it. Leave it byte-identical and fail where the sync
            # shows it (review of the 2026-10-02 fix).
            print(f"briefing: Daily/{today}.md is not UTF-8 text (UTF-16, as PowerShell 5.1's `>` "
                  "and `>>` write?); left untouched. Re-save it as UTF-8.", file=sys.stderr)
            sys.exit(1)
        txt = f.read_text(encoding="utf-8", errors="surrogateescape")
        if any("\udc80" <= ch <= "\udcff" for ch in txt):
            print(f"briefing: Daily/{today}.md has a line that is not UTF-8 (an ANSI code page?); "
                  "kept as written. Re-save it as UTF-8 to see it in the briefing.", file=sys.stderr)
        # A callback, not the string: re.sub reads backslashes in a replacement string as escapes,
        # so a rolled-over task holding a Windows path (C:\Users\...) raised "bad escape \U" on
        # every refresh (review, 2026-09-30).
        txt, n = BLOCK.subn(lambda m: keep_ticks(block, m.group(0)), txt)
        if not n:
            # No well-formed block (never rendered, or a marker was deleted): add a fresh one and
            # leave any orphan marker, and everything around it, exactly as it is.
            txt = txt.rstrip() + "\n\n" + block + "\n"
        # A strict write here raises after write_text has truncated the file: the note is emptied.
        f.write_text(txt, encoding="utf-8", errors="surrogateescape")
        print(f"Refreshed briefing in Daily/{today}.md")
    else:
        header = f"---\ntype: daily\ndate: {today}\ntags:\n  - daily\n---\n\n# {date.today():%A, %B %d, %Y}\n\n"
        body = "\n\n## 📝 Log\n- \n"
        f.write_text(header + block + body, encoding="utf-8")
        print(f"Created Daily/{today}.md with briefing")

if __name__ == "__main__":
    main()
