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

Every model call is stubbed: either rlm._claude is replaced in a driver, or a fake `claude`
executable that never talks to anything is put first on PATH.
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from _util import Checks, make_vault, write

DRIVER = r'''
import sys, json, time, importlib.util
spec = importlib.util.spec_from_file_location("rlm", sys.argv[1])
rlm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rlm)
replies = iter(json.loads(sys.argv[2]))
def fake(prompt, model, timeout):
    if "===TEXT===" in prompt:                     # a sub-agent: echo its text back
        time.sleep(0.05)
        return "ECHO " + prompt.split("===TEXT===\n", 1)[1]
    return next(replies)
rlm._claude = fake
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
    c.ok(failed_ok and r2.returncode != 0 and "[stopped" not in qa_log(v2),
         "a root CLI failure / step-budget stop is not logged as an answer, and the run exits non-zero",
         f"rc={r.returncode} calls={ncalls} log={qa_log(v)[-200:]!r} rc2={r2.returncode} "
         f"log2={qa_log(v2)[-200:]!r} {(r.stdout + r.stderr)[-300:]}")


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
    c.ok(ok and "LASTRAN" not in r2.stdout + r2.stderr,
         "a FINAL quoting a plain ``` block is the answer; ```Python3 is code; the last step runs none",
         f"rc={r.returncode} log={qa_log(v)[-200:]!r} {out[-400:]} | {(r2.stdout + r2.stderr)[-200:]}")


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
    r = drive(v, ["```python\nimport sys\nsys.exit(0)\n```", "```python\nprint('ALIVE', len(docs))\n```",
                  "```python\nimport os\nos._exit(3)\n```", "FINAL\nnot reached"], "--steps", "5", "--log")
    out = r.stdout + r.stderr
    t = [x["t"] for x in traces(v)]
    c.ok("ALIVE 1" in out and "Traceback" not in out and r.returncode != 0 and "worker_died" in t
         and "not reached" not in qa_log(v),
         "sys.exit() in a step is a step error; a dead worker ends the run cleanly, non-zero", f"{t} {out[-500:]}")


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
        r = drive(v, [f"```python\n{rebind}```", f"```python\n{builtins_}```", f"```python\n{frame}```",
                      "FINAL\ndone"], "--steps", "5", env={"RLM_OS_SANDBOX": "0"})
        out = r.stdout + r.stderr
        hook_ok = "TOPSECRET-VALUE" not in out and not evil.exists() and out.count("blocked by the RLM sandbox") >= 4
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
        c.ok(hook_ok and prof_ok, "rebinding the worker's globals or builtins does not open the hook; "
             "the macOS profile denies /private/tmp and $TMPDIR", detail)
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def main() -> int:
    c = Checks("review 2026-09-30: rlm")
    try:
        check_root_failure(c)
        check_final_with_fence(c)
        check_threads(c)
        check_worker_exit(c)
        check_symlink(c)
        check_hook_and_profile(c)
    finally:
        for d in _MINE:
            shutil.rmtree(d, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
