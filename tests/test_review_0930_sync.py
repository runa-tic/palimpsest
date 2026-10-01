"""Review of 2026-09-30, sync group: vault_push, the session-start hook and the Stop hook.

One check per confirmed finding; every check fails on ba54bc9.
1. the opener says so when state.py fails, instead of silently dropping the State block
2. vault_push runs the secret/PII scans itself when core.hooksPath is not tools/githooks
3. a session-start render of Reviews/Vault Health.md never jams a two-machine pull
4. an autostash that git refuses to re-apply is a failed pull, not a clean one
5. a staged rename across content areas commits both sides
6. the Stop hook reads its JSON as UTF-8 whatever the console code page
7. files outside the content folders are reported as not backed up
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from _util import Checks, TOOLS_SRC, git, run, write
import _util

MADE: list[Path] = []                                        # every temp dir, removed at the end


def make_vault(with_hooks: bool = False) -> Path:
    MADE.append(_util.make_vault(with_hooks))
    return MADE[-1]


def bare_remote() -> Path:
    remote = Path(tempfile.mkdtemp(prefix="palimpsest-remote-")) / "vault.git"
    MADE.append(remote.parent)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    return remote


def clone(remote: Path, name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"palimpsest-{name}-"))
    MADE.append(d)
    subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True, capture_output=True)
    git(d, "config", "user.name", name); git(d, "config", "user.email", f"{name}@example.invalid")
    shutil.copytree(TOOLS_SRC, d / "tools", dirs_exist_ok=True, ignore=shutil.ignore_patterns("cache", "__pycache__"))
    write(d, "palimpsest.json", json.dumps({"version": 1, "push_remote": "origin"}))
    return d


def two_machines() -> tuple[Path, Path, Path]:
    remote = bare_remote()
    seed = clone(remote, "seed")
    write(seed, ".gitignore", "tools/\npalimpsest.json\n")
    write(seed, "10 Notes/shared.md", "shared\n")
    write(seed, "Daily/2026-09-30.md", "the day\n")
    write(seed, "Reviews/Vault Health.md", "*Generated 2026-09-30 06:00*\ninbox=0\n")
    git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "seed"); git(seed, "push", "-q", "origin", "main")
    return remote, clone(remote, "a"), clone(remote, "b")


def remote_show(remote: Path, spec: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "--git-dir", str(remote), "show", spec], capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    c = Checks("review 2026-09-30: sync group")

    # 1. A registry that does not parse (a trailing comma, a BOM from PowerShell) crashed
    # `state.py show` with empty stdout, and the opener dropped the block without a word.
    v = make_vault()
    write(v, "State/entities.json", '{"entities": {"box": {"hot": true},}}\n')
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'tools'); "
                        "import hook_session_start as h; sys.stdout.reconfigure(encoding=\"utf-8\"); print(repr(h.state_block()))"],
                       cwd=v, capture_output=True, text=True, encoding="utf-8", errors="replace")
    c.ok("ledger unreadable" in r.stdout and "JSONDecodeError" in r.stdout,
         "1. a crashing state.py yields a visible '**State:** ledger unreadable' line", r.stdout + r.stderr)

    # 2. core.hooksPath is per-clone and does not travel: the second machine had no guard at all.
    remote = bare_remote()
    v = make_vault()                                         # no core.hooksPath
    git(v, "remote", "add", "origin", str(remote))
    write(v, "palimpsest.json", json.dumps({"version": 1, "push_remote": "origin"}))
    write(v, ".gitignore", "tools/\npalimpsest.json\n")
    write(v, "10 Notes/leak.md", "key: " + "AKIA" + "QZXW" * 4 + "\n")
    r = run(v, "vault_push.py")
    on_remote = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "-q", "main"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    h = make_vault(with_hooks=True)                         # with the hook: no second scan, true message
    write(h, "10 Notes/fine.md", "nothing secret\n")
    rh = run(h, "vault_push.py", "--no-push")
    body = git(h, "log", "-1", "--format=%B").stdout
    c.ok(r.returncode == 1 and "BLOCKED" in r.stdout and not on_remote
         and rh.returncode == 0 and "not installed" not in rh.stdout and "as the pre-commit hook" in body,
         "2. without the pre-commit hook the scans still run and a key is neither committed nor pushed",
         (r.stdout + r.stderr)[-600:] + f" | remote main: {on_remote!r} | hooked: {rh.stdout[-300:]} {body!r}")

    # 3. Both machines regenerate Reviews/Vault Health.md at every session start. Once one of them
    # had pushed its render, the other's pull died on the autostash and every later run refused.
    remote, a, b = two_machines()
    write(a, "Reviews/Vault Health.md", "*Generated 2026-09-30 09:14*\ninbox=1\n")   # a's session start
    write(a, "10 Notes/from-a.md", "a\n")
    ra = run(a, "vault_push.py")
    pushed_a = git(a, "show", "HEAD:Reviews/Vault Health.md").stdout
    git(a, "add", "-f", "Reviews/Vault Health.md"); git(a, "commit", "-q", "-m", "older machine commits a render", check=False)
    git(a, "push", "-q", "origin", "main")                  # e.g. a machine still on the old vault_push
    write(b, "Reviews/Vault Health.md", "*Generated 2026-09-30 09:20*\ninbox=2\n")   # b's session start
    r1 = run(b, "vault_push.py", "--pull-only")
    write(b, "10 Notes/from-b.md", "b\n")
    r2 = run(b, "vault_push.py")
    c.ok(ra.returncode == 0 and "09:14" not in pushed_a
         and r1.returncode == 0 and r2.returncode == 0 and not git(b, "diff", "--name-only", "--diff-filter=U").stdout
         and "09:20" in (b / "Reviews" / "Vault Health.md").read_text()
         and "09:20" not in remote_show(remote, "main:Reviews/Vault Health.md").stdout,
         "3. per-machine health renders are never committed and never block a pull",
         f"a: {ra.stdout[-200:]} | pull: {r1.stdout[-300:]} | push: {r2.stdout[-300:]}")

    # 4. A writer touching a stashed file between the autostash and its re-apply: git keeps the
    # stash, exits 0 and leaves no unmerged path; the edits silently vanished from the tree.
    remote, a, b = two_machines()
    write(a, "10 Notes/from-a.md", "a\n"); run(a, "vault_push.py")
    write(b, "Daily/2026-09-30.md", "the day + my thoughts\n")
    hook = b / ".git" / "hooks" / "post-merge"             # stands in for a racing Stop hook
    hook.write_text("#!/bin/sh\necho racer >> Daily/2026-09-30.md\n"); hook.chmod(0o755)
    r = run(b, "vault_push.py", "--pull-only")
    stashes = git(b, "stash", "list").stdout
    c.ok(r.returncode == 1 and "stash" in r.stdout and stashes,
         "4. an autostash git refused to re-apply is reported as a failed pull", r.stdout + f" | stash: {stashes!r}")

    # 5. `git mv` Inbox -> Notes printed `R  old -> new`; only the source's area went into the commit.
    v = make_vault()
    write(v, "00 Inbox/idea.md", "an idea\n"); write(v, "10 Notes/other.md", "x\n")
    write(v, ".gitignore", "tools/\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")
    git(v, "mv", "00 Inbox/idea.md", "10 Notes/idea.md")
    r = run(v, "vault_push.py", "--no-push")
    tree = git(v, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
    c.ok(r.returncode == 0 and "10 Notes/idea.md" in tree and "00 Inbox/idea.md" not in tree,
         "5. a staged cross-area rename commits the destination as well as the deletion",
         r.stdout + f" | HEAD tree: {tree}")

    # 6. Windows decodes piped stdin in the ANSI code page; Claude Code sends UTF-8. A Cyrillic
    # user name turned the transcript path into mojibake and the turn was never recorded.
    v = make_vault()
    proj = v / "Пользователь" / "projects" / "-home-me-proj"; proj.mkdir(parents=True)
    tr = proj / "abcd1234-0000-0000-0000-000000000000.jsonl"
    tr.write_text(json.dumps({"type": "user", "timestamp": "2026-09-30T01:00:00Z",
                              "message": {"role": "user", "content": "hello from the hook"}}) + "\n",
                  encoding="utf-8")
    env = {k: val for k, val in os.environ.items() if k != "CLAUDE_BRAIN_NO_HOOK"}
    env["PYTHONIOENCODING"] = "cp1252"                      # what a Western-European Windows console gives
    subprocess.run([sys.executable, str(v / "tools" / "hook_record.py")], cwd=v, env=env, capture_output=True,
                   input=json.dumps({"transcript_path": str(tr)}, ensure_ascii=False).encode("utf-8"))
    notes = list((v / "40 Resources" / "Claude Conversations").rglob("*(abcd1234).md"))
    c.ok(len(notes) == 1, "6. the Stop hook records a transcript whose path is non-ASCII", str(notes))

    # 7. Only the fixed content folders are staged; a root note or an attachments folder was never
    # backed up and never mentioned.
    v = make_vault()
    write(v, ".gitignore", "tools/\n")
    write(v, "10 Notes/seed.md", "seed\n"); git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")
    write(v, "Home.md", "my start page\n"); write(v, "Attachments/pasted.png", "png")
    write(v, "10 Notes/new.md", "new\n")
    r = run(v, "vault_push.py", "--no-push")
    c.ok(r.returncode == 0 and "NOT backing up" in r.stdout and "Home.md" in r.stdout and "Attachments" in r.stdout,
         "7. files outside the content folders are reported as not backed up", r.stdout)
    return c.done()


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for d in MADE:
            _util.rmtree(d, ignore_errors=True)
    sys.exit(rc)
