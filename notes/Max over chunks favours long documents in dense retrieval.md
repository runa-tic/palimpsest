---
type: note
created: 2026-09-03
tags:
  - retrieval
  - embeddings
  - evaluation
---

# Max over chunks favours long documents in dense retrieval

Scoring a document by its best chunk's cosine gives a 1,000-chunk transcript a thousand draws at the noise ceiling and a one-paragraph note a single draw. When cosines sit in a compressed band, the long file wins on luck. Subtract a term in ln(chunks) before ranking.

The first embedding benchmark on this vault (150 atomic notes, one English and one Russian question each, `multilingual-e5-small`) scored embeddings *below* plain keyword search on English: recall@8 of 0.23 against 0.31. The model was fine — a Russian question scored its English answer at 0.84 cosine against 0.75 for unrelated text — the ranking was the problem. The corpus median is one chunk per document, but the documents filling the top 8 had a median of 236 chunks, and 230 of 320 top-8 slots went to raw conversation transcripts. The atomic note distilled from a transcript ranked around 200th behind the transcript itself, because somewhere in a thousand chunks one happened to sit at 0.90 while the note's single chunk sat at 0.87.

This is the expected-maximum effect: the max of n draws from the same noise grows roughly with the square root of ln n. The crudest correction, `score = best cosine − β·ln(chunks)`, took English recall@8 from 0.23 to 0.48 at β = 0.01; 0.005 and 0.02 helped nearly as much, so the knee is broad. Applying the vault's directory prior on the floor-shifted score, `(score − 0.70) × prior`, reached 0.55 English and 0.25 Russian. Applying the same prior to the raw cosine had been rejected earlier because a 0.5–1.6 multiplier on values in 0.75–0.90 swamps the signal; on the shifted score it is the same nudge it is in the keyword lane.

The general rule: any best-chunk aggregation over documents of very different lengths needs a length term, and a corpus that mixes single-paragraph notes with megabyte transcripts is the worst case. Check the chunk-count distribution of the top k against the corpus before trusting a dense-retrieval number.

## Related
- Rank fusion with a noise list is worse than the good list alone
- Extraction quality collapses under naive volume
- Deduplication as flagging preserves judgment calls
