---
type: note
created: 2026-06-01
tags:
  - claude/extracted
  - knowledge-management
  - extraction
  - quality
---

# Extraction quality collapses under naive volume

Bulk extraction over 23 repo docs produced 94 notes, 90% of which were generic best-practices and duplicated boilerplate. Tightening the extraction prompt to explicitly reject non-obvious ideas and capping output hard (≤2 per doc) with human-in-the-loop flagging reduced it to 8 sharp notes. Naive extraction scales; signal-to-noise degrades catastrophically. Always extract with a strict bar: reject generic, unknown duplicates, hard caps.

## Related
- 🗺️ Knowledge Management MOC
- Deduplication as flagging preserves judgment calls
- Two-tier knowledge capture separates cheap recording from expensive extraction
- Hub notes + separate cataloging avoid vault bloat
- Structure should emerge from usage, not precede it
- Calibrate sell-pressure percentage empirically from historical spike volume rather than as

## Source

> [!abstract]- What the 2 merged version(s) added
> Consolidated 2026-07-31. Only lines carrying something the note above did not
> already say are kept, verbatim. Full originals in git at `fdd8fee`.
>
> - Pointing an extractor at 23 READMEs yielded 94 notes, mostly generic best-practice boilerplate ("modular architecture reduces coupling") and heavy duplication across repos.
> - Solution: tighten the extraction prompt to reject platitudes, cap output hard (≤2 notes per document), and validate the first batch before running at scale.
> - When in doubt, re-extract with a stronger model.
> - Extracting atomic notes from README files and documentation over-produces generic engineering best-practices ('modular pipelines reduce coupling', 'validate strategies historically') that bury signal.
> - Conversation-distilled insights are 3–5x sharper.
> - When scaling doc extraction, use tighter prompts (reject non-obviousness, hard caps like 1–2 insights/doc max), and feed output through duplicate detection to keep the brain focused on genuine discoveries rather than reformatted boilerplate.
