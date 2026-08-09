#!/usr/bin/env python3
"""Generate this week's review note: what landed, what's open, where projects stand.

Writes Reviews/Weekly/YYYY-Www.md (per ISO week). Safe to run daily — it refreshes the
current week's file in place. Wired into sync.py.

Usage (from vault root):  python _tools/weekly_review.py
"""
from __future__ import annotations
import sys, re, time
from pathlib import Path
from datetime import date, datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
NOTES = VAULT / "10 Notes"
PROJECTS = VAULT / "20 Projects"
DAILY = VAULT / "Daily"
CONVS = VAULT / "40 Resources" / "Claude Conversations"

def recent(folder: Path, days=7, recurse=False):
    cutoff = time.time() - days * 86400
    it = folder.rglob("*.md") if recurse else folder.glob("*.md")
    return [p for p in it if not p.name.startswith("_") and p.stat().st_mtime >= cutoff]

TASK = re.compile(r"^\s*- \[ \] (.+)$", re.M)

def open_tasks_in(folder: Path):
    out = []
    for p in folder.glob("*.md"):
        if p.name.startswith("_"):
            continue
        for t in TASK.findall(p.read_text(encoding="utf-8", errors="ignore")):
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
    return [(p.stem, t) for t in TASK.findall(p.read_text(encoding="utf-8", errors="ignore"))]

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
    new_convs = sorted((p.stem for p in recent(CONVS, recurse=True)), reverse=True)

    projects = []
    for p in PROJECTS.glob("*.md"):
        if p.name.startswith("_"):
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"^status:\s*(.+)$", txt, re.M)
        projects.append((p.stem, m.group(1).strip() if m else "?"))
    projects.sort()

    tasks = dedupe_tasks(open_tasks_today() + open_tasks_in(PROJECTS))

    L = [f"---\ntype: review\nweek: {tag}\ntags:\n  - review\n---\n",
         f"# 🗓️ Weekly Review — {tag}",
         f"*Generated {datetime.now():%Y-%m-%d %H:%M}. Health snapshot: [[Vault Health]].*\n"]

    L.append(f"## 🌱 New notes this week ({len(new_notes)})")
    L += [f"- [[{n}]]" for n in new_notes] or ["- *(none)*"]

    L.append(f"\n## 🤖 Conversations this week ({len(new_convs)})")
    L += [f"- [[{c}]]" for c in new_convs] or ["- *(none)*"]

    L.append("\n## 📊 Projects")
    L += [f"- {'🟢' if s=='active' else '⚪'} [[{n}]] — `{s}`" for n, s in projects] or ["- *(none)*"]

    L.append(f"\n## 🔓 Open loops ({len(tasks)})")
    L += [f"- [ ] {t}  <sub>([[{src}]])</sub>" for src, t in tasks] or ["- *(none)*"]

    L.append("\n## ✍️ Reflection")
    L.append("- What did I learn? What's the one thing to push next week?\n")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"Wrote {out.relative_to(VAULT)} — {len(new_notes)} notes, {len(new_convs)} convs, {len(tasks)} open tasks.")

if __name__ == "__main__":
    main()
