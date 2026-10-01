"""The RLM REPL must not read outside the vault, even when model-written code attacks the check.

Review P1 (2026-09-30): the audit hook read its allowed roots from module globals, so generated
code could run `sys.modules["__main__"]._READ_ROOTS = (Path("/"),)` and then read any file, which
rlm() could send to the API. Two layers now, tested separately:
  1. in-process: the hook's rules are bound in a closure (rebinding the module does nothing) and
     gc.* is blocked (it is how code would find the hook to edit it) — tested with the OS sandbox
     off, so this layer stands on its own;
  2. OS (macOS): rlm.py runs the worker under sandbox-exec. Tested with a bare interpreter under
     the generated profile — no audit hook at all — so only the kernel can refuse.
The secret file lives under the home directory, which is what the OS profile protects.
"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path
import _util
from _util import Checks, make_vault, write

DRIVER = r'''
import sys, importlib.util
spec = importlib.util.spec_from_file_location("rlm", sys.argv[1])
rlm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rlm)
secret = sys.argv[2]
code = ("import sys\nfrom pathlib import Path\n"
        "m = sys.modules['__main__']\n"
        "m._READ_ROOTS = (Path('/'),); m._within = lambda *a: True; m._BLOCK_PREFIX = ()\n"
        f"print('LEAK', open({secret!r}).read())\n")
replies = iter([f"```python\n{code}```", "```python\nimport gc\nprint(len(gc.get_objects()))\n```",
                "FINAL\ndone"])
rlm._claude = lambda prompt, model, timeout: next(replies)
sys.argv = ["rlm.py", "q", "--steps", "4"]
rlm.main()
'''


def main() -> int:
    c = Checks("rlm: sandbox escape")
    v = make_vault()
    write(v, "10 Notes/n.md", "a note\n")
    home_tmp = Path(tempfile.mkdtemp(prefix="palimpsest-secret-", dir=Path.home() / "Library" / "Caches")
                    if sys.platform == "darwin" else tempfile.mkdtemp(prefix="palimpsest-secret-"))
    secret = home_tmp / "secret.txt"
    secret.write_text("TOPSECRET-VALUE\n")
    try:
        r = subprocess.run([sys.executable, "-c", DRIVER, str(v / "tools" / "rlm.py"), str(secret)], cwd=v,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env={**os.environ, "RLM_OS_SANDBOX": "0"})
        out = r.stdout + r.stderr
        c.ok("TOPSECRET-VALUE" not in out and "blocked by the RLM sandbox" in out,
             "in-process hook: rebinding its globals does not open reads outside the vault", out[-700:])
        c.ok("gc.get_objects" in out and "blocked" in out, "in-process hook: gc introspection is blocked", out[-400:])

        if sys.platform != "darwin":
            print("SKIP: OS sandbox checks are macOS-only (sandbox-exec)")
            return c.done()
        sys.path.insert(0, str(v / "tools"))
        import importlib
        rlm = importlib.import_module("rlm")
        scratch = v / "tools" / ".rlm_scratch"; scratch.mkdir(parents=True, exist_ok=True)
        wrap = lambda code: rlm._os_sandbox([sys.executable, "-c", code], scratch)[0]
        r = subprocess.run(wrap(f"print(open({str(secret)!r}).read())"), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        c.ok(r.returncode != 0 and "TOPSECRET-VALUE" not in r.stdout and "Operation not permitted" in r.stderr,
             "OS sandbox: a bare interpreter (no hook) cannot read a file under the home dir", r.stderr[-300:])
        r = subprocess.run(wrap(f"print(open({str(v / '10 Notes' / 'n.md')!r}).read())"), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60, cwd=v)     # `-c` puts the cwd on sys.path; the worker runs from the vault
        c.ok(r.returncode == 0 and "a note" in r.stdout, "OS sandbox: the vault stays readable", r.stderr[-300:])
        r = subprocess.run(wrap("import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        c.ok(r.returncode != 0, "OS sandbox: no network", r.stderr[-200:])
        r = subprocess.run(wrap(f"open({str(home_tmp / 'w.txt')!r}, 'w').write('x')"), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        c.ok(r.returncode != 0 and not (home_tmp / "w.txt").exists(), "OS sandbox: no writes outside scratch",
             r.stderr[-200:])
        # A vault under the home dir (the usual place on a Mac) must still start: the allow-list
        # profile once denied stat() under /Users and the worker died at `import ask`.
        hv = Path(tempfile.mkdtemp(prefix="palimpsest-homevault-", dir=Path.home() / "Library" / "Caches"))
        try:
            shutil.copytree(v / "tools", hv / "tools", ignore=shutil.ignore_patterns(".rlm_scratch", "__pycache__"))
            (hv / "10 Notes").mkdir(parents=True)
            (hv / "10 Notes" / "n.md").write_text("a note\n")
            drv = ("import sys, importlib.util\n"
                   "spec = importlib.util.spec_from_file_location('rlm', sys.argv[1])\n"
                   "rlm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rlm)\n"
                   "it = iter(['```python\\nprint(\"DOCS\", len(docs))\\n```', 'FINAL\\nx'])\n"
                   "rlm._claude = lambda prompt, model, timeout: next(it)\n"
                   "sys.argv = ['rlm.py', 'q', '--steps', '3']\nrlm.main()\n")
            r = subprocess.run([sys.executable, "-c", drv, str(hv / "tools" / "rlm.py")], cwd=hv,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            c.ok("DOCS 1" in r.stdout + r.stderr and "worker died" not in (r.stdout + r.stderr).lower(),
                 "OS sandbox: a vault under the home dir starts and reads its notes", (r.stdout + r.stderr)[-300:])
        finally:
            _util.rmtree(hv, ignore_errors=True)
        cmd, note = rlm._os_sandbox(["true"], scratch)
        os.environ["RLM_OS_SANDBOX"] = "0"
        cmd0, note0 = rlm._os_sandbox(["true"], scratch)
        os.environ.pop("RLM_OS_SANDBOX", None)
        c.ok(cmd[0].endswith("sandbox-exec") and cmd0 == ["true"] and "disabled" in note0,
             "RLM_OS_SANDBOX=0 opts out, and says so", f"{note} | {note0}")
    finally:
        for f in home_tmp.iterdir():
            f.unlink()
        home_tmp.rmdir()
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
