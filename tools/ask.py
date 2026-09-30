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
    files, outside = [], 0
    for d in SEARCH_DIRS:
        for f in (VAULT / d).rglob("*.md"):
            # Skip _-prefixed files and directories, with one deliberate exception:
            # Skills/_proposed, admitted at PROPOSED_WEIGHT (see above).
            parts = f.relative_to(VAULT).parts
            if any(p.startswith("_") and p != "_proposed" for p in parts):
                continue
            # rglob yields symlinks. One that dangles, or whose target lies outside the vault (a
            # note or a whole folder linked in from elsewhere), is skipped: the embedding index is
            # keyed by the resolved vault-relative path and crashed on it, and the vault's edge is
            # what may be sent to the model, as in rlm.py's sandbox.
            try:
                f.resolve(strict=True).relative_to(VAULT)
            except (OSError, RuntimeError, ValueError):
                outside += 1
                continue
            files.append(f)
    if outside:
        print(f"ask: skipped {outside} file(s) that are dangling symlinks or lead outside the vault",
              file=sys.stderr)
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
    # Bytes decoded as-is, exactly as embed.py reads them: its chunk spans count a CRLF as two
    # characters, and read_text() would fold it to one, so on a CRLF transcript (Windows) every
    # line above the match shifted the excerpt and the rerank passage past what embeddings found.
    out = []
    for f in gather():
        try:
            out.append((f, f.read_bytes().decode("utf-8", "ignore")))
        except OSError:        # deleted by a concurrent pull since gather(); embed._sync skips it too
            continue
    return out

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

# Cross-encoder rerank of the fused list's head. A bi-encoder (embed.py) embeds question and note
# separately and compares vectors; a cross-encoder reads the pair together and scores relevance
# directly — much sharper at the top, far too slow to run over a whole vault, so it only reorders
# the first RERANK_DEPTH. Routed by the question's script: ms-marco is English-only (on Russian
# questions it pushed the right note from rank 1-6 to 9-148), mmarco is its multilingual sibling
# and is weaker and ~4x slower on English. Benchmarks, 2026-09 (notes/ has both): on a 291-note
# subset R@1 0.35 -> 0.67 and R@8 0.72 -> 0.86; on a 5,286-file vault, depth sweep of R@8 / rerank
# ms: 20 0.46/210, 50 0.55/500, 100 0.59/970, 150 0.62/2200, 200 0.63/3500 (hybrid alone 0.38).
# Past 100, R@1 and MRR are flat and English R@8 barely moves (0.73 -> 0.75) while latency doubles.
RERANK_DEPTH = 100
RERANK_CHARS = 1200
RERANK_MODELS = {"en": "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 "multi": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"}
_CE: dict[str, object] = {}

def _cross_encoder(q: str):
    lane = "multi" if re.search(r"[А-Яа-яЁё]", q) else "en"
    if lane not in _CE:
        from sentence_transformers import CrossEncoder
        _CE[lane] = CrossEncoder(RERANK_MODELS[lane], device="cpu")
    return _CE[lane]

