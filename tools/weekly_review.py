#!/usr/bin/env python3
"""Generate this week's review note: what landed, what's open, where projects stand.

Writes Reviews/Weekly/YYYY-Www.md (per ISO week). Safe to run daily — it refreshes the
generated block of the current week's file in place; your Reflection, and any loop you ticked,
are kept. Wired into sync.py.

Usage (from vault root):  python tools/weekly_review.py
"""
from __future__ import annotations
import codecs, sys, re, time, subprocess
from pathlib import Path
from datetime import date, timedelta

try:
    # UTF-8 whatever the code page, as callers read it, and backslashreplace: under "strict" a
    # surrogate (argv or a path that is not UTF-8) crashed the very error that carried it.
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
NOTES = VAULT / "10 Notes"
PROJECTS = VAULT / "20 Projects"
DAILY = VAULT / "Daily"
CONVS = VAULT / "40 Resources" / "Claude Conversations"
START, END = "<!-- weekly:start -->", "<!-- weekly:end -->"
# One well-formed pair: a START with no other START before its END (see briefing.py).
PAIR = re.compile(re.escape(START) + r"(?:(?!" + re.escape(START) + r").)*?" + re.escape(END), re.DOTALL)
REFLECTION = "## ✍️ Reflection\n- What did I learn? What's the one thing to push next week?\n"

def git_times(folder: Path, first: bool) -> dict[Path, float]:
    """Commit time per file under folder: when it was first added, or last changed."""
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "log", "--format=@%ct", "--name-only",
                            "--relative", *(["--diff-filter=A"] if first else []), "--",
                            str(folder.relative_to(VAULT))], cwd=VAULT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out, ts = {}, None
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        if line.startswith("@"):
            ts = float(line[1:])
        elif line and ts is not None and (first or VAULT / line not in out):
            out[VAULT / line] = ts          # log is newest first: first=True keeps the oldest
    return out

def recent(folder: Path, days=7, recurse=False, keys=("created",), first=True):
    """Files dated within the last `days`.

    Not by mtime: a clone, or a pull that rewrites a file, sets it to the checkout time, so on the
    second machine every old note was "new this week" and the two machines committed different
    reviews. The date is the frontmatter's (`keys`, in order), else the filename's leading date,
    else git history (first add, or last commit), and mtime only for a file git has never seen.
    """
    since = date.today() - timedelta(days=days)
    cutoff = time.time() - days * 86400
    it = folder.rglob("*.md") if recurse else folder.glob("*.md")
    out, gt = [], None
    for p in it:
        if p.name.startswith("_"):
            continue
        with p.open(encoding="utf-8", errors="ignore") as fh:
            head = fh.read(1500).lstrip("\ufeff")          # transcripts are large; the header is enough
        fm = re.match(r"---\n(.*?)\n---", head, re.DOTALL)
        found = [re.search(rf"^{k}:\s*[\"']?(\d{{4}}-\d{{2}}-\d{{2}})", fm.group(1), re.M)
                 for k in keys] if fm else []
        found.append(re.match(r"(\d{4}-\d{2}-\d{2})", p.stem))
        d = next((m.group(1) for m in found if m), None)
        try:
            day = date.fromisoformat(d) if d else None
        except ValueError:
            day = None
        if day:
            if day >= since:
                out.append(p)
            continue
        if gt is None:
            gt = git_times(folder, first)
        if gt.get(p, p.stat().st_mtime) >= cutoff:
            out.append(p)
    return out

