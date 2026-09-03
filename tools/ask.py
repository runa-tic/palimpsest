#!/usr/bin/env python3
"""Ask your second brain a question and get an answer with citations.

Retrieves the most relevant notes/conversations (keyword score fused with local multilingual
embeddings, see embed.py), then asks the local `claude` CLI to answer using only those, citing
sources as [[Note Title]].

Usage (from vault root):
  python tools/ask.py "what did I conclude about label geometry?"
  python tools/ask.py --top 12 --log "how should I tier LLM calls by cost?"

No API key needed; uses your Claude Code login.
"""
from __future__ import annotations
import sys, os, re, math, subprocess, argparse, importlib.util
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
    return s * dir_weight(path)

def dir_weight(path: Path) -> float:
    parts = path.relative_to(VAULT).parts
    if "_proposed" in parts:
        return PROPOSED_WEIGHT
    return DIR_WEIGHT.get(parts[0], 1.0)

def load_corpus() -> list[tuple[Path, str]]:
    return [(f, f.read_text(encoding="utf-8", errors="ignore")) for f in gather()]

# ---- the three retrieval modes. Each returns documents best-first; the benchmark in
# bench_retrieval.py scores exactly these functions, so what is measured is what ships.

def lexical_rank(q: str, corpus: list[tuple[Path, str]]) -> list[Path]:
    """Keyword overlap with sublinear tf, length normalisation and the directory prior."""
    qt = tokens(q)
    scored = [(score(qt, txt, f.stem, f), f) for f, txt in corpus]
    scored = [(s, f) for s, f in scored if s > 0]
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored]

# e5 cosines for a whole corpus sit in a band of roughly 0.75-0.90. A multiplicative 0.5-1.6
# directory prior on the raw cosine swamps that signal; on (cosine - EMBED_FLOOR) the same prior
# is the nudge it is in the lexical lane. Benchmark 2026-09-03 (R@8, en/ru): length penalty alone
# 0.48/0.22, penalty + this prior 0.55/0.25. Floors 0.65-0.75 score the same; 0.70 is the middle.
EMBED_FLOOR = 0.70
# Weight of the lexical list inside hybrid fusion. Equal weights LOST to embeddings alone on this
# vault: a Russian question yields a keyword list that is noise against English notes, and RRF
# gave it half the vote (R@8 ru 0.15 vs 0.25 embed-only). At 0.3 the lexical lane is a tie-breaker
# that still lifts exact identifiers: en 0.58 / ru 0.25 vs embed-only 0.55 / 0.25.
LEX_WEIGHT = 0.3

def embed_rank(q: str, idx) -> list[Path]:
    """Length-penalised best-chunk cosine from embed.Index, shifted to EMBED_FLOOR and scaled by
    the directory prior; documents best-first."""
    scored = []
    for rel, (s, _, _) in idx.doc_scores(q).items():
        p = VAULT / rel
        scored.append(((s - EMBED_FLOOR) * dir_weight(p), p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored]

RRF_K = 60

def hybrid_rank(q: str, corpus: list[tuple[Path, str]], idx, k: int = RRF_K, depth: int = 200) -> list[Path]:
    """Weighted reciprocal-rank fusion of the lexical and embedding lists. RRF needs no
    calibration between two score scales that have nothing in common; the weight (LEX_WEIGHT)
    encodes how much each list is trusted, not how its scores compare. Each list already carries
    the directory prior in its order, so it is not applied again here."""
    fused: dict[str, tuple[float, Path]] = {}
    for ranked, w in ((lexical_rank(q, corpus), LEX_WEIGHT), (embed_rank(q, idx), 1.0)):
        for r, p in enumerate(ranked[:depth]):
            key = p.resolve().as_posix().lower()
            fused[key] = (fused.get(key, (0.0, p))[0] + w / (k + r + 1), p)
    return [p for _, p in sorted(fused.values(), key=lambda x: -x[0])]

def main():
    ap = argparse.ArgumentParser(description="Ask your second brain.")
    ap.add_argument("question", nargs="+")
    ap.add_argument("--top", type=int, default=8, help="how many notes to feed as context")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    # hybrid became the default 2026-09-03 on the benchmark in
    # `40 Resources/Retrieval benchmark — lexical vs embeddings.md`: R@8 en 0.58 vs 0.31 lexical,
    # ru 0.25 vs 0.02. Costs ~130 ms more per question plus embedding whatever changed since the
    # last query (~40 s after a day of edits). --mode lexical is the old behaviour.
    ap.add_argument("--mode", choices=["lexical", "embed", "hybrid"], default="hybrid",
                    help="retrieval: weighted fusion of keyword + local embeddings (default), or either alone")
    ap.add_argument("--log", action="store_true", help="append the Q&A to Brain Q&A Log.md")
    args = ap.parse_args()

    q = " ".join(args.question)
    corpus = load_corpus()
    text_of = {f.resolve().as_posix().lower(): txt for f, txt in corpus}
    spans: dict[str, tuple[int, int]] = {}
    if args.mode != "lexical" and importlib.util.find_spec("sentence_transformers") is None:
        # Embeddings are opt-in: torch plus sentence-transformers is a gigabyte-class install and
        # everything else here runs on numpy. Without it, answer anyway.
        print("embed: sentence-transformers is not installed, so this is keyword search only. "
              "Opt in with `pip install sentence-transformers`.", file=sys.stderr)
        args.mode = "lexical"
    if args.mode == "lexical":
        ranked = lexical_rank(q, corpus)
    else:
        from embed import Index, strip_frontmatter
        idx = Index.open([f for f, _ in corpus])
        spans = {(VAULT / r).resolve().as_posix().lower(): (s, e) for r, (_, s, e) in idx.doc_scores(q).items()}
        ranked = embed_rank(q, idx) if args.mode == "embed" else hybrid_rank(q, corpus, idx)
    top = ranked[: args.top]
    if not top:
        print("No relevant notes found. Try different words, or import/extract more first.")
        return

    ctx, used, budget = [], 0, 300_000
    for f in top:
        key = f.resolve().as_posix().lower()
        txt = text_of[key]
        if len(txt) > 8000 and key in spans:
            # Embeddings know WHERE in a long transcript the relevant passage sits. Hand the
            # model that region instead of the first 8000 characters of the file.
            body = strip_frontmatter(txt)
            lo = max(0, spans[key][0] - 2000)
            excerpt = ("…" if lo else "") + body[lo: lo + 8000]
        else:
            excerpt = txt[:8000]
        block = f"### NOTE: {f.stem}\n{excerpt}\n"
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
    print(f"\n— sources scanned ({args.mode}): " + ", ".join(f.stem for f in top))

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
