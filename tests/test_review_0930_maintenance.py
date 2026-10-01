"""Regressions for the 2026-09-30 review of the maintenance tools (link_notes, dedupe,
maintenance, weekly_review, briefing). One check per finding; each fails on ba54bc9.

 1. weekly_review keeps the Reflection text and ticked loops across daily refreshes.
 2. briefing refresh keeps a rolled-over task the user ticked today.
 3. briefing with a deleted end marker never eats the text written after the old start marker.
 4. link_notes adds the note to a MOC whose heading is "## Notes & links" (not a silent no-op).
 5. --rebuild-mocs keeps an annotated "## Notes" entry whole, and later entries stay generated.
 6. maintenance ignores Obsidian's .trash folder.
 7. link_notes keeps every section after "## Related" when there is no "## Source".
 8. maintenance works in a vault whose own path contains a skipped folder name.
 9. dedupe does not group unrelated Cyrillic notes into one family.
10. one non-UTF-8 note does not crash link_notes or dedupe, and is left untouched.
11. "new this week" and "stale project" come from note dates / git history, not checkout mtime.
12. maintenance resolves attachment embeds and ".md"-suffixed links.
13. a UTF-8 BOM does not hide a note's frontmatter from link_notes and maintenance.
14. dedupe reads inline "tags: [a, b]" and does not count aliases as tags.
"""
import os, re, shutil, subprocess, sys, tempfile
from datetime import date, timedelta
from pathlib import Path
import _util
from _util import Checks, make_vault, run, write, git

NO_MEMORY = {"PALIMPSEST_MEMORY_DIR": "/nonexistent/palimpsest-test-memory"}
NOTE = ("---\ntype: note\ncreated: {created}\ntags:\n  - claude/extracted\n{tags}---\n\n# {title}\n\n"
        "{body}\n\n## Related\n- [[ ]]\n\n## Source\n- From conversation [[x]]\n")


def note(title, body, tags=(), created=None):
    return NOTE.format(created=created or date.today().isoformat(), title=title, body=body,
                       tags="".join(f"  - {t}\n" for t in tags))


def load(v, name):
    sys.path.insert(0, str(v / "tools"))
    sys.modules.pop(name, None)
    return __import__(name)


def health(v):
    r = run(v, "maintenance.py", env=NO_MEMORY)
    p = v / "Reviews" / "Vault Health.md"
    return r, (p.read_text(encoding="utf-8") if p.exists() else "")


