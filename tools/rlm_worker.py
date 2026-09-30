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
import sys, os, io, re, ast, json, math, contextlib, collections, statistics, sysconfig, threading
from pathlib import Path
from collections import Counter, defaultdict

VAULT = Path(sys.argv[1]).resolve()
SCRATCH = Path(sys.argv[2]).resolve()
TOOLS = Path(__file__).resolve().parent   # this file's own dir, where ask.py lives (not VAULT / "_tools")
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
        """int -> by position; Path -> itself (internal callers, exact); str -> relative path, else a
        unique filename. Every internal caller used to pass p.stem, which resolved to the FIRST note
        with that name, so two notes both called "Status" read as one (review, 2026-09-30); an
        ambiguous name is now an error that lists the paths instead of a silent pick."""
        if isinstance(key, int):
            return self._paths[key]
        if isinstance(key, Path):
            return key
        k = str(key).replace("\\", "/")
        named = []
        for p in self._paths:
            rel = str(p.relative_to(VAULT)).replace("\\", "/")
            if rel == k:
                return p
            if p.stem == k or rel.endswith("/" + k):
                named.append(p)
        if len(named) == 1:
            return named[0]
        if named:
            raise KeyError(f"{key!r} matches {len(named)} notes; pass one of these paths: "
                           + ", ".join(str(q.relative_to(VAULT)).replace("\\", "/") for q in named[:10]))
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
                if fn(str(p.relative_to(VAULT)).replace("\\", "/"), self.text(p))]
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
            s = ask.score(qt, self.text(p), p.stem, p)
            if s > 0:
                scored.append((s, p))
        scored.sort(key=lambda x: -x[0])
        return Corpus([p for _, p in scored[:top]], self._cache)

    def grep(self, pattern: str, ctx: int = 0, flags=re.I, limit: int = 300) -> list[dict]:
        rx = re.compile(pattern, flags)
        hits = []
        for p in self._paths:
            lines = self.text(p).splitlines()
            for i, ln in enumerate(lines):
                if rx.search(ln):
                    body = "\n".join(lines[max(0, i - ctx): i + ctx + 1]) if ctx else ln
                    hits.append({"title": p.stem, "path": str(p.relative_to(VAULT)).replace("\\", "/"),
                                 "path": str(p.relative_to(VAULT)).replace("\\", "/"),
                                 "line": i + 1, "text": body.strip()[:400]})
                    if len(hits) >= limit:
                        return hits
        return hits

    def chunks(self, size: int = 60000, per_doc_cap: int = 40000) -> list[str]:
        """Pack docs into labelled chunks sized for one sub-agent each."""
        out, cur, cur_n = [], [], 0
        for p in self._paths:
            t = self.text(p)
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
    files, outside = [], 0
    for d in CORPUS_DIRS:
        root = VAULT / d
        if not root.exists():
            continue
        for f in root.rglob("*.md"):
            parts = f.relative_to(VAULT).parts
            if any(x.startswith("_") and x != "_proposed" for x in parts):
                continue
            # A note symlinked to a file outside the vault is a read the sandbox refuses, and
            # every corpus-wide call (search/grep/chunks/filter) reads every note, so one such
            # link made all of them fail (review, 2026-09-30). Skip it, and a dangling one.
            real = f.resolve()
            if not ((real == VAULT or VAULT in real.parents) and real.is_file()):
                outside += 1
                continue
            files.append(f)
    if outside:
        print(f"rlm_worker: skipped {outside} note(s) that resolve outside the vault or to nothing",
              file=sys.stderr)
    return sorted(files)


# ---------------------------------------------------------------- LLM brokering

_BUDGET = {"used": 0, "limit": int(sys.argv[3]) if len(sys.argv) > 3 else 40}
# One request/reply on the pipe at a time. Frames carry no id, so when model code called rlm()
# from several threads, whichever thread read stdin first took the next reply (results swapped
# between texts), and every thread passed the budget check before any counted (10 calls ran
# under --subagents 5; review, 2026-09-30). The parent serves one frame at a time anyway, so
# the lock costs no parallelism: rlm_map is the parallel path.
_BROKER_LOCK = threading.Lock()


def _broker(calls: list[dict]) -> list[str]:
    with _BROKER_LOCK:
        room = _BUDGET["limit"] - _BUDGET["used"]
        if room <= 0:
            return ["[budget exhausted: no sub-agent calls remaining]"] * len(calls)
        dropped = 0
        if len(calls) > room:
            dropped = len(calls) - room
            calls = calls[:room]
        _BUDGET["used"] += len(calls)     # reserved before sending: budget() never under-reports
        _send({"t": "rlm", "calls": calls})
        reply = _recv()
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


