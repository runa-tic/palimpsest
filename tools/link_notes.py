#!/usr/bin/env python3
"""Auto-link atomic notes that still have an empty Related placeholder.

For each note in "10 Notes/" whose Related section is the placeholder `- [[ ]]`, this:
  - suggests the 5 most related notes (shared tags + shared title words), and
  - assigns it to the existing MOC whose members it overlaps most (by tags),
then rewrites the note's Related section and adds it to that MOC's note list.

Deterministic, no model calls — safe to run on every sync.

Usage (from vault root):
  python tools/link_notes.py
  python tools/link_notes.py --all   # relink every note, not just placeholders
"""
from __future__ import annotations
import sys, re, argparse
from pathlib import Path
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
NOTES = VAULT / "10 Notes"
MOCS = VAULT / "60 Maps of Content"
STOP = set("a an the of to in on for and or is are be not with from as at by it its this that than into".split())

def words(s: str) -> set[str]:
    # Unicode word characters, not [a-z0-9]: the ASCII class dropped every Cyrillic title word,
    # so a Russian note's title contributed nothing to related-note ranking.
    return {w for w in re.findall(r"[^\W_]+", s.lower()) if w not in STOP and len(w) > 3}

def frontmatter_tags(text: str) -> list[str]:
    """Tags from frontmatter, in BOTH YAML forms.

    This used to read only the block list (`tags:\\n  - x`) and silently returned [] for the
    inline form (`tags: [x, y]`). Since every MOC assignment is driven by tags, those notes were
    unassignable — 118 of them, all reported as untagged by this parser while maintenance.py,
    which handles both forms, correctly reported zero untagged notes in the vault.
    """
    m = re.match(r"---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return []
    fmt = m.group(1)
    tags = re.findall(r"^\s+- (.+?)\s*$", fmt, re.M)
    inline = re.search(r"^tags:\s*\[(.*?)\]", fmt, re.M)
    if inline:
        tags += [t.strip().strip("\"'") for t in inline.group(1).split(",") if t.strip()]
    return [t for t in tags if t and t != "claude/extracted"]

def load_notes() -> dict[str, dict]:
    out = {}
    for f in NOTES.glob("*.md"):
        if f.name.startswith("_"):
            continue
        txt = f.read_text(encoding="utf-8")
        out[f.stem] = {"path": f, "text": txt,
                       "tags": set(frontmatter_tags(txt)), "words": words(f.stem)}
    return out

def load_mocs() -> dict[str, list[str]]:
    mocs = {}
    for f in MOCS.glob("*.md"):
        if f.name.startswith("_"):
            continue
        members = re.findall(r"- \[\[([^\]]+)\]\]", f.read_text(encoding="utf-8"))
        mocs[f.stem] = members
    return mocs


def declared_tags() -> dict[str, set[str]]:
    """Optional `moc_tags:` in a MOC's frontmatter — the topics it is FOR.

    Without this a brand-new MOC has no members, therefore no tag profile, therefore never wins
    an assignment and stays empty forever. Declaring its topics gives it a profile on day one.
    Accepts either `moc_tags: [a, b]` or a YAML block list.
    """
    out = {}
    for f in MOCS.glob("*.md"):
        if f.name.startswith("_"):
            continue
        m = re.match(r"---\n(.*?)\n---", f.read_text(encoding="utf-8"), re.DOTALL)
        tags = []
        if m:
            fmt = m.group(1)
            inl = re.search(r"^moc_tags:\s*\[(.*?)\]", fmt, re.M)
            if inl:
                tags += [x.strip().strip("\"'") for x in inl.group(1).split(",") if x.strip()]
            blk = re.search(r"^moc_tags:\s*\n((?:[ \t]+-[ \t]*.+\n?)+)", fmt, re.M)
            if blk:
                tags += re.findall(r"^[ \t]+-[ \t]*(.+?)[ \t]*$", blk.group(1), re.M)
        out[f.stem] = {t for t in tags if t}
    return out


DECLARED_WEIGHT = 4.0

def moc_score(note_tags: set, profile: Counter, n_members: int, declared: set) -> float:
    """How characteristic are this note's tags OF this MOC?

    The original scorer used a raw sum of tag counts across a MOC's members, which is a
    rich-get-richer loop: a bigger MOC has bigger counts, so it wins more assignments, so it
    gets bigger. Measured 2026-07-31 — it filed 42% of a random sample into the single largest
    MOC (370 members), while dedicated LLM / Knowledge-Management maps starved at 19 and 23 and
    won nothing. Dividing by member count makes the score a PROPORTION, and drops that to 22%.
    """
    if not note_tags:
        return 0.0
    s = sum(profile[t] for t in note_tags) / max(1, n_members)
    if declared:
        s += DECLARED_WEIGHT * len(note_tags & declared) / len(note_tags)
    return s

# The Related LIST only: the heading plus the list lines directly under it. This used to run to
# the next "## Source" or to the end of the file, so a hand-written note with sections after
# Related and no Source (common) lost all of them to the regenerated links.
REL_PAT = re.compile(r"## Related\n(?:[ \t]*\n)*(?:[ \t]*- .*(?:\n|\Z))*")

def needs_linking(text: str) -> bool:
    m = REL_PAT.search(text)
    return bool(m) and "[[ ]]" in m.group(0)

# A MOC's generated "## Notes" list: every "- [[x]]..." line under the heading, annotated or not.
NOTES_LIST = re.compile(r"\n## Notes[ \t]*\n((?:- \[\[[^\]]+\]\][^\n]*(?:\n|\Z))*)")

INDEX = MOCS / "_MOC Index.md"
IDX_START, IDX_END = "<!-- moc-index:start -->", "<!-- moc-index:end -->"


def refresh_moc_index(moc_names) -> None:
    """Keep the map-of-maps current.

    Every MOC created from templates/MOC.md ends with '*Part of [[_MOC Index]].*', so that
    note must exist or each new map ships a broken link and Vault Health never goes green.
    Regenerating the list here means the hub cannot drift out of date the way a hand-kept
    index does — and an index nobody trusts is one nobody opens.
    """
    if not INDEX.exists():
        return
    body = ("\n".join(f"- [[{n}]]" for n in sorted(moc_names))
            or "*(none yet — create your first map from `templates/MOC.md`)*")
    txt = INDEX.read_text(encoding="utf-8")
    if IDX_START not in txt or IDX_END not in txt:
        return
    new = re.sub(re.escape(IDX_START) + r".*?" + re.escape(IDX_END),
                 f"{IDX_START}\n{body}\n{IDX_END}", txt, flags=re.DOTALL)
    if new != txt:
        INDEX.write_text(new, encoding="utf-8")
        print(f"  _MOC Index: {len(list(moc_names))} map(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="relink every note, not just placeholders")
    ap.add_argument("--rebuild-mocs", action="store_true",
                    help="reassign EVERY note to its best MOC and rewrite each MOC's note list "
                         "from scratch. Use after adding a MOC or changing moc_tags — normal runs "
                         "only ever append, so existing over-stuffed maps never shrink on their own.")
    args = ap.parse_args()

    notes = load_notes()
    mocs = load_mocs()
    declared = declared_tags()
    # tag profile per MOC = how often each tag appears among its current members
    moc_tags = {}
    for moc, members in mocs.items():
        c = Counter()
        for m in members:
            if m in notes:
                c.update(notes[m]["tags"])
        moc_tags[moc] = c
    sizes = {moc: max(1, len([m for m in members if m in notes]))
             for moc, members in mocs.items()}

    def best_moc_for(tags: set) -> str | None:
        best, bm = 0.0, None
        for moc in mocs:
            sc = moc_score(tags, moc_tags[moc], sizes[moc], declared.get(moc, set()))
            if sc > best:
                best, bm = sc, moc
        return bm

    if args.rebuild_mocs:
        assigned = {moc: [] for moc in mocs}
        homeless = 0
        for stem, n in sorted(notes.items()):
            m = best_moc_for(n["tags"])
            if m:
                assigned[m].append(stem)
            else:
                homeless += 1
        for moc, members in assigned.items():
            f = MOCS / f"{moc}.md"
            txt = f.read_text(encoding="utf-8")
            # Some maps are hand-curated into themed sections above the generated list. That is
            # human work — never duplicate it into the flat block below. Anything already linked
            # OUTSIDE the "## Notes" section is left exactly where the author put it and dropped
            # from the generated part.
            #
            # An annotated entry in the list itself ("- [[A]] — why it matters") is the same kind
            # of work. The list pattern used to match bare "- [[A]]" only, so it stopped at the
            # annotation: the text was split off onto a line of its own and every entry after it
            # was frozen as "curated". Whole lines now; annotated ones are kept verbatim.
            sec = NOTES_LIST.search(txt)
            kept = [l for l in (sec.group(1).splitlines() if sec else [])
                    if not re.fullmatch(r"- \[\[[^\]|#]+\]\]\s*", l)]
            without = (txt[:sec.start()] + "\n" + txt[sec.end():]) if sec else txt
            curated = set(re.findall(r"- \[\[([^\]|#]+)", "\n".join([without, *kept])))
            fresh = [m for m in sorted(members) if m not in curated]
            block = "## Notes\n" + "".join(l + "\n" for l in kept + [f"- [[{m}]]" for m in fresh])
            if sec:
                txt = txt[:sec.start()] + "\n" + block + txt[sec.end():]
            else:
                # keep the generated list ABOVE a trailing "*Part of [[_MOC Index]].*" footer
                foot = re.search(r"\n---\n\*Part of .*?\*\s*$", txt, re.DOTALL)
                if foot:
                    txt = txt[:foot.start()] + "\n" + block + txt[foot.start():]
                else:
                    txt = txt.rstrip() + "\n\n" + block
            f.write_text(txt, encoding="utf-8")
            note = f" (+{len(curated)} kept in curated sections)" if curated else ""
            print(f"  {moc}: {len(mocs[moc])} -> {len(fresh)}{note}")
        refresh_moc_index([m for m in mocs if m != "_MOC Index"])
        print(f"Rebuilt {len(assigned)} MOC(s). {homeless} note(s) matched none.")
        return

    linked = 0
    for stem, n in notes.items():
        if not (args.all or needs_linking(n["text"])):
            continue
        # related notes
        ranked = []
        for other, o in notes.items():
            if other == stem:
                continue
            s = 3 * len(n["tags"] & o["tags"]) + len(n["words"] & o["words"])
            if s > 0:
                ranked.append((s, other))
        ranked.sort(key=lambda x: -x[0])
        related = [r for _, r in ranked[:5]]
        best_moc = best_moc_for(n["tags"])

        links = []
        if best_moc:
            links.append(f"- 🗺️ [[{best_moc}]]")
        links += [f"- [[{r}]]" for r in related]
        if not links:
            continue
        new_text = REL_PAT.sub(lambda _m: "## Related\n" + "\n".join(links) + "\n", n["text"], count=1)
        n["path"].write_text(new_text, encoding="utf-8")

        # add to MOC note list if not present
        if best_moc and stem not in mocs.get(best_moc, []):
            mf = MOCS / f"{best_moc}.md"
            mt = mf.read_text(encoding="utf-8")
            # The exact heading line. A substring test passed on "## Notes & links" while the
            # replace() below found nothing, so the MOC was rewritten unchanged, the note's
            # placeholder was already gone, and it was never retried.
            h = re.search(r"(?m)^## Notes[ \t]*$", mt)
            if h:
                mt = mt[:h.end()] + f"\n- [[{stem}]]" + mt[h.end():]
            else:
                mt = mt.rstrip("\n") + f"\n\n## Notes\n- [[{stem}]]\n"
            mf.write_text(mt, encoding="utf-8")
            mocs[best_moc].append(stem)
        print(f"  linked: {stem}  ->  {best_moc or '(no MOC match)'}")
        linked += 1

    refresh_moc_index([m for m in mocs if m != "_MOC Index"])
    print(f"Done. Linked {linked} note(s).")

if __name__ == "__main__":
    main()