TASK = re.compile(r"^\s*- \[ \] (.+)$", re.M)
# The encoding a BOM names, for a note that is only read (as briefing.py's READ_BOMS). UTF-32 first:
# UTF-32 LE's BOM begins with UTF-16 LE's.
READ_BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
             (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"), (codecs.BOM_UTF8, "utf-8-sig"))
# Where a line of such a note ends: at a line end as a text-mode read gives it, and at a NUL or any
# other control character but a tab.
LINE_END = re.compile(r"\r\n?|[\x00-\x08\x0b-\x1f\x7f]")

def note_text(p: Path) -> str:
    """A daily or project note as the text this review quotes from: no NUL and no other control
    character in any line of it.

    The notes were read as UTF-8 with errors="ignore", which keeps the NULs of UTF-16. A note
    ending in an open task with no final newline, then appended to by PowerShell 5.1's `>>`
    (UTF-16), gave the task "call the bank-\x00 \x00[\x00 ...", and that went into Open loops:
    this tool wrote a NUL into the review, and from the next run on refused to refresh the review
    because it holds one, with advice (re-save it as UTF-8) that does not apply to a file that
    already is UTF-8 (review, 2026-10-04). A project's status line took the same way in.

    So the note is decoded in the encoding its BOM names, UTF-8 without one (a UTF-16 note
    contributed no task at all, silently), and a line ends at a NUL or other control character:
    UTF-16 that follows UTF-8 text on a line is NULs for ASCII and bytes like 0x04 for Cyrillic.
    What is quoted is cut there. It can keep one stray character, the first byte of the first
    UTF-16 character ("call the bank-"), since nothing says where the UTF-8 ended. errors="ignore"
    as before: a byte that does not decode is dropped."""
    raw = p.read_bytes()
    enc = next((e for bom, e in READ_BOMS if raw.startswith(bom)), "utf-8")
    return LINE_END.sub("\n", raw.decode(enc, errors="ignore"))

def open_tasks_in(folder: Path):
    out = []
    for p in folder.glob("*.md"):
        if p.name.startswith("_"):
            continue
        for t in TASK.findall(note_text(p)):
            out.append((p.stem, t))
    return out

def open_tasks_today():
    """Open tasks from the LATEST daily note only.

    briefing.py rolls unfinished tasks forward by copying them into each new daily note, so
    the newest note already holds the complete live set and every older note is a frozen
    snapshot of that same set. Summing all of Daily/ therefore counted one task once per day
    it survived — 234 boxes that were really 14 tasks, the worst offenders appearing 45 times.
    """
    dailies = sorted(p for p in DAILY.glob("*.md") if re.match(r"\d{4}-\d{2}-\d{2}", p.stem))
    if not dailies:
        return []
    p = dailies[-1]
    return [(p.stem, t) for t in TASK.findall(note_text(p))]

def dedupe_tasks(tasks):
    seen, out = set(), []
    for src, t in tasks:
        k = t.strip()
        if k not in seen:
            seen.add(k)
            out.append((src, t))
    return out

def main():
    iso = date.today().isocalendar()
    tag = f"{iso[0]}-W{iso[1]:02d}"
    out = VAULT / "Reviews" / "Weekly" / f"{tag}.md"

    new_notes = sorted(p.stem for p in recent(NOTES))
    # A conversation counts in the week it was last active, not only the week it started.
    new_convs = sorted((p.stem for p in recent(CONVS, recurse=True, keys=("ended", "date"), first=False)),
                       reverse=True)

    projects = []
    for p in PROJECTS.glob("*.md"):
        if p.name.startswith("_"):
            continue
        m = re.search(r"^status:\s*(.+)$", note_text(p), re.M)
        projects.append((p.stem, m.group(1).strip() if m else "?"))
    projects.sort()

    tasks = dedupe_tasks(open_tasks_today() + open_tasks_in(PROJECTS))

    # The file invites writing (Reflection) and ticking (Open loops), and this runs every day. It
    # used to rebuild the whole file, so each night's sync erased what was written that week.
    # Only the marked block is regenerated now, and a loop ticked in it stays ticked.
    raw = out.read_bytes() if out.exists() else None
    if raw is not None and (raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) or b"\x00" in raw):
        # UTF-16, whole (PowerShell 5.1's `>`) or appended to the Reflection (its `>>`):
        # surrogateescape keeps its bytes, but the text-mode round trip below reads its
        # "\r\x00\n\x00" as two line ends and writes two back ("\n\x00\n\x00" on macOS), so the
        # refresh rewrote the user's lines with exit 0 and nothing said (review, 2026-10-02). The
        # same guard as briefing.py's for today's note: the file stays byte-identical and the run
        # fails where the sync shows it.
        print(f"{out.relative_to(VAULT)}: not UTF-8 text (UTF-16, as PowerShell 5.1's `>` and `>>` "
              "write?) — left untouched; re-save it as UTF-8 to refresh it.", file=sys.stderr)
        return 1
    # surrogateescape here and on the write below: the Reflection is the user's, a line of it saved
    # in another code page made this read raise, and "replace" would write U+FFFD over it (review,
    # 2026-10-02). The escaped bytes go back out exactly as they came in.
    old = out.read_text(encoding="utf-8", errors="surrogateescape") if raw is not None else None
    pair = PAIR.search(old) if old is not None else None
    if old is not None and not pair:
        if START in old or END in old:
            print(f"{out.relative_to(VAULT)}: unbalanced weekly markers — left untouched; restore "
                  f"the {START} / {END} pair to refresh it.", file=sys.stderr)
            return 1
        # Written before the markers existed: everything above Reflection was generated.
        r = re.search(r"(?m)^## ✍️ Reflection", old)
        if not r:
            print(f"{out.relative_to(VAULT)}: no generated block and no Reflection heading — left "
                  f"untouched.", file=sys.stderr)
            return 1
    generated = pair.group(0) if pair else (old[:r.start()] if old is not None else "")
    ticked = {m.group(1).strip() for m in re.finditer(r"^- \[[xX]\] (.+?)  <sub>", generated, re.M)}

    front = f"---\ntype: review\nweek: {tag}\ntags:\n  - review\n---\n"
    L = [START,
         f"# 🗓️ Weekly Review — {tag}",
         # The date only: this file is committed and both machines render it, so a minute here
         # made every two-machine sync conflict on this one line.
         f"*Generated {date.today():%Y-%m-%d}. Health snapshot: [[Vault Health]].*\n"]

    L.append(f"## 🌱 New notes this week ({len(new_notes)})")
    L += [f"- [[{n}]]" for n in new_notes] or ["- *(none)*"]

    L.append(f"\n## 🤖 Conversations this week ({len(new_convs)})")
    L += [f"- [[{c}]]" for c in new_convs] or ["- *(none)*"]

    L.append("\n## 📊 Projects")
    L += [f"- {'🟢' if s=='active' else '⚪'} [[{n}]] — `{s}`" for n, s in projects] or ["- *(none)*"]

    L.append(f"\n## 🔓 Open loops ({len(tasks)})")
    L += [f"- [{'x' if t.strip() in ticked else ' '}] {t}  <sub>([[{src}]])</sub>" for src, t in tasks] \
        or ["- *(none)*"]
    L.append(END)
    block = "\n".join(L)

    if pair:
        text = old[:pair.start()] + block + old[pair.end():]
    elif old is not None:
        text = front + block + "\n\n" + old[r.start():]
    else:
        text = front + block + "\n\n" + REFLECTION
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", errors="surrogateescape")
    print(f"Wrote {out.relative_to(VAULT)} — {len(new_notes)} notes, {len(new_convs)} convs, {len(tasks)} open tasks.")

if __name__ == "__main__":
    sys.exit(main())
