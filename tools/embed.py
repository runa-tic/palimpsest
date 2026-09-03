#!/usr/bin/env python3
"""Dense retrieval over the vault with a local embedding model, cached on disk.

Why this exists: ask.py ranks by keyword overlap and semantic.py by a TF-IDF+SVD space learned
from this corpus alone. Neither knows that a Russian question and an English note mean the same
thing, or that "first trading date" is a "TGE proxy" unless the vault already says both in one
place. A trained multilingual sentence encoder does, runs on CPU in minutes for a corpus this
size, and no text leaves the box.

Model: intfloat/multilingual-e5-small (384-d, 118M params, 100+ languages). e5 expects the
"query: " / "passage: " prefixes; dropping them costs measurable accuracy.

Index: brute-force cosine over an in-memory float32 matrix. At ~25k chunks x 384 dims a query
is one matmul, well under a millisecond. An ANN index or a vector database buys nothing below
a few hundred thousand vectors and would add a dependency plus a consistency problem.

Cache: tools/cache/embed-<model>/ holds vectors.npy (float16) + manifest.json keyed by file
path and content hash; reopening embeds only files whose bytes changed.

  from embed import Index
  idx = Index.open(files)                 # load cache, embed new/changed, save
  idx.search("how do I ...", top=8)       # [Hit(path, score, start, end)]
  idx.doc_scores("...")                   # {relpath: (best-chunk cosine, start, end)}
"""
from __future__ import annotations
import sys, os, re, json, hashlib, time, math
from dataclasses import dataclass
from pathlib import Path
import numpy as np

VAULT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("BRAIN_EMBED_MODEL", "intfloat/multilingual-e5-small")
CACHE_ROOT = VAULT / "tools" / "cache"
# ~1100 chars is ~280 English tokens and ~450 Russian ones for this tokenizer: inside the
# model's 512-token window either way, with room for the title prefix.
CHUNK_CHARS = 1100
CHUNK_OVERLAP = 150
BATCH = 64
# Save the index every this many freshly embedded chunks. The 2026-09-02 build was killed with
# the session at 15,872/36,446 chunks and lost all of it because nothing was written until the
# end; a checkpointed build resumes from the last save.
CHECKPOINT = 2048
# Length penalty on the per-document score: best chunk cosine minus LEN_BETA * ln(chunks).
# Max over chunks hands a 1,000-chunk transcript a thousand draws at the noise ceiling while an
# atomic note gets one. Unpenalised (2026-09-03 benchmark, 150 notes x en/ru), 230 of 320 top-8
# slots went to raw transcripts and the notes distilled from them ranked ~200th; R@8 on English
# went 0.23 -> 0.48 from this term alone. 0.005/0.01/0.02 all help; 0.01 is the knee.
LEN_BETA = 0.01
FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def strip_frontmatter(text: str) -> str:
    return FRONTMATTER.sub("", text, count=1)


