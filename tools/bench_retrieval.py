#!/usr/bin/env python3
"""Benchmark lexical vs embedding vs hybrid vs hybrid+rerank retrieval on this vault.

Ground truth is synthetic. For a seeded random sample of atomic notes, the claude CLI writes
one English question the note answers, phrased away from the title's own words, and one
natural Russian version. The note is the single relevant document. This is the standard way
to evaluate retrieval without hand labels (pseudo-queries), and the Russian half is the vault's
real use case: a bilingual operator asking in either language against mostly-English notes.

  python tools/bench_retrieval.py --gen 150      # write tools/cache/bench-queries.jsonl
  python tools/bench_retrieval.py --run          # score lexical / embed / hybrid, write report

The report goes to `40 Resources/Retrieval benchmark — lexical vs embeddings.md`.
"""
from __future__ import annotations
import sys, os, re, json, random, argparse, shutil, subprocess, time, importlib.util
from pathlib import Path

try:
    # UTF-8 whatever the code page, as callers read it, and backslashreplace: under "strict" a
    # surrogate (argv or a path that is not UTF-8) crashed the very error that carried it.
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask  # noqa: E402

VAULT = ask.VAULT
QUERIES = VAULT / "tools" / "cache" / "bench-queries.jsonl"
REPORT = VAULT / "40 Resources" / "Retrieval benchmark — lexical vs embeddings.md"
GEN_MODEL = "claude-sonnet-5"
BATCH = 15
TOPK = 20

GEN_PROMPT = """You are building a retrieval benchmark for a personal knowledge base.
For EACH note below write:
  "en": one natural question, in English, that a person would type when they need what this
        note says. Phrase it the way someone who has NOT read the note would ask — describe the
        situation or problem. Do NOT reuse the distinctive nouns/verbs of the note's TITLE;
        use synonyms or plain description instead.
  "ru": the same question in natural, idiomatic Russian (not a word-for-word translation).
Return ONLY a JSON array: [{"id": <id>, "en": "...", "ru": "..."}, ...]. No prose, no code fences.

"""


def _body(text: str) -> str:
    return re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S).strip()


def gen(n: int, seed: int):
    notes = [p for p in (VAULT / "10 Notes").glob("*.md")]
    rng = random.Random(seed)
    rng.shuffle(notes)
    done = set()
    if QUERIES.exists():
        for line in QUERIES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["rel"])
    picked = []
    for p in notes:
        rel = p.relative_to(VAULT).as_posix()
        if rel in done:
            continue
        body = _body(p.read_text(encoding="utf-8", errors="ignore"))
        if len(body) < 400:
            continue
        picked.append((rel, p.stem, body[:700]))
        if len(picked) >= n:
            break
    gone = sum(1 for rel in done if not (VAULT / rel).exists())
    print(f"generating queries for {len(picked)} notes ({len(done)} already cached"
          + (f", {gone} of them for notes that no longer exist, which --run skips" if gone else "") + ")")
    QUERIES.parent.mkdir(parents=True, exist_ok=True)
    with QUERIES.open("a", encoding="utf-8") as fh:
        for k in range(0, len(picked), BATCH):
            batch = picked[k:k + BATCH]
            prompt = GEN_PROMPT + "\n\n".join(f"### id={i}\nTITLE: {t}\nBODY: {b}" for i, (_, t, b) in enumerate(batch))
            # shutil.which honours PATHEXT, so npm's claude.cmd is found on Windows too.
            proc = subprocess.run([shutil.which("claude") or "claude", "-p", "--model", GEN_MODEL], input=prompt, capture_output=True,
                                  text=True, encoding="utf-8", env={**os.environ, "CLAUDE_BRAIN_NO_HOOK": "1"}, errors="replace")
            out = proc.stdout or ""
            if proc.returncode != 0 or "[" not in out:
                print(f"  batch {k // BATCH}: claude failed: {(proc.stderr or out).strip()[:200]}")
                continue
            try:
                arr = json.loads(out[out.index("["): out.rindex("]") + 1])
            except Exception as e:
                print(f"  batch {k // BATCH}: unparseable ({e}): {out[:120]!r}")
                continue
            wrote = 0
            for item in arr:
                try:
                    rel, title, _ = batch[int(item["id"])]
                    en, ru = item["en"].strip(), item["ru"].strip()
                except Exception:
                    continue
                if en and ru:
                    fh.write(json.dumps({"rel": rel, "title": title, "en": en, "ru": ru}, ensure_ascii=False) + "\n")
                    wrote += 1
            fh.flush()
            print(f"  batch {k // BATCH}: {wrote}/{len(batch)} queries")


