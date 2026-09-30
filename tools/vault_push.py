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
   scan_pii.py run on every auto-commit exactly as they do on a human one — run directly when
   this clone has no pre-commit hook — and a BLOCK aborts the push and reports loudly. An unattended commit is precisely where a credential would
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
# Per-machine renders inside a content folder. maintenance.py rewrites the whole dashboard, with a
# minute timestamp and this machine's counts, at every session start, so two machines' renders
# always differ: once one machine had pushed its render, the other's pull stopped on the autostash
# and every later run refused on the unmerged path (review, 2026-09-30). They are never
# committed, and a local render is set aside across a pull and put back — the next run re-renders
# it anyway, so nothing a human wrote is at stake.
PER_MACHINE = ["Reviews/Vault Health.md"]
KEEP_OUT = [f":(exclude){p}" for p in PER_MACHINE]
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


def status(*paths: str) -> list[tuple[str, list[str]]]:
    """`git status --porcelain -z` as (XY, paths). A rename or copy carries both paths. -z because
    the plain form prints a rename as `old -> new` on one line and only the old side was read, so a
    note moved Inbox -> Notes was committed as a deletion alone (review, 2026-09-30)."""
    out = git("status", "--porcelain", "-z", "--", *paths).stdout.split("\0")
    entries, i = [], 0
    while i < len(out):
        e, i = out[i], i + 1
        if len(e) < 4:
            continue
        ps = [e[3:]]
        if ("R" in e[:2] or "C" in e[:2]) and i < len(out):
            ps.append(out[i]); i += 1          # -z: the destination first, then the source
        entries.append((e[:2], ps))
    return entries


def guard_installed() -> bool:
    """True when git will run tools/githooks/pre-commit on a commit here. core.hooksPath is
    per-clone config that `git clone` does not carry, so the second machine of a vault (or anyone
    who skipped the setup line) committed and pushed with no scan at all, under a message saying
    the scans ran (review, 2026-09-30)."""
    hook = git("rev-parse", "--git-path", "hooks/pre-commit").stdout.strip()
    if not hook:
        return False
    hp = Path(hook) if Path(hook).is_absolute() else VAULT / hook
    try:
        if hp.resolve() != (TOOLS / "githooks" / "pre-commit").resolve():
            return False
    except OSError:
        return False
    return hp.is_file() and (os.name == "nt" or os.access(hp, os.X_OK))


