#!/usr/bin/env python3
"""Sealed REPL worker for rlm.py — holds the corpus as a live variable.

Speaks newline-delimited JSON with the parent over stdin/stdout:
    parent -> {"t":"exec","code":"..."}          run code in the persistent namespace
    worker -> {"t":"result","out":"...","err":"..."}
    worker -> {"t":"rlm","calls":[{prompt,text},...]}   broker an LLM fan-out upward
    parent -> {"t":"rlm_result","results":["...",...]}

The worker never talks to the network or spawns a process itself: every LLM call is
brokered by the parent. A Python audit hook enforces that, confines writes to a scratch
dir, and confines *reads* to the vault and the Python install — because rlm() is an
egress channel in its own right, so an unrestricted read is an unrestricted send.
Environment variables are scrubbed to a functional minimum for the same reason.
"""
from __future__ import annotations
import sys, os, io, re, ast, json, math, contextlib, collections, statistics, sysconfig
from pathlib import Path
from collections import Counter, defaultdict

VAULT = Path(sys.argv[1]).resolve()
SCRATCH = Path(sys.argv[2]).resolve()
TOOLS = VAULT / "_tools"
sys.path.insert(0, str(TOOLS))
import ask  # reuse the vault's proven keyword scorer rather than inventing a second one

REAL_OUT = sys.stdout  # exec() redirects sys.stdout; protocol frames must bypass that

CORPUS_DIRS = ["10 Notes", "Skills", "20 Projects", "30 Areas", "40 Resources",
               "60 Maps of Content", "Daily", "Reviews"]


def _send(obj) -> None:
    REAL_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    REAL_OUT.flush()


def _recv():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


# ---------------------------------------------------------------- corpus


