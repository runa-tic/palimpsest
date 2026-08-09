---
type: note
created: 2026-07-20
tags: [vault, workflow, tooling]
---
# Daily-note briefing blocks are regenerated output, so durable edits go in the roll source

> Everything between `<!-- briefing:start -->` and `<!-- briefing:end -->` in a daily note is
> rebuilt from scratch on every `briefing.py` run — edits made inside the block (check-offs,
> appended text) are silently clobbered by the next run.

## Elaboration

`briefing.py` rolls unchecked `- [ ]` tasks from the **most recent previous** daily note
(`recent_daily(before=today)` → `open_tasks`) and regenerates today's whole block. On
2026-07-20 two task check-offs and an appended continuity prompt made inside today's block
were wiped by an afternoon briefing re-run that re-rolled the tasks from yesterday's
unchecked copies.

To make a change stick, apply it where the roll reads from: check off / edit the task in the
**previous day's** note (kills it for any re-run today), and — if the current day's block has
already been generated — mirror the edit there too, since tomorrow rolls from today. Content
outside the markers (the `## 📝 Log` section) is never touched by the generator and is always
safe.

## Why it matters / how I'll use it

Before checking off a rolled-over task or enriching its text, edit the previous daily note
first, then today's. Never park durable content only inside the briefing markers.

## Related
- Knowledge Management MOC

## Source
- 2026-07-20 session: briefing re-run clobbered two task closures + a continuity prompt; `_tools/briefing.py` (`recent_daily`, `open_tasks`, `build_block`).
