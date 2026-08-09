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

VAULT = Path(__file__).resolve().parent.parent
TOOLS = VAULT / "_tools"
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


def _claude(prompt: str, model: str, timeout: int) -> str:
    exe = shutil.which("claude") or "claude"
    env = {**os.environ, "CLAUDE_BRAIN_NO_HOOK": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run([exe, "-p", "--model", model], input=prompt, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[sub-agent timed out after {timeout}s]"
    if p.returncode != 0:
        return f"[claude CLI failed rc={p.returncode}: {(p.stderr or '').strip()[:300]}]"
    return (p.stdout or "").strip()


def _fanout(calls: list[dict], sub_model: str) -> list[str]:
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        futs = [ex.submit(_claude,
                          f"{c['prompt']}\n\n===TEXT===\n{c.get('text','')}",
                          c.get("model") or sub_model, SUB_TIMEOUT)
                for c in calls]
        return [f.result() for f in futs]


def _clip(s: str, n: int) -> str:
    s = s.rstrip()
    if len(s) <= n:
        return s
    return s[: n // 2] + f"\n…[{len(s)-n} chars elided]…\n" + s[-n // 2:]


def _code_of(reply: str) -> str | None:
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", reply, re.S)
    return m.group(1) if m else None


def main() -> None:
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

    worker = subprocess.Popen(
        [sys.executable, "-u", str(TOOLS / "rlm_worker.py"), str(VAULT), str(SCRATCH),
         str(args.subagents)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def w_send(obj) -> None:
        worker.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        worker.stdin.flush()

    def w_recv() -> dict:
        line = worker.stdout.readline()
        if not line:
            raise RuntimeError("REPL worker died")
        return json.loads(line)

    ready = w_recv()
    ndocs = ready["docs"]
    rec({"t": "start", "question": question, "docs": ndocs, "root": args.root_model,
         "sub": args.sub_model, "steps": args.steps, "subagents": args.subagents})
    print(f"corpus: {ndocs} docs · root {args.root_model} · subs {args.sub_model} "
          f"(≤{args.subagents}) · trace {trace.relative_to(VAULT)}\n")

    transcript: list[str] = []
    subs_used = 0
    answer = None
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
        reply = _claude(prompt, args.root_model, SUB_TIMEOUT)
        code = _code_of(reply)
        if code is None:
            answer = re.sub(r"^\s*FINAL\s*\n", "", reply.strip(), flags=re.I)
            rec({"t": "final", "step": step, "answer": answer})
            break
        print(f"── step {step} ──")
        print(textwrap.indent(_clip(code, 900), "  "))
        rec({"t": "code", "step": step, "code": code})

        w_send({"t": "exec", "code": code})
        while True:  # service brokered sub-agent fan-outs until the step finishes
            msg = w_recv()
            if msg["t"] == "rlm":
                calls = msg["calls"]
                print(f"   → {len(calls)} sub-agent(s)…", flush=True)
                results = _fanout(calls, args.sub_model)
                subs_used += len(calls)
                rec({"t": "subagents", "step": step, "n": len(calls),
                     "prompts": [c["prompt"][:200] for c in calls],
                     "chars_in": sum(len(c.get("text", "")) for c in calls),
                     "results": [r[:2000] for r in results]})
                w_send({"t": "rlm_result", "results": results})
                continue
            break
        out = (msg.get("out") or "") + (("\n" + msg["err"]) if msg.get("err") else "")
        shown = _clip(out.strip(), 6000)
        print(textwrap.indent(shown or "(no output)", "  ") + "\n")
        rec({"t": "output", "step": step, "out": out[:20000]})
        transcript.append(f"=== STEP {step} CODE ===\n{code}\n=== STEP {step} OUTPUT ===\n{shown}")
    else:
        answer = "[stopped: step budget exhausted before the root model produced an answer]"

    w_send({"t": "exit"})
    try:
        worker.wait(timeout=10)
    except subprocess.TimeoutExpired:
        worker.kill()

    print("=" * 70 + "\n" + (answer or "(no answer)") + "\n" + "=" * 70)
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


if __name__ == "__main__":
    main()
