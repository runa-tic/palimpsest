#!/usr/bin/env python3
"""Ask your second brain a question and get an answer with citations.

Retrieves the most relevant notes/conversations by keyword score, then asks the local
`claude` CLI to answer using only those, citing sources as [[Note Title]].

Usage (from vault root):
  python _tools/ask.py "what did I conclude about label geometry?"
  python _tools/ask.py --top 12 --log "how should I tier LLM calls by cost?"

No API key needed; uses your Claude Code login.
"""
from __future__ import annotations
import sys, os, re, math, subprocess, argparse
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
SEARCH_DIRS = ["10 Notes", "Skills", "40 Resources", "20 Projects", "30 Areas", "Daily", "60 Maps of Content"]
# Curated knowledge should outrank raw transcripts of the conversation that produced it.
# Without this a session log beats the atomic note distilled from it, every time.
DIR_WEIGHT = {"10 Notes": 1.6, "Skills": 1.6, "20 Projects": 1.4, "30 Areas": 1.4,
              "60 Maps of Content": 1.2, "Daily": 1.0, "40 Resources": 0.7}
# Skills/_proposed holds 600+ unreviewed skill candidates. A 2026-07-31 audit found they are
# NOT noise (none duplicates an atomic note above 31% similarity, and the sample is sound
# engineering knowledge) but nobody will ever read 600 files to promote them. So make them
# reachable at a weight that keeps them behind everything curated: they surface only when
# nothing better matches.
PROPOSED_WEIGHT = 0.5
LOG_NOTE = VAULT / "40 Resources" / "Brain Q&A Log.md"
# Deliberately NOT the extraction model. Distilling is a bulk job run nightly on every
# conversation, so it optimises for cost; answering is interactive, occasional, and judged
# directly by you, so it optimises for quality. Same reason you would not staff a call centre
# and a design review with the same tier.
DEFAULT_MODEL = "claude-sonnet-5"
STOP = set("a an the of to in on for and or is are be was were been do does did i we you my our your "
           "what how why when which who whom this that these those with from as at by it its their there "
           "about into over under can could should would will shall may might have has had not no".split())

def tokens(s: str) -> list[str]:
    # Cyrillic included: this is a bilingual vault, and an [a-z0-9]-only pattern made every
    # Russian question tokenize to nothing, so ask.py answered "No relevant notes found"
    # no matter what the vault actually held.
    return [w for w in re.findall(r"[a-z0-9а-яё]+", s.lower()) if w not in STOP and len(w) > 2]

def gather() -> list[Path]:
    files = []
    for d in SEARCH_DIRS:
        for f in (VAULT / d).rglob("*.md"):
            # Skip _-prefixed files and directories, with one deliberate exception:
            # Skills/_proposed, admitted at PROPOSED_WEIGHT (see above).
            parts = f.relative_to(VAULT).parts
            if any(p.startswith("_") and p != "_proposed" for p in parts):
                continue
            files.append(f)
    return files

def score(qtoks: list[str], text: str, title: str, path: Path) -> float:
    tl, body = title.lower(), text.lower()
    s = 0.0
    for t in set(qtoks):
        c = body.count(t)
        if c:
            s += 1 + math.log(c)   # sublinear: the 40th occurrence is not 40x the evidence
        if t in tl:
            s += 5
    # Length normalisation. Raw term counts meant a 900 KB transcript outscored the exact
    # note simply by being long enough to mention every query word a few times.
    s /= math.log(len(body) + 100)
    parts = path.relative_to(VAULT).parts
    if "_proposed" in parts:
        return s * PROPOSED_WEIGHT
    return s * DIR_WEIGHT.get(parts[0], 1.0)

def main():
    ap = argparse.ArgumentParser(description="Ask your second brain.")
    ap.add_argument("question", nargs="+")
    ap.add_argument("--top", type=int, default=8, help="how many notes to feed as context")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--log", action="store_true", help="append the Q&A to Brain Q&A Log.md")
    args = ap.parse_args()

    q = " ".join(args.question)
    qt = tokens(q)
    scored = []
    for f in gather():
        txt = f.read_text(encoding="utf-8", errors="ignore")
        sc = score(qt, txt, f.stem, f)
        if sc > 0:
            scored.append((sc, f, txt))
    scored.sort(key=lambda x: -x[0])
    top = scored[: args.top]
    if not top:
        print("No relevant notes found. Try different words, or import/extract more first.")
        return

    ctx, used, budget = [], 0, 300_000
    for sc, f, txt in top:
        block = f"### NOTE: {f.stem}\n{txt[:8000]}\n"
        if used + len(block) > budget:
            break
        ctx.append(block)
        used += len(block)

    prompt = (
        "You are the user's second brain. Answer the QUESTION using ONLY the notes below. "
        "Cite every claim inline as [[Note Title]] using the exact NOTE titles. "
        "Be concise and direct. If the notes don't contain the answer, say so plainly.\n\n"
        f"QUESTION: {q}\n\n===NOTES===\n" + "\n".join(ctx)
    )
    proc = subprocess.run(["claude", "-p", "--model", args.model],
                          input=prompt, capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, "CLAUDE_BRAIN_NO_HOOK": "1"})  # don't trigger vault hooks
    if proc.returncode != 0:
        print("claude CLI failed:", (proc.stderr or "").strip()[:300])
        sys.exit(1)
    ans = proc.stdout.strip()
    print(ans)
    print("\n— sources scanned: " + ", ".join(f.stem for _, f, _ in top))

    if args.log:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n## ❓ {q}\n*{ts}*\n\n{ans}\n"
        LOG_NOTE.parent.mkdir(parents=True, exist_ok=True)
        if not LOG_NOTE.exists():
            LOG_NOTE.write_text("---\ntype: index\ntags:\n  - brain/qa\n---\n\n# 🧠 Brain Q&A Log\n", encoding="utf-8")
        with LOG_NOTE.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        print(f"\n(logged to {LOG_NOTE.relative_to(VAULT)})")

if __name__ == "__main__":
    main()
