---
type: note
created: 2026-08-08
tags: [security, sandboxing, agents, llm-system-design]
---
# A sandbox that can call an LLM has egress, so read scope is send scope

> Blocking sockets does not contain a sandbox that is allowed to call a model, because the model call is itself an outbound channel. Whatever the sandboxed code can read, it can pass as a prompt and ship off the machine — so read confinement, not network confinement, is what actually bounds the blast radius.

## Elaboration

Built `_tools/rlm.py` on 2026-08-08 with what looked like a sound boundary: a Python audit
hook blocking `subprocess`, sockets and any write outside a scratch dir, so model-generated
code "couldn't get out". Reads were left free on the reasoning that reading is harmless.

They are not, because the same sandbox exposes `rlm(prompt, text)` — a brokered call to
`claude -p`. `open(r"...\some-service\.env").read()` followed by `rlm("summarise", secrets)` walks
the bot token, exchange keys and everything in `os.environ` straight out through the channel
the harness itself provides. The socket block never applies; nothing ever calls `connect`.
The parent does the sending, on the sandbox's behalf, exactly as designed.

The fix is to confine reads to the corpus the harness legitimately needs (the vault, plus
the Python install so imports work) and to scrub the environment before any model code runs.
Both are cheap; neither is what "sandbox" usually connotes, which is why they were missing.

Second lesson from the same review, on the containment mechanism rather than its scope: the
first version blocked a hand-written *list* of audit events — `subprocess.Popen`, `os.system`,
`os.exec`. On Windows, `import _winapi; _winapi.CreateProcess(...)` creates a process without
raising any of them, so the sandbox was open on the only platform it runs on. A deny-list of
event names is only as complete as its author's recall of CPython's audit table. Blocking by
*prefix* (`_winapi.`, `subprocess.`, `ctypes.`, `socket.`) fails closed on the primitive you
forgot, because it belongs to a family you remembered.

## Why it matters / how I'll use it

Whenever a sandbox is proposed, enumerate the channels that *cross* it by design — not the
ones an attacker would have to open. An LLM call, a log shipped to a remote collector, a
metrics counter with a string label, a webhook the harness fires on completion: each is
egress, and each converts "can read" into "can exfiltrate". Ask what the code inside can
read, and treat that answer as the list of secrets you have chosen to publish.

Applies directly to any future agent harness on this box, where a sandbox escape lands next
to a live process stack holding client session tokens and exchange credentials.

## Related
- A self-improving harness is a proposal queue with the promotion gate deleted
- Prompt injection via untrusted conversation text
- LLM confidence score + deterministic code backstop for safe auto-actions

## Source
- 2026-08-08 session: security review of `_tools/rlm_worker.py`, both findings fixed and
  regression-tested the same session.