class Corpus:
    """The vault as a variable. Slicing is free; only .text()/.chunks() pay I/O."""

    def __init__(self, paths: list[Path], cache: dict | None = None):
        self._paths = list(paths)
        self._cache = cache if cache is not None else {}

    # --- basics
    @property
    def paths(self) -> list[str]:
        return [str(p.relative_to(VAULT)).replace("\\", "/") for p in self._paths]

    @property
    def titles(self) -> list[str]:
        return [p.stem for p in self._paths]

    def __len__(self) -> int:
        return len(self._paths)

    def __repr__(self) -> str:
        mb = sum(len(self.text(i)) for i in range(len(self))) / 1e6 if len(self) <= 400 else None
        size = f", ~{mb:.1f} MB, ~{mb*1e6/4/1000:.0f}k tokens" if mb else ""
        return f"<Corpus {len(self)} docs{size}>"

    # --- resolution
    def _resolve(self, key) -> Path:
        if isinstance(key, int):
            return self._paths[key]
        k = str(key).replace("\\", "/")
        for p in self._paths:
            rel = str(p.relative_to(VAULT)).replace("\\", "/")
            if rel == k or p.stem == k or rel.endswith("/" + k):
                return p
        raise KeyError(f"no doc matching {key!r}")

    def text(self, key) -> str:
        p = self._resolve(key)
        if p not in self._cache:
            self._cache[p] = p.read_text(encoding="utf-8", errors="ignore")
        return self._cache[p]

    # --- slicing (all return a Corpus, so they compose)
    def select(self, keys) -> "Corpus":
        return Corpus([self._resolve(k) for k in keys], self._cache)

    def filter(self, fn) -> "Corpus":
        keep = [p for p in self._paths
                if fn(str(p.relative_to(VAULT)).replace("\\", "/"), self.text(p.stem))]
        return Corpus(keep, self._cache)

    def under(self, *prefixes) -> "Corpus":
        pre = tuple(x.replace("\\", "/") for x in prefixes)
        return Corpus([p for p in self._paths
                       if str(p.relative_to(VAULT)).replace("\\", "/").startswith(pre)], self._cache)

    def sample(self, n: int) -> "Corpus":
        step = max(1, len(self) // max(1, n))
        return Corpus(self._paths[::step][:n], self._cache)

    def search(self, query: str, top: int = 20) -> "Corpus":
        qt = ask.tokens(query)
        scored = []
        for p in self._paths:
            s = ask.score(qt, self.text(p.stem), p.stem, p)
            if s > 0:
                scored.append((s, p))
        scored.sort(key=lambda x: -x[0])
        return Corpus([p for _, p in scored[:top]], self._cache)

    def grep(self, pattern: str, ctx: int = 0, flags=re.I, limit: int = 300) -> list[dict]:
        rx = re.compile(pattern, flags)
        hits = []
        for p in self._paths:
            lines = self.text(p.stem).splitlines()
            for i, ln in enumerate(lines):
                if rx.search(ln):
                    body = "\n".join(lines[max(0, i - ctx): i + ctx + 1]) if ctx else ln
                    hits.append({"title": p.stem,
                                 "path": str(p.relative_to(VAULT)).replace("\\", "/"),
                                 "line": i + 1, "text": body.strip()[:400]})
                    if len(hits) >= limit:
                        return hits
        return hits

    def chunks(self, size: int = 60000, per_doc_cap: int = 40000) -> list[str]:
        """Pack docs into labelled chunks sized for one sub-agent each."""
        out, cur, cur_n = [], [], 0
        for p in self._paths:
            t = self.text(p.stem)
            body = t if len(t) <= per_doc_cap else t[: per_doc_cap // 2] + \
                f"\n…[{len(t)-per_doc_cap} chars elided]…\n" + t[-per_doc_cap // 2:]
            block = f"### NOTE: {p.stem}\n{body}\n"
            if cur and cur_n + len(block) > size:
                out.append("".join(cur))
                cur, cur_n = [], 0
            cur.append(block)
            cur_n += len(block)
        if cur:
            out.append("".join(cur))
        return out


def _gather() -> list[Path]:
    files = []
    for d in CORPUS_DIRS:
        root = VAULT / d
        if not root.exists():
            continue
        for f in root.rglob("*.md"):
            parts = f.relative_to(VAULT).parts
            if any(x.startswith("_") and x != "_proposed" for x in parts):
                continue
            files.append(f)
    return sorted(files)


# ---------------------------------------------------------------- LLM brokering

_BUDGET = {"used": 0, "limit": int(sys.argv[3]) if len(sys.argv) > 3 else 40}


def _broker(calls: list[dict]) -> list[str]:
    room = _BUDGET["limit"] - _BUDGET["used"]
    if room <= 0:
        return ["[budget exhausted: no sub-agent calls remaining]"] * len(calls)
    dropped = 0
    if len(calls) > room:
        dropped = len(calls) - room
        calls = calls[:room]
    _send({"t": "rlm", "calls": calls})
    reply = _recv()
    _BUDGET["used"] += len(calls)
    res = reply.get("results", [])
    if dropped:
        res = res + [f"[NOT RUN — sub-agent budget exhausted, {dropped} call(s) dropped]"] * dropped
    return res


def rlm(prompt: str, text: str = "", model: str | None = None) -> str:
    """One sub-agent. It sees only `prompt` and `text` — nothing else."""
    return _broker([{"prompt": prompt, "text": text, "model": model}])[0]


def rlm_map(prompt: str, texts: list[str], model: str | None = None) -> list[str]:
    """Fan out one instruction across many texts, in parallel. The core RLM primitive."""
    return _broker([{"prompt": prompt, "text": t, "model": model} for t in texts])


def budget() -> dict:
    return {"subagents_used": _BUDGET["used"],
            "subagents_left": max(0, _BUDGET["limit"] - _BUDGET["used"])}


# ---------------------------------------------------------------- sandbox

_WRITE_EVENTS = {"os.remove", "os.rename", "os.link", "os.symlink", "os.truncate",
                 "os.mkdir", "os.rmdir", "os.chmod", "os.chown", "shutil.copyfile",
                 "shutil.copymode", "shutil.copystat", "shutil.move", "shutil.rmtree"}
# Match by PREFIX, not by exact name. An exact-name deny-list is only as complete as its
# author's memory of CPython's audit table: the first version listed subprocess.Popen but
# not _winapi.CreateProcess, so `import _winapi` walked straight out of the sandbox on the
# very platform this box runs. Whole families are blocked now, so a primitive we failed to
# think of is blocked by the family it belongs to rather than let through by omission.
_BLOCK_EXACT = {"os.system", "os.startfile"}
_BLOCK_PREFIX = ("_winapi.", "subprocess.", "multiprocessing.", "os.exec", "os.spawn",
                 "os.posix_spawn", "os.fork", "ctypes.", "socket.", "pty.", "winreg.",
                 "msvcrt.", "webbrowser.", "urllib.", "ftplib.", "smtplib.", "imaplib.",
                 "poplib.", "nntplib.", "telnetlib.", "http.client.")

# Reads are confined too. Blocking sockets is not containment on its own, because rlm()
# is itself an egress channel: anything the REPL can read, it can pass to a sub-agent and
# out to the API. So the corpus is readable, the Python install is readable (imports), and
# .env files in sibling projects, session stores and ~/.claude are not.
def _roots() -> tuple[Path, ...]:
    cand = [VAULT, SCRATCH, Path(sys.prefix), Path(sys.base_prefix),
            Path(sys.executable).parent]
    for key in ("stdlib", "platstdlib", "purelib", "platlib", "data"):
        try:
            p = sysconfig.get_path(key)
            if p:
                cand.append(Path(p))
        except Exception:
            pass
    out = []
    for c in cand:
        try:
            out.append(c.resolve())
        except Exception:
            pass
    return tuple(dict.fromkeys(out))


_READ_ROOTS = _roots()


def _within(path, roots) -> bool:
    try:
        p = Path(os.fspath(path)).resolve()
    except Exception:
        return False
    return any(p == r or r in p.parents for r in roots)


def _audit(event: str, args):
    if event in _BLOCK_EXACT or event.startswith(_BLOCK_PREFIX):
        raise PermissionError(
            f"blocked by the RLM sandbox: {event}. This REPL has no network and no child "
            f"processes — use rlm()/rlm_map() for model calls, and don't shell out.")
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        if path is None or isinstance(path, int):
            return  # already-open fd; the originating open() was audited
        writing = any(c in mode for c in "wax+") if isinstance(mode, str) else bool(
            (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
        if writing:
            if not _within(path, (SCRATCH,)):
                raise PermissionError(f"blocked by the RLM sandbox: write to {path!r}. "
                                      f"Writes are confined to {SCRATCH}.")
        elif not _within(path, _READ_ROOTS):
            raise PermissionError(f"blocked by the RLM sandbox: read of {path!r}. "
                                  f"Reads are confined to the vault — the corpus is already "
                                  f"in `docs`, and anything outside it is not yours to send.")
    elif event in _WRITE_EVENTS:
        for a in args:
            if isinstance(a, (str, bytes, os.PathLike)) and not _within(a, (SCRATCH,)):
                raise PermissionError(f"blocked by the RLM sandbox: {event} on {a!r}.")


# ---------------------------------------------------------------- main loop

# Names Windows/CPython need to keep working. Everything else — API keys, tokens and
# whatever else pm2 and the Claude Code session put in the environment — is dropped before
# any model-generated code runs, since os.environ is one os.environ.copy() from egress.
_ENV_KEEP = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "TMPDIR",
             "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS",
             "OS", "PROCESSOR_ARCHITECTURE", "PYTHONIOENCODING", "LANG", "LC_ALL",
             "SYSTEMDRIVE", "PYTHONPATH", "PYTHONHOME"}


def _scrub_env() -> None:
    for k in [k for k in os.environ if k.upper() not in _ENV_KEEP]:
        try:
            del os.environ[k]
        except Exception:
            pass


def _run(code: str, ns: dict) -> None:
    """exec a block, echoing a trailing bare expression the way a real REPL does.

    Without this, `docs.search("x")` as the last line prints nothing and the root model
    burns a step re-running it inside print().
    """
    tree = ast.parse(code, "<rlm>", "exec")
    tail = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    exec(compile(tree, "<rlm>", "exec"), ns)
    if tail is not None:
        val = eval(compile(ast.Expression(tail.value), "<rlm>", "eval"), ns)
        if val is not None:
            print(repr(val)[:4000])


def main() -> None:
    docs = Corpus(_gather())
    _scrub_env()
    ns = {"__name__": "__rlm__", "docs": docs, "rlm": rlm, "rlm_map": rlm_map,
          "budget": budget, "VAULT": VAULT, "Corpus": Corpus,
          "re": re, "json": json, "math": math, "statistics": statistics,
          "collections": collections, "Counter": Counter, "defaultdict": defaultdict,
          "Path": Path}
    sys.addaudithook(_audit)
    _send({"t": "ready", "docs": len(docs), "dirs": CORPUS_DIRS})
    while True:
        msg = _recv()
        if msg.get("t") == "exit":
            return
        buf = io.StringIO()
        err = ""
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                _run(msg["code"], ns)
        except PermissionError as e:
            err = f"PermissionError: {e}"
        except Exception:
            import traceback
            err = traceback.format_exc(limit=6)
        _send({"t": "result", "out": buf.getvalue(), "err": err})


if __name__ == "__main__":
    main()
