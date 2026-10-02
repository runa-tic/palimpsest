"""Regressions for two fixes nothing pinned (review of the Windows port, 2026-10-02).

c7b601d made ask.py and bench_retrieval.py resolve `claude` through shutil.which, which honours
PATHEXT: a bare "claude" argv reaches claude.exe only, never npm's claude.cmd. Extraction check 15
pins that for extract_notes; reverting ask.py to the bare name kept the whole suite green. Each
check below runs the real call with a PATH on which a bare "claude" resolves to nothing and a
shutil.which that finds the stub, as it finds claude.cmd on Windows:

1. ask.py's answer path (main, in-process, on a one-note vault).
2. bench_retrieval.py's query generation (gen, the --gen path).
3. rlm.py's _claude, which every root and sub-agent call goes through.

4. Git for Windows defaults to core.symlinks=false. There a regular file replacing a committed
   symlink stages as M and keeps mode 120000, and the blob is the file's content: scan_secrets
   must read it like any other blob, not skip the link mode as it skips a submodule's. (Check 1 of
   test_review_findings_0930.py covers the other side: with real links the same edit stages as T.)

Checks 1-3 each fail with that tool's shutil.which call reverted to a bare "claude"; check 4 fails
if staged_blobs skips mode 120000. No model is called: the stub answers every call.
"""
import contextlib, io, json, os, shutil, subprocess, sys
from pathlib import Path
from _util import Checks, git, make_vault, run, stub, stub_path, tempdir, write

FAKE_KEY = "AKIA" + "QZXW" * 4                      # synthetic, AWS-shaped
REPLY = "STUB-ANSWER"
STUB = r'''import json, sys
prompt = sys.stdin.buffer.read().decode("utf-8", "replace")     # the tools send UTF-8, not the code page
if "retrieval benchmark" in prompt:                # bench_retrieval's GEN_PROMPT: one row for note 0
    print(json.dumps([{"id": 0, "en": "how do striped animals hide", "ru": "kak polosatye pryachutsya"}]))
else:
    print("%s")
''' % REPLY


def load(v: Path, name: str):
    """The vault's copy of a tool, imported fresh. Under quiet(): the tools reconfigure stdout and
    stderr to UTF-8 on import, which on a cp1251 console would leave this test writing UTF-8 to a
    runner that reads it in the code page."""
    sys.path.insert(0, str(v / "tools"))
    for m in ("ask", "bench_retrieval", "rlm", "state", "embed", name):
        sys.modules.pop(m, None)
    with quiet():
        return __import__(name)


@contextlib.contextmanager
def quiet():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


@contextlib.contextmanager
def only_which_finds(v: Path):
    """A bare "claude" finds nothing (PATH is one empty dir), as CreateProcess finds no claude.cmd;
    shutil.which returns the stub, as it returns claude.cmd through PATHEXT."""
    bindir = v / "fakebin"
    stub(bindir, "claude", STUB)
    exe = shutil.which("claude", path=stub_path(bindir))
    empty = tempdir("palimpsest-empty-path-")
    old_path, old_which = os.environ.get("PATH", ""), shutil.which
    os.environ["PATH"] = str(empty)
    shutil.which = lambda name, *a, **k: exe if name == "claude" else old_which(name, *a, **k)
    try:
        yield
    finally:
        os.environ["PATH"], shutil.which = old_path, old_which


def guard(c: Checks, what: str, fn):
    try:
        fn()
    except (Exception, SystemExit) as e:          # ask.main exits 1 when the CLI fails
        c.ok(False, what, f"{type(e).__name__}: {e}")


def main() -> int:
    c = Checks("review 2026-10-02: claude through shutil.which, symlinks as files")

    # 1. ask.py's answer path
    def t1():
        v = make_vault()
        write(v, "10 Notes/Zebra.md", "# Zebra\n\nzebras have stripes\n")
        ask = load(v, "ask")
        argv = sys.argv
        sys.argv = ["ask.py", "--mode", "lexical", "zebra stripes"]
        try:
            with only_which_finds(v), quiet() as (out, err):
                ask.main()
        finally:
            sys.argv = argv
        c.ok(REPLY in out.getvalue(), "1. ask.py answers through the claude shutil.which finds",
             (out.getvalue() + err.getvalue())[-300:])
    guard(c, "1. ask.py answers through the claude shutil.which finds", t1)

    # 2. bench_retrieval.py --gen
    def t2():
        v = make_vault()
        # gen() skips a note whose body is under 400 characters
        write(v, "10 Notes/Zebra.md", "# Zebra\n\n" + "Zebras have stripes that break up their outline. " * 12)
        bench = load(v, "bench_retrieval")
        with only_which_finds(v), quiet() as (out, err):
            bench.gen(1, 0)
        q = v / "tools" / "cache" / "bench-queries.jsonl"
        rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()] if q.exists() else []
        c.ok([(r["rel"], r["en"]) for r in rows] == [("10 Notes/Zebra.md", "how do striped animals hide")],
             "2. bench_retrieval --gen asks the claude shutil.which finds", f"{rows} {out.getvalue()[-300:]}")
    guard(c, "2. bench_retrieval --gen asks the claude shutil.which finds", t2)

    # 3. rlm._claude
    def t3():
        v = make_vault()
        rlm = load(v, "rlm")
        with only_which_finds(v):
            got = rlm._claude("a question", "m", 60)
        c.ok(got == REPLY, "3. rlm calls the claude shutil.which finds", repr(got))
    guard(c, "3. rlm calls the claude shutil.which finds", t3)

    # 4. core.symlinks=false: a file over a committed link is M, mode 120000, and scanned
    def t4():
        v = make_vault()
        git(v, "config", "core.symlinks", "false")      # Git for Windows' default; every OS runs it here
        write(v, "10 Notes/real.md", "note\n")
        # Committed straight into the index, which needs no symlink rights on any platform.
        target = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=v, input="real.md",
                                capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout.strip()
        git(v, "add", "-A")
        git(v, "update-index", "--add", "--cacheinfo", f"120000,{target},10 Notes/link.md")
        git(v, "commit", "-q", "-m", "seed")
        (v / "10 Notes" / "link.md").unlink(missing_ok=True)
        write(v, "10 Notes/link.md", f"key {FAKE_KEY}\n")
        git(v, "add", "-A")
        st_line = git(v, "diff", "--cached", "--name-status").stdout.strip()
        mode = git(v, "ls-files", "-s", "--", "10 Notes/link.md").stdout.split(" ")[0]
        r = run(v, "scan_secrets.py")
        c.ok(st_line == "M\t10 Notes/link.md" and mode == "120000" and r.returncode == 1 and "[HIGH]" in r.stdout,
             "4. core.symlinks=false: a file replacing a link stages as M, mode 120000, and its key blocks the commit",
             f"{st_line!r} mode={mode} rc={r.returncode} / {r.stdout.strip()[-300:]}")
    guard(c, "4. core.symlinks=false: a file replacing a link stages as M, mode 120000, and its key blocks the commit", t4)

    return c.done()


if __name__ == "__main__":
    sys.exit(main())
