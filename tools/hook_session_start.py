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

# Reinforcement only. CLAUDE.md is the authority here, because a hook is conditional — it
# needs to be registered, trusted and actually executable — while CLAUDE.md is loaded every
# session by construction. The first install of this repo proved the difference: the hooks
# silently never ran, the model saw none of this, and it opened by offering a briefing for a
# vault that had never been set up. Keep this a pointer, not a second copy that can drift.
FIRST_RUN = """\
# 🧠 Palimpsest — first run

There is no `palimpsest.json` at the vault root, so this vault has never been set up.

Read **`SETUP.md`** and follow it now: do not offer a briefing, do not create notes, do not
run the pipeline. Ask whether they want the one-time setup, and if so put its five questions
to them one at a time.
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
                f"(details: `tools/sync.log`)")
    tail = "  ⚠️ stale" if stale else ""
    return (f"**Sync:** clean — {age} "
            f"({d.get('steps','?')} steps, {d.get('duration_sec','?')}s){tail}")

def pull_line() -> str:
    """Rebase onto the remote before the briefing runs, so a session never starts rendering
    today's note on a tree the other machine has moved past. Reports what happened; a conflict
    is left for a human and said out loud."""
    try:
        # 15 s lock wait + 35 s pull < this 60 s timeout, so vault_push always reaches its own
        # cleanup (rebase --abort, lock release) instead of being killed inside it.
        env = {**os.environ, "VAULT_PUSH_LOCK_WAIT": "15", "VAULT_PUSH_NET_TIMEOUT": "35"}
        p = subprocess.run([PY, str(TOOLS / "vault_push.py"), "--pull-only"], cwd=str(VAULT), env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        out = (p.stdout or "").strip().splitlines()
        msg = out[-1].replace("vault-push: ", "") if out else f"exit {p.returncode}, no output"
    except subprocess.TimeoutExpired:
        msg = "timed out after 60s (remote unreachable?)"
    except Exception as e:
        msg = f"could not run ({type(e).__name__})"
    warn = "" if msg.startswith("pull:") else "⚠️ "
    return f"**Pull at start:** {warn}{msg}"


def pull_is_ok(pull: str) -> bool:
    """True only when pull_line() reported a completed pull (no ⚠️, not empty)."""
    return pull.startswith("**Pull at start:**") and "⚠️" not in pull


def state_block() -> str:
    """The State ledger's hot entities, dated and sourced (tools/state.py). Empty when the vault
    has no ledger. Read-only: the probes run in the sync, not here, so the opener stays fast."""
    if not (VAULT / "State" / "entities.json").exists():
        return ""
    try:
        p = subprocess.run([PY, str(TOOLS / "state.py"), "show", "--hot", "--opener"], cwd=str(VAULT),
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        return (p.stdout or "").strip()
    except Exception as e:
        return f"**State:** ⚠️ ledger unavailable ({type(e).__name__})"


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

    # A brand-new vault has nothing to brief on, and the five choices that shape everything
    # after — extraction model, skills proposer, sync cadence, nightly backup, redaction
    # list — are exactly
    # the ones a silent default would decide badly on someone's behalf. Hand them to the
    # agent instead of a wizard: this harness's interface IS the conversation.
    if not cfgmod.is_configured():
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": FIRST_RUN,
        }}))
        return

    cfg = cfgmod.load()
    pull = pull_line() if cfg["steps"].get("pull") else ""
    if not pull or pull_is_ok(pull):
        run("briefing.py")             # ensure today's daily note + briefing exists
    else:
        # Never render on a tree that may be behind the other machine: the push-time rebase would
        # then stop on today's note, or with merge=union keep both renders.
        pull += " · briefing not refreshed (tree may be behind the other machine)"
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
        if pull:
            ctx += f"\n{pull}\n"
        sb = state_block()
        if sb:
            ctx += f"\n{sb}\n"
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
