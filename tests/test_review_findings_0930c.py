"""Regressions for the fourth external review (2026-09-30), P2 findings.

1. The RLM corpus reads every note by its own path: two notes with the same filename in different
   folders used to resolve to the first one, so grep/search/filter/chunks saw one note's text twice.
   A bare name that matches several notes is an error that lists the paths.
2. ask.py answers from the ledger when no note matches: it used to exit before the STATE block
   reached the model.
3. A REPL step that never finishes is stopped at EXEC_TIMEOUT and the worker is killed; the wait
   used to block forever.
"""
import os, subprocess, sys, time
import _util
from _util import Checks, make_vault, run, write

CORPUS = r'''
import sys
sys.argv = ["rlm_worker.py", sys.argv[1], sys.argv[1] + "/tools/.rlm_scratch", "1"]
import importlib.util
spec = importlib.util.spec_from_file_location("w", sys.argv[0] if False else __import__("os").path.join(sys.argv[1], "tools", "rlm_worker.py"))
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
docs = w.Corpus(w._gather())
print("GREP", [h["path"] for h in docs.grep("beta")])
print("CHUNKS", "alpha" in "".join(docs.chunks()), "beta" in "".join(docs.chunks()))
print("FILTER", len(docs.filter(lambda rel, text: "beta" in text)))
try:
    docs.text("Status"); print("AMBIG none")
except KeyError as e:
    print("AMBIG", "2 notes" in str(e))
print("BYPATH", docs.text("10 Notes/B/Status.md").strip())
'''

TIMEOUT = r'''
import sys, importlib.util
spec = importlib.util.spec_from_file_location("rlm", sys.argv[1])
rlm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rlm)
rlm.EXEC_TIMEOUT = 3
replies = iter(["```python\nwhile True:\n    pass\n```", "FINAL\nnever reached"])
rlm._claude = lambda prompt, model, timeout: next(replies)
sys.argv = ["rlm.py", "q", "--steps", "3"]
rlm.main()
'''


def main() -> int:
    c = Checks("review findings 2026-09-30 (c)")

    v = make_vault()
    write(v, "10 Notes/A/Status.md", "alpha\n")
    write(v, "10 Notes/B/Status.md", "beta\n")
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + CORPUS, str(v)], cwd=v, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    out = r.stdout + r.stderr
    c.ok("GREP ['10 Notes/B/Status.md']" in out, "grep finds text in the second same-named note", out[-400:])
    c.ok("CHUNKS True True" in out, "chunks carry both same-named notes' text", out[-400:])
    c.ok("FILTER 1" in out, "filter sees each note's own text", out[-400:])
    c.ok("AMBIG True" in out and "BYPATH beta" in out, "a bare ambiguous name errors; a path resolves", out[-400:])

    v = make_vault()
    env = {"PALIMPSEST_MACHINE": "alpha", "CLAUDE_BRAIN_NO_HOOK": "1"}
    run(v, "state.py", "register", "my-api", "--kind", "service", env=env)
    run(v, "state.py", "add", "my-api", "host", "server-1", env=env)
    r = run(v, "ask.py", "--mode", "lexical", "--retrieve-only", "where does my-api run?", env=env)
    c.ok("my-api.host = server-1" in r.stdout and "No relevant notes" not in r.stdout,
         "a ledger-only answer is not dropped when no note matches", (r.stdout + r.stderr)[-400:])

    v = make_vault()
    write(v, "10 Notes/n.md", "a note\n")
    t0 = time.monotonic()
    try:
        r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + TIMEOUT, str(v / "tools" / "rlm.py")], cwd=v,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        out, hung = r.stdout + r.stderr, False
    except subprocess.TimeoutExpired:
        out, hung = "", True
    took = time.monotonic() - t0
    c.ok(not hung and "ran past EXEC_TIMEOUT" in out and took < 60,
         "an endless step is stopped at EXEC_TIMEOUT and the run ends", f"hung={hung} took={took:.0f}s {out[-300:]}")
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
