---
type: note
created: 2026-09-29
tags:
  - sync
  - git
  - multi-machine
---

# A union merge keeps both copies when two machines render the same block

`merge=union` makes git keep every line from both sides instead of stopping on a conflict. That is exactly right for an append-only log whose readers do not care about order, and exactly wrong for any file two machines *regenerate*. There, it converts a loud conflict into a silent duplicate.

The vault this came from is written from two machines. Daily notes kept stopping rebases on add/add conflicts, so `Daily/*.md` was switched to `merge=union`. Nine days later one day's note carried two complete briefing blocks. The chain: the machine whose scheduled run started with a failed pull did not have the other machine's copy of today's note, so it created the day from scratch; the push-time rebase then union-merged its render with the other machine's. Union can only collapse lines that are identical, and the two renders listed projects in each filesystem's glob order (APFS on one machine, NTFS on the other), so every line differed and both blocks survived. Everything that was sorted or constant in the block did collapse, which made the doubling look partial and harder to spot.

Three changes closed it, and each alone would not have:

1. **Pull first, and skip the render if the pull failed.** The sync's first step rebases onto the remote; if that fails, the briefing step is skipped and recorded as failed. The session-start hook does the same and says "briefing not refreshed" instead of silently rendering on a stale base.
2. **Make every render byte-identical.** Sort anything that comes from a directory listing. Two machines producing the same content must produce the same bytes.
3. **Union only what is truly append-only.** Palimpsest ships `merge=union` for the State ledger (`State/facts.jsonl`, whose fold ignores line order), and deliberately *not* for `Daily/`. With the pull guard in place a daily-note conflict should not happen; if it does, a stopped rebase is the signal a human should see.

The general rule: a merge driver that never fails also never tells you it merged two different things. Choose it per file, by asking whether two different versions of that file can both be right at once.

## Related
- State belongs in dated facts with a deterministic fold, not in prose
- Daily-note briefing blocks are regenerated output, so durable edits go in the roll source
