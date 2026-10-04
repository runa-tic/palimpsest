"""Regression for the 2026-10-02 review: make_vault copies the tools, not the machine's state.

make_vault copied tools/ whole, so a used clone's gitignored state (the deny list setup.py writes,
the sync receipt, rlm trajectories under logs/) landed in every throwaway vault. After one
setup.py run the setup test went 23/24 and both its push_remote checks passed without reaching the
push_remote prompt; one rlm trajectory turned rlm checks red.

1. From a git checkout of tools/, the copy holds none of the state .gitignore lists.
2. It holds every tracked tool, byte for byte; one deleted from the working tree is left out.
3. An untracked tool git does not ignore is copied too, so a new tool is tested before its first
   commit (the decision: git's view of the working tree, not of the last commit).
4. The copied pre-commit hook is still executable (POSIX only).
5. From a plain directory (PALIMPSEST_TOOLS at an exported tree) the copy holds the tools and
   none of that state. The state files come from .gitignore, so one added there but not to
   _util.TOOLS_STATE fails here.
6. So does a copy of tools/ kept untracked inside another repository, whose .gitignore knows
   nothing of it: git lists its files, TOOLS_STATE still drops the state.
7. An untracked nested repository in tools/ (a developer's scratch clone), which git lists as one
   "sub/" entry, is copied as a tree without its .git, not opened as a file.
8. From this checkout's own tools/, the copy is exactly the files git does not ignore.

These test tests/_util.py itself, which PALIMPSEST_TOOLS does not swap. Next to a _util.py from
before copy_tools existed, when make_vault copied tools/ whole, checks 1, 5 and 6 fail and 2 to 4
pass (those three guard the new copy against dropping tools, and the whole-directory copy dropped
none); the script then dies with AttributeError at check 7, which calls _util.copy_tools, so check 8
never runs and no summary line is printed. Next to a _util.py whose copy_tools still copied every
entry git lists as a file, only check 7 fails (on macOS, with IsADirectoryError).
"""
import hashlib, json, os, shutil, stat, subprocess, sys
from pathlib import Path
import _util
from _util import Checks, REPO, git, make_vault, tempdir, write

HERE = Path(__file__).resolve().parent
# Lists make_vault's copy as {relative path: [sha256, executable]}, with PALIMPSEST_TOOLS taken
# from the environment exactly as a test run would.
DRIVER = r"""
import hashlib, json, os, stat, sys
sys.path.insert(0, sys.argv[1])
import _util
t = _util.make_vault() / "tools"
out = {}
for root, _dirs, files in os.walk(t):
    for f in files:
        p = os.path.join(root, f)
        out[os.path.relpath(p, t).replace(os.sep, "/")] = [
            hashlib.sha256(open(p, "rb").read()).hexdigest(), bool(os.stat(p).st_mode & stat.S_IXUSR)]
print(json.dumps(out))
"""
TOOLS = {"a_tool.py": "print('a')\n", ".redact_terms.example.txt": "# example deny list\n",
         "githooks/pre-commit": "#!/bin/sh\nexit 0\n", "naïve.txt": "a non-ASCII name\n"}


def state_paths() -> list[str]:
    """One file for every tools/ entry in .gitignore, plus the unanchored bytecode patterns."""
    out = ["__pycache__/a_tool.cpython-313.pyc", "stray.pyc"]
    for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("tools/"):
            name = line[len("tools/"):]
            out.append(name + "x.jsonl" if name.endswith("/") else name)
    return out


