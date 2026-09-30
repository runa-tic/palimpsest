#!/usr/bin/env python3
"""Vault health pass: surfaces things that need attention into Reviews/Vault Health.md.

Checks:
  - Inbox items waiting to be processed
  - Atomic notes with an unfilled Related placeholder
  - Atomic notes with no tags
  - Orphan notes (no inbound links from anywhere)
  - Broken wikilinks (point at a file that doesn't exist)
  - Stale active projects (not touched in 30+ days)

Usage (from vault root):  python tools/maintenance.py
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

# A [[link]] inside a code span or fence is SYNTAX BEING QUOTED, not a link — Obsidian renders it
# literally and clicking it does nothing. Counting it produced phantom breaks in exactly the notes
# that document linking (Skills/Route archive-wide questions… cites `[[Note Title]]` as the shape
# sub-agents emit). The old fix was a filename skip-list (CLAUDE, AGENTS); that only covered the
# files already known to offend.
#
# Do NOT strip the spans from the text: conversation notes are titled from the opening user
# message, so backticks land INSIDE real filenames (`2026-08-08 ` clear` (6b4fbbfd)`) and blanking
# them mangles the link target — that turned 7 reported breaks into 56. Containment direction is
# what separates the two cases: a code span wrapping a whole link is quoted syntax (skip it), a
# backtick sitting inside a link is part of the title (keep it).
FENCE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$")
CODESPAN = re.compile(r"(`+)[^\n]*?\1")

def code_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in FENCE.finditer(text)] + [m.span() for m in CODESPAN.finditer(text)]

def quoted(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(a <= span[0] and span[1] <= b for a, b in spans)

# Non-knowledge paths. `.claude` matters most: .claude/worktrees/ holds git worktrees, each a
# FULL copy of the vault (agentcost-beta alone was 1,631 .md). Scanning them made every health
# number measure two vaults at once — a shadow twin's copy of a note counts as an inbound link,
# so real orphans vanished, and notes deleted in main but alive in a worktree resolved links
# that should have read as broken.
SKIP_DIRS = {"tools", "_tools", ".obsidian", ".claude", ".git", "node_modules", "_scratch"}

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
    # Freshness audit of that same directory. This is where perishable claims actually live:
    # extraction already filters temporary state out of 10 Notes/, but memory is mostly
    # "DEPLOYED / UNCOMMITTED / live / pending" — and it is loaded into context every single
    # session with nothing marking its age. In the vault this came from, entries asserting "UNCOMMITTED" were
    # wrong by the time they were relied on. Report only; memory is never auto-edited.
    MEM_STALE_DAYS = 30
    STATEFUL = re.compile(r"\b(deployed|undeployed|uncommitted|committed|live|running|stopped|"
                          r"pending|armed|disabled|enabled|in progress|not yet|awaiting|"
                          r"unverified|restart)\b", re.I)
    mem_stale, mem_counts = [], {"project": 0, "feedback": 0, "other": 0}
    external = set()
    if MEMORY.exists():
        for m in MEMORY.glob("*.md"):
            external.add(m.stem)
            mt = m.read_text(encoding="utf-8", errors="ignore")
            h = re.search(r"(?m)^#\s+(.+?)\s*$", mt)
            if h:
                external.add(h.group(1).strip())
            if m.name == "MEMORY.md":
                continue
            head = mt[:900]
            mtype = (re.search(r"^\s*type:\s*(\w+)", head, re.M) or [None, "other"])[1]
            mem_counts[mtype if mtype in mem_counts else "other"] += 1
            # Durable by kind: operator rules and preferences do not expire. Project entries
            # are deployment state, and a stateful description is the tell either way.
            desc = (re.search(r'^description:\s*"?(.*?)"?\s*$', head, re.M) or [None, ""])[1]
            # An entry explicitly retired to the past tense is not a live claim, no matter how
            # old its filename. Without this, "retire to historical" never clears the flag —
            # the audit keys on type + filename date, both unchanged by a content rewrite — so
            # the one disposition that preserves the record would look like it did nothing.
            # Only unambiguous past-tense markers. "DONE + DEPLOYED" is NOT one — it asserts a
            # feature is currently live, which is exactly the kind of aging claim to keep flagging.
            if re.match(r"\s*(HISTORICAL|RETIRED|SUPERSEDED|CLOSED)\b", desc, re.I):
                continue
            if mtype not in ("project", "dated") and not STATEFUL.search(desc):
                continue
            # Age of the CLAIM, not of the file. metadata.modified and mtime both bottom out
            # at 2026-07-20 here, because the box path migration rewrote every memory file
            # that day — so they record when the directory moved, not when the fact was
            # established. The date in the filename is the one the author actually chose.
            # A `verified: YYYY-MM-DD` stamp resets the clock rather than exempting forever.
            # "Still live" is itself a perishable claim — a feature confirmed running today can
            # be ripped out next month — so a verified entry goes quiet for the same window and
            # then asks again. Permanent exemption would just be a slower way of lying.
            ver = re.search(r"^\s*verified:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
            named = re.search(r"(\d{4})-(\d{2})-(\d{2})", m.stem)
            ts = re.search(r"^\s*modified:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
            age = None
            for src in (ver.group(1) if ver else None,
                        named.group(0) if named else None, ts.group(1) if ts else None):
                if not src:
                    continue
                try:
                    age = (datetime.now() - datetime.strptime(src, "%Y-%m-%d")).days
                    break
                except Exception:
                    pass
            if age is None:
                age = int((time.time() - m.stat().st_mtime) / 86400)
            if age >= MEM_STALE_DAYS:
                mem_stale.append((age, m.stem, desc[:110]))
        mem_stale.sort(key=lambda x: -x[0])
        idx = MEMORY / "MEMORY.md"
        if idx.exists():
            external |= set(re.findall(r"^- \[(.+?)\]\(",
                                       idx.read_text(encoding="utf-8", errors="ignore"), re.M))

    # Resolve [[wikilinks]] the way Obsidian does — against note ALIASES too, not just filenames.
    # A link to any of a note's aliases counts as a link to that note (and is not "broken"). A real
    # filename always wins over an alias of the same text. Lets one canonical note (e.g. a person, with
    # aliases for handle, @handle and full name) resolve every reference form.
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
        m = FM.match(p.read_text(encoding="utf-8", errors="ignore").lstrip("\ufeff"))
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
        spans = code_spans(txt)
        for m in LINK.finditer(txt):
            if quoted(m.span(), spans):
                continue
            s = stem_of(m.group(1))
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
        # The Related list only, as link_notes reads it: a "[[ ]]" in a later section is not a
        # placeholder link_notes would ever fill.
        rel = re.search(r"## Related\n((?:[ \t]*\n)*(?:[ \t]*- .*(?:\n|\Z))*)", txt)
        if rel and "[[ ]]" in rel.group(1):
            no_related.append(p.stem)
        fm = re.match(r"---\n(.*?)\n---", txt.lstrip("\ufeff"), re.DOTALL)
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

    # Aging claims. A note marked volatility: dated was true when written and says nothing
    # about whether it still is — which is how a stale deployment claim survived as a confident
    # fact past its own correction. Surfacing them by age is the whole mechanism: verify,
    # rewrite as timeless, or delete. Notes predating the field carry no verdict, so they are
    # counted but never accused.
    AGING_DAYS = 120
    dated_old, vol_counts = [], {"timeless": 0, "dated": 0, "live": 0, "unknown": 0, "absent": 0}
    age_cutoff = datetime.now().timestamp() - AGING_DAYS * 86400
    for p in files:
        if not in_dir(p, "10 Notes") or p.name.startswith("_"):
            continue
        head = p.read_text(encoding="utf-8", errors="ignore")[:600]
        m = re.search(r"^volatility:\s*(\w+)", head, re.M)
        if not m:
            vol_counts["absent"] += 1
            continue
        v = m.group(1).lower()
        vol_counts[v] = vol_counts.get(v, 0) + 1
        if v in ("dated", "live"):
            c = re.search(r"^created:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
            try:
                ts = datetime.strptime(c.group(1), "%Y-%m-%d").timestamp() if c else p.stat().st_mtime
            except Exception:
                ts = p.stat().st_mtime
            if ts < age_cutoff:
                dated_old.append(p.stem)

    def section(title, items, hint):
        if not items:
            return f"## {title}\n✅ none\n"
        body = "\n".join(f"- [[{i}]]" for i in items)
        return f"## {title} ({len(items)})\n> {hint}\n{body}\n"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        "---\ntype: dashboard\ntags:\n  - maintenance\n---\n",
        f"# 🩺 Vault Health\n*Generated {now} by `tools/maintenance.py`.*\n",
        section("📥 Inbox to process", inbox, "Sort these into Projects / Areas / Resources or distill into notes."),
        section("🔗 Notes needing links", no_related, "Run `python tools/link_notes.py` or link by hand."),
        section("🏷️ Untagged notes", untagged, "Add 1–3 topic tags so they surface in searches and MOCs."),
        section("🪶 Orphan notes", orphans, "Nothing links here — connect them from a related note or MOC."),
        section("🐌 Stale active projects", stale, "Untouched 30+ days — finish, pause, or archive."),
        section(f"⏳ Aging claims ({AGING_DAYS}d+)", dated_old,
                "Marked dated/live and old enough to have quietly stopped being true. "
                "Verify, rewrite as a timeless principle, or delete."),
        (f"## 🕰️ Shelf life\n> How the note base decays. `absent` predates the field "
         f"(2026-08-09) and is not a defect.\n"
         + "\n".join(f"- **{k}** — {v}" for k, v in vol_counts.items() if v) + "\n"),
        (f"## 🧠 Memory needing re-verification ({len(mem_stale)})\n"
         "> Memory is loaded into context every session and asserts deployment state — the one\n"
         "> place stale claims do real damage. These are state-carrying entries untouched for\n"
         f"> {MEM_STALE_DAYS}+ days: confirm, correct, or delete. Never trusted blind because they are here.\n"
         + ("\n".join(f"- `{a}d` **{n}** — {d}" for a, n, d in mem_stale[:20])
            if mem_stale else "✅ none")
         + (f"\n\n*({len(mem_stale) - 20} older still.)*" if len(mem_stale) > 20 else "")
         + f"\n\n*Memory holdings: {mem_counts['project']} project, {mem_counts['feedback']} "
           f"feedback, {mem_counts['other']} other.*\n"),
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
          f"orphans={len(orphans)} stale={len(stale)} empty={len(empty)} broken={len(broken)} "
          f"aging={len(dated_old)} memstale={len(mem_stale)}")

if __name__ == "__main__":
    main()
