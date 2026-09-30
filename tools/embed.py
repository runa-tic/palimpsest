#!/usr/bin/env python3
"""Dense retrieval over the vault with a local embedding model, cached on disk.

Why this exists: ask.py ranks by keyword overlap and semantic.py by a TF-IDF+SVD space learned
from this corpus alone. Neither knows that a Russian question and an English note mean the same
thing, or that "first trading date" is a "TGE proxy" unless the vault already says both in one
place. A trained multilingual sentence encoder does, runs on CPU in minutes for a corpus this
size, and no text leaves the box.

Model: intfloat/multilingual-e5-base (768-d, 278M params, 100+ languages), with
intfloat/multilingual-e5-small (384-d, 118M) as the fallback until the base index is built — see
pick_model. e5 expects the "query: " / "passage: " prefixes; dropping them costs measurable accuracy.

Index: brute-force cosine over an in-memory float32 matrix. At ~25k chunks x 384 dims a query
is one matmul, well under a millisecond. An ANN index or a vector database buys nothing below
a few hundred thousand vectors and would add a dependency plus a consistency problem.

Cache: tools/cache/embed-<model>/ holds vectors.npy (float16) + manifest.json keyed by file
path and content hash; reopening embeds only files whose bytes changed.

Writers: one at a time per cache dir (see _WriterLock). The nightly sync runs `embed.py` as a
step so the first query of the day finds the index current; an interactive query that opens the
index at the same moment waits for it instead of racing it.

  from embed import Index
  idx = Index.open(files)                 # load cache, embed new/changed, save
  idx.search("how do I ...", top=8)       # [Hit(path, score, start, end)]
  idx.doc_scores("...")                   # {relpath: (best-chunk cosine, start, end)}
"""
from __future__ import annotations
import sys, os, re, json, hashlib, time, math, zlib, importlib.util
from dataclasses import dataclass
from pathlib import Path
import numpy as np

VAULT = Path(__file__).resolve().parent.parent
# e5-base replaced e5-small as the default on a full-vault benchmark (5,286 files, 300 EN/RU
# queries, rerank at 100): R@8 0.59 -> 0.71, Russian 0.45 -> 0.63, gold note inside the
# reranker's candidates 0.64 -> 0.89 for Russian. Its index takes far longer to build, and
# Index.open builds whatever is missing before answering, so a machine that has not built it
# yet keeps answering with FALLBACK (see pick_model) instead of stalling on the first question.
PREFERRED = "intfloat/multilingual-e5-base"
FALLBACK = "intfloat/multilingual-e5-small"
PINNED = os.environ.get("BRAIN_EMBED_MODEL")      # an explicit choice is never second-guessed
MODEL = PINNED or PREFERRED
READY = 0.95   # share of the vault's files the preferred index must already cover
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
LOCK_WAIT = 120     # seconds a second writer waits before proceeding anyway
LOCK_STALE = 3600   # a lock file older than this belongs to a process that died holding it


class _WriterLock:
    """One writer per cache dir. Two processes that opened the index together (the nightly step
    and an interactive query, or two queries) both saved; the atomic renames kept the files
    consistent, but the last writer dropped the other's new rows until the next open re-embedded
    them. The lock is a file created with O_EXCL, which is portable and crash-tolerant: a lock
    older than LOCK_STALE is treated as abandoned, and a waiter gives up after LOCK_WAIT and
    proceeds anyway, which is only the old behaviour and never a hang. The holder refreshes the
    lock's mtime at every checkpoint (refresh), so an hours-long first build never reads as
    abandoned; mtime is the liveness signal because a pid check is not portable (os.kill on
    Windows terminates the process)."""

    def __init__(self, cache_dir: Path):
        self.path = cache_dir / ".lock"
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        t0, told = time.time(), False
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue                      # it vanished between the two calls; retry
                if age > LOCK_STALE:
                    try:
                        self.path.unlink()
                    except OSError:
                        # Not removable (Windows refuses while a live holder keeps it open): wait it
                        # out like a live lock. Retrying at once skipped LOCK_WAIT and the sleep, and
                        # spun a core forever.
                        pass
                    else:
                        continue
                if time.time() - t0 > LOCK_WAIT:
                    print(f"embed: another writer has held the index for {age:.0f}s; proceeding without the lock",
                          file=sys.stderr)
                    return self
                if not told:
                    print("embed: another process is updating the index; waiting", file=sys.stderr)
                    told = True
                time.sleep(1)

    def refresh(self):
        if self.fd is not None:
            try:                                  # by fd where possible: the path may not be ours
                os.utime(self.fd if os.utime in os.supports_fd else self.path)
            except OSError:
                pass

    def __exit__(self, *_):
        if self.fd is not None:                   # never remove a lock we did not take...
            try:                                  # ...nor one another writer took over since
                mine = os.path.samestat(os.fstat(self.fd), os.stat(self.path))
            except OSError:
                mine = False
            os.close(self.fd)
            self.fd = None
            if mine:
                try:
                    self.path.unlink()
                except OSError:
                    pass
FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def coverage(model_name: str, files: list[Path]) -> float:
    """Share of `files` already in `model_name`'s saved index. Reads the manifest only: no
    model load, no hashing (a changed file still counts; Index.open re-embeds that delta)."""
    try:
        m = json.loads((CACHE_ROOT / f"embed-{_slug(model_name)}" / "manifest.json").read_text(encoding="utf-8"))
        if m.get("model") != model_name or m.get("chunk_chars") != CHUNK_CHARS:
            return 0.0
        have = m.get("files", {})
    except Exception:
        return 0.0
    if not files:
        return 1.0
    return sum(1 for p in files if Index._rel(p) in have) / len(files)