def chunk_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of overlapping windows, ends snapped to a paragraph/line/word break."""
    n = len(text)
    if n == 0:
        return []
    spans, i = [], 0
    while i < n:
        j = min(n, i + CHUNK_CHARS)
        if j < n:
            lo = i + int(CHUNK_CHARS * 0.6)
            for sep in ("\n\n", "\n", " "):
                k = text.rfind(sep, lo, j)
                if k != -1:
                    j = k
                    break
        if text[i:j].strip():
            spans.append((i, j))
        if j >= n:
            break
        i = max(j - CHUNK_OVERLAP, i + 1)
    return spans


_model = None


def model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        import torch
        # torch defaults to physical cores (6 here); logical cores measured 20.6 vs 15.7 chunks/s.
        torch.set_num_threads(os.cpu_count() or torch.get_num_threads())
        _model = SentenceTransformer(MODEL, device="cpu")
        _model.max_seq_length = 512
    return _model


def embed_texts(texts: list[str], prefix: str) -> np.ndarray:
    if not texts:
        return np.zeros((0, model().get_sentence_embedding_dimension()), dtype=np.float32)
    return model().encode([prefix + t for t in texts], batch_size=BATCH, normalize_embeddings=True,
                          convert_to_numpy=True, show_progress_bar=False).astype(np.float32)


@dataclass
class Hit:
    path: Path
    score: float
    start: int   # best chunk span in the frontmatter-stripped text
    end: int


class Index:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.files: dict[str, dict] = {}              # relpath -> {"sha", "spans": [[s,e],...]}
        self.rows: list[tuple[str, int, int]] = []    # per vector row: (relpath, start, end)
        self.vec = np.zeros((0, 0), dtype=np.float32)

    # ---- persistence
    @classmethod
    def open(cls, files: list[Path], progress: bool = True) -> "Index":
        idx = cls(CACHE_ROOT / f"embed-{_slug(MODEL)}")
        idx._load()
        idx._sync(files, progress)
        return idx

    def _load(self):
        man = self.cache_dir / "manifest.json"
        vec = self.cache_dir / "vectors.npy"
        if not (man.exists() and vec.exists()):
            return
        try:
            m = json.loads(man.read_text(encoding="utf-8"))
            if m.get("model") != MODEL or m.get("chunk_chars") != CHUNK_CHARS:
                return
            v = np.load(vec).astype(np.float32)
            rows = [(r, s, e) for r, s, e in m["rows"]]
            if len(rows) != v.shape[0]:
                return
            self.files, self.rows, self.vec = m["files"], rows, v
        except Exception as e:  # a corrupt cache is a rebuild, never a crash
            print(f"embed: cache unreadable ({type(e).__name__}: {e}); rebuilding", file=sys.stderr)

    def _save(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_v = self.cache_dir / "vectors.npy.tmp"
        tmp_m = self.cache_dir / "manifest.json.tmp"
        # np.save appends .npy to any PATH that lacks it (so 'vectors.npy.tmp' became
        # 'vectors.npy.tmp.npy' and the rename below failed); a file handle is written as-is.
        with open(tmp_v, "wb") as fh:
            np.save(fh, self.vec.astype(np.float16))
        tmp_m.write_text(json.dumps({"model": MODEL, "chunk_chars": CHUNK_CHARS,
                                     "built": time.strftime("%Y-%m-%d %H:%M"),
                                     "files": self.files, "rows": self.rows}, ensure_ascii=False),
                         encoding="utf-8")
        os.replace(tmp_v, self.cache_dir / "vectors.npy")
        os.replace(tmp_m, self.cache_dir / "manifest.json")

    # ---- incremental build
    @staticmethod
    def _rel(p: Path) -> str:
        return p.resolve().relative_to(VAULT).as_posix()

    def _sync(self, files: list[Path], progress: bool):
        wanted: dict[str, tuple[Path, str, str]] = {}   # rel -> (path, sha, text)
        for p in files:
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            wanted[self._rel(p)] = (p, hashlib.sha1(raw).hexdigest(),
                                    strip_frontmatter(raw.decode("utf-8", "ignore")))

        stale = [r for r in self.files if r not in wanted or self.files[r]["sha"] != wanted[r][1]]
        fresh = [r for r in wanted if r not in self.files or self.files[r]["sha"] != wanted[r][1]]
        if not stale and not fresh:
            return

        # keep vectors of unchanged files (row order == self.rows order)
        stale_set = set(stale)
        if self.rows:
            keep_mask = np.array([r not in stale_set for r, _, _ in self.rows], dtype=bool)
            rows = [row for row, k in zip(self.rows, keep_mask) if k]
            vec = self.vec[keep_mask]
        else:
            rows, vec = [], self.vec
        for r in stale:
            self.files.pop(r, None)

        # Embed whole files in batches of ~BATCH*8 chunks and checkpoint every CHECKPOINT chunks.
        # A file enters self.files only once every one of its chunks is in self.vec, so any
        # checkpoint is internally consistent and a later open() resumes with the rest.
        self.rows, self.vec = rows, vec
        plan = [(r, chunk_spans(wanted[r][2])) for r in fresh]
        total = sum(len(s) for _, s in plan)
        if progress and total:
            print(f"embed: {len(fresh)} file(s) changed -> {total} chunks with {MODEL}", file=sys.stderr)
        t0, done, since_save, step = time.time(), 0, 0, BATCH * 8
        batch: list[tuple[str, dict, list[str], list[tuple[str, int, int]]]] = []
        n_batch = 0
        for i, (r, spans) in enumerate(plan):
            p, sha, text = wanted[r]
            batch.append((r, {"sha": sha, "spans": [list(s) for s in spans]},
                          [f"{p.stem}\n{text[s:e]}" for s, e in spans],
                          [(r, s, e) for s, e in spans]))
            n_batch += len(spans)
            if n_batch < step and i < len(plan) - 1:
                continue
            texts = [t for _, _, ts, _ in batch for t in ts]
            if texts:
                new_vec = embed_texts(texts, "passage: ")
                self.vec = np.concatenate([self.vec, new_vec]) if self.vec.size else new_vec
            for r2, entry, _, rws in batch:
                self.files[r2] = entry
                self.rows.extend(rws)
            done += n_batch
            since_save += n_batch
            batch, n_batch = [], 0
            if progress and total > step:
                rate = done / max(time.time() - t0, 1e-6)
                left = (total - done) / max(rate, 1e-6)
                print(f"embed: {done}/{total} chunks ({rate:.0f}/s, ~{left:.0f}s left)", file=sys.stderr)
            if since_save >= CHECKPOINT:
                self._save()
                since_save = 0
        if progress and total:
            print(f"embed: done in {time.time() - t0:.0f}s", file=sys.stderr)
        self._save()

    # ---- queries
    def query_vector(self, q: str) -> np.ndarray:
        return embed_texts([q], "query: ")[0]

    def doc_scores(self, q: str) -> dict[str, tuple[float, int, int]]:
        """Per document: best-chunk cosine minus LEN_BETA * ln(chunk count), with the best chunk's
        span. See LEN_BETA for why the penalty exists."""
        if not len(self.rows):
            return {}
        sims = self.vec @ self.query_vector(q)
        best: dict[str, tuple[float, int, int]] = {}
        for (r, s, e), sc in zip(self.rows, sims.tolist()):
            if r not in best or sc > best[r][0]:
                best[r] = (sc, s, e)
        return {r: (sc - LEN_BETA * math.log(max(1, len(self.files[r]["spans"]))), s, e)
                for r, (sc, s, e) in best.items()}

    def search(self, q: str, top: int = 8) -> list[Hit]:
        best = self.doc_scores(q)
        order = sorted(best.items(), key=lambda kv: -kv[1][0])[:top]
        return [Hit(VAULT / r, sc, s, e) for r, (sc, s, e) in order]


if __name__ == "__main__":
    # `python tools/embed.py [query]` builds/refreshes the index and optionally searches.
    try:
        sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from ask import gather
    idx = Index.open(gather())
    dim = idx.vec.shape[1] if idx.vec.size else 0
    print(f"index: {len(idx.files)} files, {len(idx.rows)} chunks, dim {dim}")
    if len(sys.argv) > 1:
        for h in idx.search(" ".join(sys.argv[1:]), top=10):
            print(f"{h.score:.3f}  {h.path.relative_to(VAULT)}")