def _families() -> dict[str, frozenset[str]]:
    """Near-duplicate families from Reviews/Duplicate Candidates.md (maintenance.py output):
    title -> all titles in its family. Titles there may be truncated; callers prefix-match."""
    f = VAULT / "Reviews" / "Duplicate Candidates.md"
    if not f.exists():
        return {}
    fams, cur = [], []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if cur:
                fams.append(cur)
            cur = []
        elif line.startswith("- [["):
            cur.append(line[4:].split("]]")[0].split("|")[0].strip())
    if cur:
        fams.append(cur)
    out: dict[str, frozenset[str]] = {}
    for fam in fams:
        fs = frozenset(fam)
        for t in fam:
            out[t] = fs
    return out


def _family_of(fams: dict[str, frozenset[str]], stem: str) -> frozenset[str]:
    if stem in fams:
        return fams[stem]
    for k, v in fams.items():
        if stem.startswith(k):
            return v
    return frozenset()


def _fam_rank(ranked: list[str], gold: str, members: frozenset[str]) -> int | None:
    """Rank of the gold note OR any near-duplicate of it. The vault carries 370+ duplicate
    notes; when a twin of the gold outranks it, every system is charged a miss it did not make."""
    if not members:
        return _rank_of(ranked, gold)
    for i, r in enumerate(ranked):
        st = Path(r).stem
        if r == gold or any(st == m or st.startswith(m) for m in members):
            return i + 1
    return None


def _rank_of(ranked: list[str], gold: str) -> int | None:
    try:
        return ranked.index(gold) + 1
    except ValueError:
        return None


