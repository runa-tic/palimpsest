"""Regressions for the 2026-09-30 review of rlm.py / rlm_worker.py (all fail on ba54bc9).

1. A root-model CLI failure (or a "[stopped: ...]" run) was taken as the FINAL answer: logged to
   the Q&A log with --log, recorded as "final" in the trace, and the run exited 0.
2. A FINAL answer quoting a plain ``` block was executed as REPL code and thrown away; ```Python3
   was not recognised as code; the forced-FINAL last step still ran code.
3. Threads calling rlm() at once got each other's sub-agent results and overran the budget.
4. exit()/sys.exit() in model code killed the worker, and a dead worker crashed rlm.py with a
   traceback instead of ending the run.
5. One symlinked note pointing outside the vault made docs.search/grep/chunks/filter fail.
6. The audit hook could be bypassed by rebinding rlm_worker's module globals (Path, os), builtins
   it used, or its closure cells through the traceback frame of an error it raised; on macOS the OS
   profile left /private/tmp and $TMPDIR readable.

Second round (the rework was rejected):
4b. The builtin exit()/quit() close sys.stdin before raising SystemExit, so the worker survived the
    step and died on its next read ("I/O operation on closed file"); a stray write to
    sys.__stdout__ went into the frame stream.
6b. With no OS sandbox, setting posixpath.os (whose lookups os.path.realpath makes at call time)
    still passed every read and write check; the resolver is now built from bound C functions.
7.  The allow-list profile broke sqlite3/lzma/ssl/_decimal in a venv on Homebrew Python (their
    dylibs live under /opt/homebrew/opt), and sysconfig "data" made all of /opt/homebrew readable.
8.  An untagged ``` code step was taken for the answer; a bare FINAL was recorded as an empty one.

Every model call is stubbed: either rlm._claude is replaced in a driver, or a fake `claude`
executable that never talks to anything is put first on PATH.
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from _util import TOOLS_SRC, Checks, make_vault, write

DRIVER = r'''
import os, sys, json, time, importlib.util
spec = importlib.util.spec_from_file_location("rlm", sys.argv[1])
rlm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rlm)
replies = iter(json.loads(sys.argv[2]))
def fake(prompt, model, timeout):
    if "===TEXT===" in prompt:                     # a sub-agent: echo its text back
        time.sleep(0.05)
        return "ECHO " + prompt.split("===TEXT===\n", 1)[1]
    return next(replies)
rlm._claude = fake
if os.environ.get("RLM_TEST_EXEC_TIMEOUT"):
    rlm.EXEC_TIMEOUT = float(os.environ["RLM_TEST_EXEC_TIMEOUT"])
sys.argv = ["rlm.py", "q", *sys.argv[3:]]
sys.exit(rlm.main())
'''


_MINE: list[Path] = []


def vault() -> Path:
    v = make_vault()
    _MINE.append(v)
    return v


def drive(v: Path, replies: list[str], *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", DRIVER, str(v / "tools" / "rlm.py"), json.dumps(replies), *args],
                          cwd=v, capture_output=True, text=True, timeout=120, env={**os.environ, **(env or {})})


def qa_log(v: Path) -> str:
    p = v / "40 Resources" / "Brain Q&A Log.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def traces(v: Path) -> list[dict]:
    out = []
    for f in sorted((v / "tools" / "logs" / "rlm").glob("*.jsonl")):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def check_root_failure(c: Checks) -> None:
    # The real _claude, against a fake `claude` that fails like a rate limit and counts its calls.
    v = vault()
    write(v, "10 Notes/n.md", "a note\n")
    fakebin = v / "fakebin"; fakebin.mkdir()
    calls = v / "calls.txt"
    if os.name == "nt":
        (fakebin / "claude.cmd").write_text(f'@echo x>>"{calls}"\r\n@echo rate limited (529) 1>&2\r\n@exit /b 1\r\n')
    else:
        exe = fakebin / "claude"
        exe.write_text(f"#!/bin/sh\necho x >> '{calls}'\necho 'rate limited (529)' >&2\nexit 1\n")
        exe.chmod(0o755)
    r = subprocess.run([sys.executable, str(v / "tools" / "rlm.py"), "--log", "q"], cwd=v, capture_output=True,
                       text=True, timeout=120, env={**os.environ, "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"})
    ncalls = len(calls.read_text().splitlines()) if calls.exists() else 0
    failed_ok = (r.returncode != 0 and "claude CLI failed" not in qa_log(v) and ncalls == 2
                 and not any(t["t"] == "final" for t in traces(v)))
    # A step-budget stop and an EXEC_TIMEOUT stop are not answers either.
    v2 = vault()
    write(v2, "10 Notes/n.md", "a note\n")
    r2 = drive(v2, ["```python\nx = 1\n```", "```python\nx = 2\n```"], "--steps", "2", "--log")
    # A step that runs past EXEC_TIMEOUT (shortened to 2s here) is killed and the run stops, unlogged.
    v3 = vault()
    write(v3, "10 Notes/n.md", "a note\n")
    r3 = drive(v3, ["```python\nwhile True: pass\n```", "FINAL\nnot reached"], "--steps", "3", "--log",
               env={"RLM_TEST_EXEC_TIMEOUT": "2"})
    t3 = [x["t"] for x in traces(v3)]
    timeout_ok = r3.returncode != 0 and "timeout" in t3 and qa_log(v3) == "" and "not reached" not in r3.stdout
    c.ok(failed_ok and r2.returncode != 0 and "[stopped" not in qa_log(v2) and timeout_ok,
         "a root CLI failure / step-budget stop / EXEC_TIMEOUT stop is not logged as an answer, and exits non-zero",
         f"rc={r.returncode} calls={ncalls} log={qa_log(v)[-200:]!r} rc2={r2.returncode} "
         f"log2={qa_log(v2)[-200:]!r} rc3={r3.returncode} t3={t3} {(r.stdout + r.stderr)[-300:]}")


def check_final_with_fence(c: Checks) -> None:
    v = vault()
    write(v, "10 Notes/n.md", "a note\n")
    final = "FINAL\nCheck the processes per [[n]]:\n```\npm2 jlist\n```\nThat is all."
    r = drive(v, ["```Python3\nprint('RAN', 1 + 1)\n```", final], "--steps", "3", "--log")
    out = r.stdout + r.stderr
    ok = r.returncode == 0 and "RAN 2" in out and "pm2 jlist" in qa_log(v) and "SyntaxError" not in out
    # The forced-FINAL last step never executes code.
    v2 = vault()
    write(v2, "10 Notes/n.md", "a note\n")
    r2 = drive(v2, ["```python\nprint('LAST' + 'RAN')\n```"], "--steps", "1")
    # Without FINAL an untagged fence is still a code step (not an answer for the log); a fence in
    # another language is neither code nor an answer; a bare FINAL is not an answer either: the next
    # step carries it.
    v3 = vault()
    write(v3, "10 Notes/n.md", "a note\n")
    r3 = drive(v3, ["```\nprint('PLAIN' + 'RAN')\n```", "```bash\necho no\n```\nand\n```bash\necho no\n```",
                    "FINAL", "FINAL\nthe real answer"], "--steps", "6", "--log")
    out3 = r3.stdout + r3.stderr
    t3 = [x["t"] for x in traces(v3)]
    plain_ok = (r3.returncode == 0 and "PLAINRAN" in out3 and "the real answer" in qa_log(v3)
                and "print(" not in qa_log(v3) and "echo no" not in qa_log(v3) and "empty_final" in t3
                and "no_code" in t3 and t3.count("final") == 1)
    c.ok(ok and "LASTRAN" not in r2.stdout + r2.stderr and plain_ok,
         "a FINAL quoting a plain ``` block is the answer; ```Python3 and untagged fences are code; "
         "a bare FINAL is not an answer; the last step runs none",
         f"rc={r.returncode} log={qa_log(v)[-200:]!r} {out[-400:]} | {(r2.stdout + r2.stderr)[-200:]} | "
         f"rc3={r3.returncode} t3={t3} log3={qa_log(v3)[-200:]!r}")


def check_threads(c: Checks) -> None:
    v = vault()
    write(v, "10 Notes/n.md", "a note\n")
    code = ("from concurrent.futures import ThreadPoolExecutor\n"
            "texts = [f'T{i}' for i in range(10)]\n"
            "with ThreadPoolExecutor(6) as ex:\n"
            "    res = list(ex.map(lambda t: rlm('echo', t), texts))\n"
            "ok = sum(r == 'ECHO ' + t for r, t in zip(res, texts))\n"
            "bad = sum(r.startswith('ECHO') and r != 'ECHO ' + t for r, t in zip(res, texts))\n"
            "print('RESULT', ok, bad, budget()['subagents_used'])\n")
    r = drive(v, [f"```python\n{code}```", "FINAL\ndone"], "--steps", "3", "--subagents", "5")
    out = r.stdout + r.stderr
    c.ok("RESULT 5 0 5" in out and "sub-agents: 5/5" in out,
         "concurrent rlm() calls get their own results and stay within the sub-agent budget", out[-500:])


def check_worker_exit(c: Checks) -> None:
    v = vault()
    write(v, "10 Notes/n.md", "a note\n")
    # The builtin exit()/quit() (site.Quitter) close sys.stdin before raising SystemExit; the
    # namespace (x) must survive each of them, and a write to sys.__stdout__ must not be taken for
    # a protocol frame.
    r = drive(v, ["```python\nx = 42\nexit()\n```", "```python\nprint('STILL', x)\nquit()\n```",
                  "```python\nimport sys\nprint('NOISE', file=sys.__stdout__, flush=True)\nsys.exit(0)\n```",
                  "```python\nprint('ALIVE', x, len(docs))\n```",
                  "```python\nimport os\nos._exit(3)\n```", "FINAL\nnot reached"], "--steps", "7", "--log")
    out = r.stdout + r.stderr
    t = [x["t"] for x in traces(v)]
    c.ok("STILL 42" in out and "ALIVE 42 1" in out and "Traceback" not in out and r.returncode != 0
         and t.count("worker_died") == 1 and t.index("worker_died") > t.index("output") and t.count("output") == 4
         and "not reached" not in qa_log(v),
         "exit()/quit()/sys.exit() in a step are step errors that keep the REPL; a dead worker ends the run "
         "cleanly, non-zero", f"{t} {out[-700:]}")


def check_symlink(c: Checks) -> None:
    v = vault()
    ext = Path(tempfile.mkdtemp(prefix="palimpsest-ext-"))
    try:
        (ext / "shared.md").write_text("OUTSIDE-TEXT alpha\n")
        write(v, "10 Notes/a.md", "alpha inside\n")
        (v / "10 Notes" / "shared.md").symlink_to(ext / "shared.md")
        code = ("print('HITS', len(docs.search('alpha')), len(docs.grep('alpha')), len(docs.chunks()),\n"
                "      any('OUTSIDE-TEXT' in ch for ch in docs.chunks()))\n")
        r = drive(v, [f"```python\n{code}```", "FINAL\ndone"], "--steps", "3")
        out = r.stdout + r.stderr
        c.ok("HITS 1 1 1 False" in out, "a note symlinked outside the vault is skipped, not fatal to the corpus",
             out[-500:])
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def check_hook_and_profile(c: Checks) -> None:
    v = vault()
    write(v, "10 Notes/n.md", "a note\n")
    outside = Path(tempfile.mkdtemp(prefix="palimpsest-secret-"))
    try:
        secret = outside / "secret.txt"
        secret.write_text("TOPSECRET-VALUE\n")
        evil = v / "tools" / "evil.py"
        rebind = ("import sys\nm = sys.modules['__main__']\n"
                  "class P:\n"
                  "    def __init__(self, *a): pass\n"
                  "    def resolve(self): return self\n"
                  "    def __eq__(self, o): return True\n"
                  "    parents = ()\n"
                  "m.Path = P\n"
                  f"try:\n    print('LEAK', open({str(secret)!r}).read())\nexcept Exception as e: print('E1', e)\n"
                  f"try:\n    open({str(evil)!r}, 'w').write('x')\nexcept Exception as e: print('E2', e)\n")
        builtins_ = ("import builtins\nbuiltins.any = lambda it: True\n"
                     f"try:\n    print('LEAK', open({str(secret)!r}).read())\nexcept Exception as e: print('E3', e)\n")
        # The hook's own frame, reached through the traceback of an error it raised: its rules must
        # not be writable through f_locals (closure cells are, in 3.13), nor its helper's defaults.
        frame = (f"try:\n    open({str(secret)!r})\nexcept PermissionError as e:\n"
                 "    tb = e.__traceback__\n"
                 "    while tb.tb_next: tb = tb.tb_next\n"
                 "    loc = tb.tb_frame.f_locals\n"
                 "    for k in list(loc):\n"
                 "        v = loc[k]\n"
                 "        if isinstance(v, tuple) and v and all(isinstance(x, (str, Path)) for x in v):\n"
                 "            try: loc[k] = (Path('/'),) if isinstance(v[0], Path) else ('/',)\n"
                 "            except Exception as x: print('E4', x)\n"
                 "    for f in [x for x in loc.values() if callable(x) and getattr(x, '__name__', '') == 'within']:\n"
                 "        try: f.__defaults__ = tuple(f.__defaults__)\n"
                 "        except Exception as x: print('E5', x)\n"
                 f"try:\n    print('LEAK', open({str(secret)!r}).read())\nexcept Exception as e: print('E6', e)\n")
        # The path module's own globals: os.path.realpath looks up os.fspath/os.lstat/os.getcwd and
        # isinstance in posixpath's (ntpath's) dict at call time. Every one of them, and the os
        # functions themselves, is rebound to point into scratch before the attempts.
        evil2 = v / "tools" / "evil2.py"
        pathmod = ("import os as _os, builtins\npm = _os.path\nS = str(VAULT) + '/tools/.rlm_scratch/x'\n"
                   "class FakeOS:\n"
                   "    def __getattr__(self, k): return getattr(_os, k)\n"
                   "    @staticmethod\n"
                   "    def fspath(p): return S\n"
                   "attacks = [[(pm, 'os', FakeOS())],\n"
                   "           [(pm, 'realpath', lambda p, *a, **k: S), (pm, 'abspath', lambda p: S),\n"
                   "            (pm, 'normpath', lambda p: S), (_os, 'fspath', lambda p: S), (_os, 'getcwd', lambda: S),\n"
                   "            (_os, 'readlink', lambda *a, **k: S), (_os, 'lstat', lambda *a, **k: _os.stat(S))],\n"
                   "           [(builtins, 'isinstance', lambda *a: False), (builtins, 'str', lambda *a: S)]]\n"
                   "for n, attack in enumerate(attacks):\n"
                   "    saved = [(m, k, getattr(m, k)) for m, k, _ in attack]\n"
                   "    for m, k, f in attack: setattr(m, k, f)\n"
                   "    try:\n"
                   f"        try: print('LEAK', open({str(secret)!r}).read())\n"
                   "        except Exception as e: print('E7', n, e)\n"
                   f"        try: open({str(evil2)!r}, 'w').write('x'); print('WRO' + 'TE', n)\n"
                   "        except Exception as e: print('E8', n, e)\n"
                   "    finally:\n"
                   "        for m, k, f in saved: setattr(m, k, f)   # the worker's own json needs builtins back\n")
        r = drive(v, [f"```python\n{rebind}```", f"```python\n{builtins_}```", f"```python\n{frame}```",
                      f"```python\n{pathmod}```", "FINAL\ndone"], "--steps", "6", env={"RLM_OS_SANDBOX": "0"})
        out = r.stdout + r.stderr
        hook_ok = ("TOPSECRET-VALUE" not in out and not evil.exists() and not evil2.exists() and "WROTE" not in out
                   and all(f"E{k} {n} blocked" in out for k in (7, 8) for n in range(3)))
        detail = out[-600:]
        prof_ok = True
        if sys.platform == "darwin":
            # A bare interpreter under the generated profile: only the kernel can refuse.
            sys.path.insert(0, str(v / "tools"))
            import importlib
            rlm = importlib.import_module("rlm")
            scratch = v / "tools" / ".rlm_scratch"; scratch.mkdir(parents=True, exist_ok=True)
            tmpd = Path(tempfile.mkdtemp(prefix="palimpsest-tmp-", dir="/private/tmp"))
            try:
                (tmpd / "s.txt").write_text("TMP-SECRET\n")
                leaks = []
                for f in (secret, tmpd / "s.txt"):      # $TMPDIR (/private/var/folders) and /private/tmp
                    rr = subprocess.run(rlm._os_sandbox([sys.executable, "-c", f"print(open({str(f)!r}).read())"],
                                                        scratch)[0], capture_output=True, text=True, timeout=60, cwd=v)
                    leaks.append(rr.returncode == 0 or "SECRET" in rr.stdout)
                rr = subprocess.run(rlm._os_sandbox([sys.executable, "-c",
                                                     f"print(open({str(v / '10 Notes' / 'n.md')!r}).read())"],
                                                    scratch)[0], capture_output=True, text=True, timeout=60, cwd=v)
                prof_ok = not any(leaks) and rr.returncode == 0 and "a note" in rr.stdout
                detail += f" | leaks={leaks} vault_rc={rr.returncode} {rr.stderr[-200:]}"
            finally:
                shutil.rmtree(tmpd, ignore_errors=True)
        c.ok(hook_ok and prof_ok, "rebinding the worker's or the path module's globals, or builtins, does not "
             "open the hook; the macOS profile denies /private/tmp and $TMPDIR", detail)
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def _worker_funcs(*names: str) -> dict:
    """Pull top-level functions out of the tested rlm_worker.py without importing it (importing runs
    the worker: it takes over the process's stdin/stdout)."""
    import ast
    src = TOOLS_SRC / "rlm_worker.py"
    ns: dict = {"os": os}
    for n in ast.parse(src.read_text(encoding="utf-8")).body:
        if isinstance(n, ast.FunctionDef) and n.name in names:
            exec(compile(ast.Module([n], []), str(src), "exec"), ns)
    return ns


def check_resolver(c: Checks) -> None:
    """The hook's own path resolver agrees with os.path.realpath on POSIX (links, .., loops, dangling
    links, relative paths) and, on a simulated Windows file table, with what Win32 would open."""
    ns = _worker_funcs("_real_posix", "_nt_final", "_real_nt")
    if "_real_posix" not in ns or "_real_nt" not in ns:
        c.ok(False, "the audit hook resolves paths with its own bound resolver", "no _real_posix/_real_nt")
        return
    bad = []
    if os.name != "nt":
        t = Path(tempfile.mkdtemp(prefix="palimpsest-res-"))
        cwd = os.getcwd()
        try:
            (t / "a" / "b" / "c").mkdir(parents=True)
            os.symlink(t / "a" / "b", t / "lnk"); os.symlink("../..", t / "a" / "b" / "c" / "up")
            os.symlink("c/up/../x", t / "a" / "b" / "rel"); os.symlink("/nonexistent-rlm/zz", t / "dang")
            os.symlink("loop2", t / "loop1"); os.symlink("loop1", t / "loop2")
            os.chdir(t / "a")
            for case in [str(t), f"{t}/lnk", f"{t}/lnk/c", f"{t}/lnk/../x", f"{t}/a/b/c/up/y", f"{t}/a/b/rel/q",
                         "b/c", "../lnk/c/../..", ".", "/", "//x", "/tmp/../etc", f"{t}/dang/f", f"{t}/nope/../a"]:
                if ns["_real_posix"](case) != os.path.realpath(case):
                    bad.append((case, ns["_real_posix"](case), os.path.realpath(case)))
            try:
                ns["_real_posix"](f"{t}/loop1/x")
                bad.append("symlink loop resolved")
            except ValueError:
                pass
        finally:
            os.chdir(cwd)
            shutil.rmtree(t, ignore_errors=True)
    # Windows, simulated: a canonical-case table of what exists, with one junction out of the vault.
    exist = {x.lower(): x for x in ["C:", r"C:\Users", r"C:\Users\rn", r"C:\Users\alice\vault",
                                     r"C:\Users\alice\vault\tools", r"C:\Users\alice\vault\tools\.rlm_scratch",
                                     "D:", r"D:\Secret", r"\\srv\share", r"\\srv\share\d"]}

    def final(path):
        k = path.rstrip("\\").lower()
        if k == r"c:\users\alice\vault\link" or k.startswith("c:\\users\\alice\\vault\\link\\"):
            k = ("d:\\secret" + k[len(r"c:\users\alice\vault\link"):])
        if k not in exist:
            raise FileNotFoundError(path)
        x = exist[k]
        return "\\\\?\\UNC\\" + x[2:] if x.startswith("\\\\") else "\\\\?\\" + x + ("\\" if len(x) == 2 else "")

    want = {r"c:/users/ALICE/vault/tools/.rlm_scratch/x": r"C:\Users\alice\vault\tools\.rlm_scratch\x",
            r"tools\.rlm_scratch\..\..\x": r"C:\Users\alice\vault\x",
            r"..\..\..\..\Windows": r"C:\Windows",
            r"link\s.txt": r"D:\Secret\s.txt",
            r"\Users\alice\vault\a": r"C:\Users\alice\vault\a",
            r"C:x.md": r"C:\Users\alice\vault\x.md",
            r"\\?\UNC\srv\share\d\e": r"\\srv\share\d\e",
            r"\\srv\share\d\..\f": r"\\srv\share\f",
            r"vault.\x": ValueError, r"D:x": ValueError, r"\\.\PhysicalDrive0": ValueError,
            r"\\?\C:\Users\alice\vault\..\x": ValueError}
    for case, exp in want.items():
        try:
            got = ns["_real_nt"](case, _final=final, _getcwd=lambda: r"C:\Users\alice\vault")
        except ValueError:
            got = ValueError
        if got != exp:
            bad.append((case, got, exp))
    c.ok(not bad, "the hook's bound resolver matches realpath on POSIX and Win32 semantics on a simulated table",
         repr(bad)[:600])


def check_profile_libs(c: Checks) -> None:
    """macOS only: under the generated profile a venv on this Python still imports the stdlib modules
    that link dylibs from outside the install (sqlite3, lzma, ssl, _decimal on Homebrew), and the
    rest of the Homebrew prefix (e.g. bin/brew) stays unreadable."""
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        c.ok(True, "profile libraries (skipped: no sandbox-exec)")
        return
    v = vault()
    t = Path(tempfile.mkdtemp(prefix="palimpsest-venv-"))
    try:
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(t / "venv")], check=True,
                       capture_output=True, timeout=120)
        scratch = v / "tools" / ".rlm_scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        probe = ("import sys, subprocess\nsys.path.insert(0, sys.argv[1])\nimport rlm\nfrom pathlib import Path\n"
                 "s = Path(sys.argv[2])\n"
                 "code = 'import sqlite3, lzma, ssl, _decimal, bz2, zlib, hashlib, ctypes; print(\"IMPORTS-OK\")'\n"
                 "r = subprocess.run(rlm._os_sandbox([sys.executable, '-c', code], s)[0], capture_output=True, "
                 "text=True, cwd=s)\nprint(r.stdout.strip(), r.returncode, r.stderr[-300:])\n"
                 "if Path('/opt/homebrew/bin/brew').exists():\n"
                 "    r = subprocess.run(rlm._os_sandbox([sys.executable, '-c', \"open('/opt/homebrew/bin/brew').read()\"],"
                 " s)[0], capture_output=True, text=True, cwd=s)\n"
                 "    print('BREW-READ', r.returncode)\n")
        outs = []
        for py in (sys.executable, str(t / "venv" / "bin" / "python")):
            r = subprocess.run([py, "-c", probe, str(v / "tools"), str(scratch)], capture_output=True, text=True,
                               timeout=120, cwd=scratch)
            outs.append(r.stdout + r.stderr)
        c.ok(all("IMPORTS-OK 0" in o and "BREW-READ 0" not in o for o in outs),
             "the macOS profile keeps stdlib extension imports working in a venv, and /opt/homebrew closed",
             " | ".join(o[-300:] for o in outs))
    finally:
        shutil.rmtree(t, ignore_errors=True)


