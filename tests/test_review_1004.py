"""Regressions for the review of 2026-10-04: four findings.

1. vault_push.py on a clone with no pre-commit hook ran the two scans on the index and then
   `git commit -- <paths>`, which reads those paths from the working tree again: an edit made while
   the scans ran was committed unscanned. The scans and the commit now use one snapshot index, so
   what is committed is what was scanned, and the later edit waits for the next run (which scans
   it). Here scan_pii.py is replaced by a script that appends a credential-shaped line to the note
   after scan_secrets.py has read it.
2. extract_notes.py and extract_skills.py write each item with the conversation's source_hash as
   they go. When a later item of the same pass failed, the items already written made the next run
   count the conversation as extracted, so the rest was never retried. A pass now marks the
   conversation pending before its first write, and a pending conversation is extracted again
   until a pass completes. The second write is made to fail once.
3. rlm.py started `claude -p --model M` for the root and every sub-agent: in the vault, with every
   tool and MCP server the user's settings allow, on prompts that carry vault text. It now uses
   the extractors' launch: no built-in tools, no MCP servers, no session file, a directory outside
   the vault. A CLI too old for those options falls back to the tool deny list, as extraction does.
4. extract_notes.machine_name kept only a-z, 0-9 and "-", so "мак" and "бокс" both became
   "machine" and two machines wrote one State/extracted/<kind>-machine.json. It now names a
   machine as state.py does.

Every model call goes to a fake `claude` first on PATH that never talks to anything.
"""
import json, os, shutil, subprocess, sys
from pathlib import Path
from _util import TOOLS_SRC, UTF8_STDIO, Checks, git, make_vault, run, stub, stub_path, write

KEY = "AK" + "IA" + "CONCURRENTEDIT00"      # assembled, so this file holds no credential shape
LATE_EDIT = ('from pathlib import Path\n'
             'p = Path(__file__).resolve().parent.parent / "10 Notes" / "tonight.md"\n'
             'with open(p, "a", encoding="utf-8", newline="\\n") as fh:\n'
             '    fh.write("aws_access_key_id = " + "AK" + "IA" + "CONCURRENTEDIT00" + "\\n")\n'
             'print("pii-scan: clean (staged changes).")\n')


def check_push_snapshot(c: Checks) -> None:
    v = make_vault()                                     # no hook: vault_push runs the scans itself
    write(v, "10 Notes/seed.md", "seed\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")
    write(v, "10 Notes/tonight.md", "a clean note\n")
    write(v, "tools/staged_by_hand.py", "x = 1\n")
    git(v, "add", "tools/staged_by_hand.py")
    (v / "tools" / "scan_pii.py").write_text(LATE_EDIT, encoding="utf-8")
    r = run(v, "vault_push.py", "--no-push")
    c.ok(r.returncode == 0 and "committed" in r.stdout, "1. vault_push --no-push commits on a clone with no hook",
         r.stdout + r.stderr)
    blob = git(v, "show", "HEAD:10 Notes/tonight.md", check=False).stdout
    c.ok("a clean note" in blob and KEY not in blob,
         "1. the commit holds the note as it was scanned, not the edit made while the scans ran", blob[-200:])
    files = git(v, "show", "--name-only", "--format=", "HEAD").stdout.split("\n")
    c.ok("10 Notes/tonight.md" in files and "tools/staged_by_hand.py" not in files
         and "tools/staged_by_hand.py" in git(v, "diff", "--cached", "--name-only").stdout,
         "1. a file staged by hand stays staged and out of that commit", str(files))
    st = git(v, "status", "--porcelain", "--", "10 Notes").stdout
    c.ok(KEY in (v / "10 Notes" / "tonight.md").read_text(encoding="utf-8") and st.startswith(" M"),
         "1. the later edit is left in the working tree, unstaged, for the next run", repr(st))
    shutil.copy2(TOOLS_SRC / "scan_pii.py", v / "tools" / "scan_pii.py")
    head = git(v, "rev-parse", "HEAD").stdout
    r = run(v, "vault_push.py", "--no-push")
    c.ok(r.returncode != 0 and "BLOCKED" in r.stdout and git(v, "rev-parse", "HEAD").stdout == head,
         "1. the next run scans that edit and blocks it", r.stdout + r.stderr)


TWO_ITEMS = ("import json, os, sys\n"
             "prompt = sys.stdin.read()\n"
             "with open(os.environ['STUB_CALLS'], 'a', encoding='utf-8') as fh:\n"
             "    fh.write('call\\n')\n"
             "if 'SKILL memory' in prompt:\n"
             "    print(json.dumps([\n"
             "        {'name': 'Rotate the heron ledger weekly', 'when_to_use': 'when the heron ledger grows',\n"
             "         'steps': 'archive it; start a new one', 'why': 'old entries slow the fold', 'tags': ['ops']},\n"
             "        {'name': 'Quarantine walrus exports before import', 'when_to_use': 'when a walrus export arrives',\n"
             "         'steps': 'copy it aside; validate the header', 'why': 'a bad header corrupts the table',\n"
             "         'tags': ['data']}]))\n"
             "else:\n"
             "    print(json.dumps([\n"
             "        {'title': 'Heron ledgers fold faster when archived weekly', 'tags': ['ops'], 'volatility': 'timeless',\n"
             "         'body': 'Archiving keeps the fold short. It also bounds memory.'},\n"
             "        {'title': 'Walrus exports need header validation before import', 'tags': ['data'],\n"
             "         'volatility': 'timeless', 'body': 'A malformed header shifts every column. Validate first.'}]))\n")
