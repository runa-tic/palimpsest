---
type: note
created: 2026-06-07
tags:
  - claude/extracted
  - security
  - automation
  - design
---

# Two-tier secret detection avoids alert fatigue while catching real credentials

High-confidence patterns (sk-*, ghp_*, bot tokens, key-like strings) should block commits; lower-confidence matches (EVM addresses, placeholder API IDs, redaction markers) should warn only. An allowlist exempts known-public strings by exact match, preventing false alarms without relaxing the gate on actual secrets.

## Related
- 🗺️ Security, Secrets & Safety MOC
- Secret detection combines HIGH severity (blocks), WARN severity (reports only), and allowl
- Allowlist known-public matches in automated secret guards
- Auto-saved LLM transcripts bypass content review and leak secrets into tracked files
- Default configuration should be secure; let users opt into less-secure options
- Message-based command channels require capability-gating through allowlists

## Source
