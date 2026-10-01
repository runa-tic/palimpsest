"""Regressions for the 2026-09-30 review, retrieval group (ask.py, embed.py, bench_retrieval.py).

1. [P2] CRLF files: embed.py computes chunk spans on the raw decoded bytes (CR kept) while
   ask.py sliced newline-translated text with them, so for a long CRLF transcript the excerpt
   handed to the model missed the passage the embeddings found.
2. A note symlinked from outside the vault crashed the embedding path (relative_to ValueError),
   and a dangling symlink crashed ask.py in every mode, --mode lexical included.
3. The default (hybrid) ask.py crashed when the embedding model could not be loaded (offline,
   not yet downloaded) instead of falling back to keyword search.
4. bench_retrieval.py scored queries whose gold note no longer exists as misses for every system.
5. [P2] _WriterLock spun forever at full CPU when a stale lock could not be removed; and a
   holder's exit removed a lock that another writer had taken over.
6. A long build's lock went stale after LOCK_STALE and was stolen while its holder was alive;
   checkpointing writes now refresh it.
7. Concurrent writers (the documented proceed-without-the-lock path) shared fixed temp names,
   so one crashed in os.replace, and a vectors/manifest pair from two writers loaded silently.

No model, no network: sentence_transformers and torch are stubs on PYTHONPATH (a hashed
bag-of-words encoder), and `claude` is a stub on PATH that records the prompt it was sent.
"""
import json, os, shutil, stat, subprocess, sys, tempfile, time
from pathlib import Path
from _util import Checks, can_symlink, make_vault as _make_vault, rmtree, run, stub, stub_path, write

_TMP: list[Path] = []


def make_vault() -> Path:
    v = _make_vault()
    _TMP.append(v)
    return v

ST_STUB = r'''
import os, re, zlib
import numpy as np
DIM = 4096
class SentenceTransformer:
    def __init__(self, name, device=None):
        if os.environ.get("STUB_ST_FAIL"):
            raise OSError(f"We couldn't connect to 'https://huggingface.co' to load this model: {name}")
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
class CrossEncoder:
    def __init__(self, *a, **k):
        raise OSError("stub: no cross-encoder offline")
'''
TORCH_STUB = "def set_num_threads(n): pass\ndef get_num_threads(): return 1\n"
CLAUDE_STUB = f"import os, sys\nopen(os.environ['STUB_PROMPT_OUT'], 'w', encoding='utf-8').write(sys.stdin.read())\nprint('stub answer')\n"


def stubs(v: Path) -> dict:
    d = v / ".stubs"
    write(v, ".stubs/py/sentence_transformers/__init__.py", ST_STUB)
    write(v, ".stubs/py/torch/__init__.py", TORCH_STUB)
    stub(d / "bin", "claude", CLAUDE_STUB)
    return {"PYTHONPATH": str(d / "py"), "PATH": stub_path(d / "bin"),
            "STUB_PROMPT_OUT": str(d / "prompt.txt"), "CLAUDE_BRAIN_NO_HOOK": "1"}


def py(v: Path, code: str, env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([sys.executable, "-c", code], cwd=v / "tools", capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env={**os.environ, **env}, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, -9, "", f"TIMEOUT after {timeout}s")


def check_crlf_excerpt(c: Checks):
    v = make_vault()
    env = stubs(v)
    marker = "zanzibarquux"
    lines = [f"filler line {i} lorem ipsum dolor" for i in range(3000)]
    lines += [f"the answer is {marker} and nothing else"] + ["trailing lorem ipsum"] * 20
    body = "---\ntype: conversation\n---\n# Long transcript\n" + "\n".join(lines) + "\n"
    p = v / "40 Resources" / "Claude Conversations" / "Long transcript.md"
    p.parent.mkdir(parents=True)
    p.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))   # what write_text produces on Windows
    write(v, "10 Notes/Other.md", "# Other\n\nsomething unrelated\n")
    r = run(v, "ask.py", "--no-rerank", f"what is {marker}", env=env)
    prompt = Path(env["STUB_PROMPT_OUT"]).read_text(encoding="utf-8") if Path(env["STUB_PROMPT_OUT"]).exists() else ""
    c.ok(r.returncode == 0 and f"the answer is {marker}" in prompt,
         "CRLF transcript: the excerpt sent to the model contains the passage embeddings found",
         (r.stdout + r.stderr)[-400:])


