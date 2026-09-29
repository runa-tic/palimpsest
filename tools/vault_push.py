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
  python tools/vault_push.py            # commit content + push to config's push_remote
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
CODE_HINT = "tools"


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
    # Drop gitignored paths BEFORE staging. `git add` fails outright when handed an ignored
    # path, so one ignored directory meant nothing at all got staged and the whole nightly
    # backup failed — while still reporting a tidy "nothing to commit". Worse, an ignored
    # content dir is content that is silently never backed up, so it is worth saying out loud
    # rather than skipping quietly. (Palimpsest's own repo ignores Daily/ and Reviews/ so the
    # harness does not ship generated notes; a clone used AS a vault should un-ignore them.)
    ignored = []
    if existing:
        chk = git("check-ignore", "--", *existing)
        ignored = [l.strip().strip('"') for l in chk.stdout.splitlines() if l.strip()]
        existing = [p for p in existing if p not in ignored]
    if ignored:
        print(f"vault-push: NOT backing up (gitignored): {', '.join(ignored)}")
    if not existing:
        print("vault-push: every content path is gitignored — nothing can be backed up")
        return 1
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
        # An unattended commit with no configured identity dies on "unable to auto-detect
        # email address", which reads like a bug rather than a one-line fix. Say the fix.
        if not git("config", "user.email").stdout.strip():
            print("vault-push: no git identity — commit skipped. Fix once with:")
            print('  git config user.name "you"  &&  git config user.email "you@example.com"')
            return 1
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
        # Commit ONLY the content areas this run changed. A bare `git commit` takes the whole
        # index, so anything someone had staged by hand (a half-finished tools/ edit) rode along
        # inside "vault: nightly sync" (Codex review, 2026-09). `git commit -- <pathspec>` leaves
        # everything else staged and out of this commit; the guards see the same temporary index.
        # Areas, not all existing dirs: a pathspec naming a dir git knows nothing about (an empty
        # Daily/) fails the whole commit.
        c = git("commit", "-m", msg, "--", *sorted(areas))
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
        if code_changed:
            print(f"  note: {len(code_changed)} change(s) under {CODE_HINT}/ left uncommitted (by design)")
        return 0

    # The destination is never inferred. A clone of the harness has `origin` pointing at the
    # harness repo, so "just push to origin" would publish a private vault into someone else's
    # project — the failure would be silent, remote, and irreversible.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import config as cfgmod
        remote = (cfgmod.load().get("push_remote") or "").strip()
    except Exception:
        remote = ""
    if not remote:
        print("vault-push: committed, but NOT pushed — `push_remote` is not set in "
              "palimpsest.json.")
        print("  Set it to YOUR vault's remote. If you cloned Palimpsest and are using the "
              "clone as your vault,")
        print("  `origin` still points at the harness repo and your notes would be pushed "
              "there.")
        return 0
    if remote not in git("remote").stdout.split():
        print(f"vault-push: committed, but NOT pushed — remote '{remote}' does not exist "
              f"(have: {' '.join(git('remote').stdout.split()) or 'none'})")
        return 1

    p = git("push", remote, "HEAD")
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