def run():
    if importlib.util.find_spec("sentence_transformers") is None:
        sys.exit("embeddings are not installed; `pip install sentence-transformers` first")
    rows = [json.loads(l) for l in QUERIES.read_text(encoding="utf-8").splitlines() if l.strip()] if QUERIES.exists() else []
    if not rows:
        sys.exit(f"no queries at {QUERIES.relative_to(VAULT)}; run --gen N first")
    # A gold note merged, renamed or deleted since --gen is a data problem, not a retrieval miss:
    # scored, it charged every system a miss and read as a regression.
    gone = sum(1 for r in rows if not (VAULT / r["rel"]).exists())
    rows = [r for r in rows if (VAULT / r["rel"]).exists()]
    if gone:
        print(f"skipping {gone} cached quer{'y' if gone == 1 else 'ies'} whose gold note no longer exists "
              f"(`--gen {gone}` adds replacements)")
    if not rows:
        sys.exit("every cached query's gold note is gone; run --gen N first")
    corpus = ask.load_corpus()
    rel = lambda p: p.resolve().relative_to(VAULT).as_posix()
    from embed import Index, MODEL
    idx = Index.open([p for p, _ in corpus])

    text_of = {p.resolve().as_posix().lower(): t for p, t in corpus}

    def reranked(q):
        # exactly ask.py's default path: hybrid, then the language-routed cross-encoder
        spans = {(VAULT / r).resolve().as_posix().lower(): (s, e) for r, (_, s, e) in idx.doc_scores(q).items()}
        return [rel(p) for p in ask.rerank(q, ask.hybrid_rank(q, corpus, idx), text_of, spans)[:TOPK]]

    systems = {
        "lexical": lambda q: [rel(p) for p in ask.lexical_rank(q, corpus)[:TOPK]],
        "embed":   lambda q: [rel(p) for p in ask.embed_rank(q, idx)[:TOPK]],
        "hybrid":  lambda q: [rel(p) for p in ask.hybrid_rank(q, corpus, idx)[:TOPK]],
        "hybrid+rerank": reranked,
    }
    results = {s: {"en": [], "ru": []} for s in systems}
    resf = {s: {"en": [], "ru": []} for s in systems}          # family-aware ranks
    fams = _families()
    n_fam = sum(1 for r in rows if _family_of(fams, Path(r["rel"]).stem))
    timing = {s: 0.0 for s in systems}
    examples = {"embed_wins": [], "lexical_wins": []}
    for r in rows:
        got = {}
        for lang in ("en", "ru"):
            for s, fn in systems.items():
                t0 = time.time()
                ranked = fn(r[lang])
                timing[s] += time.time() - t0
                rk = _rank_of(ranked, r["rel"])
                results[s][lang].append(rk)
                resf[s][lang].append(_fam_rank(ranked, r["rel"], _family_of(fams, Path(r["rel"]).stem)))
                got[(s, lang)] = rk
            le, em = got[("lexical", lang)], got[("embed", lang)]
            hit = lambda x: x is not None and x <= 8
            if hit(em) and not hit(le) and len(examples["embed_wins"]) < 8:
                examples["embed_wins"].append((lang, r[lang], r["title"], le, em))
            if hit(le) and not hit(em) and len(examples["lexical_wins"]) < 8:
                examples["lexical_wins"].append((lang, r[lang], r["title"], le, em))

    def metrics(ranks):
        n = len(ranks)
        rec = lambda k: sum(1 for x in ranks if x is not None and x <= k) / n
        mrr = sum(1 / x for x in ranks if x is not None) / n
        return rec(1), rec(5), rec(8), mrr

    lines = []
    lines.append(f"# Retrieval benchmark — lexical vs embeddings\n")
    lines.append(f"*Run {time.strftime('%Y-%m-%d %H:%M')} · {len(rows)} notes × 2 languages · model `{MODEL}` · "
                 f"corpus {len(corpus)} files / {len(idx.rows)} chunks · queries are synthetic (see `tools/bench_retrieval.py`)"
                 + (f" · {gone} skipped, gold note gone" if gone else "") + "*\n")
    lines.append("| system | lang | R@1 | R@5 | R@8 | R@8† | MRR | ms/query |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in systems:
        for lang in ("en", "ru", "all"):
            ranks = results[s]["en"] + results[s]["ru"] if lang == "all" else results[s][lang]
            franks = resf[s]["en"] + resf[s]["ru"] if lang == "all" else resf[s][lang]
            r1, r5, r8, mrr = metrics(ranks)
            _, _, f8, _ = metrics(franks)
            ms = 1000 * timing[s] / (2 * len(rows))
            lines.append(f"| {s} | {lang} | {r1:.2f} | {r5:.2f} | {r8:.2f} | {f8:.2f} | {mrr:.2f} | {ms:.0f} |")
    lines.append("")
    lines.append("R@8 is what matters operationally: `ask.py` feeds the top 8 documents to the model. "
                 f"R@8† also counts a near-duplicate of the gold note as a hit ({n_fam} of {len(rows)} "
                 "benchmark notes have one, per [[Duplicate Candidates]]); it is the fairer number "
                 "while the duplicate families remain unmerged.")
    lines.append("")
    for key, label in (("embed_wins", "Embeddings found it, lexical did not (top-8)"),
                       ("lexical_wins", "Lexical found it, embeddings did not (top-8)")):
        lines.append(f"## {label}\n")
        for lang, q, title, le, em in examples[key]:
            lines.append(f"- ({lang}) *{q}* → [[{title}]] — lexical rank {le or '—'}, embed rank {em or '—'}")
        lines.append("")
    report = "\n".join(lines)
    print(report)
    REPORT.write_text("---\ntype: reference\ntags:\n  - retrieval\n  - benchmark\n  - palimpsest\n---\n" + report, encoding="utf-8")
    print(f"\n(written to {REPORT.relative_to(VAULT)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", type=int, default=0, help="generate N synthetic queries (appends)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if a.gen:
        gen(a.gen, a.seed)
    if a.run:
        run()
    if not a.gen and not a.run:
        ap.print_help()


if __name__ == "__main__":
    main()