def check_symlinks(c: Checks):
    if not can_symlink():
        c.skip("symlinks: a dangling one and one leaving the vault are skipped, not a crash",
               "this process may not create symlinks: on Windows that needs admin or Developer Mode")
        return
    v = make_vault()
    env = stubs(v)
    write(v, "10 Notes/Inside.md", "# Inside\n\nthe vault's own note about zebras\n")
    _TMP.append(Path(tempfile.mkdtemp(prefix="palimpsest-outside-")))
    outside = _TMP[-1] / "Outside.md"
    outside.write_text("# Outside\n\nprivate zebras outside the vault\n", encoding="utf-8")
    (v / "10 Notes" / "Linked.md").symlink_to(outside)
    (v / "10 Notes" / "Dangling.md").symlink_to(v / "10 Notes" / "gone.md")
    lex = run(v, "ask.py", "--mode", "lexical", "--retrieve-only", "zebras", env=env)
    hyb = run(v, "ask.py", "--no-rerank", "--retrieve-only", "zebras", env=env)
    emb = run(v, "embed.py", env=env)
    out = "\n".join(f"{n}: rc={r.returncode} {(r.stdout + r.stderr)[-300:]}" for n, r in
                    (("lexical", lex), ("hybrid", hyb), ("embed.py", emb)))
    c.ok(all(r.returncode == 0 for r in (lex, hyb, emb)) and "Inside" in hyb.stdout
         and "Outside" not in lex.stdout + hyb.stdout and "Linked" not in lex.stdout + hyb.stdout,
         "symlinks: a dangling one and one leaving the vault are skipped, not a crash", out)


def check_model_unloadable(c: Checks):
    v = make_vault()
    env = {**stubs(v), "STUB_ST_FAIL": "1"}
    write(v, "10 Notes/Zebra.md", "# Zebra\n\nzebras have stripes\n")
    r = run(v, "ask.py", "--retrieve-only", "zebra stripes", env=env)
    c.ok(r.returncode == 0 and "sources (lexical):" in r.stdout and "Zebra" in r.stdout
         and "embed: skipped" in r.stderr,
         "an embedding model that cannot load falls back to keyword search and says so",
         (r.stdout + r.stderr)[-500:])


