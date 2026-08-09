---
type: note
created: 2026-08-08
tags: [agents, harness-design, llm-system-design, safety, methodology]
---
# A self-improving harness is a proposal queue with the promotion gate deleted

> An agent that CRUDs its own prompts, skills, and memory from its own trajectories is architecturally identical to a propose-then-promote pipeline — minus the promotion step. The gate is the only part that was ever load-bearing, so "self-improving" names the removal of a safeguard, not the addition of a capability.

## Elaboration

Prime Intellect's Prime Agent (released 2026-08-06, MIT) formalises harness state as
`H = (ρ, G, K, M)` — prompt notes, subagents, skills, memory — and exposes it as CRUD:
`create_prompt_note()`, `create_skill()`, `create_memory()`, `create_subagent()`, plus a
`/refine` pipeline that reads the agent's own trajectories and applies "small,
evidence-backed edits". Presented as a new abstraction, the Continual Harness.

This vault has run the same architecture since 2026-06-06. `extract_skills.py` mines
sessions for reusable procedures, `extract_notes.py` distils atomic notes, `link_notes.py`
wires them into MOCs, `MEMORY.md` holds durable facts. Trajectories in, harness state out.
The one structural difference: candidates land in `Skills/_proposed/` and a human promotes
the keepers (`Skills/README.md`, §"Why propose-only"). `/refine` writes straight to live state.

That difference is the whole design, and it was chosen on evidence, not caution. The note
pipeline already demonstrated the failure mode twice —
Extraction quality collapses under naive volume and
Deduplication as flagging preserves judgment calls. Generation is cheap; *retrieval* is
the scarce resource, and every mediocre auto-published skill dilutes it. A model grading
its own trajectory as evidence for editing its own prompt has no independent signal at any
point in the loop — the same closed circuit that
LLM confidence score + deterministic code backstop for safe auto-actions exists to
break, where the deterministic layer may downgrade a model's self-classification but never
upgrade it. `/refine` upgrades.

Prime Intellect's authors concede the harness "is not fully utilized without a trained
model", which is the honest form of the claim: self-modification pays off when a model was
co-trained to modify that specific state, not as a bolt-on to a general model.

## Why it matters / how I'll use it

When a system markets itself as self-improving, find the promotion gate and ask who
deleted it and on what evidence. If the answer is "the model reads its own transcript and
decides", that is a closed loop with no external signal, and the correct port is the
*mechanism* without the autonomy: keep the extractor, keep the proposal queue, keep the
human promote.

Concretely: adopt nothing from Prime Agent's Continual Harness — this vault already has the
better-governed version. Adopt the RLM execution model instead, which is an orthogonal and
genuinely useful idea: context as a variable in a persistent REPL, subagent delegation as
`await rlm(...)` function calls rather than a tool-call per conversational turn. That is a
token-efficiency win for fan-out triage over queues too large for any context window, and
it carries no self-modification with it.

## Related
- Extraction quality collapses under naive volume
- Deduplication as flagging preserves judgment calls
- LLM confidence score + deterministic code backstop for safe auto-actions
- Confidence-gated autosend with code backstop

## Source
- 2026-08-08 session; https://www.primeintellect.ai/blog/prime-agent, https://github.com/PrimeIntellect-ai/prime-agent
- `Skills/README.md` §"Why propose-only (not auto-publish)"
