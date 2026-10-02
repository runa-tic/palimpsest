#!/usr/bin/env python3
"""RLM — ask the brain a question that does not fit in a context window.

`ask.py` is one hop: score every note by keyword, feed the top 8 (truncated to 8k each)
to one model, answer. That works for lookups and fails for synthesis, because the choice
of what to read is made by lexical overlap before anything has been read.

This is the other shape, borrowed from Prime Intellect's Prime Agent (2026-08-06): the
corpus stays a *variable* in a persistent Python REPL, and a root model that never sees
the corpus writes code to slice it and delegates the reading to sub-agents it calls as
functions. Context cost stays flat in corpus size; only sub-agent count grows.

Nothing here is self-modifying: the harness state is fixed, sub-agents are stateless, and
every promotion of a finding into the vault stays a human's call.
See [[A self-improving harness is a proposal queue with the promotion gate deleted]].

Usage (from vault root):
  python tools/rlm.py "how has my thinking on approval gates changed over time?"
  python tools/rlm.py --steps 16 --subagents 60 --log "audit every uncommitted deploy claim"

No API key: sub-agents are `claude -p` under your Claude Code login, like ask.py.
"""
from __future__ import annotations
import sys, os, re, json, argparse, subprocess, shutil, textwrap
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

TOOLS = Path(__file__).resolve().parent   # derived, never spelled: this was "_tools", the layout
VAULT = TOOLS.parent                       # of the vault it came from, and the worker never started
SCRATCH = TOOLS / ".rlm_scratch"
LOGDIR = TOOLS / "logs" / "rlm"
ROOT_MODEL = "claude-opus-5"
SUB_MODEL = "claude-sonnet-5"
PARALLEL = 6            # concurrent `claude -p` sub-agents
SUB_TIMEOUT = 300       # seconds per sub-agent
EXEC_TIMEOUT = 240      # seconds per REPL step (excluding brokered sub-agent time)

CONTRACT = """\
You are the ROOT of a Recursive Language Model over a personal Obsidian vault ("second brain").

You cannot see the corpus. It is a variable in a persistent Python REPL you drive. You answer
by writing code that slices that variable and hands the reading to sub-agents.

NAMESPACE (re, json, math, statistics, collections, Counter, defaultdict, Path preloaded)
  docs                       Corpus over the vault ({ndocs} markdown files)
  docs.paths / docs.titles   list[str]
  docs.text(key)             full text; key = index | vault-relative path | note title
  docs.search(q, top=20)     -> Corpus   keyword-ranked subset (the scorer ask.py uses)
  docs.grep(rx, ctx=0)       -> list[{title,path,line,text}]  regex, case-insensitive
  docs.under(*prefixes)      -> Corpus   e.g. docs.under("10 Notes", "Skills")
  docs.select(keys)          -> Corpus ;  docs.filter(fn(path, text)) -> Corpus
  docs.sample(n)             -> Corpus   evenly spaced
  docs.chunks(size=60000)    -> list[str]  packed, each doc labelled "### NOTE: <title>"
  rlm(prompt, text="")       -> str        ONE sub-agent
  rlm_map(prompt, texts)     -> list[str]  sub-agents IN PARALLEL, one per text
  budget()                   -> dict       sub-agent calls used / left

RULES
1. Reply with exactly ONE ```python block per turn, or with your final answer. The namespace
   persists between turns, so build state up across steps.
2. Print at most ~2000 characters per turn. You are the coordinator, not a reader: print
   counts, samples and aggregates. Never print raw note bodies — that is what sub-agents are for.
3. Sub-agents are stateless and see ONLY the prompt and text you pass. Make each instruction
   self-contained, and tell them to answer "NONE" when a chunk holds nothing relevant, so you
   can drop it cheaply. Citations must copy the "### NOTE: <title>" label VERBATIM: vault
   filenames are truncated at 90 characters, so a title taken from the note's own "# " heading
   is often longer than the file and will not resolve as a [[wikilink]]. Pass that rule down
   to every sub-agent, and never lengthen a title yourself when writing the final answer.
4. Fan out with rlm_map rather than looping rlm(). Check budget() before a big batch.
5. The corpus is already in `docs`. Reads outside the vault, all writes, child processes and
   the network are blocked — `rlm()` is the only way out of this process. Do not shell out.
6. When you have the answer, reply with NO code block: the single word FINAL on its own line,
   then the answer in prose, citing sources inline as [[Note Title]]. State plainly what you
   could not determine rather than filling the gap.

QUESTION: {question}
"""


