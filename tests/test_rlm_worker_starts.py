"""The advertised `python tools/rlm.py "question"` must get past starting its REPL worker.

Codex review P1: rlm.py and rlm_worker.py set TOOLS = VAULT / "_tools" (the layout of the
vault this was extracted from), but the worker lives in tools/. So rlm.py spawned a path that
does not exist, the worker died before its handshake, and every run ended "REPL worker died".

This drives the real rlm.main() in a throwaway vault with the root model replaced by two
scripted replies (one code step, then FINAL) — worker start, handshake and one sandboxed exec
are all exercised, with zero model calls.
"""
import subprocess, sys
from _util import Checks, make_vault, write

DRIVER = r'''
import sys, importlib.util
spec = importlib.util.spec_from_file_location("rlm", sys.argv[1])
rlm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rlm)
replies = iter(["```python\nprint('DOCS', len(docs))\n```", "FINAL\nscripted answer"])
rlm._claude = lambda prompt, model, timeout: next(replies)
sys.argv = ["rlm.py", "how many notes", "--steps", "3"]
rlm.main()
'''


def main() -> int:
    c = Checks("rlm: worker starts")
    v = make_vault()
    for i in range(3):
        write(v, f"10 Notes/note {i}.md", f"note {i}\n")
    r = subprocess.run([sys.executable, "-c", DRIVER, str(v / "tools" / "rlm.py")], cwd=v,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    out = r.stdout + r.stderr
    c.ok("REPL worker died" not in out and r.returncode == 0, "rlm.py gets past worker start", out[-600:])
    c.ok("DOCS 3" in out, "the sandboxed exec ran against the corpus (3 notes)", out[-600:])
    c.ok("scripted answer" in out, "the run reaches FINAL", out[-300:])
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
