---
type: note
created: 2026-09-03
tags:
  - retrieval
  - embeddings
  - evaluation
---

# Rank fusion with a noise list is worse than the good list alone

Reciprocal rank fusion gives every input list an equal vote. When one list is noise, the fused result drops below the good list on its own. Weight lists by how much you trust them, which is a different question from how their scores compare.

Hybrid retrieval in `ask.py` fuses a keyword ranking and an embedding ranking with reciprocal rank fusion, chosen because it needs no calibration between a term-count score and a cosine. On the 2026-09-03 benchmark, equal-weight fusion scored recall@8 of 0.50 English and 0.15 Russian, while the embedding list alone scored 0.55 and 0.25. Fusion lost on both languages, and badly on Russian. The reason is that a Russian question against mostly English notes produces a keyword list with no real signal — the median top lexical score was 0.85 for Russian queries against 5.12 for English — and the fused list then spends half its top 8 on that lane's noise.

Weighting the keyword list at 0.3 and the embedding list at 1.0 gave 0.58 English and 0.25 Russian, the best of nine rules tried, and the two halves of the query set agreed. Gates that tried to detect noise per query, by script or by the top keyword score, did no better than the constant weight. The keyword lane's remaining value is as a tie-breaker for exact identifiers — error strings, flag names, handles — which embeddings blur.

"Needs no calibration" is a claim about score scales, not about trust. Whenever two rankers are fused, ask how each behaves on the query distribution's hard cases and give the one that degrades to noise a smaller vote. The same applies to any ensemble where one member can go blind on a subset of inputs.

## Related
- Max over chunks favours long documents in dense retrieval
- Deduplication as flagging preserves judgment calls
