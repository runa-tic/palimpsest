# Palimpsest

*Conversations in, atomic notes out — a self-distilling Obsidian vault for Claude Code, and
several months of notes on everything that went wrong building it.*

A palimpsest is a manuscript scraped down and written over, the earlier text still faintly
readable underneath. That is what this does to a working log: sessions are captured verbatim,
distilled into atomic notes, linked into maps of content, deduplicated, reviewed weekly, and
surfaced back to the agent that produced them.

It runs against a real vault, daily, and has done since June 2026.

## What it actually does

A `Stop` hook records every Claude Code session into the vault as a conversation note, live,
after each turn. A nightly pipeline then distils those transcripts into **atomic notes** (one
idea, in your own words, with frontmatter and tags), wires each new note into the relevant map
of content and its nearest siblings, proposes reusable **skills** — "when X, do Y, because Z" —
into a review queue, regenerates a vault-health report, flags likely duplicate notes, writes a
weekly review, and refreshes the daily briefing. A `SessionStart` hook feeds that briefing back
in, so the next session opens already knowing what is stale, what rolled over, and what you
were last working on.

Two ways to ask it things. `ask.py` ranks every note twice — by keyword overlap and by a local
multilingual embedding model — fuses the two rankings by rank position, feeds the best handful
to a model and answers with citations; right for lookups, and it works in a second language
because the embedding half does. That half is opt-in (`pip install sentence-transformers`,
CPU only, nothing leaves the machine) and without it `ask.py` is plain keyword search. `rlm.py` is the other shape: the corpus
stays a *variable* in a sandboxed Python REPL, and a root model that never sees the vault
writes code to slice it and fans stateless sub-agents out over the slices. Context cost stays
flat in corpus size, so it answers questions whose evidence is spread across hundreds of
transcripts — "how did my thinking on X change", "audit every claim of type Y". The idea is
borrowed from Prime Intellect's Prime Agent; the self-modifying half of that design is
deliberately *not* borrowed, for reasons in the notes.

No API key. Every model call shells out to the local `claude` CLI under your existing login.

## Why it is shaped this way

The code is a few thousand lines of Python and you could write something like it in a weekend.
What took months was learning which of the obvious designs are wrong. Those are in
[`notes/`](notes/), each one written the day something broke:

- **The skills loop proposes; a human promotes.** An agent that reads its own trajectory and
  edits its own prompts is a proposal queue with the promotion gate deleted, and the gate is
  the only part that was load-bearing.
- **Extraction quality collapses under naive volume**, and deduplication is a judgment call —
  so the dedupe step *flags* candidates instead of merging them.
- **Redaction is deny-list-only on purpose**, because blanket email and number scrubbing
  shreds real content — and that choice leaks unless every variant is listed.
- **A deny list that only runs in one writer is not a policy.** Redaction lived inside the
  transcript recorder, so hand-written notes walked straight past it. Enforcement moved to the
  commit hook, the one chokepoint every writer crosses.
- **A sandbox that can call an LLM has egress**, so read scope is send scope. Blocking sockets
  contains nothing when the harness itself hands the sandboxed code a model call.
- **Generated blocks are not durable state.** The daily briefing is regenerated output; a
  checkbox ticked inside it is silently overwritten on the next run.
- **Word overlap cannot see a reworded duplicate.** "GramJS FloodWaitError carries the wait
  duration on `.seconds`" and "…carries the wait on `.seconds`" are the same note twice and
  score 0.13 lexically. `semantic.py` compares meaning instead — TF-IDF plus a truncated SVD,
  on numpy alone, no model download — and similarity turns out *not* to be monotonic in
  usefulness, so the rule is a semantic threshold **plus** a lexical floor.
- **Max over chunks favours long documents.** Scoring a file by its best chunk's cosine hands a
  1,000-chunk transcript a thousand draws at the noise ceiling and a one-paragraph note a single
  draw. The first embedding benchmark on this vault put 230 of 320 top-8 slots on raw
  transcripts and ranked the notes distilled from them around 200th, *below keyword search*.
  A penalty in ln(chunks) took English recall@8 from 0.23 to 0.48 by itself.
- **Rank fusion with a noise list loses to the good list alone.** Reciprocal rank fusion needs no
  calibration between a term count and a cosine, but it gives every list an equal vote. A
  Russian question against English notes yields a keyword list that is noise, and equal-weight
  fusion scored below embeddings alone. Weighting the keyword lane at 0.3 keeps its wins on
  exact identifiers: recall@8 of 0.58 English / 0.25 Russian against 0.31 / 0.02 for keyword
  search, on 150 notes × two languages of synthetic questions (`bench_retrieval.py` regenerates
  the whole table on your own notes).
