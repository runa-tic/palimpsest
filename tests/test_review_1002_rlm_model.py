"""Regressions for the 2026-10-02 review of rlm.py: a model name chosen by model code reached argv.

Model-written code in the REPL can pass model= to rlm()/rlm_map(), and rlm.py put it unchecked into
[claude, "-p", "--model", model]. On Windows claude resolves to npm's claude.cmd, which CreateProcess
runs through cmd.exe, and CPython does not escape arguments for a batch file, so a model name of
`x|calc` would run calc outside the sandbox; model=123 ended the run with an uncaught TypeError.

1. Hostile model names are refused before any process starts: the stub claude never sees them, the
   model code gets an error string back, and the run completes. A valid name still gets through.
2. model=123 is refused the same way instead of crashing the run.
3. An invalid --root-model / --sub-model stops rlm.py before it calls claude at all.
4. The batch-file guard refuses cmd.exe metacharacters in the arguments of a .cmd/.bat launch.

Checks 1-4 fail with PALIMPSEST_TOOLS pointed at tools/ from 60b4bff, except the valid-name check
in 1, which is the control and passes on both. Every model call goes to a fake `claude` first on
PATH that logs its argv and never talks to anything.
"""
import json, os, subprocess, sys
from pathlib import Path
import _util
from _util import TOOLS_SRC, Checks, make_vault, rmtree, stub, stub_path, write

# Logs its argv, echoes a sub-agent's text back, and plays the root model's replies in order.
CLAUDE_STUB = ("import json, os, sys\n"
               "prompt = sys.stdin.read()\n"
               "with open(os.environ['STUB_ARGV_LOG'], 'a', encoding='utf-8') as fh:\n"
               "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
               "if '===TEXT===' in prompt:\n"
               "    print('ECHO ' + prompt.split('===TEXT===\\n', 1)[1])\n"
               "else:\n"
               "    nf = os.environ['STUB_ROOT_N']\n"
               "    n = int(open(nf).read()) if os.path.exists(nf) else 0\n"
               "    open(nf, 'w').write(str(n + 1))\n"
               "    replies = json.loads(open(os.environ['STUB_REPLIES'], encoding='utf-8').read())\n"
               "    print(replies[min(n, len(replies) - 1)])\n")

HOSTILE = ["x|calc", 'a" & calc & "b', "%USERPROFILE%", "sonnet\n", "--dangerously-skip-permissions"]
REFUSED = "[sub-agent refused model name"

_MINE: list[Path] = []