class ClaudeError(RuntimeError):
    """`claude -p` failed or timed out. Raised, not returned as text: the root loop used to take
    "[claude CLI failed rc=1: ...]" for a FINAL answer, log it to the Q&A log and exit 0."""


# A model name goes on the claude command line, and model code picks it: rlm(..., model=...) went
# into argv unchecked (review, 2026-10-02). Only what a model id is made of; never a leading "-",
# which could read as an option.
_MODEL_NAME = re.compile(r"[A-Za-z0-9._:\[\]-]{1,100}")
_BATCH_META = frozenset('&|<>^%"!()\r\n')


def _model_problem(model) -> str | None:
    """Why `model` may not go on the claude command line, or None when it may."""
    if isinstance(model, str) and _MODEL_NAME.fullmatch(model) and not model.startswith("-"):
        return None
    return (f"refused model name {repr(model)[:120]}: a model name is a str of 1-100 characters "
            f"from A-Z a-z 0-9 . _ : [ ] - that does not start with '-'")


def _batch_guard(argv: list[str]) -> None:
    """Refuse to launch a .cmd/.bat with an argument cmd.exe would parse. On Windows claude is
    npm's claude.cmd, which CreateProcess runs through cmd.exe, and CPython does not escape
    arguments for a batch file, so `x|calc` as a model name would run calc outside the sandbox
    (review, 2026-10-02). Model names are checked before this; it holds for any argument added later.
    argv[0] is the resolved path, not model input: "Program Files (x86)" is CPython's to quote."""
    if not str(argv[0]).lower().endswith((".cmd", ".bat")):
        return
    for a in argv[1:]:
        if any(ch in _BATCH_META for ch in a):
            raise ClaudeError(f"refused to run {Path(argv[0]).name} with argument {a[:120]!r}: "
                              f"cmd.exe would interpret & | < > ^ % \" ! ( ) or a newline in it")


def _claude(prompt: str, model: str, timeout: int) -> str:
    bad = _model_problem(model)
    if bad:
        raise ClaudeError(bad)    # model=123 was a TypeError from Popen that ended the run
    exe = shutil.which("claude") or "claude"
    argv = [exe, "-p", "--model", model]
    _batch_guard(argv)
    env = {**os.environ, "CLAUDE_BRAIN_NO_HOOK": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"timed out after {timeout}s") from None
    if p.returncode != 0:
        raise ClaudeError(f"claude CLI failed rc={p.returncode}: {(p.stderr or '').strip()[:300]}")
    return (p.stdout or "").strip()


def _sub(prompt: str, model: str, timeout: int) -> str:
    # A failed sub-agent is a result the root can see and work around, so it stays text here.
    try:
        return _claude(prompt, model, timeout)
    except ClaudeError as e:
        return f"[sub-agent {e}]"


def _fanout(calls: list[dict], sub_model: str) -> list[str]:
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        futs = [ex.submit(_sub,
                          f"{c['prompt']}\n\n===TEXT===\n{c.get('text','')}",
                          c.get("model") or sub_model, SUB_TIMEOUT)
                for c in calls]
        return [f.result() for f in futs]