def _make_audit(read_roots=_READ_ROOTS, scratch=SCRATCH, block_exact=frozenset(_BLOCK_EXACT),
                block_prefix=_BLOCK_PREFIX + ("gc.",), write_events=frozenset(_WRITE_EVENTS)):
    """Build the audit hook with every rule bound NOW. The first version looked its rules up in
    module globals at call time, so model-written code could rebind them from
    sys.modules["__main__"] (an external review set _READ_ROOTS to "/" and read outside the vault,
    2026-09-30). The second bound the rules but still called a module-level _within that looked
    up Path and os there, so rebinding sys.modules["__main__"].Path passed every check (review,
    2026-09-30). Now every rule and primitive is a default argument of a nested function: plain
    strings and C functions, no module globals, no builtins looked up at call time, no closure
    cells (a traceback through this hook hands out its frame, and in 3.13 frame.f_locals writes
    through to cells; a default is a per-call local). gc.* is blocked because gc.get_objects is
    how code would find the hook itself, and reading or replacing within's __defaults__/__code__
    is refused. Still defence in depth, not a boundary: the path functions it calls live in
    posixpath/ntpath, which code in this interpreter can patch. The boundary is the OS sandbox
    rlm.py wraps this process in, where one exists."""
    sep = os.sep

    def within(path, roots, _fspath=os.fspath, _realpath=os.path.realpath, _isinstance=isinstance,
               _str=str, _bytes=bytes, _enc=sys.getfilesystemencoding(), _sep=sep, _exc=Exception) -> bool:
        try:
            s = _fspath(path)
            # str.__str__ copies a str subclass to a plain str, so overridden methods never run here
            s = _bytes.decode(s, _enc, "surrogateescape") if _isinstance(s, _bytes) else _str.__str__(s)
            p = _str.__str__(_realpath(s))
        except _exc:
            return False
        for r in roots:
            if p == r or p.startswith(r if r.endswith(_sep) else r + _sep):
                return True
        return False

    def _audit(event, args, _reads=tuple(str(r) for r in read_roots), _scratch=(str(scratch),),
               _shown=str(scratch), _exact=block_exact, _prefix=block_prefix, _writes=write_events,
               _within=within, _perm=PermissionError, _isinstance=isinstance, _len=len, _any=any,
               _str=str, _bytes=bytes, _int=int, _pathlike=os.PathLike,
               _wmask=os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
        if event in _exact or event.startswith(_prefix):
            raise _perm(
                f"blocked by the RLM sandbox: {event}. This REPL has no network and no child "
                f"processes — use rlm()/rlm_map() for model calls, and don't shell out.")
        if event == "open":
            n = _len(args)
            path = args[0] if n > 0 else None
            mode = args[1] if n > 1 else None
            flags = args[2] if n > 2 else None
            if path is None or _isinstance(path, _int):
                return  # already-open fd; the originating open() was audited
            writing = _any(c in mode for c in "wax+") if _isinstance(mode, _str) else (
                (flags or 0) & _wmask) != 0
            if writing:
                if not _within(path, _scratch):
                    raise _perm(f"blocked by the RLM sandbox: write to {path!r}. "
                                f"Writes are confined to {_shown}.")
            elif not _within(path, _reads):
                raise _perm(f"blocked by the RLM sandbox: read of {path!r}. "
                            f"Reads are confined to the vault — the corpus is already "
                            f"in `docs`, and anything outside it is not yours to send.")
        elif event in _writes:
            for a in args:
                if _isinstance(a, (_str, _bytes, _pathlike)) and not _within(a, _scratch):
                    raise _perm(f"blocked by the RLM sandbox: {event} on {a!r}.")
        elif event in ("object.__getattr__", "object.__setattr__") and args and args[0] is _within:
            raise _perm(f"blocked by the RLM sandbox: {event} on the sandbox's own check.")

    return _audit


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
    sys.addaudithook(_make_audit())
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
        except BaseException as e:
            # exit()/sys.exit()/KeyboardInterrupt from model code used to end this process and
            # with it the namespace holding every paid sub-agent result. It ends the step instead.
            err = (f"{type(e).__name__}{e.args!r}: exit() ends this step, not the REPL; "
                   f"reply FINAL when you are done")
        _send({"t": "result", "out": buf.getvalue(), "err": err})


if __name__ == "__main__":
    main()