def run_guards() -> tuple[bool, str]:
    """The pre-commit hook's two scans, run directly. (ok, output). They read the whole index —
    a superset of what `git commit -- <areas>` takes, so stricter than the hook, never looser."""
    out = []
    for s in ("scan_secrets.py", "scan_pii.py"):
        if not (TOOLS / s).is_file():
            return False, f"{s} is missing — refusing to commit unscanned"
        try:
            g = subprocess.run([sys.executable or "python", str(TOOLS / s)], cwd=str(VAULT), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", env=ENV, timeout=300,
                               creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return False, f"{s} timed out — refusing to commit unscanned"
        out.append((g.stdout + g.stderr).strip())
        if g.returncode != 0:
            return False, "\n".join(out)
    return True, "\n".join(out)


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


def _set_aside() -> dict[str, bytes]:
    """Take this machine's uncommitted PER_MACHINE renders out of the pull's way: back to HEAD if
    tracked, removed if not (an untracked render made the pull refuse to check out the other
    machine's copy). Returns the bytes to put back."""
    kept = {}
    for rel in PER_MACHINE:
        f = VAULT / rel
        if not f.is_file() or not git("status", "--porcelain", "--", rel).stdout.strip():
            continue
        kept[rel] = f.read_bytes()
        if git("cat-file", "-e", f"HEAD:{rel}").returncode == 0:
            git("checkout", "HEAD", "--", rel)
        else:
            git("rm", "-q", "--cached", "--ignore-unmatch", "--", rel)
            f.unlink()
    return kept


def pull_rebase(remote: str) -> tuple[bool, str]:
    """`git pull --rebase --autostash <remote> <branch>`. Returns (ok, one-line report). Never
    forces, never resolves: a conflicted rebase is aborted — git then re-applies the autostash —
    and the file list is reported for a human. This machine's PER_MACHINE renders sit out the
    pull and come back after it, whatever it did."""
    kept = _set_aside()
    try:
        return _pull_rebase(remote)
    finally:
        for rel, data in kept.items():
            try:
                (VAULT / rel).parent.mkdir(parents=True, exist_ok=True)
                (VAULT / rel).write_bytes(data)
            except OSError:
                pass  # a render; the next maintenance run writes it again


def _pull_rebase(remote: str) -> tuple[bool, str]:
    before = git("rev-parse", "HEAD").stdout.strip()
    stashes = len(git("stash", "list").stdout.splitlines())
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
    # git also refuses the re-apply outright when a stashed file was rewritten between the stash
    # and its re-apply (a racing Stop hook, the sync's own steps): it keeps the stash, exits 0 and
    # leaves no unmerged path, so the edits vanished from the tree while this said "rebased"
    # (review, 2026-09-30). A stash that outlived the pull is the tell.
    if len(git("stash", "list").stdout.splitlines()) > stashes:
        return False, ("AUTOSTASH NOT RE-APPLIED after pull — your local edits are in `git stash list` "
                       "(newest entry), not in the tree; restore them by hand with `git stash pop`")
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
    # Anything outside the content folders and the code is never staged, and was never mentioned
    # either: a note at the vault root or Obsidian's pasted attachments (the root, by default)
    # were silently left out of the backup (review, 2026-09-30). Reported, not staged — an allow
    # list cannot push what it never names, so what lives outside it stays the user's call.
    known = set(CONTENT) | set(CODE)
    outside = sorted({p.split("/")[0] + ("/" if "/" in p else "")
                      for _, ps in status() for p in ps if p.split("/")[0] not in known})
    if outside:
        more = f" and {len(outside) - 8} more" if len(outside) > 8 else ""
        print(f"vault-push: NOT backing up (outside the content folders): {', '.join(outside[:8])}{more} "
              "— move into a content folder, commit by hand, or gitignore to silence")
    if not existing:
        print("vault-push: every content path is gitignored — nothing can be backed up")
        return 1
    changed = status(*existing, *KEEP_OUT)

    code_paths = [c for c in CODE if _present(c)]
    code_changed = status(*code_paths) if code_paths else []
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
        add = git("add", "--", *existing, *KEEP_OUT)
        if add.returncode != 0:
            print(f"vault-push: FAILED to stage — {add.stderr.strip()[:200]}")
            return 1
        # Counts by top-level area, so the message says what the run actually produced.
        # No hook on this clone means no guard at all, so run its scans here instead: a missing
        # setup line must cost a warning, never an unscanned push.
        hooked = guard_installed()
        if not hooked:
            print("vault-push: the pre-commit guard is not installed on this clone (core.hooksPath is not "
                  "tools/githooks) — running its scans directly. Install once: git config core.hooksPath tools/githooks")
            ok, out = run_guards()
            if not ok:
                print("vault-push: BLOCKED by a commit guard — NOT committing or pushing. Staged for review:")
                print("  " + "\n  ".join(out.splitlines()[-6:]))
                return 1
        # Both sides of a rename, so a move between areas commits the addition with the deletion;
        # only areas this run stages, so a note moved out of tools/ cannot take code along.
        areas: dict[str, int] = {}
        for _, paths in changed:
            for top in {p.split("/")[0] for p in paths} & set(existing):
                areas[top] = areas.get(top, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(areas.items(), key=lambda x: -x[1]))
        with_code = args.code and code_changed
        msg = (f"vault: sync {datetime.now():%Y-%m-%d}{' + code' if with_code else ''}\n\n"
               f"Automated snapshot of the sync pipeline's output ({summary}).\n"
               f"{'Code/config included (--code).' if with_code else f'Content only — anything under {CODE_HINT}/ is left for a deliberate commit.'}\n"
               f"Written by tools/vault_push.py; secret-scan and pii-scan ran "
               f"{'as the pre-commit hook' if hooked else 'directly (no pre-commit hook on this clone)'}.")
        # NO --no-verify. If a guard blocks, that is the system working.
        # Commit ONLY the content areas this run changed. A bare `git commit` takes the whole
        # index, so anything someone had staged by hand (a half-finished tools/ edit) rode along
        # inside the automated commit (Codex review, 2026-09). `git commit -- <pathspec>` leaves
        # everything else staged and out of this commit; the guards see the same temporary index.
        # Areas, not all existing dirs: a pathspec naming a dir git knows nothing about (an empty
        # Daily/) fails the whole commit.
        c = git("commit", "-m", msg, "--", *sorted(areas), *KEEP_OUT)
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