# The tool's own main(), with its writer failing on the second item of the pass.
FAIL_SECOND = UTF8_STDIO + ("import sys\n"
                            "sys.path.insert(0, 'tools')\n"
                            "import {mod} as m\n"
                            "real, n = m.{fn}, [0]\n"
                            "def flaky(*a, **k):\n"
                            "    n[0] += 1\n"
                            "    if n[0] == 2:\n"
                            "        raise OSError('disk full')\n"
                            "    return real(*a, **k)\n"
                            "m.{fn} = flaky\n"
                            "sys.argv = ['{mod}.py']\n"
                            "m.main()\n")


def check_partial_extraction(c: Checks) -> None:
    for mod, fn, state, outdir, first, second in (
            ("extract_notes", "write_atomic_note", ".extract_state.json", "10 Notes",
             "Heron ledgers fold faster when archived weekly.md", "Walrus exports need header validation before import.md"),
            ("extract_skills", "write_proposed_skill", ".extract_skills_state.json", "Skills/_proposed",
             "Rotate the heron ledger weekly.md", "Quarantine walrus exports before import.md")):
        v = make_vault()
        stub(v / "fakebin", "claude", TWO_ITEMS)
        calls = v / "calls.log"
        env = {**os.environ, "PATH": stub_path(v / "fakebin"), "STUB_CALLS": str(calls)}
        write(v, "40 Resources/Claude Conversations/Claude Code/demo/2026-09-01 herons (aaaaaaaa).md",
              "---\ntype: x\n---\n\n# herons\n\na talk about heron ledgers and walrus exports\n")
        n_calls = lambda: len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
        r1 = subprocess.run([sys.executable, "-c", FAIL_SECOND.format(mod=mod, fn=fn)], cwd=v, env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        c.ok(r1.returncode != 0 and (v / outdir / first).exists() and not (v / outdir / second).exists()
             and n_calls() == 1,
             f"2. {mod}: a pass whose second write fails keeps the first item and exits non-zero",
             f"rc={r1.returncode} {r1.stdout[-300:]} {r1.stderr[-300:]}")
        r2 = run(v, f"{mod}.py", env=env)
        c.ok(r2.returncode == 0 and (v / outdir / second).exists() and n_calls() == 2,
             f"2. {mod}: the next run extracts that conversation again and writes the item that was lost",
             f"rc={r2.returncode} calls={n_calls()} {r2.stdout[-300:]} {r2.stderr[-300:]}")
        st = json.loads((v / "tools" / state).read_text(encoding="utf-8")) if (v / "tools" / state).exists() else {}
        entry = next(iter(st.values()), {})
        c.ok(len(st) == 1 and "pending" not in entry and entry.get("sig"),
             f"2. {mod}: the completed pass is the checkpoint, with nothing left pending", str(st))
        r3 = run(v, f"{mod}.py", env=env)
        c.ok(r3.returncode == 0 and n_calls() == 2, f"2. {mod}: and a further run calls no model",
             f"rc={r3.returncode} calls={n_calls()} {r3.stdout[-300:]}")


# Logs how it was started, echoes a sub-agent's text back, and plays the root's replies in order.
# With STUB_OLD_CLI set it is a CLI from before --tools: that option is an argv error.
LAUNCH_STUB = ("import json, os, sys\n"
               "prompt = sys.stdin.read()\n"
               "with open(os.environ['STUB_LAUNCH_LOG'], 'a', encoding='utf-8') as fh:\n"
               "    fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
               "                         'mcp': os.environ.get('ENABLE_CLAUDEAI_MCP_SERVERS')}) + '\\n')\n"
               "if os.environ.get('STUB_OLD_CLI') and '--tools' in sys.argv:\n"
               "    sys.stderr.write(\"error: unknown option '--tools'\\n\")\n"
               "    sys.exit(1)\n"
               "if '===TEXT===' in prompt:\n"
               "    print('ECHO ' + prompt.split('===TEXT===\\n', 1)[1])\n"
               "else:\n"
               "    nf = os.environ['STUB_ROOT_N']\n"
               "    n = int(open(nf).read()) if os.path.exists(nf) else 0\n"
               "    open(nf, 'w').write(str(n + 1))\n"
               "    replies = json.loads(open(os.environ['STUB_REPLIES'], encoding='utf-8').read())\n"
               "    print(replies[min(n, len(replies) - 1)])\n")


def run_rlm(old_cli: bool) -> tuple[subprocess.CompletedProcess, list[dict], Path]:
    v = make_vault()
    write(v, "10 Notes/n.md", "a note\n")
    stub(v / "fakebin", "claude", LAUNCH_STUB)
    replies = ["```python\nprint('SUB', rlm('echo', 'T'))\n```", "FINAL\ndone"]
    (v / "replies.json").write_text(json.dumps(replies), encoding="utf-8")
    env = {**os.environ, "PATH": stub_path(v / "fakebin"), "STUB_LAUNCH_LOG": str(v / "launch.log"),
           "STUB_ROOT_N": str(v / "root_n"), "STUB_REPLIES": str(v / "replies.json")}
    env.pop("ENABLE_CLAUDEAI_MCP_SERVERS", None)
    if old_cli:
        env["STUB_OLD_CLI"] = "1"
    r = subprocess.run([sys.executable, str(v / "tools" / "rlm.py"), "--steps", "3", "--root-model", "root-test",
                        "--sub-model", "sub-test", "q"], cwd=v, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    log = v / "launch.log"
    seen = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return r, seen, v


def check_rlm_launch(c: Checks) -> None:
    r, seen, v = run_rlm(old_cli=False)
    out = r.stdout + r.stderr
    vault = v.resolve()
    models = {s["argv"][s["argv"].index("--model") + 1] for s in seen if "--model" in s["argv"][:-1]}
    c.ok(r.returncode == 0 and "SUB ECHO T" in out and "\ndone\n" in r.stdout and models == {"root-test", "sub-test"},
         "3. rlm.py still runs its root and its sub-agent through claude", f"rc={r.returncode} {out[-500:]}")

    def no_tools(a: list[str]) -> bool:
        return "--tools" in a[:-1] and a[a.index("--tools") + 1] == ""

    c.ok(bool(seen) and all(no_tools(s["argv"]) and "--strict-mcp-config" in s["argv"]
                            and "--no-session-persistence" in s["argv"] for s in seen),
         "3. every claude it starts has no built-in tools, no MCP servers and no session file",
         str([s["argv"] for s in seen]))
    c.ok(bool(seen) and all(s["mcp"] == "false" for s in seen),
         "3. ...and claude.ai connectors are off in its environment", str([s["mcp"] for s in seen]))
    outside = lambda d: Path(d).resolve() != vault and vault not in Path(d).resolve().parents
    c.ok(bool(seen) and all(outside(s["cwd"]) for s in seen),
         "3. ...and runs outside the vault, where no CLAUDE.md or project settings are read",
         str([s["cwd"] for s in seen]))

    r, seen, v = run_rlm(old_cli=True)
    out = r.stdout + r.stderr
    passed = [s["argv"] for s in seen if "--tools" not in s["argv"]]
    c.ok(r.returncode == 0 and "SUB ECHO T" in out and "\ndone\n" in r.stdout and passed
         and all("--disallowedTools" in a and "Bash" in a[a.index("--disallowedTools") + 1] for a in passed),
         "3. a CLI without --tools is run with the tool deny list instead, and the run completes",
         f"rc={r.returncode} {[s['argv'] for s in seen]} {out[-400:]}")


NAMES = ["мак", "бокс", "Mac_Book Pro", "box-2", "--", "日本"]
NAME_DRIVER = UTF8_STDIO + ("import json, os, sys\n"
                            "sys.path.insert(0, 'tools')\n"
                            "import extract_notes as en, state\n"
                            "out = []\n"
                            f"for name in {NAMES!r}:\n"
                            "    os.environ['PALIMPSEST_MACHINE'] = name\n"
                            "    out.append([name, en.machine_name(), state._machine(), en.Ledger('notes').path.name])\n"
                            "print(json.dumps(out))\n")


def check_machine_names(c: Checks) -> None:
    v = make_vault()
    r = subprocess.run([sys.executable, "-c", NAME_DRIVER], cwd=v, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        rows = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        rows = []
    got = {name: mine for name, mine, _, _ in rows}
    c.ok(len(rows) == len(NAMES) and all(mine == theirs for _, mine, theirs, _ in rows),
         "4. extract_notes names a machine exactly as state.py does", f"{rows} {r.stderr[-300:]}")
    c.ok(got.get("мак") == "мак" and got.get("бокс") == "бокс" and len(set(got.values())) == len(NAMES),
         "4. two Cyrillic machine names stay two names", str(got))
    c.ok(bool(rows) and all(path == f"notes-{mine}.json" for _, mine, _, path in rows)
         and got.get("Mac_Book Pro") == "mac-book-pro" and got.get("box-2") == "box-2",
         "4. each machine writes its own ledger file, and an ASCII name keeps the file it had", str(rows))


def main() -> int:
    c = Checks("review 2026-10-04")
    check_push_snapshot(c)
    check_partial_extraction(c)
    check_rlm_launch(c)
    check_machine_names(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
