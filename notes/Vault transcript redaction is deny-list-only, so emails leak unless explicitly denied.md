---
type: note
created: 2026-06-14
tags:
  - operations
  - security
  - privacy
  - vault-tooling
---
# Vault transcript redaction is deny-list-only, so emails leak unless explicitly denied

> The Stop-hook recorder (`hook_record.py` → `import_claude.process_transcript` → `redact.redact_text`) always masks credential *shapes* (API keys, tokens, private keys via `scan_secrets.HIGH`), but for PII like emails it is **deny-list-only**: an address passes through verbatim unless it is listed in `_tools/.redact_terms.txt`. By design — blanket email/number redaction would "shred real content."

## Elaboration

Consequence chain that makes this a real exposure, not a theoretical one:
1. The vault saves the transcript **after every assistant turn** (not at session end), so anything said mid-session is on disk immediately.
2. `write_note` is **content-stable** (`import_claude.py:122` — skips rewriting an unchanged note), so adding a term to the deny list scrubs *future* writes but does **not** retroactively clean already-written notes. Existing leaks must be scrubbed by hand.
3. Conversation notes are **committed and pushed** (`40 Resources/Claude Conversations/`, a private GitHub remote), so a leaked address is also in **git history** — remediation needs a history rewrite (filter-repo/BFG) + force-push, not just a working-tree edit.

Verified 2026-06-14 (Codex-coexistence workflow): the user's main Outlook address sat un-redacted in two committed conversation notes since the initial commit, because it was never on the deny list — while previously-denied Gmail addresses were correctly masked. Note the on-disk occurrences were *historical git commit-author content* discussed in old sessions, distinct from the ChatGPT/Codex-login email.

## Why it matters / how I'll use it

To actually remove a PII string from the brain: (a) add it to `_tools/.redact_terms.txt` (stops future leaks), (b) manually scrub the existing affected notes (content-stable writes won't), (c) rewrite git history + force-push if the vault is pushed. The secret-scan pre-commit guard won't catch plain emails — it's tuned for credential shapes.

## Related
- 🗺️ Operations & Reliability MOC
- Allowlist known-public matches in automated secret guards
- Auto-saved LLM transcripts bypass content review and leak secrets into tracked files
- A frontier model your stack depends on can be export-controlled overnight

## Source
- Codex/vault coexistence workflow, 2026-06-14; `_tools/redact.py`, `import_claude.py`, `.redact_terms.txt`.
