#!/usr/bin/env python3
"""Commit the night's vault output and push it. Last step of the sync.

The vault syncs automatically but commits by hand, so the working tree drifts further from
the remote every night while the backup silently ages — 358 files had accumulated before
2026-08-09. This closes that gap without turning the repo into a junk drawer.

Three rules it will not break:

1. **The commit guards are never bypassed.** No `--no-verify`, ever. scan_secrets.py and
   scan_pii.py run on every auto-commit exactly as they do on a human one, and a BLOCK aborts
   the push and reports loudly. An unattended commit is precisely where a credential would
   escape unnoticed, so the guard has to be strictest here, not most lenient.
2. **It never force-pushes and never resolves conflicts.** A rejected push is reported and
   left alone; divergence is a human's problem.
3. **Code is not auto-committed.** Only vault CONTENT is staged — notes, transcripts, dailies,
   reviews, maps, projects, areas. Anything under `tools/` is a deliberate change deserving a
   real message, and a 06:00 job should not immortalise a half-finished edit. Pending code
   changes are reported, not committed.

Usage:
  python tools/vault_push.py            # commit content + push
  python tools/vault_push.py --dry-run  # say what it would do
  python tools/vault_push.py --no-push  # commit only
"""
from __future__ import annotations
import sys, subprocess, argparse
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
# Generated or hand-written knowledge: safe to snapshot unattended.
CONTENT = ["00 Inbox", "10 Notes", "20 Projects", "30 Areas", "40 Resources", "50 Archive",
           "60 Maps of Content", "Daily", "Reviews", "Skills", "Templates"]
CODE_HINT = "_tools"


def git(*args, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(VAULT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


def main() -> int:
    ap = argparse.ArgumentParser(description="Commit + push the vault's content.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    if not (VAULT / ".git").exists():
        print("not a git repo — nothing to do")
        return 0

    existing = [p for p in CONTENT if (VAULT / p).exists()]
    st = git("status", "--porcelain", "--", *existing)
    changed = [l for l in st.stdout.splitlines() if l.strip()]

    code_st = git("status", "--porcelain", "--", CODE_HINT)
    code_changed = [l for l in code_st.stdout.splitlines() if l.strip()]

    unpushed = 0
    up = git("rev-list", "--count", "@{u}..HEAD")
    if up.returncode == 0 and up.stdout.strip().isdigit():
        unpushed = int(up.stdout.strip())

    if not changed and not unpushed:
        print("vault-push: nothing to commit, nothing to push")
        if code_changed:
            print(f"  ({len(code_changed)} pending change(s) under {CODE_HINT}/ — commit those by hand)")
        return 0

    if args.dry_run:
        print(f"vault-push (dry run): would commit {len(changed)} content change(s), "
              f"{unpushed} commit(s) already unpushed")
        if code_changed:
            print(f"  would LEAVE {len(code_changed)} change(s) under {CODE_HINT}/ uncommitted")
        return 0

    if changed:
        add = git("add", "--", *existing)
        if add.returncode != 0:
            print(f"vault-push: FAILED to stage — {add.stderr.strip()[:200]}")
            return 1
        # Counts by top-level area, so the message says what the night actually produced.
        areas: dict[str, int] = {}
        for line in changed:
            path = line[3:].strip().strip('"')
            areas[path.split("/")[0]] = areas.get(path.split("/")[0], 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(areas.items(), key=lambda x: -x[1]))
        msg = (f"vault: nightly sync {datetime.now():%Y-%m-%d}\n\n"
               f"Automated snapshot of the sync pipeline's output ({summary}).\n"
               f"Content only — anything under {CODE_HINT}/ is left for a deliberate commit.\n"
               f"Written by tools/vault_push.py; secret-scan and pii-scan ran as normal.")
        # NO --no-verify. If a guard blocks, that is the system working.
        c = git("commit", "-m", msg)
        if c.returncode != 0:
            out = (c.stdout + c.stderr).strip()
            if "pii-scan" in out or "secret-scan" in out or "blocked" in out.lower():
                print("vault-push: BLOCKED by a commit guard — NOT pushing. Staged for review:")
                print("  " + "\n  ".join(out.splitlines()[-6:]))
                return 1
            print(f"vault-push: commit failed — {out.splitlines()[-1][:200] if out else '?'}")
            return 1
        print(f"vault-push: committed {len(changed)} content change(s) ({summary})")

    if args.no_push:
        print("vault-push: --no-push, stopping before the remote")
        return 0

    if not git("remote").stdout.strip():
        print("vault-push: no remote configured — commit only")
        return 0

    p = git("push")
    if p.returncode != 0:
        err = (p.stderr + p.stdout).strip()
        # Never force, never auto-merge. Diverged history is a human decision.
        print(f"vault-push: PUSH FAILED (not retried, never forced) — {err.splitlines()[-1][:200] if err else '?'}")
        return 1
    head = git("rev-parse", "--short", "HEAD").stdout.strip()
    print(f"vault-push: pushed to {git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()} @ {head}")
    if code_changed:
        print(f"  note: {len(code_changed)} change(s) under {CODE_HINT}/ still uncommitted (by design)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
