#!/usr/bin/env python3
r"""Palimpsest — full sync pipeline orchestrator.

Runs import -> extract -> link -> maintenance -> briefing, each with a hard timeout so
a single stuck step can never block the whole run. Logs to tools/sync.log.

Run manually:   python tools/sync.py
For an unattended cadence, drive this from your OS scheduler. A Startup-folder shortcut is
the obvious shortcut and the wrong one: it fires at logon only, so on a machine that stays
up for weeks the brain silently stops syncing. Use a real daily job, and read the ok/failures
in .sync_status.json rather than the scheduler's exit code — the launcher returns immediately.
"""
from __future__ import annotations
import sys, json, subprocess
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TOOLS = Path(__file__).resolve().parent
VAULT = TOOLS.parent
LOG = TOOLS / "sync.log"
# Machine-local run receipt. The launcher calls this via `start "" /min pythonw.exe`, which
# returns immediately and discards the exit code, so exiting non-zero alerts nobody. Writing
# the verdict here lets hook_session_start.py surface it in the session opener instead —
# the one place the operator reliably reads.
STATUS = TOOLS / ".sync_status.json"
PY = sys.executable or "python"

sys.path.insert(0, str(TOOLS))
import config as cfgmod

# Which steps run, which model distils, and how long each may take are the user's calls, not
# this file's — see tools/config.py for what each choice costs and tools/setup.py to make
# them. Order below is the pipeline order; palimpsest.json only toggles and re-times it.
CFG = cfgmod.load()
_ARGS = {
    "import":      ["import_claude.py", "code"],
    "extract":     ["extract_notes.py", "--model", CFG["extraction_model"]],
    "skills":      ["extract_skills.py", "--model", CFG["extraction_model"]],
    "link":        ["link_notes.py"],
    "maintenance": ["maintenance.py"],
    "dedupe":      ["dedupe.py"],
    "triage":      ["triage_skills.py"],
    "weekly":      ["weekly_review.py"],
    "briefing":    ["briefing.py"],
    "push":        ["vault_push.py"],
}
# (label, script + args, timeout seconds)
STEPS = [(name, args, CFG["timeouts"].get(name, 300))
         for name, args in _ARGS.items() if CFG["steps"].get(name)]

def log(msg: str):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")

def write_status(started: datetime, failures: list[str]):
    """Persist the run verdict. Best-effort: a status-write failure must never fail a sync."""
    try:
        finished = datetime.now()
        STATUS.write_text(json.dumps({
            "started": started.isoformat(timespec="seconds"),
            "finished": finished.isoformat(timespec="seconds"),
            "duration_sec": int((finished - started).total_seconds()),
            "steps": len(STEPS),
            "failures": failures,
            "ok": not failures,
        }, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"!! could not write {STATUS.name}: {e}")


def main():
    started = datetime.now()
    log(f"\n===== SYNC START {started:%Y-%m-%d %H:%M:%S} =====")
    failures: list[str] = []
    for name, args, timeout in STEPS:
        log(f"\n[{datetime.now():%H:%M:%S}] === {name} ===")
        try:
            p = subprocess.run([PY, str(TOOLS / args[0]), *args[1:]], cwd=str(VAULT),
                               capture_output=True, text=True, encoding="utf-8", timeout=timeout)
            if p.stdout.strip():
                log(p.stdout.strip())
            if p.stderr.strip():
                log("STDERR: " + p.stderr.strip())
            # subprocess.run does NOT raise on a non-zero exit, so without this check a step
            # that died still printed "done" — which is how the 07-28 run reported nine clean
            # steps while 70 conversations failed inside them. Report what actually happened.
            if p.returncode != 0:
                log(f"!! {name} exited {p.returncode}")
                print(f"{name}: FAILED (exit {p.returncode})")
                failures.append(name)
            else:
                print(f"{name}: done")
        except subprocess.TimeoutExpired:
            log(f"!! {name} timed out after {timeout}s — skipped")
            print(f"{name}: TIMED OUT")
            failures.append(name)
        except Exception as e:
            log(f"!! {name} failed: {e}")
            print(f"{name}: FAILED ({e})")
            failures.append(name)
    verdict = f"FAILED steps: {', '.join(failures)}" if failures else "all steps clean"
    log(f"===== SYNC END {datetime.now():%Y-%m-%d %H:%M:%S} — {verdict} =====")
    print(f"Sync complete ({verdict}). Log: {LOG}")
    write_status(started, failures)
    # Exit non-zero when anything failed, so a caller (or a future watchdog) can tell a real
    # run from a broken one instead of reading 5,000 lines of log.
    sys.exit(1 if failures else 0)

if __name__ == "__main__":
    main()
