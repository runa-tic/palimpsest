---
type: note
created: 2026-09-29
tags:
  - retrieval
  - reranking
  - evaluation
---

# A cross-encoder rerank beat a full LLM memory server on this vault

Before adopting a heavier retrieval system, find out which of its parts carries the gain. Here it was the smallest one: a cross-encoder reranker. Bolted onto the existing hybrid search, it beat the whole system on everything but one English column, with no LLM calls and a tenth of the latency.

The heavier system was Hindsight, an open-source agent-memory server. Every stored note goes through LLM fact extraction; background LLM consolidation merges the facts into "observations"; and recall fuses semantic, keyword, graph and temporal search, then reranks with a cross-encoder. The test used 291 atomic notes (150 target notes plus their 141 near-duplicates) and 300 synthetic questions, 150 English and 150 Russian, with one correct note each. Hindsight ran locally with the same multilingual embedding model as `ask.py`. Ingest took 88 minutes of LLM extraction and consolidation another 43.

| system | R@1 en / ru | R@8 en / ru | MRR | ms/query |
|---|---|---|---|---|
| `ask.py` hybrid | 0.52 / 0.19 | 0.85 / 0.59 | 0.48 | 21 |
| Hindsight, default reranker | 0.67 / 0.02 | 0.91 / 0.12 | 0.41 | 954 |
| Hindsight, multilingual reranker | 0.59 / 0.44 | 0.74 / 0.63 | 0.58 | 2,194 |
| hybrid + language-routed cross-encoder | 0.76 / 0.57 | 0.90 / 0.82 | 0.73 | 231 |

Two findings came from looking *inside* the pipeline rather than at its output:

- **The default reranker made Russian unusable.** Per-stage scores for Russian questions showed Hindsight's semantic stage ranking the right note 1st to 6th, and the final order, which was exactly the English-only reranker's order, pushing it to 9th through 148th. A model that has never seen a language produces noise, and noise in the last stage overrides every stage before it.
- **The extraction added nothing here.** Hindsight's advantage over plain hybrid was concentrated at rank 1, which is the reranker's job. The same class of model on `ask.py`'s own top candidates beat it on every column but one: Hindsight's default configuration kept English top-8 by a hair, 0.91 against 0.90, while scoring 0.12 on Russian. Extracting facts from notes that were already distilled is a second distillation.

On the full 5,286-file vault the reranker's limit is the first stage: it can only reorder what hybrid returns. Reranking the top 20 / 50 / 100 / 150 / 200 gave R@8 0.46 / 0.55 / 0.59 / 0.62 / 0.63 (hybrid alone: 0.38), at 0.2 / 0.5 / 1.0 / 2.2 / 3.5 s on a laptop CPU. Past 100, R@1 and MRR stop moving and English R@8 barely does (0.73, 0.75, 0.75); only Russian keeps climbing, because its first stage is weaker, with the right note in hybrid's top 20 only 35% of the time. `ask.py` ships depth 100, routed by script: `ms-marco-MiniLM-L-6-v2` for English, `mmarco-mMiniLMv2-L12-H384-v1` for anything with Cyrillic.

Limits: synthetic questions written from the notes' own text, one run, and a comparison of retrieval only. Hindsight's temporal and graph recall were not exercised, and neither was its capture path.

The general rule: benchmark per stage, not per system. When a pipeline wins, find the stage that wins, and check whether it transplants.

## Related
- Rank fusion with a noise list is worse than the good list alone
- Max over chunks favours long documents in dense retrieval
