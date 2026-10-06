"""Regressions for the review of 2026-10-06: four findings.

1. ask.py started `claude -p --model M` for its answer, and bench_retrieval.py --gen for its
   questions: in the directory they were run from (the vault), with every tool and MCP server the
   user's settings allow, on a prompt that carries retrieved notes and transcripts. Text inside a
   retrieved transcript could therefore ask for more reads, or for a tool the vault allows. Both
   now use the extractors' launch: no built-in tools, no MCP servers, no session file, a directory
   outside the vault; a CLI too old for those options falls back to the tool deny list.
2. extract_notes.py and extract_skills.py wrote each item straight to its final name. A write cut
   short (the disk filling, or sync.py's timeout killing the step) left a fragment there, and the
   retry, which never overwrites an existing item, kept the fragment and checkpointed the
   conversation as done. An item is now written to a temp file and renamed into place. The first
   item's write is cut after 80 characters, once by an error and once by killing the process.
3. Both extractors read and decoded a conversation outside the handler that skips a failed one, so
   a transcript that is not UTF-8 ended the run before any later conversation was looked at. It is
   now skipped and reported like any other failure. Here a UTF-16 and a cp1251 transcript sort
   before a valid one.
4. weekly_review.py matched a loop ticked in the review to the open task by its whole text. The
   briefing appends a mark to a task that has gone stale, so a loop ticked while the task had no
   mark came back unticked once it had one, and a task also open in a project note was listed
   twice. Tasks are now compared without the mark, and a ticked loop is shown without it.

With PALIMPSEST_TOOLS pointed at the tools from before this change, every check fails except those
marked "(held before)". Every model call goes to a fake `claude` first on PATH that never
talks to anything.
"""
import json, os, re, subprocess, sys
from datetime import date, timedelta
from pathlib import Path
from _util import UTF8_STDIO, Checks, make_vault, run, stub, stub_path, write

CONV = "40 Resources/Claude Conversations/Claude Code/demo/"
TALK = "---\ntype: x\n---\n\n# herons\n\na talk about heron ledgers and walrus exports\n"

# Logs how it was started, then answers ask.py, or bench_retrieval's GEN_PROMPT with one row.
# With STUB_OLD_CLI set it is a CLI from before --tools: that option is an argv error.
LAUNCH_STUB = ("import json, os, sys\n"
               "prompt = sys.stdin.buffer.read().decode('utf-8', 'replace')\n"
               "with open(os.environ['STUB_LAUNCH_LOG'], 'a', encoding='utf-8') as fh:\n"
               "    fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
               "                         'mcp': os.environ.get('ENABLE_CLAUDEAI_MCP_SERVERS')}) + '\\n')\n"
               "if os.environ.get('STUB_OLD_CLI') and '--tools' in sys.argv:\n"
               "    sys.stderr.write(\"error: unknown option '--tools'\\n\")\n"
               "    sys.exit(1)\n"
               "if 'retrieval benchmark' in prompt:\n"
               "    print(json.dumps([{'id': 0, 'en': 'how do striped animals hide', 'ru': 'kak polosatye pryachutsya'}]))\n"
               "else:\n"
               "    print('STUB-ANSWER')\n")