def check_bench_missing_gold(c: Checks):
    v = make_vault()
    env = stubs(v)
    for t, b in (("Alpha note", "alpha particles and helium nuclei"), ("Beta note", "beta decay emits electrons"),
                 ("Gamma note", "gamma rays are photons")):
        write(v, f"10 Notes/{t}.md", f"# {t}\n\n{b}\n")
    rows = [{"rel": "10 Notes/Alpha note.md", "title": "Alpha note", "en": "alpha helium nuclei", "ru": "alpha helium"},
            {"rel": "10 Notes/Merged away.md", "title": "Merged away", "en": "gamma photons", "ru": "gamma photons"}]
    (v / "40 Resources").mkdir()
    write(v, "tools/cache/bench-queries.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    r = run(v, "bench_retrieval.py", "--run", env=env)
    row = next((l for l in r.stdout.splitlines() if l.startswith("| lexical | en |")), "")
    c.ok(r.returncode == 0 and row.split("|")[5].strip() == "1.00" and "no longer exist" in r.stdout,
         "bench: a query whose gold note is gone is skipped and reported, not scored as a miss",
         (r.stdout + r.stderr)[-600:])


def check_lock_unremovable_stale(c: Checks):
    v = make_vault()
    code = r'''
import os, time, pathlib, embed
embed.LOCK_WAIT = 2
d = pathlib.Path("cache/embed-x")
d.mkdir(parents=True, exist_ok=True)
lock = d / ".lock"
lock.write_text("99999")
old = time.time() - 2 * embed.LOCK_STALE
os.utime(lock, (old, old))
real = pathlib.Path.unlink
def unlink(self, *a, **k):          # what Windows does while the holder keeps its fd open
    if self.name == ".lock":
        raise PermissionError(13, "The process cannot access the file")
    return real(self, *a, **k)
pathlib.Path.unlink = unlink
t0 = time.time()
with embed._WriterLock(d):
    pass
print("RETURNED", round(time.time() - t0))
pathlib.Path.unlink = real
if os.name == "nt":
    raise SystemExit   # a held lock cannot be deleted on Windows, so it is never taken over
# a holder whose lock was taken over must not remove the new holder's lock on exit
lock.unlink()
a = embed._WriterLock(d).__enter__()
os.utime(lock, (old, old))
real(lock); lock.write_text("12345")   # another writer reclaimed it
a.__exit__(None, None, None)
print("KEPT", lock.exists() and lock.read_text() == "12345")
'''
    r = py(v, code, {}, timeout=30)
    c.ok("RETURNED" in r.stdout, "_WriterLock: an unremovable stale lock waits LOCK_WAIT then proceeds",
         (r.stdout + r.stderr)[-400:])
    if os.name == "nt":
        c.skip("_WriterLock: exit never removes the lock of a writer that took it over",
               "Windows cannot delete a lock file its holder keeps open, so no writer can take it over")
    else:
        c.ok("KEPT True" in r.stdout, "_WriterLock: exit never removes the lock of a writer that took it over",
             (r.stdout + r.stderr)[-400:])


def check_lock_refreshed(c: Checks):
    v = make_vault()
    code = r'''
import os, sys, time, pathlib, subprocess
import numpy as np, embed
idx = embed.Index(pathlib.Path("cache/embed-x"))
idx.vec = np.ones((2, 4), dtype=np.float32); idx.rows = [("a.md", 0, 1), ("a.md", 1, 2)]
idx.files = {"a.md": {"sha": "s", "spans": [[0, 1], [1, 2]]}}
lock = idx.cache_dir / ".lock"
with embed._WriterLock(idx.cache_dir) as held:
    idx._lock = held
    old = time.time() - 2 * embed.LOCK_STALE       # an hours-long first build
    os.utime(lock, (old, old))
    idx._save()                                    # a checkpoint
    other = subprocess.run([sys.executable, "-c",
        "import embed, pathlib; embed.LOCK_WAIT = 2\n"
        "l = embed._WriterLock(pathlib.Path('cache/embed-x')).__enter__()\n"
        "print('STOLE' if l.fd is not None else 'WAITED')"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    print(other.stdout.strip())
'''
    r = py(v, code, {}, timeout=60)
    c.ok("WAITED" in r.stdout, "a checkpoint refreshes the writer lock, so a live build's lock is not stolen",
         (r.stdout + r.stderr)[-400:])


def check_concurrent_save(c: Checks):
    v = make_vault()
    code = r'''
import os, sys, subprocess, pathlib
import numpy as np, embed
d = pathlib.Path("cache/embed-x")
def make(val):
    i = embed.Index(d)
    i.vec = np.full((2, 4), val, dtype=np.float32); i.rows = [("a.md", 0, 1), ("b.md", 0, 1)]
    i.files = {"a.md": {"sha": str(val), "spans": [[0, 1]]}, "b.md": {"sha": str(val), "spans": [[0, 1]]}}
    return i
B = ("import numpy as np, pathlib, embed\n"
     "i = embed.Index(pathlib.Path('cache/embed-x'))\n"
     "i.vec = np.full((2, 4), 2, dtype=np.float32); i.rows = [('a.md', 0, 1), ('b.md', 0, 1)]\n"
     "i.files = {'a.md': {'sha': '2', 'spans': [[0, 1]]}, 'b.md': {'sha': '2', 'spans': [[0, 1]]}}\n"
     "i._save()\n")
real, fired = os.replace, []
def replace(src, dst):
    real(src, dst)
    if not fired:                     # writer B saves in full between writer A's two renames
        fired.append(1)
        subprocess.run([sys.executable, "-c", B], check=True)
os.replace = replace
try:
    make(1)._save()
    print("SAVED")
except Exception as e:
    print("CRASH", type(e).__name__, e)
os.replace = real
r = embed.Index(d); r._load()
print("LOADED", len(r.rows), float(r.vec[0, 0]) if r.vec.size else None, r.files.get("a.md", {}).get("sha"))
'''
    r = py(v, code, {}, timeout=60)
    out = r.stdout
    loaded = next((l for l in out.splitlines() if l.startswith("LOADED")), "")
    parts = loaded.split()
    # a consistent pair loads (vectors and manifest from the same writer), or a torn one is
    # rejected with a message; never A's manifest over B's vectors
    consistent = len(parts) == 4 and (parts[1] == "0" and "does not match" in r.stderr
                                      or parts[2:] in (["1.0", "1"], ["2.0", "2"]))
    c.ok("SAVED" in out and consistent,
         "concurrent saves: no crash, and a vectors/manifest pair from two writers is rejected, not loaded",
         (out + r.stderr)[-500:])


def main() -> int:
    c = Checks("review 2026-09-30: retrieval")
    try:
        check_crlf_excerpt(c)
        check_symlinks(c)
        check_model_unloadable(c)
        check_bench_missing_gold(c)
        check_lock_unremovable_stale(c)
        check_lock_refreshed(c)
        check_concurrent_save(c)
    finally:
        for d in _TMP:
            rmtree(d)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