def _clip(s: str, n: int) -> str:
    s = s.rstrip()
    if len(s) <= n:
        return s
    return s[: n // 2] + f"\n…[{len(s)-n} chars elided]…\n" + s[-n // 2:]


_FINAL = re.compile(r"\A\s*FINAL[ \t]*(?:\n|\Z)", re.I)
_FENCE = re.compile(r"```[ \t]*([\w.+-]*)[ \t]*\n(.*?)```", re.S)   # fences paired left to right
_PYTAG = re.compile(r"(?:python|py)[\d.]*", re.I)


def _code_of(reply: str) -> str | None:
    """The step's code, or None when the reply is the answer. FINAL is checked first: the fence
    used to be matched before it, so a FINAL quoting a command in a plain ``` block was executed as
    REPL code and the answer thrown away (review, 2026-09-30). Without FINAL a python/py-tagged
    fence is the code, and failing that an untagged one, as before: a code step whose fence
    lacked the tag was otherwise taken for the answer and, with --log, appended to the Q&A log
    (review, 2026-09-30). A fence tagged with another language is not code."""
    if _FINAL.match(reply):
        return None
    fences = [(m.group(1), m.group(2)) for m in _FENCE.finditer(reply)]
    for want in (lambda t: _PYTAG.fullmatch(t), lambda t: t == ""):
        for tag, body in fences:
            if want(tag):
                return body
    return None


_LC_DYLIB = {0xC, 0x20, 0x80000018, 0x8000001F, 0x80000023}   # load, lazy, weak, reexport, upward
_LC_RPATH = 0x8000001C


def _macho_links(path: Path) -> tuple[list[str], list[str]]:
    """(dylib install names, rpaths) from a Mach-O file's load commands, thin or universal.
    Header parsing only; anything that is not Mach-O gives two empty lists."""
    names, rpaths = [], []
    le = lambda b: int.from_bytes(b, "little")
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
            magic = int.from_bytes(head[:4], "big") if len(head) == 8 else 0
            offsets = [0]
            if magic in (0xCAFEBABE, 0xCAFEBABF):
                n = int.from_bytes(head[4:8], "big")
                if n > 16:           # a Java class file shares the magic; its "count" is a version
                    return names, rpaths
                size = 20 if magic == 0xCAFEBABE else 32
                tbl = fh.read(n * size)
                offsets = [int.from_bytes(tbl[i * size + 8: i * size + (12 if size == 20 else 16)], "big")
                           for i in range(n)]
            for off in offsets:
                fh.seek(off)
                h = fh.read(32)
                hsz = {0xFEEDFACF: 32, 0xFEEDFACE: 28}.get(le(h[:4]) if len(h) == 32 else 0)
                if not hsz:
                    continue
                ncmds, sizeofcmds = le(h[16:20]), le(h[20:24])
                fh.seek(off + hsz)
                cmds = fh.read(min(sizeofcmds, 1 << 20))
                pos = 0
                for _ in range(ncmds):
                    if pos + 12 > len(cmds):
                        break
                    cmd, cs = le(cmds[pos:pos + 4]), le(cmds[pos + 4:pos + 8])
                    if cs < 12:
                        break
                    if cmd in _LC_DYLIB or cmd == _LC_RPATH:
                        s = cmds[pos + le(cmds[pos + 8:pos + 12]):pos + cs].split(b"\0", 1)[0]
                        (names if cmd != _LC_RPATH else rpaths).append(s.decode("utf-8", "replace"))
                    pos += cs
    except OSError:
        pass
    return names, rpaths


def _linked_lib_dirs() -> set[Path]:
    """Directories holding the dylibs the interpreter and its stdlib extension modules load, found
    by walking their Mach-O load commands. A venv or pyenv Python on Homebrew links sqlite, xz,
    openssl and mpdecimal from /opt/homebrew/opt/*, outside every sysconfig path, so the allow-list
    profile broke `import sqlite3/lzma/ssl` there (review, 2026-09-30); a plain Homebrew Python
    only worked because its "data" path is the whole of /opt/homebrew. System libraries
    (/usr/lib, /System) are allowed already and not followed."""
    import sysconfig
    exe = Path(sys.executable).resolve()
    seeds = [exe]
    # In a venv sysconfig's platstdlib is the venv's own (empty) lib dir, so take the extension
    # dir from sys.path and from the build config instead.
    dyn = {Path(x) for x in sys.path if x and Path(x).name == "lib-dynload"}
    if sysconfig.get_config_var("DESTSHARED"):
        dyn.add(Path(sysconfig.get_config_var("DESTSHARED")))
    for d in sorted(dyn):
        if d.is_dir():
            seeds += sorted(d.glob("*.so"))
    seen, dirs, todo = set(), set(), list(seeds)
    while todo and len(seen) < 500:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        names, rpaths = _macho_links(f)
        subst = lambda x: x.replace("@loader_path", str(f.parent)).replace("@executable_path", str(exe.parent))
        rpaths = [subst(r) for r in rpaths]
        for name in names:
            cands = ([r + name[len("@rpath"):] for r in rpaths] if name.startswith("@rpath/")
                     else [subst(name)])
            for c in cands:
                if not c.startswith("/") or c.startswith(("/usr/lib/", "/System/")):
                    continue
                p = Path(c)
                if not p.exists():
                    continue
                rp = p.resolve()
                dirs.update({p.parent, rp.parent})
                todo.append(rp)
    return dirs


def _os_sandbox(cmd: list[str], scratch: Path) -> tuple[list[str], str]:
    """Wrap the REPL worker in an OS-enforced sandbox where one is available.

    The worker's audit hook runs inside the same interpreter as the model-written code, so it is
    defence in depth, not a boundary: an external review (2026-09-30) rebound the hook's allowed
    roots from generated code and read a file outside the vault, which rlm() could then send out.
    On macOS the kernel enforces it instead (sandbox-exec): no network, no writes outside scratch,
    and reads confined to the vault, the scratch dir, the Python install (prefix, stdlib and
    site-packages), the directories of the dylibs it links, and the system files an interpreter
    needs. That is an allow-list: the first profile only denied /Users and /Volumes, which left
    /private/tmp and $TMPDIR (other sessions' scratch and task output), /etc and /opt readable
    once the hook was bypassed (review, 2026-09-30). sysconfig's "data" path is not a root: on
    Homebrew Python it is /opt/homebrew itself, etc/ and var/ included. Outside home, stat()
    metadata stays allowed (as before) so path resolution works; contents do not.
    Elsewhere the worker runs with the in-process hook only, and says so. RLM_OS_SANDBOX=0 opts out."""
    if os.environ.get("RLM_OS_SANDBOX") == "0":
        return cmd, "OS sandbox disabled (RLM_OS_SANDBOX=0): in-process read/write checks only"
    exe = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
    if not exe:
        return cmd, (f"no OS sandbox on {sys.platform}: read confinement is the in-process hook only, "
                     f"which model-written code in the same interpreter could defeat")
    import sysconfig
    roots = {VAULT.resolve(), scratch.resolve(), Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
             Path(sys.executable).resolve().parent}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            roots.add(Path(sysconfig.get_path(key)).resolve())
        except Exception:
            pass
    libdirs = _linked_lib_dirs()
    q = lambda x: '"' + str(x).replace("\\", "\\\\").replace('"', '\\"') + '"'
    ancestors = {a for r in roots | libdirs for a in r.parents}
    # What dyld, libSystem and the interpreter read outside the Python install: the root listing,
    # system libraries and frameworks, devices, and the local timezone. Nothing user-writable.
    system = ['(literal "/")', '(subpath "/System")', '(subpath "/usr/lib")', '(subpath "/usr/share")',
              '(subpath "/dev")', '(literal "/private/etc/localtime")', '(subpath "/private/var/db/timezone")',
              '(subpath "/private/var/db/dyld")']
    allowed = system + [f"(subpath {q(r)})" for r in sorted(roots | libdirs)]
    profile = "\n".join([
        "(version 1)", "(allow default)", "(deny network*)",
        '(deny file-read* (subpath "/"))',
        '(allow file-read-metadata (subpath "/"))',
        '(deny file-read-metadata (subpath "/Users") (subpath "/Volumes"))',
        "(allow file-read* " + " ".join(allowed) + ")",
        # Re-allow metadata under the same roots: the /Users deny above otherwise wins for
        # file-read-metadata even after the file-read* allow, so with the vault under /Users
        # (the usual place on a Mac) every stat() inside it failed EPERM, `import ask` missed,
        # and the worker died at startup. The tests kept their vaults in $TMPDIR and never saw it.
        "(allow file-read-metadata " + " ".join(allowed) + ")",
        # path resolution stats every parent; allow that metadata, never their contents
        "(allow file-read-metadata " + " ".join(f"(literal {q(a)})" for a in sorted(ancestors)) + ")",
        '(deny file-write* (subpath "/"))',
        f'(allow file-write* (subpath {q(scratch.resolve())}) (literal "/dev/null") (literal "/dev/tty") '
        r'(regex #"^/dev/fd/"))',
    ])
    return [exe, "-p", profile, *cmd], ("OS sandbox: sandbox-exec (no network; reads confined to the vault, "
                                        "the Python install, its linked libraries and system libraries; "
                                        "writes to scratch)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Recursive-LM query over the vault.")
    ap.add_argument("question", nargs="+")
    ap.add_argument("--steps", type=int, default=12, help="max REPL turns for the root model")
    ap.add_argument("--subagents", type=int, default=40, help="max sub-agent calls")
    ap.add_argument("--root-model", default=ROOT_MODEL)
    ap.add_argument("--sub-model", default=SUB_MODEL)
    ap.add_argument("--log", action="store_true", help="append the answer to Brain Q&A Log.md")
    ap.add_argument("--verbose", action="store_true", help="echo each step's code and output")
    args = ap.parse_args()
    question = " ".join(args.question)
    for flag, model in (("--root-model", args.root_model), ("--sub-model", args.sub_model)):
        if bad := _model_problem(model):
            ap.error(f"{flag}: {bad}")

    SCRATCH.mkdir(parents=True, exist_ok=True)
    LOGDIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower())[:48].strip("-")
    trace = LOGDIR / f"{stamp}-{slug}.jsonl"

    def rec(obj) -> None:
        with trace.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    wcmd, sandbox_note = _os_sandbox([sys.executable, "-u", str(TOOLS / "rlm_worker.py"), str(VAULT),
                                      str(SCRATCH), str(args.subagents)], SCRATCH)
    print(f"rlm: {sandbox_note}", file=sys.stderr)
    worker = subprocess.Popen(
        wcmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})

    def w_send(obj) -> None:
        worker.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        worker.stdin.flush()

    # A reader thread feeds a queue so every wait has a deadline (select() does not work on
    # Windows pipes). readline() alone blocked forever: EXEC_TIMEOUT was declared and never
    # enforced, so one `while True: pass` from the model hung the query (review, 2026-09-30).
    import queue, threading, time
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in iter(worker.stdout.readline, "")] + [lines.put("")],
                     daemon=True).start()

    def w_recv(timeout: float) -> dict:
        try:
            line = lines.get(timeout=max(timeout, 0.1))
        except queue.Empty:
            raise TimeoutError from None
        if not line:
            raise RuntimeError("REPL worker died")
        return json.loads(line)

    try:
        ready = w_recv(120)     # corpus load + handshake
        ndocs = ready["docs"]
    except (TimeoutError, RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
        # A worker that dies (or hangs) before its handshake escaped as a traceback; the step
        # loop's handling never covered it. End the same way: a record, a message, exit 1.
        died = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        worker.kill()
        rec({"t": "worker_died", "step": 0, "error": died})
        print(f"rlm: the REPL worker did not start ({died}); its stderr is above. "
              f"RLM_OS_SANDBOX=0 tells a sandbox fault from a worker bug.", file=sys.stderr)
        return 1
    rec({"t": "start", "question": question, "docs": ndocs, "root": args.root_model,
         "sub": args.sub_model, "steps": args.steps, "subagents": args.subagents})
    print(f"corpus: {ndocs} docs · root {args.root_model} · subs {args.sub_model} "
          f"(≤{args.subagents}) · trace {trace.relative_to(VAULT)}\n")

    transcript: list[str] = []
    subs_used = 0
    answer = None     # only ever a FINAL the root model wrote
    stopped = None    # why the run ended without one; never logged as an answer
    for step in range(1, args.steps + 1):
        # literal substitution, not .format(): the contract shows dict literals like
        # {title,path,line,text}, which str.format would try to interpret as fields.
        prompt = CONTRACT.replace("{ndocs}", str(ndocs)).replace("{question}", question)
        if transcript:
            prompt += "\n\n===SESSION SO FAR===\n" + "\n".join(transcript)
        prompt += f"\n\n(step {step} of {args.steps}; sub-agent calls used {subs_used}/{args.subagents})\n"
        if step == args.steps:
            # Without this the last step is spent on another code block and the run ends with
            # nothing, having paid for every sub-agent it dispatched. Force the landing.
            prompt += ("\nThis is your LAST step. Do NOT write code. Reply with FINAL and the "
                       "best answer your gathered evidence supports, stating explicitly what "
                       "you could not determine.\n")
        reply, err = None, ""
        for attempt in (1, 2):      # one retry: a 529 or a slow turn is usually transient
            try:
                reply = _claude(prompt, args.root_model, SUB_TIMEOUT)
                if reply.strip():
                    break
                reply, err = None, "empty reply"
            except ClaudeError as e:
                err = str(e)
            print(f"   ✗ root model call failed at step {step} ({err})"
                  + ("; retrying" if attempt == 1 else ""), file=sys.stderr)
        if reply is None:
            stopped = f"[stopped: the root model call failed twice at step {step}: {err}]"
            rec({"t": "root_failed", "step": step, "error": err})
            break
        code = _code_of(reply)
        if code is None and not _FINAL.match(reply) and "```" in reply:
            # Neither FINAL nor a python block, but fenced: a step in the wrong language (```bash),
            # not an answer to log. Without a fence, prose with no FINAL is still the answer.
            rec({"t": "no_code", "step": step, "reply": reply[:2000]})
            print(f"── step {step} ──\n  (no ```python block and no FINAL; nothing run)\n")
            transcript.append(f"=== STEP {step} ===\nYour reply had no ```python block and no FINAL, so "
                              f"nothing ran. Only python runs here; reply FINAL when you have the answer.")
            continue
        if code is None:
            text = _FINAL.sub("", reply.strip(), count=1).strip()
            if text:
                answer = text
                rec({"t": "final", "step": step, "answer": answer})
                break
            # A bare FINAL used to be recorded as a final answer of "" and print "(no answer)".
            # It is a step with nothing in it: say so and let the next step carry the answer.
            rec({"t": "empty_final", "step": step})
            print(f"── step {step} ──\n  (FINAL with no answer text)\n")
            transcript.append(f"=== STEP {step} ===\nYou replied FINAL with no answer after it. "
                              f"Reply FINAL on its own line followed by the answer.")
            continue
        if step == args.steps:
            # The last step was told not to write code; running it anyway only spends time and
            # sub-agents on output nobody will read.
            rec({"t": "code_not_run", "step": step, "code": code})
            stopped = "[stopped: step budget exhausted before the root model produced an answer]"
            break
        print(f"── step {step} ──")
        print(textwrap.indent(_clip(code, 900), "  "))
        rec({"t": "code", "step": step, "code": code})

        deadline = time.monotonic() + EXEC_TIMEOUT
        timed_out, died = False, ""
        try:
            w_send({"t": "exec", "code": code})
            while True:  # service brokered sub-agent fan-outs until the step finishes
                try:
                    msg = w_recv(deadline - time.monotonic())
                except TimeoutError:
                    timed_out = True
                    break
                if msg["t"] == "rlm":
                    # The cap is enforced HERE, not only in the worker: model code can reach the
                    # worker's module globals (sys.modules['__main__']) and raise _BUDGET['limit'] or
                    # call _send itself, so a worker-side count is advisory against hostile code
                    # (review, 2026-09-30). Calls past --subagents are never run.
                    asked = list(msg.get("calls") or [])
                    room = max(0, args.subagents - subs_used)
                    calls, over = asked[:room], max(0, len(asked) - room)
                    print(f"   → {len(calls)} sub-agent(s)…" + (f" ({over} over budget, not run)" if over else ""),
                          flush=True)
                    t_sub = time.monotonic()
                    results = _fanout(calls, args.sub_model) if calls else []
                    results += [f"[NOT RUN — sub-agent budget exhausted, {over} call(s) dropped]"] * over
                    deadline += time.monotonic() - t_sub      # sub-agent time does not count
                    subs_used += len(calls)
                    rec({"t": "subagents", "step": step, "n": len(calls),
                         "prompts": [c["prompt"][:200] for c in calls],
                         "chars_in": sum(len(c.get("text", "")) for c in calls),
                         "results": [r[:2000] for r in results]})
                    w_send({"t": "rlm_result", "results": results})
                    continue
                break
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            # A dead worker (os._exit, OOM, a sandbox abort) or a garbled frame used to escape
            # as a traceback that lost the run: no stop record, no summary. It ends the run
            # the way a timeout does.
            died = f"{type(e).__name__}: {e}"
        if timed_out:
            worker.kill()
            stopped = (f"[stopped: step {step} ran past EXEC_TIMEOUT ({EXEC_TIMEOUT}s, excluding sub-agent "
                      f"time); the REPL worker was killed]")
            print(f"   ✗ step {step} exceeded {EXEC_TIMEOUT}s — worker killed")
            rec({"t": "timeout", "step": step, "limit_s": EXEC_TIMEOUT})
            break
        if died:
            worker.kill()
            stopped = f"[stopped: the REPL worker died during step {step} ({died})]"
            print(f"   ✗ REPL worker died during step {step} ({died})")
            rec({"t": "worker_died", "step": step, "error": died})
            break
        out = (msg.get("out") or "") + (("\n" + msg["err"]) if msg.get("err") else "")
        shown = _clip(out.strip(), 6000)
        print(textwrap.indent(shown or "(no output)", "  ") + "\n")
        rec({"t": "output", "step": step, "out": out[:20000]})
        transcript.append(f"=== STEP {step} CODE ===\n{code}\n=== STEP {step} OUTPUT ===\n{shown}")
    else:
        stopped = "[stopped: step budget exhausted before the root model produced an answer]"

    try:
        w_send({"t": "exit"})
        worker.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        worker.kill()
    try:
        worker.stdin.close()   # else a dead worker's unflushed "exit" frame errors again at shutdown
    except (OSError, ValueError):
        pass

    print("=" * 70 + "\n" + (answer or stopped or "(no answer)") + "\n" + "=" * 70)
    print(f"sub-agents: {subs_used}/{args.subagents} · trace: {trace.relative_to(VAULT)}")

    if args.log and answer:
        log = VAULT / "40 Resources" / "Brain Q&A Log.md"
        log.parent.mkdir(parents=True, exist_ok=True)
        if not log.exists():
            log.write_text("---\ntype: index\ntags:\n  - brain/qa\n---\n\n# 🧠 Brain Q&A Log\n",
                           encoding="utf-8")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## ❓ {question}\n*{datetime.now():%Y-%m-%d %H:%M} · rlm.py "
                     f"({subs_used} sub-agents)*\n\n{answer}\n")
        print(f"(logged to {log.relative_to(VAULT)})")
    # Non-zero when there is no answer, so a caller can tell a failed or cut-off run from one.
    return 0 if answer else 1


if __name__ == "__main__":
    sys.exit(main())
