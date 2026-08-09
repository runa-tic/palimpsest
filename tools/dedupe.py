#!/usr/bin/env python3
"""Flag near-duplicate atomic notes in 10 Notes/ so you can merge them.

Extraction re-runs over evolving conversations naturally produce notes that say the same
thing in different words. This finds likely pairs (by word + tag overlap) and writes them
to Reviews/Duplicate Candidates.md for you to review. Report-only — never deletes or merges.

Usage (from vault root):
  python _tools/dedupe.py                 # default threshold 0.50
  python _tools/dedupe.py --threshold 0.35   # wider net, mostly noise
"""
from __future__ import annotations
import sys, re, argparse
from pathlib import Path
from itertools import combinations

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
NOTES = VAULT / "10 Notes"
OUT = VAULT / "Reviews" / "Duplicate Candidates.md"
STOP = set("a an the of to in on for and or is are be not with from as at by it its this that than into "
           "you your our we my me but if then so can could should would will not no".split())

def words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 3}

def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0

def load():
    notes = {}
    for f in NOTES.glob("*.md"):
        if f.name.startswith("_"):
            continue
        txt = f.read_text(encoding="utf-8")
        fm = re.match(r"---\n(.*?)\n---", txt, re.DOTALL)
        tags = set(re.findall(r"^\s+- (.+)$", fm.group(1), re.M)) if fm else set()
        tags.discard("claude/extracted")
        body = txt.split("---", 2)[-1]
        notes[f.stem] = {
            "title_w": words(f.stem),
            "body_w": words(body),
            "tags": tags,
        }
    return notes

def main():
    ap = argparse.ArgumentParser(description="Find near-duplicate atomic notes.")
    # 0.35. It was raised to 0.50 on 2026-07-31 on the reasoning that the 35-49% band held
    # "different notes that merely share vocabulary" — that was WRONG, and reverted the same
    # day. A 15-agent semantic sweep found a 16-note family all saying "use the first trading
    # date as a TGE proxy"; its pairs score 42-46%, i.e. exactly the band 0.50 discards. The
    # 200-unreadable-rows problem that motivated the raise is real, but the fix is grouping
    # pairs into FAMILIES (below), not hiding them.
    ap.add_argument("--threshold", type=float, default=0.35, help="min combined score to flag (0-1)")
    args = ap.parse_args()

    notes = load()
    pairs = []
    for a, b in combinations(notes, 2):
        na, nb = notes[a], notes[b]
        body_sim = jaccard(na["body_w"], nb["body_w"])
        title_sim = jaccard(na["title_w"], nb["title_w"])
        tag_sim = jaccard(na["tags"], nb["tags"])
        # title agreement and tag agreement are strong duplicate signals
        score = 0.5 * body_sim + 0.35 * title_sim + 0.15 * tag_sim
        if score >= args.threshold:
            pairs.append((score, a, b, body_sim, title_sim))
    pairs.sort(key=lambda x: -x[0])

    # Group pairs into families. Duplicates arrive in clusters, not couples: one insight
    # re-extracted from N conversations yields N notes and N*(N-1)/2 pairs, which reads as
    # scattered noise until you group it. A 16-note family is 120 pairs — the single most
    # important signal in the report, and pairwise output buries it completely.
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    for _, a, b, _, _ in pairs:
        union(a, b)
    fams = {}
    for _, a, b, _, _ in pairs:
        fams.setdefault(find(a), set()).update((a, b))
    best = {}
    for score, a, b, _, _ in pairs:
        r = find(a)
        best[r] = max(best.get(r, 0), score)
    families = sorted(fams.values(), key=lambda m: (-len(m), -best[find(next(iter(m)))]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    dupes = sum(len(m) - 1 for m in families)
    lines = ["---\ntype: dashboard\ntags:\n  - maintenance\n---\n",
             "# 👯 Duplicate Candidates",
             f"*{len(families)} famil(ies) covering {sum(len(m) for m in families)} notes at "
             f"threshold {args.threshold} (from {len(pairs)} pairs). Merging every family down to "
             f"one note each would remove {dupes} files.*\n",
             "> Keep the best-written note, fold in anything unique from the rest, redirect "
             "inbound links, then delete. Biggest families first — they are where the win is.\n"]
    if not families:
        lines.append("✅ No likely duplicates found.")
    for m in families:
        members = sorted(m)
        lines.append(f"## {len(members)} notes — up to {len(members) - 1} removable")
        lines += [f"- [[{x}]]" for x in members]
        lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(VAULT)} — {len(families)} famil(ies), "
          f"{len(pairs)} pair(s), {dupes} removable.")

if __name__ == "__main__":
    main()