def launched(script: str, *args: str, old_cli: bool = False) -> tuple[subprocess.CompletedProcess, list[dict], Path]:
    v = make_vault()
    # bench_retrieval's gen() skips a note whose body is under 400 characters
    write(v, "10 Notes/Zebra.md", "# Zebra\n\n" + "Zebras have stripes that break up their outline. " * 12)
    stub(v / "fakebin", "claude", LAUNCH_STUB)
    env = {"PATH": stub_path(v / "fakebin"), "STUB_LAUNCH_LOG": str(v / "launch.log")}
    if old_cli:
        env["STUB_OLD_CLI"] = "1"
    full = {**os.environ, **env}
    full.pop("ENABLE_CLAUDEAI_MCP_SERVERS", None)
    r = subprocess.run([sys.executable, str(v / "tools" / script), *args], cwd=v, env=full, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=180)
    log = v / "launch.log"
    return r, [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else [], v


def no_tools(a: list[str]) -> bool:
    return "--tools" in a[:-1] and a[a.index("--tools") + 1] == ""


def check_launch(c: Checks) -> None:
    for what, script, args, answered in (
            ("ask.py", "ask.py", ("--mode", "lexical", "zebra stripes"),
             lambda r, v: "STUB-ANSWER" in r.stdout),
            ("bench_retrieval.py --gen", "bench_retrieval.py", ("--gen", "1"),
             lambda r, v: "how do striped animals hide" in (
                 (v / "tools" / "cache" / "bench-queries.jsonl").read_text(encoding="utf-8")
                 if (v / "tools" / "cache" / "bench-queries.jsonl").exists() else ""))):
        r, seen, v = launched(script, *args)
        vault = v.resolve()
        c.ok(r.returncode == 0 and answered(r, v) and len(seen) == 1,
             f"1. {what} still gets its answer from claude (held before)", f"rc={r.returncode} {(r.stdout + r.stderr)[-400:]}")
        c.ok(bool(seen) and all(no_tools(s["argv"]) and "--strict-mcp-config" in s["argv"]
                                and "--no-session-persistence" in s["argv"] and s["mcp"] == "false" for s in seen),
             f"1. {what}: the claude it starts has no built-in tools, no MCP servers or connectors, and no session file",
             str(seen))
        outside = lambda d: Path(d).resolve() != vault and vault not in Path(d).resolve().parents
        c.ok(bool(seen) and all(outside(s["cwd"]) for s in seen),
             f"1. {what}: ...and runs outside the vault, where no CLAUDE.md or project settings are read",
             str([s["cwd"] for s in seen]))
        r, seen, v = launched(script, *args, old_cli=True)
        passed = [s["argv"] for s in seen if "--tools" not in s["argv"]]
        c.ok(r.returncode == 0 and answered(r, v) and passed
             and all("--disallowedTools" in a and "Bash" in a[a.index("--disallowedTools") + 1] for a in passed),
             f"1. {what}: a CLI without --tools is run with the tool deny list instead, and still answers",
             f"rc={r.returncode} {[s['argv'] for s in seen]} {(r.stdout + r.stderr)[-300:]}")


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
KINDS = (("extract_notes", ".extract_state.json", "10 Notes",
          "Heron ledgers fold faster when archived weekly.md", "Walrus exports need header validation before import.md"),
         ("extract_skills", ".extract_skills_state.json", "Skills/_proposed",
          "Rotate the heron ledger weekly.md", "Quarantine walrus exports before import.md"))
# The tool's own main(), with the first write of a note or a skill cut after 80 characters: by an
# error (the disk filling) or by the process dying (sync.py's timeout). Every text file opened for
# writing goes through io.open, whether by Path.write_text, open() or os.fdopen, so the cut does
# not depend on how the tool writes.
CUT = UTF8_STDIO + ("import builtins, io, os, sys\n"
                    "sys.path.insert(0, 'tools')\n"
                    "real_open, fired = io.open, []\n"
                    "class Cut:\n"
                    "    def __init__(self, fh): self.fh = fh\n"
                    "    def __getattr__(self, name): return getattr(self.fh, name)\n"
                    "    def __enter__(self): return self\n"
                    "    def __exit__(self, *a): return self.fh.__exit__(*a)\n"
                    "    def write(self, s):\n"
                    "        if s.startswith('---\\ntype: ') and not fired:\n"
                    "            fired.append(1)\n"
                    "            self.fh.write(s[:80])\n"
                    "            self.fh.flush()\n"
                    "            if '{how}' == 'kill':\n"
                    "                os._exit(137)\n"
                    "            raise OSError(28, 'No space left on device')\n"
                    "        return self.fh.write(s)\n"
                    "def cut_open(file, mode='r', *a, **k):\n"
                    "    fh = real_open(file, mode, *a, **k)\n"
                    "    return Cut(fh) if 'w' in mode and 'b' not in mode else fh\n"
                    "io.open = builtins.open = cut_open\n"
                    "import {mod} as m\n"
                    "sys.argv = ['{mod}.py']\n"
                    "m.main()\n")


def whole(p: Path) -> bool:
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return "## Source" in text and text.rstrip().endswith("]]")


def check_cut_write(c: Checks) -> None:
    for mod, state, outdir, first, second in KINDS:
        for how, said in (("error", "fails partway"), ("kill", "is killed partway")):
            v = make_vault()
            stub(v / "fakebin", "claude", TWO_ITEMS)
            calls = v / "calls.log"
            env = {**os.environ, "PATH": stub_path(v / "fakebin"), "STUB_CALLS": str(calls)}
            write(v, CONV + "2026-09-01 herons (aaaaaaaa).md", TALK)
            n_calls = lambda: len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
            r1 = subprocess.run([sys.executable, "-c", CUT.format(mod=mod, how=how)], cwd=v, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            left = sorted(p.name for p in (v / outdir).glob("*")) if (v / outdir).exists() else []
            c.ok(r1.returncode != 0 and n_calls() == 1 and not left,
                 f"2. {mod}: a write that {said} leaves nothing under the item's name",
                 f"rc={r1.returncode} left={left} {r1.stdout[-300:]} {r1.stderr[-300:]}")
            r2 = run(v, f"{mod}.py", env=env)
            st = json.loads((v / "tools" / state).read_text(encoding="utf-8")) if (v / "tools" / state).exists() else {}
            entry = next(iter(st.values()), {})
            c.ok(r2.returncode == 0 and whole(v / outdir / first) and whole(v / outdir / second)
                 and len(st) == 1 and "pending" not in entry and bool(entry.get("sig")),
                 f"2. {mod}: ...and the next run writes both items whole before it checkpoints the conversation",
                 f"rc={r2.returncode} first={(v / outdir / first).read_text(encoding='utf-8')[:120] if (v / outdir / first).exists() else None!r} "
                 f"{st} {r2.stdout[-300:]} {r2.stderr[-300:]}")


def check_unreadable(c: Checks) -> None:
    for mod, state, outdir, first, second in KINDS:
        v = make_vault()
        stub(v / "fakebin", "claude", TWO_ITEMS)
        calls = v / "calls.log"
        env = {**os.environ, "PATH": stub_path(v / "fakebin"), "STUB_CALLS": str(calls)}
        (v / CONV).mkdir(parents=True)
        utf16 = v / CONV / "2026-09-01 a notepad save (aaaaaaaa).md"
        cp1251 = v / CONV / "2026-09-02 an old export (bbbbbbbb).md"
        utf16.write_bytes(TALK.encode("utf-16"))
        cp1251.write_bytes("# разговор\n\nцапля и морж\n".encode("cp1251"))
        write(v, CONV + "2026-09-03 herons (cccccccc).md", TALK)
        n_calls = lambda: len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
        r = run(v, f"{mod}.py", env=env)
        out = r.stdout + r.stderr
        c.ok(whole(v / outdir / first) and whole(v / outdir / second) and n_calls() == 1,
             f"3. {mod}: a conversation after two that are not UTF-8 is still extracted",
             f"rc={r.returncode} calls={n_calls()} {out[-400:]}")
        c.ok(r.returncode != 0 and "Traceback" not in out and "FAILED on 2 conversation(s)" in out
             and all(re.search(re.escape(p.name) + r"\n\s+! skipped \(not UTF-8", out) for p in (utf16, cp1251)),
             f"3. {mod}: ...and the run names the two it skipped and does not report itself clean",
             f"rc={r.returncode} {out[-500:]}")
        r = run(v, f"{mod}.py", env=env)
        c.ok(r.returncode != 0 and n_calls() == 1 and "FAILED on 2 conversation(s)" in r.stdout,
             f"3. {mod}: a further run reports them again and sends nothing to the model",
             f"rc={r.returncode} calls={n_calls()} {r.stdout[-300:]}")


TODAY = date.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()
PARKED, FRESH = "PARKED: raise the fleet throughput?", "a task first written yesterday"


def loops(v: Path) -> list[str]:
    """The Open loops lines of this week's review."""
    iso = TODAY.isocalendar()
    p = v / "Reviews" / "Weekly" / f"{iso[0]}-W{iso[1]:02d}.md"
    return [l for l in (p.read_text(encoding="utf-8") if p.exists() else "").splitlines() if l.startswith("- [")]


def tick(v: Path, needle: str) -> None:
    iso = TODAY.isocalendar()
    p = v / "Reviews" / "Weekly" / f"{iso[0]}-W{iso[1]:02d}.md"
    p.write_text("\n".join("- [x]" + l[5:] if l.startswith("- [ ] ") and needle in l else l
                           for l in p.read_text(encoding="utf-8").split("\n")), encoding="utf-8")


def check_weekly(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", json.dumps({"version": 1, "steps": {"pull": False}}))
    form = f"send the form by {D(-3)}"
    write(v, f"Daily/{D(-10)}.md", f"# ten days ago\n\n- [ ] {PARKED}\n")
    write(v, f"Daily/{D(-1)}.md", f"# yesterday\n\n- [ ] {PARKED}\n- [ ] {FRESH}\n- [ ] {form}\n")
    write(v, "20 Projects/Fleet.md", f"---\nstatus: active\n---\n\n# Fleet\n\n- [ ] {PARKED}\n")
    run(v, "weekly_review.py")
    tick(v, PARKED)
    tick(v, FRESH)
    r = run(v, "briefing.py")
    today = (v / "Daily" / f"{D(0)}.md").read_text(encoding="utf-8") if r.returncode == 0 else ""
    marked = f"- [ ] {PARKED} ⏰ *open since {D(-10)}*" in today and f"- [ ] {form} ⏰ *due {D(-3)} passed*" in today
    r = run(v, "weekly_review.py")
    ls = loops(v)
    parked = [l for l in ls if PARKED in l]
    c.ok(marked and len(ls) == 3 and len(parked) == 1 and parked[0].startswith(f"- [x] {PARKED}  <sub>"),
         "4. a loop ticked in the review stays ticked when the briefing marks its task as stale, listed once "
         "and without the mark",
         f"marked={marked} rc={r.returncode} {ls} {r.stderr[-200:]}")
    c.ok(any(l.startswith(f"- [x] {FRESH}  <sub>") for l in ls),
         "4. ...as does one whose task has no mark (held before)", str(ls))
    c.ok(any(l.startswith(f"- [ ] {form} ⏰ *due {D(-3)} passed*  <sub>") for l in ls),
         "4. ...and an open loop shows the mark its task carries (held before)", str(ls))
    tick(v, form)
    run(v, "weekly_review.py")
    ls = loops(v)
    c.ok(any(l.startswith(f"- [x] {form}  <sub>") for l in ls) and len(ls) == 3
         and sum(l.startswith("- [x] ") for l in ls) == 3,
         "4. a loop ticked while it shows a mark stays ticked and loses the mark", str(ls))


def main() -> int:
    c = Checks("review 2026-10-06: ask's launch, cut writes, unreadable transcripts, ticked loops")
    check_launch(c)
    check_cut_write(c)
    check_unreadable(c)
    check_weekly(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
