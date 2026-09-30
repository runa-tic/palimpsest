#!/usr/bin/env python3
r"""Palimpsest — full sync pipeline orchestrator.

Runs [pull ->] import -> extract -> link -> maintenance -> ... -> briefing [-> push], each with
a hard timeout so a single stuck step can never block the whole run. Logs to tools/sync.log.
With a remote and two machines, turn on "pull" and "push" in palimpsest.json: the pull runs
FIRST, and if it fails the briefing is skipped for that run (see skip_reason).

Run manually:   python tools/sync.py
For an unattended cadence, drive this from your OS scheduler. A Startup-folder shortcut is
the obvious shortcut and the wrong one: it fires at logon only, so on a machine that stays
up for weeks the brain silently stops syncing. Use a real daily job, and read the ok/failures
in .sync_status.json rather than the scheduler's exit code — the launcher returns immediately.
"""
from __future__ import annotations
import os, sys, json, time, subprocess
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
LOCK = TOOLS / ".sync.lock"
LOCK_STALE_S = 3 * 3600  # a lock older than this belongs to a run that died; take it over
PY = sys.executable or "python"

sys.path.insert(0, str(TOOLS))
import config as cfgmod

# Which steps run, which model distils, and how long each may take are the user's calls, not
# this file's — see tools/config.py for what each choice costs and tools/setup.py to make
# them. Order below is the pipeline order; palimpsest.json only toggles and re-times it.
CFG = cfgmod.load()
# Why that load fell back to DEFAULTS ('' when palimpsest.json is fine or absent). On DEFAULTS pull,
# push and state are OFF, so every step can run clean while the backup has silently stopped: the
# run counts it as a failure of its own (see _main).
CONFIG_PROBLEM = cfgmod.problem()
_ARGS = {
    # Pull FIRST: regenerating the briefing or the reviews before pulling is how two machines
    # end up writing the same day's note on different bases.
    "pull":        ["vault_push.py", "--pull-only"],
    "import":      ["import_claude.py", "code"],
    "state":       ["state.py", "probe"],
    "extract":     ["extract_notes.py", "--model", CFG["extraction_model"]],
    "skills":      ["extract_skills.py", "--model", CFG["extraction_model"]],
    "link":        ["link_notes.py"],
    "maintenance": ["maintenance.py"],
    "dedupe":      ["dedupe.py"],
    "triage":      ["triage_skills.py"],
    "weekly":      ["weekly_review.py"],
    "embed":       ["embed.py"],
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
            **({"config_problem": CONFIG_PROBLEM} if CONFIG_PROBLEM else {}),
        }, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"!! could not write {STATUS.name}: {e}")


def skip_reason(name: str, failures: list[str]) -> str:
    """Why a step must not run given the steps that already failed this run ('' = run it).
    Only the briefing depends on the tree being current: rendering today's daily on a base the
    other machine has moved past creates or rewrites the file, and the push-time rebase then
    either stops on it or — with Daily/*.md merge=union — silently keeps BOTH renders (a doubled
    briefing block in the source vault). Every other step is machine-local."""
    if name == "briefing" and "pull" in failures:
        return "pull failed, tree may be behind the other machine"
    return ""


def acquire_lock() -> bool:
    """Atomic create; a stale lock (a run that died) is reclaimed. Keeps a manual run and the
    scheduled one from interleaving their commits and pulls."""
    try:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime > LOCK_STALE_S:
            LOCK.unlink()
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"pid {os.getpid()} started {datetime.now():%Y-%m-%d %H:%M:%S}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def main():
    if not acquire_lock():
        print(f"sync: another run holds {LOCK.name} — skipping this one")
        log(f"\n===== SYNC SKIPPED {datetime.now():%Y-%m-%d %H:%M:%S} — lock held =====")
        sys.exit(0)
    try:
        _main()
    finally:
        try:
            LOCK.unlink()
        except FileNotFoundError:
            pass


def _main():
    started = datetime.now()
    log(f"\n===== SYNC START {started:%Y-%m-%d %H:%M:%S} =====")
    failures: list[str] = []
    if CONFIG_PROBLEM:
        log(f"!! config: {CONFIG_PROBLEM}")
        log("!! WARNING this run uses DEFAULTS — pull, push and state are OFF — so its verdict is FAILED")
        print(f"config: FAILED (config) — {CONFIG_PROBLEM}. Running on DEFAULTS: pull, push and state are OFF.")
        failures.append("config")
    for name, args, timeout in STEPS:
        log(f"\n[{datetime.now():%H:%M:%S}] === {name} ===")
        why = skip_reason(name, failures)
        if why:
            log(f"!! {name} skipped — {why}")
            print(f"{name}: SKIPPED ({why})")
            failures.append(name)
            continue
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
