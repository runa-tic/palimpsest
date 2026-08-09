---
type: note
created: 2026-08-08
tags: [security, privacy, vault-tooling, methodology]
---
# A deny list that only runs in one writer is not a policy

> Redaction enforced inside one write path protects that path and nothing else. Every other way content reaches the repo — a hand-typed note, a script, a manual edit — walks straight past it, so the list describes an intention rather than a guarantee until it runs at a chokepoint all writers share.

## Elaboration

This vault's `_tools/.redact_terms.txt` is applied by `redact.redact_text`, which is called
from `import_claude.write_note` — the conversation recorder. That is one writer. Atomic
notes, project pages and daily entries are authored by hand and never touch it.

The gap surfaced on 2026-08-08: a service login address deny-listed weeks earlier was sitting in an
atomic note written on 08-04, four days *after* the whole PII remediation had been declared
complete and closed. Nothing failed. The deny list did exactly what it was wired to do; it
simply was not wired to the path that leaked. A second address in the same note had never
been listed at all, so even a correctly-wired recorder would have passed it through.

The fix is to move enforcement to the narrowest point every writer must cross. For a git
vault that is the commit, so `_tools/scan_pii.py` now runs in the pre-commit hook beside the
secret scanner, reusing the same deny-list parser. Two tiers, following
Two-tier secret detection avoids alert fatigue while catching real credentials: terms
that are unambiguously identifiers (anything with an `@`, or a 5+ character alphabetic term)
block the commit; short or numeric literals only warn, because the vault is full of numbers
and a guard that wedges the automated sync on a coincidence gets disabled within a week.

One discipline the pass depends on: never print the value while removing it. The Stop hook
records the session into the vault, so echoing an address during cleanup recreates the leak
in a fresh file — masked forms and counts only, and deny the term *before* touching the file
that holds it.

## Why it matters / how I'll use it

When a control is described by where it is configured rather than where it runs, ask which
writers actually execute it. A rule enforced in the tool you happened to build first is a
filter on one channel; a rule enforced at the commit, the API boundary or the queue insert
is a policy. The same question exposed the read-scope gap in
A sandbox that can call an LLM has egress, so read scope is send scope — enumerate every
path that crosses the boundary, not the one you were thinking about when you built it.

Corollary for closure discipline: the 08-01 remediation was genuinely complete, but it added
a new `- [x]` line instead of ticking the rolled-over `- [ ]` one, so `briefing.py` kept
surfacing a finished task for eight days. Close the item the tracker is actually reading.

## Related
- Vault transcript redaction is deny-list-only, so emails leak unless explicitly denied
- A sandbox that can call an LLM has egress, so read scope is send scope
- Deny-list-only PII redaction leaks unless every address variant is explicitly blocked

## Source
- 2026-08-08 session: `_tools/scan_pii.py`, `_tools/githooks/pre-commit`; closure of the
  PII task first completed in `57315aa` (2026-08-01).
