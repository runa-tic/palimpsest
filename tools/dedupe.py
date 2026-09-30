#!/usr/bin/env python3
"""Flag near-duplicate atomic notes in 10 Notes/ so you can merge them.

Extraction re-runs over evolving conversations naturally produce notes that say the same
thing in different words. This finds likely pairs (by word + tag overlap) and writes them
to Reviews/Duplicate Candidates.md for you to review. Report-only — never deletes or merges.

Usage (from vault root):
  python tools/dedupe.py                 # default threshold 0.50
  python tools/dedupe.py --threshold 0.35   # wider net, mostly noise
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
    # Unicode word characters: [a-z0-9] dropped every Cyrillic word, so all a Russian note had
    # left was its English headings, every Russian pair scored 1.0 and one family swallowed them.
    return {w for w in re.findall(r"[^\W_]+", s.lower()) if w not in STOP and len(w) > 3}

# Boilerplate every note shares: heading lines, and the generated Related / Source sections
# (links and "From conversation"). Left in, they are the overlap between any two notes.
BOILER = re.compile(r"(?ms)^#{1,6} (?:Related|Source)\b.*?(?=^#{1,6} |\Z)|^#{1,6} [^\n]*$")

def jaccard(a: set, b: set) -> float:
    # 0 when EITHER side is empty: nothing to compare is not evidence of agreement.
    return len(a & b) / len(a | b) if (a and b) else 0.0

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
            "body_w": words(BOILER.sub("", body)),
            "tags": tags,
            "text": body,          # raw, for the semantic pass
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
    # 0.78 + a lexical FLOOR, not LSA alone. Calibrated 2026-08-09 and it is not monotonic:
    # the 0.97+ band is dominated by same-topic-different-claim pairs (every PKM note points
    # the same way in a dense topic), while the real reworded duplicates sit at 0.78-0.87.
    # Requiring a few shared distinctive words kills the pure-topic pairs without losing them.
    ap.add_argument("--semantic-threshold", type=float, default=0.78,
                    help="min LSA similarity for the meaning-based pass (0-1)")
    ap.add_argument("--semantic-lexical-floor", type=float, default=0.08,
                    help="min word overlap a semantic pair must also clear")
    ap.add_argument("--no-semantic", action="store_true", help="lexical pass only")
    args = ap.parse_args()

    notes = load()
    pairs = []
    flagged = set()
    for a, b in combinations(notes, 2):
        na, nb = notes[a], notes[b]
        body_sim = jaccard(na["body_w"], nb["body_w"])
        title_sim = jaccard(na["title_w"], nb["title_w"])
        tag_sim = jaccard(na["tags"], nb["tags"])
        # title agreement and tag agreement are strong duplicate signals
        score = 0.5 * body_sim + 0.35 * title_sim + 0.15 * tag_sim
        if score >= args.threshold:
            pairs.append((score, a, b, body_sim, title_sim))
            flagged.add(frozenset((a, b)))

    # Second pass, on meaning rather than vocabulary. Word overlap cannot see a duplicate that
    # was reworded: measured 2026-08-09, "GramJS FloodWaitError carries the wait duration on
    # .seconds" and "...carries the wait on .seconds" — the same note twice — score 0.13 here,
    # nowhere near any usable threshold. LSA scores that pair 0.79. Precision at this cut is
    # roughly half, which is the right trade for a report that FLAGS and never merges: the
    # alternative is that these stay invisible forever.
    semantic_only, sem_pairs = 0, []
    if not args.no_semantic:
        try:
            from semantic import Space
            docs = {n: n + "\n" + notes[n]["text"] for n in notes}
            sp = Space.build(docs)
            M = sp.matrix()
            for i in range(len(sp.names)):
                for j in range(i + 1, len(sp.names)):
                    if M[i, j] < args.semantic_threshold:
                        continue
                    a, b = sp.names[i], sp.names[j]
                    if frozenset((a, b)) in flagged:
                        continue
                    lex = jaccard(notes[a]["title_w"] | notes[a]["body_w"],
                                  notes[b]["title_w"] | notes[b]["body_w"])
                    if lex < args.semantic_lexical_floor:
                        continue
                    sem_pairs.append((float(M[i, j]), a, b))
                    semantic_only += 1
        except Exception as e:
            print(f"  (semantic pass skipped: {type(e).__name__}: {e})")
    sem_pairs.sort(key=lambda x: -x[0])
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
    fam_of = {}
    for fi, m in enumerate(families):
        for n in m:
            fam_of[n] = fi
    sem_pairs = [(sc, a, b) for sc, a, b in sem_pairs
                 if fam_of.get(a, -1) != fam_of.get(b, -2)]
    if sem_pairs:
        shown = sem_pairs[:60]
        lines.append("---\n")
        lines.append(f"## 🧠 Found by meaning ({len(sem_pairs)} pairs)")
        lines.append("> These share almost no vocabulary, so no keyword threshold could ever "
                     "surface them — and roughly half are topically adjacent rather than "
                     "duplicate. Deliberately NOT merged into the families above: at this "
                     "precision two bad links would chain three real families into one blob. "
                     "Verify each pair on its own.\n")
        lines += [f"- `{sc:.2f}` [[{a}]] ↔ [[{b}]]" for sc, a, b in shown]
        if len(sem_pairs) > len(shown):
            lines.append(f"\n*{len(sem_pairs) - len(shown)} more below {shown[-1][0]:.2f} — "
                         f"raise `--semantic-threshold` to shorten this list.*")
        lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(VAULT)} — {len(families)} famil(ies), "
          f"{len(pairs)} pair(s), {dupes} removable"
          + (f" ({semantic_only} found by meaning, invisible to word overlap)." if semantic_only
             else "."))

if __name__ == "__main__":
    main()
