#!/usr/bin/env python3
"""Commit the vault's content, rebase onto the remote, push. Last step of the sync.

The vault syncs automatically but commits by hand, so the working tree drifts further from
the remote every night while the backup silently ages — 358 files had accumulated before
2026-08-09. This closes that gap without turning the repo into a junk drawer, and since
2026-09 it is safe with two machines writing the same vault: it rebases onto the remote
before every push (a machine that only committed and pushed got its push rejected on every
run once the other machine had pushed, and silently stayed behind for weeks).

Rules it will not break:

1. **The commit guards are never bypassed.** No `--no-verify`, ever. scan_secrets.py and
   scan_pii.py run on every auto-commit exactly as they do on a human one, and a BLOCK aborts
   the push and reports loudly. An unattended commit is precisely where a credential would
   escape unnoticed, so the guard has to be strictest here, not most lenient.
2. **It never force-pushes and never resolves a conflict.** It does `git pull --rebase
   --autostash`; a rebase that stops on a conflict is aborted (which restores the tree and the
   autostash), reported, and left for a human. Two machines writing different files never need
   one; the same lines of the same file are a human decision.
3. **Code is not auto-committed.** Only vault CONTENT is staged — notes, transcripts, dailies,
   reviews, maps, projects, areas, the State ledger. Anything under `tools/` is a deliberate
   change deserving a real message; pending code changes are reported, not committed, unless
   --code is passed (the guards still run). And the commit names its paths, so something staged
   by hand never rides along inside an automated commit.
4. **The destination is never inferred.** `push_remote` in palimpsest.json names it; a clone of
   the harness has `origin` pointing at the harness repo.

Usage:
  python tools/vault_push.py              # commit content + pull --rebase + push to push_remote
  python tools/vault_push.py --dry-run    # say what it would do
  python tools/vault_push.py --no-push    # commit only; no remote traffic at all
  python tools/vault_push.py --pull-only  # rebase onto the remote, nothing else
                                          # (the sync's first step, and the session-start hook)
  python tools/vault_push.py --code       # also commit tools/ and root config (guards run)
"""
from __future__ import annotations
import os, sys, subprocess, argparse, time
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TOOLS = Path(__file__).resolve().parent
VAULT = TOOLS.parent
# Generated or hand-written knowledge: safe to snapshot unattended.
CONTENT = ["00 Inbox", "10 Notes", "20 Projects", "30 Areas", "40 Resources", "50 Archive",
           "60 Maps of Content", "Daily", "Reviews", "Skills", "Templates", "State"]
CODE_HINT = "tools"
# Committed only with --code (the commit guards still run): tools and the root config files.
CODE = ["tools", ".claude", "CLAUDE.md", ".gitignore", ".gitattributes"]
NET_TIMEOUT = int(os.environ.get("VAULT_PUSH_NET_TIMEOUT", "120"))  # seconds for anything that talks to the remote
# A caller that kills this process loses the rebase-abort and lock release in its finally block,
# so every caller must allow LOCK_WAIT_S + NET_TIMEOUT (+ a push) or pass tighter budgets here.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
# One vault-push at a time per machine. Two pulls at once — a session-start hook racing the
# scheduled run, or two sessions opening together — made git die with "Cannot fast-forward your
# working tree" in the source vault: the first pull moved HEAD while the second was fetching.
LOCK = TOOLS / ".vault_push.lock"
LOCK_WAIT_S = int(os.environ.get("VAULT_PUSH_LOCK_WAIT", "45"))  # wait for the other run, then skip this cycle
LOCK_STALE_S = 15 * 60    # a lock older than this belongs to a dead run

# Unattended: never sit on a credential or editor prompt.
ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}


