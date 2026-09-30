#!/usr/bin/env python3
"""Turn the skill-proposal queue into review batches a human will actually finish.

`extract_skills.py` proposes; a human promotes. That gate is the design. But a gate nobody
can pass through is the same as no skills at all: this vault reached 622 proposals against
20 promoted, and nobody was ever going to read 622 files.

WHAT THIS DOES NOT DO, and why — both measured on the real queue on 2026-08-09, because
both were my confident guesses first:

  * "Cluster size = votes." The idea was that the same procedure extracted from three
    different conversations is evidence it recurs. Measured: every high-similarity pair in
    the queue shares a source conversation. The duplicates are chunking artifacts from one
    long session, not independent rediscovery. Requiring 2+ distinct sources shortlists
    exactly nothing, forever.
  * "Trigger vocabulary recurring across the corpus = reusable." Measured: median 23
    matching conversations, 572 of 623 proposals scoring 6+. It does not discriminate, and
    it is arguably inverted — the four LOWEST scorers were the most specific and useful
    procedures in the queue (pythonw for pm2-spawned processes, per-machine .local.json).

So quality ranking without judgment is not available here. What IS available is hygiene and
sequencing, which is enough to make the gate passable:

  structural  — no trigger, no steps, or no rationale means it is a note, not a procedure.
  duplicate   — collapse near-identical proposals to their most complete representative.
  covered     — near-duplicate of an already-promoted skill; the gate already ran on it.
  batching    — emit a small themed batch per run instead of one flat 600-item list.

Eight decisions a week is a gate someone uses; 622 is a gate that guarantees nothing is ever
promoted. Whether a procedure is CORRECT for this environment stays a human's call: a
promoted skill is consulted before acting, so a wrong one is applied later, autonomously.

Usage:
  python tools/triage_skills.py                 # write Reviews/Skill Proposals.md
  python tools/triage_skills.py --batch 12
"""
from __future__ import annotations
import sys, re, json, argparse
from pathlib import Path
from datetime import datetime
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dedupe import words, jaccard          # one tuned similarity function, not two

VAULT = Path(__file__).resolve().parent.parent
SKILLS = VAULT / "Skills"
PROPOSED = SKILLS / "_proposed"
OUT = VAULT / "Reviews" / "Skill Proposals.md"

# Calibrated on the real queue: with body words included, pairwise similarity is
# max 0.43 / p99 0.09 / median 0.016, and hand-checked pairs at 0.30+ were genuinely the
# same procedure. Title+trigger alone is useless here (median 0.000) — the strings are too
# short for Jaccard, which is why dedupe.py weighs bodies too.
DUP_SIM = 0.30
COVERED_SIM = 0.30


def parse(path: Path) -> dict:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    fm = re.match(r"---\n(.*?)\n---", txt, re.DOTALL)
    front = fm.group(1) if fm else ""
    m = re.search(r"^trigger:\s*(.*?)\s*$", front, re.M)
    trigger = m.group(1) if m else ""
    if trigger.startswith('"'):
        # extract_skills writes the trigger JSON-escaped (a valid YAML double-quoted scalar);
        # older proposals hold it raw between quotes. A raw one can still parse as JSON —
        # "C:\new\tools" decodes to a newline and a tab — so the decoded text counts only if it
        # is exactly what the writer would produce: it re-encodes to the same line, and holds no
        # whitespace but spaces (the writer collapses it).
        try:
            dec = json.loads(trigger)
            ok = (isinstance(dec, str) and json.dumps(dec, ensure_ascii=False) == trigger
                  and not re.search(r"[^\S ]", dec))
        except ValueError:
            ok = False
        if ok:
            trigger = dec
        else:
            trigger = trigger[1:-1] if len(trigger) > 1 and trigger.endswith('"') else trigger[1:]
    trigger = trigger.strip()
    m = re.search(r"^source:\s*\"?\[\[(.+?)\]\]", front, re.M)
    source = m.group(1).strip() if m else ""
    m = re.search(r"^created:\s*(\S+)", front, re.M)
    created = m.group(1) if m else ""
    tags = set(re.findall(r"^\s+- (.+)$", front, re.M)) - {"skill/proposed", "skill"}
    body = re.sub(r"> Proposed by extract_skills.*", "", txt.split("---", 2)[-1], flags=re.S)
    steps = re.search(r"##\s*Steps\s*\n(.*?)(?=\n##|\Z)", body, re.S)
    why = re.search(r"##\s*Why\s*\n(.*?)(?=\n##|\Z)", body, re.S)
    step_lines = [l for l in (steps.group(1).splitlines() if steps else [])
                  if l.strip() and re.match(r"\s*(?:[-*]|\d+\.)\s+\S", l)]
    return {"path": path, "title": path.stem, "trigger": trigger, "source": source,
            "created": created, "tags": tags, "steps": len(step_lines),
            "why": (why.group(1).strip() if why else ""),
            "tw": words(path.stem), "gw": words(trigger), "bw": words(body)}