def section(text, title):
    m = re.search(r"^## [^\n]*" + re.escape(title) + r"[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def main() -> int:
    c = Checks("review 2026-09-30: maintenance tools")
    made = []
    today = date.today()

    # 1. weekly review keeps the user's Reflection and ticks
    v = make_vault(); made.append(v)
    write(v, f"Daily/{today.isoformat()}.md", "- [ ] alpha task\n- [ ] beta task\n")
    run(v, "weekly_review.py")
    iso = today.isocalendar()
    wk = v / "Reviews" / "Weekly" / f"{iso[0]}-W{iso[1]:02d}.md"
    t = wk.read_text(encoding="utf-8")
    t = t.replace("- [ ] alpha task", "- [x] alpha task") + "\nMy reflection: ship the fix first.\n"
    wk.write_text(t, encoding="utf-8")
    run(v, "weekly_review.py")
    t = wk.read_text(encoding="utf-8")
    c.ok("My reflection: ship the fix first." in t and "- [x] alpha task" in t and "- [ ] beta task" in t,
         "weekly refresh keeps the Reflection text and a ticked loop", t[-600:])

    # 2. briefing refresh keeps a ticked rolled-over task
    v = make_vault(); made.append(v)
    write(v, f"Daily/{(today - timedelta(days=1)).isoformat()}.md", "- [ ] pay rent\n- [ ] call mom\n")
    run(v, "briefing.py")
    d = v / "Daily" / f"{today.isoformat()}.md"
    d.write_text(d.read_text(encoding="utf-8").replace("- [ ] pay rent", "- [x] pay rent"), encoding="utf-8")
    run(v, "briefing.py")
    t = d.read_text(encoding="utf-8")
    c.ok("- [x] pay rent" in t and "- [ ] pay rent" not in t and "- [ ] call mom" in t,
         "briefing refresh keeps a rolled-over task the user ticked", t[:700])

    # 3. deleted end marker: two refreshes later the user's log is still there
    v = make_vault(); made.append(v)
    run(v, "briefing.py")
    d = v / "Daily" / f"{today.isoformat()}.md"
    t = d.read_text(encoding="utf-8").replace("<!-- briefing:end -->\n", "")
    d.write_text(t + "\nMy log entry that must survive.\n", encoding="utf-8")
    r1, r2 = run(v, "briefing.py"), run(v, "briefing.py")
    t = d.read_text(encoding="utf-8")
    c.ok("My log entry that must survive." in t, "a missing end marker never deletes the user's text",
         t + r1.stderr + r2.stderr)

    # 4. MOC heading "## Notes & links"
    v = make_vault(); made.append(v)
    write(v, "60 Maps of Content/Ops.md", "---\nmoc_tags: [ops]\n---\n# Ops\n\n## Notes & links\n- [[Seed]]\n")
    write(v, "10 Notes/Seed.md", note("Seed", "seed body", ["ops"]).replace("- [[ ]]", "- [[Ops]]"))
    write(v, "10 Notes/Alpha restart rule.md", note("Alpha restart rule", "restart rule", ["ops"]))
    run(v, "link_notes.py")
    moc = (v / "60 Maps of Content/Ops.md").read_text(encoding="utf-8")
    c.ok("[[Alpha restart rule]]" in moc, "link_notes adds the note under a '## Notes & links' heading", moc)

    # 5. --rebuild-mocs with an annotated entry
    v = make_vault(); made.append(v)
    write(v, "60 Maps of Content/PKM.md",
          "---\nmoc_tags: [pkm]\n---\n# PKM\n\n## Notes\n- [[Alpha idea]] — the original framing\n- [[Beta idea]]\n")
    write(v, "60 Maps of Content/Writing.md", "---\nmoc_tags: [writing]\n---\n# Writing\n\n## Notes\n")
    write(v, "10 Notes/Alpha idea.md", note("Alpha idea", "a", ["pkm"]))
    write(v, "10 Notes/Beta idea.md", note("Beta idea", "b", ["writing"]))
    write(v, "10 Notes/Gamma idea.md", note("Gamma idea", "g", ["writing"]))
    run(v, "link_notes.py", "--rebuild-mocs")
    pkm = (v / "60 Maps of Content/PKM.md").read_text(encoding="utf-8")
    wr = (v / "60 Maps of Content/Writing.md").read_text(encoding="utf-8")
    c.ok("- [[Alpha idea]] — the original framing\n" in pkm and not re.search(r"(?m)^ — the original", pkm)
         and "[[Beta idea]]" not in pkm and "[[Beta idea]]" in wr,
         "--rebuild-mocs keeps an annotation with its note and still moves later entries", pkm + "\n----\n" + wr)

    # 6. .trash is not part of the vault
    v = make_vault(); made.append(v)
    write(v, "10 Notes/Live.md", "see [[Gone]]\n")
    write(v, "10 Notes/Lonely.md", "nobody links here\n")
    write(v, ".trash/Gone.md", "deleted\n")
    write(v, ".trash/Old.md", "[[Lonely]] [[Live]]\n")
    _, h = health(v)
    c.ok("`Live` → `Gone`" in h and "[[Lonely]]" in section(h, "Orphan notes"),
         ".trash neither resolves links nor supplies inbound ones", h[-900:])

    # 7. sections after Related survive
    v = make_vault(); made.append(v)
    write(v, "60 Maps of Content/Ops.md", "---\nmoc_tags: [ops]\n---\n# Ops\n\n## Notes\n")
    write(v, "10 Notes/Sibling.md", note("Sibling", "s", ["ops"]))
    write(v, "10 Notes/Hand written.md",
          "---\ntags: [ops]\n---\n# Hand written\n\n## Related\n- [[ ]]\n\n## Open questions\n"
          "Why does it wedge?\n\n## Examples\nThe 07-06 outage.\n")
    run(v, "link_notes.py")
    t = (v / "10 Notes/Hand written.md").read_text(encoding="utf-8")
    c.ok("Why does it wedge?" in t and "The 07-06 outage." in t and "[[Sibling]]" in t,
         "link_notes keeps the sections after '## Related'", t)

    # 8. vault path with a skipped folder name above it
    v = make_vault(); made.append(v)
    outer = Path(tempfile.mkdtemp(prefix="palimpsest-test-")); made.append(outer)
    nested = outer / "node_modules" / "vault"
    nested.parent.mkdir(parents=True)
    shutil.move(str(v), str(nested))
    write(nested, "10 Notes/Lonely.md", "nobody links here\n")
    r, h = health(nested)
    c.ok("[[Lonely]]" in section(h, "Orphan notes"), "maintenance scans a vault under a 'node_modules' path",
         r.stdout + h[-500:])

    # 9. Cyrillic notes
    v = make_vault(); made.append(v)
    write(v, "10 Notes/Перезапуск бота.md",
          note("Перезапуск бота", "Перезапускать бота только после проверки сессии и логов сервера.", ["ops"]))
    write(v, "10 Notes/Полив томатов.md",
          note("Полив томатов", "Томаты поливать утром под корень тёплой отстоянной водой.", ["garden"]))
    write(v, "10 Notes/Обрезка малины.md",
          note("Обрезка малины", "Малину обрезать осенью до земли, оставляя молодые побеги.", ["garden"]))
    write(v, "10 Notes/Перезапуск бота после проверки.md",
          note("Перезапуск бота после проверки",
               "Перезапускать бота только после проверки сессии и логов сервера, никогда вслепую.", ["ops"]))
    r = run(v, "dedupe.py", "--no-semantic")
    rep = (v / "Reviews" / "Duplicate Candidates.md").read_text(encoding="utf-8") if r.returncode == 0 else ""
    fams = re.findall(r"^## (\d+) notes", rep, re.M)
    c.ok(fams == ["2"] and "[[Перезапуск бота]]" in rep and "[[Перезапуск бота после проверки]]" in rep,
         "dedupe flags the reworded Cyrillic pair and does not chain unrelated notes into it",
         r.stdout + r.stderr + rep)

    # 10. a cp1251 note
    v = make_vault(); made.append(v)
    write(v, "60 Maps of Content/Ops.md", "---\nmoc_tags: [ops]\n---\n# Ops\n\n## Notes\n")
    write(v, "10 Notes/Good note.md", note("Good note", "fine", ["ops"]))
    bad = v / "10 Notes" / "Заметка cp1251.md"
    raw = note("Заметка", "Привет, мир", ["ops"]).encode("cp1251")
    bad.write_bytes(raw)
    r1 = run(v, "link_notes.py")
    r2 = run(v, "dedupe.py", "--no-semantic")
    good = (v / "10 Notes/Good note.md").read_text(encoding="utf-8")
    c.ok(r1.returncode == 0 and r2.returncode == 0 and "[[Ops]]" in good and bad.read_bytes() == raw
         and "Заметка cp1251" in r1.stderr,
         "a non-UTF-8 note is skipped with a warning, not a crash, and left byte-identical",
         r1.stdout + r1.stderr + r2.stderr)

    # 11. dates from content/history, not mtime
    v = make_vault(); made.append(v)
    old = (today - timedelta(days=60)).isoformat()
    write(v, "10 Notes/Old dated.md", note("Old dated", "x", ["a"], created=old))
    write(v, "10 Notes/Old undated.md", "no frontmatter, committed long ago\n")
    write(v, "20 Projects/Abandoned.md", "---\nstatus: active\n---\n# Abandoned\n")
    git(v, "add", "-A")
    stamp = f"{old}T12:00:00"
    subprocess.run(["git", "commit", "-q", "-m", "old"], cwd=v, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
    write(v, "10 Notes/Fresh.md", "written today, untracked\n")   # every mtime is "now", as after a clone
    run(v, "weekly_review.py")
    wk = (v / "Reviews" / "Weekly" / f"{iso[0]}-W{iso[1]:02d}.md").read_text(encoding="utf-8")
    new = section(wk, "New notes this week")
    _, h = health(v)
    c.ok("[[Fresh]]" in new and "[[Old dated]]" not in new and "[[Old undated]]" not in new
         and "[[Abandoned]]" in section(h, "Stale active projects"),
         "new-this-week and stale use note dates and git history, not checkout mtime",
         new + "\n----\n" + section(h, "Stale active projects"))

    # 12. attachments and .md-suffixed links
    v = make_vault(); made.append(v)
    (v / "assets").mkdir()
    (v / "assets" / "diagram.png").write_bytes(b"\x89PNG\r\n")
    (v / "papers").mkdir()
    (v / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    write(v, "10 Notes/Source.md", "![[diagram.png]] and [[paper.pdf]] and [[Target.md]]\n")
    write(v, "10 Notes/Target.md", "target\n")
    _, h = health(v)
    c.ok("Broken links\n✅ none" in h and "[[Target]]" not in section(h, "Orphan notes"),
         "attachments and [[Note.md]] resolve, and the .md form counts as inbound", h[-700:])

    # 13. BOM
    v = make_vault(); made.append(v)
    write(v, "60 Maps of Content/Ops.md", "---\nmoc_tags: [ops]\n---\n# Ops\n\n## Notes\n")
    write(v, "10 Notes/Bommed.md", "﻿" + note("Bommed", "b", ["ops"]))
    run(v, "link_notes.py")
    t = (v / "10 Notes/Bommed.md").read_text(encoding="utf-8")
    _, h = health(v)
    c.ok("[[Ops]]" in t and "[[Bommed]]" not in section(h, "Untagged notes"),
         "a BOM does not hide the frontmatter (MOC assigned, not flagged untagged)", t + section(h, "Untagged"))

    # 14. dedupe tag parsing
    v = make_vault(); made.append(v)
    write(v, "10 Notes/Inline.md", "---\ntags: [ops, infra]\n---\nbody\n")
    write(v, "10 Notes/Aliased.md", "---\ntags: []\naliases:\n  - Some Alias\n---\nbody\n")
    dd = load(v, "dedupe")
    dd.NOTES = v / "10 Notes"
    notes = dd.load()
    c.ok(notes["Inline"]["tags"] == {"ops", "infra"} and notes["Aliased"]["tags"] == set(),
         "dedupe reads inline tags and ignores aliases", str({k: n["tags"] for k, n in notes.items()}))

    for p in made:
        _util.rmtree(p, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