- **Notes rarely rot; memory does.** Extraction already discards temporary state, so shelf-life
  labelling finds little in `10 Notes/`. The perishable claims live in Claude's memory
  directory, which is loaded into context every session and asserts deployment state as fact —
  one entry insisted its author was abroad for 47 days after they came home.
- **Verification expires too.** Confirming a claim still true resets its clock rather than
  exempting it forever; "still live" is itself perishable.

## Quickstart

The clone is a working vault. `CLAUDE.md` and `.claude/settings.json` ship configured, so
opening a Claude Code session in it walks you through setup on the first turn.

```bash
git clone <this repo> && cd palimpsest
git config core.hooksPath tools/githooks     # secret + PII commit guards
claude                                        # first session: it will offer setup

# or just double-click the launcher in the vault root:
#   Claude Code.cmd   (Windows)      ./claude-code.sh   (macOS / Linux)
# both cd to the vault from their own location, so they survive it being moved

python tools/setup.py                      # five decisions; prints your OS's scheduler command
python tools/sync.py                       # the whole pipeline, idempotent
python tools/ask.py "what did I decide about X?"
python tools/rlm.py --steps 8 --subagents 20 "how has my thinking on X changed?"

pip install sentence-transformers          # optional: local embeddings for ask.py (~1 GB, CPU)
python tools/embed.py                      # build the index once; later runs embed only what changed
python tools/bench_retrieval.py --gen 100 --run   # keyword vs embeddings vs hybrid, on YOUR notes
```

You can skip `setup.py` entirely: with the hooks registered, an unconfigured vault makes the
SessionStart hook hand the *agent* the same five questions, and it walks you through them
conversationally and writes `palimpsest.json` for you. That is the intended path — the
interface to this thing is a conversation, so onboarding may as well be one. The five choices
are the extraction model (the recurring nightly cost), whether the skills proposer runs in the
pipeline, when the sync fires, whether to back up nightly to a remote you name, and what
goes in the redaction deny list; each default is
argued rather than assumed, in `tools/config.py`.

`sync.py` runs each step under a hard timeout, records `ok`/`failures` to
`.sync_status.json`, and is safe to run repeatedly — every step no-ops when nothing changed.
Drive it from a real daily scheduled job, not a logon-triggered shortcut.

**Nightly backup is opt-in.** With `steps.push` enabled, the pipeline ends by committing the
night's content and pushing it, so a vault that syncs automatically stops drifting from its
remote. It never uses `--no-verify` (the secret and PII guards run exactly as on a human
commit, and a block aborts the push), never force-pushes or resolves divergence, and never
auto-commits anything under `tools/` — a 06:00 job should not immortalise a half-finished edit.

Set `push_remote` in `palimpsest.json` to your own vault's remote. It is deliberately not
inferred: if you cloned this repo and are using the clone as your vault, `origin` points at
*this* project, and an unconfigured push would publish your private notes here. For that same
reason a clone-as-vault should un-ignore `Daily/` and `Reviews/` — they are gitignored so the
harness does not ship generated notes, and `vault_push` will tell you it is skipping them.

## Layout

`tools/` holds the pipeline: `import_claude.py` and `hook_record.py` capture, `extract_notes.py`
and `extract_skills.py` distil, `link_notes.py` wires, `maintenance.py`, `dedupe.py` and
`weekly_review.py` report, `briefing.py` and `hook_session_start.py` close the loop, `ask.py`,
`embed.py` and `rlm.py`/`rlm_worker.py` retrieve (`bench_retrieval.py` scores the first two),
and `redact.py`, `scan_secrets.py` and `scan_pii.py`
keep private strings out of git. `templates/` holds the note schemas. `CLAUDE.md` is
the operating protocol (with `SETUP.md` holding the one-time onboarding, so it costs no
context once configured) — the part that makes an agent behave like the vault's brain rather
than a chatbot standing next to it.

## Limitations, honestly

Built for Windows with Obsidian and a PARA layout, so paths and a couple of process details
assume that. The skills extractor is disabled in the default pipeline — it burned most of a
sync window and failed most of its inputs without checkpointing, which is documented in
`sync.py` rather than quietly fixed. Extraction runs on a wall-clock timeout, so a long
absence takes several nightly runs to drain. Embeddings are opt-in and CPU-bound: the first
index over a large vault takes tens of minutes (the nightly sync then keeps it fresh, and that
step is a no-op until the package is installed), and the small multilingual model closes only
part of the cross-language gap — recall@8 of 0.25 in Russian against 0.58
in English on the same notes. None of this is a product; it is one person's working system, published because
the failure log is more useful than the code.

## Credit

Written by runa-tic. Claude Code was the environment it was built in and the subject it was built
around, which is also why the failure log exists.

MIT.