def sim(a: dict, b: dict) -> float:
    return (0.30 * jaccard(a["tw"], b["tw"]) + 0.20 * jaccard(a["gw"], b["gw"])
            + 0.35 * jaccard(a["bw"], b["bw"]) + 0.15 * jaccard(a["tags"], b["tags"]))


def theme(p: dict) -> str:
    """Dominant non-generic tag, so a batch reads as one topic instead of a grab bag."""
    generic = {"skill", "skills", "meta", "claude/extracted", "workflow", "process"}
    for t in sorted(p["tags"]):
        if t.lower() not in generic:
            return t
    return "misc"


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch skill proposals for human review.")
    ap.add_argument("--batch", type=int, default=8, help="proposals to surface this run")
    args = ap.parse_args()

    if not PROPOSED.exists():
        print("no Skills/_proposed/ — nothing to triage")
        return 0
    props = [parse(f) for f in sorted(PROPOSED.glob("*.md"))]
    promoted = [parse(f) for f in sorted(SKILLS.glob("*.md")) if f.stem.lower() != "readme"]
    total = len(props)

    incomplete = [p for p in props if not p["trigger"] or p["steps"] < 1 or not p["why"]]
    live = [p for p in props if p not in incomplete]

    covered = [p for p in live if any(sim(p, q) >= COVERED_SIM for q in promoted)]
    live = [p for p in live if p not in covered]

    # Collapse duplicates to the most complete member; keep the rest to delete on promote.
    clusters: list[list[dict]] = []
    for p in live:
        for c in clusters:
            if any(sim(p, q) >= DUP_SIM for q in c):
                c.append(p)
                break
        else:
            clusters.append([p])
    dupes = sum(len(c) - 1 for c in clusters)

    reps = [max(c, key=lambda p: (p["steps"], len(p["why"]))) for c in clusters]
    dup_of = {id(max(c, key=lambda p: (p["steps"], len(p["why"])))): [p for p in c if p is not
              max(c, key=lambda p: (p["steps"], len(p["why"])))] for c in clusters}

    # Sequence: themed together, most-developed first, newest first. Deterministic, so the
    # same batch reappears until the files are actually promoted or deleted.
    by_theme = Counter(theme(p) for p in reps)
    reps.sort(key=lambda p: (-by_theme[theme(p)], theme(p), -p["steps"], p["created"]),
              reverse=False)
    batch = reps[: args.batch]

    lines = ["---", "type: index", "tags:", "  - skills", "  - review", "---", "",
             "# 🛠 Skill Proposals — this week's batch", "",
             f"*Generated {datetime.now():%Y-%m-%d %H:%M} by `tools/triage_skills.py`. "
             f"Promoting stays yours: a promoted skill is consulted before acting, so a wrong "
             f"one gets applied later, autonomously. This only decides reading order.*", "",
             f"**{total} proposals → {len(reps)} distinct → {len(batch)} in this batch.** "
             f"{len(incomplete)} incomplete (no trigger, no steps, or no rationale), "
             f"{len(covered)} already covered by a promoted skill, {dupes} near-duplicates "
             f"folded into their most complete version. Nothing is deleted — everything stays "
             f"in `Skills/_proposed/` until you act.", "",
             "For each: **promote** (move into `Skills/`, set `status: active`) or **delete**. "
             "Do neither and it returns next run.", ""]

    for i, p in enumerate(batch, 1):
        lines += [f"## {i}. {p['title']}", "",
                  f"- **Trigger:** {p['trigger'] or '—'}",
                  f"- **Theme:** {theme(p)} · {p['steps']} step(s) · proposed {p['created']}",
                  f"- **File:** `Skills/_proposed/{p['path'].name}`"]
        others = dup_of.get(id(p)) or []
        if others:
            lines += [f"- **Folds in {len(others)} near-duplicate(s):** "
                      + ", ".join(f"`{q['path'].name}`" for q in others[:4])
                      + (" …" if len(others) > 4 else "")]
        lines += [""]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(VAULT)} — {total} proposals -> {len(reps)} distinct, "
          f"{len(batch)} batched ({len(incomplete)} incomplete, {len(covered)} covered, "
          f"{dupes} duplicates folded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