def rerank(q: str, ranked: list[Path], text_of: dict[str, str], spans: dict[str, tuple[int, int]] | None = None,
           depth: int = RERANK_DEPTH) -> list[Path]:
    """Reorder ranked[:depth] by cross-encoder score over (question, title + passage); the tail keeps
    its order. The passage is the note's head, or for a long file the region around embed.py's best
    chunk. Any failure (model not downloaded, offline) returns the input object unchanged."""
    head = ranked[:depth]
    if len(head) < 2:
        return ranked
    try:
        from embed import strip_frontmatter
        model = _cross_encoder(q)
        pairs = []
        for f in head:
            key = f.resolve().as_posix().lower()
            body = strip_frontmatter(text_of.get(key, ""))
            lo = spans[key][0] if spans and key in spans and len(body) > 2 * RERANK_CHARS else 0
            pairs.append((q, f"{f.stem}. {body[lo: lo + RERANK_CHARS]}"))
        scores = model.predict(pairs, batch_size=32, show_progress_bar=False)
    except Exception as e:
        print(f"rerank: skipped ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
        return ranked
    return [f for _, f in sorted(zip(scores, head), key=lambda x: -x[0])] + ranked[depth:]

def state_context(q: str) -> tuple[str, list[str]]:
    """State-first hop. If the question names a registered State-ledger entity (or alias), prepend
    the ledger's current facts for it — dated, sourced — so "where does X run" is answered from
    State/facts.jsonl before any note is searched. Prose that restates state rots; the ledger folds."""
    try:
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("state", Path(__file__).resolve().parent / "state.py")
        st = _iu.module_from_spec(spec); spec.loader.exec_module(st)
        kinds, ents, alias = st.load_entities()
        if not ents:
            return "", []
        ql = q.lower()
        hits: list[str] = []
        for name, eid in sorted(alias.items(), key=lambda kv: -len(kv[0])):
            if len(name) < 3 or eid in hits:
                continue
            if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", ql):
                hits.append(eid)
        if not hits:
            return "", []
        facts, _ = st.load_facts()
        cur, obs = st.fold(facts), st.load_observed()
        lines = ["### STATE (ledger — current view, dated and sourced; prefer it over prose for "
                 "where-does-X-run / status / flag questions; cite [[State Register]] and the fact's source. "
                 "A ⚠️ flag means the value is uncertain: say it is stale or disputed, do not assert it as current)"]
        for eid in hits[:6]:
            recs = cur.get(eid, {})
            if not recs:
                lines.append(f"- {eid}: no facts recorded")
                continue
            for attr, rec in sorted(recs.items(), key=lambda kv: st.attr_order(kv[0])):
                # The same warnings `state.py show` prints: this block tells the model to prefer the
                # ledger over prose, so a stale or contested value must not arrive looking settled.
                flags = ""
                if st.is_stale(rec, eid, attr, kinds, ents, obs):
                    flags += f"  ⚠️ STALE: not re-observed since {st.last_seen(rec, eid, attr, obs)}"
                for c in rec.get("conflicts") or []:
                    flags += f"  ⚠️ CONFLICT: {c.get('reason') or 'unresolved contradiction'}"
                lines.append(f"- {eid}.{attr} = {st.render_value(rec)}  ({rec.get('kind')}, "
                             f"{st.when_str(rec, eid, attr, obs)}; source: {st.src_str(rec) or 'n/a'}){flags}")
        return "\n".join(lines) + "\n", hits
    except Exception as e:
        print(f"state hop skipped ({type(e).__name__})", file=sys.stderr)
        return "", []

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
    # rerank is the default since 2026-09 (see RERANK_DEPTH). Needs the two cross-encoders (~0.5 GB,
    # fetched from Hugging Face on first use); without them it warns and keeps the hybrid order.
    ap.add_argument("--no-rerank", dest="rerank", action="store_false",
                    help=f"skip the cross-encoder reorder of the top {RERANK_DEPTH} (embed/hybrid modes)")
    ap.add_argument("--log", action="store_true", help="append the Q&A to Brain Q&A Log.md")
    ap.add_argument("--retrieve-only", action="store_true",
                    help="print the ranked sources and stop — no model call (for checking retrieval)")
    args = ap.parse_args()

    q = " ".join(args.question)
    state_ctx, _ = state_context(q)
    corpus = load_corpus()
    text_of = {f.resolve().as_posix().lower(): txt for f, txt in corpus}
    spans: dict[str, tuple[int, int]] = {}
    reranked = False
    if args.mode != "lexical" and importlib.util.find_spec("sentence_transformers") is None:
        # Embeddings are opt-in: torch plus sentence-transformers is a gigabyte-class install and
        # everything else here runs on numpy. Without it, answer anyway.
        print("embed: sentence-transformers is not installed, so this is keyword search only. "
              "Opt in with `pip install sentence-transformers`.", file=sys.stderr)
        args.mode = "lexical"
    if args.mode == "lexical":
        ranked = lexical_rank(q, corpus)
    else:
        from embed import Index, strip_frontmatter, pick_model
        hint = pick_model([f for f, _ in corpus])
        if hint:
            print(hint, file=sys.stderr)
        idx = Index.open([f for f, _ in corpus])
        spans = {(VAULT / r).resolve().as_posix().lower(): (s, e) for r, (_, s, e) in idx.doc_scores(q).items()}
        ranked = embed_rank(q, idx) if args.mode == "embed" else hybrid_rank(q, corpus, idx)
        if args.rerank:
            fused = ranked
            ranked = rerank(q, fused, text_of, spans)
            reranked = ranked is not fused      # rerank hands back its input object when it falls back
    top = ranked[: args.top]
    label = f"{args.mode}{'+rerank' if reranked else ''}"
    if not top and not state_ctx:
        # Stop only when BOTH sources are empty: a question the ledger answers ("where does X
        # run") in a vault with no matching prose used to be dropped here (review, 2026-09-30).
        print("No relevant notes found. Try different words, or import/extract more first.")
        return
    if args.retrieve_only:
        if state_ctx:
            print(state_ctx)
        print(f"sources ({label}):\n" + ("\n".join(f"  {i}. {f.stem}" for i, f in enumerate(top, 1))
                                          or "  (none; the STATE block above is the evidence)"))
        return

    ctx, used, budget = [], 0, 300_000
    if state_ctx:
        ctx.append(state_ctx)
        used += len(state_ctx)
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
        "Be concise and direct. If the notes don't contain the answer, say so plainly. "
        "If a STATE block is present it is the state ledger's current view (dated, sourced): for "
        "questions about where something runs, its status or its flags, answer from STATE and cite "
        "[[State Register]] plus the fact's own source.\n\n"
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
    print(f"\n— sources scanned ({label}): " + ", ".join(f.stem for f in top))

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
