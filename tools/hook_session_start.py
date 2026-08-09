#!/usr/bin/env python3
"""SessionStart hook: greet each brain session with vault health + today's briefing.

Refreshes the daily briefing and the health report, then emits a compact digest as
SessionStart additionalContext so Claude can open with a status the moment you start.
Best-effort and silent on failure; never blocks the session.
"""
import os, sys, json, subprocess, re
from pathlib import Path
from datetime import date, datetime

TOOLS = Path(__file__).resolve().parent
VAULT = TOOLS.parent
PY = sys.executable or "python"
STATUS = TOOLS / ".sync_status.json"
STALE_DAYS = 3

sys.path.insert(0, str(TOOLS))
import config as cfgmod

FIRST_RUN = """\
# 🧠 Palimpsest — first run

This vault has no `palimpsest.json`, so nobody has made the setup decisions yet. Don't run
the pipeline and don't create notes until they're made — the choices change what every
nightly run costs and what leaves the machine.

Greet the user, say plainly that this is a fresh Palimpsest vault, and walk them through
four questions **one at a time**, recommending a default and explaining the trade-off in a
sentence. Then write the answers by running `python tools/setup.py` for them, or by writing
`palimpsest.json` directly using the schema in `tools/config.py`.

1. **Extraction model.** Distilling runs once per conversation, every night, so this is the
   recurring cost of the whole system. Recommend `claude-haiku-4-5-20251001`; note that
   Sonnet gives noticeably better notes for several times the nightly cost.
2. **Skills proposer in the nightly sync?** Recommend OFF, and say why concretely: in the
   vault this came from it consumed most of the sync window, failed most of its inputs
   without checkpointing so the same failures retried nightly, and grew a 654-deep proposal
   queue against 16 promoted skills. Better run by hand with `--limit`.
3. **Sync time.** It rewrites notes while it runs, so it wants an hour they're never working
   in the vault. Recommend 06:00 local. Afterwards, give them the exact scheduler command
   for their OS — `python tools/setup.py` prints it — and warn that a Startup-folder
   shortcut only fires at logon, which on an always-on machine means it never runs.
4. **Redaction deny list.** `tools/.redact_terms.txt` is scrubbed from recorded transcripts
   and blocked at commit time. It is deny-list-only by design, because blanket scrubbing
   shreds real content — so anything unlisted passes through verbatim. Ask them to seed it
   with their own email addresses and phone numbers before the first commit.

Then point them at `git config core.hooksPath tools/githooks` for the commit guards, and at
`python tools/sync.py` for the first run. Read `notes/` if they ask why any default is what
it is — each note was written the day the alternative failed.
"""


def _age(then: datetime) -> str:
    s = (datetime.now() - then).total_seconds()
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def sync_line() -> str:
    """One line on the last sync run.

    Always emits something. A silent line would be ambiguous — 'no warning' and 'the hook
    broke' look identical, which is the same failure mode that let a disabled watchdog sit
    unnoticed for six days.
    """
    if not STATUS.exists():
        return "**Sync:** no run recorded yet (status file appears after the next sync)"
    try:
        d = json.loads(STATUS.read_text(encoding="utf-8"))
        fin = datetime.fromisoformat(d["finished"])
    except Exception as e:
        return f"**Sync:** ⚠️ unreadable status file ({type(e).__name__})"
    age, stale = _age(fin), (datetime.now() - fin).days >= STALE_DAYS
    if d.get("failures"):
        return (f"**Sync:** ⚠️ FAILED — {', '.join(d['failures'])} — {age} "
                f"(details: `_tools/sync.log`)")
    tail = "  ⚠️ stale" if stale else ""
    return (f"**Sync:** clean — {age} "
            f"({d.get('steps','?')} steps, {d.get('duration_sec','?')}s){tail}")

def run(script: str) -> str:
    try:
        return subprocess.run([PY, str(TOOLS / script)], cwd=str(VAULT),
                              capture_output=True, text=True, encoding="utf-8", timeout=90).stdout or ""
    except Exception:
        return ""

def main():
    # Skip for our own `claude -p` tool-runs.
    if os.environ.get("CLAUDE_BRAIN_NO_HOOK"):
        return
    today = date.today().isoformat()

    # A brand-new vault has nothing to brief on, and the four choices that shape everything
    # after — extraction model, skills proposer, sync cadence, redaction list — are exactly
    # the ones a silent default would decide badly on someone's behalf. Hand them to the
    # agent instead of a wizard: this harness's interface IS the conversation.
    if not cfgmod.is_configured():
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": FIRST_RUN,
        }}))
        return

    run("briefing.py")                 # ensure today's daily note + briefing exists
    health_out = run("maintenance.py") # refresh Reviews/Vault Health.md

    brief = ""
    daily = VAULT / "Daily" / f"{today}.md"
    if daily.exists():
        m = re.search(r"<!-- briefing:start -->(.*?)<!-- briefing:end -->",
                      daily.read_text(encoding="utf-8"), re.DOTALL)
        if m:
            brief = m.group(1).strip()

    health = ""
    m = re.search(r"(inbox=.*)", health_out)
    if m:
        health = m.group(1).strip()

    ctx = f"# 🧠 Brain session — {today}\n"
    if health:
        ctx += f"\n**Vault health:** {health}  (details: [[Vault Health]])\n"
    try:
        ctx += f"\n{sync_line()}\n"
    except Exception:
        pass  # the opener must never break on its own status line
    if brief:
        ctx += f"\n{brief}\n"
    ctx += ("\n*(You are this vault's brain. Open with a brief status drawn from the above, "
            "then help. Recall from notes and cite [[them]]; capture durable insights as you go.)*")

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }}))

if __name__ == "__main__":
    main()
