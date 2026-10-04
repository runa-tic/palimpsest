"""Regression checks for the 2026-10-02 review of the tools' stream setup.

Every entry point writes UTF-8 whatever the code page, because its callers read it as UTF-8. Since
c22bdff, stderr was reconfigured with an encoding alone, which also swaps its error handler from
backslashreplace to strict, and three entry points (config.py and both hooks) had no setup at all.

1. A surrogate in argv (a byte that is not UTF-8) gets argparse's usage error and exit 2: under
   "strict", `state.py lint --caf\\xe9` died printing that error, with a traceback and exit 1.
2. `config.py --check`, the launchers' pre-flight, prints its warning and exits 3 on a code page
   that lacks the warning's em dash (cp932, Latin-1). It crashed with exit 1, which the launchers
   read as "no problem", so a broken palimpsest.json was never mentioned.
3. The SessionStart hook writes its stderr (the same warning) as UTF-8, not in the code page.
4. A crashing tool's traceback with a non-ASCII path arrives on stderr as valid UTF-8, the path
   intact. This already held at c22bdff; it guards the encoding while the error handler changes.
5. Every entry point but rlm_worker.py (whose stdout is the worker protocol), after its own stream
   setup: a line and a traceback carrying a non-ASCII path and a surrogate arrive whole, as UTF-8.
"""
import codecs, json, os, re, subprocess, sys
from _util import Checks, copy_tools, make_vault, tempdir, write

BROKEN = '{"version": 1,,}\n'
NAME = "\u041f\u0440\u0438\u043c\u0435\u0440"          # "Пример": a word, not a user name
# Tools that set their streams up under `if __name__ == "__main__"`, so the sweep runs them as the
# script, with arguments that do no work. Every other entry point does it at import.
AS_MAIN = {"config.py": [], "embed.py": ["--help"], "redact.py": [], "setup.py": ["--help"],
           "triage_skills.py": ["--help"]}


def env_for(**extra) -> dict:
    """The child's environment without UTF-8 mode or a forced encoding, so the code page under test
    is the one it gets; the hooks' own guard is off unless a check turns it back on."""
    e = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    e["CLAUDE_BRAIN_NO_HOOK"] = "1"
    e.update(extra)
    return e


def utf8(b: bytes):
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return None


def stdout_codec(env: dict) -> str:
    r = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"], capture_output=True,
                       env=env)
    try:
        return codecs.lookup(r.stdout.decode("ascii", "replace").strip()).name
    except LookupError:
        return ""


def check_argv_surrogate(c: Checks):
    v = make_vault()
    if os.name == "nt":
        # argv arrives as UTF-16 on Windows, so no byte in it can fail to be UTF-8: the same lone
        # surrogate is put in sys.argv by a driver, which then runs the tool as the script.
        cmd = [sys.executable, "-c", "import runpy, sys\nsys.argv = ['tools/state.py', 'lint', '--caf\\udce9']\n"
                                     "runpy.run_path('tools/state.py', run_name='__main__')\n"]
    else:
        cmd = [sys.executable, str(v / "tools" / "state.py"), "lint", b"--caf\xe9"]
    # PYTHONUTF8=1 decodes argv as UTF-8 with surrogateescape whatever the locale, so the byte is a
    # surrogate under a cp1251 locale too (where it would otherwise read as a letter).
    r = subprocess.run(cmd, cwd=v, capture_output=True, env=env_for(PYTHONUTF8="1"), timeout=120)
    err = utf8(r.stderr)
    c.ok(r.returncode == 2 and err is not None and "usage:" in err and "unrecognized arguments" in err
         and "--caf\\udce9" in err and "Traceback" not in err,
         "1. `state.py lint --caf<surrogate>` gets argparse's usage error (exit 2), not a traceback",
         f"rc={r.returncode} stderr={r.stderr[-700:]!r}")


def check_config_check(c: Checks):
    v = make_vault()
    write(v, "palimpsest.json", BROKEN)
    # PYTHONIOENCODING runs everywhere: cp932 is what a Japanese Windows writes to the launcher's
    # `>nul`. The real locales run where the system has them.
    cases = [("PYTHONIOENCODING=cp932", env_for(PYTHONIOENCODING="cp932")),
             ("PYTHONIOENCODING=latin-1", env_for(PYTHONIOENCODING="latin-1"))]
    for loc, enc in (("ja_JP.SJIS", "shift_jis"), ("en_US.ISO8859-1", "latin-1")):
        e = env_for(LC_ALL=loc)
        if os.name == "nt" or stdout_codec(e) != codecs.lookup(enc).name:
            why = "LC_ALL does not pick the code page on Windows" if os.name == "nt" else f"no {loc} locale here"
            c.skip(f"2. config.py --check under LC_ALL={loc}", f"{why}; the PYTHONIOENCODING cases stand in")
            continue
        cases.append((f"LC_ALL={loc}", e))
    for label, e in cases:
        r = subprocess.run([sys.executable, str(v / "tools" / "config.py"), "--check"], cwd=v,
                           capture_output=True, env=e, timeout=120)
        out = utf8(r.stdout)
        c.ok(r.returncode == 3 and out is not None and "WARNING palimpsest.json is unreadable" in out
             and b"Traceback" not in r.stderr,
             f"2. config.py --check with a broken palimpsest.json prints its warning and exits 3 ({label})",
             f"rc={r.returncode} stdout={r.stdout[-300:]!r} stderr={r.stderr[-500:]!r}")