def check_parent_budget(c: Checks) -> None:
    """9. The sub-agent cap is the parent's: raising the worker's own _BUDGET from model code does not
    buy calls past --subagents (the stub counts every sub-agent it is asked to run)."""
    v = vault()
    write(v, "10 Notes/n.md", "a note\n")
    code = ("import sys\nsys.modules['__main__']._BUDGET['limit'] = 1000\n"
            "res = rlm_map('echo', [f'T{i}' for i in range(10)])\n"
            "print('RAN', sum(r.startswith('ECHO') for r in res), 'DROPPED', sum('NOT RUN' in r for r in res))\n")
    r = drive(v, [f"```python\n{code}```", "FINAL\ndone"], "--steps", "3", "--subagents", "3")
    out = r.stdout + r.stderr
    n = sum(x.get("n", 0) for x in traces(v) if x["t"] == "subagents")
    c.ok("RAN 3 DROPPED 7" in out and n == 3 and "sub-agents: 3/3" in out,
         "the parent caps sub-agent calls at --subagents even when model code raises the worker's budget",
         f"n={n} {out[-400:]}")


def main() -> int:
    c = Checks("review 2026-09-30: rlm")
    try:
        check_root_failure(c)
        check_final_with_fence(c)
        check_threads(c)
        check_worker_exit(c)
        check_symlink(c)
        check_hook_and_profile(c)
        check_resolver(c)
        check_profile_libs(c)
        check_parent_budget(c)
    finally:
        for d in _MINE:
            shutil.rmtree(d, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
