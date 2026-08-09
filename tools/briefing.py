#!/usr/bin/env python3
"""Generate (or enrich) today's daily note with a morning briefing.

Fills today's Daily/YYYY-MM-DD.md with:
  - unchecked tasks rolled over from the most recent previous daily note
  - active projects
  - a resurfaced "note of the day" (rotates daily)
  - a digest of conversations from the last 2 days

Safe to re-run: if today's note already has a briefing block it is refreshed in place,
and your own content below it is preserved.

Usage (from vault root):  python _tools/briefing.py
"""
from __future__ import annotations
import sys, re, random
from pathlib import Path
from datetime import date, datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
DAILY = VAULT / "Daily"
NOTES = VAULT / "10 Notes"
PROJECTS = VAULT / "20 Projects"
CONVS = VAULT / "40 Resources" / "Claude Conversations"
START, END = "<!-- briefing:start -->", "<!-- briefing:end -->"

def recent_daily(before: str) -> Path | None:
    cands = sorted(p for p in DAILY.glob("*.md")
                   if re.match(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem < before)
    return cands[-1] if cands else None

def open_tasks(p: Path | None) -> list[str]:
    if not p or not p.exists():
        return []
    return re.findall(r"^\s*- \[ \] (.+)$", p.read_text(encoding="utf-8"), re.M)

def active_projects() -> list[str]:
    out = []
    for p in PROJECTS.glob("*.md"):
        if p.name.startswith("_"):
            continue
        if re.search(r"^status:\s*active", p.read_text(encoding="utf-8", errors="ignore"), re.M):
            out.append(p.stem)
    return out

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
    return out

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
    lines.append(END)
    return "\n".join(lines)

def main():
    today = date.today().isoformat()
    DAILY.mkdir(parents=True, exist_ok=True)
    f = DAILY / f"{today}.md"
    block = build_block()
    if f.exists():
        txt = f.read_text(encoding="utf-8")
        if START in txt and END in txt:
            txt = re.sub(re.escape(START) + r".*?" + re.escape(END), block, txt, flags=re.DOTALL)
        else:
            txt = txt.rstrip() + "\n\n" + block + "\n"
        f.write_text(txt, encoding="utf-8")
        print(f"Refreshed briefing in Daily/{today}.md")
    else:
        header = f"---\ntype: daily\ndate: {today}\ntags:\n  - daily\n---\n\n# {date.today():%A, %B %d, %Y}\n\n"
        body = "\n\n## 📝 Log\n- \n"
        f.write_text(header + block + body, encoding="utf-8")
        print(f"Created Daily/{today}.md with briefing")

if __name__ == "__main__":
    main()