def git(*args, check: bool = False, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(VAULT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check, env=ENV,
                          timeout=timeout, creationflags=NO_WINDOW)


def _last(text: str, n: int = 200) -> str:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return lines[-1][:n] if lines else "?"


def rebase_in_progress() -> bool:
    gd = git("rev-parse", "--git-dir").stdout.strip()
    d = Path(gd) if Path(gd).is_absolute() else VAULT / gd
    return (d / "rebase-merge").exists() or (d / "rebase-apply").exists()


def unmerged() -> list[str]:
    return [l for l in git("diff", "--name-only", "--diff-filter=U").stdout.splitlines() if l.strip()]


def push_target() -> tuple[str, str]:
    """(remote, error). The remote is palimpsest.json's push_remote and must exist."""
    sys.path.insert(0, str(TOOLS))
    try:
        import config as cfgmod
        remote = (cfgmod.load().get("push_remote") or "").strip()
    except Exception:
        remote = ""
    if not remote:
        return "", ("`push_remote` is not set in palimpsest.json. Set it to YOUR vault's remote. If "
                    "you cloned Palimpsest and use the clone as your vault, `origin` still points at "
                    "the harness repo and your notes would be pushed there.")
    have = git("remote").stdout.split()
    if remote not in have:
        return "", f"remote '{remote}' does not exist (have: {' '.join(have) or 'none'})"
    return remote, ""


def branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def pull_rebase(remote: str) -> tuple[bool, str]:
    """`git pull --rebase --autostash <remote> <branch>`. Returns (ok, one-line report). Never
    forces, never resolves: a conflicted rebase is aborted — git then re-applies the autostash —
    and the file list is reported for a human."""
    before = git("rev-parse", "HEAD").stdout.strip()
    br = branch()
    try:
        p = git("pull", "--rebase", "--autostash", remote, br, timeout=NET_TIMEOUT)
    except subprocess.TimeoutExpired:
        if rebase_in_progress():
            git("rebase", "--abort")
        return False, f"PULL TIMED OUT after {NET_TIMEOUT}s — remote unreachable? left as-is"
    if p.returncode != 0 and "couldn't find remote ref" in (p.stderr + p.stdout):
        return True, f"pull: {remote}/{br} does not exist yet (first push will create it)"
    if p.returncode != 0 and rebase_in_progress():
        files = unmerged()
        git("rebase", "--abort")  # restores HEAD and the autostash
        return False, (f"PULL CONFLICT in {len(files)} file(s) — rebase aborted, tree restored, "
                       f"left for a human: {', '.join(files[:5])}")
    # The autostash can conflict on re-apply after a *successful* pull. On the rebase path git
    # exits 0 and only warns; on the fast-forward path it exits non-zero. Either way HEAD has
    # moved, the stash is kept and the tree holds the conflict, so classify by the tree.
    files = unmerged()
    if files:
        return False, (f"AUTOSTASH CONFLICT after pull in {len(files)} file(s) — your local "
                       f"edits are in `git stash list`; resolve by hand: {', '.join(files[:5])}")
    if p.returncode != 0:
        return False, f"PULL FAILED (not retried) — {_last(p.stderr + p.stdout)}"
    after = git("rev-parse", "HEAD").stdout.strip()
    if after == before:
        return True, "pull: already up to date"
    return True, f"pull: rebased onto {remote}/{br} @ {after[:7]}"


def acquire_lock() -> bool:
    """O_EXCL create with the pid inside; cross-platform (no fcntl on Windows). Waits up to
    LOCK_WAIT_S for a live holder, removes a stale one, never steals a live one."""
    deadline = time.time() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > LOCK_STALE_S:
                try: LOCK.unlink()
                except FileNotFoundError: pass
                continue
            if time.time() > deadline:
                return False
            time.sleep(0.5)


def release_lock() -> None:
    try: LOCK.unlink()
    except FileNotFoundError: pass


def _present(rel: str) -> bool:
    """On disk OR tracked. Checking only the disk skipped a content directory that had been
    deleted whole, so its deletions were never staged and the run said "nothing new to commit"
    (review, 2026-09-30). `git add -A -- <gone but tracked dir>` stages them fine; only a path
    git has never seen would make `git add` fail, and ls-files keeps those out."""
    return (VAULT / rel).exists() or bool(git("ls-files", "--", rel).stdout.strip())


def main() -> int:
    if not (VAULT / ".git").exists():
        print("not a git repo — nothing to do")
        return 0
    if not acquire_lock():
        print(f"vault-push: another vault-push has held {LOCK.name} for >{LOCK_WAIT_S}s — skipped this run; "
              f"the next one picks up (stale after {LOCK_STALE_S // 60} min)")
        return 1
    try:
        return _run()
    finally:
        release_lock()


def _run() -> int:
    ap = argparse.ArgumentParser(description="Commit the vault's content, pull --rebase, push.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true", help="commit only; touch no remote")
    ap.add_argument("--pull-only", action="store_true", help="rebase onto the remote; nothing else")
    ap.add_argument("--code", action="store_true", help="also commit tools/ and root config (guards run)")
    args = ap.parse_args()

    if rebase_in_progress():
        print("vault-push: a rebase is already in progress — finish or abort it by hand first")
        return 1
    conflicted = unmerged()
    if conflicted:
        print("vault-push: unmerged paths in the index — resolve by hand first; an unattended `git add` "
              f"would stage conflict markers as content: {', '.join(conflicted[:5])}")
        return 1

    if args.pull_only:
        remote, err = push_target()
        if not remote:
            print(f"vault-push: nothing to pull from — {err}")
            return 0
        if args.dry_run:
            print(f"vault-push (dry run): would pull --rebase --autostash from {remote}")
            return 0
        ok, msg = pull_rebase(remote)
        print(f"vault-push: {msg}")
        return 0 if ok else 1

    existing = [p for p in CONTENT if _present(p)]
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

    code_paths = [c for c in CODE if _present(c)]
    code_st = git("status", "--porcelain", "--", *code_paths) if code_paths else None
    code_changed = [l for l in code_st.stdout.splitlines() if l.strip()] if code_st else []
    if args.code and code_changed:
        changed += code_changed
        existing += code_paths

    remote, remote_err = ("", "--no-push") if args.no_push else push_target()

    if args.dry_run:
        print(f"vault-push (dry run): would commit {len(changed)} change(s)"
              f"{' (+ code)' if args.code and code_changed else ''}, then "
              + (f"pull --rebase from and push to {remote}" if remote else f"stop ({remote_err})"))
        if code_changed and not args.code:
            print(f"  would LEAVE {len(code_changed)} change(s) under {CODE_HINT}/ uncommitted (pass --code)")
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
        # Counts by top-level area, so the message says what the run actually produced.
        areas: dict[str, int] = {}
        for line in changed:
            path = line[3:].strip().strip('"')
            areas[path.split("/")[0]] = areas.get(path.split("/")[0], 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(areas.items(), key=lambda x: -x[1]))
        with_code = args.code and code_changed
        msg = (f"vault: sync {datetime.now():%Y-%m-%d}{' + code' if with_code else ''}\n\n"
               f"Automated snapshot of the sync pipeline's output ({summary}).\n"
               f"{'Code/config included (--code).' if with_code else f'Content only — anything under {CODE_HINT}/ is left for a deliberate commit.'}\n"
               f"Written by tools/vault_push.py; secret-scan and pii-scan ran as normal.")
        # NO --no-verify. If a guard blocks, that is the system working.
        # Commit ONLY the content areas this run changed. A bare `git commit` takes the whole
        # index, so anything someone had staged by hand (a half-finished tools/ edit) rode along
        # inside the automated commit (Codex review, 2026-09). `git commit -- <pathspec>` leaves
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
            print(f"vault-push: commit failed — {_last(out)}")
            return 1
        print(f"vault-push: committed {len(changed)} change(s) ({summary})")
    else:
        print("vault-push: nothing new to commit")

    pending = f"  note: {len(code_changed)} change(s) under {CODE_HINT}/ left uncommitted (by design; --code commits them)"
    if args.no_push:
        print("vault-push: --no-push, stopping before the remote")
        if code_changed and not args.code:
            print(pending)
        return 0
    if not remote:
        print(f"vault-push: committed, but NOT pushed — {remote_err}")
        return 0 if "not set" in remote_err else 1

    # Pull AFTER committing, so the rebase carries a real commit and the autostash only has to
    # hold uncommitted code edits (content was just committed).
    ok, msg = pull_rebase(remote)
    print(f"vault-push: {msg}")
    if not ok:
        return 1

    br = branch()
    known = git("rev-parse", "--verify", "--quiet", f"{remote}/{br}").returncode == 0
    ahead = git("rev-list", "--count", f"{remote}/{br}..HEAD").stdout.strip() if known else "new branch"
    if ahead == "0":
        print("vault-push: nothing to push")
        if code_changed and not args.code:
            print(pending)
        return 0
    try:
        p = git("push", remote, "HEAD", timeout=NET_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"vault-push: PUSH TIMED OUT after {NET_TIMEOUT}s — will retry next run")
        return 1
    if p.returncode != 0:
        # Never force. A rejection right after a clean rebase means the other machine pushed in
        # the window between; the next run's pull picks it up.
        print(f"vault-push: PUSH FAILED (not retried, never forced) — {_last(p.stderr + p.stdout)}")
        return 1
    head = git("rev-parse", "--short", "HEAD").stdout.strip()
    print(f"vault-push: pushed {ahead} commit(s) to {remote}/{br} @ {head}")
    if code_changed and not args.code:
        print(pending)
    return 0


if __name__ == "__main__":
    sys.exit(main())