def run_rlm(replies: list[str], *args: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    """rlm.py against the stub; returns the run and every argv the stub was started with."""
    v = make_vault()
    _MINE.append(v)
    write(v, "10 Notes/n.md", "a note\n")
    stub(v / "fakebin", "claude", CLAUDE_STUB)
    (v / "replies.json").write_text(json.dumps(replies), encoding="utf-8")
    env = {**os.environ, "PATH": stub_path(v / "fakebin"), "STUB_ARGV_LOG": str(v / "argv.log"),
           "STUB_ROOT_N": str(v / "root_n"), "STUB_REPLIES": str(v / "replies.json")}
    r = subprocess.run([sys.executable, str(v / "tools" / "rlm.py"), *args, "q"], cwd=v, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    log = v / "argv.log"
    seen = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return r, seen


def models(seen: list[list[str]]) -> list[str]:
    return [a[a.index("--model") + 1] for a in seen if "--model" in a[:-1]]


def check_hostile_names(c: Checks) -> None:
    code = (f"bad = {HOSTILE!r}\n"
            "res = [rlm('echo', 'T', model=m) for m in bad] + rlm_map('echo', ['M1', 'M2'], model='x|calc')\n"
            f"print('REFUSED', sum(r.startswith({REFUSED!r}) for r in res), 'OF', len(res))\n"
            "print('GOOD', rlm('echo', 'FINE', model='claude-haiku-5'))\n")
    r, seen = run_rlm([f"```python\n{code}```", "FINAL\ndone"], "--steps", "3",
                      "--root-model", "root-test", "--sub-model", "sub-test")
    out = r.stdout + r.stderr
    got = models(seen)
    c.ok(r.returncode == 0 and f"REFUSED {len(HOSTILE) + 2} OF {len(HOSTILE) + 2}" in out
         and "\ndone\n" in r.stdout and set(got) <= {"root-test", "claude-haiku-5"}
         and not any("calc" in a or "%" in a for argv in seen for a in argv),
         "hostile model names from rlm()/rlm_map() are refused to the model, never reach claude, and "
         "the run completes", f"rc={r.returncode} models={got!r} {out[-600:]}")
    c.ok("GOOD ECHO FINE" in out and got.count("claude-haiku-5") == 1,
         "a valid per-call model name still reaches claude", f"models={got!r} {out[-400:]}")


def check_int_model(c: Checks) -> None:
    code = ("r = rlm('echo', 'T', model=123)\n"
            f"print('INT', r.startswith({REFUSED!r}))\n"
            "print('AFTER', rlm('echo', 'STILL'))\n")
    r, seen = run_rlm([f"```python\n{code}```", "FINAL\ndone"], "--steps", "3",
                      "--root-model", "root-test", "--sub-model", "sub-test")
    out = r.stdout + r.stderr
    got = models(seen)
    c.ok(r.returncode == 0 and "INT True" in out and "AFTER ECHO STILL" in out and "Traceback" not in out
         and "\ndone\n" in r.stdout and sorted(set(got)) == ["root-test", "sub-test"],
         "model=123 is refused as a step result, not a TypeError that ends the run",
         f"rc={r.returncode} models={got!r} {out[-600:]}")


def check_cli_models(c: Checks) -> None:
    sub_call = "```python\nprint(rlm('echo', 'T'))\n```"
    r1, seen1 = run_rlm(["FINAL\ndone"], "--steps", "2", "--root-model", "x|calc")
    r2, seen2 = run_rlm([sub_call, "FINAL\ndone"], "--steps", "3", "--sub-model", "%USERPROFILE%")
    c.ok(r1.returncode != 0 and r2.returncode != 0 and not seen1 and not seen2
         and "--root-model" in r1.stderr and "--sub-model" in r2.stderr,
         "an invalid --root-model / --sub-model stops rlm.py before claude is ever started",
         f"rc1={r1.returncode} seen1={seen1!r} {r1.stderr[-300:]} | rc2={r2.returncode} seen2={seen2!r} "
         f"{r2.stderr[-300:]}")


GUARD = r'''
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location("rlm", sys.argv[1])
rlm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rlm)
out = []
for argv in json.loads(sys.stdin.read()):
    try:
        rlm._batch_guard(argv)
        out.append("ran")
    except rlm.ClaudeError:
        out.append("refused")
print("RESULT", json.dumps(out))
'''


NAMES = r'''
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location("rlm", sys.argv[1])
rlm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rlm)
print("RESULT", json.dumps([rlm._model_problem(m) is None for m in json.loads(sys.stdin.read())]))
'''


def check_model_names(c: Checks) -> None:
    """Real model ids pass, Vertex and Bedrock forms included; anything cmd.exe could read does not."""
    ok = ["claude-opus-5", "claude-sonnet-4-5-20250929", "claude-opus-4-6[1m]", "claude-sonnet-4-5@20250929",
          "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
          "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    bad = ["x|calc", 'a" & calc & "b', "%USERPROFILE%", "-p", "", "a b", "a\nb", "x" * 201]
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + NAMES, str(TOOLS_SRC / "rlm.py")],
                       input=json.dumps(ok + bad), capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), "")
    got = json.loads(line[7:]) if line else []
    c.ok(got == [True] * len(ok) + [False] * len(bad),
         "model ids pass, Vertex (@) and Bedrock ARN (/) forms included; metacharacters, options, "
         "spaces, newlines and 201 characters do not", f"{got} {r.stderr[-300:]}")


def check_batch_guard(c: Checks) -> None:
    """The guard itself, with fake exe paths: no batch file is run, so this holds on every OS."""
    npm = r"C:\fake\npm\claude.cmd"
    refused = [[npm, "-p", "--model", f"a{ch}b"] for ch in '&|<>^%"!()\n\r']
    refused += [[r"C:\fake\npm\claude.BAT", "-p", "--model", "a&b"], [r"C:\fake\npm\Claude.Cmd", "%PATH%"]]
    ran = [[npm, "-p", "--model", "claude-sonnet-5"],
           [r"C:\Program Files (x86)\nodejs\claude.cmd", "-p", "--model", "claude-sonnet-5"],
           [r"C:\fake\claude.exe", "-p", "--model", "a&b"], ["/usr/local/bin/claude", "-p", "--model", "x|y"]]
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + GUARD, str(TOOLS_SRC / "rlm.py")],
                       input=json.dumps(refused + ran), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), "")
    got = json.loads(line[len("RESULT "):]) if line else []
    c.ok(got == ["refused"] * len(refused) + ["ran"] * len(ran),
         "a .cmd/.bat launch is refused when an argument holds & | < > ^ % \" ! ( ) or a newline; "
         "the exe path and non-batch launches are not",
         f"got={got!r} {(r.stdout + r.stderr)[-400:]}")


def main() -> int:
    c = Checks("review 2026-10-02: rlm model names")
    try:
        check_hostile_names(c)
        check_int_model(c)
        check_cli_models(c)
        check_model_names(c)
        check_batch_guard(c)
    finally:
        for d in _MINE:
            rmtree(d)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
