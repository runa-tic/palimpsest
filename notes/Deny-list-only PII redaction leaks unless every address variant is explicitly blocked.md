---
type: note
created: 2026-06-12
tags:
  - claude/extracted
  - security
  - pii
  - redaction
---

# Deny-list-only PII redaction leaks unless every address variant is explicitly blocked

A vault transcript redaction system using only deny-lists leaked PII into committed, pushed git history because email address variants (outlook, gmail) were not all listed. Even though some variants were redacted, others slipped through. Deny-list-only approaches are inherently incomplete; systems redacting PII should either use allowlists (redact everything except known-safe tokens) or maintain comprehensive, documented deny-lists with a tracking mechanism for what variants have leaked.

## Related
- 🗺️ Software Architecture & API Design MOC
- Deny-list redaction avoids over-scrubbing when legitimate and sensitive data overlap in sh
- Vault transcript redaction is deny-list-only, so emails leak unless explicitly denied
- Apply output scrubbing at the single write choke point for automatic, idempotent redaction
- Test infrastructure separately from auth—user provides credentials only when needed

## Source
