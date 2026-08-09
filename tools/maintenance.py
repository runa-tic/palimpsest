#!/usr/bin/env python3
"""Vault health pass: surfaces things that need attention into Reviews/Vault Health.md.

Checks:
  - Inbox items waiting to be processed
  - Atomic notes with an unfilled Related placeholder
  - Atomic notes with no tags
  - Orphan notes (no inbound links from anywhere)
  - Broken wikilinks (point at a file that doesn't exist)
  - Stale active projects (not touched in 30+ days)

Usage (from vault root):  python _tools/maintenance.py
"""
from __future__ import annotations
import sys, os, re, time
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
OUT = VAULT / "Reviews" / "Vault Health.md"
LINK = re.compile(r"\[\[([^\]|#]+)")

# Non-knowledge paths. `.claude` matters most: .claude/worktrees/ holds git worktrees, each a
# FULL copy of the vault (agentcost-beta alone was 1,631 .md). Scanning them made every health
# number measure two vaults at once — a shadow twin's copy of a note counts as an inbound link,
# so real orphans vanished, and notes deleted in main but alive in a worktree resolved links
# that should have read as broken.
SKIP_DIRS = {"_tools", ".obsidian", ".claude", ".git", "node_modules", "_scratch"}

def all_md() -> list[Path]:
    return [p for p in VAULT.rglob("*.md") if not SKIP_DIRS.intersection(p.parts)]

def stem_of(target: str) -> str:
    return target.strip().split("/")[-1]

