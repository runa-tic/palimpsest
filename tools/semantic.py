#!/usr/bin/env python3
"""Meaning-based similarity over the vault, on numpy alone.

Everything here compares notes by counting shared words. That fails in a way this vault has
measured twice: dedupe.py's own comment records a 16-note family all saying "use the first
trading date as a TGE proxy" scoring 42-46% — semantically identical, lexically different —
so no threshold on word overlap separates it from coincidence. Then triage_skills.py found
title+trigger overlap across 622 proposals with a median of 0.000, i.e. no signal at all.

Latent semantic analysis fixes the specific defect: TF-IDF then a truncated SVD, so documents
are compared in a space learned from how words CO-OCCUR across this corpus rather than whether
they literally match. "TGE proxy" and "first trading date" end up near each other because your
own notes use them in the same contexts. It is weaker than a trained embedding model, and it
needs nothing that is not already installed.

Doc-doc only. Projecting an arbitrary query into the space (for ask.py) needs the term-space
components, which build() returns but nothing uses yet.

  from semantic import Space
  sp = Space.build({name: text, ...})
  sp.similarity(a, b)        # 0..1
  sp.neighbours(a, n=5)      # [(name, score), ...]
"""
from __future__ import annotations
import re, math
import numpy as np

STOP = set("a an the of to in on for and or is are be not with from as at by it its this that "
           "than into you your our we my me but if then so can could should would will no "
           "have has had was were been do does did what how why when which who them they".split())
TOKEN = re.compile(r"[a-z0-9а-яё]+")


def tokens(s: str) -> list[str]:
    return [w for w in TOKEN.findall(s.lower()) if w not in STOP and len(w) > 2]


class Space:
    def __init__(self, names, emb, vocab, term_comp, idf):
        self.names = names
        self.index = {n: i for i, n in enumerate(names)}
        self.emb = emb                # (n_docs, k) L2-normalised
        self.vocab = vocab            # term -> column
        self.term_comp = term_comp    # (n_terms, k) for projecting new text
        self.idf = idf

    # ---- construction
    @classmethod
    def build(cls, docs: dict[str, str], k: int = 200, min_df: int = 2, max_df_ratio: float = 0.4):
        names = list(docs)
        n = len(names)
        if n < 3:
            raise ValueError("need at least 3 documents")
        toks = [tokens(docs[nm]) for nm in names]

        df: dict[str, int] = {}
        for t in toks:
            for w in set(t):
                df[w] = df.get(w, 0) + 1
        max_df = max(min_df, int(n * max_df_ratio))
        # Terms in almost every note carry no discriminating power and dominate the first
        # components; terms in one note cannot relate two notes at all. Both are dropped.
        vocab = {w: i for i, w in enumerate(sorted(v for v, c in df.items() if min_df <= c <= max_df))}
        if not vocab:
            raise ValueError("empty vocabulary")
        idf = np.zeros(len(vocab), dtype=np.float32)
        for w, i in vocab.items():
            idf[i] = math.log(n / df[w])

        X = np.zeros((n, len(vocab)), dtype=np.float32)
        for r, t in enumerate(toks):
            for w in t:
                j = vocab.get(w)
                if j is not None:
                    X[r, j] += 1.0
        np.log1p(X, out=X)            # sublinear tf: the 40th mention is not 40x the evidence
        X *= idf
        norms = np.linalg.norm(X, axis=1, keepdims=True)
        np.divide(X, np.maximum(norms, 1e-9), out=X)

        # Truncated SVD via the doc-doc Gram matrix: n is ~10^3 while the vocabulary is ~10^4,
        # so eigendecomposing (n x n) is far cheaper than factorising X directly, and gives the
        # same left singular vectors.
        G = X @ X.T
        w_eig, V = np.linalg.eigh(G)
        keep = min(k, n - 1)
        w_eig, V = w_eig[::-1][:keep], V[:, ::-1][:, :keep]
        w_eig = np.maximum(w_eig, 0)
        sv = np.sqrt(w_eig)
        emb = V * sv                                        # (n, k)
        emb /= np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9)
        with np.errstate(divide="ignore", invalid="ignore"):
            term_comp = np.nan_to_num(X.T @ V / np.maximum(sv, 1e-9))
        return cls(names, emb.astype(np.float32), vocab, term_comp.astype(np.float32), idf)

    # ---- queries
    def similarity(self, a: str, b: str) -> float:
        return float(self.emb[self.index[a]] @ self.emb[self.index[b]])

    def neighbours(self, a: str, n: int = 5) -> list[tuple[str, float]]:
        sims = self.emb @ self.emb[self.index[a]]
        order = np.argsort(-sims)
        return [(self.names[i], float(sims[i])) for i in order if self.names[i] != a][:n]

    def matrix(self) -> np.ndarray:
        return self.emb @ self.emb.T

    def project(self, text: str) -> np.ndarray:
        """Embed arbitrary text into the learned space (for query-side retrieval)."""
        v = np.zeros(len(self.vocab), dtype=np.float32)
        for w in tokens(text):
            j = self.vocab.get(w)
            if j is not None:
                v[j] += 1.0
        np.log1p(v, out=v)
        v *= self.idf
        nv = np.linalg.norm(v)
        if nv > 0:
            v /= nv
        q = v @ self.term_comp
        nq = np.linalg.norm(q)
        return q / nq if nq > 0 else q
