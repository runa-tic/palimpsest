"""Review of 2026-10-02, the suite runner (tests/run_all.py). Every check fails on 60b4bff.

1. a word that matches no script exits 2 and runs nothing (it ran 0 scripts and exited 0, green)
2. a path names its script: `tests/test_x.py` runs test_x.py, as `test_x` does
3. -h / --help prints the usage and runs nothing
4. a timed-out script says TIMEOUT and shows what it wrote by then, stdout and stderr
5. a runner started with -X utf8 reads each child in the code page the child writes

Each check runs the real run_all.main() over a dir of fake test scripts, so the suite is not
re-run from inside itself.
"""
import codecs, os, subprocess, sys, textwrap
from pathlib import Path
import _util
import run_all
from _util import Checks, REPO, write

# run_all with its script dir and time limit pointed elsewhere, so a check can use fake scripts
# and a timeout of seconds instead of ten minutes.
DRIVER = textwrap.dedent("""
    import sys; from pathlib import Path
    sys.path.insert(0, sys.argv[1]); import run_all
    run_all.HERE, run_all.LIMIT_S = Path(sys.argv[2]), int(sys.argv[3])
    sys.argv = ["run_all.py", *sys.argv[4:]]
    sys.exit(run_all.main())
""")
# A fake test that logs its own name, so a check can tell which scripts ran.
FAKE = textwrap.dedent("""
    from pathlib import Path
    with open(Path(__file__).parent / "ran.log", "a") as f:
        f.write(Path(__file__).name + "\\n")
    print("fake: 1/1 passed")
""")


def fakes(**scripts: str) -> Path:
    d = _util.tempdir("palimpsest-runall-")
    for name, body in scripts.items():
        write(d, f"test_{name}.py", body)
    return d


def drive(d: Path, *words: str, limit: int = 600, flags: tuple = (), env: dict | None = None):
    return subprocess.run([sys.executable, *flags, "-c", _util.UTF8_STDIO + DRIVER, str(REPO / "tests"), str(d),
                           str(limit), *words], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env)


def ran(d: Path) -> list[str]:
    log = d / "ran.log"
    names = log.read_text(encoding="utf-8").split() if log.exists() else []
    log.unlink(missing_ok=True)
    return names


def main() -> int:
    c = Checks("review 2026-10-02: run_all")
    d = fakes(alpha=FAKE, beta=FAKE)

    # 1. `run_all.py nosuchword` (a typo, an option) printed "0/0 scripts passed" and exited 0.
    for words in (["nosuchword"], ["alpha", "nosuchword"], ["--verbose"]):
        r = drive(d, *words)
        names = ran(d)
        c.ok(r.returncode == 2 and words[-1] in r.stderr and not names,
             f"1. {' '.join(words)!r} exits 2, names the word and runs nothing",
             f"rc={r.returncode} ran={names} out={(r.stdout + r.stderr)[-300:]!r}")

    # 2. A path, as a shell completes it, matched no script name.
    got = {}
    for word in ("tests/test_alpha.py", os.path.join("tests", "test_alpha.py"), "test_alpha"):
        r = drive(d, word)
        got[word] = (r.returncode, ran(d), "1/1 scripts passed" in r.stdout)
    c.ok(all(g == (0, ["test_alpha.py"], True) for g in got.values()),
         "2. a path to a script, or its stem, runs that script and only it", f"(rc, ran, 1/1) per word: {got}")

    # 3. --help was a word like any other: 0/0, exit 0, and no usage.
    for flag in ("-h", "--help"):
        r = drive(d, flag)
        names = ran(d)
        c.ok(r.returncode == 0 and run_all.__doc__.strip() in r.stdout and not names,
             f"3. {flag} prints the usage and runs nothing", f"rc={r.returncode} ran={names} out={r.stdout[-300:]!r}")

    # 4. A hung script printed a bare FAIL with nothing saying it had timed out, and its stderr
    # never reached the tail.
    slow = fakes(slow=textwrap.dedent("""
        import sys, time
        print("got as far as step 3", flush=True)
        print("stuck waiting on a lock", file=sys.stderr, flush=True)
        time.sleep(600)
    """))
    # 20 s, not 5: on a slow Windows machine with antivirus a fresh interpreter can take seconds
    # to print its first line.
    r = drive(slow, limit=20)
    c.ok(r.returncode == 1 and "TIMEOUT after 20s" in r.stdout and "got as far as step 3" in r.stdout
         and "stuck waiting on a lock" in r.stdout,
         "4. a timed-out script says TIMEOUT and shows its partial stdout and stderr", r.stdout[-500:])

    # 4b. Two failed scripts exited 2, the code a usage error exits with.
    two = fakes(alpha="import sys; print('FAIL  x'); sys.exit(1)\n", beta="import sys; sys.exit(1)\n")
    r = drive(two)
    c.ok(r.returncode == 1 and "0/2 scripts passed" in r.stdout,
         "4b. two failed scripts exit 1, not 2 (2 is a usage error)", f"rc={r.returncode} {r.stdout[-200:]!r}")

    # 5. -X utf8 does not reach the children: the runner read their code page as UTF-8, and a
    # Cyrillic failure detail came back as U+FFFD. Only a child writing something other than
    # UTF-8 can show it: the ANSI code page on a stock Windows, ru_RU.CP1251 elsewhere if installed.
    env = {k: val for k, val in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["LC_ALL"] = "ru_RU.CP1251"                           # Windows takes its code page from the system
    enc = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"], env=env,
                         capture_output=True).stdout.decode("ascii", "replace").strip()
    try:
        enc = codecs.lookup(enc).name
    except LookupError:
        enc = ""
    word = next((w for w in ("Пользователь", "café") if enc and enc != "utf-8" and _encodes(w, enc)), None)
    if word is None:
        c.skip("5. a runner started with -X utf8 reads a child in the child's code page",
               f"a child writes {enc or 'an unknown encoding'} here, so there is nothing to misread")
    else:
        cyr = fakes(cyr=f"import sys\nprint({ascii(word)})\nsys.exit(1)\n")
        r = drive(cyr, flags=("-X", "utf8"), env=env)
        c.ok(word in r.stdout, f"5. a runner started with -X utf8 reads a {enc} child as {enc}", r.stdout[-300:])
    return c.done()


def _encodes(text: str, enc: str) -> bool:
    try:
        text.encode(enc)
        return True
    except UnicodeEncodeError:
        return False


if __name__ == "__main__":
    sys.exit(main())