def use_model(name: str):
    global MODEL, _model
    if name != MODEL:
        MODEL, _model = name, None


def pick_model(files: list[Path]) -> str | None:
    """Choose the model an interactive query should use; returns a hint line when it falls back.
    `python tools/embed.py` (and the nightly embed step) always builds PREFERRED, checkpointed,
    so a time-budgeted nightly run finishes it over a few nights; queries switch over by
    themselves once it covers READY of the vault."""
    if PINNED:
        use_model(PINNED)
        return None
    c = coverage(PREFERRED, files)
    if c >= READY:
        use_model(PREFERRED)
        return None
    use_model(FALLBACK)
    return (f"embed: the {PREFERRED.split('/')[-1]} index covers {c:.0%} of the vault; answering with "
            f"{FALLBACK.split('/')[-1]}. Build it with `python tools/embed.py` (resumable).")


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
        self._lock: _WriterLock | None = None         # held while _sync writes

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
            v16 = np.load(vec)
            rows = [(r, s, e) for r, s, e in m["rows"]]
            # The pair is published by two renames, so writers that raced without the lock (see
            # _WriterLock) can leave one's vectors beside the other's manifest. The row count alone
            # let an equal-length pair load misaligned; the checksum catches it (older manifests
            # have none and keep the count check).
            crc = m.get("vectors_crc")
            if len(rows) != v16.shape[0] or (crc is not None and crc != zlib.crc32(np.ascontiguousarray(v16))):
                print("embed: vectors.npy does not match manifest.json (two writers saved at once); rebuilding",
                      file=sys.stderr)
                return
            self.files, self.rows, self.vec = m["files"], rows, v16.astype(np.float32)
        except Exception as e:  # a corrupt cache is a rebuild, never a crash
            print(f"embed: cache unreadable ({type(e).__name__}: {e}); rebuilding", file=sys.stderr)

    def _save(self):
        if self._lock:
            self._lock.refresh()                      # a checkpoint is proof of life
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Per-process temp names: a writer that proceeded without the lock shared the fixed ones
        # with the holder, renamed its half-written file away, and one of them crashed in os.replace.
        tmp_v = self.cache_dir / f"vectors.npy.{os.getpid()}.tmp"
        tmp_m = self.cache_dir / f"manifest.json.{os.getpid()}.tmp"
        v16 = np.ascontiguousarray(self.vec.astype(np.float16))
        # np.save appends .npy to any PATH that lacks it (so 'vectors.npy.tmp' became
        # 'vectors.npy.tmp.npy' and the rename below failed); a file handle is written as-is.
        with open(tmp_v, "wb") as fh:
            np.save(fh, v16)
        tmp_m.write_text(json.dumps({"model": MODEL, "chunk_chars": CHUNK_CHARS,
                                     "built": time.strftime("%Y-%m-%d %H:%M"),
                                     "vectors_crc": zlib.crc32(v16),
                                     "files": self.files, "rows": self.rows}, ensure_ascii=False),
                         encoding="utf-8")
        os.replace(tmp_v, self.cache_dir / "vectors.npy")
        os.replace(tmp_m, self.cache_dir / "manifest.json")

    # ---- incremental build
    @staticmethod
    def _rel(p: Path) -> str | None:
        try:
            return p.resolve().relative_to(VAULT).as_posix()
        except ValueError:       # a symlink out of the vault (ask.gather drops those): not indexed
            return None

    def _sync(self, files: list[Path], progress: bool):
        wanted: dict[str, tuple[Path, str, str]] = {}   # rel -> (path, sha, text)
        for p in files:
            rel = self._rel(p)
            if rel is None:
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            wanted[rel] = (p, hashlib.sha1(raw).hexdigest(),
                           strip_frontmatter(raw.decode("utf-8", "ignore")))

        if not any(self._delta(wanted)):
            return
        with _WriterLock(self.cache_dir) as self._lock:
            # Another writer may have saved while we waited: reload and recompute against the
            # files on disk so its rows are kept rather than overwritten.
            self._load()
            stale, fresh = self._delta(wanted)
            if not stale and not fresh:
                return
            self._apply(wanted, stale, fresh, progress)

    def _delta(self, wanted: dict) -> tuple[list[str], list[str]]:
        stale = [r for r in self.files if r not in wanted or self.files[r]["sha"] != wanted[r][1]]
        fresh = [r for r in wanted if r not in self.files or self.files[r]["sha"] != wanted[r][1]]
        return stale, fresh

    def _apply(self, wanted: dict, stale: list[str], fresh: list[str], progress: bool):
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
    if importlib.util.find_spec("sentence_transformers") is None:
        # The nightly sync runs this unconditionally; without the optional package it is a no-op,
        # not a failure, so the sync stays green on a vault that never opted in.
        print("embed: sentence-transformers is not installed; nothing to build "
              "(opt in with `pip install sentence-transformers`)")
        sys.exit(0)
    from ask import gather
    idx = Index.open(gather())
    dim = idx.vec.shape[1] if idx.vec.size else 0
    print(f"index: {len(idx.files)} files, {len(idx.rows)} chunks, dim {dim}")
    if len(sys.argv) > 1:
        for h in idx.search(" ".join(sys.argv[1:]), top=10):
            print(f"{h.score:.3f}  {h.path.relative_to(VAULT)}")
