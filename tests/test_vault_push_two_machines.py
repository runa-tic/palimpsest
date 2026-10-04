"""Two machines, one remote: vault_push must rebase before pushing and never force or resolve.

Before the port, vault_push only committed and pushed, so once the other machine had pushed,
this machine's push was rejected on every run and it silently stayed behind. Now: commit,
pull --rebase --autostash, push; a same-line conflict aborts the rebase, restores the tree,
reports the files and exits 1.
"""
import json, subprocess, sys
from pathlib import Path
from _util import Checks, copy_tools, git, run, tempdir, write


def clone(remote: Path, name: str) -> Path:
    d = tempdir(f"palimpsest-{name}-")
    subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True)
    git(d, "config", "user.name", name); git(d, "config", "user.email", f"{name}@example.invalid")
    # copy_tools, not a copytree of TOOLS_SRC: that carried a used clone's gitignored state (its
    # deny list, sync receipt and logs/) into this vault (review, 2026-10-02).
    copy_tools(d / "tools")
    write(d, "palimpsest.json", json.dumps({"version": 1, "push_remote": "origin"}))
    return d


def main() -> int:
    c = Checks("vault_push: two machines")
    remote = tempdir("palimpsest-remote-") / "vault.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = clone(remote, "seed")
    write(seed, ".gitignore", "tools/\npalimpsest.json\n")        # code is not what we are testing
    write(seed, "10 Notes/shared.md", "line one\nline two\n")
    git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "seed"); git(seed, "push", "-q", "origin", "main")

    a, b = clone(remote, "a"), clone(remote, "b")
    write(a, "10 Notes/from-a.md", "written on a\n")
    r = run(a, "vault_push.py")
    c.ok(r.returncode == 0 and "pushed" in r.stdout, "machine a pushes", r.stdout)

    write(b, "10 Notes/from-b.md", "written on b\n")          # b is now one commit behind
    r = run(b, "vault_push.py")
    c.ok(r.returncode == 0 and "rebased onto" in r.stdout and "pushed" in r.stdout,
         "machine b (behind) rebases and pushes instead of staying behind", r.stdout)
    r = run(a, "vault_push.py", "--pull-only")
    c.ok(r.returncode == 0 and (a / "10 Notes" / "from-b.md").exists(), "--pull-only brings b's note to a", r.stdout)

    write(a, "10 Notes/shared.md", "line one EDITED ON A\nline two\n")
    run(a, "vault_push.py")
    write(b, "10 Notes/shared.md", "line one EDITED ON B\nline two\n")
    r = run(b, "vault_push.py")
    c.ok(r.returncode == 1 and "PULL CONFLICT" in r.stdout and "shared.md" in r.stdout,
         "a same-line conflict is reported, not resolved", r.stdout)
    gd = git(b, "rev-parse", "--git-dir").stdout.strip()
    c.ok(not (b / gd / "rebase-merge").exists() and not (b / gd / "rebase-apply").exists(),
         "the rebase was aborted (no rebase left in progress)")
    c.ok("EDITED ON B" in (b / "10 Notes" / "shared.md").read_text(), "b's own edit is intact in its tree")
    remote_log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "main"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    shared_on_remote = subprocess.run(["git", "--git-dir", str(remote), "show", "main:10 Notes/shared.md"],
                                      capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    c.ok("EDITED ON A" in shared_on_remote, "nothing was forced over a's version on the remote", shared_on_remote)

    n = clone(remote, "noremote")
    write(n, "palimpsest.json", json.dumps({"version": 1}))
    write(n, "10 Notes/local.md", "x\n")
    r = run(n, "vault_push.py")
    c.ok(r.returncode == 0 and "NOT pushed" in r.stdout and "push_remote" in r.stdout,
         "without push_remote it commits but never guesses a remote", r.stdout)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
