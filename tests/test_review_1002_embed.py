"""Regressions for the 2026-10-02 review of embed.py's command line. No test passed it an argument.

1. -h/--help print the usage and build nothing; any other option exits 2 and builds nothing.
2. /?, /h and /help (the Windows habit) print the usage too; they were taken as a query, so
   `embed.py /?` built the index to search for "/?".
3. `--` ends the options wherever it stands: `-- -zebra` and `stripes -- -zebra` build and search.
   Only a leading `--` was honoured; the second was refused as an unknown option '--'.
4. [P2] Without numpy, `embed.py` (the nightly sync's embed step) and `embed.py --help` exit 0
   with a message, like a missing sentence-transformers. The module-level `import numpy` raised
   before either check, so the step failed every night with a traceback. An importer (ask.py)
   still gets the ImportError.

Checks 2, 3 (the mid-line `--`) and 4 (the two exit-0 checks) fail with PALIMPSEST_TOOLS pointed
at tools/ from 60b4bff.

No model, no network: sentence_transformers and torch are stubs on PYTHONPATH (a hashed
bag-of-words encoder), and "no numpy" is a numpy package on PYTHONPATH whose import raises.
"""
import os, re, subprocess, sys
from pathlib import Path
import _util
from _util import Checks, make_vault, run, write

ST_STUB = r'''
import re, zlib
import numpy as np
DIM = 4096
class SentenceTransformer:
    def __init__(self, name, device=None):
        self.max_seq_length = 512
    def get_sentence_embedding_dimension(self):
        return DIM
    def encode(self, texts, **kw):
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                if w not in ("query", "passage"):
                    out[i, zlib.crc32(w.encode()) % DIM] += 1
            n = np.linalg.norm(out[i])
            if n:
                out[i] /= n
        return out
'''
TORCH_STUB = "def set_num_threads(n): pass\ndef get_num_threads(): return 1\n"
NO_NUMPY = "raise ImportError(\"No module named 'numpy' (blocked by the test)\")\n"


def vault(numpy: bool = True) -> tuple[Path, dict]:
    v = make_vault()
    write(v, "10 Notes/Zebra.md", "# Zebra\n\nzebra stripes on the savanna\n")
    write(v, "10 Notes/Other.md", "# Other\n\nsomething unrelated entirely\n")
    write(v, ".stubs/py/sentence_transformers/__init__.py", ST_STUB)
    write(v, ".stubs/py/torch/__init__.py", TORCH_STUB)
    path = [str(v / ".stubs" / "py")]
    if not numpy:
        write(v, ".stubs/nonumpy/numpy/__init__.py", NO_NUMPY)
        path.append(str(v / ".stubs" / "nonumpy"))
    return v, {"PYTHONPATH": os.pathsep.join(path), "CLAUDE_BRAIN_NO_HOOK": "1"}


def cache(v: Path) -> bool:
    return (v / "tools" / "cache").exists()


def out(r: subprocess.CompletedProcess) -> str:
    return f"exit={r.returncode} stdout={r.stdout[-300:]!r} stderr={r.stderr[-400:]!r}"


def top_hit(r: subprocess.CompletedProcess) -> str:
    """The first search result line's path, '/'-separated (Windows prints '\\'), or '' when
    nothing was searched."""
    hits = [m.group(1) for m in (re.match(r"-?\d+\.\d+  (.+)$", l) for l in r.stdout.splitlines()) if m]
    return hits[0].strip().replace("\\", "/") if hits else ""


def check_help_and_unknown(c: Checks):
    v, env = vault()
    for flag in ("--help", "-h"):
        r = run(v, "embed.py", flag, env=env)
        c.ok(r.returncode == 0 and r.stdout.startswith("usage:") and not cache(v),
             f"`embed.py {flag}` prints the usage and builds nothing", out(r))
    for argv in (["-x"], ["-x", "--", "zebra"]):
        r = run(v, "embed.py", *argv, env=env)
        c.ok(r.returncode == 2 and "unknown option '-x'" in r.stderr and not cache(v),
             f"`embed.py {' '.join(argv)}` exits 2 on the unknown option and builds nothing", out(r))


def check_windows_help(c: Checks):
    v, env = vault()
    for flag in ("/?", "/h", "/help", "/HELP"):
        r = run(v, "embed.py", flag, env=env)
        c.ok(r.returncode == 0 and r.stdout.startswith("usage:") and not cache(v),
             f"`embed.py {flag}` prints the usage instead of building to search for {flag!r}", out(r))


def check_double_dash(c: Checks):
    for argv in (["--", "-zebra"], ["stripes", "--", "-zebra"]):
        v, env = vault()
        r = run(v, "embed.py", *argv, env=env)
        c.ok(r.returncode == 0 and "index: 2 files" in r.stdout and top_hit(r) == "10 Notes/Zebra.md",
             f"`embed.py {' '.join(argv)}` builds the index and searches for the words after `--`", out(r))


def check_no_numpy(c: Checks):
    v, env = vault(numpy=False)
    r = run(v, "embed.py", env=env)
    c.ok(r.returncode == 0 and "numpy" in r.stdout and "nothing to build" in r.stdout
         and "Traceback" not in r.stdout + r.stderr and not cache(v),
         "without numpy the nightly `embed.py` is a no-op that says why (exit 0, no traceback)", out(r))
    r = run(v, "embed.py", "--help", env=env)
    c.ok(r.returncode == 0 and r.stdout.startswith("usage:") and "Traceback" not in r.stdout + r.stderr,
         "without numpy `embed.py --help` still prints the usage", out(r))
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + "import embed"], cwd=v / "tools",
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, **env}, timeout=60)
    c.ok(r.returncode != 0 and "ImportError" in r.stderr and "blocked by the test" in r.stderr,
         "without numpy an importer of embed still gets the ImportError, not a module that half works",
         out(r))


def main() -> int:
    c = Checks("review 2026-10-02: embed.py arguments")
    check_help_and_unknown(c)
    check_windows_help(c)
    check_double_dash(c)
    check_no_numpy(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
