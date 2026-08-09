# Palimpsest — first-run setup

> Read this only when `palimpsest.json` is absent from the vault root. `CLAUDE.md` points here
> so the onboarding does not sit in every session's context forever — it is a one-time event,
> and it was 47% of that file.

## The setup interview

**Does `palimpsest.json` exist at the vault root?** Check with a tool call; do not assume.

**If it does not exist, this vault has never been set up.** Everything below in this file is
suspended until it is. Do not offer a briefing, do not read Vault Health, do not create notes,
do not run the pipeline — there is nothing to brief on and the setup choices change what every
nightly run costs. Instead, open by saying plainly that this is a fresh Palimpsest vault that
needs a one-time setup, and ask whether they want to do it now.

If they say yes, ask these **five** questions **one at a time**, recommending the default and
giving the trade-off in a sentence. Do not dump them all at once, and do not proceed to the
next before they answer. Say "five" if you announce a count — the deny list is a question too,
not an afterthought, and a user told "four" will wonder what went wrong at the fifth.

1. **Seed the redaction deny list — ASK THIS FIRST, before anything else.** `tools/.redact_terms.txt`
   is scrubbed from recorded transcripts and blocked at commit time. It is deny-list-only by
   design — blanket scrubbing shreds real content — so anything unlisted passes through
   verbatim. Ask for their email addresses and phone numbers now and write the file
   immediately.
   **Why first:** the `Stop` hook records this conversation to disk after *every* turn, and
   redaction is applied at write time against whatever the list holds *then*. Ask it fifth and
   four turns are already on disk unprotected — if they mention an address while answering an
   earlier question, it is written verbatim and a later deny-list entry cannot reach back and
   scrub it. That is this project's own documented failure (see
   `notes/Deny-list-only PII redaction leaks unless every address variant is explicitly blocked.md`),
   so do not reproduce it during setup. Anything they typed *before* this point is already
   recorded: say so plainly rather than implying the list protects it retroactively.
2. **Which model distils transcripts into notes?** It runs once per conversation, every night,
   so it is the recurring cost of the whole system. Recommend `claude-haiku-4-5-20251001`.
   Sonnet gives noticeably better notes for several times the nightly cost.
3. **Run the skills proposer in the nightly sync?** Recommend **off**, concretely: in the vault
   this came from it consumed most of the sync window, failed most of its inputs without
   checkpointing so the same failures retried every night, and grew a 654-deep proposal queue
   against 16 actually-promoted skills. Better run by hand with `--limit` when wanted. If they
   choose Sonnet above *and* turn this on, say plainly that this is the expensive corner of the
   config and the first run's duration is worth watching.
4. **What time should the sync run?** It rewrites notes while it works, so it wants an hour
   they are never mid-session in the vault. Recommend 06:00 local.
5. **Nightly backup?** With it on, the pipeline ends by committing that night's notes and
   pushing them to the remote named in `push_remote`, so the vault stops drifting from its
   backup. Recommend **off until they have a remote they trust** — the commit guards are the
   only thing standing between an unattended commit and a published secret. It never bypasses
   those guards, never force-pushes, and never commits code. If they say yes, you must also
   ask which remote and write `push_remote`: it is never inferred, because a clone of this
   repo has `origin` pointing at the harness and their notes would be published here.

**Before telling them to run the first sync, warn them what it costs.** `import_claude` pulls
this vault's own Claude Code transcripts and `extract_notes` then makes one model call per
conversation — on a machine with real history that is minutes to tens of minutes and a real
bill, not a quick smoke test. Tell them the count first (`ls ~/.claude/projects/<this-vault's-slug>`)
so the number is their decision. Do NOT start a sync yourself while the interview is still
running: the pipeline rewrites notes and the setup is not finished until `palimpsest.json` is
written. If they want a cheap first look, `python tools/briefing.py` alone is instant.

Then write `palimpsest.json` (schema in `tools/config.py`), and give them, in this order: the
exact scheduler command for their OS for the time they chose (`python tools/setup.py` prints
it), `git config core.hooksPath tools/githooks` for the commit guards, and finally
`python tools/sync.py` for the first run.

**Do not hand them hook-registration JSON if they cloned this repo** — `.claude/settings.json`
ships with SessionStart and Stop already wired, and you can prove it: the notice you are
reading came from that hook. Only walk through registration if they copied the tools into an
existing vault, in which case `setup.py` prints the snippet, and the event names must nest
under a top-level `"hooks"` key (omitting that wrapper fails silently, and `/hooks` in a fresh
session shows an empty list).

Two things worth telling them unprompted at the end, because both fail *silently*. The hooks
invoke `python3`; on Windows that often resolves to a WindowsApps alias which can be turned
off under Settings → App execution aliases, after which nothing is recorded and no first-run
notice appears. Offer to pin `.claude/settings.json` to an absolute interpreter path if they
want that dependency gone. And the pipeline's own model default differs from the one you just
wrote to `palimpsest.json` only if they ran a tool by hand — `sync.py` always passes the
configured model explicitly.

If they decline, say the vault will stay unconfigured and this prompt will return next session,
then help with whatever they actually asked for. **Once `palimpsest.json` exists, ignore this
entire section** and behave as the rest of this file describes.