def main():
    files = all_md()
    by_stem = {p.stem: p for p in files}
    inbound = {p.stem: 0 for p in files}
    broken = []

    # 0-byte .md files: usually Obsidian stubs auto-created from unresolved [[links]] (the
    # doubled extension, e.g. foo.py.md, is the tell). Pure junk; surface them for cleanup.
    empty = sorted(
        str(p.relative_to(VAULT))
        for p in files
        if "node_modules" not in p.parts and p.name != ".gitkeep.md" and p.stat().st_size == 0
    )

    # Conversation transcripts, templates, and the guide notes contain example/placeholder
    # [[links]] that aren't real — don't treat those as broken links. "Vault Health" is THIS
    # script's own output: it lists every orphan/need-links note as a - [[wikilink]], so scanning
    # it would count those notes as "linked-to" and un-orphan them on the next run — a self-
    # referential feedback loop that made the orphan count oscillate run to run. Exclude it.
    # Reviews/ as a whole is generated output, not knowledge: dashboards quote note titles,
    # illustrate link syntax, and carry TRUNCATED titles from reports. The 2026-07-31 merge plan
    # alone contributed 7 phantom "broken links" (`[[wikilinks]]`, `[[title]]`, ellipsised names)
    # that no reader could ever fix, because the targets were never meant to be notes.
    skip_src = {"Home", "START HERE", "CLAUDE", "AGENTS", "Vault Health"}
    def scannable(p):
        return ("Templates" not in p.parts and "Claude Conversations" not in p.parts
                and "Reviews" not in p.parts and p.stem not in skip_src)

    # The vault is only half the brain: durable facts also live in Claude's memory directory,
    # and extracted notes legitimately cite them by title ("Deterministic safety backstops" is
    # referenced 8 times). Those targets are real knowledge, just stored outside the vault, so
    # counting them as BROKEN cries wolf — 26 of 37 reported breaks on 2026-07-31 were these.
    # They are resolvable-but-external: not broken, and not inbound links either.
    # Claude Code slugifies the project path by replacing every non-alphanumeric character
    # with "-", so this directory is derivable rather than hardcoded to one machine.
    # Override with PALIMPSEST_MEMORY_DIR if your layout differs.
    _env = os.environ.get("PALIMPSEST_MEMORY_DIR")
    MEMORY = Path(_env) if _env else (Path.home() / ".claude" / "projects"
                                      / re.sub(r"[^A-Za-z0-9]", "-", str(VAULT)) / "memory")
    external = set()
    if MEMORY.exists():
        for m in MEMORY.glob("*.md"):
            external.add(m.stem)
            mt = m.read_text(encoding="utf-8", errors="ignore")
            h = re.search(r"(?m)^#\s+(.+?)\s*$", mt)
            if h:
                external.add(h.group(1).strip())
        idx = MEMORY / "MEMORY.md"
        if idx.exists():
            external |= set(re.findall(r"^- \[(.+?)\]\(",
                                       idx.read_text(encoding="utf-8", errors="ignore"), re.M))

    # Resolve [[wikilinks]] the way Obsidian does — against note ALIASES too, not just filenames.
    # A link to any of a note's aliases counts as a link to that note (and is not "broken"). A real
    # filename always wins over an alias of the same text. Lets one canonical note (e.g. a
    # person whose aliases cover handle, @handle and full name) resolve every reference form.
    FM = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
    def parse_aliases(fmt: str) -> list[str]:
        out = []
        blk = re.search(r"^aliases:\s*\n((?:[ \t]*-[ \t]*.+\n?)+)", fmt, re.M)
        if blk:
            out += re.findall(r"^[ \t]*-[ \t]*(.+?)[ \t]*$", blk.group(1), re.M)
        inl = re.search(r"^aliases:\s*\[(.*?)\]", fmt, re.M)
        if inl:
            out += [a for a in inl.group(1).split(",")]
        def unq(a: str) -> str:
            # Peel ONE matching outer quote pair only — a blanket .strip(quotes) over-strips an
            # alias that legitimately ends in a quote (e.g. 'Say "traders" not "market makers"').
            a = a.strip()
            return a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a
        return [unq(a) for a in out if a.strip()]
    alias_to_stem = {}
    for p in files:
        m = FM.match(p.read_text(encoding="utf-8", errors="ignore"))
        if not m:
            continue
        for a in parse_aliases(m.group(1)):
            if a not in by_stem:
                alias_to_stem.setdefault(a, p.stem)

    for p in files:
        # Only REAL notes count as link sources — for BOTH inbound/orphan detection and broken
        # links. Conversation transcripts and templates are generated records full of incidental
        # [[mentions]]; counting them as inbound links masks true orphans, AND makes the result
        # non-deterministic: the Stop hook rewrites a transcript every turn, so a note mentioned in
        # passing would flip in and out of the orphan set run to run.
        if not scannable(p):
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for tgt in LINK.findall(txt):
            s = stem_of(tgt)
            canon = s if s in inbound else alias_to_stem.get(s)
            if canon:
                if canon != p.stem:
                    inbound[canon] += 1
            elif s in external:
                pass          # resolvable, but it lives in the memory dir, not the vault
            elif s.strip():
                broken.append((p.stem, s))

    def in_dir(p, d):
        return d in [part for part in p.parts]

    atomic = [p for p in files if in_dir(p, "10 Notes") and not p.name.startswith("_")]
    no_related, untagged, orphans = [], [], []
    for p in atomic:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        rel = re.search(r"## Related\n(.*?)(?=\n## Source|\Z)", txt, re.DOTALL)
        if rel and "[[ ]]" in rel.group(1):
            no_related.append(p.stem)
        fm = re.match(r"---\n(.*?)\n---", txt, re.DOTALL)
        fm_text = fm.group(1) if fm else ""
        # Tags come in two YAML forms: a block list (`tags:\n  - x`) or an inline
        # array (`tags: [x, y]`). Read both so inline-tagged notes aren't false-flagged.
        tags = re.findall(r"^\s+- (.+)$", fm_text, re.M)
        inline = re.search(r"^tags:\s*\[(.*?)\]", fm_text, re.M)
        if inline:
            tags += [t.strip().strip("\"'") for t in inline.group(1).split(",") if t.strip()]
        if not [t for t in tags if t != "claude/extracted"]:
            untagged.append(p.stem)
        if inbound.get(p.stem, 0) == 0:
            orphans.append(p.stem)

    inbox = [p.stem for p in files if in_dir(p, "00 Inbox") and not p.name.startswith("_")]

    stale = []
    cutoff = time.time() - 30 * 86400
    for p in files:
        if in_dir(p, "20 Projects") and not p.name.startswith("_"):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^status:\s*active", txt, re.M) and p.stat().st_mtime < cutoff:
                stale.append(p.stem)

    def section(title, items, hint):
        if not items:
            return f"## {title}\n✅ none\n"
        body = "\n".join(f"- [[{i}]]" for i in items)
        return f"## {title} ({len(items)})\n> {hint}\n{body}\n"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        "---\ntype: dashboard\ntags:\n  - maintenance\n---\n",
        f"# 🩺 Vault Health\n*Generated {now} by `_tools/maintenance.py`.*\n",
        section("📥 Inbox to process", inbox, "Sort these into Projects / Areas / Resources or distill into notes."),
        section("🔗 Notes needing links", no_related, "Run `python _tools/link_notes.py` or link by hand."),
        section("🏷️ Untagged notes", untagged, "Add 1–3 topic tags so they surface in searches and MOCs."),
        section("🪶 Orphan notes", orphans, "Nothing links here — connect them from a related note or MOC."),
        section("🐌 Stale active projects", stale, "Untouched 30+ days — finish, pause, or archive."),
    ]
    if empty:
        el = "\n".join(f"- `{e}`" for e in empty[:50])
        parts.append(
            f"## 🗑️ Empty notes ({len(empty)})\n"
            "> 0-byte .md files, usually Obsidian stubs from unresolved [[links]]. Safe to delete.\n"
            f"{el}\n"
        )
    else:
        parts.append("## 🗑️ Empty notes\n✅ none\n")
    if broken:
        bl = "\n".join(f"- `{src}` → `{tgt}`" for src, tgt in broken[:50])
        parts.append(f"## ❌ Broken links ({len(broken)})\n> These wikilinks point at missing notes.\n{bl}\n")
    else:
        parts.append("## ❌ Broken links\n✅ none\n")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(VAULT)}")
    print(f"  inbox={len(inbox)} need-links={len(no_related)} untagged={len(untagged)} "
          f"orphans={len(orphans)} stale={len(stale)} empty={len(empty)} broken={len(broken)}")

if __name__ == "__main__":
    main()
