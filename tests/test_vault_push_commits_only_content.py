"""An unattended content commit must never carry code that happened to be staged.

Codex review P1: vault_push.py ran `git add -- <content dirs>` and then a bare `git commit`,
which commits the WHOLE index — so a half-finished tools/unfinished.py that someone had
staged rode along inside "vault: nightly sync". The commit is now limited to the content
paths by pathspec; anything else stays staged, uncommitted, and is reported.
"""
import sys
from _util import Checks, make_vault, git, run, write


def main() -> int:
    c = Checks("vault_push: content-only commit")
    v = make_vault()
    write(v, "10 Notes/seed.md", "seed\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")

    write(v, "tools/unfinished.py", "def half_done(:\n")
    git(v, "add", "tools/unfinished.py")                 # staged by hand, not meant for a sync
    write(v, "10 Notes/tonight.md", "a note the night produced\n")
    write(v, "Templates/empty-dir-marker/.keep", "")     # a content dir with nothing tracked yet
    r = run(v, "vault_push.py", "--no-push")
    c.ok(r.returncode == 0, "vault_push --no-push succeeds", r.stdout + r.stderr)

    files = git(v, "show", "--name-only", "--format=", "HEAD").stdout.split("\n")
    c.ok("10 Notes/tonight.md" in files, "the content change is committed", str(files))
    c.ok("tools/unfinished.py" not in files, "the staged code file is NOT in the sync commit", str(files))
    staged = git(v, "diff", "--cached", "--name-only").stdout.split()
    c.ok("tools/unfinished.py" in staged, "...and is still staged, untouched", str(staged))
    c.ok("tools/" in r.stdout and "uncommitted" in r.stdout, "the pending code change is reported", r.stdout)

    v2 = make_vault()
    write(v2, "10 Notes/seed.md", "seed\n")
    git(v2, "add", "-A"); git(v2, "commit", "-q", "-m", "seed")
    (v2 / "Daily").mkdir()                                # exists, but empty and untracked
    write(v2, "10 Notes/only.md", "x\n")
    r = run(v2, "vault_push.py", "--no-push")
    c.ok(r.returncode == 0 and "committed" in r.stdout, "an empty content dir does not break the pathspec commit",
         r.stdout + r.stderr)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