def check_hook_stderr(c: Checks):
    v = make_vault()
    write(v, "palimpsest.json", BROKEN)
    e = env_for(PYTHONIOENCODING="cp932", PALIMPSEST_MEMORY_DIR=str(v / "no-memory"))
    e.pop("CLAUDE_BRAIN_NO_HOOK")
    r = subprocess.run([sys.executable, str(v / "tools" / "hook_session_start.py")], cwd=v, capture_output=True,
                       env=e, timeout=300)
    err = utf8(r.stderr)
    try:
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        ctx = ""
    c.ok(r.returncode == 0 and "**Config:**" in ctx and err is not None and "WARNING" in err
         and "\u2014" in err and "\\u2014" not in err,
         "3. the SessionStart hook writes its stderr as UTF-8 (cp932 code page) and its JSON still parses",
         f"rc={r.returncode} stderr={r.stderr[-400:]!r} stdout={r.stdout[:200]!r}")


def check_traceback_path(c: Checks):
    v = tempdir() / f"{NAME} vault"
    # copy_tools, not a copytree of TOOLS_SRC: that carried a used clone's gitignored state (its
    # deny list, sync receipt and logs/) into this vault (review, 2026-10-02).
    copy_tools(v / "tools")
    write(v, "State/entities.json", '{"entities": {"box": {"hot": true},}}\n')     # trailing comma
    script = v / "tools" / "state.py"
    # cp1252 lacks Cyrillic: a tool writing in the code page would escape the path, or mangle it.
    r = subprocess.run([sys.executable, str(script), "show", "--hot"], cwd=v, capture_output=True,
                       env=env_for(PYTHONIOENCODING="cp1252"), timeout=120)
    err = utf8(r.stderr)
    c.ok(r.returncode == 1 and err is not None and "Traceback" in err and "JSONDecodeError" in err
         and str(script) in err,
         "4. a crashing tool's traceback with a non-ASCII path arrives on stderr as UTF-8, path intact",
         f"rc={r.returncode} stderr={r.stderr[-700:]!r}")


# Runs one tool's stream setup (at import, or as the script for AS_MAIN), then writes a line and
# raises, both carrying a non-ASCII path and a lone surrogate. No UTF8_STDIO here: it would set the
# very streams this checks. ASCII escapes only, so the driver's own source is never in question.
SWEEP = r"""
import os, runpy, sys
tool, how = sys.argv[1], sys.argv[2]
sys.argv = [tool, *sys.argv[3:]]
sys.path.insert(0, os.path.dirname(tool))
try:
    runpy.run_path(tool, run_name="__main__" if how == "main" else "_streams_probe")
except SystemExit:
    pass
except ImportError as e:
    os.write(1, ("\nNEEDS " + str(e.name or e)).encode("ascii", "backslashreplace"))
    os._exit(77)
p = os.path.join("10 Notes", "\u041f\u0440\u0438\u043c\u0435\u0440 caf\udce9.md")
print("\nOUT " + p, flush=True)
raise RuntimeError("cannot read " + p)
"""


def check_every_entry_point(c: Checks):
    v = make_vault()
    tools = [p for p in sorted((v / "tools").glob("*.py")) if p.name != "rlm_worker.py"
             and re.search(r'^if __name__ == "__main__":', p.read_text(encoding="utf-8"), re.M)]
    shown = os.path.join("10 Notes", f"{NAME} caf") + "\\udce9.md"       # the surrogate as its escape
    bad, ran = [], []
    for p in tools:
        how = "main" if p.name in AS_MAIN else "import"
        r = subprocess.run([sys.executable, "-c", SWEEP, str(p), how, *AS_MAIN.get(p.name, [])], cwd=v,
                           capture_output=True, input=b"", env=env_for(PYTHONIOENCODING="cp1252"), timeout=120)
        if r.returncode == 77:
            need = r.stdout.split(b"NEEDS ")[-1].decode("ascii", "replace")
            c.skip(f"5. {p.name}'s stream setup", f"it needs {need}, which this Python lacks")
            continue
        out, err = utf8(r.stdout), utf8(r.stderr)
        ran.append(p.name)
        if not (out is not None and f"OUT {shown}" in out and err is not None and "Traceback" in err
                and f"RuntimeError: cannot read {shown}" in err):
            bad.append(f"{p.name}: rc={r.returncode} stdout={r.stdout[-160:]!r} stderr={r.stderr[-240:]!r}")
    c.ok(len(ran) >= 15 and not bad,
         f"5. all {len(ran)} entry points write a non-ASCII path and a surrogate whole, as UTF-8, "
         "on stdout and in a traceback", "\n        ".join(bad) or f"only {ran}")


def main() -> int:
    c = Checks("review 2026-10-02: stream setup")
    for fn in (check_argv_surrogate, check_config_check, check_hook_stderr, check_traceback_path,
               check_every_entry_point):
        try:
            fn(c)
        except Exception as e:
            c.ok(False, f"{fn.__name__} raised", f"{type(e).__name__}: {e}")
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