def copied(src: Path, env: dict | None = None) -> dict | str:
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + DRIVER, str(HERE)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120,
                       env={**os.environ, "PALIMPSEST_TOOLS": str(src), **(env or {})})
    try:
        return json.loads(r.stdout)
    except ValueError:
        return f"driver exit {r.returncode}: {(r.stdout + r.stderr)[-400:]}"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    c = Checks("review 2026-10-02: make_vault copies tools, not state")
    state = state_paths()
    want = {**TOOLS, "new_tool.py": ""}

    def holds_tools_only(got) -> bool:
        return not isinstance(got, str) and not set(state) & set(got) and set(want) <= set(got)

    def why(got) -> str:
        return got if isinstance(got, str) else (f"leaked: {sorted(set(state) & set(got))}; "
                                                 f"missing: {sorted(set(want) - set(got))}")

    # A git checkout: tools committed, then a used clone's state, a deletion and a new tool.
    src = tempdir("palimpsest-tools-src-")
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.name", "test")
    git(src, "config", "user.email", "test@example.invalid")
    shutil.copy2(REPO / ".gitignore", src / ".gitignore")
    for rel, text in {**TOOLS, "gone.py": "x = 1\n"}.items():
        write(src, f"tools/{rel}", text)
    hook = src / "tools" / "githooks" / "pre-commit"
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "tools")
    (src / "tools" / "gone.py").unlink()
    write(src, "tools/new_tool.py", "print('new')\n")
    for rel in state:
        write(src, f"tools/{rel}", "machine state\n")

    got = copied(src / "tools")
    if isinstance(got, str):
        for what in ("no state", "tracked tools", "untracked tool"):
            c.ok(False, f"make_vault from a git checkout ({what})", got)
    else:
        leaked = sorted(set(state) & set(got))
        c.ok(not leaked, "from a git checkout, the copy holds none of the gitignored state "
             "(deny list, sync receipt, logs/, ...)", f"leaked: {leaked}")
        wrong = [rel for rel in TOOLS if got.get(rel, [None])[0] != sha(src / "tools" / rel)]
        c.ok(not wrong and "gone.py" not in got,
             "it holds every tracked tool byte for byte, and leaves out one deleted from the working tree",
             f"missing or different: {wrong}; gone.py copied: {'gone.py' in got}; got {sorted(got)}")
        c.ok(got.get("new_tool.py", [None])[0] == sha(src / "tools" / "new_tool.py"),
             "an untracked tool git does not ignore is copied, so a new tool is tested before its commit",
             f"got {sorted(got)}")
        if os.name == "nt":
            c.skip("the copied pre-commit hook stays executable", "Windows has no executable bit")
        else:
            c.ok(got.get("githooks/pre-commit", [None, False])[1], "the copied pre-commit hook stays executable",
                 str(got.get("githooks/pre-commit")))

    # A plain directory: the same working tree with no git around it. The ceiling keeps git from
    # finding a repository above the temp dir, so this takes the fallback on every machine.
    plain = tempdir("palimpsest-tools-plain-")
    shutil.copytree(src / "tools", plain / "tools")
    ceiling = {"GIT_CEILING_DIRECTORIES": str(plain.resolve())}
    inside = subprocess.run(["git", "-C", str(plain / "tools"), "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, env={**os.environ, **ceiling})
    if inside.returncode == 0:
        c.skip("from a plain directory, the copy holds the tools and none of the state",
               "git still finds a work tree around the temp dir")
    else:
        got = copied(plain / "tools", ceiling)
        c.ok(holds_tools_only(got), "from a plain directory (an exported tree), the copy holds the tools "
             "and none of the state", why(got))

    # The same tree untracked inside a repository whose .gitignore says nothing about it: git
    # lists every file in it, state included, so TOOLS_STATE has to filter what git returns.
    other = tempdir("palimpsest-tools-other-")
    git(other, "init", "-q", "-b", "main")
    shutil.copytree(src / "tools", other / "kept" / "tools")
    got = copied(other / "kept" / "tools")
    c.ok(holds_tools_only(got), "from a tools/ kept untracked in another repository, the copy holds the "
         "tools and none of the state", why(got))

    # An untracked nested repository in tools/ (a developer's scratch clone): git lists it as one
    # "sub/" entry, and copying that entry as a file raised IsADirectoryError.
    nest = tempdir("palimpsest-tools-nested-")
    git(nest, "init", "-q", "-b", "main")
    write(nest, "tools/a.py", "print(1)\n")
    git(nest, "add", "tools/a.py"); git(nest, "commit", "-q", "-m", "seed")
    git(nest / "tools", "init", "-q", "sub")
    write(nest, "tools/sub/f.py", "print(2)\n")
    dst = tempdir("palimpsest-tools-nested-dst-")
    try:
        _util.copy_tools(dst / "tools", nest / "tools")
        got = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file())
    except OSError as e:
        got = [f"{type(e).__name__}: {e}"]
    c.ok(got == ["tools/a.py", "tools/sub/f.py"], "an untracked nested repository in tools/ is copied as a "
         "tree (without its .git), not opened as a file", str(got))

    # This checkout's own tools/: exactly what is on disk and not ignored, whatever a past
    # setup.py, sync or rlm run left behind in it.
    on_disk = sorted(p.relative_to(_util.TOOLS_SRC).as_posix() for p in Path(_util.TOOLS_SRC).rglob("*")
                     if p.is_file() or p.is_symlink())
    r = subprocess.run(["git", "-C", str(_util.TOOLS_SRC), "check-ignore", "--stdin", "-z"], capture_output=True,
                       input=b"".join(os.fsencode(p) + b"\0" for p in on_disk))
    if r.returncode not in (0, 1):
        c.skip("from this checkout's tools/, the copy is exactly the files git does not ignore",
               "TOOLS_SRC is not in a git work tree; the plain-directory check covers it")
    else:
        ignored = {os.fsdecode(p) for p in r.stdout.split(b"\0") if p}
        expect = set(on_disk) - ignored
        t = make_vault() / "tools"
        have = {p.relative_to(t).as_posix() for p in t.rglob("*") if p.is_file() or p.is_symlink()}
        c.ok(have == expect, f"from this checkout's tools/ ({len(expect)} files), the copy is exactly "
             "the files git does not ignore",
             f"extra: {sorted(have - expect)}; missing: {sorted(expect - have)}")
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
