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


def _claude(prompt: str, model: str, timeout: int) -> str:
    exe = shutil.which("claude") or "claude"
    env = {**os.environ, "CLAUDE_BRAIN_NO_HOOK": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run([exe, "-p", "--model", model], input=prompt, capture_output=True,
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
_CODE = re.compile(r"```[ \t]*(?:python|py)[\d.]*[ \t]*\n(.*?)```", re.I | re.S)


def _code_of(reply: str) -> str | None:
    """The step's code, or None when the reply is the answer. FINAL is checked first and only a
    fence tagged python/py counts: the tag used to be optional, so a FINAL quoting a command in a
    plain ``` block was executed as REPL code and the answer thrown away (review, 2026-09-30)."""
    if _FINAL.match(reply):
        return None
    m = _CODE.search(reply)
    return m.group(1) if m else None


def _os_sandbox(cmd: list[str], scratch: Path) -> tuple[list[str], str]:
    """Wrap the REPL worker in an OS-enforced sandbox where one is available.

    The worker's audit hook runs inside the same interpreter as the model-written code, so it is
    defence in depth, not a boundary: an external review (2026-09-30) rebound the hook's allowed
    roots from generated code and read a file outside the vault, which rlm() could then send out.
    On macOS the kernel enforces it instead (sandbox-exec): no network, no reads under /Users or
    /Volumes except the vault, the scratch dir and the Python install, no writes outside scratch.
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
    for key in ("stdlib", "platstdlib", "purelib", "platlib", "data"):
        try:
            roots.add(Path(sysconfig.get_path(key)).resolve())
        except Exception:
            pass
    q = lambda x: '"' + str(x).replace("\\", "\\\\").replace('"', '\\"') + '"'
    ancestors = {a for r in roots for a in r.parents}
    profile = "\n".join([
        "(version 1)", "(allow default)", "(deny network*)",
        '(deny file-read* (subpath "/Users") (subpath "/Volumes"))',
        "(allow file-read* " + " ".join(f"(subpath {q(r)})" for r in sorted(roots)) + ")",
        # path resolution stats every parent; allow that metadata, never their contents
        "(allow file-read-metadata " + " ".join(f"(literal {q(a)})" for a in sorted(ancestors)) + ")",
        '(deny file-write* (subpath "/"))',
        f'(allow file-write* (subpath {q(scratch.resolve())}) (literal "/dev/null") (literal "/dev/tty") '
        r'(regex #"^/dev/fd/"))',
    ])
    return [exe, "-p", profile, *cmd], "OS sandbox: sandbox-exec (no network; reads confined to the vault)"


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

    ready = w_recv(120)     # corpus load + handshake
    ndocs = ready["docs"]
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
        if code is None:
            answer = _FINAL.sub("", reply.strip(), count=1).strip()
            rec({"t": "final", "step": step, "answer": answer})
            break
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
                    calls = msg["calls"]
                    print(f"   → {len(calls)} sub-agent(s)…", flush=True)
                    t_sub = time.monotonic()
                    results = _fanout(calls, args.sub_model)
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
